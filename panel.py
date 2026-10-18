"""
Local web control panel for moments2tg: http://127.0.0.1:8765

Start/stop the poller, pick accounts from WeFlow, edit config.json, run a pass
or a Moments refresh right away, test Telegram, watch sent posts and the log.
Listens on 127.0.0.1 only.
"""

import json
import logging
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import autostart
import moments2tg as core
from alpha_admin import AlphaAdmin
from weflow_admin import WeFlowAdmin

log = logging.getLogger("moments2tg.panel")

PANEL_HTML = core.BASE_DIR / "panel.html"
EXAMPLE_CONFIG = core.BASE_DIR / "config.example.json"
SECRET_FIELDS = ("weflow_token", "telegram_bot_token")
MASK = "••••"


class RingLogHandler(logging.Handler):
    """Keeps the last log lines in memory for the panel."""

    def __init__(self, size: int = 1000):
        super().__init__()
        self.lines: deque = deque(maxlen=size)
        self.seq = 0
        self._ring_lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._ring_lock:
            self.seq += 1
            self.lines.append({"seq": self.seq, "level": record.levelname, "text": msg})

    def since(self, after: int) -> list[dict]:
        with self._ring_lock:
            return [line for line in self.lines if line["seq"] > after]


def mask(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    return MASK + value[-4:] if len(value) > 8 else MASK


class Panel:
    def __init__(self, config_path: Path, port: int):
        self.config_path = Path(config_path)
        self.port = port
        self.worker = core.Worker(self.config_path)
        self.ring = RingLogHandler()
        self._installing = False
        logging.getLogger().addHandler(self.ring)

    # ----- config file

    def read_raw_config(self) -> dict:
        path = self.config_path if self.config_path.exists() else EXAMPLE_CONFIG
        raw = json.loads(path.read_text(encoding="utf-8"))
        if path == EXAMPLE_CONFIG:
            for k in SECRET_FIELDS + ("telegram_chat_id",):
                raw[k] = ""
            raw["usernames"] = []
        raw.setdefault("refresh", {})
        raw.setdefault("panel_autostart", False)
        return raw

    def public_config(self) -> dict:
        raw = self.read_raw_config()
        for k in SECRET_FIELDS:
            raw[k] = mask(raw.get(k, ""))
        raw["_exists"] = self.config_path.exists()
        return raw

    def save_config(self, incoming: dict):
        old = self.read_raw_config() if self.config_path.exists() else {}
        incoming.pop("_exists", None)
        for k in SECRET_FIELDS:
            if str(incoming.get(k, "")).startswith(MASK):
                incoming[k] = old.get(k, "")
        users = []
        for u in incoming.get("usernames") or []:
            if isinstance(u, str) and u.strip():
                users.append(u.strip())
            elif isinstance(u, dict) and str(u.get("username", "")).strip():
                entry = {"username": u["username"].strip()}
                for opt in ("label", "wechat_name"):
                    if str(u.get(opt) or "").strip():
                        entry[opt] = u[opt].strip()
                users.append(entry if len(entry) > 1 else entry["username"])
        incoming["usernames"] = users
        # validate by parsing it the same way the worker does
        tmp = self.config_path.with_suffix(".panel.tmp")
        tmp.write_text(json.dumps(incoming, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            core.load_config(tmp)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(self.config_path)
        log.info("config saved from the panel (%d accounts)", len(users))

    # ----- data

    def status(self) -> dict:
        st = dict(self.worker.status)
        st["running"] = self.worker.running
        cfg_error = None
        try:
            self.worker.ensure_loaded()
            cfg = self.worker.cfg
            st["accounts"] = len(cfg["usernames"])
            st["poll_interval_sec"] = cfg["poll_interval_sec"]
            st["refresh_enabled"] = bool(cfg["refresh"]["enabled"])
            st["refresh_interval_sec"] = cfg["refresh"].get("interval_sec", 900)
            st["telegram_ok"] = self.worker.tg is not None
            r = self.worker.refresher
            st["next_refresh_at"] = int(r.last_run + r.interval) if r and r.last_run else None
        except Exception as e:
            cfg_error = str(e)
        st["config_error"] = cfg_error
        st["config_exists"] = self.config_path.exists()
        st["platform"] = sys.platform
        st["refresh_supported"] = sys.platform == "win32"
        st["refresh_installed"] = self.refresh_installed()
        st["refresh_installing"] = self._installing
        try:
            st["autostart"] = autostart.status()
        except Exception as e:
            st["autostart"] = {"enabled": False, "supported": False, "error": str(e)}
        st["history"] = list(self.worker.history)[:50]
        st["now"] = int(time.time())
        return st

    def accounts(self) -> dict:
        raw = self.read_raw_config()
        url = raw.get("weflow_url") or "http://127.0.0.1:5031"
        weflow = core.WeFlow(url, raw.get("weflow_token", ""))
        if not weflow.health():
            raise RuntimeError(f"WeFlow не отвечает на {url}. Запустите WeFlow и включите API-сервис.")
        usernames = weflow.sns_usernames()
        try:
            contacts = weflow.contacts()
        except Exception:
            contacts = {}
        selected = {}
        for u in raw.get("usernames") or []:
            if isinstance(u, str):
                selected[u] = {}
            elif isinstance(u, dict) and u.get("username"):
                selected[u["username"]] = u
        rows = []
        for name in sorted(set(usernames) | set(selected)):
            c = contacts.get(name, {})
            sel = selected.get(name)
            rows.append({
                "username": name,
                "remark": c.get("remark") or "",
                "nickname": c.get("nickname") or c.get("displayName") or "",
                "selected": sel is not None,
                "label": (sel or {}).get("label") or "",
                "wechat_name": (sel or {}).get("wechat_name") or "",
                "has_moments": name in usernames,
            })
        return {"accounts": rows}

    def test_telegram(self):
        self.worker.ensure_loaded()
        if self.worker.tg is None:
            raise RuntimeError("Не заданы токен бота и канал")
        self.worker.tg.send_text("✅ moments2tg: тестовое сообщение из панели управления")

    # ----- Telegram helpers for the setup wizard

    def _tg_call(self, token: str, method: str, **params):
        raw = self.read_raw_config()
        token = token if token and not token.startswith(MASK) else raw.get("telegram_bot_token", "")
        if not token:
            raise RuntimeError("Не указан токен бота")
        base = (raw.get("telegram_api_base") or "https://api.telegram.org").rstrip("/")
        proxy = raw.get("telegram_proxy")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            r = core.requests.post(f"{base}/bot{token}/{method}", data=params, timeout=30, proxies=proxies)
            body = r.json()
        except Exception as e:
            raise RuntimeError(f"Telegram недоступен: {e}")
        if not body.get("ok"):
            desc = body.get("description") or f"HTTP {r.status_code}"
            if r.status_code == 401:
                desc = "неверный токен бота"
            raise RuntimeError(f"Telegram: {desc}")
        return body["result"]

    def telegram_check(self, data: dict) -> dict:
        token = str(data.get("token") or "").strip()
        me = self._tg_call(token, "getMe")
        out = {"ok": True, "bot": {"id": me["id"], "username": me.get("username"), "name": me.get("first_name")}}
        chat_id = str(data.get("chat_id") or "").strip()
        if chat_id:
            chat = self._tg_call(token, "getChat", chat_id=chat_id)
            out["chat"] = {"id": chat["id"], "title": chat.get("title") or chat.get("first_name"),
                           "username": chat.get("username"), "type": chat.get("type")}
            member = self._tg_call(token, "getChatMember", chat_id=chat_id, user_id=me["id"])
            status = member.get("status")
            can_post = status == "creator" or (status == "administrator" and (
                member.get("can_post_messages", True) if chat.get("type") == "channel" else True))
            out["member"] = {"status": status, "can_post": bool(can_post)}
            if not can_post:
                out["message"] = "Бот не может публиковать в этом чате: сделайте его администратором с правом публикации"
            else:
                out["message"] = f"Всё готово: @{me.get('username')} может публиковать в «{out['chat']['title']}»"
        else:
            out["message"] = f"Бот найден: @{me.get('username')}"
        return out

    def telegram_discover(self, data: dict) -> dict:
        """Channels/groups where the bot was added, from pending getUpdates."""
        token = str(data.get("token") or "").strip()
        updates = self._tg_call(token, "getUpdates", allowed_updates=json.dumps(
            ["my_chat_member", "channel_post", "message"]), limit=100, timeout=0)
        chats = {}
        for u in updates:
            for key in ("my_chat_member", "channel_post", "message"):
                obj = u.get(key)
                if not obj or not obj.get("chat"):
                    continue
                c = obj["chat"]
                if c.get("type") == "private":
                    continue
                chats[c["id"]] = {"id": c["id"], "title": c.get("title"), "username": c.get("username"),
                                  "type": c.get("type")}
        return {"ok": True, "chats": list(chats.values())}

    # ----- optional refresher dependency

    def refresh_installed(self) -> bool:
        import importlib.util
        return importlib.util.find_spec("pyweixin") is not None

    def install_refresh(self) -> dict:
        if sys.platform != "win32":
            raise RuntimeError("Обновление лент через pywechat работает только в Windows")
        if self._installing:
            return {"ok": True, "message": "Установка уже идёт"}
        req = core.BASE_DIR / "requirements-refresh.txt"

        def run():
            self._installing = True
            log.info("installing pywechat: %s -m pip install -r %s", sys.executable, req)
            try:
                proc = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                                      capture_output=True, text=True)
                for line in (proc.stdout + proc.stderr).splitlines()[-15:]:
                    log.info("pip: %s", line)
                if proc.returncode == 0:
                    log.info("pywechat installed")
                else:
                    log.error("pywechat install failed (exit %s)", proc.returncode)
            finally:
                self._installing = False

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "message": "Устанавливаю pywechat, ход установки виден в «Журнале»"}

    # ----- WeFlow management

    def weflow_admin(self) -> WeFlowAdmin:
        raw = self.read_raw_config()
        return WeFlowAdmin(raw.get("weflow_config_path") or None, raw.get("weflow_exe") or None)

    def weflow_client(self) -> core.WeFlow:
        raw = self.read_raw_config()
        return core.WeFlow(raw.get("weflow_url") or "http://127.0.0.1:5031", raw.get("weflow_token", ""))

    # ----- Alpha management

    def alpha_admin(self) -> AlphaAdmin:
        return AlphaAdmin(self.read_raw_config().get("alpha", {}).get("exe") or None)

    def alpha_info(self) -> dict:
        raw = self.read_raw_config()
        a = raw.get("alpha", {})
        info = self.alpha_admin().info()
        info["manage"] = bool(a.get("manage"))
        info["autostart"] = a.get("autostart", True)
        info["watchdog"] = a.get("watchdog", True)
        info["exe_override"] = a.get("exe") or ""
        info["url"] = raw.get("weflow_url") or ""
        info["api_up"] = self.weflow_client().health()
        return info

    def alpha_process(self, action: str) -> dict:
        admin = self.alpha_admin()
        if action == "start":
            admin.start()
            return {"ok": True, "message": "Alpha запускается"}
        if action == "stop":
            admin.stop()
            return {"ok": True, "message": "Alpha остановлен"}
        if action == "restart":
            admin.stop()
            admin.start()
            return {"ok": True, "message": "Alpha перезапускается"}
        raise ValueError(action)

    def alpha_settings(self, data: dict) -> dict:
        raw = self.read_raw_config()
        a = raw.setdefault("alpha", {})
        for k in ("manage", "autostart", "watchdog"):
            if k in data:
                a[k] = bool(data[k])
        if "exe" in data:
            a["exe"] = str(data.get("exe") or "").strip()
        self.save_config(raw)
        if a.get("manage") and a.get("autostart") and not self.alpha_admin().running():
            try:
                self.alpha_admin().start()
            except Exception as e:
                log.warning("Alpha autostart after save failed: %s", e)
        return {"ok": True, "message": "Настройки Alpha сохранены"}

    def weflow_info(self) -> dict:
        info = self.weflow_admin().info()
        plain = info.pop("token_plain", "")
        raw = self.read_raw_config()
        info["token_matches"] = bool(plain) and plain == raw.get("weflow_token")
        client = self.weflow_client()
        info["api_up"] = client.health()
        info["block_delete"] = None
        if info["api_up"]:
            try:
                info["block_delete"] = client.block_delete_status()
            except Exception as e:
                info["block_delete_error"] = str(e)
        info["panel_weflow_url"] = raw.get("weflow_url") or ""
        info["weflow_exe_override"] = raw.get("weflow_exe") or ""
        info["weflow_config_override"] = raw.get("weflow_config_path") or ""
        return info

    def weflow_apply(self, data: dict) -> dict:
        restart = bool(data.pop("restart", True))
        sync = bool(data.pop("sync", True))
        admin = self.weflow_admin()
        before = admin.info()
        result = admin.apply(data, restart=restart)
        msg = "Настройки WeFlow сохранены"
        if result["restarted"]:
            msg += ", WeFlow перезапускается"
            self._wait_weflow_api()
        if sync:
            raw = self.read_raw_config()
            port = data.get("httpApiPort") or (before.get("settings") or {}).get("httpApiPort") or 5031
            raw["weflow_url"] = f"http://127.0.0.1:{int(port)}"
            token = (data.get("httpApiToken") or "").strip() or before.get("token_plain") or ""
            if token:
                raw["weflow_token"] = token
            self.save_config(raw)
            msg += "; адрес и токен moments2tg обновлены"
        log.info("WeFlow settings changed from the panel: %s", ", ".join(result["changed"]) or "-")
        return {"ok": True, "message": msg, **result}

    def _wait_weflow_api(self, timeout: float = 90):
        def wait():
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(3)
                try:
                    if self.weflow_client().health():
                        log.info("WeFlow API is up")
                        return
                except Exception:
                    pass
            log.warning("WeFlow API did not come up within %ss (WeFlow may be waiting for a password "
                        "or the database is not connected)", int(timeout))
        threading.Thread(target=wait, daemon=True).start()

    def weflow_process(self, action: str) -> dict:
        admin = self.weflow_admin()
        if action == "start":
            admin.start()
            self._wait_weflow_api()
            return {"ok": True, "message": "WeFlow запускается"}
        if action == "stop":
            admin.stop()
            return {"ok": True, "message": "WeFlow остановлен"}
        if action == "restart":
            admin.stop()
            admin.start()
            self._wait_weflow_api()
            return {"ok": True, "message": "WeFlow перезапускается"}
        raise ValueError(action)

    def weflow_block_delete(self, enabled: bool) -> dict:
        self.weflow_client().set_block_delete(enabled)
        return {"ok": True, "message": "Защита от удаления Moments " + ("включена" if enabled else "выключена")}

    def test_weflow(self) -> dict:
        raw = self.read_raw_config()
        weflow = core.WeFlow(raw.get("weflow_url") or "http://127.0.0.1:5031", raw.get("weflow_token", ""))
        if not weflow.health():
            raise RuntimeError("WeFlow не отвечает (приложение не запущено или API-сервис выключен)")
        n = len(weflow.sns_usernames())  # also checks the token
        return {"message": f"WeFlow на связи, аккаунтов с Moments: {n}"}


def make_handler(panel: Panel):
    allowed_hosts = {f"127.0.0.1:{panel.port}", f"localhost:{panel.port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "moments2tg-panel"

        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            # blocks DNS-rebinding: only our own origin may talk to the panel
            if self.headers.get("Host") not in allowed_hosts:
                self._json({"error": "bad host"}, 403)
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            try:
                if url.path in ("/", "/index.html"):
                    body = PANEL_HTML.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/api/status":
                    self._json(panel.status())
                elif url.path == "/api/config":
                    self._json(panel.public_config())
                elif url.path == "/api/alpha":
                    self._json(panel.alpha_info())
                elif url.path == "/api/weflow":
                    self._json(panel.weflow_info())
                elif url.path == "/api/accounts":
                    self._json(panel.accounts())
                elif url.path == "/api/log":
                    after = int((parse_qs(url.query).get("after") or ["0"])[0])
                    self._json({"lines": panel.ring.since(after)})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            if not self._host_ok():
                return
            # a custom header can't be sent cross-site without CORS preflight -> CSRF guard
            if self.headers.get("X-Panel") != "1":
                self._json({"error": "missing X-Panel header"}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                self._json({"error": "bad json"}, 400)
                return
            w = panel.worker
            try:
                path = urlparse(self.path).path
                if path == "/api/config":
                    panel.save_config(data)
                    self._json({"ok": True, "message": "Настройки сохранены"})
                elif path == "/api/start":
                    w.start()
                    self._json({"ok": True, "message": "Запущено"})
                elif path == "/api/stop":
                    w.stop()
                    self._json({"ok": True, "message": "Остановка…"})
                elif path == "/api/poll":
                    w.poll_now(force_refresh=bool(data.get("refresh")))
                    self._json({"ok": True, "message": "Проход запущен"})
                elif path == "/api/test-telegram":
                    panel.test_telegram()
                    self._json({"ok": True, "message": "Тестовое сообщение отправлено"})
                elif path == "/api/test-weflow":
                    self._json({"ok": True, **panel.test_weflow()})
                elif path == "/api/telegram/check":
                    self._json(panel.telegram_check(data))
                elif path == "/api/telegram/discover":
                    self._json(panel.telegram_discover(data))
                elif path == "/api/autostart":
                    st = autostart.enable() if data.get("enabled") else autostart.disable()
                    log.info("autostart %s: %s", "enabled" if st["enabled"] else "disabled", st["path"])
                    self._json({"ok": True, "message": "Автозапуск " + ("включён" if st["enabled"] else "выключен"), **st})
                elif path == "/api/install-refresh":
                    self._json(panel.install_refresh())
                elif path == "/api/weflow/settings":
                    self._json(panel.weflow_apply(data))
                elif path in ("/api/weflow/start", "/api/weflow/stop", "/api/weflow/restart"):
                    self._json(panel.weflow_process(path.rsplit("/", 1)[-1]))
                elif path in ("/api/alpha/start", "/api/alpha/stop", "/api/alpha/restart"):
                    self._json(panel.alpha_process(path.rsplit("/", 1)[-1]))
                elif path == "/api/alpha/settings":
                    self._json(panel.alpha_settings(data))
                elif path == "/api/weflow/block-delete":
                    self._json(panel.weflow_block_delete(bool(data.get("enabled"))))
                elif path == "/api/weflow/paths":
                    raw = panel.read_raw_config()
                    for k in ("weflow_exe", "weflow_config_path"):
                        raw[k] = str(data.get(k) or "").strip()
                    panel.save_config(raw)
                    self._json({"ok": True, "message": "Пути сохранены"})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return Handler


def serve(config_path: Path, port: int = 8765, open_browser: bool = True):
    panel = Panel(config_path, port)
    try:
        raw = panel.read_raw_config()
        alpha = raw.get("alpha", {})
        if alpha.get("manage") and alpha.get("autostart") and not panel.alpha_admin().running():
            log.info("autostart: launching Alpha")
            panel.alpha_admin().start()
        if raw.get("panel_autostart") and config_path.exists():
            panel.worker.start()
    except Exception as e:
        log.warning("autostart skipped: %s", e)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(panel))
    url = f"http://127.0.0.1:{port}/"
    log.info("control panel: %s", url)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    finally:
        panel.worker.stop()
