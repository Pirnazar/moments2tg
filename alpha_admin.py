"""
Manage the Alpha desktop app for the control panel (Windows, macOS, Linux):
start / stop / restart the process and report whether it is running.

Alpha keeps its own settings (including the HTTP API token) encrypted in its
app data, so — unlike WeFlow — moments2tg does not edit them. You set the API up
once in Alpha's own UI; this module only launches Alpha and watches it, so it
can be auto-started with the panel and restarted if it goes away.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
PROCESS_NAMES = {"alpha.exe", "alpha"}


def candidate_exe_paths() -> list[Path]:
    home = Path.home()
    if IS_WIN:
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        progs = [Path(os.environ[v]) for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]
        return ([p / "Alpha" / "Alpha.exe" for p in progs]
                + [local / "Programs" / "Alpha" / "Alpha.exe"])
    if IS_MAC:
        return [Path("/Applications/Alpha.app"), home / "Applications" / "Alpha.app"]
    paths = [Path("/opt/Alpha/alpha"), home / ".local" / "bin" / "alpha"]
    for folder in (home / "Applications", home / "Downloads", home):
        if folder.is_dir():
            paths += sorted(folder.glob("Alpha*.AppImage"), reverse=True)
    return paths


def _app_path_of(exe: str) -> Path:
    p = Path(exe)
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return p


class AlphaAdminError(Exception):
    pass


class AlphaAdmin:
    def __init__(self, exe_path: str | None = None):
        self.exe_override = Path(exe_path).expanduser() if exe_path else None

    # ----- process

    def _main_processes(self) -> list[psutil.Process]:
        found = []
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info["name"] or "").lower() not in PROCESS_NAMES:
                    continue
                if any(str(a).startswith("--type=") for a in p.cmdline()):
                    continue  # Electron child (renderer/gpu/utility)
                p.info["exe"] = p.exe()
                found.append(p)
            except (psutil.Error, OSError):
                continue
        return found

    def processes(self) -> list[dict]:
        out = []
        for p in self._main_processes():
            path = p.info.get("exe") or ""
            if not IS_WIN and not IS_MAC:
                try:
                    path = p.environ().get("APPIMAGE") or path
                except (psutil.Error, OSError):
                    pass
            out.append({"pid": p.pid, "path": path})
        return out

    def running(self) -> bool:
        return bool(self._main_processes())

    def exe_path(self) -> Path | None:
        if self.exe_override:
            return self.exe_override
        for p in self.processes():
            if p["path"]:
                return _app_path_of(p["path"]) if IS_MAC else Path(p["path"])
        for c in candidate_exe_paths():
            if c.exists():
                return c
        return None

    def start(self):
        exe = self.exe_path()
        if not exe or not exe.exists():
            raise AlphaAdminError("Приложение Alpha не найдено. Установите его (start --setup) "
                                  "или укажите путь на вкладке Alpha.")
        if self.running():
            return
        common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        if IS_WIN:
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags, **common)
        elif IS_MAC:
            # -j launches hidden/backgrounded where the app supports it
            subprocess.Popen(["open", "-g", "-a", str(_app_path_of(str(exe)))], **common)
        else:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), start_new_session=True, **common)

    def stop(self, timeout: float = 20):
        procs = self._main_processes()
        if not procs:
            return
        tree = []
        for p in procs:
            try:
                tree += p.children(recursive=True)
            except psutil.Error:
                pass
            tree.append(p)
        for p in tree:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(tree, timeout=min(timeout, 8))
        for p in alive:
            try:
                p.kill()
            except psutil.Error:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running():
                return
            time.sleep(0.5)
        raise AlphaAdminError("Alpha не завершился вовремя")

    def info(self) -> dict:
        procs = self.processes()
        exe = self.exe_path()
        return {
            "exe_path": str(exe) if exe else "",
            "exe_exists": bool(exe and exe.exists()),
            "running": bool(procs),
            "pids": [p["pid"] for p in procs],
        }
