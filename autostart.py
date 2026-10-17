"""
Start the moments2tg panel at user login, without admin rights:
- Windows: a .cmd in the user's Startup folder;
- macOS:   a LaunchAgent in ~/Library/LaunchAgents;
- Linux:   an XDG autostart .desktop entry in ~/.config/autostart.
All of them run start.bat / start.sh with --panel --no-browser in the user's desktop session
(the session matters: WeFlow is a desktop app and pywechat needs the interactive desktop).
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LABEL = "com.moments2tg.panel"
ARGS = ["--panel", "--no-browser"]


def _entry_path() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "moments2tg.cmd"
    if sys.platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "autostart" / "moments2tg.desktop"


def status() -> dict:
    path = _entry_path()
    return {"enabled": path.exists(), "path": str(path), "supported": True}


def enable():
    path = _entry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        script = BASE_DIR / "start.bat"
        # minimized console; start.bat keeps the panel running
        path.write_text(f'@echo off\r\nstart "moments2tg" /min cmd /c ""{script}" {" ".join(ARGS)}"\r\n', encoding="utf-8")
    elif sys.platform == "darwin":
        script = BASE_DIR / "start.sh"
        log = BASE_DIR / "autostart.log"
        with path.open("wb") as f:
            plistlib.dump({
                "Label": LABEL,
                "ProgramArguments": ["/bin/bash", str(script), *ARGS],
                "WorkingDirectory": str(BASE_DIR),
                "RunAtLoad": True,
                "StandardOutPath": str(log),
                "StandardErrorPath": str(log),
            }, f)
    else:
        script = BASE_DIR / "start.sh"
        path.write_text("\n".join([
            "[Desktop Entry]",
            "Type=Application",
            "Name=moments2tg",
            "Comment=WeChat Moments to Telegram",
            f'Exec=/bin/bash "{script}" {" ".join(ARGS)}',
            f"Path={BASE_DIR}",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "",
        ]), encoding="utf-8")
    return status()


def disable():
    path = _entry_path()
    if sys.platform == "darwin" and path.exists():
        # stop the agent if it was loaded; ignore errors (it may not be running)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.unlink(missing_ok=True)
    return status()
