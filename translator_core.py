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
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import requests

# --------------------------------------------------------------------------
# تنظیمات سرویس و مدل
# --------------------------------------------------------------------------

# ─── سرویس ترجمه: OpenCode Zen از طریق خودِ CLI ───
# ردهٔ رایگان Zen فقط به درخواستِ «از داخل خودِ OpenCode» جواب می‌دهد؛
# پس ترجمه با اجرای `opencode run` انجام می‌شود (ترافیک واقعی OpenCode).
# تابع api_translate_segments فقط برای تست با سرور موک محلی می‌ماند.
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"  # فقط مرجع تست

# مدل‌های رایگان OpenCode Zen (پسوند -free). فهرست پویا با `opencode models`
# خوانده می‌شود؛ این لیست فقط پشتیبانِ نبودِ CLI است.
OPENCODE_FREE_MODELS = [
    "deepseek-v4.1-flash-free",   # پیش‌فرض: سریع و چندزبانه
    "glm-5.3-flash-free",
    "minimax-m3.1-flash-free",
    "mimo-v2.6-flash-free",
    "longcat-2.5-preview-free",
    "nemotron-3.5-lightning-free",
    "nemotron-3.5-super-vl-free",
    "beam-free",
    "big-pickle",
]
DEFAULT_MODEL = OPENCODE_FREE_MODELS[0]

# مکث پیش‌فرض بین صفحات در حالت پشت‌سرهم (Groq با سقف ~۸هزار توکن در دقیقه).
# در حالت موازی (Top Tools AI) مکث معنی‌دار نیست؛ فاصله‌گذار نرخ (spacing) اعمال می‌شود.
DEFAULT_DELAY = 15.0

# فاصلهٔ پیش‌فرض شروع درخواست‌ها در حالت موازی (ثانیه) ≈ ۴۳ درخواست در دقیقه
# زیر سقف ۴۵RPM سرویس‌ها؛ ۴29ها هم خودکار طبق retry-after مدیریت می‌شوند.
DEFAULT_SPACING = 1.4

# کمترین مقیاس مجاز برای کوچک‌شدن متن یک بلوک (زیر این حد، اندازه دیگر خوانا نیست)
MIN_BLOCK_SCALE = 0.55

# ضریب یکنواخت سایز فونت فارسی: وزیرمتن در هم‌اندازهٔ فونت‌های لاتین بزرگ‌تر دیده
# می‌شود؛ ~۱۰٪ کوچک‌تر کردن، نتیجه را هم‌قدِ متن اصلی می‌کند (فیدبک کاربر).
FONT_SCALE_FACTOR = 0.90

# فاصلهٔ خطوط: کف بالاتر + تقویت ۱۲٪ نسبت به PDF اصلی (فارسی به فاصلهٔ بیشتری
# نیاز دارد؛ فیدبک کاربر: «فاصله بین خطوط کمه»).
MIN_LEADING = 1.55
MAX_LEADING = 2.0

# زبان‌های راست‌به‌چپ (برای آینه‌سازی و dir)
RTL_LANGS = {"fa", "ar", "he", "ur", "ps", "ckb", "sd", "ug", "yi"}

# نام نمایشی زبان‌ها برای پرامپت
LANG_NAMES = {
    "auto": "auto-detect the source language",
    "en": "English", "fa": "Persian (Farsi)", "ar": "Arabic",
    "fr": "French", "de": "German", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "tr": "Turkish", "ru": "Russian",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "hi": "Hindi",
    "ur": "Urdu",
}

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

def build_system_prompt(src_lang: str = "auto", dst_lang: str = "fa") -> str:
    """پرامپت سیستمی بر اساس زبان مبدأ و مقصد انتخابی ادمین."""
    src = LANG_NAMES.get(src_lang, src_lang or "auto-detect the source language")
    dst = LANG_NAMES.get(dst_lang, dst_lang or "Persian (Farsi)")
    return f"""You are a meticulous, strictly literal document translator for books and documents.

Task: translate from {src} into {dst}.

You will receive a JSON object of the form {{"segments": ["...", ...]}}.
Return ONLY a JSON object of the form {{"translations": ["...", ...]}} with EXACTLY the same number of items, where item i is the faithful translation of segment i into {dst}.

Strict rules:
1. Absolute fidelity: translate every sentence completely. Never add, omit, summarize, paraphrase, explain, or answer the content. Bring the exact sentences as they are, only in {dst}.
2. Keep the sentence order and any internal line breaks; do not merge or split items.
3. Keep numbers, units, formulas, code, URLs, emails, and proper nouns of the source language unchanged. Well-known people/places may use their common spelling in {dst}.
4. Use natural, correct {dst} with proper punctuation; use half-space (ZWNJ) where appropriate if {dst} needs it.
5. Headings, list items, captions and page numbers must be translated in the same concise style.
6. Output only the JSON object. No explanations, no markdown, no notes."""


SYSTEM_PROMPT = build_system_prompt("auto", "fa")

# --------------------------------------------------------------------------
# خطاها
# --------------------------------------------------------------------------


class TranslationError(Exception):
    """خطای قابل تلاشِ مجدد در ترجمه."""


class FatalTranslationError(TranslationError):
    """خطای غیرقابل جبران (مثل کلید نامعتبر) — تلاش مجدد بی‌فایده است.

    code برای تصمیم فیلورورِ ربات است:
      • auth     — کلید API نامعتبر
      • access   — دسترسی مسدود (اشتراک/آی‌پی)
      • model    — مدل پیدا نشد
      • provider — قطعی بالادستیِ خود سرویس (مثل provider_http_401 گیت‌وی)
      • config   — سایر موارد
    """

    def __init__(self, msg: str, code: str = "config"):
        super().__init__(msg)
        self.code = code


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
            leading = min(max(bbox.height / n_lines / size * 1.12, MIN_LEADING), MAX_LEADING)
        else:
            leading = MIN_LEADING
        blocks.append({
            "bbox": bbox,
            "line_rects": line_rects,
            "raw_lines": raw_lines,
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


def _union_rects(rects: list["pymupdf.Rect"]) -> "pymupdf.Rect":
    u = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        u |= r
    return u


def _split_blocks_by_tables(page: "pymupdf.Page", blocks: list[dict],
                            log=print) -> list[dict]:
    """بلوک‌های داخل جدول را به‌ازای هر سلول جدا می‌کند.

    بدون این کار، سلول‌های یک ردیف جدول در یک بلوک می‌افتند و ترجمه‌شان به‌صورت
    یک پاراگراف روی چند خانه می‌نشیند و از خطوط شبکه بیرون می‌زند. با جداسازی،
    ترجمهٔ هر سلول در خانهٔ آینه‌شدهٔ خودش درج می‌شود.
    """
    if len(blocks) < 2:
        return blocks
    try:
        tabs = page.find_tables()
        tables = [t for t in tabs.tables
                  if t.row_count >= 2 and t.col_count >= 2]
    except Exception:  # noqa: BLE001
        return blocks
    if not tables:
        return blocks

    # تک‌تک سلول‌های همهٔ جدول‌ها (سلول‌های ادغام‌شده فقط یک‌بار)
    all_cells: list[pymupdf.Rect] = []
    seen = set()
    for t in tables:
        for row in t.rows:
            for c in (row.cells or []):
                if not c:
                    continue
                key = (round(c[0], 1), round(c[1], 1), round(c[2], 1), round(c[3], 1))
                if key not in seen:
                    seen.add(key)
                    all_cells.append(pymupdf.Rect(c))
    if len(all_cells) < 2:
        return blocks

    # هر خطِ متن در سلولی می‌افتد که مرکزش داخل آن است
    per_cell: list[list[tuple[int, int]]] = [[] for _ in all_cells]
    for bi, b in enumerate(blocks):
        for li, lr in enumerate(b["line_rects"]):
            cx, cy = (lr.x0 + lr.x1) / 2, (lr.y0 + lr.y1) / 2
            for ci, cell in enumerate(all_cells):
                if cell.x0 - 1 <= cx <= cell.x1 + 1 and cell.y0 - 1 <= cy <= cell.y1 + 1:
                    per_cell[ci].append((bi, li))
                    break

    consumed_pairs: set[tuple[int, int]] = set()
    consumed_blocks: set[int] = set()
    cell_blocks: list[dict] = []
    for ci, items in enumerate(per_cell):
        if not items:
            continue
        cell = all_cells[ci]
        cnt: dict[int, int] = {}
        for bi, li in items:
            cnt[bi] = cnt.get(bi, 0) + 1
        # اگر هر بلوکِ درگیر کامل داخل همین سلول است، دست نمی‌زنیم
        if all(cnt[bi] == len(blocks[bi]["line_rects"]) for bi in cnt):
            continue
        clipped: list[pymupdf.Rect] = []
        raws: list[str] = []
        src = blocks[items[0][0]]
        for bi, li in items:
            lr = blocks[bi]["line_rects"][li]
            cr = pymupdf.Rect(max(lr.x0, cell.x0), max(lr.y0, cell.y0),
                              min(lr.x1, cell.x1), min(lr.y1, cell.y1))
            if cr.is_empty or cr.width <= 1:
                continue
            consumed_pairs.add((bi, li))
            consumed_blocks.add(bi)
            clipped.append(cr)
            raws.append(blocks[bi]["raw_lines"][li] if li < len(blocks[bi].get("raw_lines", []))
                        else blocks[bi]["text"])
        if not clipped:
            continue
        bbox = _union_rects(clipped)
        cell_blocks.append({
            "bbox": bbox,
            "line_rects": clipped,
            "raw_lines": raws,
            "text": _join_lines(raws),
            "size": src["size"],
            "leading": src["leading"],
            "bold": src["bold"],
            "italic": src["italic"],
            "color": src["color"],
            "align": _detect_align(bbox, clipped),
        })

    if not cell_blocks:
        return blocks
    log(f"{len(cell_blocks)} سلول جدول جدا و مستقل ترجمه می‌شود.")

    out: list[dict] = []
    for bi, b in enumerate(blocks):
        if bi not in consumed_blocks:
            out.append(b)
            continue
        resid = [li for li in range(len(b["line_rects"])) if (bi, li) not in consumed_pairs]
        if resid:
            lrs = [b["line_rects"][li] for li in resid]
            # گارد: اگر raw_lines به هر دلیلی کوتاه‌تر بود (بلوک‌های ادغام‌شدهٔ قدیمی)،
            # به‌جای IndexError خط بدون متنِ شناخته‌شده رد شود.
            raws = [b["raw_lines"][li] if li < len(b["raw_lines"]) else "" for li in resid]
            nb = dict(b)
            nb["line_rects"] = lrs
            nb["raw_lines"] = raws
            nb["bbox"] = _union_rects(lrs)
            nb["text"] = _join_lines(raws)
            nb["align"] = _detect_align(nb["bbox"], lrs)
            out.append(nb)
    out.extend(cell_blocks)
    return out


# --------------------------------------------------------------------------
# هایلایت‌ها — انتقال هایلایتِ متنِ اصلی onto ترجمه
# --------------------------------------------------------------------------

HL_MARK_TYPES = ("Highlight", "Underline", "Squiggly", "StrikeOut")
HL_DEFAULT_COLOR = (1.0, 0.9, 0.2)


def _page_highlights(page: "pymupdf.Page") -> list[tuple["pymupdf.Rect", tuple[float, float, float]]]:
    """مستطیل و رنگ هایلایت‌های واقعی (Annotation) صفحه.

    فقط انواع علامت‌گذاریِ متن (هایلایت/زیرخط/…) — با این فرض که روی متن نشسته‌اند.
    رنگ از خود حاشیه‌نویسی خوانده می‌شود (پیش‌فرض: زرد استاندارد)."""
    out: list[tuple["pymupdf.Rect", tuple[float, float, float]]] = []
    try:
        for a in (page.annots() or []):
            try:
                if a.type[1] not in HL_MARK_TYPES:
                    continue
                r = pymupdf.Rect(a.rect)
                if r.is_empty or r.is_infinite or r.width <= 1 or r.height <= 1:
                    continue
                col = HL_DEFAULT_COLOR
                try:
                    c = (a.colors or {}).get("stroke") or []
                    if len(c) >= 3:
                        col = tuple(max(0.0, min(1.0, float(x))) for x in c[:3])
                except Exception:  # noqa: BLE001
                    pass
                out.append((r, col))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return []
    return out


def _split_blocks_by_highlights(
        blocks: list[dict], hl: list[tuple["pymupdf.Rect", tuple[float, float, float]]],
        log=print) -> tuple[list[dict], int, int]:
    """بلوک‌ها را در مرزِ خط‌های هایلایت‌شده می‌شکند.

    خروجی: (بلوک‌های جدید، تعداد بخش‌های هایلایت‌شده، تعداد هایلایت بی‌اثر)
    بخش‌های هایلایت‌شده با فلگ _hl=True و رنگ _hl_color مشخص می‌شوند تا بعد از
    ترجمه، هایلایتِ تازه دقیقاً زیر همان بخش از ترجمه بنشیند. هایلایت‌هایی که
    هیچ خط متنی باشان همپوشانی نداشتند (روی عکس/حاشیه) به‌عنوان بی‌اثر گزارش می‌شوند.
    """
    if not hl or not blocks:
        return blocks, 0, 0
    n_hl = 0
    used = [False] * len(hl)
    out: list[dict] = []
    for b in blocks:
        lrs = b["line_rects"]
        raws = list(b.get("raw_lines") or [])
        if len(raws) < len(lrs):
            raws += [""] * (len(lrs) - len(raws))
        marks: list[bool] = [False] * len(lrs)
        mark_color: dict[int, tuple[float, float, float]] = {}
        for li, lr in enumerate(lrs):
            la = max(lr.width * lr.height, 1e-6)
            for hi, (hr, col) in enumerate(hl):
                it = lr & hr
                if it.is_empty:
                    continue
                ia = it.width * it.height
                ha = max(hr.width * hr.height, 1e-6)
                # هم پوششِ چشمگیرِ خط، هم هایلایتِ کوچکِ درونِ خط (مثل چند کلمه)
                if ia / la >= 0.35 or ia / ha >= 0.5:
                    marks[li] = True
                    used[hi] = True
                    mark_color.setdefault(li, col)
                    break
        if not any(marks):
            out.append(b)
            continue
        if all(marks):
            # کل بلوک هایلایت است — دست‌نخورده نگه داشته می‌شود
            b["_hl"] = True
            b["_hl_color"] = mark_color.get(0, HL_DEFAULT_COLOR)
            n_hl += 1
            out.append(b)
            continue
        # شکستن به ران‌های پیوستهٔ هایلایت/غیرهایلایت
        runs: list[dict] = []
        cur: dict | None = None
        for li, m in enumerate(marks):
            if cur is None or cur["hl"] != m:
                cur = {"hl": m, "idx": []}
                runs.append(cur)
            cur["idx"].append(li)
        for run in runs:
            nb = {k: v for k, v in b.items()
                  if k not in ("bbox", "line_rects", "raw_lines", "text", "_hl", "_hl_color")}
            nb["line_rects"] = [lrs[i] for i in run["idx"]]
            nb["raw_lines"] = [raws[i] for i in run["idx"]]
            nb["text"] = _join_lines(nb["raw_lines"])
            if not nb["text"].strip():
                continue
            nb["bbox"] = _union_rects(nb["line_rects"])
            if nb["bbox"].is_empty or nb["bbox"].width <= 2 or nb["bbox"].height <= 2:
                continue
            nb["align"] = _detect_align(nb["bbox"], nb["line_rects"])
            if run["hl"]:
                nb["_hl"] = True
                nb["_hl_color"] = mark_color.get(run["idx"][0], HL_DEFAULT_COLOR)
                n_hl += 1
            out.append(nb)
    dropped = sum(1 for u in used if not u)
    if n_hl or dropped:
        log(f"هایلایت‌ها: {n_hl} بخش متنِ هایلایت‌شده برای انتقال روی ترجمه جدا شد"
            + (f"؛ {dropped} هایلایت روی متن نبود (عکس/حاشیه) و حذف می‌شود." if dropped else "."))
    return out, n_hl, dropped


def _batch_items(items: list[tuple[int, str]], max_chars: int = 12000) -> list[list[tuple[int, str]]]:
    """دسته‌بندی آیتم‌ها بر اساس حجم کاراکتر (بچ بزرگ‌تر = فراخوانی CLI کمتر = سریع‌تر)."""
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


# --------------------------------------------------------------------------
# OCR — استخراج متن از صفحات تصویری (اسکن/اسکرین‌شات/کامیک) با Tesseract
# --------------------------------------------------------------------------

# نگاشت کد زبان → زبان Tesseract
TESS_LANG_MAP = {
    "en": "eng", "fa": "fas", "ar": "ara", "fr": "fra", "de": "deu",
    "es": "spa", "it": "ita", "pt": "por", "tr": "tur", "ru": "rus",
    "zh": "chi_sim", "ja": "chi_sim", "ko": "kor", "hi": "hin", "ur": "urd",
}


def tess_lang_for(src_lang: str) -> str:
    """کد زبان تسرکت بر اساس زبان مبدأ؛ «auto» ترکیب انگلیسی+فارسی."""
    if not src_lang or src_lang == "auto":
        return "eng+fas"
    return TESS_LANG_MAP.get(src_lang, "eng")


def ocr_available() -> bool:
    return bool(shutil.which("tesseract"))


def _sample_fill(pix, box_px: tuple[float, float, float, float],
                 page_w_px: int, page_h_px: int) -> tuple[float, float, float]:
    """رنگ پس‌زمینهٔ اطراف کادر متن از خود تصویر نمونه‌برداری می‌شود تا پوششِ
    متن اصلی هم‌رنگ حباب/صفحه باشد (سفید، مشکی یا هر رنگ دیگر)."""
    x0, y0, x1, y1 = box_px
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    pts = [(cx, y0 - 3), (cx, y1 + 3), (x0 - 3, cy), (x1 + 3, cy),
           (cx, y0 - 9), (cx, y1 + 9)]
    cols = []
    try:
        for px, py in pts:
            ix = int(min(max(px, 0), page_w_px - 1))
            iy = int(min(max(py, 0), page_h_px - 1))
            p = pix.pixel(ix, iy)
            if p and len(p) >= 3:
                cols.append((p[0], p[1], p[2]))
    except Exception:  # noqa: BLE001
        return (1.0, 1.0, 1.0)
    if not cols:
        return (1.0, 1.0, 1.0)
    med = tuple(sorted(c[i] for c in cols)[len(cols) // 2] for i in range(3))
    return (round(med[0] / 255.0, 3), round(med[1] / 255.0, 3), round(med[2] / 255.0, 3))


def ocr_page_blocks(page: "pymupdf.Page", langs: str, dpi: int = 200,
                    log=print) -> list[dict]:
    """متن صفحهٔ تصویری با Tesseract استخراج و به بلوک‌های هم‌ساختارِ
    extract_blocks تبدیل می‌شود (bbox/اندازه/فاصلهٔ خط) + رنگ پس‌زمینه برای
    پوششِ متن اصلی. خروجی خالی یعنی OCR چیزی پیدا نکرد."""
    if not langs:
        langs = "eng+fas"
    import tempfile
    zoom = dpi / 72.0
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    except Exception as e:  # noqa: BLE001
        log(f"OCR: رندر صفحه ناموفق بود: {e}")
        return []
    tmpd = Path(tempfile.mkdtemp(prefix="ocr"))
    tmp = tmpd / "page.png"
    try:
        tmp.write_bytes(pix.tobytes("png"))
        try:
            proc = subprocess.run(
                ["tesseract", str(tmp), "stdout", "-l", langs,
                 "--oem", "3", "--psm", "3", "tsv"],
                capture_output=True, text=True, timeout=180)
        except FileNotFoundError:
            log("tesseract نصب نیست؛ OCR انجام نشد.")
            return []
        except subprocess.TimeoutExpired:
            log("OCR تایم‌اوت شد؛ این صفحه رد شد.")
            return []
        if proc.returncode != 0:
            log(f"OCR ناموفق بود: {(proc.stderr or '')[:120]}")
            return []
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmpd.rmdir()
        except OSError:
            pass

    scale = 72.0 / float(dpi)
    pw, ph = pix.width, pix.height
    header: list[str] | None = None
    lines: dict[tuple, list] = {}
    order: list[tuple] = []
    for ln in (proc.stdout or "").splitlines():
        parts = ln.split("\t")
        if header is None:
            if ln.startswith("level\t"):
                header = parts
            continue
        if header is None or len(parts) < len(header):
            continue
        d = dict(zip(header, parts))
        if d.get("level") != "5":  # فقط واژه‌ها
            continue
        try:
            conf = float(d.get("conf", "-1"))
        except ValueError:
            conf = -1.0
        txt = (d.get("text") or "").strip()
        if conf < 35 or not txt:
            continue
        try:
            key = (int(d.get("block_num", "0")), int(d.get("par_num", "0")),
                   int(d.get("line_num", "0")))
        except ValueError:
            continue
        if key not in lines:
            lines[key] = []
            order.append(key)
        try:
            lines[key].append((int(d.get("left", "0")), int(d.get("top", "0")),
                               int(d.get("width", "0")), int(d.get("height", "0")), txt))
        except ValueError:
            continue
    if not lines:
        return []

    # هر (block, par) تسرکت یک بلوک؛ خطوطش به‌ترتیب x مرتب می‌شوند
    par_lines: dict[tuple, list] = {}
    par_order: list[tuple] = []
    for key in order:
        pk = (key[0], key[1])
        if pk not in par_lines:
            par_lines[pk] = []
            par_order.append(pk)
        ws = sorted(lines[key], key=lambda w: w[0])
        rect = pymupdf.Rect(min(w[0] for w in ws) * scale, min(w[1] for w in ws) * scale,
                            max(w[0] + w[2] for w in ws) * scale,
                            max(w[1] + w[3] for w in ws) * scale)
        par_lines[pk].append((rect, " ".join(w[4] for w in ws)))

    blocks: list[dict] = []
    for pk in par_order:
        lrs = [r for r, _ in par_lines[pk]]
        raws = [t for _, t in par_lines[pk]]
        if not any(t.strip() for t in raws):
            continue
        bbox = _union_rects(lrs)
        if bbox.width <= 2 or bbox.height <= 2:
            continue
        hs = sorted(r.height for r in lrs)
        size = max(round((hs[len(hs) // 2] * 0.74) * 2) / 2, 4.5)
        leading = MIN_LEADING
        if len(lrs) > 1:
            tops = sorted(r.y0 for r in lrs)
            gaps = [b - a for a, b in zip(tops, tops[1:]) if b - a > 1]
            if gaps:
                g = sorted(gaps)[len(gaps) // 2]
                leading = min(max(g / max(size, 1.0), 1.2), 1.9)
        fill = _sample_fill(pix, (bbox.x0 / scale, bbox.y0 / scale,
                                  bbox.x1 / scale, bbox.y1 / scale), pw, ph)
        lum = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
        blocks.append({
            "bbox": bbox, "line_rects": lrs, "raw_lines": raws,
            "text": _join_lines(raws), "size": size, "leading": leading,
            "bold": False, "italic": False,
            "color": "#ffffff" if lum < 0.45 else "#000000",
            "align": "right", "_ocr": True, "_fill": fill,
        })
    # ترتیب خواندن: بالا به پایین؛ در هم‌ارتفاع‌ها راست به چپ (اسناد RTL/کمیک)
    blocks.sort(key=lambda b: (round(b["bbox"].y0, 1), -round(b["bbox"].x0, 1)))
    return blocks


def _rasterize_page(page: "pymupdf.Page", dpi: int, quality: int, log=print) -> None:
    """بازسازی محتوای صفحهٔ تصویری به یک JPEG فشرده — کاهش شدید حجم خروجی.
    فقط برای صفحاتی به‌کار می‌رود که همهٔ محتوایشان تصویر است (حالت عکس‌محور)."""
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
        jpg = pix.tobytes("jpeg", jpg_quality=quality)
        page.clean_contents()
        # منابع کهنهٔ صفحه (تصویر اصلی حجیم) خالی شود تا در ذخیرهٔ نهایی با
        # garbage=4 جمع‌آوری شود؛ وگرنه تصویر یتیم در /Resources می‌ماند و
        # از حجم خروجی چیزی کم نمی‌شود.
        try:
            page.parent.xref_set_key(page.xref, "Resources", "<<>>")
        except Exception:  # noqa: BLE001
            pass
        page.insert_image(page.rect, stream=jpg)
    except Exception as e:  # noqa: BLE001
        log(f"فشرده‌سازی صفحهٔ تصویری ناموفق بود (اصل حفظ شد): {e}")


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


def api_translate_segments(api_key: str, model: str, segments: list[str], *,
                            temperature: float = 0.0, timeout: int = 240,
                            max_retries: int = 5, log=print,
                            base_url: str = "",
                            src_lang: str = "auto", dst_lang: str = "fa",
                            usage_acc: dict | None = None,
                            usage_lock: threading.Lock | None = None) -> list[str]:
    """ترجمهٔ یک دسته متن با هر سرویس سازگار OpenAI (JSON mode)؛
    فقط برای تست با سرور موک محلی استفاده می‌شود (مسیر اصلی: OpenCode CLI).
    اگر usage_acc داده شود، مصرف توکن هر پاسخ موفق
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
            {"role": "system", "content": build_system_prompt(src_lang, dst_lang)},
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
            body = (resp.text or "").strip()
            if resp.status_code == 401:
                # گیت‌وی سرویس (مثل Top Tools AI) وقتی کلیدِ ما سالم است ولی
                # تأمین‌کنندهٔ بالادستی‌اش رد می‌کند، همان 401 را با بدنهٔ
                # provider_http_401 برمی‌گرداند — این قطعیِ سرویس است نه مشکل کلید.
                if "provider_http" in body:
                    raise FatalTranslationError(
                        "سرویس ترجمه موقتاً قطع است (تأمین‌کنندهٔ بالادستی درخواست را رد کرد).",
                        code="provider")
                raise FatalTranslationError("کلید API نامعتبر است (کد 401).", code="auth")
            if resp.status_code == 403:
                if "provider_http" in body:
                    raise FatalTranslationError(
                        "سرویس ترجمه موقتاً قطع است (تأمین‌کنندهٔ بالادستی دسترسی را مسدود کرد).",
                        code="provider")
                raise FatalTranslationError(
                    "دسترسی مسدود شد (کد 403). اگر مدل نیازمند اشتراک است، آن را در پنل سرویس فعال کنید؛ "
                    "کلید را بررسی کنید و در صورت محدودیت جغرافیایی IP را عوض کنید.", code="access")
            if resp.status_code == 404:
                raise FatalTranslationError(
                    f"مدل '{model}' پیدا نشد (کد 404). نام مدل را در تنظیمات اصلاح کنید.",
                    code="model")
            if resp.status_code >= 400:
                # خطای شناخته‌نشده (گاهی گیت‌وی سرویس خطاهای گذرای 400/5xx می‌دهد)
                # → با بدنهٔ خطا در پیام، قابل تلاش مجدد در نظر گرفته می‌شود.
                if "provider_http_401" in body:
                    raise FatalTranslationError(
                        "سرویس ترجمه موقتاً قطع است (تأمین‌کنندهٔ بالادستی درخواست را رد کرد).",
                        code="provider")
                if "model_not_found" in body or "does not exist" in body:
                    raise FatalTranslationError(
                        f"مدل '{model}' روی این سرویس پیدا نشد (کد {resp.status_code}). "
                        "نام مدل را در تنظیمات اصلاح کنید.", code="model")
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


def selftest(api_key: str, model: str, log=print, base_url: str = "",
             src_lang: str = "auto", dst_lang: str = "fa") -> list[str]:
    """تست سریع اتصال/کلید/مدل با دو جملهٔ کوتاه."""
    segs = ["Hello world.", "The sun rises in the east, and the moon watches quietly."]
    outs = api_translate_segments(api_key, model, segs, log=log, base_url=base_url,
                                  src_lang=src_lang, dst_lang=dst_lang)
    for s, o in zip(segs, outs):
        log(f"EN : {s}")
        log(f"FA : {o}")
    log("تست اتصال موفق بود.")
    return outs


# --------------------------------------------------------------------------
# ترجمه از طریق خودِ CLI اوپن‌کد (مسیر اصلی — ردهٔ رایگان Zen فقط از داخل
# OpenCode جواب می‌دهد، پس ترجمه را `opencode run` انجام می‌دهد)
# --------------------------------------------------------------------------


def find_opencode(explicit: str = "") -> str:
    """یافتن باینری opencode: پارامتر صریح > متغیر محیطی > PATH > مسیر نصب پیش‌فرض."""
    cands = [explicit or "", os.environ.get("OPENCODE_BIN", ""), "opencode",
             str(Path.home() / ".opencode" / "bin" / "opencode")]
    for c in cands:
        if not c:
            continue
        p = shutil.which(c)
        if p:
            return p
        try:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        except OSError:
            continue
    return ""


def _oc_parse_events(out: str) -> tuple[str, dict]:
    """از خروجی --format json (رویدادهای خط‌به‌خط) آخرین متن پاسخ و توکن‌های
    آخرین step_finish را برمی‌گرداند."""
    text = ""
    tokens: dict = {}
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln or not ln.startswith("{"):
            continue
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        part = d.get("part") or {}
        if t == "text" and isinstance(part.get("text"), str):
            text = part["text"]
        elif t == "step_finish":
            tk = part.get("tokens") or {}
            if isinstance(tk, dict) and tk:
                tokens = tk
    return text, tokens


def oc_translate_segments(model: str, segments: list[str], *,
                          src_lang: str = "auto", dst_lang: str = "fa",
                          timeout: int = 300, max_retries: int = 4, log=print,
                          oc_bin: str = "opencode", oc_cwd: str | None = None,
                          usage_acc: dict | None = None,
                          usage_lock: threading.Lock | None = None) -> list[str]:
    """ترجمهٔ یک دسته با اجرای `opencode run` (کلید/احراز از auth.json).

    مدل باید بدون پیشوند provider باشد (مثل deepseek-v4.1-flash-free)؛
    پیشوند opencode/ اینجا اضافه می‌شود.
    """
    if not segments:
        return []
    binpath = find_opencode(oc_bin)
    if not binpath:
        raise FatalTranslationError(
            "CLI اوپن‌کد پیدا نشد؛ OPENCODE_BIN را تنظیم کنید.", code="config")
    payload = json.dumps({"segments": segments}, ensure_ascii=False)
    prompt = (build_system_prompt(src_lang, dst_lang)
              + "\n\nTranslate the following JSON now and return only the JSON object:\n"
              + payload)
    args = [binpath, "run", "-m", f"opencode/{model}", "--pure", "--format", "json", prompt]
    run_cwd = None
    if oc_cwd:
        run_cwd = str(Path(oc_cwd).resolve())
        try:
            Path(run_cwd).mkdir(parents=True, exist_ok=True)
        except OSError:
            run_cwd = None
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout,
                cwd=run_cwd, stdin=subprocess.DEVNULL)
            out, errout = proc.stdout or "", proc.stderr or ""
            blob = out + "\n" + errout
            if "FreeTierError" in blob:
                raise FatalTranslationError(
                    "ردهٔ رایگان OpenCode فقط از داخل خودِ OpenCode در دسترس است.",
                    code="access")
            if "Model not found" in blob or "model_not_found" in blob:
                raise FatalTranslationError(
                    f"مدل '{model}' روی OpenCode پیدا نشد؛ مدل دیگری انتخاب کنید.",
                    code="model")
            if "Insufficient account funds" in blob:
                raise FatalTranslationError(
                    "موجودی حساب OpenCode کافی نیست؛ مدل رایگان انتخاب کنید.",
                    code="access")
            if not out.strip():
                raise TranslationError(
                    f"CLI پاسخی نداد (کد {proc.returncode}): {errout.strip()[:160]}")
            text, tokens = _oc_parse_events(out)
            if tokens and usage_acc is not None:
                if usage_lock is not None:
                    usage_lock.acquire()
                try:
                    inp = int(tokens.get("input", 0) or 0)
                    outp = int(tokens.get("output", 0) or 0)
                    usage_acc["prompt_tokens"] = usage_acc.get("prompt_tokens", 0) + inp
                    usage_acc["completion_tokens"] = usage_acc.get("completion_tokens", 0) + outp
                    usage_acc["total_tokens"] = usage_acc.get("total_tokens", 0) + inp + outp
                    usage_acc["api_calls"] = usage_acc.get("api_calls", 0) + 1
                finally:
                    if usage_lock is not None:
                        usage_lock.release()
            if not text.strip():
                raise TranslationError("خروجی CLI بدون متن بود.")
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise TranslationError("پاسخ CLI JSON نبود: " + text[:120])
            parsed = json.loads(m.group(0))
            trans = parsed.get("translations") if isinstance(parsed, dict) else None
            if not isinstance(trans, list):
                raise TranslationError("پاسخ فهرست 'translations' نداشت.")
            if len(trans) != len(segments):
                log(f"هشدار: طول پاسخ ({len(trans)}) با ورودی ({len(segments)}) فرق داشت؛ اصلاح شد.")
                if len(trans) < len(segments):
                    trans = list(trans) + list(segments[len(trans):])
                else:
                    trans = trans[:len(segments)]
            return [str(t) for t in trans]
        except FatalTranslationError:
            raise
        except (TranslationError, subprocess.TimeoutExpired, json.JSONDecodeError,
                KeyError, IndexError) as e:
            last_err = e
            wait = min(4 * attempt, 20)
            log(f"تلاش {attempt}/{max_retries} ناموفق بود ({e})؛ {wait} ثانیه صبر…")
            time.sleep(wait)
    raise TranslationError(f"ترجمهٔ این بخش پس از چند تلاش ناموفق ماند: {last_err}")


# --------------------------------------------------------------------------
# بازسازی PDF
# --------------------------------------------------------------------------


def _build_css(log=print):
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
    scale: ضریب یکنواختِ اندازهٔ فونت (برای هم‌اندازه شدن بلوک‌های صفحه).
    جهت و چینش بر اساس زبان مقصد انتخابی ادمین تعیین می‌شود."""
    esc = html_mod.escape(text).replace("\n", "<br/>")
    rtl = bool(b.get("_rtl", True))
    align = b.get("align", "right")
    if align == "right" and not rtl:
        align = "left"  # زبان مقصد چپ‌به‌راست است
    style = [
        f"font-size:{b['size'] * scale * FONT_SCALE_FACTOR:.2f}px",
        f"line-height:{b.get('leading', MIN_LEADING)}",
        f"text-align:{align}",
        f"color:{b['color']}",
    ]
    if b.get("bold"):
        style.append("font-weight:bold")
    if b.get("italic"):
        style.append("font-style:italic")
    return f'<div dir="{"rtl" if rtl else "ltr"}" style="{";".join(style)}">{esc}</div>'


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
                # raw_lines باید هم‌طولِ line_rects بماند؛ ناهم‌خوانی‌اش بعداً
                # در تفکیک جدول‌ها IndexError «list index out of range» می‌دهد.
                big["raw_lines"] = big.get("raw_lines", []) + small.get("raw_lines", [])
                big["text"] = big["text"] + "\n" + small["text"]
                n = max(len(big["line_rects"]), 1)
                if n > 1 and big["size"] > 0:
                    big["leading"] = min(max(u.height / n / big["size"] * 1.12, MIN_LEADING), MAX_LEADING)
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


def _delete_annotations(page: "pymupdf.Page") -> int:
    """حذف همهٔ حاشیه‌نویسی‌های صفحه (هایلایت‌های قدیمی و…).

    هایلایت‌های اصلی با متن انگلیسی سر جای قبلی‌شان می‌مانند و بعد از ترجمه
    بهم‌ریخته و جابه‌جا دیده می‌شوند؛ به‌جای آن‌ها هایلایت تازه طبق ترجمه گذاشته
    می‌شود. تعداد حذف‌شده‌ها برمی‌گردد."""
    n = 0
    try:
        for a in list(page.annots() or []):
            try:
                page.delete_annot(a)
                n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return n


def _remove_images(page: "pymupdf.Page", bboxes: list["pymupdf.Rect"]) -> None:
    """حذف استقرار تصاویر از صفحه (برای بازدرج در موقعیت آینه‌ای)."""
    for r in bboxes:
        page.add_redact_annot(pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5))
    try:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    except (TypeError, AttributeError):
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE)


def _mirror_rect(r: "pymupdf.Rect", page_w: float) -> "pymupdf.Rect":
    """آینهٔ افقی یک رکت حول محور عمودی وسط صفحه."""
    return pymupdf.Rect(page_w - r.x1, r.y0, page_w - r.x0, r.y1)


def _page_flippable(page: "pymupdf.Page") -> bool:
    """شرط‌های امن برای آینه‌کردن کل محتوای گرافیکی صفحه:
    چرخش صفر و منطبق‌بودن ناحیهٔ دید با MediaBox (تا محور آینه درست باشد)."""
    try:
        if page.rotation != 0:
            return False
        mb = page.mediabox
        pr = page.rect
        return (abs(mb.x0) < 0.01 and abs(mb.y0) < 0.01
                and abs(pr.width - mb.width) < 0.01
                and abs(pr.height - mb.height) < 0.01)
    except Exception:  # noqa: BLE001
        return False


def _flip_page_graphics(doc, page: "pymupdf.Page") -> None:
    """محتوای باقی‌ماندهٔ صفحه (جدول‌ها، کادرها، خطوط و هر ترسیم برداری) را
    حول محور عمودی وسط صفحه آینهٔ افقی می‌کند.

    در این نقطه متن‌ها حذف و تصاویر جمع‌آوری شده‌اند؛ این تابع فقط ترسیم‌های
    برداری را می‌گیرد تا شبکهٔ جدول‌ها و کادرها با متن آینه‌شده هم‌تراز بمانند.
    با پیچیدن کل جریان محتوا در «q + ماتریس آینه + Q» انجام می‌شود.
    """
    page.wrap_contents()  # تضمین توازن q/Q در جریان‌های موجود
    xrefs = page.get_contents()
    if not xrefs:
        return
    e = page.mediabox.x0 + page.mediabox.x1   # x' = -x + e  (آینهٔ افقی)
    cm = f"q\n-1 0 0 1 {e:.3f} 0 cm\n".encode()
    first = doc.xref_stream(xrefs[0])
    doc.update_stream(xrefs[0], cm + first)
    last_xref = xrefs[-1]
    last = doc.xref_stream(last_xref)
    doc.update_stream(last_xref, last + b"\nQ\n")


def _collect_images(page: "pymupdf.Page") -> list[dict]:
    """تصاویر شطرنجیِ مستقر در صفحه با xref معتبر برای جابه‌جایی."""
    imgs: list[dict] = []
    try:
        for info in page.get_image_info(xrefs=True):
            xref = int(info.get("xref", 0) or 0)
            bbox = info.get("bbox")
            if xref > 0 and bbox:
                r = pymupdf.Rect(bbox)
                if not r.is_empty and r.width > 1 and r.height > 1:
                    imgs.append({"xref": xref, "bbox": r})
    except Exception:  # noqa: BLE001
        pass
    return imgs


def _reinsert_images(doc, page: "pymupdf.Page", imgs: list[dict], page_w: float,
                     log=print) -> None:
    """بازدرج تصاویر جمع‌آوری‌شده در موقعیت آینه‌ای — باید بعد از _flip_page_graphics
    اجرا شود تا جریان جدیدِ تصویر زیر ماتریس آینه نرود و محتوایش برعکس نشود."""
    moved = 0
    for im in imgs:
        mr = _mirror_rect(im["bbox"], page_w)
        if mr.is_empty or mr.width <= 1 or mr.height <= 1:
            continue
        try:
            page.insert_image(mr, xref=im["xref"])  # ارجاع به همان شیء (با شفافیت)
            moved += 1
        except Exception:  # noqa: BLE001
            try:
                raw = doc.extract_image(im["xref"])
                page.insert_image(mr, stream=raw["image"])
                moved += 1
            except Exception:  # noqa: BLE001
                pass
    if moved:
        log(f"{moved} تصویر به موقعیت آینه‌ای منتقل شد.")


# پیش‌تنظیم کیفیت تصویر خروجی — «small» بر اساس درخواست کاربر پیش‌فرض است
_IMGQ_PRESETS: dict[str, dict | None] = {
    "small": {"dpi_threshold": 110, "dpi_target": 96, "quality": 60},
    "balanced": {"dpi_threshold": 150, "dpi_target": 120, "quality": 70},
    "high": None,
}


def _finalize_save(doc, out_path, log=print, img_quality: str = "small") -> None:
    """ذخیرهٔ نهایی با فشرده‌سازی کامل برای کاهش حجم فایل خروجی:

      ۱) subset_fonts — فقط نویسه‌های استفاده‌شده از فونت فارسی نگه داشته می‌شود
      ۲) rewrite_images — تصاویر (حتی بدون‌افت DPI مثل PNG اسکرین‌شات) به JPEG
         با کیفیت/ resolusion تنظیم‌شده بازفشرده می‌شوند؛ «small» تهاجمی‌ترین است
      ۳) save با garbage=4/clean — اشیای بلااستفاده و نسخه‌های تکراری حذف می‌شوند
    """
    try:
        doc.subset_fonts()
        log("فشرده‌سازی فونت‌ها انجام شد.")
    except Exception as e:  # noqa: BLE001
        log(f"فشرده‌سازی فونت ناموفق بود: {e}")
    p = _IMGQ_PRESETS.get(img_quality, _IMGQ_PRESETS["small"])
    if p:
        try:
            doc.rewrite_images(dpi_threshold=p["dpi_threshold"],
                               dpi_target=p["dpi_target"], quality=p["quality"],
                               lossy=True, lossless=True, bitonal=True,
                               color=True, gray=True)
            log(f"بازفشرده‌سازی تصاویر انجام شد (کیفیت: {img_quality}).")
        except TypeError:
            try:  # نسخه‌های قدیمی‌تر pymupdf بدون پارامترهای تفصیلی
                doc.rewrite_images(dpi_threshold=p["dpi_threshold"],
                                   dpi_target=p["dpi_target"], quality=p["quality"])
                log("بازفشرده‌سازی تصاویر انجام شد (حالت سازگار).")
            except Exception as e:  # noqa: BLE001
                log(f"بازفشرده‌سازی تصاویر ناموفق بود: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"بازفشرده‌سازی تصاویر ناموفق بود: {e}")
    doc.save(str(out_path), garbage=4, deflate=True, deflate_images=True,
             deflate_fonts=True, clean=True)


def _fit_rect(b: dict, rect: "pymupdf.Rect", page_rect: "pymupdf.Rect",
              siblings: list["pymupdf.Rect"] | None = None) -> "pymupdf.Rect":
    """کادرهای خیلی باریک (مثل شمارهٔ صفحه) را برای جای‌دادن متن فارسی گسترش می‌دهد،
    بدون جابه‌جا کردن لبهٔ لنگرِ چینش. اگر گسترش به بلوک مجاور (مثل سلول کناری
    جدول یا خط شبکهٔ آن) سرریز شود، گسترش لغو می‌شود تا متن از خانه بیرون نزند."""
    if rect.width <= 0 or rect.width >= b["size"] * 3:
        return rect
    extra = b["size"] * 6
    if b["align"] == "center":
        nr = pymupdf.Rect(rect.x0 - extra, rect.y0, rect.x1 + extra, rect.y1)
    else:  # راست‌چین: لبهٔ راست ثابت می‌ماند و به سمت چپ گسترش می‌یابد
        nr = pymupdf.Rect(rect.x0 - extra, rect.y0, rect.x1, rect.y1)
    nr.x0 = max(nr.x0, page_rect.x0 + 2)
    nr.x1 = min(nr.x1, page_rect.x1 - 2)
    for s in (siblings or []):
        try:
            if nr.intersects(s):
                return rect  # سرریز به همسایه — همان کادر باریک می‌ماند (متن کوچک می‌شود)
        except Exception:  # noqa: BLE001
            continue
    return nr


def translate_pdf(input_pdf, output_pdf, api_key, *, model=DEFAULT_MODEL, delay=None,
                  progress=None, cancelled=None, log=print, translator=None,
                  mirror=True, base_url="", workers=1, spacing=DEFAULT_SPACING,
                  src_lang="auto", dst_lang="fa", use_cli=False,
                  oc_bin="opencode", oc_cwd=None,
                  start_page: int = 0, end_page: int | None = None,
                  extract_progress=None,
                  page_mode: str = "auto", ocr_langs: str = "",
                  ocr_workers: int = 3, img_quality: str = "small",
                  plain_save: bool = False) -> dict:
    """
    ترجمهٔ کامل فایل PDF به زبان مقصد با حفظ چیدمان.

    src_lang/dst_lang: زبان مبدأ و مقصد (پیش‌فرض: خودکار → فارسی).
    mirror: آینه‌سازی افقی چیدمان — فقط وقتی زبان مقصد راست‌به‌چپ است معنی دارد.
    use_cli: ترجمه با اجرای CLI اوپن‌کد (مسیر اصلی سرویس Zen؛ ردهٔ رایگان فقط
    از داخل خودِ OpenCode جواب می‌دهد). api_key در این حالت استفاده نمی‌شود.
    workers: تعداد ترجمه‌های موازی؛ spacing حداقل فاصلهٔ شروع درخواست‌ها/فراخوانی‌ها.
    start_page/end_page: بازهٔ صفحات (۰مبنا؛ انتهای بازه ساختارِ باز) — برای ترجمهٔ
    پارت‌به‌پارت و ادامه از چک‌پوینت؛ پیش‌فرض کل فایل.
    page_mode: "text" فقط لایهٔ متن، "image" فقط OCR (اسکن/اسکرین‌شات/کامیک)،
    "auto" — اول متن؛ اگر صفحه متن نداشت OCR.
    img_quality: فشرده‌سازی تصاویر خروجی — "small" (پیش‌فرض، تهاجمی)،
    "balanced"، "high" (بدون بازفشرده‌سازی).
    translator: برای تست — تابعی (segments -> translations) بدون اینترنت.
    خروجی: دیکشنری آمار شامل usage (مصرف توکن ورودی/خروجی/مجموع و تعداد درخواست‌ها).
    """
    progress = progress or (lambda done, total: None)
    extract_progress = extract_progress or (lambda done, total: None)
    if delay is None:
        delay = DEFAULT_DELAY
    t0 = time.time()
    ensure_fonts(log)
    rtl = dst_lang in RTL_LANGS
    mirror = mirror and rtl

    in_path = Path(input_pdf)
    if not in_path.exists():
        raise FileNotFoundError(f"فایل ورودی پیدا نشد: {in_path}")
    out_path = Path(output_pdf)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".part.pdf")

    doc = pymupdf.open(str(in_path))
    total_all = doc.page_count
    p_start = max(0, min(int(start_page), total_all - 1))
    p_end = total_all if end_page is None else max(p_start + 1, min(int(end_page), total_all))
    total = p_end - p_start
    if p_start > 0:
        log(f"ادامه از صفحهٔ {p_start + 1} تا {p_end} از مجموع {total_all} صفحه.")
    log(f"فایل «{in_path.name}» با {total_all} صفحه باز شد. مدل: {model}")
    arch, css = _build_css(log)

    usage: dict = {}
    usage_lock = threading.Lock()
    limiter = _RateLimiter(spacing if workers > 1 else 0.0)
    stats = {"pages": total_all, "range_start": p_start, "range_end": p_end,
             "translated": 0, "no_text_pages": 0, "ocr_pages": 0,
             "failed_pages": [], "skipped_pages": [],
             "hl_applied": 0, "hl_dropped": 0, "annots_removed": 0,
             "output": str(out_path), "cancelled": False,
             "usage": usage}

    _PURE_NUM_RE = re.compile(r"^[\d\s.,;:\-–—/()%#]+$")

    def _call_batch(batch):
        """فراخوانی سرویس ترجمه برای یک دسته؛ فاصله‌گذار نرخ بین ورکرها مشترک است."""
        limiter.wait()
        segs_b = [t for _, t in batch]
        if use_cli:
            return oc_translate_segments(model, segs_b, src_lang=src_lang, dst_lang=dst_lang,
                                         log=log, oc_bin=oc_bin, oc_cwd=oc_cwd,
                                         usage_acc=usage, usage_lock=usage_lock)
        return api_translate_segments(
            api_key, model, segs_b, log=log, base_url=base_url,
            src_lang=src_lang, dst_lang=dst_lang,
            usage_acc=usage, usage_lock=usage_lock)

    # مدارشکن: شمارش شکست‌های پشت‌سرهمِ دسته‌ها؛ اگر سرویس مرده باشد به‌جای
    # سوزاندن ساعت‌ها و ساختن ده‌ها صفحهٔ خالی/بدون ترجمه، کل اجرا متوقف می‌شود
    consec_fail = {"n": 0}

    def _call_batch_guarded(batch):
        """فراخوانی بچ با یک تلاش مجدد بیرونی + مدارشکن قطع کامل سرویس."""
        for att in range(2):
            try:
                outs = _call_batch(batch)
                consec_fail["n"] = 0
                return outs
            except FatalTranslationError:
                raise
            except Exception as e:  # noqa: BLE001
                if att == 0:
                    log(f"دستهٔ ترجمه ناموفق بود ({e})؛ ۲۰ ثانیه صبر و تلاش دوباره…")
                    time.sleep(20.0)
                else:
                    consec_fail["n"] += 1
                    if consec_fail["n"] >= 8:
                        raise FatalTranslationError(
                            "سرویس ترجمه ۸ دستهٔ پشت‌سرهم را بی‌پاسخ گذاشت؛ اجرا متوقف شد "
                            "تا چک‌پوینت گرفته شود و چرخهٔ بعد ادامه دهد.", code="provider")
                    raise

    pool = None
    if workers > 1 and translator is None:
        pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="tr")
        log(f"ترجمهٔ موازی فعال شد: {workers} ورکر، فاصلهٔ شروع درخواست‌ها {spacing:.1f} ثانیه.")

    # کارهای صفحه: دیکشنری {pno, blocks, segs, trans, tasks, fut}
    # fut: فاتور OCR برای صفحات تصویری (قبل از فاز ۲ حل می‌شود)
    page_jobs: list[dict] = []

    # کش سگمنت‌های ترجمه‌شدهٔ همین اجرا — سرصفحه/پاصفحه و جمله‌های تکراری کتاب‌ها
    # دوباره ترجمه نمی‌شوند (سرعت به‌طور محسوس بالاتر).
    # seg_cache: نتیجهٔ نهایی؛ pending: سگمنت در-flight → (Future، اندیس در خروجی بچ)
    seg_cache: dict[str, str] = {}
    pending: dict[str, tuple] = {}
    cache_lock = threading.Lock()

    # OCR: فقط وقتی لازم است (حالت عکس‌محور/خودکار) و tesseract در دسترس باشد
    ocr_pool: ThreadPoolExecutor | None = None
    tess_langs = (ocr_langs or "").strip() or tess_lang_for(src_lang)
    want_ocr = page_mode in ("auto", "image") and ocr_available()
    if page_mode == "image" and not ocr_available():
        log("هشدار: tesseract نصب نیست؛ حالت عکس‌محور به حالت خودکار برمی‌گردد.")
    if want_ocr and page_mode == "image":
        log(f"حالت عکس‌محور: متن صفحات با OCR ({tess_langs}) استخراج می‌شود…")
    if want_ocr:
        ocr_pool = ThreadPoolExecutor(max_workers=max(1, min(ocr_workers, 4)),
                                      thread_name_prefix="ocr")

    def _enqueue_page(pno: int, blocks: list[dict]) -> dict:
        """ساخت کار صفحه: کش/اعداد خالص حذف و بچ‌های ترجمه صف می‌شوند.
        سگمنت تکراری (مثل سرصفحهٔ یکسان صفحات) اگر بچ در-flight یا نتیجه‌شده
        داشته باشد، دوباره ارسال نمی‌شود و از همان نتیجه کپی می‌شود (xrefs)."""
        segs = [b["text"] for b in blocks]
        trans = [""] * len(segs)
        items = []
        xrefs = []   # (اندیس در صفحه، Future بچ دیگر، اندیس در خروجی آن بچ)
        for i, s in enumerate(segs):
            st = s.strip()
            if not st:
                continue
            if _PURE_NUM_RE.match(st):
                trans[i] = s  # اعداد و علائم خالص (مثل شمارهٔ صفحه) نیازی به ترجمه ندارند
                continue
            with cache_lock:
                hit = seg_cache.get(st)
                pend = pending.get(st)
            if hit is not None:
                trans[i] = hit
                continue
            if pend is not None:
                xrefs.append((i, pend[0], pend[1]))
                continue
            items.append((i, s))

        if translator is not None:
            # حالت تست: مترجم موک — از همان مسیر کش می‌گذرد تا تست‌ها
            # دقیقاً رفتار مسیر واقعی را ببینند.
            got = [str(t) for t in translator([s for _, s in items])]
            got = got + [""] * max(len(items) - len(got), 0)
            for (i, s), tr in zip(items, got):
                trans[i] = tr
                if s and len(s) > 6:
                    with cache_lock:
                        seg_cache[s.strip()] = tr
            return {"pno": pno, "blocks": blocks, "segs": segs, "trans": trans,
                    "tasks": [], "xrefs": xrefs}

        tasks = []
        for batch in _batch_items(items):
            fut = pool.submit(_call_batch_guarded, batch) if pool is not None else None
            for out_idx, (_i, s) in enumerate(batch):
                with cache_lock:
                    pending.setdefault(s.strip(), (fut, out_idx))
            tasks.append((batch, fut))
        return {"pno": pno, "blocks": blocks, "segs": segs, "trans": trans,
                "tasks": tasks, "xrefs": xrefs}

    try:
        # ── فاز ۱: استخراج صفحات و صف‌کردن بچ‌های ترجمه؛ ترجمهٔ صفحات بعدی
        #    در پس‌زمینه به‌موازات بازسازی صفحات قبلی پیش می‌رود.
        #    هر صفحه در گارد جداگانه است تا یک صفحهٔ خراب/عجیب PDF کل فایل
        #    چندصفحه‌ای را با خطای «list index out of range» نکشد.
        for pno in range(p_start, p_end):
            if cancelled is not None and cancelled.is_set():
                stats["cancelled"] = True
                log("لغو شد؛ پیشرفت فعلی ذخیره می‌شود…")
                break
            try:
                page = doc[pno]
                # هایلایت‌های صفحه باید قبل از هر تغییری خوانده شوند
                hl = _page_highlights(page)
                blocks = extract_blocks(page)
                if page_mode == "image" and ocr_pool is not None:
                    # کاربر گفته عکس‌محور است؛ OCR اولویت دارد (لایهٔ متن، پشتیبان می‌ماند)
                    fut = ocr_pool.submit(ocr_page_blocks, page, tess_langs, 200, log)
                    page_jobs.append({"pno": pno, "fut": fut, "text_fallback": blocks})
                    continue
                if not blocks and page_mode == "auto" and ocr_pool is not None:
                    fut = ocr_pool.submit(ocr_page_blocks, page, tess_langs, 200, log)
                    page_jobs.append({"pno": pno, "fut": fut, "text_fallback": [], "hl": hl})
                    continue
                if not blocks:
                    stats["no_text_pages"] += 1
                    log(f"صفحهٔ {pno + 1}: متن قابل استخراج نداشت (تصویری است؟) — بدون تغییر کپی شد.")
                    page_jobs.append({"pno": pno, "blocks": [], "segs": [], "trans": [], "tasks": []})
                    continue
                # جلوگیری از «متن روی متن»: بلوک‌های همپوشان ادغام می‌شوند
                blocks = _merge_overlapping_blocks(blocks)
                # جدول‌ها: هر سلول یک بلوک مستقل تا ترجمه از خانه بیرون نزند
                blocks = _split_blocks_by_tables(page, blocks, log=log)
                # هایلایت‌های صفحه: بلوک‌ها در مرز خط‌های هایلایت‌شده می‌شکنند تا
                # هایلایت تازه دقیقاً روی ترجمهٔ همان بخش بنشیند
                n_hl = n_drop = 0
                if hl:
                    blocks, n_hl, n_drop = _split_blocks_by_highlights(blocks, hl, log=log)
                    stats["hl_applied"] += n_hl
                    stats["hl_dropped"] += n_drop
                # جهت متن (راست‌چین/چپ‌چین) بر اساس زبان مقصد
                for b in blocks:
                    b["_rtl"] = rtl
                pj = _enqueue_page(pno, blocks)
                pj["hl"] = hl
                page_jobs.append(pj)
            except FatalTranslationError:
                raise
            except Exception as e:  # noqa: BLE001
                # صفحهٔ مشکل‌دار بدون ترجمه کپی می‌شود؛ ادامهٔ فایل زنده می‌ماند
                stats["skipped_pages"].append(pno + 1)
                log(f"صفحهٔ {pno + 1}: خطا در استخراج متن ({e}) — بدون تغییر کپی شد.")
                log(traceback.format_exc())
                page_jobs.append({"pno": pno, "blocks": [], "segs": [], "trans": [], "tasks": []})
                continue
            finally:
                extract_progress(pno - p_start + 1, total)

        # ── فاز ۱٫۵: حل نتایج OCR صفحات تصویری
        resolved: list[dict] = []
        for pj in page_jobs:
            if pj.get("fut") is None:
                resolved.append(pj)
                continue
            pno = pj["pno"]
            try:
                oblocks = pj["fut"].result()
            except Exception as e:  # noqa: BLE001
                log(f"صفحهٔ {pno + 1}: خطای OCR ({e}) — بدون تغییر کپی می‌شود.")
                oblocks = []
            if oblocks:
                stats["ocr_pages"] += 1
                for b in oblocks:
                    b["_rtl"] = rtl
                pj2 = _enqueue_page(pno, oblocks)
                pj2["hl"] = pj.get("hl") or []
                resolved.append(pj2)
            elif pj.get("text_fallback"):
                resolved.append(_enqueue_page(pno, pj["text_fallback"]))
            else:
                stats["no_text_pages"] += 1
                log(f"صفحهٔ {pno + 1}: نه متن قابل استخراج داشت و نه OCR چیزی پیدا کرد — کپی شد.")
                resolved.append({"pno": pno, "blocks": [], "segs": [], "trans": [], "tasks": []})
        page_jobs = resolved

        # ── فاز ۲: بازسازی صفحات به ترتیب؛ نتیجهٔ هر بچ گرفته و درج می‌شود
        for pj in page_jobs:
            pno, blocks, segs, trans, tasks = (pj["pno"], pj["blocks"], pj["segs"],
                                               pj["trans"], pj["tasks"])
            if cancelled is not None and cancelled.is_set():
                stats["cancelled"] = True
                log("لغو شد؛ پیشرفت فعلی ذخیره می‌شود…")
                break

            if not blocks:
                progress(pno - p_start + 1, total)
                continue

            page = doc[pno]
            # کل بازسازی صفحه در برابر خطاهای غیرمنتظره مقاوم شده تا یک صفحهٔ ناساز
            # هیچ‌وقت فایل چندصفحه‌ای (تا ۲۰۰۰ صفحه) را نیمه‌کاره نکشد.
            try:
                # نتیجهٔ بچ‌های ترجمهٔ همین صفحه گرفته می‌شود
                for batch, fut in tasks:
                    try:
                        outs = fut.result() if fut is not None else _call_batch_guarded(batch)
                    except FatalTranslationError:
                        raise
                    except TranslationError as e:
                        stats["failed_pages"].append(pno + 1)
                        log(f"صفحهٔ {pno + 1}: {e} — متن اصلیِ بلوک‌های این بخش حفظ می‌شود "
                            "(صفحه خالی نمی‌شود).")
                        outs = None
                    except Exception as e:  # noqa: BLE001
                        stats["failed_pages"].append(pno + 1)
                        log(f"صفحهٔ {pno + 1}: خطای غیرمنتظره در ترجمهٔ بخش ({e}) — "
                            "متن اصلیِ بلوک‌های این بخش حفظ می‌شود.")
                        log(traceback.format_exc())
                        outs = None
                    if outs is not None:
                        for (idx, src_seg), tr in zip(batch, outs):
                            trans[idx] = tr
                            if src_seg and len(src_seg) > 6:
                                with cache_lock:
                                    seg_cache[src_seg.strip()] = tr
                # سگمنت‌های مشترک با بچ صفحات قبل (سرصفحهٔ تکراری و…)
                for i, xf, out_idx in pj.get("xrefs") or []:
                    try:
                        xouts = xf.result() if xf is not None else None
                    except Exception:  # noqa: BLE001
                        xouts = None
                    if xouts is not None and out_idx < len(xouts):
                        trans[i] = xouts[out_idx]
                        continue
                    # بچ بدون پول (حالت ترتیبی) — نتیجه تا این لحظه در کش است
                    with cache_lock:
                        hit = seg_cache.get((segs[i] or "").strip())
                    if hit is not None:
                        trans[i] = hit

                ocr_page = all(b.get("_ocr") for b in blocks)

                # ۰) حذف هایلایت‌ها و حاشیه‌نویسی‌های اصلی — هایلایت تازه طبق ترجمه گذاشته می‌شود
                stats["annots_removed"] += _delete_annotations(page)

                has_trans = [bool((trans[i] or "").strip()) for i in range(len(blocks))]

                # ۱) حذف متن اصلی فقط برای بلوک‌هایی که ترجمه دارند؛
                #    بلوک بدون ترجمه (خطای سرویس) متن اصلی‌اش حفظ می‌شود تا صفحه خالی نشود
                if ocr_page:
                    # متن داخل تصویر است: فشرده‌سازی شدید صفحه + پوشش هم‌رنگِ محل متن
                    if img_quality != "high":
                        _rasterize_page(page, 100 if img_quality == "small" else 120,
                                        60 if img_quality == "small" else 70, log)
                    for i, b in enumerate(blocks):
                        if not has_trans[i]:
                            continue
                        fr = pymupdf.Rect(b["bbox"].x0 - 1.0, b["bbox"].y0 - 1.0,
                                          b["bbox"].x1 + 1.0, b["bbox"].y1 + 1.0)
                        fr.intersect(page.rect)
                        if fr.is_empty or fr.width <= 1 or fr.height <= 1:
                            continue
                        try:
                            page.draw_rect(fr, color=None, fill=b.get("_fill") or (1, 1, 1))
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    # حذف متن اصلی صفحه (تصاویر و گرافیک دست‌نخورده می‌مانند)
                    for i, b in enumerate(blocks):
                        if not has_trans[i]:
                            continue
                        for r in b["line_rects"]:
                            pr = pymupdf.Rect(r.x0 - 0.5, r.y0 - 0.5, r.x1 + 0.5, r.y1 + 0.5)
                            page.add_redact_annot(pr, fill=False)
                    _apply_redactions(page)

                # ۲) کادرهای هدف: با آینه‌سازی افقی، متن چپ‌چین انگلیسی به موقعیت متقارنِ راست می‌رود
                #    (صفحات عکس‌محور/OCR قابل آینه‌شدن نیستند — تصویر سر جایش می‌ماند)
                page_w = page.rect.width
                page_mirror = mirror and not ocr_page
                rects = []
                for b in blocks:
                    r = b["bbox"]
                    if page_mirror:
                        r = _mirror_rect(b["bbox"], page_w)
                    rects.append(r)

                # ۲٫۵) آینه‌سازی محتوای گرافیکی: جدول‌ها/کادرها/خطوط با متن هم‌تراز می‌شوند و
                #      تصاویر به موقعیت آینه‌ای منتقل می‌شوند (بدون برعکس‌شدن محتوای خودشان)
                if page_mirror and _page_flippable(page):
                    try:
                        imgs = _collect_images(page)
                        if imgs:
                            # حذف استقرار فعلی (با مختصات اصلی، قبل از فلیپ)
                            _remove_images(page, [im["bbox"] for im in imgs])
                        # فلیپ فقط ترسیم‌های برداری باقی‌مانده را آینه می‌کند
                        _flip_page_graphics(doc, page)
                        # بازدرج تصاویر بعد از فلیپ → زیر ماتریس آینه نمی‌روند
                        _reinsert_images(doc, page, imgs, page_w, log=log)
                    except Exception as e:  # noqa: BLE001
                        log(f"صفحهٔ {pno + 1}: آینه‌سازی گرافیک ناموفق بود: {e}")
                    # لینک‌ها هم به موقعیت آینه‌ای می‌روند
                    try:
                        for lnk in page.get_links():
                            fr = lnk.get("from")
                            if fr is not None:
                                lnk["from"] = _mirror_rect(fr, page_w)
                                page.update_link(lnk)
                    except Exception:  # noqa: BLE001
                        pass

                # ۳) مقیاس یکنواخت صفحه: کمترین مقیاسی که همهٔ پاراگراف‌ها جا شوند؛
                #    همهٔ بلوک‌ها با همان ضریب کوچک می‌شوند تا اندازه‌ها یکنواخت بماند
                scales = {}
                for i, (b, t) in enumerate(zip(blocks, trans)):
                    if (t or "").strip():
                        scales[i] = _measure_scale(t, b, rects[i], css, arch)
                para_scales = [scales[i] for i, b in enumerate(blocks)
                               if i in scales and len(b["line_rects"]) > 1]
                page_scale = max(min(para_scales), 0.62) if para_scales else 1.0
                page_scale = min(page_scale, 1.0)

                # ۴) درج ترجمهٔ فارسی؛ برای بلوک‌های هایلایت‌دار بعد از درج،
                #    هایلایت با اندازهٔ واقعی متن روی همان ناحیه کشیده می‌شود
                for i, (b, t) in enumerate(zip(blocks, trans)):
                    t = (t or "").strip()
                    if not t:
                        continue
                    s_own = scales.get(i, 1.0)
                    use = page_scale if s_own >= page_scale else s_own
                    if s_own < page_scale:
                        log(f"صفحهٔ {pno + 1}: یک بلوک با مقیاس {int(s_own * 100)}٪ درج شد "
                            f"(در مقیاس یکنواختِ {int(page_scale * 100)}٪ جا نشد).")
                    rect = _fit_rect(b, rects[i], page.rect,
                                     siblings=[rects[j] for j in range(len(rects)) if j != i])
                    if rect.is_empty or rect.width <= 2 or rect.height <= 2:
                        continue
                    try:
                        _spare, shrink = page.insert_htmlbox(
                            rect, _block_html(t, b, use), css=css, archive=arch)
                        if shrink and shrink < 0.85:
                            log(f"صفحهٔ {pno + 1}: یک بلوک برای جا شدن به {int(shrink * 100)}٪ کوچک شد.")
                    except Exception as e:  # noqa: BLE001
                        log(f"صفحهٔ {pno + 1}: درج متن ناموفق بود: {e}")
                        _spare = None
                    if b.get("_hl"):
                        # هایلایت تازه — بعد از درج متن می‌کشیم تا اندازه‌اش دقیقاً
                        # به‌اندازهٔ متن واقعیِ درج‌شده باشد (نه کل کادرِ اصلی)؛
                        # نیمه‌شفاف است تا مثل ماژیکِ هایلایت، متن زیرش خوانا بماند.
                        try:
                            used_h = rect.height
                            try:
                                spare_f = float(_spare) if _spare is not None else None
                            except Exception:  # noqa: BLE001
                                spare_f = None
                            if spare_f is not None and spare_f >= 0:
                                used_h = min(max(rect.height - spare_f, 6.0), rect.height)
                            hr = pymupdf.Rect(rect.x0 - 1.2, rect.y0 - 1.0,
                                              rect.x1 + 1.2, rect.y0 + used_h + 1.0)
                            hr.intersect(page.rect)
                            if not hr.is_empty and hr.width > 1 and hr.height > 1:
                                page.draw_rect(hr, color=None,
                                               fill=b.get("_hl_color") or HL_DEFAULT_COLOR,
                                               fill_opacity=0.45)
                        except Exception:  # noqa: BLE001
                            pass

                stats["translated"] += 1
            except FatalTranslationError:
                raise
            except Exception as e:  # noqa: BLE001
                # صفحهٔ مشکل‌دار رد می‌شود (متن اصلی می‌ماند) ولی ادامهٔ فایل زنده می‌ماند
                stats["failed_pages"].append(pno + 1)
                log(f"صفحهٔ {pno + 1}: خطای غیرمنتظره در بازسازی ({e}) — "
                    "این صفحه بدون ترجمه کپی می‌شود؛ ادامهٔ فایل زنده است.")
                log(traceback.format_exc())
            progress(pno - p_start + 1, total)

            # ذخیرهٔ موقت هر ۲۵ صفحه (برای جلوگیری از از دست رفتن پیشرفت)
            if (pno + 1 - p_start) % 25 == 0 and pno + 1 < p_end:
                try:
                    doc.save(str(tmp_path), garbage=3, deflate=True)
                    log(f"ذخیرهٔ موقت انجام شد ({pno + 1} از {p_end}).")
                except Exception as e:  # noqa: BLE001
                    log(f"ذخیرهٔ موقت ناموفق بود (ادامه می‌دهیم): {e}")

            if pool is None and translator is None and delay and pno + 1 < p_end:
                time.sleep(delay)
    finally:
        # بستن استخرها: کارهای در صف لغو می‌شوند؛ حداکثر «workers» درخواستِ در جریان تمام می‌شود
        if ocr_pool is not None:
            ocr_pool.shutdown(wait=False, cancel_futures=True)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # اگر سرویس هیچ صفحه‌ای را ترجمه نکرد (موج قطعی 5xx/قطعیِ کامل)، به‌جای
    # تحویل فایلِ دست‌نخورده با ظاهرِ موفقیت، خطای فیلورورِ ربات پرتاب می‌شود.
    if not stats["cancelled"] and stats["translated"] == 0 and stats["failed_pages"]:
        raise FatalTranslationError(
            "هیچ صفحه‌ای ترجمه نشد؛ سرویس ترجمه پاسخگو نبود.", code="provider")

    if stats["cancelled"]:
        try:
            _finalize_save(doc, tmp_path, log, img_quality)
        except Exception as e:  # noqa: BLE001
            log(f"ذخیرهٔ نهایی ناتمام ناموفق بود ({e})؛ ذخیرهٔ ساده…")
            try:
                doc.save(str(tmp_path), garbage=1, deflate=True)
            except Exception as e2:  # noqa: BLE001
                log(f"ذخیرهٔ ساده هم ناموفق بود: {e2}")
        stats["output"] = str(tmp_path)
    else:
        try:
            if plain_save:
                # حالت فصل‌به‌فصل: فشرده‌سازی سنگین (بازفشرده‌سازی تصاویر/زیرمجموعهٔ فونت)
                # فقط یک‌بار در مونتاژ نهایی انجام می‌شود — نه در هر فصل (افت کیفیت تجمعی ندارد)
                doc.save(str(out_path), garbage=3, deflate=True)
            else:
                _finalize_save(doc, out_path, log, img_quality)
        except Exception as e:  # noqa: BLE001
            log(f"ذخیرهٔ نهایی ناموفق بود ({e})؛ ذخیرهٔ ساده…")
            try:
                doc.save(str(out_path), garbage=1, deflate=True)
            except Exception as e2:  # noqa: BLE001
                log(f"ذخیرهٔ ساده هم ناموفق بود: {e2}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    doc.close()

    dt = time.time() - t0
    ocr_note = f" | OCR: {stats['ocr_pages']} صفحه" if stats.get("ocr_pages") else ""
    hl_note = ""
    if stats.get("hl_applied") or stats.get("hl_dropped"):
        hl_note = (f" | هایلایت: {stats['hl_applied']} بخش منتقل شد"
                   + (f"، {stats['hl_dropped']} حذف شد" if stats.get("hl_dropped") else ""))
    log(f"پایان: {stats['translated']} از {total} صفحه در {dt:.0f} ثانیه پردازش شد{ocr_note}{hl_note}. "
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
    ap.add_argument("--src-lang", default="auto", help="زبان مبدأ (auto یا کد زبان)")
    ap.add_argument("--dst-lang", default="fa", help="زبان مقصد (پیش‌فرض fa)")
    ap.add_argument("--use-cli", action="store_true",
                    help="ترجمه با CLI اوپن‌کد به‌جای وب‌سرویس سازگار OpenAI")
    ap.add_argument("--oc-bin", default="opencode", help="مسیر باینری opencode")
    ap.add_argument("--page-mode", default="auto", choices=["auto", "text", "image"],
                    help="حالت صفحه: text (لایهٔ متن)، image (OCR)، auto")
    ap.add_argument("--img-quality", default="small", choices=["small", "balanced", "high"],
                    help="کیفیت/حجم تصاویر خروجی")
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
                  src_lang=a.src_lang, dst_lang=a.dst_lang,
                  use_cli=a.use_cli, oc_bin=a.oc_bin,
                  page_mode=a.page_mode, img_quality=a.img_quality,
                  progress=lambda d, t: print(f"صفحه {d}/{t}", end="\r", flush=True),
                  log=print)
    print()
