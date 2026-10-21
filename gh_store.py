#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ذخیره‌سازی چک‌پوینت‌های ترجمه بین چرخه‌های GitHub Actions
---------------------------------------------------------
فایل‌های بزرگ (PDF ورودی + خروجی ناتمام) نمی‌توانند در گیت کامیت شوند؛
پس بین چرخه‌ها با این ماژول ذخیره می‌شوند:

  مسیر ۱ (اصلی): Artifact API — POST /runs/{id}/artifacts و PUT فایل zip
  مسیر ۲ (پشتیبان): Release asset — ساخت release موقت با تگ cp-... و آپلود asset

هر دو مسیر با توکن GITHUB_TOKEN خودِ ورک‌فلو (actions:write / contents:write) کار می‌کنند.
پس از بازیابی در چرخهٔ بعد، چک‌پوینت حذف می‌شود (cleanup_checkpoint).
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import requests


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


def _api(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}"


def build_zip(files: dict[str, Path], dst: Path) -> Path:
    """ساخت zip از فایل‌های چک‌پوینت (نام در zip ← مسیر واقعی)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for arc, p in files.items():
            z.write(p, arcname=arc)
    return dst


def _extract_zip(data: bytes, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # مسیرهای تودرتو را صاف کن
            (dest_dir / name).write_bytes(z.read(info))


# --------------------------------------------------------------------------
# مسیر ۱: Artifact API
# --------------------------------------------------------------------------

def _upload_artifact(repo: str, token: str, run_id: int, name: str,
                     zip_path: Path, log=print) -> int:
    """آپلود zip به‌عنوان artifact متعلق به run جاری؛ id را برمی‌گرداند."""
    api = _api(repo)
    r = requests.post(f"{api}/actions/runs/{run_id}/artifacts", headers=_hdr(token),
                      json={"name": name}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"artifact create {r.status_code}: {r.text[:160]}")
    up = ""
    try:
        j = r.json() or {}
        up = j.get("upload_url") or j.get("archive_upload_url") or ""
    except Exception:  # noqa: BLE001
        pass
    if not up:
        up = r.headers.get("Location", "")
    if not up:
        raise RuntimeError("artifact upload_url دریافت نشد")
    data = zip_path.read_bytes()
    r2 = requests.put(up, headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/zip",
                                   "Content-Length": str(len(data))},
                      data=data, timeout=1800)
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"artifact upload {r2.status_code}: {r2.text[:160]}")
    # id را از لیست پیدا کن (ثبت گاهی چند ثانیه طول می‌کشد)
    for _ in range(10):
        lst = requests.get(f"{api}/actions/runs/{run_id}/artifacts?per_page=100",
                           headers=_hdr(token), timeout=30).json()
        for a in lst.get("artifacts", []):
            if a.get("name") == name:
                log(f"artifact {a['id']} آپلود شد ({zip_path.stat().st_size / 1e6:.1f}MB).")
                return int(a["id"])
        time.sleep(2)
    raise RuntimeError("artifact پس از آپلود در لیست پیدا نشد")


def _download_artifact(repo: str, token: str, art_id: int, dest_dir: Path,
                       log=print) -> bool:
    api = _api(repo)
    meta = requests.get(f"{api}/actions/artifacts/{art_id}", headers=_hdr(token),
                        timeout=30)
    if meta.status_code != 200:
        log(f"artifact {art_id} پیدا نشد ({meta.status_code}).")
        return False
    if meta.json().get("expired"):
        log("artifact منقضی شده است.")
        return False
    url = meta.json().get("archive_download_url")
    if not url:
        return False
    r = requests.get(url, headers=_hdr(token), timeout=3600)
    if r.status_code != 200:
        log(f"دانلود artifact ناموفق ({r.status_code}).")
        return False
    _extract_zip(r.content, dest_dir)
    log(f"artifact {art_id} بازیابی شد → {dest_dir}")
    return True


def _delete_artifact(repo: str, token: str, art_id: int) -> None:
    try:
        requests.delete(f"{_api(repo)}/actions/artifacts/{art_id}",
                        headers=_hdr(token), timeout=30)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# مسیر ۲: Release asset (پشتیبان)
# --------------------------------------------------------------------------

def _upload_release(repo: str, token: str, name: str, zip_path: Path,
                    branch: str = "main") -> str:
    """release موقت با تگ=name و یک asset zip؛ «release:<tag>:<asset_id>» برمی‌گرداند."""
    api = _api(repo)
    data = zip_path.read_bytes()
    # اگر release قبلی با همین تگ مانده، اول پاکش کن
    old = requests.get(f"{api}/releases/tags/{name}", headers=_hdr(token), timeout=30)
    if old.status_code == 200:
        requests.delete(f"{api}/releases/{old.json()['id']}", headers=_hdr(token),
                        timeout=30)
        time.sleep(1)
        try:  # تگ یتیم را هم پاک کن
            requests.delete(f"{api}/git/refs/tags/{name}", headers=_hdr(token),
                            timeout=30)
        except Exception:  # noqa: BLE001
            pass
    r = requests.post(f"{api}/releases", headers=_hdr(token), timeout=30, json={
        "tag_name": name, "target_commitish": branch, "name": name,
        "body": "checkpoint (auto)", "prerelease": True})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"release create {r.status_code}: {r.text[:160]}")
    rel_id = r.json()["id"]
    r2 = requests.post(
        f"https://uploads.github.com/repos/{repo}/releases/{rel_id}/assets"
        f"?name=checkpoint.zip",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/zip",
                 "Content-Length": str(len(data))},
        data=data, timeout=3600)
    if r2.status_code not in (200, 201):
        raise RuntimeError(f"release asset upload {r2.status_code}: {r2.text[:160]}")
    return f"release:{name}:{r2.json()['id']}"


def _download_release(repo: str, token: str, tag: str, dest_dir: Path,
                      log=print) -> bool:
    r = requests.get(f"{_api(repo)}/releases/tags/{tag}", headers=_hdr(token),
                     timeout=30)
    if r.status_code != 200:
        log(f"release {tag} پیدا نشد ({r.status_code}).")
        return False
    assets = r.json().get("assets") or []
    if not assets:
        return False
    a = assets[0]
    r2 = requests.get(f"{_api(repo)}/releases/assets/{a['id']}",
                      headers={**_hdr(token), "Accept": "application/octet-stream"},
                      timeout=3600)
    if r2.status_code != 200:
        log(f"دانلود asset ناموفق ({r2.status_code}).")
        return False
    _extract_zip(r2.content, dest_dir)
    log(f"release asset بازیابی شد → {dest_dir}")
    return True


def _delete_release(repo: str, token: str, tag: str) -> None:
    try:
        r = requests.get(f"{_api(repo)}/releases/tags/{tag}", headers=_hdr(token),
                         timeout=30)
        if r.status_code == 200:
            requests.delete(f"{_api(repo)}/releases/{r.json()['id']}",
                            headers=_hdr(token), timeout=30)
        requests.delete(f"{_api(repo)}/git/refs/tags/{tag}", headers=_hdr(token),
                        timeout=30)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# رابط عمومی
# --------------------------------------------------------------------------

def upload_checkpoint(repo: str, token: str, run_id: int, name: str,
                      files: dict[str, Path], *, branch: str = "main",
                      workdir: Path | None = None, log=print) -> str:
    """آپلود چک‌پوینت؛ مرجع آن (artifact:<id> یا release:<tag>:<aid>) برمی‌گردد."""
    tmp = (workdir or Path("data/tmp")) / f"{name}.zip"
    build_zip(files, tmp)
    try:
        if run_id:
            art = _upload_artifact(repo, token, run_id, name, tmp, log=log)
            tmp.unlink(missing_ok=True)
            return f"artifact:{art}"
        raise RuntimeError("run_id در دسترس نیست")
    except Exception as e:  # noqa: BLE001
        log(f"مسیر Artifact ناموفق بود ({e})؛ مسیر Release امتحان می‌شود…")
        try:
            ref = _upload_release(repo, token, name, tmp, branch=branch)
            tmp.unlink(missing_ok=True)
            log(f"چک‌پوینت به‌صورت release ذخیره شد: {ref}")
            return ref
        except Exception as e2:  # noqa: BLE001
            log(f"مسیر Release هم ناموفق بود: {e2}")
            tmp.unlink(missing_ok=True)
            return ""


def download_checkpoint(repo: str, token: str, ref: str, dest_dir: Path,
                        log=print) -> bool:
    """بازیابی چک‌پوینت از مرجع ذخیره‌شده به dest_dir."""
    try:
        if ref.startswith("artifact:"):
            return _download_artifact(repo, token, int(ref.split(":", 1)[1]),
                                      dest_dir, log=log)
        if ref.startswith("release:"):
            parts = ref.split(":")
            return _download_release(repo, token, parts[1], dest_dir, log=log)
    except Exception as e:  # noqa: BLE001
        log(f"بازیابی چک‌پوینت ناموفق بود: {e}")
    return False


def cleanup_checkpoint(repo: str, token: str, ref: str) -> None:
    try:
        if ref.startswith("artifact:"):
            _delete_artifact(repo, token, int(ref.split(":", 1)[1]))
        elif ref.startswith("release:"):
            _delete_release(repo, token, ref.split(":")[1])
    except Exception:  # noqa: BLE001
        pass
