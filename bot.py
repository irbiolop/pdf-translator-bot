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

import base64
import json
import os
import queue
import signal
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
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", core.DEFAULT_MODEL).strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GH_REPO = os.environ.get("GITHUB_REPO", "").strip()  # owner/name
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip() or "main"
DEFAULT_PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "40") or 40)
FORCE_JOIN_ENABLED = os.environ.get("FORCE_JOIN_ENABLED", "0") == "1"
FORCE_JOIN_CHANNEL = os.environ.get("FORCE_JOIN_CHANNEL", "").strip()
PORT = int(os.environ.get("PORT", "10000") or 10000)
# سقف زمان هر چرخه (دقیقه). روی GitHub Actions باید < سقف ۶ ساعتهٔ جاب باشد؛
# صفر یعنی بدون سقف (اجرای محلی/Render).
RUN_MAX_MINUTES = float(os.environ.get("RUN_MAX_MINUTES", "0") or 0)

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_REPO_PATH = "data/state.json"
TMP_DIR = Path("data/tmp")

# «سریع‌ترین زمان ممکن» = کمترین مکثی که با سقف ~۸هزار توکن در دقیقهٔ تیر فری
# بدون رگبار 429 می‌چرخد؛ 429ها هم خودکار طبق retry-after مدیریت می‌شوند.
SPEEDS = {"fast": 15.0, "balanced": 25.0, "safe": 40.0}
SPEED_LABELS = {"fast": "حداکثر سرعت (۱۵ث)", "balanced": "متعادل (۲۵ث)", "safe": "محتاط (۴۰ث)"}
SPEED_ORDER = ["fast", "balanced", "safe"]

MAX_DOWNLOAD = 19 * 1024 * 1024   # سقف دانلود فایل در Bot API تلگرام
MAX_QUEUE = 6                     # حداکثر فایل در صف

state_lock = threading.RLock()
dirty = threading.Event()
job_q: "queue.Queue[dict]" = queue.Queue()
user_pending: set[int] = set()
admin_pending: dict[int, str] = {}   # chat_id -> نوع ورودی منتظره (limit/channel)
worker_busy = threading.Event()
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
            "speed": "fast",
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
    with state_lock:
        payload = json.dumps(state, ensure_ascii=False, indent=1)
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
            f"⚡ سرعت: {SPEED_LABELS.get(cfg.get('speed'), cfg.get('speed'))}",
            f"🤖 مدل: <code>{GROQ_MODEL}</code>",
            f"⏱ آپ‌تایم: {(now - start_ts) // 60:.0f} دقیقه",
        ]
        if top:
            lines.append("")
            lines.append("🏆 برترین کاربران:")
            for i, (uid, u) in enumerate(top, 1):
                nm = (u.get("name") or "").strip() or u.get("username") or f"کاربر {uid}"
                lines.append(f"{i}. {nm} — {u.get('pages', 0)} صفحه، {u.get('files', 0)} فایل")
        return "\n".join(lines)


def admin_menu() -> dict:
    with state_lock:
        c = dict(state["config"])
    b = lambda t, d: {"text": t, "callback_data": d}  # noqa: E731
    return {"inline_keyboard": [
        [b("📊 آمار کاربران", "ad:stats")],
        [b(f"📄 سقف صفحه: {c.get('page_limit')}", "ad:limit"),
         b(f"⚡ سرعت: {SPEED_LABELS.get(c.get('speed'), c.get('speed'))}", "ad:speed")],
        [b(f"📢 کانال اجباری: {'✅ روشن' if c.get('force_join') else '❌ خاموش'}", "ad:toggle")],
        [b(f"🔗 کانال: {c.get('channel') or '— تنظیم نشده —'}", "ad:channel")],
        [b("💾 ذخیرهٔ دستی وضعیت", "ad:sync")],
    ]}


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
    if not chat or not is_admin(uid):
        return
    if data == "ad:stats":
        edit(chat, mid, stats_text(), reply_markup=admin_menu())
    elif data == "ad:limit":
        admin_pending[chat] = "limit"
        with state_lock: pl = state["config"].get("page_limit")
        edit(chat, mid, f"سقف صفحهٔ فعلی: <b>{pl}</b>\nعدد جدید را بفرست (۱ تا ۵۰۰):")
    elif data == "ad:speed":
        with state_lock:
            idx = SPEED_ORDER.index(state["config"]["speed"]) if state["config"]["speed"] in SPEED_ORDER else 0
            state["config"]["speed"] = SPEED_ORDER[(idx + 1) % len(SPEED_ORDER)]
        dirty.set()
        edit(chat, mid, "پنل ادمین ⚙️", reply_markup=admin_menu())
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
            n = max(1, min(500, int(txt)))
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


# --------------------------------------------------------------------------
# پردازش فایل‌ها
# --------------------------------------------------------------------------
WELCOME = (
    "👋 <b>مترجم PDF انگلیسی → فارسی</b>\n\n"
    "فایل PDF انگلیسی را بفرست تا با حفظ چیدمان (موقعیت، اندازه، بولد/رنگ) "
    "به فارسی ترجمه و راست‌به‌چپ برگردانده شود.\n\n"
    f"📄 سقف فعلی: تا <b>{{limit}}</b> صفحه برای هر فایل\n"
    f"🤖 مدل: <code>{GROQ_MODEL}</code>\n\n"
    "💡 فقط کافیست فایل را بفرست؛ پیشرفت ترجمه را همین‌جا نشان می‌دهم."
)

HELP = (
    "📄 کافیست فایل PDF انگلیسی را بفرستی (نه لینک، نه عکس).\n"
    f"سقف صفحه: تا <b>{{limit}}</b> صفحه در هر فایل.\n"
    "ترجمه وفادار به متن اصلی است و چیدمان (جای متن، اندازه، بولد، رنگ) "
    "حفظ و چیدمان به‌صورت آینه‌ای راست‌چین می‌شود.\n\n"
    "دستورها:\n"
    "/start — شروع\n/help — همین راهنما\n/id — آیدی عددی تو"
)


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
    send(chat, WELCOME.format(limit=pl))


def on_document(m: dict) -> None:
    chat = m["chat"]["id"]
    uid = m["from"]["id"]
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
    if int(doc.get("file_size", 0) or 0) > MAX_DOWNLOAD:
        send(chat, "حجم فایل بیشتر از ۱۹ مگابایت است (سقف دانلود Bot API تلگرام). "
                   "فایل را سبک‌تر بفرست یا چند بخشش کن.")
        return
    if uid in user_pending:
        send(chat, "⏳ یک فایل از تو در حال ترجمه است؛ صبر کن تمام شود.")
        return
    if job_q.qsize() >= MAX_QUEUE:
        send(chat, "صف ربات پر است؛ چند دقیقه بعد دوباره امتحان کن.")
        return
    user_pending.add(uid)
    job_q.put({"chat": chat, "uid": uid, "file_id": doc["file_id"], "name": name})
    send(chat, f"🟡 فایل در صف قرار گرفت (جایگاه {job_q.qsize()}). "
               "به‌محض شروع ترجمه خبر می‌دهم.")


def process_job(job: dict) -> None:
    chat, uid = job["chat"], job["uid"]
    file_id, name = job["file_id"], job["name"]
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

    try:
        f = api("getFile", file_id=file_id, timeout=30)
        fp = f.get("file_path", "")
        r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}", timeout=180)
        r.raise_for_status()
        data = r.content
    except Exception as e:  # noqa: BLE001
        edit(chat, stid, f"❌ خطا در دانلود فایل: <code>{e}</code>")
        return

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    in_pdf = TMP_DIR / f"{uid}_{stamp}_in.pdf"
    out_pdf = TMP_DIR / f"{uid}_{stamp}_fa.pdf"
    in_pdf.write_bytes(data)

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

    with state_lock:
        delay = SPEEDS.get(state["config"].get("speed", "fast"), 15.0)
    upd(f"🌍 شروع ترجمهٔ <b>{n_pages}</b> صفحه با <code>{GROQ_MODEL}</code>…", force=True)
    t0 = time.time()

    def progress(done: int, total: int) -> None:
        pct = done * 100 // max(total, 1)
        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        upd(f"🔄 ترجمه: {bar} {done}/{total} صفحه ({pct}٪)")

    try:
        core.translate_pdf(str(in_pdf), str(out_pdf), GROQ_KEY, model=GROQ_MODEL,
                           delay=delay, progress=progress, cancelled=None,
                           log=lambda *a: None, mirror=True)
    except core.FatalTranslationError as e:
        edit(chat, stid, f"❌ خطای پیکربندی ترجمه: <code>{e}</code>\n"
                         "این مشکل کلید/مدل است؛ به ادمین خبر بده.")
        in_pdf.unlink(missing_ok=True)
        return
    except core.TranslationError as e:
        edit(chat, stid, f"❌ ترجمه ناتمام ماند: <code>{e}</code>\n"
                         "سرویس ترجمه شلوغ است؛ چند دقیقه بعد دوباره امتحان کن.")
        in_pdf.unlink(missing_ok=True)
        return

    secs = time.time() - t0
    upd("📤 در حال ارسال فایل ترجمه‌شده…", force=True)
    try:
        upload_document(chat, out_pdf,
                        f"✅ ترجمهٔ «{name}» — {n_pages} صفحه در {secs / 60:.1f} دقیقه")
    except TgError as e:
        edit(chat, stid, f"❌ ارسال فایل ناموفق بود: <code>{e}</code>")
        in_pdf.unlink(missing_ok=True)
        out_pdf.unlink(missing_ok=True)
        return
    in_pdf.unlink(missing_ok=True)
    out_pdf.unlink(missing_ok=True)

    with state_lock:
        rec = state["users"].setdefault(str(uid), {})
        rec["pages"] = rec.get("pages", 0) + n_pages
        rec["files"] = rec.get("files", 0) + 1
        state["totals"]["files"] = state["totals"].get("files", 0) + 1
        state["totals"]["pages"] = state["totals"].get("pages", 0) + n_pages
    dirty.set()
    log(f"فایل «{name}» ({n_pages} صفحه) برای {uid} در {secs:.0f}s ترجمه شد.")


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
                "model": GROQ_MODEL,
                "speed": state.get("config", {}).get("speed"),
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
    elif text.startswith("/ping") and is_admin(uid):
        try:
            core.selftest(GROQ_KEY, GROQ_MODEL, log=lambda *a: None)
            send(chat_id, "✅ اتصال Groq برقرار است و مدل پاسخ داد.")
        except Exception as e:  # noqa: BLE001
            send(chat_id, f"❌ خطای Groq: <code>{e}</code>")
    elif "document" in m:
        on_document(m)
    elif text:
        send(chat_id, "فایل PDF انگلیسی را بفرست تا ترجمه‌اش کنم 📄\n/help برای راهنما")


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
    while not stop_event.is_set():
        # حالت Actions: پایان تمیز چرخه قبل از سقف ۶ ساعتهٔ جاب
        if RUN_MAX_MINUTES and time.time() - start_ts > RUN_MAX_MINUTES * 60:
            log(f"زمان چرخه تمام شد ({RUN_MAX_MINUTES:.0f} دقیقه) — پایان تمیز.")
            return
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
    missing = [k for k, v in (("TELEGRAM_TOKEN", BOT_TOKEN), ("GROQ_API_KEY", GROQ_KEY)) if not v]
    if missing:
        sys.exit(f"متغیر محیطی تنظیم نشده: {', '.join(missing)}")
    BOT_USERNAME = (api("getMe", timeout=20) or {}).get("username", "")
    log(f"ربات @{BOT_USERNAME} روشن شد. مدل: {GROQ_MODEL} | ادمین: {ADMIN_ID}")
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

    # فقط در پایان تمیز چرخه (حالت Actions) به اینجا می‌رسیم؛
    # اول کارِ در جریان را تمام می‌کنیم، بعد وضعیت را ذخیره و خارج می‌شویم.
    if RUN_MAX_MINUTES:
        log("در انتظار پایان ترجمهٔ در جریان (حداکثر ۲۵ دقیقه)…")
        deadline = time.time() + 25 * 60
        while (worker_busy.is_set() or not job_q.empty()) and time.time() < deadline:
            time.sleep(5)
        stop_event.set()
        try:
            commit_state("cycle-end")
        except Exception as e:  # noqa: BLE001
            log(f"کامیت پایان چرخه ناموفق بود: {e}")
        log("چرخه تمام شد ✅")


if __name__ == "__main__":
    main()
