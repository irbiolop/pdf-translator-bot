#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات تلگرام مترجم PDF انگلیسی → فارسی
--------------------------------------
- پولینگ طولانی تلگرام با requests (بدون وابستگی سنگین)
- ترجمه با translator_core (Groq) با حفظ چیدمان آینه‌ای و اندازهٔ یکنواخت
- پنل ادمین داخل ربات: سقف صفحه، کانال اجباری، سرعت ترجمه، آمار کاربران
- ذخیرهٔ وضعیت (تنظیمات/آمار) در مخزن GitHub تا با ری‌استارت از دست نرود
- وب‌سرور کوچک /health (برای استقرار روی Render)
- حالت GitHub Actions: با RUN_MAX_MINUTES قبل از سقف ۶ ساعتهٔ جاب،
  به‌صورت تمیز خارج می‌شود و workflow بعدی را خودش زنجیر می‌کند (۲۴/۷)
"""
from __future__ import annotations

import asyncio
import base64
import collections
import html as html_mod
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import pymupdf
import requests

import translator_core as core

# --------------------------------------------------------------------------
# تنظیمات از متغیرهای محیطی
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
OPENCODE_API_KEY = os.environ.get("OPENCODE_API_KEY", "").strip()
OC_BIN = os.environ.get("OPENCODE_BIN", "opencode").strip() or "opencode"
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
# MTProto (Telethon) برای دانلود/آپلود فایل‌های بزرگ‌تر از سقف Bot API
TG_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
TG_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
# کاربران مجاز اولیه (کاما جدا)؛ تازه‌کارها پیش‌فرض غیرمجازند تا ادمین تأیید کند
ALLOWED_ENV = tuple(int(x) for x in os.environ.get("ALLOWED_USERS", "").replace(";", ",").split(",") if x.strip().isdigit())
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GH_REPO = os.environ.get("GITHUB_REPO", "").strip()  # owner/name
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip() or "main"
DEFAULT_PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "40") or 40)
# سقف قابل تنظیم سقف صفحه در پنل ادمین (پیش‌فرض ۲۰۰۰؛ با متغیر MAX_PAGE_LIMIT عوض می‌شود)
MAX_PAGE_LIMIT = int(os.environ.get("MAX_PAGE_LIMIT", "2000") or 2000)
FORCE_JOIN_ENABLED = os.environ.get("FORCE_JOIN_ENABLED", "0") == "1"
FORCE_JOIN_CHANNEL = os.environ.get("FORCE_JOIN_CHANNEL", "").strip()
PORT = int(os.environ.get("PORT", "10000") or 10000)
# سقف زمان هر چرخه (دقیقه). روی GitHub Actions باید < سقف ۶ ساعتهٔ جاب باشد؛
# صفر یعنی بدون سقف (اجرای محلی/Render).
RUN_MAX_MINUTES = float(os.environ.get("RUN_MAX_MINUTES", "0") or 0)

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", core.DEFAULT_MODEL).strip() or core.DEFAULT_MODEL


def engine() -> dict:
    """تنظیمات فعلی سرویس ترجمه (OpenCode Zen از طریق CLI) با مدل انتخابی ادمین."""
    with state_lock:
        model = state["config"].get("model", DEFAULT_MODEL)
    if not model:
        model = DEFAULT_MODEL
    return {"label": "OpenCode Zen", "model": model}

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_REPO_PATH = "data/state.json"
TMP_DIR = Path("data/tmp")

# سرعت ترجمه: (تعداد فراخوانی همزمانِ opencode، فاصلهٔ شروع فراخوانی‌ها به ثانیه)
# محافظه‌کارانه تا الگوی مصرف شبیه استفادهٔ عادی از OpenCode بماند (ضد بن).
SPEEDS_OC = {"fast": (3, 2.5), "balanced": (2, 4.0), "safe": (1, 8.0)}
SPEED_LABELS_OC = {"fast": "موازی ×۳ — سریع‌ترین", "balanced": "موازی ×۲ — متعادل",
                   "safe": "تک‌کار‌کره — محتاط"}
SPEED_ORDER = ["fast", "balanced", "safe"]


def speed_label(speed: str) -> str:
    return SPEED_LABELS_OC.get(speed, speed)


# پیام کوتاه فارسی برای خطاهای مرگبار — بدنهٔ خام انگلیسی سرویس به کاربر نشان داده نمی‌شود
ERR_SHORT = {
    "auth": "کلید سرویس ترجمه نامعتبر است",
    "access": "دسترسی به سرویس ترجمه مسدود است (ردهٔ رایگان/موجودی/سقف مصرف)",
    "model": "مدل انتخابی روی سرویس موجود نیست؛ با /model عوضش کن",
    "provider": "سرویس ترجمه موقتاً قطع است (خطای بالادستیِ سرویس)",
    "config": "خطای پیکربندی سرویس ترجمه",
}


def short_err(err: Exception) -> str:
    """پیام خطای فشرده؛ جزئیات خام فقط برای کدهای ناشناخته و حداکثر ۱۴۰ نویسه."""
    code = getattr(err, "code", "")
    if code in ERR_SHORT:
        return ERR_SHORT[code]
    d = str(err).strip() or "خطای نامشخص"
    return d if len(d) <= 140 else d[:137] + "…"


provider_alert_ts: dict = {}  # label → آخرین زمان هشدار (throttle ۳۰ دقیقه‌ای)


def alert_admin_provider_down(label: str, err: Exception) -> None:
    """هشدار قطعی/خطای سرویس به ادمین — حداکثر هر ۳۰ دقیقه یک‌بار."""
    now = time.time()
    if now - provider_alert_ts.get(label, 0.0) < 1800:
        return
    provider_alert_ts[label] = now
    if not ADMIN_ID:
        return
    try:
        send(ADMIN_ID,
             f"⚠️ <b>خطای سرویس ترجمه: {label}</b>\n"
             f"علت: {short_err(err)}\n"
             "وضعیت سرویس را با /ping چک کن.")
    except TgError:
        pass

MAX_DOWNLOAD = 19 * 1024 * 1024   # سقف دانلود مستقیم Bot API تلگرام
MAX_MTPROTO = 1900 * 1024 * 1024  # سقف منطقی فایل با MTProto (~۲GB تلگرام)
MAX_QUEUE = 6                     # حداکثر فایل در صف
# تخمین زمان ترجمه برای فایل‌های خیلی بزرگ (ثانیه به‌ازای هر صفحه؛ شامل پردازش PDF).
# اگر فایل تا سقف سخت جاب جا نشود، به‌جای کشته‌شدن وسط کار، با پیام راهنما رد می‌شود.
SEC_PER_PAGE_EST = float(os.environ.get("SEC_PER_PAGE_EST", "7.5") or 7.5)
JOB_HARD_MINUTES = float(os.environ.get("JOB_HARD_MINUTES", "345") or 345)  # سقف timeout-minutes ورک‌فلو منهای حاشیه

state_lock = threading.RLock()
dirty = threading.Event()
job_q: "queue.Queue[dict]" = queue.Queue()
user_pending: set[int] = set()
auth_requested: set[int] = set()   # کاربرانی که برایشان درخواست دسترسی به ادمین فرستاده شده
admin_pending: dict[int, str] = {}   # chat_id -> نوع ورودی منتظره (limit/channel/allow)
worker_busy = threading.Event()
draining = threading.Event()      # چرخه در حال بسته‌شدن است؛ فایل جدید پذیرفته نمی‌شود
start_ts = time.time()
stop_event = threading.Event()
BOT_USERNAME = ""


def log(*args) -> None:
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


def _default_state() -> dict:
    return {
        "config": {
            "page_limit": DEFAULT_PAGE_LIMIT,
            "force_join": FORCE_JOIN_ENABLED,
            "channel": FORCE_JOIN_CHANNEL,
            "speed": "balanced",
            "model": DEFAULT_MODEL,
            "src_lang": "auto",
            "dst_lang": "fa",
            "allowed": list(ALLOWED_ENV),
        },
        "users": {},
        "totals": {"files": 0, "pages": 0},
    }


state = _default_state()

# --------------------------------------------------------------------------
# ذخیره/بازیابی وضعیت در GitHub (در برابر خواب و ری‌استارت Render)
# --------------------------------------------------------------------------
GH_API = f"https://api.github.com/repos/{GH_REPO}"


def _gh_headers() -> dict:
    return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}


def load_state() -> None:
    if not (GH_TOKEN and GH_REPO):
        log("GitHub برای ذخیرهٔ وضعیت تنظیم نشده؛ وضعیت فقط در حافظه است.")
        return
    try:
        r = requests.get(f"{GH_API}/contents/{STATE_REPO_PATH}?ref={GH_BRANCH}",
                         headers=_gh_headers(), timeout=20)
        if r.status_code == 200:
            data = json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
            with state_lock:
                dflt = _default_state()
                state["config"] = {**dflt["config"], **data.get("config", {})}
                state["users"] = data.get("users", {})
                state["totals"] = {**dflt["totals"], **data.get("totals", {})}
            log(f"وضعیت از GitHub بازیابی شد ({len(state['users'])} کاربر).")
        else:
            log("وضعیت ذخیره‌شده‌ای در GitHub نبود؛ شروع تازه.")
    except Exception as e:  # noqa: BLE001
        log(f"خواندن وضعیت از GitHub ناموفق بود: {e}")


def commit_state(reason: str = "") -> None:
    if not (GH_TOKEN and GH_REPO):
        return
    # نسخهٔ کامیت‌شده در ریپوی عمومی، بدون نام کاربری/نام واقعی کاربران (حفظ حریم خصوصی).
    # روی خود ربات، state کامل می‌ماند.
    with state_lock:
        pub = json.loads(json.dumps(state))  # کپی عمیق
        for rec in (pub.get("users") or {}).values():
            rec.pop("name", None)
            rec.pop("username", None)
        payload = json.dumps(pub, ensure_ascii=False, indent=1)
    sha = None
    try:
        r = requests.get(f"{GH_API}/contents/{STATE_REPO_PATH}?ref={GH_BRANCH}",
                         headers=_gh_headers(), timeout=20)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:  # noqa: BLE001
        pass
    body = {
        "message": f"chore: state sync {reason} ({time.strftime('%Y-%m-%d %H:%M')})",
        "content": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
        "branch": GH_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r2 = requests.put(f"{GH_API}/contents/{STATE_REPO_PATH}",
                      headers=_gh_headers(), json=body, timeout=30)
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"commit_state {r2.status_code}: {r2.text[:200]}")
    log(f"وضعیت در GitHub ذخیره شد ({reason}).")


def sync_loop() -> None:
    """هر تغییری حداکثر ~۳ دقیقه بعد در GitHub کامیت می‌شود (ادغام تغییرات پشت‌سرهم)."""
    while not stop_event.is_set():
        dirty.wait(timeout=600)
        if stop_event.is_set():
            return
        dirty.clear()
        time.sleep(120)  # فرصت ادغام برای تغییرات پیاپی
        try:
            commit_state("auto")
        except Exception as e:  # noqa: BLE001
            log(f"همگام‌سازی ناموفق: {e}")
            dirty.set()
            time.sleep(60)


# --------------------------------------------------------------------------
# API تلگرام
# --------------------------------------------------------------------------
class TgError(Exception):
    pass


def api(method: str, *, timeout: int = 40, retries: int = 2, **params):
    last = None
    for _ in range(retries + 1):
        try:
            r = requests.post(f"{TG}/{method}", json=params, timeout=timeout)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            if r.status_code == 429:
                wait = float(data.get("parameters", {}).get("retry_after", 3))
                time.sleep(min(wait + 1, 30))
                continue
            raise TgError(f"{method}: {data.get('description', r.status_code)}")
        except TgError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise TgError(f"{method}: {last}")


def send(chat_id, text, reply_markup=None, **kw):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", **kw}
    if reply_markup:
        p["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("sendMessage", **p)


def edit(chat_id, message_id, text, reply_markup=None, **kw):
    p = {"chat_id": chat_id, "message_id": message_id, "text": text,
         "parse_mode": "HTML", **kw}
    if reply_markup:
        p["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        return api("editMessageText", **p)
    except TgError as e:
        if "message is not modified" in str(e).lower():
            return None
        raise


def upload_document(chat_id, path: Path, caption: str):
    with open(path, "rb") as fh:
        r = requests.post(
            f"{TG}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": fh},
            timeout=300,
        )
        data = r.json()
        if not data.get("ok"):
            raise TgError(f"sendDocument: {data.get('description', r.status_code)}")
        return data["result"]


# --------------------------------------------------------------------------
# OpenCode Zen — کلید فقط «موقع ترجمه» روشن می‌شود و بعد از کار خاموش (ضد بن)
# --------------------------------------------------------------------------
AUTH_JSON_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"


def opencode_arm() -> None:
    """نوشتن احراز OpenCode در محل فایل خود CLI — فقط پیش از شروع ترجمه."""
    if not OPENCODE_API_KEY:
        raise RuntimeError("کلید OPENCODE_API_KEY تنظیم نشده است.")
    AUTH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_JSON_PATH.write_text(
        json.dumps({"zen": {"type": "api", "key": OPENCODE_API_KEY}}), encoding="utf-8")
    log("سرویس ترجمه (OpenCode) روشن شد.")


def opencode_disarm() -> None:
    """حذف احراز OpenCode — بعد از پایان ترجمه؛ بین ترجمه‌ها هیچ کلیدی روی دیسک نیست."""
    try:
        AUTH_JSON_PATH.unlink()
        log("سرویس ترجمه (OpenCode) خاموش شد.")
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log(f"خاموش‌کردن OpenCode ناموفق بود: {e}")


def list_free_models() -> list[str]:
    """فهرست مدل‌های رایگان از خود CLI (لیست محلی، بدون مصرف کلید)."""
    try:
        binp = core.find_opencode(OC_BIN)
        if binp:
            out = subprocess.run([binp, "models"], capture_output=True, text=True,
                                 timeout=90, stdin=subprocess.DEVNULL).stdout or ""
            ms = []
            for ln in out.splitlines():
                ln = ln.strip()
                if "/" in ln:
                    name = ln.split("/", 1)[1].split()[0]
                    if name not in ms:
                        ms.append(name)
            free = [m for m in ms if m.endswith("-free") or m == "big-pickle"]
            if free:
                return free
    except Exception as e:  # noqa: BLE001
        log(f"خواندن لیست مدل‌های OpenCode ناموفق بود: {e}")
    return list(core.OPENCODE_FREE_MODELS)


# --------------------------------------------------------------------------
# MTProto (Telethon) — دانلود/آپلود فایل‌های بزرگ‌تر از سقف Bot API
# --------------------------------------------------------------------------
class MTProtoClient:
    """کلاینت Telethon همیشه‌روشن در یک ترد جدا با event loop خودش.

    نکتهٔ مهم: ربات‌ها نمی‌توانند تاریخچهٔ چت را بخوانند (GetHistory ممنوع است)
    ولی messages.getMessages با آیدی دقیق مجاز است. برای بی‌نیازی از کش entity
    سه مسیر به‌ترتیب امتحان می‌شود:
      ۱) کش مدیای زنده — آپدیت‌های دریافتی همان لحظه (بدون نیاز به هیچ رزولوشنی)
      ۲) InputPeerUser(uid, 0) — برای گفتگوی خصوصیِ کاربر با خودِ ربات، سرور
         access_hash صفر را می‌پذیرد (تست واقعی شد)
      ۳) رزولوشن با آیدی خام از کش entity Telethon
    """

    def __init__(self) -> None:
        self._client = None
        self._loop = None
        self.ready = threading.Event()
        self._media: "collections.OrderedDict[tuple[int, int], object]" = collections.OrderedDict()
        self._media_lock = threading.Condition()

    def start(self) -> None:
        if not (TG_API_ID and TG_API_HASH):
            log("MTProto تنظیم نشده (TELEGRAM_API_ID/HASH) — فایل‌های بزرگ پشتیبانی نمی‌شوند.")
            return
        try:
            import telethon  # noqa: F401
        except Exception as e:  # noqa: BLE001
            log(f"telethon نصب نیست؛ فایل‌های بزرگ پشتیبانی نمی‌شوند: {e}")
            return

        def _runner():
            import asyncio
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._boot())
            except Exception as e:  # noqa: BLE001
                log(f"MTProto راه‌اندازی نشد: {e}")
                return
            self.ready.set()
            self._loop.run_forever()

        threading.Thread(target=_runner, daemon=True, name="mtproto").start()

    async def _boot(self):
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
        self._client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH,
                                      request_retries=3, connection_retries=5)
        await self._client.start(bot_token=BOT_TOKEN)

        @self._client.on(events.NewMessage)
        async def _cache_media(e):  # هر مدیایی که همین حالا می‌رسد کش می‌شود
            try:
                if e.message is not None and e.message.document:
                    self._remember_media(e.chat_id, e.message.id, e.message)
            except Exception:  # noqa: BLE001
                pass
        log("MTProto متصل شد.")

    def _remember_media(self, chat_id: int, mid: int, msg) -> None:
        with self._media_lock:
            self._media[(int(chat_id), int(mid))] = msg
            while len(self._media) > 200:   # جلوگیری از رشد بی‌حد حافظه
                self._media.popitem(last=False)
            self._media_lock.notify_all()

    def _take_media(self, chat_id: int, mid: int, timeout: float = 20.0):
        """مدیای زنده را صبر می‌کند (آپدیت Telethon معمولاً هم‌زمان Bot API می‌رسد)."""
        key = (int(chat_id), int(mid))
        deadline = time.time() + max(timeout, 0.0)
        with self._media_lock:
            while True:
                if key in self._media:
                    return self._media[key]
                remain = deadline - time.time()
                if remain <= 0:
                    return None
                self._media_lock.wait(remain)

    def _call(self, coro_fn, timeout: float = 1800.0):
        if not (self.ready.is_set() and self._client and self._loop):
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
            return fut.result(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            log(f"MTProto call ناموفق بود: {type(e).__name__}: {e}")
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    async def _fetch_msg(self, peer, message_id: int):
        msg = await self._client.get_messages(peer, ids=[message_id])
        m = msg[0] if isinstance(msg, list) else msg
        if not m or not getattr(m, "document", None):
            return None
        try:
            self._remember_media(peer.user_id if hasattr(peer, "user_id") else 0,
                                 message_id, m)
        except Exception:  # noqa: BLE001
            pass
        return m

    def download(self, chat_id: int, message_id: int, dest: Path) -> bool:
        """دانلود مدیای یک پیام با MTProto (بدون سقف ۲۰MB Bot API)."""
        import asyncio
        chat_id, message_id = int(chat_id), int(message_id)
        self.last_error = ""
        if not self.ready.is_set():
            self.last_error = "کلاینت MTProto آماده نیست"
            return False

        async def _dl_cached(m) -> bool:
            out = await self._client.download_media(m, file=str(dest))
            p = Path(str(out)) if out else dest
            return p.exists() and p.stat().st_size > 0

        # ۱) پیام زندهٔ کش‌شده (کاملاً بدون رزولوشن)
        m = self._take_media(chat_id, message_id, timeout=20.0)
        if m is not None and self._call(lambda: _dl_cached(m)):
            return True

        # ۲) InputPeerUser با hash صفر — برای گفتگوی خصوصی کاربر با ربات
        try:
            from telethon.tl.types import InputPeerUser
            r = self._call(lambda: self._dl_by_peer(InputPeerUser(chat_id, 0),
                                                    message_id, dest))
            if r:
                return True
        except Exception as e:  # noqa: BLE001
            log(f"مسیر hash=0 ناموفق: {e}")
            self.last_error = self.last_error or str(e)

        # ۳) رزولوشن با کش entity Telethon
        if self._call(lambda: self._dl_by_peer(chat_id, message_id, dest)):
            return True

        log(f"دانلود MTProto کامل شکست خورد ({chat_id}/{message_id}): "
            f"{self.last_error or 'پیام پیدا نشد'}")
        try:
            if ADMIN_ID:
                send(ADMIN_ID, f"⚠️ خطای MTProto\nگفتگو: <code>{chat_id}</code> | پیام: "
                               f"<code>{message_id}</code>\nعلت: "
                               f"<code>{html_mod.escape((self.last_error or 'پیام پیدا نشد')[:160])}</code>")
        except TgError:
            pass
        return False

    async def _dl_by_peer(self, peer, message_id: int, dest: Path) -> bool:
        m = await self._fetch_msg(peer, message_id)
        if m is None:
            self.last_error = self.last_error or "پیام با این آیدی پیدا نشد"
            return False
        out = await self._client.download_media(m, file=str(dest))
        p = Path(str(out)) if out else dest
        return p.exists() and p.stat().st_size > 0

    def upload(self, chat_id: int, path: Path, caption: str) -> bool:
        """ارسال فایل خروجی بزرگ با MTProto (بدون سقف ۵۰MB Bot API)."""
        import asyncio

        async def _do():
            peer = None
            try:
                from telethon.tl.types import InputPeerUser
                peer = InputPeerUser(int(chat_id), 0)
            except Exception:  # noqa: BLE001
                peer = int(chat_id)
            await self._client.send_file(peer, str(path), caption=caption[:1024],
                                         parse_mode="html", force_document=True)
            return True

        return bool(self._call(_do, timeout=3600.0))


MT = MTProtoClient()


def mtproto_ready() -> bool:
    return MT.ready.is_set()


# --------------------------------------------------------------------------
# force join و کاربران
# --------------------------------------------------------------------------
def channel_ref(ch: str) -> str:
    if not ch:
        return ""
    return ch if (ch.startswith("@") or ch.startswith("-")) else "@" + ch


def force_join_ok(uid: int) -> bool:
    with state_lock:
        enabled = bool(state["config"].get("force_join"))
        ch = state["config"].get("channel", "")
    if not enabled or not ch:
        return True
    try:
        m = api("getChatMember", chat_id=channel_ref(ch), user_id=uid, timeout=20)
        return m.get("status") in ("creator", "administrator", "member")
    except TgError:
        # اگر ربات نتواند عضویت را بپرسد (مثلاً ادمین کانال نیست)، کاربر را محروم نکن
        return True


def join_kb(ch: str) -> dict:
    rows = []
    ref = channel_ref(ch)
    if ref.startswith("@"):
        rows.append([{"text": "📢 عضویت در کانال", "url": f"https://t.me/{ref.lstrip('@')}"}])
    rows.append([{"text": "✅ بررسی مجدد عضویت", "callback_data": "check_join"}])
    return {"inline_keyboard": rows}


def touch_user(m: dict) -> None:
    u = m.get("from", {})
    uid = str(u.get("id", ""))
    if not uid:
        return
    with state_lock:
        rec = state["users"].setdefault(uid, {"first": time.strftime("%Y-%m-%d")})
        rec.update({
            "last": time.strftime("%Y-%m-%d %H:%M"),
            "last_ts": time.time(),
            "username": u.get("username") or "",
            "name": (u.get("first_name") or "") + (" " + u.get("last_name") if u.get("last_name") else ""),
        })
    dirty.set()


# --------------------------------------------------------------------------
# پنل ادمین
# --------------------------------------------------------------------------
def is_admin(uid) -> bool:
    try:
        return bool(ADMIN_ID) and int(uid) == ADMIN_ID
    except (TypeError, ValueError):
        return False


def is_allowed(uid) -> bool:
    """کاربران تازه پیش‌فرض غیرمجازند؛ فقط ادمین و فهرست تأییدشده‌ها حق استفاده دارند."""
    if is_admin(uid):
        return True
    try:
        u = int(uid)
    except (TypeError, ValueError):
        return False
    with state_lock:
        return u in (state["config"].get("allowed") or [])


def allow_user(uid: int) -> None:
    with state_lock:
        lst = state["config"].setdefault("allowed", [])
        if uid not in lst:
            lst.append(uid)
    dirty.set()


def deny_user(uid: int) -> None:
    with state_lock:
        lst = state["config"].setdefault("allowed", [])
        if uid in lst:
            lst.remove(uid)
    dirty.set()


def user_display_name(uid: int, m: dict | None = None) -> str:
    if m:
        u = m.get("from", {}) or {}
        nm = (u.get("first_name") or "") + ((" " + u["last_name"]) if u.get("last_name") else "")
        if nm.strip():
            return nm.strip()
    with state_lock:
        rec = state["users"].get(str(uid)) or {}
    return rec.get("name") or rec.get("username") or ""


def request_access(m: dict, *, silent: bool = False) -> None:
    """کاربر غیرمجاز: پاسخ به او + درخواست تأیید با دکمه برای ادمین."""
    uid = m.get("from", {}).get("id")
    chat = m.get("chat", {}).get("id")
    if not uid or chat is None or not ADMIN_ID:
        return
    if not silent:
        try:
            send(chat, "⛔️ شما هنوز مجاز به استفاده از این ربات نیستید.\n"
                       "درخواست دسترسی برای ادمین ارسال شد؛ بعد از تأیید می‌توانید فایل بفرستید.")
        except TgError:
            pass
    if uid in auth_requested:
        return  # درخواست قبلاً ثبت شده؛ ادمین را اسپم نکن
    auth_requested.add(uid)
    u = m.get("from", {}) or {}
    uname = f"@{u['username']}" if u.get("username") else ""
    nm = html_mod.escape(user_display_name(uid, m))
    kb = {"inline_keyboard": [[
        {"text": "✅ تأیید دسترسی", "callback_data": f"authok:{uid}"},
        {"text": "❌ رد", "callback_data": f"authno:{uid}"},
    ]]}
    try:
        send(ADMIN_ID,
             f"🔔 <b>درخواست دسترسی جدید</b>\n"
             f"👤 {nm or 'کاربر'}" + (f" ({uname})" if uname else "") + f"\n"
             f"🆔 <code>{uid}</code>\n\n"
             "برای اجازهٔ استفاده، تأیید کن:",
             reply_markup=kb)
    except TgError:
        pass


def stats_text() -> str:
    with state_lock:
        users = state["users"]
        totals = state["totals"]
        cfg = state["config"]
        now = time.time()
        active_today = sum(1 for u in users.values()
                           if now - float(u.get("last_ts", 0)) < 86400)
        top = sorted(users.items(), key=lambda kv: kv[1].get("pages", 0), reverse=True)[:5]
        lines = [
            "📊 <b>آمار ربات</b>",
            f"👥 کل کاربران: {len(users)}",
            f"🟢 فعال در ۲۴ ساعت: {active_today}",
            f"📚 فایل‌های ترجمه‌شده: {totals.get('files', 0)}",
            f"📄 صفحه‌های ترجمه‌شده: {totals.get('pages', 0)}",
            f"⏳ صف فعلی: {job_q.qsize()}",
            f"⚡ سرعت: {speed_label(cfg.get('speed'))}",
            f"🤖 سرویس: OpenCode Zen — <code>{cfg.get('model')}</code>",
            f"🌐 زبان: {cfg.get('src_lang', 'auto')} → {cfg.get('dst_lang', 'fa')}",
            f"🛡 کاربران مجاز: {len(cfg.get('allowed') or [])}",
            f"🧮 توکن مصرفی کل: {totals.get('tokens', 0):,}",
            f"⏱ آپ‌تایم: {(now - start_ts) // 60:.0f} دقیقه",
        ]
        if top:
            lines.append("")
            lines.append("🏆 برترین کاربران:")
            for i, (uid, u) in enumerate(top, 1):
                nm = (u.get("name") or "").strip() or u.get("username") or f"کاربر {uid}"
                lines.append(f"{i}. {nm} — {u.get('pages', 0)} صفحه، {u.get('files', 0)} فایل، {u.get('tokens', 0):,} توکن")
        return "\n".join(lines)


def admin_menu() -> dict:
    with state_lock:
        c = dict(state["config"])
    b = lambda t, d: {"text": t, "callback_data": d}  # noqa: E731
    src = c.get("src_lang", "auto")
    dst = c.get("dst_lang", "fa")
    return {"inline_keyboard": [
        [b("📊 آمار کاربران", "ad:stats")],
        [b(f"🧠 مدل: {c.get('model')}", "ad:model"),
         b(f"⚡ سرعت: {speed_label(c.get('speed'))}", "ad:speed")],
        [b(f"🌐 زبان: {src} → {dst}", "ad:lang")],
        [b(f"📄 سقف صفحه: {c.get('page_limit')}", "ad:limit"),
         b(f"📢 کانال اجباری: {'✅ روشن' if c.get('force_join') else '❌ خاموش'}", "ad:toggle")],
        [b(f"🔗 کانال: {c.get('channel') or '— تنظیم نشده —'}", "ad:channel"),
         b(f"🛡 مجاز: {len(c.get('allowed') or [])}", "ad:allowed")],
        [b("💾 ذخیرهٔ دستی وضعیت", "ad:sync")],
    ]}


def model_menu() -> dict:
    """دکمه‌های انتخاب مدل رایگان (۳تایی در هر ردیف)."""
    with state_lock:
        cur = state["config"].get("model", DEFAULT_MODEL)
    models = list_free_models()
    if cur not in models:
        models.insert(0, cur)
    rows, row = [], []
    for m in models:
        label = ("✅ " if m == cur else "") + m
        row.append({"text": label, "callback_data": f"mdl:{m}"[:64]})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "↺ تازه‌سازی لیست", "callback_data": "ad:model"}])
    return {"inline_keyboard": rows}


SRC_CHOICES = [("auto", "خودکار"), ("en", "انگلیسی"), ("ar", "عربی"), ("fr", "فرانسوی"),
               ("de", "آلمانی"), ("tr", "ترکی"), ("ru", "روسی"), ("zh", "چینی"),
               ("es", "اسپانیایی"), ("it", "ایتالیایی"), ("pt", "پرتغالی")]
DST_CHOICES = [("fa", "فارسی"), ("en", "انگلیسی"), ("ar", "عربی"), ("tr", "ترکی")]


def lang_menu() -> dict:
    with state_lock:
        c = dict(state["config"])
    rows = [[{"text": "— زبان مبدأ —"}]]
    row = []
    for code, label in [x for x in SRC_CHOICES if x[1]]:
        mark = "✅ " if code == c.get("src_lang", "auto") else ""
        row.append({"text": f"{mark}{label}", "callback_data": f"lsrc:{code}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "— زبان مقصد —"}])
    row = []
    for code, label in DST_CHOICES:
        mark = "✅ " if code == c.get("dst_lang", "fa") else ""
        row.append({"text": f"{mark}{label}", "callback_data": f"ldst:{code}"})
    rows.append(row)
    return {"inline_keyboard": rows}


def handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    uid = cb.get("from", {}).get("id")
    msg = cb.get("message", {})
    chat = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    try:
        api("answerCallbackQuery", callback_query_id=cb["id"], timeout=15)
    except TgError:
        pass
    if not chat:
        return
    # دکمهٔ بررسی مجدد عضویت مال کاربران عادی است؛ بقیه فقط ادمین
    if not is_admin(uid) and data != "check_join":
        return
    if data == "ad:stats":
        edit(chat, mid, stats_text(), reply_markup=admin_menu())
    elif data == "ad:limit":
        admin_pending[chat] = "limit"
        with state_lock: pl = state["config"].get("page_limit")
        edit(chat, mid, f"سقف صفحهٔ فعلی: <b>{pl}</b>\n"
                        f"عدد جدید را بفرست (۱ تا {MAX_PAGE_LIMIT:,}):\n"
                        "⚠️ فایل‌های خیلی بزرگ فقط اگر تا پایان چرخهٔ جاری جا شوند قبول می‌شوند.")
    elif data == "ad:speed":
        with state_lock:
            idx = SPEED_ORDER.index(state["config"]["speed"]) if state["config"]["speed"] in SPEED_ORDER else 0
            state["config"]["speed"] = SPEED_ORDER[(idx + 1) % len(SPEED_ORDER)]
        dirty.set()
        edit(chat, mid, "پنل ادمین ⚙️", reply_markup=admin_menu())
    elif data == "ad:model":
        edit(chat, mid, "🧠 مدل ترجمه را انتخاب کن (مدل‌های رایگان OpenCode Zen):",
             reply_markup=model_menu())
    elif data.startswith("mdl:"):
        mdl = data[4:]
        with state_lock:
            state["config"]["model"] = mdl
        dirty.set()
        edit(chat, mid, f"مدل شد: <b>{mdl}</b> ✅", reply_markup=model_menu())
    elif data == "ad:lang":
        edit(chat, mid, "🌐 زبان مبدأ و مقصد را انتخاب کن:", reply_markup=lang_menu())
    elif data.startswith("lsrc:"):
        code = data[5:]
        with state_lock:
            state["config"]["src_lang"] = code
        dirty.set()
        edit(chat, mid, f"زبان مبدأ: <b>{code}</b>", reply_markup=lang_menu())
    elif data.startswith("ldst:"):
        code = data[5:]
        with state_lock:
            state["config"]["dst_lang"] = code
        dirty.set()
        edit(chat, mid, f"زبان مقصد: <b>{code}</b>\n(برای مقصد راست‌به‌چپ، چیدمان آینه‌ای می‌شود)",
             reply_markup=lang_menu())
    elif data == "ad:allowed":
        with state_lock:
            lst = list(state["config"].get("allowed") or [])
        names = []
        for u in lst[:20]:
            rec = state["users"].get(str(u)) or {}
            nm = rec.get("name") or rec.get("username") or ""
            names.append(f"• <code>{u}</code>" + (f" — {html_mod.escape(nm)}" if nm else ""))
        edit(chat, mid,
             f"🛡 کاربران مجاز ({len(lst)}):\n" + ("\n".join(names) or "— هیچ —") +
             "\n\nاضافهٔ دستی: /allow &lt;آیدی&gt;\nحذف: /deny &lt;آیدی&gt;",
             reply_markup=admin_menu())
    elif data.startswith("authok:") or data.startswith("authno:"):
        act, suid = data.split(":", 1)
        try:
            suid_i = int(suid)
        except ValueError:
            return
        if act == "authok":
            allow_user(suid_i)
            edit(chat, mid, f"✅ دسترسی کاربر <code>{suid_i}</code> تأیید و ذخیره شد.")
            try:
                send(suid_i, "✅ دسترسی شما به ربات تأیید شد!\nحالا می‌توانی فایل PDF بفرستی.")
            except TgError:
                pass
        else:
            deny_user(suid_i)
            edit(chat, mid, f"❌ درخواست کاربر <code>{suid_i}</code> رد شد.")
            try:
                send(suid_i, "متأسفانه درخواست دسترسی شما رد شد.")
            except TgError:
                pass
        auth_requested.discard(suid_i)
    elif data == "ad:toggle":
        with state_lock:
            state["config"]["force_join"] = not bool(state["config"].get("force_join"))
        dirty.set()
        edit(chat, mid, "پنل ادمین ⚙️", reply_markup=admin_menu())
    elif data == "ad:channel":
        admin_pending[chat] = "channel"
        with state_lock: ch = state["config"].get("channel", "")
        edit(chat, mid, (f"کانال فعلی: <b>{ch or '—'}</b>\n"
                         "آیدی کانال جدید را بفرست (مثل <code>@mychannel</code> یا <code>-100...</code>).\n"
                         "برای غیرفعال‌سازی فقط بنویس: خاموش"))
    elif data == "ad:sync":
        edit(chat, mid, "💾 در حال ذخیرهٔ وضعیت در GitHub…")
        try:
            commit_state("manual")
            edit(chat, mid, "ذخیره شد ✅", reply_markup=admin_menu())
        except Exception as e:  # noqa: BLE001
            edit(chat, mid, f"خطا در ذخیره: <code>{e}</code>", reply_markup=admin_menu())
    elif data == "check_join":
        if force_join_ok(uid):
            edit(chat, mid, "عضویت تأیید شد ✅\nحالا فایل PDF‌ات را بفرست.")
        else:
            with state_lock: ch = state["config"].get("channel", "")
            send(chat, "هنوز عضو کانال نشدی.", reply_markup=join_kb(ch))


def handle_admin_text(m: dict) -> None:
    chat = m["chat"]["id"]
    kind = admin_pending.pop(chat, None)
    txt = (m.get("text") or "").strip()
    if kind == "limit":
        try:
            n = max(1, min(MAX_PAGE_LIMIT, int(txt)))
        except ValueError:
            send(chat, "عدد معتبر بفرست (مثلاً ۵۰).")
            admin_pending[chat] = "limit"
            return
        with state_lock:
            state["config"]["page_limit"] = n
        dirty.set()
        send(chat, f"سقف صفحه شد: <b>{n}</b>", reply_markup=admin_menu())
    elif kind == "channel":
        if txt.lower() in ("خاموش", "off", "-"):
            with state_lock:
                state["config"]["channel"] = ""
        else:
            with state_lock:
                state["config"]["channel"] = txt
        dirty.set()
        with state_lock: ch = state["config"].get("channel", "")
        send(chat, f"کانال: <b>{ch or 'خاموش'}</b>", reply_markup=admin_menu())
    elif kind == "allow":
        if txt.lower() in ("لغو", "off", "-"):
            send(chat, "عملیات لغو شد.", reply_markup=admin_menu())
            return
        try:
            uid = int(txt.lstrip("@"))
            allow_user(uid)
            send(chat, f"✅ کاربر <code>{uid}</code> به فهرست مجاز اضافه شد.", reply_markup=admin_menu())
            try:
                send(uid, "✅ دسترسی شما به ربات تأیید شد!\nحالا می‌توانی فایل PDF بفرستی.")
            except TgError:
                pass
        except ValueError:
            send(chat, "آیدی عددی معتبر بفرست (مثل ۷۴۳۳۳۵۷۷۰۰) یا /allow را دوباره بزن.")
            admin_pending[chat] = "allow"


# --------------------------------------------------------------------------
# پردازش فایل‌ها
# --------------------------------------------------------------------------
WELCOME = (
    "👋 <b>مترجم PDF با هوش مصنوعی</b>\n\n"
    "فایل PDF را بفرست تا با حفظ چیدمان (موقعیت، اندازه، بولد/رنگ، جدول‌ها و عکس‌ها) "
    "به زبان مقصد ترجمه و راست‌چین تحویل داده شود.\n\n"
    "📄 سقف فعلی: تا <b>{limit}</b> صفحه برای هر فایل\n"
    "🤖 موتور: {engine} — مدل <code>{model}</code>\n"
    "🌐 زبان: {src} → {dst}\n"
    "📎 فایل‌های بزرگ تا ~۲ گیگابایت هم پذیرفته می‌شود\n\n"
    "💡 فقط کافیست فایل را بفرست؛ پیشرفت ترجمه را همین‌جا نشان می‌دهم."
)

HELP = (
    "📄 کافیست فایل PDF را بفرستی (نه لینک، نه عکس).\n"
    f"سقف صفحه: تا <b>{{limit}}</b> صفحه در هر فایل.\n"
    "ترجمه وفادار به متن اصلی است و چیدمان (جای متن، عکس، جدول، اندازه، بولد، رنگ) "
    "حفظ و برای زبان مقصد راست‌چین/آینه‌ای می‌شود.\n\n"
    "دستورها:\n"
    "/start — شروع\n/help — همین راهنما\n/id — آیدی عددی تو"
)

LANG_LABEL = {"auto": "خودکار", "en": "انگلیسی", "fa": "فارسی", "ar": "عربی", "fr": "فرانسوی",
              "de": "آلمانی", "es": "اسپانیایی", "it": "ایتالیایی", "pt": "پرتغالی",
              "tr": "ترکی", "ru": "روسی", "zh": "چینی", "ja": "ژاپنی", "ko": "کره‌ای",
              "hi": "هندی", "ur": "اردو"}


def cmd_start(m: dict) -> None:
    chat = m["chat"]["id"]
    uid = m.get("from", {}).get("id")
    touch_user(m)
    if not force_join_ok(uid):
        with state_lock:
            ch = state["config"].get("channel", "")
        send(chat, "برای استفاده از ربات، اول عضو کانال شو:", reply_markup=join_kb(ch))
        return
    with state_lock:
        pl = int(state["config"].get("page_limit", 40))
        src = state["config"].get("src_lang", "auto")
        dst = state["config"].get("dst_lang", "fa")
    eng = engine()
    send(chat, WELCOME.format(limit=pl, engine=eng["label"], model=eng["model"],
                              src=LANG_LABEL.get(src, src), dst=LANG_LABEL.get(dst, dst)))


def on_document(m: dict) -> None:
    chat = m["chat"]["id"]
    uid = m["from"]["id"]
    if draining.is_set():
        send(chat, "🔄 ربات در حال تعویض چرخهٔ سرویس است؛ حدوداً ۳ دقیقه بعد همین فایل را دوباره بفرست.")
        return
    touch_user(m)
    if not force_join_ok(uid):
        with state_lock:
            ch = state["config"].get("channel", "")
        send(chat, "برای استفاده از ربات، اول عضو کانال شو:", reply_markup=join_kb(ch))
        return
    doc = m.get("document", {})
    name = doc.get("file_name") or "file.pdf"
    if not name.lower().endswith(".pdf"):
        send(chat, "فقط فایل PDF بفرست (فایل ورد/عکس قابل ترجمه نیست).")
        return
    fsize = int(doc.get("file_size", 0) or 0)
    if fsize > MAX_MTPROTO:
        send(chat, "❌ حجم فایل از ~۲ گیگابایت بیشتر است؛ تلگرام اجازه نمی‌دهد.")
        return
    if fsize > MAX_DOWNLOAD and not mtproto_ready():
        send(chat, "⚠️ حجم فایل بیشتر از ۱۹ مگابایت است و پشتیبانی فایل بزرگ (MTProto) "
                   "هنوز آماده نیست؛ چند لحظه بعد دوباره امتحان کن یا فایل را سبک‌تر بفرست.")
        return
    if uid in user_pending:
        send(chat, "⏳ یک فایل از تو در حال ترجمه است؛ صبر کن تمام شود.")
        return
    if job_q.qsize() >= MAX_QUEUE:
        send(chat, "صف ربات پر است؛ چند دقیقه بعد دوباره امتحان کن.")
        return
    user_pending.add(uid)
    job_q.put({"chat": chat, "uid": uid, "file_id": doc["file_id"],
               "mid": m.get("message_id"), "size": fsize, "name": name,
               "uname": (m.get("from", {}) or {}).get("username") or ""})
    send(chat, f"🟡 فایل در صف قرار گرفت (جایگاه {job_q.qsize()}). "
               "به‌محض شروع ترجمه خبر می‌دهم.")


def process_job(job: dict) -> None:
    chat, uid = job["chat"], job["uid"]
    file_id, name = job["file_id"], job["name"]
    fsize = int(job.get("size", 0) or 0)
    mid = job.get("mid")
    st = send(chat, "⬇️ در حال دریافت فایل…")
    stid = st["message_id"]
    last_edit = [0.0]

    def upd(text: str, force: bool = False) -> None:
        if force or time.time() - last_edit[0] > 9:
            last_edit[0] = time.time()
            try:
                edit(chat, stid, text)
            except TgError:
                pass

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    in_pdf = TMP_DIR / f"{uid}_{stamp}_in.pdf"
    out_pdf = TMP_DIR / f"{uid}_{stamp}_fa.pdf"

    # ── دانلود: فایل‌های ≤۱۹MB از Bot API؛ بزرگ‌تر از طریق MTProto (تا ~۲GB)
    try:
        if fsize > MAX_DOWNLOAD:
            if not mtproto_ready():
                raise RuntimeError("پشتیبانی MTProto برای فایل‌های بزرگ هنوز آماده نیست.")
            edit(chat, stid, f"⬇️ فایل بزرگ ({fsize / (1024 * 1024):.0f} مگابایت) — "
                             "دانلود با MTProto…")
            if not MT.download(chat, int(mid or 0), in_pdf):
                raise RuntimeError("دانلود MTProto ناموفق بود.")
        else:
            f = api("getFile", file_id=file_id, timeout=30)
            fp = f.get("file_path", "")
            r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}", timeout=180)
            r.raise_for_status()
            in_pdf.write_bytes(r.content)
    except Exception as e:  # noqa: BLE001
        edit(chat, stid, f"❌ خطا در دانلود فایل: <code>{html_mod.escape(str(e))[:140]}</code>")
        in_pdf.unlink(missing_ok=True)
        return

    try:
        with pymupdf.open(str(in_pdf)) as d:
            n_pages = d.page_count
    except Exception:  # noqa: BLE001
        edit(chat, stid, "❌ این فایل قابل خواندن نیست (شاید رمز‌گذاری شده باشد).")
        in_pdf.unlink(missing_ok=True)
        return

    with state_lock:
        page_limit = int(state["config"].get("page_limit", 40))
    if n_pages > page_limit:
        edit(chat, stid, f"⚠️ فایل <b>{n_pages}</b> صفحه دارد؛ سقف فعلی ربات "
                         f"<b>{page_limit}</b> صفحه است. فایل کوچک‌تری بفرست یا از ادمین سقف را بپرس.")
        in_pdf.unlink(missing_ok=True)
        return

    # ── گیت زمان چرخه: فایل خیلی بزرگ اگر تا سقف سخت جاب جا نشود، وسط کار کشته می‌شود؛
    #    پس از اول با پیام راهنما رد می‌شود تا در چرخهٔ بعدی (خالی‌تر) پذیرفته شود.
    if RUN_MAX_MINUTES:
        needed_min = 5 + n_pages * SEC_PER_PAGE_EST / 60
        remaining_min = (start_ts + JOB_HARD_MINUTES * 60) - time.time()
        if needed_min > remaining_min:
            wait_min = max(int(RUN_MAX_MINUTES - (time.time() - start_ts) / 60) + 5, 5)
            edit(chat, stid, f"⏳ فایل <b>{n_pages}</b> صفحه‌ای حدوداً <b>{needed_min:.0f} دقیقه</b> "
                             f"وقت می‌برد؛ اما تا پایان امن این چرخه فقط "
                             f"<b>{max(remaining_min, 0):.0f} دقیقه</b> مانده.\n"
                             f"⏱ حدود <b>{wait_min} دقیقه</b> دیگر چرخهٔ تازه شروع می‌شود؛ "
                             "همین فایل را آن موقع دوباره بفرست تا کامل ترجمه شود.")
            in_pdf.unlink(missing_ok=True)
            return

    with state_lock:
        speed = state["config"].get("speed", "balanced")
        src_lang = state["config"].get("src_lang", "auto")
        dst_lang = state["config"].get("dst_lang", "fa")
    w, sp = SPEEDS_OC.get(speed, SPEEDS_OC["balanced"])
    eng = engine()
    t0 = time.time()

    def progress(done: int, total: int) -> None:
        pct = done * 100 // max(total, 1)
        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        upd(f"🔄 ترجمه: {bar} {done}/{total} صفحه ({pct}٪)")

    upd(f"🌍 شروع ترجمهٔ <b>{n_pages}</b> صفحه با <code>{eng['model']}</code> "
        f"({LANG_LABEL.get(src_lang, src_lang)} → {LANG_LABEL.get(dst_lang, dst_lang)})…",
        force=True)

    # سرویس ترجمه فقط برای همین ترجمه «روشن» و بعد از کار «خاموش» می‌شود (ضد بن)
    try:
        opencode_arm()
    except Exception as e:  # noqa: BLE001
        edit(chat, stid, f"❌ سرویس ترجمه آماده نیست: <code>{html_mod.escape(str(e))[:140]}</code>")
        in_pdf.unlink(missing_ok=True)
        return
    try:
        res = core.translate_pdf(str(in_pdf), str(out_pdf), "", model=eng["model"],
                                 base_url="", workers=w, spacing=sp,
                                 progress=progress, cancelled=None,
                                 log=lambda *a: None, mirror=True,
                                 src_lang=src_lang, dst_lang=dst_lang, use_cli=True,
                                 oc_bin=OC_BIN, oc_cwd=str(TMP_DIR))
    except core.FatalTranslationError as err:
        alert_admin_provider_down("OpenCode Zen", err)
        edit(chat, stid, "❌ ترجمه انجام نشد: " + short_err(err) + "\nبه ادمین خبر بده.")
        in_pdf.unlink(missing_ok=True)
        return
    except core.TranslationError as err:
        edit(chat, stid, f"❌ ترجمه ناتمام ماند: {short_err(err)}\n"
                         "سرویس ترجمه شلوغ است؛ چند دقیقه بعد دوباره امتحان کن.")
        in_pdf.unlink(missing_ok=True)
        return
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()  # استک کامل در لاگ Actions برای دیباگ
        edit(chat, stid, f"❌ خطای غیرمنتظره در ترجمه: <code>{html_mod.escape(str(e))[:140]}</code>")
        in_pdf.unlink(missing_ok=True)
        return
    finally:
        opencode_disarm()
    usage = (res or {}).get("usage", {})

    secs = time.time() - t0
    out_mb = out_pdf.stat().st_size / (1024 * 1024)
    upd("📤 در حال ارسال فایل ترجمه‌شده…", force=True)
    failed_n = len((res or {}).get("failed_pages", []))
    cap = (f"✅ ترجمهٔ «{html_mod.escape(name)}» — {n_pages} صفحه در {secs / 60:.1f} دقیقه"
           f" | حجم: {out_mb:.1f}MB")
    if failed_n:
        cap += f"\n⚠️ {failed_n} صفحه به‌خاطر خطای سرویس ترجمه نشد (متن اصلی ماند)."

    # ── آپلود: ≤۴۹.۵MB با Bot API؛ بزرگ‌تر (یا اگر Bot API شکست خورد) با MTProto
    sent = False
    if out_mb <= 49.5:
        try:
            upload_document(chat, out_pdf, cap)
            sent = True
        except TgError as e:
            log(f"آپلود Bot API ناموفق بود؛ تلاش با MTProto: {e}")
    if not sent:
        if mtproto_ready():
            upd("📤 فایل بزرگ است — ارسال با MTProto…")
            sent = MT.upload(chat, out_pdf, cap)
        if not sent:
            edit(chat, stid, f"❌ ارسال فایل ({out_mb:.1f} مگابایت) ناموفق بود؛ دوباره امتحان کن.")
            in_pdf.unlink(missing_ok=True)
            out_pdf.unlink(missing_ok=True)
            return
    in_pdf.unlink(missing_ok=True)
    out_pdf.unlink(missing_ok=True)


    total_tokens = int(usage.get("total_tokens", 0) or 0)
    with state_lock:
        rec = state["users"].setdefault(str(uid), {})
        rec["pages"] = rec.get("pages", 0) + n_pages
        rec["files"] = rec.get("files", 0) + 1
        rec["tokens"] = rec.get("tokens", 0) + total_tokens
        state["totals"]["files"] = state["totals"].get("files", 0) + 1
        state["totals"]["pages"] = state["totals"].get("pages", 0) + n_pages
        state["totals"]["tokens"] = state["totals"].get("tokens", 0) + total_tokens
    dirty.set()
    log(f"فایل «{name}» ({n_pages} صفحه) برای {uid} در {secs:.0f}s ترجمه شد ({total_tokens:,} توکن).")

    # گزارش مصرف توکن هر فایل برای ادمین
    if ADMIN_ID:
        try:
            uname = job.get("uname") or ""
            send(ADMIN_ID,
                 "📊 <b>گزارش مصرف توکن</b>\n"
                 f"📄 فایل: <code>{html_mod.escape(name)}</code>\n"
                 f"👤 کاربر: <code>{uid}</code>" + (f" (@{uname})" if uname else "") + "\n"
                 f"📑 صفحات: {n_pages} | ⏱ {secs / 60:.1f} دقیقه\n"
                 f"🔤 ورودی: {usage.get('prompt_tokens', 0):,} | خروجی: {usage.get('completion_tokens', 0):,}\n"
                 f"🧮 مجموع: <b>{total_tokens:,}</b> توکن در {usage.get('api_calls', 0)} درخواست\n"
                 f"🤖 {eng['label']} / <code>{eng['model']}</code>")
        except TgError:
            pass


def worker() -> None:
    while not stop_event.is_set():
        try:
            job = job_q.get(timeout=1)
        except queue.Empty:
            continue
        worker_busy.set()
        try:
            process_job(job)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                send(job["chat"], "❌ خطای غیرمنتظره در پردازش؛ دوباره امتحان کن.")
            except TgError:
                pass
        finally:
            user_pending.discard(job["uid"])
            worker_busy.clear()
            job_q.task_done()


# --------------------------------------------------------------------------
# وب‌سرور /health (برای keep-alive)
# --------------------------------------------------------------------------
def health_server() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({
                "ok": True,
                "bot": BOT_USERNAME,
                "uptime_s": int(time.time() - start_ts),
                "queue": job_q.qsize(),
                "busy": worker_busy.is_set(),
                "users": len(state.get("users", {})),
                "model": state.get("config", {}).get("model"),
                "speed": state.get("config", {}).get("speed"),
                "mtproto": mtproto_ready(),
                "opencode": bool(OPENCODE_API_KEY),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # بی‌صدا
            pass

    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except Exception as e:  # noqa: BLE001
        log(f"health server خطا داد: {e}")


# --------------------------------------------------------------------------
# حلقهٔ پولینگ و مسیریابی پیام‌ها
# --------------------------------------------------------------------------
def handle_message(m: dict) -> None:
    chat = m.get("chat", {})
    chat_id = chat.get("id")
    uid = m.get("from", {}).get("id")
    if chat_id is None or uid is None:
        return
    text = (m.get("text") or "").strip()
    if is_admin(uid) and chat_id in admin_pending and text and not text.startswith("/"):
        handle_admin_text(m)
        return
    # ── دروازهٔ مجوز: کاربران جدید پیش‌فرض غیرمجازند؛ ادمین تأیید می‌کند
    if not is_allowed(uid):
        if text.startswith("/id"):
            send(chat_id, f"آیدی عددی تو: <code>{uid}</code>")
            return
        touch_user(m)
        request_access(m, silent=(uid in auth_requested))
        if uid in auth_requested:
            send(chat_id, "درخواست شما قبلاً ثبت شده؛ منتظر تأیید ادمین باش.")
        return
    if text.startswith("/start"):
        cmd_start(m)
    elif text.startswith("/help"):
        with state_lock:
            pl = int(state["config"].get("page_limit", 40))
        send(chat_id, HELP.format(limit=pl))
    elif text.startswith("/id"):
        send(chat_id, f"آیدی عددی تو: <code>{uid}</code>")
    elif text.startswith("/admin") and is_admin(uid):
        send(chat_id, "پنل ادمین ⚙️", reply_markup=admin_menu())
    elif text.startswith("/model") and is_admin(uid):
        send(chat_id, "🧠 مدل ترجمه (مدل‌های رایگان OpenCode Zen):", reply_markup=model_menu())
    elif text.startswith("/lang") and is_admin(uid):
        send(chat_id, "🌐 زبان مبدأ و مقصد:", reply_markup=lang_menu())
    elif text.startswith("/allow") and is_admin(uid):
        parts = text.split()
        if len(parts) == 2 and parts[1].lstrip("@").isdigit():
            au = int(parts[1].lstrip("@"))
            allow_user(au)
            send(chat_id, f"✅ کاربر <code>{au}</code> مجاز شد.")
            try:
                send(au, "✅ دسترسی شما به ربات تأیید شد!\nحالا می‌توانی فایل PDF بفرستی.")
            except TgError:
                pass
        else:
            admin_pending[chat_id] = "allow"
            send(chat_id, "آیدی عددی کاربر را بفرست (یا برای لغو بنویس: لغو)")
    elif text.startswith("/deny") and is_admin(uid):
        parts = text.split()
        if len(parts) == 2 and parts[1].lstrip("@").isdigit():
            du = int(parts[1].lstrip("@"))
            deny_user(du)
            send(chat_id, f"❌ دسترسی کاربر <code>{du}</code> گرفته شد.")
        else:
            send(chat_id, "شکل درست: <code>/deny 123456789</code>")
    elif text.startswith("/ping") and is_admin(uid):
        # تست واقعی سرویس ترجمه — فقط با فرمان ادمین (کلید موقتاً روشن می‌شود)
        eng_p = engine()
        send(chat_id, f"🏓 در حال تست OpenCode Zen با مدل <code>{eng_p['model']}</code>…")
        try:
            opencode_arm()
            try:
                outs = core.oc_translate_segments(
                    eng_p["model"], ["Hello world.", "The sun rises in the east."],
                    src_lang=state["config"].get("src_lang", "auto"),
                    dst_lang=state["config"].get("dst_lang", "fa"),
                    log=lambda *a: None, oc_bin=OC_BIN, oc_cwd=str(TMP_DIR), max_retries=1)
                lines = [f"✅ OpenCode Zen — <code>{eng_p['model']}</code> پاسخ داد:"]
                lines += [f"• {html_mod.escape(o)[:80]}" for o in outs]
            finally:
                opencode_disarm()
        except Exception as e:  # noqa: BLE001
            lines = [f"❌ OpenCode Zen — {short_err(e)}"]
        mt = "✅ متصل" if mtproto_ready() else ("❌ تنظیم نشده" if not (TG_API_ID and TG_API_HASH) else "⏳ در حال اتصال…")
        oc_key = "✅ هست" if OPENCODE_API_KEY else "❌ نیست"
        send(chat_id, "🏓 وضعیت:\n" + "\n".join(lines) +
             f"\n🔑 کلید OpenCode: {oc_key}\n📡 MTProto: {mt}")
    elif "document" in m:
        on_document(m)
    elif text:
        send(chat_id, "فایل PDF را بفرست تا ترجمه‌اش کنم 📄\n/help برای راهنما")


def handle_update(upd: dict) -> None:
    try:
        if "message" in upd:
            handle_message(upd["message"])
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"])
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def polling_loop() -> None:
    offset = 0
    backoff = 2
    conflicts = 0
    drain_noted = False
    while not stop_event.is_set():
        # حالت Actions: پس از پایان چرخه، تا خالی شدن صف ادامه می‌دهیم؛
        # ترجمهٔ در جریان (مثلاً فایل ۲۰۰۰ صفحه‌ای) نباید وسط کار کشته شود.
        if RUN_MAX_MINUTES and time.time() - start_ts > RUN_MAX_MINUTES * 60:
            if not worker_busy.is_set() and job_q.empty():
                # تأیید دوم بعد از چند ثانیه: جاب تازه‌ای بین دو بررسی شروع نشده باشد
                time.sleep(5)
                if worker_busy.is_set() or not job_q.empty():
                    continue
                log(f"زمان چرخه تمام شد ({RUN_MAX_MINUTES:.0f} دقیقه) و صف خالی است — پایان تمیز.")
                return
            if not drain_noted:
                drain_noted = True
                draining.set()
                log("زمان چرخه تمام شد؛ در انتظار اتمام کارهای در جریان "
                    "(فایل جدید پذیرفته نمی‌شود)…")
        try:
            r = requests.get(f"{TG}/getUpdates", params={
                "offset": offset, "timeout": 30,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }, timeout=45)
            data = r.json()
            if not data.get("ok"):
                raise TgError(f"getUpdates: {data.get('description', r.status_code)}")
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                handle_update(upd)
            backoff = 2
            conflicts = 0
        except Exception as e:  # noqa: BLE001
            # اگر دو نمونهٔ ربات همزمان پولینگ کنند، تلگرام 409 Conflict می‌دهد؛
            # با کد خروج ۳ به workflow می‌گوییم سریع و بدون اخلال چرخهٔ بعد را بسازد.
            if "Conflict" in str(e) and "getUpdates" in str(e):
                conflicts += 1
                if conflicts >= 3:
                    log("تداخل پایدار با نمونهٔ دیگر — خروج با کد ۳.")
                    sys.exit(3)
            else:
                conflicts = 0
            log(f"polling: {e} — تلاش مجدد بعد از {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> None:
    global BOT_USERNAME
    if not BOT_TOKEN:
        sys.exit("متغیر محیطی تنظیم نشده: TELEGRAM_TOKEN")
    if not OPENCODE_API_KEY:
        sys.exit("متغیر محیطی تنظیم نشده: OPENCODE_API_KEY")
    if not core.find_opencode(OC_BIN):
        sys.exit("CLI اوپن‌کد پیدا نشد (OPENCODE_BIN یا نصب opencode در workflow لازم است).")
    BOT_USERNAME = (api("getMe", timeout=20) or {}).get("username", "")
    eng = engine()
    log(f"ربات @{BOT_USERNAME} روشن شد. سرویس: {eng['label']} | مدل: {eng['model']} | ادمین: {ADMIN_ID}")
    log(f"دسترسی‌های اولیه: {len(state['config'].get('allowed') or [])} کاربر | "
        f"MTProto: {'فعال' if (TG_API_ID and TG_API_HASH) else 'خاموش'}")
    if RUN_MAX_MINUTES:
        log(f"حالت GitHub Actions: هر چرخه {RUN_MAX_MINUTES:.0f} دقیقه و بعد ری‌استارت زنجیره‌ای.")
    try:
        api("setMyCommands", commands=[
            {"command": "start", "description": "شروع"},
            {"command": "help", "description": "راهنما"},
            {"command": "id", "description": "آیدی عددی من"},
        ], timeout=20)
    except TgError:
        pass
    load_state()
    # اگر مدل ذخیره‌شدهٔ قبلی از سرویس قدیمی بود، به مدل رایگان پیش‌فرض برگرد
    with state_lock:
        mdl = state["config"].get("model", "")
        if not mdl or mdl in ("Top-Tools-Ai",) or mdl.startswith(("openai/", "qwen/", "llama-")):
            state["config"]["model"] = DEFAULT_MODEL
            dirty.set()

    MT.start()  # کلاینت MTProto برای فایل‌های بزرگ (در ترد جدا)

    threading.Thread(target=health_server, daemon=True).start()
    threading.Thread(target=sync_loop, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()

    def bye(signum, _frm):
        log(f"سیگنال {signum} — ذخیرهٔ وضعیت و خروج…")
        try:
            commit_state("shutdown")
        except Exception:  # noqa: BLE001
            pass
        stop_event.set()
        os._exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    polling_loop()

    # polling_loop فقط وقتی برمی‌گردد که صف خالی و worker بیکار باشد؛
    # این بلوک فقط برای اطمینان است (عملاً بلافاصله رد می‌شود).
    if RUN_MAX_MINUTES:
        stop_event.set()
        try:
            commit_state("cycle-end")
        except Exception as e:  # noqa: BLE001
            log(f"کامیت پایان چرخه ناموفق بود: {e}")
        log("چرخه تمام شد ✅")


if __name__ == "__main__":
    main()
