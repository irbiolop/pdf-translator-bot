#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ذخیره‌سازی چک‌پوینت‌های ترجمه بین چرخه‌های GitHub Actions
---------------------------------------------------------
فایل‌های بزرگ (PDF ورودی + خروجی ناتمام) نمی‌توانند در گیت کامیت شوند؛
پس بین چرخه‌ها روی «Release asset» ذخیره می‌شوند:

  • هر چک‌پوینت = یک release موقت با تگ یکتا (cp-<hash>-in / cp-<hash>-pp)
  • سقف هر asset ~۲GB است (هم‌سقف MTProto) و منقضی نمی‌شود
  • آپلود/دانلود استریمی است (فایل چندصد مگابایتی حافظه را نمی‌گیرد)
  • آپلودِ تگِ تکراری = جایگزینی کامل (release قبلی پاک می‌شود)

قبلاً از Artifact API استفاده می‌شد که دو ایراد داشت: آپلود zip در حافظه
و وابستگی به run جاری — برای فایل‌های سنگین کند و شکست‌پذیر بود.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import requests

_CHUNK = 1024 * 1024


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


def _api(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}"


def anon_tag(jobkey: str, kind: str) -> str:
    """تگ یکتا و بدون آیدی کاربر برای ریپوی عمومی: cp-<sha1[:16]>-in/-pp"""
    h = hashlib.sha1(str(jobkey).encode("utf-8")).hexdigest()[:16]
    return f"cp-{h}-{kind}"


def _delete_release_by_tag(repo: str, token: str, tag: str) -> None:
    api = _api(repo)
    try:
        r = requests.get(f"{api}/releases/tags/{tag}", headers=_hdr(token), timeout=30)
        if r.status_code == 200:
            requests.delete(f"{api}/releases/{r.json()['id']}",
                            headers=_hdr(token), timeout=30)
            time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass
    try:  # تگ یتیم هم پاک شود
        requests.delete(f"{api}/git/refs/tags/{tag}", headers=_hdr(token), timeout=30)
    except Exception:  # noqa: BLE001
        pass


def upload_release_file(repo: str, token: str, tag: str, fname: str, src: Path,
                        *, branch: str = "main", log=print) -> str:
    """آپلود یک فایل به‌عنوان asset یک release با تگ داده‌شده (جایگزینی کامل).
    مرجع ذخیره‌شده «rel:<tag>» است. نام asset = نام فایل (in.pdf / partial.pdf)."""
    src = Path(src)
    if not src.exists() or src.stat().st_size == 0:
        raise RuntimeError(f"فایل برای آپلود پیدا نشد: {src}")
    api = _api(repo)
    _delete_release_by_tag(repo, token, tag)
    r = requests.post(f"{api}/releases", headers=_hdr(token), timeout=30, json={
        "tag_name": tag, "target_commitish": branch, "name": tag,
        "body": "checkpoint (auto)", "prerelease": True})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"release create {r.status_code}: {r.text[:160]}")
    rel_id = r.json()["id"]
    size = src.stat().st_size
    with open(src, "rb") as fh:  # آپلود استریمی — حافظه را اشغال نمی‌کند
        r2 = requests.post(
            f"https://uploads.github.com/repos/{repo}/releases/{rel_id}/assets"
            f"?name={fname}",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/octet-stream",
                     "Content-Length": str(size)},
            data=fh, timeout=7200)
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"release asset upload {r2.status_code}: {r2.text[:160]}")
    log(f"چک‌پوینت آپلود شد: {tag}/{fname} ({size / 1e6:.1f}MB).")
    return f"rel:{tag}"


def download_release_file(repo: str, token: str, ref: str, dest_dir: Path,
                          *, fname: str = "", log=print) -> bool:
    """دانلود استریمی asset از مرجع «rel:<tag>» به dest_dir/<نام asset>."""
    if not ref.startswith("rel:"):
        return False
    tag = ref.split(":", 1)[1]
    dest_dir = Path(dest_dir)
    try:
        r = requests.get(f"{_api(repo)}/releases/tags/{tag}", headers=_hdr(token),
                         timeout=30)
        if r.status_code != 200:
            log(f"release {tag} پیدا نشد ({r.status_code}).")
            return False
        assets = r.json().get("assets") or []
        if fname:
            assets = [a for a in assets if a.get("name") == fname]
        if not assets:
            log(f"asset در release {tag} نبود.")
            return False
        a = assets[0]
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / (a.get("name") or fname or "checkpoint.bin")
        tmp = out.with_suffix(out.suffix + ".part")
        r2 = requests.get(f"{_api(repo)}/releases/assets/{a['id']}",
                          headers={**_hdr(token), "Accept": "application/octet-stream"},
                          stream=True, timeout=7200)
        if r2.status_code != 200:
            log(f"دانلود asset ناموفق ({r2.status_code}).")
            return False
        n = 0
        with open(tmp, "wb") as fh:
            for chunk in r2.iter_content(chunk_size=_CHUNK):
                if chunk:
                    fh.write(chunk)
                    n += len(chunk)
        if n != int(a.get("size") or n):
            log(f"حجم دانلودشده ({n}) با asset ({a.get('size')}) یکی نبود.")
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(out)
        log(f"چک‌پوینت بازیابی شد: {out.name} ({n / 1e6:.1f}MB).")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"بازیابی چک‌پوینت ناموفق بود: {e}")
        return False


def cleanup_release(repo: str, token: str, ref: str, *, log=print) -> None:
    """حذف release/تگ مرجع داده‌شده."""
    if not ref or not ref.startswith("rel:"):
        return
    tag = ref.split(":", 1)[1]
    _delete_release_by_tag(repo, token, tag)
    log(f"چک‌پوینت پاک شد: {tag}.")


# سازگاری با نام‌های قدیمی (اگر جایی استفاده شده باشد)
upload_checkpoint = None
download_checkpoint = None
cleanup_checkpoint = None
