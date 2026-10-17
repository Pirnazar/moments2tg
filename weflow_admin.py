"""
Manages the local WeFlow installation for the control panel (Windows, macOS, Linux):
- reads/writes the WeFlow settings that matter for moments2tg
  (HTTP API on/port/token, start at login, start minimized, close-to-tray);
- starts / stops / restarts the WeFlow process;
- toggles WeFlow's "block Moments deletion" trigger over its HTTP API.

WeFlow keeps settings in electron-store (WeFlow-config.json in the Electron
userData folder) and holds them in memory while running, so the file is only
edited while WeFlow is stopped and WeFlow is started again afterwards.
Encrypted values ("safe:...") are never decrypted here; WeFlow accepts a plain
value for httpApiToken and re-encrypts it the next time it saves that field.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
SAFE_PREFIX = "safe:"
LOCK_PREFIX = "lock:"
CLOSE_BEHAVIORS = ("ask", "tray", "quit")
# main process names: weflow.exe (Windows), WeFlow (macOS bundle), weflow (Linux)
PROCESS_NAMES = {"weflow.exe", "weflow"}

# settings the panel may change, with their types
EDITABLE = {
    "httpApiEnabled": bool,
    "httpApiPort": int,
    "httpApiToken": str,
    "launchAtStartup": bool,
    "silentStartup": bool,
    "windowCloseBehavior": str,
}


def config_dir_candidates() -> list[Path]:
    home = Path.home()
    if IS_WIN:
        base = [Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")]
    elif IS_MAC:
        base = [home / "Library" / "Application Support"]
    else:
        base = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")]
    # Electron names the folder after the app; Linux file systems are case-sensitive
    return [b / name for b in base for name in ("WeFlow", "weflow")]


def default_config_path() -> Path:
    candidates = [d / "WeFlow-config.json" for d in config_dir_candidates()]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def candidate_exe_paths() -> list[Path]:
    home = Path.home()
    if IS_WIN:
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        progs = [Path(os.environ[v]) for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v)]
        return ([local / "Programs" / "WeFlow" / "weflow.exe", local / "Programs" / "weflow" / "weflow.exe"]
                + [p / "WeFlow" / "weflow.exe" for p in progs])
    if IS_MAC:
        return [Path("/Applications/WeFlow.app"), home / "Applications" / "WeFlow.app"]
    paths = [home / "Applications" / "WeFlow.AppImage", Path("/opt/WeFlow/weflow"), Path("/usr/bin/weflow"),
             home / ".local" / "bin" / "weflow"]
    for folder in (home / "Applications", home / "Downloads", home):
        if folder.is_dir():
            paths += sorted(folder.glob("WeFlow*.AppImage"), reverse=True)
    return paths


def app_path_of(exe: str) -> Path:
    """macOS: /Applications/WeFlow.app/Contents/MacOS/WeFlow -> /Applications/WeFlow.app"""
    p = Path(exe)
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return p


class WeFlowAdminError(Exception):
    pass


class WeFlowAdmin:
    def __init__(self, config_path: str | None = None, exe_path: str | None = None):
        self.config_path = Path(config_path).expanduser() if config_path else default_config_path()
        self.exe_override = Path(exe_path).expanduser() if exe_path else None

    # ----- process

    def _main_processes(self) -> list[psutil.Process]:
        found = []
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info["name"] or "").lower() not in PROCESS_NAMES:
                    continue
                # skip Electron child processes (renderer/gpu/utility)
                if any(str(a).startswith("--type=") for a in p.cmdline()):
                    continue
                p.info["exe"] = p.exe()
                found.append(p)
            except (psutil.Error, OSError):
                continue
        return found

    def processes(self) -> list[dict]:
        """Running WeFlow main processes: [{"pid": int, "path": str}]."""
        out = []
        for p in self._main_processes():
            path = p.info["exe"] or ""
            if not IS_WIN and not IS_MAC:
                # an AppImage runs from a temporary mount; report the image file itself
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
                return app_path_of(p["path"]) if IS_MAC else Path(p["path"])
        for p in candidate_exe_paths():
            if p.exists():
                return p
        return None

    def start(self):
        exe = self.exe_path()
        if not exe or not exe.exists():
            raise WeFlowAdminError("Не найдено приложение WeFlow. Укажите путь к нему на вкладке WeFlow.")
        if self.running():
            return
        common = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        if IS_WIN:
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags, **common)
        elif IS_MAC:
            subprocess.Popen(["open", "-a", str(app_path_of(str(exe)))], **common)
        else:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), start_new_session=True, **common)

    def stop(self, timeout: float = 20):
        procs = self._main_processes()
        if not procs:
            return
        # WeFlow may only hide to the tray when asked to close, so terminate the whole tree
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
                time.sleep(1)  # let file handles close
                return
            time.sleep(0.5)
        raise WeFlowAdminError("WeFlow не завершился вовремя")

    # ----- settings file

    def read(self) -> dict:
        if not self.config_path.exists():
            raise WeFlowAdminError(f"Файл настроек WeFlow не найден: {self.config_path}. "
                                   "Запустите WeFlow хотя бы один раз.")
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def info(self) -> dict:
        procs = self.processes()
        exe = self.exe_path()
        out = {
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
            "exe_path": str(exe) if exe else "",
            "exe_exists": bool(exe and exe.exists()),
            "running": bool(procs),
            "pids": [p["pid"] for p in procs],
        }
        if not out["config_exists"]:
            return out
        data = self.read()
        token = data.get("httpApiToken") or ""
        out["settings"] = {
            "httpApiEnabled": bool(data.get("httpApiEnabled")),
            "httpApiPort": int(data.get("httpApiPort") or 5031),
            "httpApiHost": data.get("httpApiHost") or "127.0.0.1",
            "launchAtStartup": data.get("launchAtStartup") is True,
            "silentStartup": data.get("silentStartup") is True,
            "windowCloseBehavior": data.get("windowCloseBehavior") or "ask",
        }
        out["token_state"] = ("empty" if not token else
                              "encrypted" if token.startswith((SAFE_PREFIX, LOCK_PREFIX)) else "plain")
        out["token_plain"] = token if out["token_state"] == "plain" else ""
        wcdb = data.get("wcdbLibPath") or ""
        out["diag"] = {
            "onboardingDone": bool(data.get("onboardingDone")),
            "dbPath": data.get("dbPath") or "",
            "myAccountId": data.get("myAccountId") or "",
            "wcdbLibPath": wcdb,
            "wcdbLibExists": bool(wcdb and Path(os.path.expandvars(wcdb)).exists()),
            "appLock": bool(data.get("authEnabled")),
            "lockMode": str(data.get("decryptKey") or "").startswith(LOCK_PREFIX),
        }
        return out

    def apply(self, changes: dict, restart: bool = True) -> dict:
        """Writes the given settings; restarts WeFlow if it is running (or if restart=True)."""
        clean = {}
        for key, value in changes.items():
            if key not in EDITABLE:
                raise WeFlowAdminError(f"Настройку {key} менять нельзя")
            typ = EDITABLE[key]
            if typ is bool:
                value = bool(value)
            elif typ is int:
                value = int(value)
                if not 1024 <= value <= 65535:
                    raise WeFlowAdminError("Порт должен быть в диапазоне 1024–65535")
            else:
                value = str(value).strip()
            if key == "windowCloseBehavior" and value not in CLOSE_BEHAVIORS:
                raise WeFlowAdminError(f"windowCloseBehavior: одно из {CLOSE_BEHAVIORS}")
            if key == "httpApiToken" and not value:
                continue  # empty = keep the current token
            clean[key] = value
        if not clean:
            return {"changed": [], "restarted": False}

        was_running = self.running()
        if was_running:
            self.stop()
        data = self.read()
        backup = self.config_path.with_name(self.config_path.name + ".moments2tg.bak")
        if not backup.exists():
            shutil.copy2(self.config_path, backup)
        data.update(clean)
        tmp = self.config_path.with_suffix(".moments2tg.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent="\t"), encoding="utf-8")
        tmp.replace(self.config_path)

        started = False
        if was_running or restart:
            self.start()
            started = True
        return {"changed": sorted(clean), "restarted": started, "backup": str(backup)}

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(24)
