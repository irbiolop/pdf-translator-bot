#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
هستهٔ برنامهٔ ترجمهٔ PDF انگلیسی → فارسی با حفظ چیدمان
--------------------------------------------------------
مراحل کار:
  ۱) متن هر صفحه با PyMuPDF استخراج می‌شود (بلوک‌ها + موقعیت + اندازه + بولد/ایتالیک + رنگ)
  ۲) متن هر صفحه (بلوک‌به‌بلوک) به سرویس ترجمه سازگار OpenAI (Groq یا Top Tools AI)
     داده می‌شود و ترجمهٔ وفادار گرفته می‌شود — حالت موازی با چند ورکر هم پشتیبانی می‌شود.
  ۳) متن اصلیِ صفحه حذف (redact) می‌شود و ترجمهٔ فارسی دقیقاً در همان کادرها
     با همان اندازه/بولد/رنگ و چینش راست‌به‌چپ درج می‌شود.

این ماژول هیچ رابط گرافیکی ندارد و مستقل تست‌شدنی است:
    python translator_core.py input.pdf -o output.pdf
"""

from __future__ import annotations

import html as html_mod
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import requests

# --------------------------------------------------------------------------
# تنظیمات سرویس و مدل
# --------------------------------------------------------------------------

# ─── سرویس‌های سازگار با OpenAI (drop-in) ───
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
TTAI_API_URL = "https://top-tools-ai.com/api/v1/chat/completions"

# مدل‌های سرویس Top Tools AI (سازگار با OpenAI):
#   • Top-Tools-Ai: ۱۰ میلیون توکن در روز، رایگان روزانه (بدون اشتراک)
#   • بقیه: ۱۰ میلیون توکن خوش‌آمدگویی حساب جدید یا اشتراک پولی
#   سقف‌ها: ~۴۵ درخواست/دقیقه و حداکثر ۵ درخواست همزمان (۶مین → 429)
#   → ترجمهٔ موازی با حداکثر ۴ ورکر + فاصله‌گذار شروع درخواست‌ها امن است.
TTAI_MODELS = [
    "Top-Tools-Ai",          # سهمیهٔ روزانهٔ رایگان ۱۰M توکن — پیش‌فرض
    "GLM-5.3-Flash",
    "DeepSeek-V4-Flash",
    "DeepSeek-V4.1-Flash",
    "MiMo-V2.5",
    "MiniMax-M3",
    "GLM-5.3",
    "Kimi-K2.6",
]

# مدل‌های رایگانِ مناسب Groq (فهرست قابل تغییر است؛ در رابط گرافیکی قابل ویرایش است)
#
# ⚠ محدودیت‌های پلن رایگان Groq (اکتبر ۲۰۲۶ — مدل‌به‌مدل، در سطح سازمان سنجیده می‌شود):
#    حدوداً برای هر مدل چت:  ۳۰ درخواست در دقیقه | ۱٬۰۰۰ درخواست در روز
#                            ۸٬۰۰۰ توکن در دقیقه | ۲۰۰٬۰۰۰ توکن در روز
#    (دقیق سقف هر مدل را در console.groq.com/docs/rate-limits با حساب خودتان ببینید)
#    توجه: مدل‌های Llama از فهرست رایگان حذف شده‌اند (فقط Enterprise)؛
#    اگر روی کلید شما هنوز فعال است می‌توانید دستی بنویسید.
FREE_MODELS = [
    "openai/gpt-oss-120b",       # پیش‌فرض: بهترین کیفیت ترجمه در تیر فری فعلی
    "openai/gpt-oss-20b",        # سریع‌تر و سبک‌تر
    "qwen/qwen3.8-27b",          # گزینهٔ چندزبانه
    "qwen/qwen3.6-27b",
    "llama-3.3-70b-versatile",   # قدیمی: فقط اگر روی کلید شما هنوز فعال باشد
    "llama-3.1-8b-instant",      # قدیمی: فقط اگر روی کلید شما هنوز فعال باشد
]
DEFAULT_MODEL = FREE_MODELS[0]

# مکث پیش‌فرض بین صفحات در حالت پشت‌سرهم (Groq با سقف ~۸هزار توکن در دقیقه).
# در حالت موازی (Top Tools AI) مکث معنی‌دار نیست؛ فاصله‌گذار نرخ (spacing) اعمال می‌شود.
DEFAULT_DELAY = 15.0

# فاصلهٔ پیش‌فرض شروع درخواست‌ها در حالت موازی (ثانیه) ≈ ۴۳ درخواست در دقیقه
# زیر سقف ۴۵RPM سرویس‌ها؛ ۴29ها هم خودکار طبق retry-after مدیریت می‌شوند.
DEFAULT_SPACING = 1.4

# کمترین مقیاس مجاز برای کوچک‌شدن متن یک بلوک (زیر این حد، اندازه دیگر خوانا نیست)
MIN_BLOCK_SCALE = 0.55

APP_DIR = Path(__file__).resolve().parent
FONTS_DIR = APP_DIR / "fonts"
FONT_REG = FONTS_DIR / "Vazirmatn-Regular.ttf"
FONT_BOLD = FONTS_DIR / "Vazirmatn-Bold.ttf"

# منابع دانلود فونت وزیرمتن (در صورت نبود، به‌صورت خودکار دانلود می‌شود)
FONT_URLS = {
    FONT_REG.name: [
        "https://raw.githubusercontent.com/rastikerdar/vazirmatn/master/fonts/ttf/Vazirmatn-Regular.ttf",
        "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/ttf/Vazirmatn-Regular.ttf",
    ],
    FONT_BOLD.name: [
        "https://raw.githubusercontent.com/rastikerdar/vazirmatn/master/fonts/ttf/Vazirmatn-Bold.ttf",
        "https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/ttf/Vazirmatn-Bold.ttf",
    ],
}

# دستور سخت‌گیرانه برای مدل: ترجمهٔ کاملاً وفادار، بدون هیچ افزودن/کاستن/توضیح
SYSTEM_PROMPT = """You are a meticulous, strictly literal English-to-Persian (Farsi) translator for books and documents.

You will receive a JSON object of the form {"segments": ["...", ...]}.
Return ONLY a JSON object of the form {"translations": ["...", ...]} with EXACTLY the same number of items, where item i is the faithful Persian translation of segment i.

Strict rules:
1. Absolute fidelity: translate every sentence completely. Never add, omit, summarize, paraphrase, explain, or answer the content. Bring the exact sentences as they are, only in Persian.
2. Keep the sentence order and any internal line breaks; do not merge or split items.
3. Keep numbers, units, formulas, code, URLs, emails, and Latin proper nouns unchanged. Well-known people/places may use their common Persian spelling.
4. Use natural, correct Persian with proper punctuation and half-space (ZWNJ) where appropriate.
5. Headings, list items, captions and page numbers must be translated in the same concise style.
6. Output only the JSON object. No explanations, no markdown, no notes."""

# --------------------------------------------------------------------------
# خطاها
# --------------------------------------------------------------------------


class TranslationError(Exception):
    """خطای قابل تلاشِ مجدد در ترجمه."""


class FatalTranslationError(TranslationError):
    """خطای غیرقابل جبران (مثل کلید نامعتبر) — تلاش مجدد بی‌فایده است."""


# --------------------------------------------------------------------------
# فونت فارسی
# --------------------------------------------------------------------------


def ensure_fonts(log=print) -> None:
    """در صورت نبود فونت وزیرمتن، آن را دانلود می‌کند."""
    try:
        FONTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"هشدار: ساخت پوشهٔ فونت ممکن نشد: {e}")
        return
    for fname, urls in FONT_URLS.items():
        dest = FONTS_DIR / fname
        if dest.exists() and dest.stat().st_size > 10_000:
            continue
        for url in urls:
            try:
                log(f"در حال دانلود فونت: {fname} ...")
                r = requests.get(url, timeout=90)
                r.raise_for_status()
                if len(r.content) < 10_000:
                    continue  # پاسخ معتبر نبود
                dest.write_bytes(r.content)
                log(f"فونت دانلود شد: {dest}")
                break
            except Exception as e:  # noqa: BLE001
                log(f"دانلود ناموفق ({url}): {e}")
        else:
            log("هشدار: فونت وزیرمتن دانلود نشد؛ از فونت پیش‌فرض استفاده می‌شود "
                "(می‌توانید فایل‌های TTF را دستی در پوشهٔ fonts بگذارید).")


# --------------------------------------------------------------------------
# استخراج بلوک‌های متنی با استایل
# --------------------------------------------------------------------------

# خطوطی که «آیتم لیست» به نظر می‌رسند باید خط جدا بمانند
_LIST_RE = re.compile(r"^([•·▪‣◦–—-]|\d{1,3}[.)]|[A-Za-z][.)])\s+")
# پایان خط با خط‌تیره = کلمهٔ شکسته به خط بعد
_HYPHEN_END_RE = re.compile(r"([A-Za-z])[-\u2010\u2011]\s*$")
_LOWER_START_RE = re.compile(r"^[a-z]")


def _join_lines(raw_lines: list[str]) -> str:
    """ادغام خطوط یک بلوک: پاراگراف روان می‌شود؛ آیتم‌های لیست خط جدا می‌مانند."""
    parts: list[str] = []
    for ln in raw_lines:
        t = ln.strip()
        if not t:
            continue
        if parts and _LIST_RE.match(t):
            parts.append(t)  # آیتم جدید لیست
        elif parts and _HYPHEN_END_RE.search(parts[-1]) and _LOWER_START_RE.match(t):
            # کلمهٔ شکسته با خط‌تیره؛ دو نیمه را به هم بچسبان
            parts[-1] = _HYPHEN_END_RE.sub(r"\1", parts[-1]) + t
        elif parts:
            parts[-1] = parts[-1] + " " + t
        else:
            parts.append(t)
    return "\n".join(parts)


def _char_weighted(values: list[tuple[float, int]]) -> float:
    total = sum(n for _, n in values) or 1
    return sum(v * n for v, n in values) / total


def _detect_align(bbox: "pymupdf.Rect", line_rects: list["pymupdf.Rect"]) -> str:
    """تشخیص وسط‌چین بودن (برای تیترها)؛ غیر از آن راست‌چین فارسی."""
    if not line_rects:
        return "right"
    if len(line_rects) == 1:
        r = line_rects[0]
        left_gap = r.x0 - bbox.x0
        right_gap = bbox.x1 - r.x1
        if left_gap > 18 and abs(left_gap - right_gap) <= 8:
            return "center"
        return "right"
    c_block = (bbox.x0 + bbox.x1) / 2
    devs = [abs((r.x0 + r.x1) / 2 - c_block) for r in line_rects]
    return "center" if max(devs) <= 6 else "right"


def extract_blocks(page: "pymupdf.Page") -> list[dict]:
    """بلوک‌های متنی صفحه را به‌همراه هندسه و استایل غالب برمی‌گرداند."""
    data = page.get_text("dict")
    blocks: list[dict] = []
    for b in data.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines = b.get("lines") or []
        if not lines:
            continue
        raw_lines: list[str] = []
        line_rects: list[pymupdf.Rect] = []
        size_w: list[tuple[float, int]] = []
        colors: dict[int, int] = {}
        bold_chars = italic_chars = total_chars = 0
        for ln in lines:
            txt = "".join(sp.get("text", "") for sp in ln.get("spans", []))
            raw_lines.append(txt)
            line_rects.append(pymupdf.Rect(ln["bbox"]))
            for sp in ln.get("spans", []):
                t = sp.get("text", "")
                n = max(len(t.strip()), 1)
                size_w.append((float(sp.get("size", 11.0)), n))
                total_chars += n
                colors[int(sp.get("color", 0))] = colors.get(int(sp.get("color", 0)), 0) + n
                fname = (sp.get("font", "") or "").lower()
                fl = int(sp.get("flags", 0))
                if (fl & 16) or ("bold" in fname):
                    bold_chars += n
                if (fl & 2) or ("italic" in fname) or ("oblique" in fname):
                    italic_chars += n
        text = _join_lines(raw_lines)
        if not text.strip():
            continue
        bbox = pymupdf.Rect(b["bbox"])
        if bbox.is_empty or bbox.is_infinite or bbox.width <= 2 or bbox.height <= 2:
            continue
        color = max(colors.items(), key=lambda kv: kv[1])[0] if colors else 0
        size = round(_char_weighted(size_w) * 2) / 2   # گرد کردن به ۰٫۵
        # فاصلهٔ خطوط را از خود PDF اصلی تخمین می‌زنیم تا چیدمان وفادار بماند
        n_lines = max(len(line_rects), 1)
        if n_lines > 1 and size > 0:
            leading = min(max(bbox.height / n_lines / size, 1.05), 1.8)
        else:
            leading = 1.4
        blocks.append({
            "bbox": bbox,
            "line_rects": line_rects,
            "text": text,
            "size": size,
            "leading": leading,
            "bold": total_chars > 0 and bold_chars / total_chars > 0.5,
            "italic": total_chars > 0 and italic_chars / total_chars > 0.5,
            "color": "#{:06x}".format(color & 0xFFFFFF),
            "align": _detect_align(bbox, line_rects),
        })
    return blocks


# --------------------------------------------------------------------------
# فراخوانی وب‌سرویس ترجمه (سازگار OpenAI)
# --------------------------------------------------------------------------


def _batch_items(items: list[tuple[int, str]], max_chars: int = 6000) -> list[list[tuple[int, str]]]:
    """دسته‌بندی آیتم‌ها بر اساس حجم کاراکتر (برای جلوگیری از برخورد به سقف توکن)."""
    batches, cur, size = [], [], 0
    for it in items:
        n = max(len(it[1]), 1)
        if cur and size + n > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += n
    if cur:
        batches.append(cur)
    return batches


class _RateLimiter:
    """حداقل فاصلهٔ زمانی بین شروع درخواست‌ها؛ بین همهٔ ورکرها مشترک است
    تا سقف RPM سرویس با هر تعداد ترجمهٔ موازی رعایت شود."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(float(min_interval), 0.0)
        self._lock = threading.Lock()
        self._next_t = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.time()
            t = max(now, self._next_t)
            self._next_t = t + self.min_interval
        d = t - now
        if d > 0:
            time.sleep(d)


def groq_translate_segments(api_key: str, model: str, segments: list[str], *,
                            temperature: float = 0.0, timeout: int = 240,
                            max_retries: int = 5, log=print,
                            base_url: str = "",
                            usage_acc: dict | None = None,
                            usage_lock: threading.Lock | None = None) -> list[str]:
    """ترجمهٔ یک دسته متن با هر سرویس سازگار OpenAI (JSON mode)؛
    خروجی هم‌طول segments است. اگر usage_acc داده شود، مصرف توکن هر پاسخ موفق
    (prompt/completion/total و تعداد درخواست) در آن جمع زده می‌شود —
    برای انحصار بین ورکرها usage_lock را هم بدهید."""
    if not segments:
        return []
    url = base_url or GROQ_API_URL
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": json.dumps({"segments": segments}, ensure_ascii=False)},
        ],
    }
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 429:
                wait = resp.headers.get("retry-after")
                wait = float(wait) if wait else min(2 * attempt, 15)
                log(f"محدودیت نرخ سرویس؛ {wait:.0f} ثانیه صبر می‌کنیم…")
                time.sleep(wait)
                continue
            if resp.status_code == 400 and "response_format" in (resp.text or ""):
                # سرویس JSON mode را پشتیبانی نمی‌کند؛ بدون آن ادامه می‌دهیم
                payload.pop("response_format", None)
                continue
            if resp.status_code == 401:
                raise FatalTranslationError("کلید API نامعتبر است (کد 401).")
            if resp.status_code == 403:
                raise FatalTranslationError(
                    "دسترسی مسدود شد (کد 403). اگر مدل نیازمند اشتراک است، آن را در پنل سرویس فعال کنید؛ "
                    "کلید را بررسی کنید و در صورت محدودیت جغرافیایی IP را عوض کنید.")
            if resp.status_code == 404:
                raise FatalTranslationError(
                    f"مدل '{model}' پیدا نشد (کد 404). نام مدل را در تنظیمات اصلاح کنید.")
            if resp.status_code >= 400:
                # خطای شناخته‌نشده (گاهی گیت‌وی سرویس خطاهای گذرای 400/5xx می‌دهد)
                # → با بدنهٔ خطا در پیام، قابل تلاش مجدد در نظر گرفته می‌شود.
                body = (resp.text or "").strip()
                if "model_not_found" in body or "does not exist" in body:
                    raise FatalTranslationError(
                        f"مدل '{model}' روی این سرویس پیدا نشد (کد {resp.status_code}). "
                        "نام مدل را در تنظیمات اصلاح کنید.")
                raise TranslationError(f"کد {resp.status_code}: {body[:180]}")
            data = resp.json()
            u = data.get("usage") or {}
            if u and usage_acc is not None:
                if usage_lock is not None:
                    usage_lock.acquire()
                try:
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        usage_acc[k] = usage_acc.get(k, 0) + int(u.get(k, 0) or 0)
                    usage_acc["api_calls"] = usage_acc.get("api_calls", 0) + 1
                finally:
                    if usage_lock is not None:
                        usage_lock.release()
            content = data["choices"][0]["message"]["content"]
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", content, re.S)
                if not m:
                    raise TranslationError("پاسخ مدل JSON نبود.") from None
                parsed = json.loads(m.group(0))
            trans = parsed.get("translations") if isinstance(parsed, dict) else None
            if not isinstance(trans, list):
                raise TranslationError("پاسخ مدل فهرست 'translations' نداشت.")
            if len(trans) != len(segments):
                log(f"هشدار: طول پاسخ ({len(trans)}) با ورودی ({len(segments)}) فرق داشت؛ اصلاح شد.")
                if len(trans) < len(segments):
                    trans = list(trans) + list(segments[len(trans):])
                else:
                    trans = trans[:len(segments)]
            return [str(t) for t in trans]
        except FatalTranslationError:
            raise
        except (requests.RequestException, TranslationError, KeyError, IndexError) as e:
            last_err = e
            # بازهٔ بلندتر بین تلاش‌ها (۴ تا ۲۰ ثانیه) تا از موج‌های گذرای خطای سرویس رد شویم
            wait = min(4 * attempt, 20)
            log(f"تلاش {attempt}/{max_retries} ناموفق بود ({e})؛ {wait} ثانیه صبر…")
            time.sleep(wait)
    raise TranslationError(f"ترجمهٔ این بخش پس از چند تلاش ناموفق ماند: {last_err}")


def selftest(api_key: str, model: str, log=print, base_url: str = "") -> list[str]:
    """تست سریع اتصال/کلید/مدل با دو جملهٔ کوتاه."""
    segs = ["Hello world.", "The sun rises in the east, and the moon watches quietly."]
    outs = groq_translate_segments(api_key, model, segs, log=log, base_url=base_url)
    for s, o in zip(segs, outs):
        log(f"EN : {s}")
        log(f"FA : {o}")
    log("تست اتصال موفق بود.")
    return outs


# --------------------------------------------------------------------------
# بازسازی PDF
# --------------------------------------------------------------------------


def _build_css():
    """CSS فونت فارسی به‌همراه آرشیو فونت؛ اگر فونت نبود فونت پیش‌فرض."""
    if FONT_REG.exists() and FONT_BOLD.exists():
        try:
            arch = pymupdf.Archive(str(FONTS_DIR))
            css = (
                f"@font-face {{ font-family: pv; src: url({FONT_REG.name}); }}\n"
                f"@font-face {{ font-family: pv; src: url({FONT_BOLD.name}); font-weight: bold; }}\n"
                # حاشیهٔ پیش‌فرض موتور HTML صفر شود تا اندازه‌گیری و درج دقیق و یکسان باشند
                "* { font-family: pv; margin: 0; padding: 0; }"
            )
            return arch, css
        except Exception as e:  # noqa: BLE001
            log(f"هشدار: بارگذاری فونت وزیرمتن ممکن نشد: {e}")
    return None, "* { font-family: sans-serif; margin: 0; padding: 0; }"


def _block_html(text: str, b: dict, scale: float = 1.0) -> str:
    """ساخت HTML یک بلوک با راست‌به‌چپ، اندازه، بولد/ایتالیک، رنگ و چینش.
    scale: ضریب یکنواختِ اندازهٔ فونت (برای هم‌اندازه شدن بلوک‌های صفحه)."""
    esc = html_mod.escape(text).replace("\n", "<br/>")
    style = [
        f"font-size:{b['size'] * scale:.2f}px",
        f"line-height:{b.get('leading', 1.4)}",
        f"text-align:{b['align']}",
        f"color:{b['color']}",
    ]
    if b.get("bold"):
        style.append("font-weight:bold")
    if b.get("italic"):
        style.append("font-style:italic")
    return f'<div dir="rtl" style="{";".join(style)}">{esc}</div>'


def _measure_scale(text: str, b: dict, rect: "pymupdf.Rect", css: str, arch) -> float:
    """
    بزرگ‌ترین ضریب (تا ۱٫۰) که متن با آن در کادر جا می‌شود.
    دقیقاً با همان مکانیزمِ insert_htmlbox (story.fit_scale + فلگ NO_OVERFLOW)
    اندازه‌گیری می‌شود تا نتیجه با درج واقعی مو‌به‌مو یکی باشد.
    """
    try:
        temp = pymupdf.Rect(0, 0, rect.width, rect.height)
        mycss = "body {margin:1px;}" + (css or "")
        story = pymupdf.Story(html=_block_html(text, b, 1.0), user_css=mycss, archive=arch)
        fit = story.fit_scale(
            temp,
            scale_min=1,
            scale_max=1.0 / MIN_BLOCK_SCALE,
            flags=pymupdf.mupdf.FZ_PLACE_STORY_FLAG_NO_OVERFLOW,
        )
        # big_enough یعنی در بازهٔ [۱، ۱/MIN] نقطه‌ای پیدا شد که متن در کادرِ بزرگ‌شده جا می‌شود؛
        # parameter همان ضریب بزرگ‌نمایی کادر است → مقیاس متن = ۱ / parameter
        if fit.big_enough:
            s = 1.0 / float(fit.parameter)
            return max(min(s, 1.0), MIN_BLOCK_SCALE)
        return MIN_BLOCK_SCALE  # حتی با بیشترین بزرگ‌نمایی کادر جا نشد
    except Exception:  # noqa: BLE001
        # اگر اندازه‌گیری ممکن نشد، بدون تغییر درج شود (insert_htmlbox خودش کوچک می‌کند)
        return 1.0


def _merge_overlapping_blocks(blocks: list[dict]) -> list[dict]:
    """ادغام بلوک‌هایی که کادرهایشان به‌طور چشمگیری روی هم افتاده‌اند،
    تا ترجمهٔ دو بلوک روی هم ننشیند (علت اصلی «متن روی متن»)."""
    def _area(r):
        return max(0.0, r.x1 - r.x0) * max(0.0, r.y1 - r.y0)

    merged_any = True
    while merged_any:
        merged_any = False
        for i in range(len(blocks)):
            done = False
            for j in range(i + 1, len(blocks)):
                a, b = blocks[i], blocks[j]
                ix0, iy0 = max(a["bbox"].x0, b["bbox"].x0), max(a["bbox"].y0, b["bbox"].y0)
                ix1, iy1 = min(a["bbox"].x1, b["bbox"].x1), min(a["bbox"].y1, b["bbox"].y1)
                if ix1 <= ix0 or iy1 <= iy0:
                    continue
                inter = (ix1 - ix0) * (iy1 - iy0)
                smaller = min(_area(a["bbox"]), _area(b["bbox"]))
                if smaller <= 0 or inter / smaller <= 0.3:
                    continue
                big, small = (a, b) if _area(a["bbox"]) >= _area(b["bbox"]) else (b, a)
                u = pymupdf.Rect(big["bbox"])
                u.include_rect(small["bbox"])
                big["bbox"] = u
                big["line_rects"] = big["line_rects"] + small["line_rects"]
                big["text"] = big["text"] + "\n" + small["text"]
                n = max(len(big["line_rects"]), 1)
                if n > 1 and big["size"] > 0:
                    big["leading"] = min(max(u.height / n / big["size"], 1.05), 1.8)
                blocks.remove(small)
                merged_any = done = True
                break
            if done:
                break
    return blocks


def _apply_redactions(page: "pymupdf.Page") -> None:
    """حذف متن اصلی بدون دست‌زدن به تصاویر و گرافیک."""
    try:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    except (TypeError, AttributeError):  # سازگاری با نسخه‌های قدیمی‌تر
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)


def _fit_rect(b: dict, rect: "pymupdf.Rect", page_rect: "pymupdf.Rect") -> "pymupdf.Rect":
    """کادرهای خیلی باریک (مثل شمارهٔ صفحه) را برای جای‌دادن متن فارسی گسترش می‌دهد،
    بدون جابه‌جا کردن لبهٔ لنگرِ چینش."""
    if rect.width <= 0 or rect.width >= b["size"] * 3:
        return rect
    extra = b["size"] * 6
    if b["align"] == "center":
        nr = pymupdf.Rect(rect.x0 - extra, rect.y0, rect.x1 + extra, rect.y1)
    else:  # راست‌چین: لبهٔ راست ثابت می‌ماند و به سمت چپ گسترش می‌یابد
        nr = pymupdf.Rect(rect.x0 - extra, rect.y0, rect.x1, rect.y1)
    nr.x0 = max(nr.x0, page_rect.x0 + 2)
    nr.x1 = min(nr.x1, page_rect.x1 - 2)
    return nr


def translate_pdf(input_pdf, output_pdf, api_key, *, model=DEFAULT_MODEL, delay=None,
                  progress=None, cancelled=None, log=print, translator=None,
                  mirror=True, base_url="", workers=1, spacing=DEFAULT_SPACING) -> dict:
    """
    ترجمهٔ کامل فایل PDF انگلیسی به فارسی با حفظ چیدمان.

    mirror: آینه‌سازی افقی چیدمان (تیتر/پاراگراف چپ‌چین انگلیسی به سمت راست صفحه می‌رود
    و ستون‌ها جابه‌جا می‌شوند) — برای متقارن‌سازی راست‌به‌چپ.
    base_url: نشانی chat/completions سرویس سازگار OpenAI (پیش‌فرض Groq؛
    برای Top Tools AI مقدار TTAI_API_URL را بدهید).
    workers: تعداد ترجمه‌های موازی. ۱ = پشت‌سرهم با مکث delay بین صفحات (مناسب Groq
    با سقف TPM)؛ ≥۲ = بچ‌های صفحات مختلف به‌موازات هم ترجمه می‌شوند (مناسب
    Top Tools AI با سقف RPM/همزمانی) و spacing حداقل فاصلهٔ شروع درخواست‌هاست.
    translator: در حالت عادی None است (ترجمه با وب‌سرویس). برای تست، تابعی
    (segments -> translations) می‌پذیرد تا بدون اینترنت هم پایپ‌لاین آزمایش شود.
    خروجی: دیکشنری آمار شامل usage (مصرف توکن ورودی/خروجی/مجموع و تعداد درخواست‌ها).
    """
    progress = progress or (lambda done, total: None)
    if delay is None:
        delay = DEFAULT_DELAY
    t0 = time.time()
    ensure_fonts(log)

    in_path = Path(input_pdf)
    if not in_path.exists():
        raise FileNotFoundError(f"فایل ورودی پیدا نشد: {in_path}")
    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".part.pdf")

    doc = pymupdf.open(str(in_path))
    total = doc.page_count
    log(f"فایل «{in_path.name}» با {total} صفحه باز شد. مدل: {model}")
    arch, css = _build_css()

    usage: dict = {}
    usage_lock = threading.Lock()
    limiter = _RateLimiter(spacing if workers > 1 else 0.0)
    stats = {"pages": total, "translated": 0, "no_text_pages": 0,
             "failed_pages": [], "output": str(out_path), "cancelled": False,
             "usage": usage}

    _PURE_NUM_RE = re.compile(r"^[\d\s.,;:\-–—/()%#]+$")

    def _call_batch(batch):
        """فراخوانی وب‌سرویس برای یک دسته؛ فاصله‌گذار نرخ بین ورکرها مشترک است."""
        limiter.wait()
        return groq_translate_segments(
            api_key, model, [t for _, t in batch], log=log, base_url=base_url,
            usage_acc=usage, usage_lock=usage_lock)

    pool = None
    if workers > 1 and translator is None:
        pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="tr")
        log(f"ترجمهٔ موازی فعال شد: {workers} ورکر، فاصلهٔ شروع درخواست‌ها {spacing:.1f} ثانیه.")

    # (pno, blocks, segs, trans, tasks) — tasks: [(batch, Future|None), ...]
    page_jobs: list[tuple[int, list, list, list, list]] = []

    try:
        # ── فاز ۱: استخراج صفحات و صف‌کردن بچ‌های ترجمه؛ ترجمهٔ صفحات بعدی
        #    در پس‌زمینه به‌موازات بازسازی صفحات قبلی پیش می‌رود
        for pno in range(total):
            if cancelled is not None and cancelled.is_set():
                stats["cancelled"] = True
                log("لغو شد؛ پیشرفت فعلی ذخیره می‌شود…")
                break

            page = doc[pno]
            blocks = extract_blocks(page)
            if not blocks:
                stats["no_text_pages"] += 1
                log(f"صفحهٔ {pno + 1}: متن قابل استخراج نداشت (تصویری است؟) — بدون تغییر کپی شد.")
                page_jobs.append((pno, [], [], [], []))
                continue
            # جلوگیری از «متن روی متن»: بلوک‌های همپوشان ادغام می‌شوند
            blocks = _merge_overlapping_blocks(blocks)

            segs = [b["text"] for b in blocks]
            trans = [""] * len(segs)

            if translator is not None:
                # حالت تست: ترجمهٔ ساختگی
                got = [str(t) for t in translator(segs)]
                trans = got + segs[len(got):]
                page_jobs.append((pno, blocks, segs, trans, []))
                continue

            # اعداد و علائم خالص (مثل شمارهٔ صفحه) نیازی به ترجمه ندارند
            items = [(i, s) for i, s in enumerate(segs)
                     if s.strip() and not _PURE_NUM_RE.match(s.strip())]
            for idx, s in enumerate(segs):
                if _PURE_NUM_RE.match(s.strip() or "x0x"):
                    trans[idx] = s

            tasks = []
            for batch in _batch_items(items):
                if pool is not None:
                    tasks.append((batch, pool.submit(_call_batch, batch)))
                else:
                    tasks.append((batch, None))
            page_jobs.append((pno, blocks, segs, trans, tasks))

        # ── فاز ۲: بازسازی صفحات به ترتیب؛ نتیجهٔ هر بچ گرفته و درج می‌شود
        for pno, blocks, segs, trans, tasks in page_jobs:
            if cancelled is not None and cancelled.is_set():
                stats["cancelled"] = True
                log("لغو شد؛ پیشرفت فعلی ذخیره می‌شود…")
                break

            if not blocks:
                progress(pno + 1, total)
                continue

            page = doc[pno]
            for batch, fut in tasks:
                try:
                    outs = fut.result() if fut is not None else _call_batch(batch)
                except FatalTranslationError:
                    raise
                except TranslationError as e:
                    stats["failed_pages"].append(pno + 1)
                    log(f"صفحهٔ {pno + 1}: {e} — متن اصلیِ این بخش حفظ شد.")
                    outs = None
                if outs is not None:
                    for (idx, _), tr in zip(batch, outs):
                        trans[idx] = tr

            # ۱) حذف متن اصلی صفحه
            for b in blocks:
                for r in b["line_rects"]:
                    pr = pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5)
                    page.add_redact_annot(pr, fill=False)
            _apply_redactions(page)

            # ۲) کادرهای هدف: با آینه‌سازی افقی، متن چپ‌چین انگلیسی به موقعیت متقارنِ راست می‌رود
            page_w = page.rect.width
            rects = []
            for b in blocks:
                r = b["bbox"]
                if mirror:
                    r = pymupdf.Rect(page_w - r.x1, r.y0, page_w - r.x0, r.y1)
                rects.append(r)

            # ۳) مقیاس یکنواخت صفحه: کمترین مقیاسی که همهٔ پاراگراف‌ها جا شوند؛
            #    همهٔ بلوک‌ها با همان ضریب کوچک می‌شوند تا اندازه‌ها یکنواخت بماند
            scales = {}
            for i, (b, t) in enumerate(zip(blocks, trans)):
                if (t or "").strip():
                    scales[i] = _measure_scale(t, b, rects[i], css, arch)
            para_scales = [scales[i] for i, b in enumerate(blocks)
                           if i in scales and len(b["line_rects"]) > 1]
            page_scale = max(min(para_scales), 0.7) if para_scales else 1.0
            page_scale = min(page_scale, 1.0)

            # ۴) درج ترجمهٔ فارسی
            for i, (b, t) in enumerate(zip(blocks, trans)):
                t = (t or "").strip()
                if not t:
                    continue
                s_own = scales.get(i, 1.0)
                use = page_scale if s_own >= page_scale else s_own
                if s_own < page_scale:
                    log(f"صفحهٔ {pno + 1}: یک بلوک با مقیاس {int(s_own * 100)}٪ درج شد "
                        f"(در مقیاس یکنواختِ {int(page_scale * 100)}٪ جا نشد).")
                rect = _fit_rect(b, rects[i], page.rect)
                if rect.is_empty or rect.width <= 2 or rect.height <= 2:
                    continue
                try:
                    _spare, shrink = page.insert_htmlbox(
                        rect, _block_html(t, b, use), css=css, archive=arch)
                    if shrink and shrink < 0.85:
                        log(f"صفحهٔ {pno + 1}: یک بلوک برای جا شدن به {int(shrink * 100)}٪ کوچک شد.")
                except Exception as e:  # noqa: BLE001
                    log(f"صفحهٔ {pno + 1}: درج متن ناموفق بود: {e}")

            stats["translated"] += 1
            progress(pno + 1, total)

            # ذخیرهٔ موقت هر ۲۵ صفحه (برای جلوگیری از از دست رفتن پیشرفت)
            if (pno + 1) % 25 == 0 and pno + 1 < total:
                doc.save(str(tmp_path), garbage=3, deflate=True)
                log(f"ذخیرهٔ موقت انجام شد ({pno + 1} از {total}).")

            if pool is None and delay and pno + 1 < total:
                time.sleep(delay)
    finally:
        # بستن استخر: کارهای در صف لغو می‌شوند؛ حداکثر «workers» درخواستِ در جریان تمام می‌شود
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    if stats["cancelled"]:
        doc.save(str(tmp_path), garbage=3, deflate=True)
        stats["output"] = str(tmp_path)
    else:
        doc.save(str(out_path), garbage=3, deflate=True)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    doc.close()

    dt = time.time() - t0
    log(f"پایان: {stats['translated']} از {total} صفحه در {dt:.0f} ثانیه پردازش شد. "
        f"خروجی: {stats['output']}")
    if usage:
        log(f"مصرف توکن: ورودی {usage.get('prompt_tokens', 0):,} | "
            f"خروجی {usage.get('completion_tokens', 0):,} | "
            f"مجموع {usage.get('total_tokens', 0):,} در {usage.get('api_calls', 0)} درخواست")
    return stats


# --------------------------------------------------------------------------
# اجرای مستقل از خط فرمان
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ترجمهٔ PDF انگلیسی → فارسی (هستهٔ برنامه)")
    ap.add_argument("input", help="مسیر فایل PDF انگلیسی")
    ap.add_argument("-o", "--output", help="مسیر خروجی (پیش‌فرض: نام فایل + fa.")
    ap.add_argument("-k", "--api-key", help="کلید API گروق (در نبود آن از config.json خوانده می‌شود)")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL, help="نام مدل")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="مکث بین صفحات به ثانیه (حالت پشت‌سرهم)")
    ap.add_argument("--base-url", default="", help="نشانی سرویس سازگار OpenAI (پیش‌فرض Groq)")
    ap.add_argument("--workers", type=int, default=1, help="تعداد ترجمه‌های موازی (۱ = پشت‌سرهم)")
    ap.add_argument("--no-mirror", action="store_true",
                    help="غیرفعال‌کردن آینه‌سازی افقی چیدمان")
    a = ap.parse_args()

    _key = a.api_key or ""
    if not _key:
        _cfg = APP_DIR / "config.json"
        if _cfg.exists():
            try:
                _key = json.loads(_cfg.read_text(encoding="utf-8")).get("api_key", "")
            except Exception:
                _key = ""
    if not _key:
        print("کلید API لازم است. با --api-key بدهید یا در config.json ذخیره کنید.")
        sys.exit(1)

    _out = a.output or str(Path(a.input).with_name(Path(a.input).stem + "_fa.pdf"))
    translate_pdf(a.input, _out, _key, model=a.model, delay=a.delay, mirror=not a.no_mirror,
                  base_url=a.base_url, workers=max(1, a.workers),
                  progress=lambda d, t: print(f"صفحه {d}/{t}", end="\r", flush=True),
                  log=print)
    print()
