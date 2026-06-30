#!/usr/bin/env python3
"""Cross-platform installer for the regq skill (Windows / Linux / macOS).

Downloads skill-regq.zip from the regq file server (or installs from a local
copy) and unpacks it into the RealCoder skills directory. Standard library
only, so it runs anywhere Python 3 is available.

Usage:
    python  install-skill.py                              # Windows
    python3 install-skill.py                              # Linux / macOS
    python  install-skill.py --base-url http://host:28683/regq
    python  install-skill.py --source ./skill-regq.zip    # offline / local zip
    python  install-skill.py --skills-dir /custom/skills

It can also be piped straight from the server:
    curl -s http://host:28683/regq/install-skill.py | python3 -

Server URL precedence: --base-url  >  $REGQ_BASE_URL  >  built-in default.
"""

import argparse
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_BASE_URL = "http://192.168.61.147:28683/regq"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description="Install the regq skill (cross-platform)")
    ap.add_argument("--base-url",
                    default=os.environ.get("REGQ_BASE_URL", DEFAULT_BASE_URL),
                    help="regq server base URL (the .../regq directory)")
    ap.add_argument("--source",
                    help="install from a local skill-regq.zip instead of downloading")
    ap.add_argument("--skills-dir",
                    default=str(Path.home() / ".claude" / "skills"),
                    help="target skills directory (default: ~/.claude/skills)")
    args = ap.parse_args()

    skills_dir = Path(args.skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)

    tmp = None
    try:
        if args.source:
            zip_path = Path(args.source)
            if not zip_path.exists():
                log(f"error: source not found: {zip_path}")
                return 1
            log(f"Installing from local file: {zip_path}")
        else:
            url = args.base_url.rstrip("/") + "/skill-regq.zip"
            log(f"Downloading {url}")
            fd, tmp = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            zip_path = Path(tmp)
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    zip_path.write_bytes(r.read())
            except Exception as e:  # noqa: BLE001
                log(f"error: download failed: {e}")
                return 1

        with zipfile.ZipFile(zip_path) as z:
            top = sorted({n.split("/")[0] for n in z.namelist() if n.strip("/")})
            # Remove any previous install so renamed/removed files don't linger
            for t in top:
                dest = skills_dir / t
                if dest.is_dir():
                    log(f"Removing previous {dest}")
                    shutil.rmtree(dest, ignore_errors=True)
                elif dest.exists():
                    dest.unlink()
            z.extractall(skills_dir)

        log(f"Installed {top} into {skills_dir}")
        log("Restart RealCoder to activate.")
        return 0
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    sys.exit(main())
