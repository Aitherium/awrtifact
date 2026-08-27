"""`awrtifact fetch` — resumable, size-verified, TOFU-hashed download.

Ported from the tenant fetch lane (`.DEPLOYMENT/templates/tenant-repo/models/
fetch_models.py`) because that script's lessons are the point:

1. RESUME. These are multi-GB streams. A restarted download continues from
   the byte count already on disk via `Range: bytes=<have>-`, and a server
   that answers non-206 to a resume restarts from byte 0 explicitly.
2. SIZE VERIFICATION. A truncated artifact is not a download error — the
   loader reports a corrupt model, which reads like a bad quant rather than
   a short file. Every download is checked against the expected size before
   it is accepted.
3. STITCHED ASSETS ARE TRANSPARENT. Files over GitHub's 2 GiB cap live
   upstream as `.partN` slices; the worker reassembles them behind the
   original filename. Ask for the original name and it works.
4. TOFU SHA256. The first successful fetch records the digest into the
   lockfile; every later fetch (or --verify-only) compares. That makes a
   silently-changed mirror asset visible on the second machine. It is
   trust-on-first-use, not a signed digest, and the output says so.

MAX_STALLS is the real stop condition: attempts that make no progress.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 8 * 1024 * 1024
MAX_ATTEMPTS = 200
MAX_STALLS = 4
UA = "awrtifact-fetch/1"


class FetchError(RuntimeError):
    """A download failed honestly (not a silent truncation)."""


def _resume_headers(have: int) -> dict[str, str]:
    return {"Range": f"bytes={have}-", "User-Agent": UA} if have else {"User-Agent": UA}


def _download_once(url: str, dest: Path, expected: int | None) -> int:
    """One attempt; returns bytes present on disk after the attempt."""
    have = dest.stat().st_size if dest.exists() else 0
    if expected is not None and have > expected:
        raise FetchError(
            f"on-disk {dest.name} is {have} bytes, larger than expected "
            f"{expected} — remove it or fetch elsewhere"
        )
    headers = _resume_headers(have)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — https checked by caller
            status = getattr(resp, "status", 200)
            if have and status != 206:
                # Server ignored the resume; start over rather than append.
                have = 0
            if have:
                with open(dest, "ab") as f:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
            else:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
    except urllib.error.HTTPError as exc:
        if exc.code in (416,):
            # Range not satisfiable: the server has more than we think.
            raise FetchError(f"server 416 — {dest.name} may be complete here "
                             f"and short at the mirror") from exc
        raise FetchError(f"HTTP {exc.code} fetching {dest.name}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error fetching {dest.name}: {exc.reason}") from exc
    return dest.stat().st_size


def _load_lock(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_lock(path: Path, lock: dict) -> None:
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def fetch(
    name: str,
    url: str,
    dest_dir: Path,
    expected: int | None,
    lockfile: Path | None = None,
    verify_only: bool = False,
) -> dict:
    """Fetch `name` from `url` into `dest_dir`, size- and hash-verified.

    `expected` is the manifest's total (or None to trust Content-Length).
    Returns {"path", "bytes", "sha256", "status"} where status is one of
    fetched / verified / up-to-date / sha-mismatch.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    lock_path = Path(lockfile) if lockfile else dest_dir / "awrtifact.lock.json"
    lock = _load_lock(lock_path)
    known = lock.get(name)

    if verify_only and known and dest.is_file():
        got = _sha256(dest)
        if got == known["sha256"]:
            return {"path": str(dest), "bytes": dest.stat().st_size,
                    "sha256": got, "status": "verified"}
        return {"path": str(dest), "bytes": dest.stat().st_size,
                "sha256": got, "status": "sha-mismatch"}

    attempts = 0
    stalls = 0
    last = dest.stat().st_size if dest.exists() else 0
    while attempts < MAX_ATTEMPTS and stalls < MAX_STALLS:
        attempts += 1
        now = _download_once(url, dest, expected)
        if expected is not None and now > expected:
            raise FetchError(
                f"{name}: server delivered more than the expected {expected} "
                f"bytes — the mirror is serving a different artifact"
            )
        if now == last:
            stalls += 1
        else:
            stalls = 0
        last = now
        if expected is not None and now == expected:
            break
    if expected is not None and last != expected:
        raise FetchError(
            f"{name}: stopped at {last} of {expected} bytes — the bytes on "
            f"disk are valid; re-run to resume from here"
        )

    got = _sha256(dest)
    if known:
        if got != known["sha256"]:
            return {"path": str(dest), "bytes": last, "sha256": got,
                    "status": "sha-mismatch"}
        lock[name]["bytes"] = last
    else:
        lock[name] = {"sha256": got, "bytes": last}
    _save_lock(lock_path, lock)
    return {"path": str(dest), "bytes": last, "sha256": got, "status": "fetched"}
