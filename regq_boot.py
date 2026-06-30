#!/usr/bin/env python3
"""regq_boot.py - cross-platform bootstrap for the regq skill.

Replaces the shell-specific bits of the skill (mkdir -p / curl / wget / echo >)
with a single stdlib-only Python program that behaves identically on Windows
and Linux/macOS.

Subcommands:
  prepare           Ensure cache dir + Python deps + db-list.json + regq.py,
                    then detect which SoC(s) match the current working dir.
  db --soc NAME     Ensure the given SoC's reg.db is downloaded / up to date.

Every run prints exactly one JSON object to stdout (diagnostics go to stderr),
so the calling agent can parse the result deterministically.
"""

import argparse
import importlib.util
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

# Directory of this script (the skill directory after Marketplace install).
SKILL_DIR = Path(__file__).resolve().parent

# Base URL of the regq file server's "regq/" directory. Override with the
# REGQ_BASE_URL env var to point at a test server (e.g. a local http.server)
# without editing or repackaging the skill:
#   REGQ_BASE_URL=http://127.0.0.1:28683/regq
DEFAULT_BASE_URL = "http://192.168.61.147:28683/regq"
BASE_URL = os.environ.get("REGQ_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
DB_LIST_URL = BASE_URL + "/db-list.json"
CACHE_DIR = Path.home() / ".cache" / "regq"
HTTP_TIMEOUT = 15


def _resolve(url):
    """Resolve a db-list URL against BASE_URL.

    Absolute URLs (http://...) are returned unchanged for back-compat with
    older db-list.json files; relative paths (e.g. "regq.py", "dbs/x.db") are
    joined onto BASE_URL so one db-list.json works for any server."""
    return urljoin(BASE_URL + "/", url) if url else url


def _eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def ensure_deps():
    """Install missing Python deps.

    windows-curses is REQUIRED on Windows (regq.py imports curses at load time);
    pyserial/paramiko are only needed for live UART/SSH reads.
    """
    req = [("serial", "pyserial"), ("paramiko", "paramiko")]
    if platform.system() == "Windows":
        req.append(("curses", "windows-curses"))
    missing = [pkg for mod, pkg in req if importlib.util.find_spec(mod) is None]
    installed, failed = [], []
    for pkg in missing:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", pkg],
                stdout=sys.stderr, stderr=sys.stderr,
            )
            installed.append(pkg)
        except Exception as e:  # noqa: BLE001
            _eprint(f"pip install {pkg} failed: {e}")
            failed.append(pkg)
    present = [pkg for mod, pkg in req if importlib.util.find_spec(mod) is not None]
    return {"installed": installed, "failed": failed, "present": present}


def _download(url, dest: Path):
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        data = r.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return data


def fetch_db_list():
    """Fetch db-list.json; check skill dir first, then server, then cache."""
    # 1. Check if bundled in skill directory (Marketplace install)
    bundled = SKILL_DIR / "db-list.json"
    if bundled.exists():
        try:
            return json.loads(bundled.read_text(encoding="utf-8")), "bundled"
        except Exception as e:
            _eprint(f"bundled db-list.json unreadable: {e}")
    # 2. Try server download
    dest = CACHE_DIR / "db-list.json"
    try:
        data = _download(DB_LIST_URL, dest)
        return json.loads(data.decode("utf-8")), "ok"
    except Exception as e:  # noqa: BLE001
        _eprint(f"db-list fetch failed: {e}")
        if dest.exists():
            try:
                return json.loads(dest.read_text(encoding="utf-8")), "cache"
            except Exception as e2:  # noqa: BLE001
                _eprint(f"cached db-list unreadable: {e2}")
        return None, "error"


def _read_version(path: Path):
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def ensure_regq(db_list):
    """Download regq.py when the version differs or the file is missing.

    If regq.py is bundled alongside regq_boot.py (Marketplace install), copy
    it to the cache so the rest of the skill works identically.
    """
    # 1. Check if bundled in skill directory
    bundled = SKILL_DIR / "regq.py"
    if bundled.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        script = CACHE_DIR / "regq.py"
        try:
            import shutil
            shutil.copy2(str(bundled), str(script))
            return {"path": str(script), "version": "bundled", "status": "bundled"}
        except Exception as e:
            _eprint(f"copy bundled regq.py failed: {e}")
    # 2. Fall back to server download
    url = _resolve(db_list.get("regq_url"))
    want = str(db_list.get("regq_version", ""))
    script = CACHE_DIR / "regq.py"
    vfile = CACHE_DIR / "regq.py.version"
    have = _read_version(vfile)
    status = "cached"
    if url and (not script.exists() or have != want):
        try:
            _download(url, script)
            vfile.write_text(want, encoding="utf-8")
            status = "updated"
        except Exception as e:  # noqa: BLE001
            _eprint(f"regq.py download failed: {e}")
            status = "stale" if script.exists() else "error"
    return {"path": str(script), "version": want, "status": status}


def _db_available(name, meta):
    """Return True if the SoC DB is cached locally or accessible on the server."""
    if (CACHE_DIR / f"{name}.db").exists():
        return True
    url = _resolve(meta.get("db_url"))
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


def detect_socs(db_list):
    cwd = os.getcwd()
    low = cwd.lower()
    out = []
    for name, meta in (db_list.get("socs", {}) or {}).items():
        if not _db_available(name, meta):
            continue
        kws = meta.get("keywords", []) or []
        out.append({
            "name": name,
            "keywords": kws,
            "matched": any(str(k).lower() in low for k in kws),
            "version": str(meta.get("version", "")),
            "db_cached": (CACHE_DIR / f"{name}.db").exists(),
            "cached_version": _read_version(CACHE_DIR / f"{name}.version"),
        })
    return cwd, out


def cmd_prepare():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    result = {
        "cache_dir": str(CACHE_DIR),
        "python_exe": sys.executable,
        "platform": platform.system(),
        "base_url": BASE_URL,
        "deps": ensure_deps(),
    }
    db_list, dl_status = fetch_db_list()
    result["db_list_status"] = dl_status
    if db_list is None:
        result["status"] = "error"
        result["message"] = "regq server unreachable and no local db-list cache"
        return result
    result["regq"] = ensure_regq(db_list)
    cwd, socs = detect_socs(db_list)
    result["cwd"] = cwd
    result["socs"] = socs
    result["matched_socs"] = [s["name"] for s in socs if s["matched"]]
    result["status"] = "ok"
    return result


def cmd_db(soc):
    db_list, _ = fetch_db_list()
    if db_list is None:
        return {"status": "error", "soc": soc, "message": "no db-list available"}
    meta = (db_list.get("socs", {}) or {}).get(soc)
    if meta is None:
        return {"status": "error", "soc": soc, "message": f"unknown SoC: {soc}"}
    want = str(meta.get("version", ""))
    url = _resolve(meta.get("db_url"))
    dbfile = CACHE_DIR / f"{soc}.db"
    vfile = CACHE_DIR / f"{soc}.version"
    have = _read_version(vfile)
    status = "cached"
    if url and (not dbfile.exists() or have != want):
        try:
            _eprint(f"Downloading {soc}.db {want} ...")
            _download(url, dbfile)
            vfile.write_text(want, encoding="utf-8")
            status = "updated"
        except Exception as e:  # noqa: BLE001
            _eprint(f"{soc}.db download failed: {e}")
            if not dbfile.exists():
                return {"status": "error", "soc": soc, "message": str(e)}
            status = "stale"
    return {"status": "ok", "soc": soc, "db_path": str(dbfile),
            "version": want, "db_status": status}


def main():
    ap = argparse.ArgumentParser(description="regq skill bootstrap (cross-platform)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare")
    p_db = sub.add_parser("db")
    p_db.add_argument("--soc", required=True)
    args = ap.parse_args()

    out = cmd_prepare() if args.cmd == "prepare" else cmd_db(args.soc)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
