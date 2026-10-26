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
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import pymupdf
import requests

import gh_store
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
# موازی‌سازی فقط «زمان انتظار» را پنهان می‌کند؛ فاصله‌گذارِ نرخ مشترک است و
# الگوی مصرف در سقف RPM معقول می‌ماند (ضد بن).
SPEEDS_OC = {"fast": (10, 1.0), "balanced": (6, 2.0), "safe": (2, 6.0)}
SPEED_LABELS_OC = {"fast": "موازی ×۱۰ — سریع‌ترین", "balanced": "موازی ×۶ — متعادل",
                   "safe": "موازی ×۲ — محتاط"}
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
# تخمین زمان ترجمه به‌ازای هر صفحه (ثانیه؛ شامل استخراج و بازسازی PDF).
# فقط برای هشدار زمانی استفاده می‌شود؛ با چک‌پوینت، فایل‌های بزرگ دیگر رد نمی‌شوند.
SEC_PER_PAGE_EST = float(os.environ.get("SEC_PER_PAGE_EST", "2.5") or 2.5)
JOB_HARD_MINUTES = float(os.environ.get("JOB_HARD_MINUTES", "345") or 345)  # سقف timeout-minutes ورک‌فلو منهای حاشیه
# مهلت «تخلیهٔ طبیعی» در پایان چرخه؛ بعد از آن کارِ در جریان توقف موقت و چک‌پوینت می‌شود
DRAIN_GRACE_MINUTES = float(os.environ.get("DRAIN_GRACE_MINUTES", "45") or 45)
# از این تعداد صفحه به بالا، قبل از ترجمه پرسیده می‌شود: کل فایل یا بازهٔ صفحه‌ای؟
RANGE_ASK_MIN = int(os.environ.get("RANGE_ASK_MIN", "15") or 15)
# مهلت پاسخ به سوال انتخاب بازه (ثانیه)؛ بی‌پاسخ = کل فایل
RANGE_WAIT_SEC = int(os.environ.get("RANGE_WAIT_SEC", "480") or 480)
# مهلت پاسخ به سوال «متن‌محور یا عکس‌محور» برای فایل‌های کوچک (ثانیه)
MODE_WAIT_SEC = int(os.environ.get("MODE_WAIT_SEC", "45") or 45)
RUN_ID = int(os.environ.get("GITHUB_RUN_ID", "0") or 0)   # برای آپلود artifact چک‌پوینت
CP_DIR = Path("data/cp")
MIN_CYCLE_LEFT_MIN = 10.0  # اگر کمتر از این تا پایان چرخه مانده، کار تازه صفِ چرخهٔ بعد می‌شود
# فصل‌به‌فصل: فایل‌های بزرگ‌تر از این تعداد صفحه، فصل‌به‌فصل ترجمه و گزارش می‌شوند
CHAPTER_MIN_PAGES = int(os.environ.get("CHAPTER_MIN_PAGES", "60") or 60)
# مقصد کپی خروجی برای کارهای ادمین (کاربر بار دیگری درخواست کرده)
DELIVERY_EXTRA = os.environ.get("DELIVERY_EXTRA", "@PrivateUssser").strip()

state_lock = threading.RLock()
dirty = threading.Event()
job_q: "queue.Queue[dict]" = queue.Queue()
superseded: set = set()  # کارهایی که با آپلود تازهٔ همان فایل جایگزین شده‌اند
user_pending: set[int] = set()
auth_requested: set[int] = set()   # کاربرانی که برایشان درخواست دسترسی به ادمین فرستاده شده
admin_pending: dict[int, str] = {}   # chat_id -> نوع ورودی منتظره (limit/channel/allow)
worker_busy = threading.Event()
draining = threading.Event()      # چرخه در حال بسته‌شدن است؛ فایل جدید پذیرفته نمی‌شود
job_pause = threading.Event()     # درخواست توقف موقت کارِ در جریان (چک‌پوینت گرفته می‌شود)
start_ts = time.time()
stop_event = threading.Event()
BOT_USERNAME = ""

# کارهای فعالِ همین چرخه: jobkey → اطلاعات برای دکمه‌های توقف/دریافت/ادامه
active_jobs: dict[str, dict] = {}
# پاسخ کاربر به سوال «کل فایل یا بازه؟»: uid → Event (و متن پاسخ)
range_replies: dict[int, threading.Event] = {}
range_values: dict[int, str] = {}
# پاسخ کاربر به سوال «متن‌محور یا عکس‌محور؟»
mode_values: dict[int, str] = {}
range_needed: dict[int, bool] = {}


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
            "imgq": "small",
            "allowed": list(ALLOWED_ENV),
        },
        "users": {},
        "totals": {"files": 0, "pages": 0},
        "jobs": {},   # کارهای معلق/چک‌پوینت‌شده برای ادامه در چرخهٔ بعد
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
                state["jobs"] = data.get("jobs", {})
            log(f"وضعیت از GitHub بازیابی شد ({len(state['users'])} کاربر، "
                f"{len(state['jobs'])} کارِ معلق).")
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
# چک‌پوینت، بازهٔ صفحه و تحویل فایل
# --------------------------------------------------------------------------
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def cp_dir(jobkey: str) -> Path:
    return CP_DIR / jobkey


def parse_range(txt: str, n_pages: int) -> tuple[int, int] | None:
    """«۱۰-۵۰» / «10 تا 50» / «from 10 to 50» → بازهٔ ۱مبنا (شروع، پایان)؛ نامعتبر = None."""
    t = (txt or "").strip().translate(_FA_DIGITS).lower()
    t = t.replace("از", " ").replace("تا", " ").replace("to", " ").replace("from", " ")
    m = re.search(r"(\d{1,5})\D{1,6}(\d{1,5})", t)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,5})", t)
        if not m:
            return None
        s, e = int(m.group(1)), n_pages  # فقط شروع ← تا پایان فایل
    if s > e:
        s, e = e, s
    s, e = max(1, s), min(n_pages, e)
    if s > e:
        return None
    return s, e


def _fmt_pages(s: int, e: int) -> str:
    return f"{s:,}" if s == e else f"{s:,} تا {e:,}"


def job_progress_kb(jobkey: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "⏸ توقف موقت", "callback_data": f"jb:pause:{jobkey}"},
        {"text": "📥 دریافت تا اینجا", "callback_data": f"jb:part:{jobkey}"},
    ]]}


def paused_kb(jobkey: str, done: int) -> dict:
    return {"inline_keyboard": [[
        {"text": f"▶️ ادامه از صفحهٔ {done + 1:,}", "callback_data": f"jb:resume:{jobkey}"},
        {"text": "📥 دریافت بخش ترجمه‌شده", "callback_data": f"jb:part:{jobkey}"},
    ]]}


MODE_CHOICES = [("auto", "🤖 خودکار"), ("text", "📝 متن‌محور"),
                ("image", "🖼 عکس‌محور (کامیک/اسکن)")]
MODE_LABELS = {"auto": "🤖 خودکار", "text": "📝 متن‌محور", "image": "🖼 عکس‌محور (OCR)"}


def ask_kb(jobkey: str, with_range: bool) -> dict:
    """کیبورد سوال شروع: بازهٔ صفحه (برای فایل بزرگ) + حالت فایل (متن/عکس)."""
    rows = []
    if with_range:
        rows.append([
            {"text": "📖 کل فایل", "callback_data": f"rng:all:{jobkey}"},
            {"text": "✂️ از صفحه … تا صفحه …", "callback_data": f"rng:part:{jobkey}"},
        ])
    rows.append([{"text": t, "callback_data": f"mod:{v}:{jobkey}"} for v, t in MODE_CHOICES])
    return {"inline_keyboard": rows}


def range_only_kb(jobkey: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "📖 کل فایل", "callback_data": f"rng:all:{jobkey}"},
        {"text": "✂️ از صفحه … تا صفحه …", "callback_data": f"rng:part:{jobkey}"},
    ]]}


def deliver_file(chat: int, path: Path, caption: str) -> bool:
    """ارسال فایل خروجی: ≤۴۹.۵MB با Bot API؛ بزرگ‌تر با MTProto."""
    mb = path.stat().st_size / (1024 * 1024)
    if mb <= 49.5:
        try:
            upload_document(chat, path, caption)
            return True
        except TgError as e:
            log(f"آپلود Bot API ناموفق بود؛ تلاش با MTProto: {e}")
    if mtproto_ready():
        return MT.upload(chat, path, caption)
    return False


def merge_partials(old_partial: Path | None, new_part: Path, split_at: int,
                   out_path: Path) -> Path:
    """ادغام پیشرفت قبلی و بخش تازه: صفحات [۰, split_at) از فایل قبلی + بقیه از فایل تازه.
    (split_at = ۰ یعنی کل خروجی همان فایل تازه است.)

    باگ قبلی: سند جدید بعد از خواندن page_count با with بسته می‌شد و insert_pdf
    بعدی روی سندِ بسته ValueError می‌داد → چک‌پوینت دوم/تحویل کارِ ادامه‌یافته
    همیشه کرش می‌کرد («ادامه کار نمی‌کند»)."""
    if not old_partial or split_at <= 0 or not Path(old_partial).exists():
        return Path(new_part)
    fin = pymupdf.open()
    try:
        with pymupdf.open(str(new_part)) as nd:
            n = nd.page_count
            if split_at >= n:  # همهٔ صفحات از قبل ترجمه‌شده‌اند؛ همان فایل قبلی کافی است
                fin.close()
                shutil.copyfile(old_partial, out_path)
                return out_path
            with pymupdf.open(str(old_partial)) as od:
                fin.insert_pdf(od, from_page=0,
                               to_page=min(split_at, od.page_count) - 1)
            fin.insert_pdf(nd, from_page=split_at, to_page=n - 1)
        fin.save(str(out_path), garbage=3, deflate=True)
    finally:
        fin.close()
    return out_path


def save_job_record(jobkey: str, **fields) -> None:
    with state_lock:
        rec = state["jobs"].setdefault(jobkey, {})
        rec.update(fields)
        rec["ts"] = time.time()
    dirty.set()


# --------------------------------------------------------------------------
# ترجمهٔ فصل‌به‌فصل — فایل‌های بزرگ (کتاب) فصل‌به‌فصل ترجمه، چک‌پوینت و گزارش می‌شوند
# --------------------------------------------------------------------------

def book_chapters(pdf_path: Path, rs: int = 1, re_: int | None = None,
                  min_pages: int = 3, max_chunks: int = 45) -> list[dict]:
    """فصل‌های کتاب از فهرست (TOC) PDF برای بازهٔ ۱مبنا [rs, re_].

    خروجی: [{"t": عنوان, "s": شروع, "e": پایان}] (۱مبنا، شامل). اگر فهرست معتبر
    نبود یا فصل‌ها به بازه نخورد، تکه‌های هم‌اندازهٔ «بخش» ساخته می‌شود تا همیشه
    پوشش کامل [rs, re_] با گزارش مرحله‌ای داشته باشیم."""
    try:
        with pymupdf.open(str(pdf_path)) as d:
            n = d.page_count
            toc = d.get_toc(simple=True)
    except Exception:  # noqa: BLE001
        n, toc = 0, []
    if not n or rs > n:
        return []
    rs = max(1, rs)
    re_ = min(n, re_ or n)
    if re_ < rs:
        return []
    pts: list[tuple[str, int]] = []
    for lvl, t, p in (toc or []):
        if lvl != 1 or not p:
            continue
        p = max(1, min(int(p), n))
        if pts and p <= pts[-1][1]:
            continue
        title = re.sub(r"\s+", " ", (t or "").strip())[:60] or "فصل"
        pts.append((title, p))
    cuts = [p for _, p in pts if rs < p <= re_]
    segs: list[tuple[int, int]] = []
    prev = rs
    for p in cuts:
        if p - 1 >= prev:
            segs.append((prev, p - 1))
        prev = p
    if prev <= re_:
        segs.append((prev, re_))
    tmap = {p: t for t, p in pts}
    chs = []
    for i, (s, e) in enumerate(segs):
        t = tmap.get(s) or ("صفحات آغازین" if i == 0 else f"بخش {i + 1}")
        chs.append({"t": t, "s": s, "e": e})
    # ادغام فصل‌های خیلی کوچک با قبلی
    merged: list[dict] = []
    for c in chs:
        if merged and (c["e"] - c["s"] + 1) < min_pages:
            merged[-1]["e"] = c["e"]
        else:
            merged.append(c)
    # سقف تعداد فصل
    while len(merged) > max_chunks:
        try:
            idx = min(range(1, len(merged)), key=lambda i: merged[i]["e"] - merged[i]["s"])
        except ValueError:
            break
        merged[idx - 1]["e"] = merged[idx]["e"]
        merged.pop(idx)
    return merged


def extract_pages(src: Path, s0: int, e0: int, out: Path) -> Path:
    """صفحات [s0, e0] صفرمبنا از سند کامل را به یک PDF کوچک مستقل برش می‌زند."""
    d = pymupdf.open()
    try:
        with pymupdf.open(str(src)) as s:
            d.insert_pdf(s, from_page=s0, to_page=e0)
        d.save(str(out), garbage=3, deflate=True)
    finally:
        d.close()
    return out


def build_master(in_pdf: Path, chapters: list[dict], done_k: int, ch_dir: Path,
                 out_path: Path, img_quality: str = "small",
                 finalize: bool = True, log=log) -> Path:
    """مونتاژ نسخهٔ کامل/میانی: صفحات فصل‌های کامل‌شده از فایل فصل‌ها، بقیه از اصل.

    finalize=True فقط برای تحویل نهایی: زیرمجموعه‌سازی فونت + بازفشرده‌سازی
    تصاویر یک‌بار روی کل سند (در ترجمهٔ فصل‌به‌فصل این کار عمداً به تعویق افتاده)."""
    fin = pymupdf.open(str(in_pdf))
    try:
        for k in range(min(done_k, len(chapters))):
            ch = chapters[k]
            f = ch_dir / f"ch_{k:03d}.pdf"
            if not f.exists():
                log(f"فایل فصل {k + 1} پیدا نشد؛ آن صفحات از نسخهٔ اصلی می‌ماند.")
                continue
            s0, e0 = ch["s"] - 1, ch["e"] - 1
            try:
                fin.delete_pages(list(range(s0, e0 + 1)))
                with pymupdf.open(str(f)) as nd:
                    fin.insert_pdf(nd, start_at=s0)
            except Exception as e:  # noqa: BLE001
                log(f"ادغام فصل {k + 1} ناموفق بود: {e}")
        if finalize:
            try:
                core._finalize_save(fin, str(out_path), log, img_quality)
            except Exception as e:  # noqa: BLE001
                log(f"ذخیرهٔ نهایی مونتاژ ناموفق بود ({e})؛ ذخیرهٔ ساده…")
                fin.save(str(out_path), garbage=3, deflate=True)
        else:
            fin.save(str(out_path), garbage=3, deflate=True)
    finally:
        fin.close()
    return out_path


def upload_chapter_checkpoint(jobkey: str, in_pdf: Path, ch_pdf: Path, k: int,
                              ref_in: str) -> str:
    """چک‌پوینت فصل: in.pdf فقط بار اول آپلود می‌شود؛ فایل فصل k در تگ خودش.

    خروجی: ref_in (شاید جدید). شکست آپلود مرگبار نیست — فصل در چرخهٔ بعد دوباره
    ترجمه می‌شود."""
    d = cp_dir(jobkey)
    d.mkdir(parents=True, exist_ok=True)
    try:
        if not (d / "in.pdf").exists() and in_pdf.exists():
            shutil.copyfile(in_pdf, d / "in.pdf")
        if (d / "in.pdf").exists() and GH_TOKEN and GH_REPO and not ref_in:
            ref_in = gh_store.upload_release_file(
                GH_REPO, GH_TOKEN, gh_store.anon_tag(jobkey, "in"),
                "in.pdf", d / "in.pdf", branch=GH_BRANCH, log=log)
    except Exception as e:  # noqa: BLE001
        log(f"آپلود in.pdf چک‌پوینت ناموفق بود: {e}")
    try:
        if GH_TOKEN and GH_REPO and ch_pdf.exists():
            gh_store.upload_release_file(
                GH_REPO, GH_TOKEN, gh_store.anon_tag(jobkey, f"ch{k:03d}"),
                "ch.pdf", ch_pdf, branch=GH_BRANCH, log=log)
    except Exception as e:  # noqa: BLE001
        log(f"آپلود چک‌پوینت فصل {k + 1} ناموفق بود: {e}")
    return ref_in


def extra_delivery_chat() -> int | None:
    """چت مقصد کپی خروجی برای کارهای ادمین (یک‌بار resolve و کش می‌شود)."""
    if not DELIVERY_EXTRA or not DELIVERY_EXTRA.startswith("@"):
        return None
    if "id" in extra_delivery_chat.cache:
        return extra_delivery_chat.cache["id"]
    cid = None
    try:
        c = api("getChat", chat_id=DELIVERY_EXTRA, timeout=20)
        cid = int(c.get("id") or 0) or None
    except TgError:
        cid = None
    except Exception:  # noqa: BLE001
        cid = None
    extra_delivery_chat.cache["id"] = cid
    return cid


extra_delivery_chat.cache = {}


def _translate_by_chapters(ctx: dict) -> dict:
    """حلقهٔ فصل‌به‌فصل: هر فصل جدا ترجمه، جدا چک‌پوینت و جدا به کاربر گزارش می‌شود.

    ctx: jobkey, chat, uid, aj, upd, in_pdf, rs, re_, chapters, start_k,
         w, sp, eng, mode, src_lang, dst_lang, imgq, name, n_pages
    خروجی: {"status": "done"|"paused"|"failed", "res": {...}, "done_pages": int, ...}
    """
    jobkey = ctx["jobkey"]
    chat, uid = ctx["chat"], ctx["uid"]
    aj, upd = ctx["aj"], ctx["upd"]
    in_pdf = ctx["in_pdf"]
    chapters = ctx["chapters"]
    n_ch = len(chapters)
    done_k = int(ctx.get("start_k") or 0)
    re_ = ctx["re_"]
    eng, w, sp = ctx["eng"], ctx["w"], ctx["sp"]
    mode = ctx["mode"]
    src_lang, dst_lang = ctx["src_lang"], ctx["dst_lang"]
    imgq = ctx["imgq"]
    n_pages_total = int(ctx.get("n_pages") or 0)
    ch_dir = cp_dir(jobkey)
    ch_dir.mkdir(parents=True, exist_ok=True)
    ref_in = ctx.get("ref_in") or ""
    usage_tot: dict = {}
    agg = {"failed": 0, "skipped": 0, "ocr": 0, "hl": 0}
    ch_refs = list(ctx.get("ch_refs") or [])
    t_loop = time.time()

    def _rec_update(k_done: int) -> None:
        done_pages = chapters[k_done - 1]["e"] if k_done > 0 else 0
        save_job_record(jobkey, status="paused", chapters=json.dumps(chapters, ensure_ascii=False),
                        chapters_done=k_done, ch_refs=list(ch_refs), done_pages=done_pages,
                        ref_in=ref_in, pages_total=n_pages_total, cp_local=True)
        try:
            commit_state("chapter-checkpoint")
        except Exception:  # noqa: BLE001
            pass

    def _ensure_in_uploaded() -> None:
        """اگر هنوز فایل اصلی چک‌پوینت نشده (توقف قبل از اولین فصل)، آپلودش کن تا
        چرخهٔ بعد بتواند از صفرِ همین فایل ادامه دهد — کاربر فایل را دوباره نفرستد."""
        nonlocal ref_in
        if ref_in or not (GH_TOKEN and GH_REPO) or not in_pdf.exists():
            return
        try:
            d = cp_dir(jobkey)
            d.mkdir(parents=True, exist_ok=True)
            if not (d / "in.pdf").exists():
                shutil.copyfile(in_pdf, d / "in.pdf")
            ref_in = gh_store.upload_release_file(
                GH_REPO, GH_TOKEN, gh_store.anon_tag(jobkey, "in"),
                "in.pdf", d / "in.pdf", branch=GH_BRANCH, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"آپلود in.pdf در توقف زودهنگام ناموفق بود: {e}")

    for k in range(done_k, n_ch):
        ch = chapters[k]
        ch_pages = ch["e"] - ch["s"] + 1
        # ── گیت زمان قبل از شروع فصل ──
        if RUN_MAX_MINUTES:
            remaining_min = ((start_ts + JOB_HARD_MINUTES * 60) - time.time()) / 60.0
            overhead = 3.0 if n_pages_total > 300 else 0.5
            ch_min = overhead + ch_pages * SEC_PER_PAGE_EST / 60
            if remaining_min < max(ch_min + 20, MIN_CYCLE_LEFT_MIN + 10):
                _ensure_in_uploaded()
                _rec_update(done_k)
                upd(f"⏳ تا پایان این چرخه برای فصل بعدی («{html_mod.escape(ch['t'])}») وقت کافی نیست؛ "
                    f"چرخهٔ تازه خودش از همین‌جا ادامه می‌دهد "
                    f"({done_k}/{n_ch} فصل کامل شده).", force=True)
                return {"status": "paused", "done_k": done_k, "agg": agg,
                        "usage": usage_tot, "reason": "time"}
        if draining.is_set() or stop_event.is_set():
            _ensure_in_uploaded()
            _rec_update(done_k)
            upd(f"⏳ پایان چرخهٔ سرویس — {done_k}/{n_ch} فصل ذخیره شد؛ چرخهٔ بعد خودش ادامه می‌دهد.",
                force=True)
            return {"status": "paused", "done_k": done_k, "agg": agg,
                    "usage": usage_tot, "reason": "drain"}

        k1 = k + 1
        title = ch["t"]
        upd(f"📘 فصل {k1}/{n_ch}: «{html_mod.escape(title)}» — {ch_pages:,} صفحه…",
            force=True, kb=job_progress_kb(jobkey))

        def progress(d: int, t: int, _s0=ch["s"], _k1=k1, _n=n_ch) -> None:
            done_abs = _s0 - 1 + d
            aj["last_done"] = done_abs
            pct = d * 100 // max(t, 1)
            bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
            upd(f"📘 فصل {_k1}/{_n}: {bar} {d:,}/{t:,} صفحه ({pct}٪)",
                kb=job_progress_kb(jobkey))

        def extract_progress(d: int, t: int, _k1=k1, _n=n_ch) -> None:
            if d % 20 == 0 or d == t:
                upd(f"📖 فصل {_k1}/{_n}: آماده‌سازی متن {d:,}/{t:,} صفحه…",
                    kb=job_progress_kb(jobkey))

        ch_out = TMP_DIR / f"{jobkey}_chrun_{k:03d}.pdf"
        t_ch = time.time()
        try:
            res = core.translate_pdf(
                str(in_pdf), str(ch_out), "", model=eng["model"],
                base_url="", workers=w, spacing=sp,
                progress=progress, cancelled=job_pause,
                extract_progress=extract_progress,
                start_page=ch["s"] - 1, end_page=ch["e"],
                log=lambda *a: None, mirror=True,
                src_lang=src_lang, dst_lang=dst_lang, use_cli=True,
                oc_bin=OC_BIN, oc_cwd=str(TMP_DIR),
                page_mode=mode, ocr_langs=core.tess_lang_for(src_lang),
                img_quality=imgq, plain_save=True)
        except core.FatalTranslationError as err:
            _rec_update(done_k)
            return {"status": "failed", "err": err, "done_k": done_k, "agg": agg,
                    "usage": usage_tot, "kind": "fatal"}
        except core.TranslationError as err:
            _rec_update(done_k)
            return {"status": "failed", "err": err, "done_k": done_k, "agg": agg,
                    "usage": usage_tot, "kind": "trans"}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _rec_update(done_k)
            return {"status": "failed", "err": e, "done_k": done_k, "agg": agg,
                    "usage": usage_tot, "kind": "unexpected"}

        if res.get("cancelled"):
            ch_out.unlink(missing_ok=True)
            _ensure_in_uploaded()
            _rec_update(done_k)
            upd(f"⏸ توقف موقت — تا فصل {done_k}/{n_ch} ذخیره شد؛ با دکمهٔ ادامه از همین‌جا ادامه می‌گیرد.",
                force=True, kb=paused_kb(jobkey, chapters[done_k - 1]["e"] if done_k else 0))
            return {"status": "paused", "done_k": done_k, "agg": agg,
                    "usage": usage_tot, "reason": "cancel"}

        # ── برش فایل کوچک فصل + چک‌پوینت ──
        ch_pdf = ch_dir / f"ch_{k:03d}.pdf"
        try:
            extract_pages(ch_out, ch["s"] - 1, ch["e"] - 1, ch_pdf)
        except Exception as e:  # noqa: BLE001
            log(f"برش فصل {k1} ناموفق بود: {e}")
            return {"status": "failed",
                    "err": f"خطا در ذخیرهٔ فصل {k1}: {e}", "done_k": done_k,
                    "agg": agg, "usage": usage_tot, "kind": "unexpected"}
        ch_out.unlink(missing_ok=True)
        ref_in = upload_chapter_checkpoint(jobkey, in_pdf, ch_pdf, k, ref_in)
        done_k = k + 1
        ch_refs.append(("rel:" + gh_store.anon_tag(jobkey, f"ch{k:03d}"))
                       if GH_TOKEN and GH_REPO else f"local:ch{k:03d}")
        _rec_update(done_k)

        # ── گزارش فصل به کاربر (پیام جدا تا همه‌شان در چت بماند) ──
        u = (res or {}).get("usage", {})
        for kk, vv in u.items():
            usage_tot[kk] = usage_tot.get(kk, 0) + int(vv or 0)
        agg["failed"] += len((res or {}).get("failed_pages", []))
        agg["skipped"] += len((res or {}).get("skipped_pages", []))
        agg["ocr"] += int((res or {}).get("ocr_pages", 0) or 0)
        agg["hl"] += int((res or {}).get("hl_applied", 0) or 0)
        dt_ch = time.time() - t_ch
        done_pages = ch["e"]
        pct_all = done_pages * 100 // max(re_, 1)
        hl_note = f" | 🖍 {int((res or {}).get('hl_applied', 0) or 0):,} هایلایت روی ترجمه نشست" \
            if int((res or {}).get("hl_applied", 0) or 0) else ""
        try:
            send(chat, f"✅ فصل {k1}/{n_ch}: «{html_mod.escape(title)}» — "
                       f"{ch_pages:,} صفحه در {dt_ch / 60:.1f} دقیقه{hl_note}\n"
                       f"📊 پیشرفت کل: {done_pages:,}/{re_:,} صفحه ({pct_all}٪)")
        except TgError:
            pass
        upd(f"📊 فصل {k1}/{n_ch} کامل شد — {done_pages:,}/{re_:,} صفحه ({pct_all}٪)",
            force=True, kb=job_progress_kb(jobkey))

    # ── همهٔ فصل‌ها کامل شد: مونتاژ نهایی ──
    master = TMP_DIR / f"{jobkey}_master.pdf"
    try:
        build_master(in_pdf, chapters, done_k, ch_dir, master, imgq, finalize=True)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return {"status": "failed", "err": f"خطا در مونتاژ نهایی: {e}",
                "done_k": done_k, "agg": agg, "usage": usage_tot, "kind": "unexpected"}
    translated_pages = sum(c["e"] - c["s"] + 1 for c in chapters)
    res = {"output": str(master), "usage": usage_tot,
           "failed_pages": [], "skipped_pages": [],
           "translated": translated_pages, "ocr_pages": agg["ocr"],
           "hl_applied": agg["hl"]}
    return {"status": "done", "res": res, "done_k": done_k, "done_pages": re_,
            "usage": usage_tot, "secs": time.time() - t_loop, "ch_refs": list(ch_refs)}


def get_job_record(jobkey: str) -> dict:
    with state_lock:
        return dict(state["jobs"].get(jobkey) or {})


def drop_job_record(jobkey: str) -> None:
    with state_lock:
        state["jobs"].pop(jobkey, None)
    dirty.set()


def save_checkpoint(jobkey: str, rec: dict, in_pdf: Path, partial: Path,
                    done_pages: int) -> str:
    """چک‌پوینت (PDF ورودی + خروجی ناتمام) را در Release asset ذخیره می‌کند.

    • in.pdf فقط اولین بار آپلود می‌شود (چک‌پوینت‌های بعدی فقط partial را عوض می‌کنند)
    • آپلود استریمی است (فایل چندصد مگابایتی حافظه را نمی‌گیرد)
    • همهٔ فیلدهای لازم برای ادامه (بازه/حالت/صاحب کار) در رکورد state نوشته
      می‌شوند — قبلاً فقط در meta.json بودند و بعد از ادامه گم می‌شدند.
    """
    d = cp_dir(jobkey)
    d.mkdir(parents=True, exist_ok=True)
    try:
        if in_pdf.exists() and in_pdf != d / "in.pdf":
            shutil.move(str(in_pdf), str(d / "in.pdf"))
        if partial.exists() and partial != d / "partial.pdf":
            shutil.move(str(partial), str(d / "partial.pdf"))
    except Exception as e:  # noqa: BLE001
        log(f"آماده‌سازی چک‌پوینت ناموفق بود: {e}")
        return ""
    ref_in = rec.get("ref_in") or ""
    ref_pp = ""
    if GH_TOKEN and GH_REPO:
        try:
            if not ref_in and (d / "in.pdf").exists():
                ref_in = gh_store.upload_release_file(
                    GH_REPO, GH_TOKEN, gh_store.anon_tag(jobkey, "in"),
                    "in.pdf", d / "in.pdf", branch=GH_BRANCH, log=log)
            if (d / "partial.pdf").exists():
                ref_pp = gh_store.upload_release_file(
                    GH_REPO, GH_TOKEN, gh_store.anon_tag(jobkey, "pp"),
                    "partial.pdf", d / "partial.pdf", branch=GH_BRANCH, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"آپلود چک‌پوینت ناموفق بود: {e}")
    # فیلدهای ادامه — قبلاً اینها فقط در meta.json بودند و بازسازی کار از دست می‌رفت
    save_job_record(jobkey, status="paused", done_pages=done_pages,
                    pages_total=rec.get("pages_total"),
                    range_s=rec.get("range_s"), range_e=rec.get("range_e"),
                    uid=rec.get("uid"), chat=rec.get("chat"), name=rec.get("name"),
                    size=rec.get("size"), mode=rec.get("mode") or "auto",
                    ref_in=ref_in, ref_pp=ref_pp, cp_local=True)
    return ref_pp or ref_in


def boot_resume() -> None:
    """چک‌پوینت‌ها و کارهای معلق چرخهٔ قبل را بازیابی و دوباره صف می‌کند."""
    with state_lock:
        jobs = dict(state.get("jobs") or {})
    if not jobs:
        return
    now = time.time()
    n_q = n_r = 0
    seen_files: set[tuple] = set()  # حذف تکراری‌ها: (uid, size) برای صف/سوال
    for key, rec in sorted(jobs.items(), key=lambda kv: (kv[1].get("ts") or 0)):
        st = rec.get("status")
        if st not in ("queued", "paused", "failed_cycle", "resuming", "ask"):
            drop_job_record(key)
            continue
        if now - (rec.get("ts") or 0) > 14 * 86400:  # چک‌پوینت کهنه
            drop_job_record(key)
            for r in (rec.get("ref_in") or "", rec.get("ref_pp") or ""):
                gh_store.cleanup_release(GH_REPO, GH_TOKEN, r)
            for r in (rec.get("ch_refs") or []):
                gh_store.cleanup_release(GH_REPO, GH_TOKEN, r)
            continue
        if st in ("queued", "ask"):
            sig = (rec.get("uid"), rec.get("size"))
            if sig in seen_files:
                # فایل تکراری (همان کاربر همان فایل چندبار فرستاده) — قدیمی حذف می‌شود
                drop_job_record(key)
                continue
            seen_files.add(sig)
            job = {k: v for k, v in rec.items()
                   if k in ("chat", "uid", "file_id", "mid", "size", "name",
                            "uname", "asked", "mode")}
            job["jobkey"] = key
            if rec.get("range_e"):
                job["range"] = (int(rec.get("range_s") or 1), int(rec["range_e"]))
            if st == "ask":
                save_job_record(key, asked=False, status="queued")  # سوال دوباره پرسیده می‌شود
            job_q.put(job)
            n_q += 1
            continue
        # paused / failed_cycle → چک‌پوینت بیاور
        d = cp_dir(key)
        if rec.get("chapters"):
            # حالت فصل‌به‌فصل: in.pdf + هر فایل فصل کافی است (partial.pdf ندارد)
            ok = True
            if not (d / "in.pdf").exists():
                ok = bool(rec.get("ref_in")) and bool(GH_TOKEN and GH_REPO) and \
                    gh_store.download_release_file(GH_REPO, GH_TOKEN, rec["ref_in"], d,
                                                   fname="in.pdf", log=log)
            n_ok = 0
            for i, ref in enumerate(rec.get("ch_refs") or []):
                f = d / f"ch_{i:03d}.pdf"
                if f.exists():
                    n_ok += 1
                elif ref and GH_TOKEN and GH_REPO and \
                        gh_store.download_release_file(GH_REPO, GH_TOKEN, ref, d,
                                                       fname="ch.pdf", log=log):
                    try:
                        (d / "ch.pdf").rename(f)
                        n_ok += 1
                    except Exception:  # noqa: BLE001
                        pass
            if not ok or not (d / "in.pdf").exists():
                log(f"چک‌پوینت فصلی «{rec.get('name')}» بازیابی نشد؛ به کاربر خبر می‌دهم.")
                drop_job_record(key)
                try:
                    send(rec.get("chat"), f"⚠️ ادامهٔ ترجمهٔ «{html_mod.escape(str(rec.get('name') or 'فایل'))}» "
                                          "ممکن نشد (چک‌پوینت در دسترس نبود). لطفاً فایل را دوباره بفرست.")
                except TgError:
                    pass
                continue
            if n_ok < len(rec.get("ch_refs") or []):
                # بعضی فصل‌ها نیامدند؛ همان تعداد موجود ادامه می‌گیرد (فصل‌های جامانده دوباره ترجمه می‌شوند)
                save_job_record(key, ch_refs=(rec.get("ch_refs") or [])[:n_ok])
            job_q.put({"resume": key})
            n_r += 1
            continue
        ok = (d / "in.pdf").exists() and (d / "partial.pdf").exists()
        if not ok and GH_TOKEN and GH_REPO:
            if rec.get("ref_in") and not (d / "in.pdf").exists():
                ok = gh_store.download_release_file(GH_REPO, GH_TOKEN, rec["ref_in"], d,
                                                    fname="in.pdf", log=log)
            if ok and rec.get("ref_pp") and not (d / "partial.pdf").exists():
                ok = gh_store.download_release_file(GH_REPO, GH_TOKEN, rec["ref_pp"], d,
                                                    fname="partial.pdf", log=log)
        if not ok:
            log(f"چک‌پوینت «{rec.get('name')}» بازیابی نشد؛ به کاربر خبر می‌دهم.")
            drop_job_record(key)
            try:
                send(rec.get("chat"), f"⚠️ ادامهٔ ترجمهٔ «{html_mod.escape(str(rec.get('name') or 'فایل'))}» "
                                      "ممکن نشد (چک‌پوینت در دسترس نبود). لطفاً فایل را دوباره بفرست.")
            except TgError:
                pass
            continue
        job_q.put({"resume": key})
        n_r += 1
    if n_q or n_r:
        log(f"ادامهٔ کارهای چرخهٔ قبل: {n_q} کار در صف + {n_r} چک‌پوینت بازیابی شد.")
        try:
            commit_state("boot-resume")
        except Exception:  # noqa: BLE001
            pass


def snapshot_queues_to_state() -> None:
    """در پایان چرخه: کارهای صف‌نشسته را برای چرخهٔ بعد در state ذخیره کن."""
    moved = 0
    while True:
        try:
            job = job_q.get_nowait()
        except queue.Empty:
            break
        job_q.task_done()
        if job.get("resume"):
            save_job_record(job["resume"], status="paused")  # چک‌پوینت هنوز معتبر است
            moved += 1
            continue
        key = job.get("jobkey") or f"{job.get('uid')}_{int(time.time())}_{moved}"
        save_job_record(key, status="queued", chat=job.get("chat"),
                        uid=job.get("uid"), file_id=job.get("file_id"),
                        mid=job.get("mid"), size=job.get("size"),
                        name=job.get("name"), uname=job.get("uname"),
                        range=job.get("range"), asked=job.get("asked"),
                        mode=job.get("mode"))
        moved += 1
    if moved:
        log(f"{moved} کارِ صف‌نشده برای چرخهٔ بعد ذخیره شد.")


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
        self._progress = None  # (mb, done) برای نمایش پیشرفت
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
        import concurrent.futures
        try:
            fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # کوروتین معلق باید واقعاً کنسل شود وگرنه حلقهٔ MTProto برای همیشه گیر می‌کند
            try:
                fut.cancel()
            except Exception:  # noqa: BLE001
                pass
            self.last_error = f"تایم‌اوت {int(timeout)} ثانیه‌ای MTProto"
            log(f"MTProto: {self.last_error}")
            return None
        except Exception as e:  # noqa: BLE001
            log(f"MTProto call ناموفق بود: {type(e).__name__}: {e}")
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    async def _fetch_msg(self, peer, message_id: int):
        import asyncio as _aio
        msg = await _aio.wait_for(self._client.get_messages(peer, ids=[message_id]), timeout=90.0)
        m = msg[0] if isinstance(msg, list) else msg
        if not m or not getattr(m, "document", None):
            return None
        try:
            self._remember_media(peer.user_id if hasattr(peer, "user_id") else 0,
                                 message_id, m)
        except Exception:  # noqa: BLE001
            pass
        return m

    async def _dl_stream(self, m, dest: Path) -> bool:
        """دانلود تکه‌به‌تکه با ساعت‌چی ضد گیر: هر تکه باید ظرف ۱۲۰ ثانیه برسد.

        علت باگ «دانلود گیر می‌کند»: download_media بدون هیچ سقفی روی پاسخ تکه‌ها
        منتظر می‌ماند و انتقال متوقف‌شده تا ۳۰ دقیقه (تایم‌اوت مسیر) بلوک می‌کرد."""
        import asyncio as _aio
        dest = Path(dest)
        tmp = dest.with_suffix(".part_dl")
        got = 0
        last_log = time.time()
        try:
            with open(tmp, "wb") as f:
                it = self._client.iter_download(m, request_size=512 * 1024,
                                                chunk_size=512 * 1024)
                while True:
                    try:
                        chunk = await _aio.wait_for(it.__anext__(), timeout=120.0)
                    except StopAsyncIteration:
                        break
                    except _aio.TimeoutError:
                        raise RuntimeError(
                            f"دانلود متوقف شد — تکه‌ای ظرف ۱۲۰ ثانیه نرسید ({got / 1048576:.0f}MB گرفته شده)")
                    if chunk:
                        f.write(chunk)
                        got += len(chunk)
                        if time.time() - last_log > 30:
                            last_log = time.time()
                            log(f"دانلود MTProto: {got / 1048576:.0f}MB…")
            if got <= 0:
                raise RuntimeError("هیچ داده‌ای دانلود نشد")
            tmp.replace(dest)
            return True
        finally:
            try:
                if not dest.exists() and tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    def download(self, chat_id: int, message_id: int, dest: Path) -> bool:
        """دانلود مدیای یک پیام با MTProto (بدون سقف ۲۰MB Bot API) — ضد گیرِ کامل.

        ۱) کش مدیای زندهٔ کلاینت مشترک (پیام‌هایی که همین حالا رسیده‌اند)
        ۲ و ۳) کلاینت تازهٔ مستقل با event loop خودش در همین ترد — هر تلاش ایزوله است
        و گیر کردنش هیچ‌وقت کلاینت مشترک را مسموم نمی‌کند؛ همهٔ awaitها ساعت‌چی دارند."""
        import asyncio
        chat_id, message_id = int(chat_id), int(message_id)
        self.last_error = ""
        dest = Path(dest)

        def _clean_partial():
            try:
                dest.with_suffix(".part_dl").unlink(missing_ok=True)
                dest.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

        # ۱) پیام زندهٔ کش‌شده (کاملاً بدون رزولوشن)
        m = self._take_media(chat_id, message_id, timeout=20.0)
        if m is not None:
            if self._call(lambda: self._dl_stream(m, dest), timeout=1800.0):
                try:
                    if self._progress:
                        self._progress(dest.stat().st_size / 1048576, True)
                except Exception:  # noqa: BLE001
                    pass
                return True
            _clean_partial()

        # ۲ و ۳) دو تلاش با کلاینت تازهٔ مستقل (hash صفر + رزولوشن عمومی)
        for attempt in (1, 2):
            if attempt == 2:
                time.sleep(15.0)
            ok, err = self._download_fresh(chat_id, message_id, dest)
            if ok:
                return True
            self.last_error = err or self.last_error
            log(f"دانلود با کلاینت تازه (تلاش {attempt}) ناموفق بود: {err}")
            _clean_partial()

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

    def _download_fresh(self, chat_id: int, message_id: int, dest: Path) -> tuple[bool, str]:
        """یک تلاش دانلود با کلاینت تازهٔ مستقل (loop خودش) — کامل ایزوله و ساعت‌چی‌دار."""
        import asyncio

        last_err = ""

        async def _run() -> bool:
            nonlocal last_err
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            from telethon.tl.types import InputPeerUser
            client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH,
                                    request_retries=3, connection_retries=3,
                                    timeout=30)
            await asyncio.wait_for(client.connect(), timeout=60.0)
            try:
                if not await client.is_user_authorized():
                    await asyncio.wait_for(client.start(bot_token=BOT_TOKEN), timeout=60.0)
                m = None
                for peer in (InputPeerUser(int(chat_id), 0), int(chat_id)):
                    try:
                        msg = await asyncio.wait_for(
                            client.get_messages(peer, ids=[int(message_id)]), timeout=90.0)
                        mm = msg[0] if isinstance(msg, list) else msg
                        if mm is not None and getattr(mm, "document", None):
                            m = mm
                            break
                    except Exception as e:  # noqa: BLE001
                        last_err = f"{type(e).__name__}: {e}"
                if m is None:
                    raise RuntimeError(last_err or "پیام با این آیدی پیدا نشد")
                tmp = dest.with_suffix(".part_dl")
                got = 0
                last_ui = time.time()
                with open(tmp, "wb") as f:
                    it = client.iter_download(m, request_size=512 * 1024,
                                              chunk_size=512 * 1024)
                    while True:
                        try:
                            chunk = await asyncio.wait_for(it.__anext__(), timeout=120.0)
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            raise RuntimeError(
                                f"دانلود متوقف شد — تکه‌ای ظرف ۱۲۰ ثانیه نرسید ({got / 1048576:.0f}MB)")
                        if chunk:
                            f.write(chunk)
                            got += len(chunk)
                            if self._progress and time.time() - last_ui > 20:
                                last_ui = time.time()
                                try:
                                    self._progress(got / 1048576, False)
                                except Exception:  # noqa: BLE001
                                    pass
                if got <= 0:
                    raise RuntimeError("هیچ داده‌ای دانلود نشد")
                tmp.replace(dest)
                return True
            finally:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

        try:
            return bool(asyncio.run(_run())), last_err
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    async def _dl_by_peer(self, peer, message_id: int, dest: Path) -> bool:
        m = await self._fetch_msg(peer, message_id)
        if m is None:
            self.last_error = self.last_error or "پیام با این آیدی پیدا نشد"
            return False
        return await self._dl_stream(m, dest)

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
            f"💾 چک‌پوینت‌های معلق: {sum(1 for j in (state.get('jobs') or {}).values() if j.get('status') == 'paused')}",
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


IMGQ_LABELS = {"small": "کم — کوچک‌ترین فایل", "balanced": "متعادل", "high": "بالا — بی‌تغییر"}
IMGQ_ORDER = ["small", "balanced", "high"]


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
        [b(f"🌐 زبان: {src} → {dst}", "ad:lang"),
         b(f"🖼 کیفیت خروجی: {IMGQ_LABELS.get(c.get('imgq', 'small'))}", "ad:imgq")],
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


def handle_send_partial(chat: int, jkey: str, aj: dict, rec: dict) -> None:
    """دکمهٔ «دریافت تا اینجا»: آخرین snapshot ذخیره‌شده را می‌فرستد (بدون توقف ترجمه)."""
    def _do() -> None:
        try:
            chapters = None
            try:
                if rec.get("chapters"):
                    chapters = json.loads(rec["chapters"])
            except Exception:  # noqa: BLE001
                chapters = None
            if chapters:
                # حالت فصل‌به‌فصل: مونتاژ از فایل‌های فصل (چون master مدام ذخیره نمی‌شود)
                d = cp_dir(jkey)
                done_k = sum(1 for i in range(len(chapters))
                             if (d / f"ch_{i:03d}.pdf").exists())
                if not done_k or not (d / "in.pdf").exists():
                    send(chat, "⏳ هنوز فصلی کامل نشده؛ چند دقیقهٔ دیگر دوباره بزن.")
                    return
                snap = TMP_DIR / f"snap_{jkey}.pdf"
                try:
                    build_master(d / "in.pdf", chapters, done_k, d, snap,
                                 finalize=False)
                    done = chapters[done_k - 1]["e"]
                    cap = (f"📥 بخش ترجمه‌شدهٔ «{html_mod.escape(str(aj.get('name') or rec.get('name') or 'فایل'))}» — "
                           f"تا فصل {done_k}/{len(chapters)} (صفحهٔ {done:,})؛ بقیه در فایل متن اصلی است")
                    if deliver_file(chat, snap, cap):
                        try:
                            send(chat, "ℹ️ این نسخهٔ میانی است؛ نسخهٔ کامل بعد از پایان ترجمه ارسال می‌شود.")
                        except TgError:
                            pass
                    else:
                        send(chat, "❌ ارسال فایل ناموفق بود؛ دوباره امتحان کن.")
                except Exception as e:  # noqa: BLE001
                    log(f"مونتاژ/ارسال بخش میانی فصلی ناموفق بود: {e}")
                    send(chat, "❌ آماده‌سازی نسخهٔ میانی ناموفق بود؛ چند لحظهٔ دیگر دوباره بزن.")
                finally:
                    snap.unlink(missing_ok=True)
                return
            cands = []
            if aj.get("part_pdf") and Path(aj["part_pdf"]).exists():
                cands.append(Path(aj["part_pdf"]))
            d = cp_dir(jkey)
            if (d / "partial.pdf").exists():
                cands.append(d / "partial.pdf")
            src = cands[0] if cands else None
            if src is None:
                send(chat, "⏳ هنوز ذخیرهٔ میانگیری انجام نشده (هر ۲۵ صفحه یک‌بار ذخیره می‌شود)؛ "
                           "چند دقیقهٔ دیگر دوباره بزن.")
                return
            snap = TMP_DIR / f"snap_{jkey}.pdf"
            try:
                shutil.copyfile(src, snap)
                try:  # اعتبارسنجی: کپی وسط نوشتن می‌تواند ناقص باشد
                    with pymupdf.open(str(snap)) as _chk:
                        _chk.page_count
                except Exception:
                    snap.unlink(missing_ok=True)
                    send(chat, "⏳ همین لحظه در حال ذخیرهٔ میانگیری بودم؛ چند ثانیهٔ دیگر دوباره بزن.")
                    return
                done = aj.get("last_done") or rec.get("done_pages") or 0
                cap = (f"📥 بخش ترجمه‌شدهٔ «{html_mod.escape(str(aj.get('name') or rec.get('name') or 'فایل'))}» — "
                       f"تا صفحهٔ {done:,} (بقیهٔ صفحات در فایل به‌صورت متن اصلی مانده)")
                if deliver_file(chat, snap, cap):
                    try:
                        send(chat, "ℹ️ این نسخهٔ میانی است؛ نسخهٔ کامل بعد از پایان ترجمه ارسال می‌شود.")
                    except TgError:
                        pass
                else:
                    send(chat, "❌ ارسال فایل ناموفق بود؛ دوباره امتحان کن.")
            except Exception as e:  # noqa: BLE001
                log(f"ارسال بخش میانی ناموفق بود: {e}")
                send(chat, "❌ ارسال بخش میانی ناموفق بود؛ چند لحظهٔ دیگر دوباره بزن.")
            finally:
                snap.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=_do, daemon=True, name="partial-send").start()


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
    # دکمه‌های کارِ ترجمه (توقف/دریافت/ادامه/بازه/حالت) مال کاربرِ صاحب کار است؛ بقیه فقط ادمین
    prefix, _, rest = data.partition(":")
    if prefix in ("jb", "rng", "mod"):
        kind, _, jkey = rest.partition(":")
        try:
            aj = active_jobs.get(jkey) or {}
            rec = get_job_record(jkey)
            owner = aj.get("uid") or rec.get("uid")
            if not (is_admin(uid) or (owner and int(owner) == int(uid))):
                return
            if prefix == "rng":
                ev = range_replies.get(int(uid))
                if ev is None or ev.is_set():
                    return
                if kind == "all":
                    range_values[int(uid)] = "all"
                    ev.set()
                elif kind == "part":
                    edit(chat, mid, "✂️ بازهٔ صفحه را بفرست (مثلاً: <code>۱۰-۵۰</code> یا <code>۱۰ تا ۵۰</code>)\n"
                                    "فقط شروع را بفرستی، تا پایان فایل ترجمه می‌شود.\n"
                                    "برای لغو بنویس: <code>لغو</code>")
                return
            if prefix == "mod":
                ev = range_replies.get(int(uid))
                if ev is None or ev.is_set() or kind not in MODE_LABELS:
                    return
                mode_values[int(uid)] = kind
                if range_needed.get(int(uid)) and not range_values.get(int(uid)):
                    # هنوز جواب بازه نیامده؛ فقط حالت ثبت و ردیفش حذف می‌شود
                    try:
                        edit(chat, mid, f"✅ حالت ترجمه: {MODE_LABELS[kind]}\n"
                                        "حالا کل فایل یا بازهٔ صفحه را انتخاب کن:",
                             reply_markup=range_only_kb(jkey))
                    except TgError:
                        pass
                    return
                ev.set()
                try:
                    edit(chat, mid, f"✅ حالت ترجمه: {MODE_LABELS[kind]}")
                except TgError:
                    pass
                return
            # prefix == "jb"
            if kind == "pause":
                job_pause.set()
                try:
                    edit(chat, mid, "⏸ درخواست توقف ثبت شد؛ تا پایان صفحهٔ جاری صبر کن…")
                except TgError:
                    pass
                return
            if kind == "part":
                handle_send_partial(chat, jkey, aj, rec)
                return
            if kind == "resume":
                if draining.is_set():
                    try:
                        send(chat, "⏳ چرخه در حال بسته‌شدن است؛ چرخهٔ بعدی (چند دقیقهٔ دیگر) "
                                   "خودکار ادامه می‌دهد.")
                    except TgError:
                        pass
                    return
                if rec.get("status") != "paused":
                    return  # قبلاً صف شده یا تمام شده
                d = cp_dir(jkey)
                if not (d / "in.pdf").exists() and not rec.get("ref_in"):
                    try:
                        send(chat, "⚠️ چک‌پوینت در دسترس نیست؛ لطفاً فایل را دوباره بفرست.")
                    except TgError:
                        pass
                    return
                save_job_record(jkey, status="resuming")
                job_q.put({"resume": jkey})
                if rec.get("chapters"):
                    rmsg = f"▶️ ادامهٔ ترجمه از فصل {(rec.get('chapters_done') or 0) + 1} در صف گذاشته شد."
                else:
                    rmsg = f"▶️ ادامهٔ ترجمه از صفحهٔ {(rec.get('done_pages') or 0) + 1:,} در صف گذاشته شد."
                try:
                    send(chat, rmsg)
                except TgError:
                    pass
                return
        except (ValueError, TypeError):
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
    elif data == "ad:imgq":
        with state_lock:
            cur = state["config"].get("imgq", "small")
            state["config"]["imgq"] = IMGQ_ORDER[(IMGQ_ORDER.index(cur) + 1) % len(IMGQ_ORDER)] \
                if cur in IMGQ_ORDER else "small"
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
    "🖼 فایل‌های عکس‌محور (اسکن، اسکرین‌شات کتاب، کامیک) هم با <b>OCR</b> ترجمه می‌شوند — "
    "بعد از ارسال فایل، حالتش را انتخاب کن\n"
    "✂️ فایل‌های بزرگ: می‌توانی «از صفحه … تا صفحه …» ترجمه بگیری\n"
    "⏸ حین ترجمه: دکمهٔ توقف موقت و دریافت بخش ترجمه‌شده\n"
    "🔁 فایل‌های خیلی بزرگ: اگر در یک چرخه (۶ ساعت) کامل نشوند، از همان‌جا در چرخهٔ بعد خودکار ادامه می‌گیرند"
)

HELP = (
    "📄 کافیست فایل PDF را بفرستی (نه لینک، نه عکس).\n"
    f"سقف صفحه: تا <b>{{limit}}</b> صفحه در هر فایل (برای کل فایل یا هر بازه).\n"
    "ترجمه وفادار به متن اصلی است و چیدمان (جای متن، عکس، جدول، اندازه، بولد، رنگ) "
    "حفظ و برای زبان مقصد راست‌چین/آینه‌ای می‌شود.\n\n"
    "🖼 <b>فایل عکس‌محور (اسکن/اسکرین‌شات/کامیک):</b> بعد از ارسال، «عکس‌محور» را بزن؛ "
    "متن با OCR از تصویر برداشته، ترجمه و روی همان جای تصویر نشسته می‌شود. "
    "«خودکار» هم صفحات بی‌متن را خودش با OCR می‌گیرد.\n"
    "✂️ <b>ترجمهٔ بازه‌ای:</b> برای فایل‌های ۱۵ صفحه‌ای به بالا، اول پرسیده می‌شود؛ "
    "بازه را مثل <code>۱۰-۵۰</code> بفرست.\n"
    "⏸ <b>توقف موقت:</b> دکمهٔ زیر پیام پیشرفت — تا همان‌جا ذخیره می‌شود.\n"
    "📥 <b>دریافت تا اینجا:</b> آخرین snapshot ترجمه‌شده را می‌فرستد (بدون توقف).\n"
    "▶️ <b>ادامه:</b> با دکمه یا خودکار در چرخهٔ بعد (فایل‌های خیلی بزرگ بین چرخه‌های ۶ ساعته از همان‌جا ادامه می‌گیرند).\n\n"
    "دستورها:\n/start — شروع\n/help — همین راهنما\n/id — آیدی عددی تو"
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
    ev = range_replies.get(uid)
    if ev is not None and not ev.is_set():
        send(chat, "⏳ اول به سوال قبلی جواب بده (کل فایل یا بازهٔ صفحه را بفرست).")
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
    # ضد تکرار هوشمند: اگر همین فایل در حال ترجمه/چک‌پوینت فعال است، رد می‌شود؛
    # ولی اگر فقط صف شده یا دانلودش گیر کرده، آپلود تازه جایگزینش می‌شود
    # (پیام تازه = مسیر دانلود زندهٔ MTProto که مطمئن‌ترین راه است).
    with state_lock:
        old_keys = [k for k, r in (state.get("jobs") or {}).items()
                    if int((r or {}).get("uid") or -1) == uid
                    and int((r or {}).get("size") or -2) == fsize]
    in_queue = any(j.get("uid") == uid and j.get("size") == fsize
                   for j in list(job_q.queue) if j.get("jobkey") not in superseded)
    protected = [k for k in old_keys
                 if (k in active_jobs and k not in superseded)
                 or get_job_record(k).get("status") in ("paused", "resuming")]
    if protected or (in_queue and not old_keys):
        send(chat, "⏳ همین فایل قبلاً در صف یا حال ترجمه است (خودکار ادامه می‌گیرد)؛ "
                   "نیازی به فرستادن دوباره نیست.")
        return
    for k in old_keys:
        if k not in protected:
            superseded.add(k)
            drop_job_record(k)
    if in_queue:
        # نسخهٔ قدیمی در صف فقط نادیده گرفته می‌شود (حذف از Queue امن نیست)
        send(chat, "♻️ نسخهٔ قبلیِ همین فایل کنار گذاشته شد؛ همین نسخهٔ تازه را می‌گیرم.")
    user_pending.add(uid)
    jobkey = f"{uid}_{int(time.time())}"
    job_q.put({"chat": chat, "uid": uid, "file_id": doc["file_id"],
               "mid": m.get("message_id"), "size": fsize, "name": name,
               "uname": (m.get("from", {}) or {}).get("username") or "",
               "jobkey": jobkey})
    send(chat, f"🟡 فایل در صف قرار گرفت (جایگاه {job_q.qsize()}). "
               "به‌محض شروع ترجمه خبر می‌دهم.")


def process_job(job: dict) -> None:
    """یک کار ترجمه را کامل انجام می‌دهد: دانلود، پرسش بازه/حالت، ترجمه، تحویل/چک‌پوینت."""
    jobkey = job.get("jobkey") or f"{job.get('uid')}_{int(time.time())}"

    # ── مسیر ادامه از چک‌پوینت ──
    if job.get("resume"):
        rec = get_job_record(jobkey)
        if not rec or rec.get("status") not in ("resuming", "paused", "failed_cycle", "queued"):
            return
        d = cp_dir(jobkey)
        if not (d / "in.pdf").exists() and rec.get("ref_in") and GH_TOKEN and GH_REPO:
            gh_store.download_release_file(GH_REPO, GH_TOKEN, rec["ref_in"], d,
                                           fname="in.pdf", log=log)
        ch_mode = bool(rec.get("chapters"))
        if ch_mode:
            # فصل‌ها: هر چک‌پوینت فصل، تگ خودش را دارد
            refs = list(rec.get("ch_refs") or [])
            for i, ref in enumerate(refs):
                f = d / f"ch_{i:03d}.pdf"
                if ref and not f.exists() and GH_TOKEN and GH_REPO:
                    if gh_store.download_release_file(GH_REPO, GH_TOKEN, ref, d,
                                                      fname="ch.pdf", log=log):
                        try:
                            (d / "ch.pdf").rename(f)
                        except Exception:  # noqa: BLE001
                            pass
            if not (d / "in.pdf").exists():
                log(f"چک‌پوینت {jobkey} ناقص است (in.pdf).")
                drop_job_record(jobkey)
                try:
                    send(rec.get("chat"), "⚠️ ادامهٔ ترجمه ممکن نشد (چک‌پوینت در دسترس نبود). "
                                          "لطفاً فایل را دوباره بفرست.")
                except TgError:
                    pass
                return
        else:
            if (d / "in.pdf").exists() and not (d / "partial.pdf").exists() \
                    and rec.get("ref_pp") and GH_TOKEN and GH_REPO:
                gh_store.download_release_file(GH_REPO, GH_TOKEN, rec["ref_pp"], d,
                                               fname="partial.pdf", log=log)
            if not (d / "in.pdf").exists() or not (d / "partial.pdf").exists():
                log(f"چک‌پوینت {jobkey} ناقص است (in/partial).")
                drop_job_record(jobkey)
                try:
                    send(rec.get("chat"), "⚠️ ادامهٔ ترجمه ممکن نشد (چک‌پوینت در دسترس نبود). "
                                          "لطفاً فایل را دوباره بفرست.")
                except TgError:
                    pass
                return
        job = {"chat": rec.get("chat"), "uid": rec.get("uid"), "name": rec.get("name"),
               "size": rec.get("size"), "uname": rec.get("uname"), "jobkey": jobkey,
               "mode": rec.get("mode") or "auto"}
        if rec.get("range_e"):
            job["range"] = (int(rec.get("range_s") or 1), int(rec["range_e"]))
        return _process_file(job, resume_rec=rec)

    return _process_file(job)


def _process_file(job: dict, resume_rec: dict | None = None) -> None:
    chat, uid = int(job["chat"]), int(job["uid"])
    jobkey = job["jobkey"]
    name = job.get("name") or "file.pdf"
    fsize = int(job.get("size", 0) or 0)
    mid = job.get("mid")
    st = send(chat, "⬇️ در حال دریافت فایل…")
    stid = st["message_id"]
    last_edit = [0.0]

    def upd(text: str, force: bool = False, kb: dict | None = None) -> None:
        if force or time.time() - last_edit[0] > 9:
            last_edit[0] = time.time()
            try:
                edit(chat, stid, text, reply_markup=kb)
            except TgError:
                pass

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    in_pdf = TMP_DIR / f"{uid}_{stamp}_in.pdf"
    out_pdf = TMP_DIR / f"{uid}_{stamp}_fa.pdf"

    aj = {"uid": uid, "chat": chat, "stid": stid, "name": name,
          "part_pdf": "", "last_done": 0}
    active_jobs[jobkey] = aj
    try:
        if resume_rec:
            d = cp_dir(jobkey)
            in_pdf = d / "in.pdf"
            partial_old = d / "partial.pdf" if (d / "partial.pdf").exists() else None
            done_old = int(resume_rec.get("done_pages") or 0)
            n_pages = int(resume_rec.get("pages_total") or 0)
            try:
                if not n_pages:
                    with pymupdf.open(str(in_pdf)) as _d:
                        n_pages = _d.page_count
            except Exception:
                n_pages = 0
            range_ = job.get("range")
            # فصل‌به‌فصل: فهرست فصل‌ها + تعداد فصل‌های کامل‌شده از فایل‌های موجود
            chapters = None
            start_k = 0
            if resume_rec.get("chapters"):
                try:
                    chapters = json.loads(resume_rec["chapters"])
                except Exception:  # noqa: BLE001
                    chapters = None
            if chapters:
                start_k = sum(1 for i in range(len(chapters))
                              if (d / f"ch_{i:03d}.pdf").exists())
                done_old = chapters[start_k - 1]["e"] if start_k else 0
            upd(f"▶️ ادامهٔ ترجمهٔ «{html_mod.escape(name)}» "
                + (f"از فصل {start_k + 1}/{len(chapters)}…" if chapters
                   else f"از صفحهٔ {done_old + 1:,}…"),
                force=True, kb=job_progress_kb(jobkey))
        else:
            partial_old, done_old = None, 0
            chapters, start_k = None, 0
            # ── دانلود: فایل‌های ≤۱۹MB از Bot API؛ بزرگ‌تر از طریق MTProto (تا ~۲GB)
            try:
                if fsize > MAX_DOWNLOAD:
                    if not mtproto_ready():
                        raise RuntimeError("پشتیبانی MTProto برای فایل‌های بزرگ هنوز آماده نیست.")
                    upd(f"⬇️ فایل بزرگ ({fsize / (1024 * 1024):.0f} مگابایت) — دانلود با MTProto…",
                        force=True)

                    def dl_report(mb: float, done: bool = False) -> None:
                        with state_lock:
                            rec = state["jobs"].get(jobkey)
                            if rec is not None:
                                rec["dl_mb"] = round(float(mb), 1)
                        dirty.set()
                        upd(f"⬇️ دانلود MTProto: {mb:.0f} مگابایت…")

                    MT._progress = dl_report
                    try:
                        ok_dl = False
                        for att in range(2):
                            if MT.download(chat, int(mid or 0), in_pdf):
                                ok_dl = True
                                break
                            if att == 0:
                                upd("🔁 دانلود ناتمام ماند؛ ۲۰ ثانیه دیگر دوباره امتحان می‌کنم…",
                                    force=True)
                                time.sleep(20.0)
                    finally:
                        MT._progress = None
                    if not ok_dl:
                        upd("❌ دانلود فایل بزرگ از تلگرام ناموفق ماند.\n"
                            "💡 اگر همین حالا خودِ فایل را یک‌بار دیگر به ربات بفرستی، "
                            "از مسیر زنده (سریع و مطمئن) دانلود می‌شود؛ رکورد قبلی خودکار کنار می‌رود.",
                            force=True)
                        raise RuntimeError("دانلود MTProto ناموفق بود.")
                else:
                    f = api("getFile", file_id=job["file_id"], timeout=30)
                    fp = f.get("file_path", "")
                    r = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}",
                                     timeout=180)
                    r.raise_for_status()
                    in_pdf.write_bytes(r.content)
            except Exception as e:  # noqa: BLE001
                upd(f"❌ خطا در دانلود فایل: <code>{html_mod.escape(str(e))[:140]}</code>", force=True)
                in_pdf.unlink(missing_ok=True)
                return
            try:
                with pymupdf.open(str(in_pdf)) as _d:
                    n_pages = _d.page_count
            except Exception:  # noqa: BLE001
                upd("❌ این فایل قابل خواندن نیست (شاید رمزگذاری شده باشد).", force=True)
                in_pdf.unlink(missing_ok=True)
                return
            range_ = job.get("range")

        with state_lock:
            page_limit = int(state["config"].get("page_limit", 40))

        # ── پرسش «کل فایل یا بازه؟» و «متن‌محور یا عکس‌محور؟» — فقط برای کار تازه
        mode = (job.get("mode") or "auto").strip() or "auto"
        if not resume_rec and range_ is None and not job.get("asked") and not draining.is_set():
            need_range = n_pages >= RANGE_ASK_MIN
            save_job_record(jobkey, status="ask", chat=chat, uid=uid, name=name,
                            file_id=job.get("file_id"), mid=mid, size=fsize,
                            pages_total=n_pages, mode=mode)
            if need_range:
                send(chat, f"📚 فایل «{html_mod.escape(name)}» — <b>{n_pages:,}</b> صفحه.\n"
                           "چطور ترجمه کنم؟\n\n"
                           "🖼 <b>حالت فایل:</b> متن‌محور (متن قابل انتخاب) یا عکس‌محور "
                           "(اسکن/اسکرین‌شات/کامیک)؟ مطمئن نیستی؟ «خودکار» را بزن.",
                     reply_markup=ask_kb(jobkey, True))
            else:
                send(chat, f"🖼 فایل «{html_mod.escape(name)}» <b>متن‌محور</b> است یا "
                           "<b>عکس‌محور</b> (اسکن/اسکرین‌شات کتاب/کامیک)؟\n"
                           "در حالت عکس‌محور، متن با OCR از تصویر برداشته می‌شود. "
                           "مطمئن نیستی؟ «خودکار» را بزن.",
                     reply_markup=ask_kb(jobkey, False))
            ev = threading.Event()
            range_replies[uid] = ev
            range_needed[uid] = need_range
            mode_values.pop(uid, None)
            deadline = time.time() + (RANGE_WAIT_SEC if need_range else MODE_WAIT_SEC)
            while not ev.is_set() and time.time() < deadline:
                if job_pause.is_set() or stop_event.is_set() or draining.is_set():
                    break
                ev.wait(2.0)
            val = (range_values.pop(uid, "") or "").strip()
            mode = (mode_values.pop(uid, "") or "auto").strip() or "auto"
            range_replies.pop(uid, None)
            range_needed.pop(uid, None)
            save_job_record(jobkey, asked=True, mode=mode)
            if job_pause.is_set() or draining.is_set() or stop_event.is_set():
                save_job_record(jobkey, status="queued", chat=chat, uid=uid,
                                file_id=job.get("file_id"), mid=mid, size=fsize,
                                name=name, uname=job.get("uname"), asked=False,
                                mode=mode)
                in_pdf.unlink(missing_ok=True)
                upd("⏳ چرخهٔ سرویس در حال تعویض است؛ فایل برای چرخهٔ بعدی نگه داشته شد و "
                    "کارش خودکار ادامه پیدا می‌کند — نیازی به فرستادن دوباره نیست.", force=True)
                return
            if val.translate(_FA_DIGITS).lower() in ("لغو", "cancel", "off", "-"):
                upd("لغو شد.", force=True)
                in_pdf.unlink(missing_ok=True)
                return
            if val and val != "all":
                pr = parse_range(val, n_pages)
                if pr:
                    range_ = pr
                else:
                    upd("بازهٔ ارسالی معتبر نبود؛ کل فایل ترجمه می‌شود.", force=True)
        elif not resume_rec and range_ is None and not job.get("asked") and draining.is_set():
            # چرخه در حال بسته‌شدن است؛ کار برای چرخهٔ بعد ذخیره می‌شود
            save_job_record(jobkey, status="queued", chat=chat, uid=uid,
                            file_id=job.get("file_id"), mid=mid, size=fsize,
                            name=name, uname=job.get("uname"), asked=False,
                            mode=mode)
            in_pdf.unlink(missing_ok=True)
            upd("⏳ چرخه در حال بسته‌شدن است؛ فایل برای چرخهٔ بعدی صف شد (خودکار ادامه می‌گیرد).",
                force=True)
            return

        # ── اعمال بازه و سقف صفحه
        if range_:
            rs, re_ = int(range_[0]), int(range_[1])
            rs, re_ = max(1, min(rs, n_pages)), max(rs, min(re_, n_pages))
            range_n = re_ - rs + 1
            if range_n > page_limit:
                upd(f"⚠️ بازهٔ انتخابی <b>{range_n:,}</b> صفحه است؛ سقف فعلی ربات "
                    f"<b>{page_limit:,}</b> صفحه. بازهٔ کوچک‌تری انتخاب کن یا از ادمین سقف را بخواه.",
                    force=True)
                if not resume_rec:
                    in_pdf.unlink(missing_ok=True)
                return
        else:
            rs, re_, range_n = 1, n_pages, n_pages
            if n_pages > page_limit:
                upd(f"⚠️ فایل <b>{n_pages:,}</b> صفحه دارد؛ سقف فعلی ربات <b>{page_limit:,}</b> صفحه است.\n"
                    "دکمهٔ «✂️ از صفحه … تا صفحه …» را بزن و بازهٔ کوچک‌تری بده، یا از ادمین سقف را بخواه.",
                    force=True)
                in_pdf.unlink(missing_ok=True)
                return

        # ── گیت زمان چرخه: اگر زمان کم است کار به چرخهٔ بعد می‌رود (خودکار ادامه می‌گیرد)؛
        #    وگرنه شروع می‌شود حتی اگر کامل نشود — چون چک‌پوینت می‌گیریم
        if RUN_MAX_MINUTES:
            needed_min = 5 + (re_ - done_old) * SEC_PER_PAGE_EST / 60
            remaining_min = ((start_ts + JOB_HARD_MINUTES * 60) - time.time()) / 60.0
            if remaining_min < MIN_CYCLE_LEFT_MIN and not resume_rec:
                wait_min = max(int(RUN_MAX_MINUTES - (time.time() - start_ts) / 60) + 5, 5)
                save_job_record(jobkey, status="queued", chat=chat, uid=uid,
                                file_id=job.get("file_id"), mid=mid, size=fsize,
                                name=name, uname=job.get("uname"),
                                range_s=rs if range_ else None,
                                range_e=re_ if range_ else None, asked=True,
                                pages_total=n_pages)
                upd(f"⏳ تا پایان این چرخه فقط <b>{max(remaining_min, 0):.0f} دقیقه</b> مانده و "
                    f"این کار حدوداً <b>{needed_min:.0f} دقیقه</b> وقت می‌برد.\n"
                    f"⏱ حدود <b>{wait_min} دقیقه</b> دیگر چرخهٔ تازه شروع می‌شود و همین کار "
                    "خودکار و از همان‌جا ادامه پیدا می‌کند — نیازی به فرستادن دوباره نیست.",
                    force=True)
                in_pdf.unlink(missing_ok=True)
                return
            if needed_min > remaining_min:
                upd(f"ℹ️ این کار حدوداً <b>{needed_min:.0f} دقیقه</b> وقت می‌برد ولی تا پایان چرخه فقط "
                    f"<b>{remaining_min:.0f} دقیقه</b> هست؛ تا جایی که بشود ترجمه می‌کنم، بعد "
                    "چک‌پوینت می‌گیرم و چرخهٔ بعدی خودش ادامه می‌دهد.", force=True)

        with state_lock:
            speed = state["config"].get("speed", "balanced")
            src_lang = state["config"].get("src_lang", "auto")
            dst_lang = state["config"].get("dst_lang", "fa")
            imgq = state["config"].get("imgq", "small")
        w, sp = SPEEDS_OC.get(speed, SPEEDS_OC["balanced"])
        eng = engine()
        t0 = time.time()
        mode_note = " | 🖼 عکس‌محور (OCR)" if mode == "image" else ""

        # ── فصل‌به‌فصل برای فایل‌های بزرگ (کتاب) — فقط کار تازه؛ ادامه از رکورد می‌آید ──
        if chapters is None and not resume_rec and (re_ - rs + 1) >= CHAPTER_MIN_PAGES:
            try:
                chapters = book_chapters(in_pdf, rs, re_)
            except Exception:  # noqa: BLE001
                chapters = None
            if not chapters or len(chapters) < 2:
                chapters = None

        def progress(rel_done: int, rel_total: int) -> None:
            done_abs = done_old + rel_done
            aj["last_done"] = done_abs
            pct = rel_done * 100 // max(rel_total, 1)
            bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
            rng_note = f" (بازهٔ {rs:,}–{re_:,})" if range_ else ""
            upd(f"🔄 ترجمه: {bar} {done_abs:,}/{re_:,} صفحه ({pct}٪){rng_note}{mode_note}",
                kb=job_progress_kb(jobkey))

        def extract_progress(rel_done: int, rel_total: int) -> None:
            if rel_done % 20 == 0 or rel_done == rel_total:
                upd(f"📖 آماده‌سازی: متنِ {rel_done:,}/{rel_total:,} صفحه استخراج شد…",
                    kb=job_progress_kb(jobkey))

        if chapters:
            ch_first = chapters[min(start_k, len(chapters) - 1)]
            upd(f"📚 این کتاب <b>{len(chapters)}</b> فصل دارد — <b>فصل‌به‌فصل</b> ترجمه می‌کنم و "
                "بعد از هر فصل گزارش می‌دهم. هایلایت‌های متن اصلی هم حذف و طبق ترجمه دوباره گذاشته "
                "می‌شوند و هیچ صفحه‌ای خالی نمی‌ماند.\n"
                f"▶️ شروع از فصل {min(start_k, len(chapters) - 1) + 1}: "
                f"«{html_mod.escape(ch_first['t'])}» (صفحات {ch_first['s']:,}–{ch_first['e']:,}) "
                f"با <code>{eng['model']}</code>{mode_note}",
                force=True, kb=job_progress_kb(jobkey))
        elif resume_rec:
            upd(f"🌍 ادامهٔ ترجمه از صفحهٔ {done_old + 1:,} تا {re_:,} با <code>{eng['model']}</code>…",
                force=True, kb=job_progress_kb(jobkey))
        else:
            upd(f"🌍 شروع ترجمهٔ <b>{range_n:,}</b> صفحه با <code>{eng['model']}</code> "
                f"({LANG_LABEL.get(src_lang, src_lang)} → {LANG_LABEL.get(dst_lang, dst_lang)})"
                f"{mode_note}…",
                force=True, kb=job_progress_kb(jobkey))
        if not chapters:
            aj["part_pdf"] = str(out_pdf.with_suffix(".part.pdf"))  # برای دکمهٔ «دریافت تا اینجا»

        ch_agg = None
        # سرویس ترجمه فقط برای همین ترجمه «روشن» و بعد از کار «خاموش» می‌شود (ضد بن)
        try:
            opencode_arm()
        except Exception as e:  # noqa: BLE001
            upd(f"❌ سرویس ترجمه آماده نیست: <code>{html_mod.escape(str(e))[:140]}</code>", force=True)
            in_pdf.unlink(missing_ok=True)
            return
        try:
            if chapters:
                ctx = {"jobkey": jobkey, "chat": chat, "uid": uid, "aj": aj, "upd": upd,
                       "in_pdf": in_pdf, "rs": rs, "re_": re_, "chapters": chapters,
                       "start_k": start_k, "ref_in": (resume_rec or {}).get("ref_in") or "",
                       "ch_refs": list((resume_rec or {}).get("ch_refs") or []),
                       "w": w, "sp": sp, "eng": eng, "mode": mode,
                       "src_lang": src_lang, "dst_lang": dst_lang, "imgq": imgq,
                       "name": name, "n_pages": n_pages}
                cres = _translate_by_chapters(ctx)
                ch_agg = cres.get("agg") or {}
                if cres["status"] == "paused":
                    return
                if cres["status"] == "failed":
                    err = cres["err"]
                    if cres.get("kind") == "fatal":
                        alert_admin_provider_down("OpenCode Zen", err)
                    upd("❌ ترجمه در فصل «...» ناتمام ماند: " + short_err(err) + "\n"
                        "✅ فصل‌های کامل‌شده ذخیره شده‌اند؛ دوباره امتحان کن تا از همان فصل ادامه دهم.",
                        force=True)
                    return
                res = cres["res"]
                rel_translated = int((res or {}).get("translated", 0) or 0)
                done_abs = re_
            else:
                try:
                    res = core.translate_pdf(
                        str(in_pdf), str(out_pdf), "", model=eng["model"],
                        base_url="", workers=w, spacing=sp,
                        progress=progress, cancelled=job_pause,
                        extract_progress=extract_progress,
                        # ادامه: done_old اندیس مطلقِ اولین صفحهٔ ترجمه‌نشده است؛
                        # کار تازه: شروع از ابتدای بازه (rs-1)
                        start_page=done_old if resume_rec else (rs - 1), end_page=re_,
                        log=lambda *a: None, mirror=True,
                        src_lang=src_lang, dst_lang=dst_lang, use_cli=True,
                        oc_bin=OC_BIN, oc_cwd=str(TMP_DIR),
                        page_mode=mode, ocr_langs=core.tess_lang_for(src_lang),
                        img_quality=imgq)
                except core.FatalTranslationError as err:
                    alert_admin_provider_down("OpenCode Zen", err)
                    upd("❌ ترجمه انجام نشد: " + short_err(err) + "\nبه ادمین خبر بده.", force=True)
                    if not resume_rec:
                        in_pdf.unlink(missing_ok=True)
                    return
                except core.TranslationError as err:
                    upd(f"❌ ترجمه ناتمام ماند: {short_err(err)}\n"
                        "سرویس ترجمه شلوغ است؛ چند دقیقه بعد دوباره امتحان کن.", force=True)
                    if not resume_rec:
                        in_pdf.unlink(missing_ok=True)
                    return
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()  # استک کامل در لاگ Actions برای دیباگ
                    upd(f"❌ خطای غیرمنتظره در ترجمه: <code>{html_mod.escape(str(e))[:140]}</code>",
                        force=True)
                    if not resume_rec:
                        in_pdf.unlink(missing_ok=True)
                    return
                rel_translated = int((res or {}).get("translated", 0) or 0)
                done_abs = done_old + rel_translated
        finally:
            opencode_disarm()

        usage = (res or {}).get("usage", {})
        total_tokens = int(usage.get("total_tokens", 0) or 0)
        secs = time.time() - t0
        if ch_agg is not None:
            failed_n = int(ch_agg.get("failed", 0) or 0)
            skipped_n = int(ch_agg.get("skipped", 0) or 0)
        else:
            failed_n = len((res or {}).get("failed_pages", []))
            skipped_n = len((res or {}).get("skipped_pages", []))
        rel_translated = int((res or {}).get("translated", 0) or 0)
        if not chapters:
            done_abs = done_old + rel_translated

        # ── توقف موقت / پایان چرخه: چک‌پوینت بگیر و خبر بده
        if (res or {}).get("cancelled"):
            # ادغام پیشرفت قبلی با بخش تازه (برای ادامهٔ کار) — وگرنه پیشرفت قبلی از بین می‌رود
            new_part = Path((res or {}).get("output") or "")
            merged = new_part
            if new_part.exists() and (done_old > 0 or (resume_rec and partial_old)):
                merged = merge_partials(partial_old, new_part, done_old,
                                        TMP_DIR / f"{jobkey}_cp_partial.pdf")
                if merged != new_part:
                    new_part.unlink(missing_ok=True)
            ref = save_checkpoint(jobkey,
                                  {"name": name, "pages_total": n_pages, "size": fsize,
                                   "uid": uid, "chat": chat, "range_s": rs if range_ else None,
                                   "range_e": re_ if range_ else None,
                                   "mode": mode,
                                   "ref_in": (resume_rec or {}).get("ref_in") or ""},
                                  in_pdf, merged, done_abs)
            out_mb = 0.0
            pp = cp_dir(jobkey) / "partial.pdf"
            if pp.exists():
                out_mb = pp.stat().st_size / (1024 * 1024)
            reason = "توقف موقت" if not draining.is_set() else "پایان چرخهٔ سرویس"
            msg = (f"⏸ <b>{reason}</b> — ترجمه تا صفحهٔ <b>{done_abs:,}</b> ذخیره شد "
                   f"(از {re_:,}).\n"
                   "▶️ با دکمهٔ ادامه، از همین‌جا ادامه می‌گیرد؛ چرخهٔ بعدی هم خودش ادامه می‌دهد.")
            if not ref and not pp.exists():
                msg += "\n⚠️ ذخیرهٔ چک‌پوینت ناموفق بود؛ فایل را دوباره بفرست."
            upd(msg, force=True, kb=paused_kb(jobkey, done_abs))
            log(f"چک‌پوینت {jobkey}: {done_abs} صفحه ({out_mb:.1f}MB) ref={ref or 'محلی'}")
            if ADMIN_ID and not ref:
                try:
                    send(ADMIN_ID, f"⚠️ آپلود چک‌پوینت «{html_mod.escape(name)}» ناموفق بود "
                                   f"(done={done_abs}) — ادامه فقط در همین رانر ممکن است.")
                except TgError:
                    pass
            return

        # ── موفقیت: ادغام پیشرفت قبلی (اگر ادامه بود)، تحویل، پاکسازی
        upd("📤 در حال آماده‌سازی و ارسال فایل ترجمه‌شده…", force=True)
        if chapters:
            final_path = Path((res or {}).get("output") or "")
            if not final_path.exists():
                upd("❌ فایل نهایی مونتاژشده پیدا نشد؛ دوباره امتحان کن.", force=True)
                return
        else:
            final_path = out_pdf
            if resume_rec and done_old > 0 and partial_old and partial_old.exists():
                final_path = TMP_DIR / f"{uid}_{stamp}_final.pdf"
                merge_partials(partial_old, out_pdf, done_old, final_path)
                out_pdf.unlink(missing_ok=True)

        out_mb = final_path.stat().st_size / (1024 * 1024)
        if chapters:
            cap = (f"✅ ترجمهٔ کامل «{html_mod.escape(name)}» — {re_ - rs + 1:,} صفحه در "
                   f"{secs / 60:.1f} دقیقه ({len(chapters)} فصل) | حجم: {out_mb:.1f}MB")
        elif range_ and done_abs < re_:
            cap = (f"✅ ترجمهٔ «{html_mod.escape(name)}» — صفحات {_fmt_pages(rs, re_)} در "
                   f"{secs / 60:.1f} دقیقه | حجم: {out_mb:.1f}MB")
        else:
            cap = (f"✅ ترجمهٔ «{html_mod.escape(name)}» — {re_ - done_old:,} صفحه در "
                   f"{secs / 60:.1f} دقیقه | حجم: {out_mb:.1f}MB")
        warn = ""
        if failed_n:
            warn += f"\n⚠️ {failed_n} بخش به‌خاطر خطای سرویس ترجمه نشد (متن اصلی همان بخش ماند)."
        if skipped_n:
            warn += f"\n⚠️ {skipped_n} صفحه متن قابل استخراج نداشت (بدون تغییر کپی شد)."
        ocr_n = int((res or {}).get("ocr_pages", 0) or 0)
        if ocr_n:
            warn += f"\n🖼 {ocr_n} صفحه با OCR ترجمه شد (متن از تصویر برداشته شد)."
        hl_n = int((res or {}).get("hl_applied", 0) or 0)
        if hl_n:
            warn += f"\n🖍 {hl_n:,} هایلایت حذف و طبق ترجمه دوباره گذاشته شد."
        cap += warn

        upd("📤 در حال ارسال فایل ترجمه‌شده…", force=True)
        if not deliver_file(chat, final_path, cap):
            upd(f"❌ ارسال فایل ({out_mb:.1f} مگابایت) ناموفق بود؛ دوباره امتحان کن.", force=True)
            if not resume_rec:
                in_pdf.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            return

        # کپی خروجی برای ادمین به آدرس درخواستی (فقط کارهای خودِ ادمین)
        if chapters and uid == ADMIN_ID:
            cid = extra_delivery_chat()
            if cid and cid != chat:
                try:
                    if deliver_file(cid, final_path,
                                    f"📘 کپی ترجمهٔ «{html_mod.escape(name)}» ({out_mb:.1f}MB)"):
                        try:
                            send(chat, f"📨 یک نسخه هم به {DELIVERY_EXTRA} ارسال شد.")
                        except TgError:
                            pass
                except Exception as e:  # noqa: BLE001
                    log(f"ارسال کپی به {DELIVERY_EXTRA} ناموفق بود: {e}")

        # پاکسازی چک‌پوینت و رکورد
        old_refs = [(resume_rec or {}).get("ref_in") or "",
                    (resume_rec or {}).get("ref_pp") or ""]
        if chapters:
            old_refs += list((resume_rec or {}).get("ch_refs") or [])
            old_refs += list(cres.get("ch_refs") or [])
        for old_ref in old_refs:
            if old_ref:
                gh_store.cleanup_release(GH_REPO, GH_TOKEN, old_ref)
        shutil.rmtree(cp_dir(jobkey), ignore_errors=True)
        drop_job_record(jobkey)

        pages_counted = rel_translated
        with state_lock:
            rec_u = state["users"].setdefault(str(uid), {})
            rec_u["pages"] = rec_u.get("pages", 0) + pages_counted
            rec_u["files"] = rec_u.get("files", 0) + 1
            rec_u["tokens"] = rec_u.get("tokens", 0) + total_tokens
            state["totals"]["files"] = state["totals"].get("files", 0) + 1
            state["totals"]["pages"] = state["totals"].get("pages", 0) + pages_counted
            state["totals"]["tokens"] = state["totals"].get("tokens", 0) + total_tokens
        dirty.set()
        log(f"فایل «{name}» ({pages_counted} صفحه در این نوبت) برای {uid} در {secs:.0f}s "
            f"ترجمه شد ({total_tokens:,} توکن).")

        if in_pdf.exists() and not resume_rec:
            in_pdf.unlink(missing_ok=True)
        if chapters:
            # فایل‌های فصل و مونتاژ دیگر لازم نیستند
            try:
                Path(str((res or {}).get("output") or "")).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        if not ADMIN_ID:
            return
        try:
            uname = job.get("uname") or ""
            send(ADMIN_ID,
                 "📊 <b>گزارش مصرف توکن</b>\n"
                 f"📄 فایل: <code>{html_mod.escape(name)}</code>\n"
                 f"👤 کاربر: <code>{uid}</code>" + (f" (@{uname})" if uname else "") + "\n"
                 f"📑 صفحات این نوبت: {pages_counted:,} | ⏱ {secs / 60:.1f} دقیقه\n"
                 f"🔤 ورودی: {usage.get('prompt_tokens', 0):,} | خروجی: {usage.get('completion_tokens', 0):,}\n"
                 f"🧮 مجموع: <b>{total_tokens:,}</b> توکن در {usage.get('api_calls', 0)} درخواست\n"
                 f"🤖 {eng['label']} / <code>{eng['model']}</code>")
        except TgError:
            pass
    finally:
        active_jobs.pop(jobkey, None)


def worker() -> None:
    while not stop_event.is_set():
        try:
            job = job_q.get(timeout=1)
        except queue.Empty:
            if draining.is_set():
                break   # چرخه بسته شد و کار دیگری نیست
            continue
        if draining.is_set():
            # چرخه در حال بسته‌شدن است؛ این کار برای چرخهٔ بعد ذخیره می‌شود
            if job.get("resume"):
                save_job_record(job["resume"], status="paused")
            else:
                key = job.get("jobkey") or f"{job.get('uid')}_{int(time.time())}"
                save_job_record(key, status="queued", chat=job.get("chat"),
                                uid=job.get("uid"), file_id=job.get("file_id"),
                                mid=job.get("mid"), size=job.get("size"),
                                name=job.get("name"), uname=job.get("uname"),
                                range_s=(job.get("range") or (None, None))[0],
                                range_e=(job.get("range") or (None, None))[1],
                                asked=job.get("asked"), mode=job.get("mode"))
            job_q.task_done()
            continue
        job_pause.clear()   # توقفِ کار قبلی نباید به این کار سرایت کند
        if job.get("jobkey") and job.get("jobkey") in superseded:
            # این کار با آپلود تازهٔ همان فایل جایگزین شده — نادیده گرفته می‌شود
            superseded.discard(job.get("jobkey"))
            job_q.task_done()
            continue
        worker_busy.set()
        try:
            process_job(job)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            try:
                send(job.get("chat"), "❌ خطای غیرمنتظره در پردازش؛ دوباره امتحان کن.")
            except TgError:
                pass
        finally:
            user_pending.discard(job.get("uid"))
            worker_busy.clear()
            job_q.task_done()
    snapshot_queues_to_state()


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
    # ── پاسخ کاربر به سوال «بازهٔ صفحه» (متن آزاد؛ دستورها رد نمی‌شوند)
    _rev = range_replies.get(uid)
    if _rev is not None and not _rev.is_set() and text and not text.startswith("/"):
        range_values[uid] = text
        _rev.set()
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
    drain_deadline = 0.0
    while not stop_event.is_set():
        # حالت Actions: پس از پایان چرخه، فاز تخلیه شروع می‌شود؛ کارهای کوچک فرصت
        # تمام‌کردن طبیعی دارند (مهلت DRAIN_GRACE) و کارهای بزرگ توقف موقت +
        # چک‌پوینت می‌شوند تا در چرخهٔ بعد از همان‌جا ادامه بگیرند.
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
                drain_deadline = time.time() + DRAIN_GRACE_MINUTES * 60
                log("زمان چرخه تمام شد؛ فاز تخلیه شروع شد (فایل جدید پذیرفته نمی‌شود)…")
            elif worker_busy.is_set() and not job_pause.is_set() and time.time() > drain_deadline:
                job_pause.set()
                log("مهلت تخلیه تمام شد؛ درخواست توقف موقتِ کار در جریان (چک‌پوینت)…")
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

    # چک‌پوینت‌ها و کارهای معلق چرخهٔ قبل — قبل از روشن‌شدن ورکر صف می‌شوند
    boot_resume()

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
