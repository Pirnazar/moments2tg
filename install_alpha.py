"""
Download and install Alpha (the WeChat reader whose HTTP API moments2tg uses).

Alpha is third-party, closed-source software; we do NOT bundle or redistribute
it. This script fetches the official, unmodified installer from the project's
GitHub release, verifies its SHA-256 against a pinned value, and launches it —
exactly what you would do by hand. If Alpha is already installed, it does nothing.

    python install_alpha.py          # ask, then download + run the installer
    python install_alpha.py --yes    # no prompt
    python install_alpha.py --silent # Windows: silent install (/S)

After installing, open Alpha once to connect your WeChat and enable its
HTTP API (Settings -> 接口与集成 -> 本机 HTTP API). moments2tg talks to that API.
"""

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CACHE = BASE_DIR / "cache"
ALPHA_VERSION = "0.2.0"
REL_BASE = f"https://github.com/xunchahaha/alpha-releases/releases/download/v{ALPHA_VERSION}"

# Pinned official release assets (SHA-256 from the v0.2.0 release notes).
RELEASES = {
    "win32": {
        "name": f"Alpha-{ALPHA_VERSION}-x64-Setup.exe",
        "sha256": "d89dbab2204cac21ca2d726893b4c3fa79ab16456dd5c9c74b0af515e4723529",
        "size": 137881716,
    },
    "darwin": {
        "name": f"Alpha-{ALPHA_VERSION}-arm64.dmg",
        "sha256": "cdfb50640f1c6cb33fedfacff1a21c4c2a6f8d3f197000170b9f3c826c74224f",
        "size": 182723934,
    },
    "linux": {
        "name": f"Alpha-{ALPHA_VERSION}-x64.AppImage",
        "sha256": "d8da1c0a798fef443fb0d7ea5ca2f4287910656230a0818ad7941f0ade732107",
        "size": 176693363,
    },
}


def installed_location() -> str | None:
    """Return a human path if Alpha already seems installed, else None."""
    home = Path.home()
    if sys.platform == "win32":
        cands = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Alpha" / "Alpha.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Alpha" / "Alpha.exe",
        ]
    elif sys.platform == "darwin":
        cands = [Path("/Applications/Alpha.app"), home / "Applications" / "Alpha.app"]
    else:
        cands = list((home / "Applications").glob("Alpha*.AppImage")) if (home / "Applications").is_dir() else []
        cands += [Path("/opt/Alpha/alpha")]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, expect_size: int):
    print(f"[alpha] downloading {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or expect_size)
        done = 0
        last = -1
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                pct = int(done * 100 / total) if total else 0
                if pct != last and pct % 5 == 0:
                    print(f"\r[alpha] {pct}%  ({done // (1024*1024)} MiB)", end="", flush=True)
                    last = pct
    print()


def install_file(path: Path, silent: bool):
    if sys.platform == "win32":
        print("[alpha] launching the installer...")
        if silent:
            # electron-builder NSIS silent, per-user
            subprocess.run([str(path), "/S"], check=False)
        else:
            print("      SmartScreen may warn (unsigned): More info -> Run anyway.")
            subprocess.run([str(path)], check=False)
    elif sys.platform == "darwin":
        print("[alpha] opening the disk image; drag Alpha into Applications.")
        subprocess.run(["open", str(path)], check=False)
    else:
        target = Path.home() / "Applications"
        target.mkdir(parents=True, exist_ok=True)
        dest = target / path.name
        if dest.resolve() != path.resolve():
            dest.write_bytes(path.read_bytes())
        os.chmod(dest, 0o755)
        print(f"[alpha] AppImage ready: {dest}\n      run it:  \"{dest}\"")


def main() -> int:
    ap = argparse.ArgumentParser(description="Download + install Alpha (official release).")
    ap.add_argument("--yes", action="store_true", help="don't prompt")
    ap.add_argument("--silent", action="store_true", help="Windows: silent install (/S)")
    ap.add_argument("--force", action="store_true", help="install even if Alpha is found")
    args = ap.parse_args()

    where = installed_location()
    if where and not args.force:
        print(f"[alpha] already installed: {where}")
        print("        open Alpha, connect WeChat and enable its HTTP API if you haven't.")
        return 0

    rel = RELEASES.get(sys.platform)
    if not rel:
        print(f"[alpha] no Alpha build mapped for platform '{sys.platform}'.")
        return 1
    if sys.platform == "darwin" and os.uname().machine not in ("arm64", "aarch64"):
        print("[alpha] Alpha for macOS is Apple Silicon (arm64) only; no Intel build exists.")
        return 1

    url = f"{REL_BASE}/{rel['name']}"
    size_mb = rel["size"] // (1024 * 1024)
    if not args.yes:
        print(f"This will download the official Alpha {ALPHA_VERSION} installer (~{size_mb} MiB)")
        print(f"from {url}")
        print("verify its SHA-256, and run it. Alpha is third-party, unsigned software.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("[alpha] cancelled.")
            return 1

    CACHE.mkdir(exist_ok=True)
    dest = CACHE / rel["name"]
    if not (dest.exists() and sha256_of(dest) == rel["sha256"]):
        download(url, dest, rel["size"])

    print("[alpha] verifying SHA-256...")
    actual = sha256_of(dest)
    if actual != rel["sha256"]:
        print(f"[alpha] CHECKSUM MISMATCH!\n  expected {rel['sha256']}\n  actual   {actual}")
        print("[alpha] refusing to run a file that does not match the official release.")
        try:
            dest.unlink()
        except OSError:
            pass
        return 2
    print("[alpha] checksum OK, this is the official release.")

    install_file(dest, args.silent)

    print()
    print("[alpha] Next, one time: open Alpha, connect your WeChat (it finds the key itself),")
    print("        then Settings -> 接口与集成 -> 本机 HTTP API: set a token and 保存并启动.")
    print("        Put that address and token into moments2tg (the panel's Settings tab).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[alpha] cancelled.")
        sys.exit(1)
