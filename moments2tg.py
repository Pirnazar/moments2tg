"""
Moments (WeChat 朋友圈) -> Telegram channel.

Periodically polls the local WeFlow HTTP API (/api/v1/sns/timeline) for the
selected accounts and reposts new posts (text + photos/videos) to a Telegram
channel through the Bot API. Already sent post ids are kept in state.json.

Usage:
    python moments2tg.py              # run forever
    python moments2tg.py --once       # one polling pass and exit
    python moments2tg.py --list       # show accounts that have Moments (wxid + name)
    python moments2tg.py --test       # send a test message to the channel
    python moments2tg.py --refresh-test   # open Moments of the configured friends once (pywechat)
"""

import argparse
import html
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
MEDIA_GROUP_LIMIT = 10
PHOTO_SIZE_LIMIT = 10 * 1024 * 1024
UPLOAD_SIZE_LIMIT = 50 * 1024 * 1024
SEEN_KEEP = 20000

log = logging.getLogger("moments2tg")


# --------------------------------------------------------------------------- config / state

class ConfigError(Exception):
    pass


def load_config(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Config not found: {path}\nCopy config.example.json to config.json and fill it in.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("weflow_url", "http://127.0.0.1:5031")
    cfg.setdefault("poll_interval_sec", 120)
    cfg.setdefault("fetch_limit", 20)
    cfg.setdefault("skip_existing_on_first_run", True)
    cfg.setdefault("include_comments", False)
    cfg.setdefault("live_photo_as_video", False)
    cfg.setdefault("telegram_proxy", None)
    cfg.setdefault("telegram_api_base", "https://api.telegram.org")
    cfg.setdefault("state_file", "state.json")
    refresh = cfg.setdefault("refresh", {})
    refresh.setdefault("enabled", False)
    refresh.setdefault("after_refresh_wait_sec", 10)
    # secrets may come from the environment instead of the file
    cfg["weflow_token"] = os.environ.get("WEFLOW_TOKEN") or cfg.get("weflow_token", "")
    cfg["telegram_bot_token"] = os.environ.get("TG_BOT_TOKEN") or cfg.get("telegram_bot_token", "")
    cfg["telegram_chat_id"] = os.environ.get("TG_CHAT_ID") or cfg.get("telegram_chat_id", "")
    # usernames: list of "wxid" or {"username": "wxid", "label": "...", "wechat_name": "..."}
    users = []
    for item in cfg.get("usernames", []):
        if isinstance(item, str):
            users.append({"username": item, "label": None, "wechat_name": None})
        elif isinstance(item, dict) and item.get("username"):
            users.append({"username": item["username"], "label": item.get("label"),
                          "wechat_name": item.get("wechat_name")})
    cfg["usernames"] = users
    return cfg


class State:
    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, int] = {}
        self.initialized: set[str] = set()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.seen = data.get("seen", {})
            self.initialized = set(data.get("initialized_users", []))

    def is_seen(self, post_id: str) -> bool:
        return post_id in self.seen

    def mark(self, post_id: str, create_time: int):
        self.seen[post_id] = int(create_time or 0)

    def save(self):
        if len(self.seen) > SEEN_KEEP:
            newest = sorted(self.seen.items(), key=lambda kv: kv[1], reverse=True)[:SEEN_KEEP]
            self.seen = dict(newest)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "seen": self.seen,
            "initialized_users": sorted(self.initialized),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# --------------------------------------------------------------------------- WeFlow API

class WeFlow:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, **params):
        r = self.s.get(f"{self.base}{path}", params=params, timeout=60)
        if r.status_code == 401:
            raise RuntimeError("WeFlow API: 401 Unauthorized - check weflow_token (Settings -> API service)")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"WeFlow API error: {data.get('error')}")
        return data

    def _post(self, path: str, method: str = "POST"):
        r = self.s.request(method, f"{self.base}{path}", json={}, timeout=60)
        if r.status_code == 401:
            raise RuntimeError("WeFlow API: 401 Unauthorized - check weflow_token (Settings -> API service)")
        data = r.json()
        if not r.ok or (isinstance(data, dict) and data.get("success") is False):
            raise RuntimeError(f"WeFlow API error: {data.get('error') if isinstance(data, dict) else r.status_code}")
        return data

    def block_delete_status(self) -> bool:
        return bool(self._get("/api/v1/sns/block-delete/status").get("installed"))

    def set_block_delete(self, enabled: bool):
        self._post("/api/v1/sns/block-delete/" + ("install" if enabled else "uninstall"))

    def health(self) -> bool:
        try:
            return self.s.get(f"{self.base}/health", timeout=5).ok
        except requests.RequestException:
            return False

    def timeline(self, username: str, limit: int) -> list[dict]:
        data = self._get("/api/v1/sns/timeline", usernames=username, limit=limit,
                         offset=0, media=1, replace=0, inline=0)
        return data.get("timeline") or []

    def sns_usernames(self) -> list[str]:
        return self._get("/api/v1/sns/usernames").get("usernames") or []

    def contacts(self) -> dict[str, dict]:
        data = self._get("/api/v1/contacts", limit=100000)
        return {c["username"]: c for c in data.get("contacts") or [] if c.get("username")}

    def download(self, url: str) -> tuple[bytes, str]:
        r = self.s.get(url, timeout=180)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "application/octet-stream")


# --------------------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, bot_token: str, chat_id: str, proxy: str | None = None,
                 api_base: str = "https://api.telegram.org"):
        if not bot_token or not chat_id:
            raise ConfigError("telegram_bot_token and telegram_chat_id must be set")
        self.api = f"{(api_base or 'https://api.telegram.org').rstrip('/')}/bot{bot_token}"
        self.chat_id = chat_id
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def call(self, method: str, data: dict | None = None, files: dict | None = None, attempts: int = 5):
        for attempt in range(1, attempts + 1):
            try:
                r = self.s.post(f"{self.api}/{method}", data=data, files=files, timeout=300)
                body = r.json()
            except (requests.RequestException, ValueError) as e:
                log.warning("Telegram %s failed (%s), attempt %d/%d", method, e, attempt, attempts)
                time.sleep(min(60, 5 * attempt))
                continue
            if body.get("ok"):
                return body["result"]
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and retry_after:
                log.warning("Telegram flood limit, sleeping %ss", retry_after)
                time.sleep(int(retry_after) + 1)
                continue
            if r.status_code >= 500:
                time.sleep(min(60, 5 * attempt))
                continue
            raise TelegramError(f"{method}: {body.get('description')}")
        raise TelegramError(f"{method}: giving up after {attempts} attempts")

    def send_text(self, text: str):
        for chunk in split_text(text, MESSAGE_LIMIT):
            self.call("sendMessage", {"chat_id": self.chat_id, "text": chunk,
                                      "parse_mode": "HTML", "disable_web_page_preview": "true"})

    def send_media(self, items: list[dict], caption: str | None):
        """items: [{"kind": "photo"|"video"|"document", "data": bytes, "name": str}]"""
        if len(items) == 1:
            it = items[0]
            method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[it["kind"]]
            data = {"chat_id": self.chat_id}
            if caption:
                data.update(caption=caption, parse_mode="HTML")
            if it["kind"] == "video":
                data["supports_streaming"] = "true"
            self.call(method, data, files={it["kind"]: (it["name"], it["data"])})
            return

        # photos/videos can be mixed in an album, documents must go in their own album
        visual = [i for i in items if i["kind"] in ("photo", "video")]
        docs = [i for i in items if i["kind"] == "document"]
        groups = [visual[i:i + MEDIA_GROUP_LIMIT] for i in range(0, len(visual), MEDIA_GROUP_LIMIT)]
        groups += [docs[i:i + MEDIA_GROUP_LIMIT] for i in range(0, len(docs), MEDIA_GROUP_LIMIT)]
        for gi, group in enumerate(groups):
            if len(group) == 1:
                self.send_media(group, caption if gi == 0 else None)
                continue
            media, files = [], {}
            for idx, it in enumerate(group):
                field = f"file{idx}"
                files[field] = (it["name"], it["data"])
                entry = {"type": it["kind"], "media": f"attach://{field}"}
                if gi == 0 and idx == 0 and caption:
                    entry.update(caption=caption, parse_mode="HTML")
                if it["kind"] == "video":
                    entry["supports_streaming"] = True
                media.append(entry)
            self.call("sendMediaGroup", {"chat_id": self.chat_id, "media": json.dumps(media)}, files=files)


class TelegramError(Exception):
    pass


def split_text(text: str, limit: int) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


# --------------------------------------------------------------------------- formatting

def is_video_url(url: str) -> bool:
    u = (url or "").lower()
    return "video" in u or ".mp4" in u


def format_post(post: dict, label: str | None, include_comments: bool) -> str:
    esc = html.escape
    name = label or post.get("nickname") or post.get("username")
    ts = int(post.get("createTime") or 0)
    when = datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else ""
    lines = [f"<b>{esc(name)}</b>" + (f"  ·  {when}" if when else "")]

    text = (post.get("contentDesc") or "").strip()
    if text:
        lines += ["", esc(text)]

    if post.get("linkUrl"):
        title = post.get("linkTitle") or post["linkUrl"]
        lines += ["", f'🔗 <a href="{esc(post["linkUrl"], quote=True)}">{esc(title)}</a>']

    loc = post.get("location") or {}
    place = loc.get("poiName") or loc.get("label") or loc.get("poiAddressName") or loc.get("city")
    if place:
        lines += ["", f"📍 {esc(place)}"]

    if include_comments:
        likes = post.get("likes") or []
        if likes:
            lines += ["", "❤️ " + esc(", ".join(likes))]
        comments = post.get("comments") or []
        if comments:
            lines.append("")
            for c in comments:
                who = esc(c.get("nickname") or "")
                if c.get("refNickname"):
                    who += f" → {esc(c['refNickname'])}"
                lines.append(f"💬 <b>{who}</b>: {esc(c.get('content') or '')}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- core

def collect_media(weflow: WeFlow, post: dict, live_as_video: bool) -> tuple[list[dict], int]:
    """Downloads all post media through the WeFlow proxy (which decrypts it)."""
    items, failed = [], 0
    is_video_post = post.get("type") == 15
    for idx, m in enumerate(post.get("media") or []):
        src = m.get("proxyUrl") or m.get("resolvedUrl") or m.get("url")
        video = is_video_post or is_video_url(m.get("rawUrl") or m.get("url") or "")
        live = m.get("livePhoto") if isinstance(m.get("livePhoto"), dict) else None
        if live and live_as_video:
            src = live.get("proxyUrl") or live.get("resolvedUrl") or live.get("url") or src
            video = True
        if not src:
            failed += 1
            continue
        try:
            data, ctype = weflow.download(src)
        except Exception as e:
            log.warning("media download failed for post %s: %s", post.get("id"), e)
            failed += 1
            continue
        if not data:
            failed += 1
            continue
        if ctype.startswith("video/"):
            video = True
        elif ctype.startswith("image/"):
            video = False
        if len(data) > UPLOAD_SIZE_LIMIT:
            log.warning("media %d of post %s is too large for Bot API (%d bytes)", idx, post.get("id"), len(data))
            failed += 1
            continue
        if video:
            kind, ext = "video", "mp4"
        else:
            kind = "photo" if len(data) <= PHOTO_SIZE_LIMIT else "document"
            ext = (ctype.split("/")[-1].split(";")[0] or "jpg").replace("jpeg", "jpg")
        items.append({"kind": kind, "data": data, "name": f"{post.get('id')}_{idx}.{ext}"})
    return items, failed


def send_post(tg: Telegram, weflow: WeFlow, post: dict, label: str | None, cfg: dict):
    text = format_post(post, label, cfg["include_comments"])
    items, failed = collect_media(weflow, post, cfg["live_photo_as_video"])
    if failed:
        text += f"\n\n<i>⚠️ не удалось загрузить медиа: {failed}</i>"

    if not items:
        tg.send_text(text)
        return

    caption = text if len(text) <= CAPTION_LIMIT else None
    try:
        tg.send_media(items, caption)
    except TelegramError as e:
        # e.g. PHOTO_INVALID_DIMENSIONS - retry everything as plain files
        log.warning("send_media failed (%s), retrying as documents", e)
        for it in items:
            it["kind"] = "document"
        tg.send_media(items, caption)
    if caption is None:
        tg.send_text(text)


def poll_once(cfg: dict, weflow: WeFlow, tg: Telegram, state: State, on_sent=None) -> int:
    sent = 0
    for user in cfg["usernames"]:
        username = user["username"]
        try:
            posts = weflow.timeline(username, cfg["fetch_limit"])
        except Exception as e:
            log.error("timeline for %s failed: %s", username, e)
            continue
        posts = [p for p in posts if p.get("id") and p.get("username", username) == username]

        if username not in state.initialized:
            state.initialized.add(username)
            if cfg["skip_existing_on_first_run"]:
                for p in posts:
                    state.mark(str(p["id"]), p.get("createTime"))
                state.save()
                log.info("%s: first run, marked %d existing posts as seen", username, len(posts))
                continue

        new_posts = sorted((p for p in posts if not state.is_seen(str(p["id"]))),
                           key=lambda p: int(p.get("createTime") or 0))
        for p in new_posts:
            try:
                send_post(tg, weflow, p, user["label"], cfg)
            except Exception as e:
                log.error("failed to send post %s of %s: %s", p["id"], username, e)
                break  # keep order: retry this post on next pass
            state.mark(str(p["id"]), p.get("createTime"))
            state.save()
            sent += 1
            log.info("sent post %s of %s", p["id"], username)
            if on_sent:
                on_sent(p, user)
    return sent


def resolve_wechat_names(weflow: WeFlow, users: list[dict]) -> list[str]:
    """Names to type into WeChat search for the pywechat refresher: explicit
    wechat_name, otherwise the contact's remark / nickname from WeFlow."""
    missing = [u for u in users if not u["wechat_name"]]
    if missing:
        try:
            contacts = weflow.contacts()
        except Exception as e:
            log.warning("cannot load contacts from WeFlow to resolve WeChat names: %s", e)
            contacts = {}
        for u in missing:
            c = contacts.get(u["username"], {})
            name = c.get("remark") or c.get("nickname") or c.get("displayName")
            if name:
                u["wechat_name"] = name
            else:
                log.warning("%s: no WeChat name known, set \"wechat_name\" in config to refresh it",
                            u["username"])
    return [u["wechat_name"] for u in users if u["wechat_name"]]


def cmd_list(weflow: WeFlow):
    usernames = weflow.sns_usernames()
    try:
        contacts = weflow.contacts()
    except Exception:
        contacts = {}
    print(f"{len(usernames)} accounts with Moments:\n")
    for u in sorted(usernames):
        c = contacts.get(u, {})
        name = c.get("remark") or c.get("displayName") or c.get("nickname") or ""
        print(f"  {u:<40} {name}")


class Worker:
    """Polling loop with start/stop and status, shared by the CLI and the control panel."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pass_lock = threading.Lock()
        self._force_refresh = False
        self._cfg_mtime = None
        self.cfg = self.weflow = self.tg = self.refresher = self.state = None
        self._wechat_names = None
        self.history: deque = deque(maxlen=100)
        self.status = {
            "running": False, "busy": None, "started_at": None,
            "last_pass_at": None, "last_pass_sent": 0, "total_sent": 0,
            "next_pass_at": None, "weflow_ok": None, "last_error": None,
            "last_refresh_at": None, "last_refresh_result": None,
        }

    # ----- components

    def ensure_loaded(self):
        """(Re)builds clients when config.json changed; keeps refresher timing."""
        mtime = self.config_path.stat().st_mtime if self.config_path.exists() else None
        if self.cfg is not None and mtime == self._cfg_mtime:
            return
        cfg = load_config(self.config_path)
        self.weflow = WeFlow(cfg["weflow_url"], cfg["weflow_token"])
        self.tg = None
        if cfg["telegram_bot_token"] and cfg["telegram_chat_id"]:
            self.tg = Telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], cfg["telegram_proxy"],
                               cfg["telegram_api_base"])
        last_run = self.refresher.last_run if self.refresher else 0.0
        self.refresher = None
        if cfg["refresh"]["enabled"] and sys.platform != "win32":
            log.warning("Moments refresh (pywechat) works on Windows only, ignoring refresh.enabled")
        elif cfg["refresh"]["enabled"]:
            from refresher import MomentsRefresher
            self.refresher = MomentsRefresher(cfg["refresh"])
            self.refresher.last_run = last_run
        self.state = State((BASE_DIR / cfg["state_file"]).resolve())
        self._wechat_names = None
        self.cfg = cfg
        self._cfg_mtime = mtime
        log.info("config loaded: %d accounts, poll every %ss%s", len(cfg["usernames"]),
                 cfg["poll_interval_sec"],
                 f", Moments refresh every {self.refresher.interval}s" if self.refresher else "")

    def wechat_names(self) -> list[str]:
        if self._wechat_names is None and self.weflow.health():
            self._wechat_names = resolve_wechat_names(self.weflow, self.cfg["usernames"])
        if self._wechat_names is not None:
            return self._wechat_names
        return [u["wechat_name"] for u in self.cfg["usernames"] if u["wechat_name"]]

    # ----- actions

    def _on_sent(self, post: dict, user: dict):
        self.status["total_sent"] += 1
        self.history.appendleft({
            "id": str(post.get("id")),
            "username": user["username"],
            "name": user["label"] or post.get("nickname") or user["username"],
            "createTime": int(post.get("createTime") or 0),
            "text": (post.get("contentDesc") or "")[:200],
            "media": len(post.get("media") or []),
            "sent_at": int(time.time()),
        })

    def refresh_moments(self, force: bool = False) -> int:
        """Opens friends' Moments in WeChat (pywechat). force: even if disabled / user is active."""
        self.ensure_loaded()
        refresher = self.refresher
        if refresher is None and force:
            from refresher import MomentsRefresher
            refresher = MomentsRefresher(self.cfg["refresh"])
        if refresher is None:
            return 0
        names = self.wechat_names()
        if not names:
            log.warning("Moments refresh: no WeChat names to open")
            self.status["last_refresh_result"] = "нет имён для поиска"
            return 0
        saved = refresher.idle_required
        if force:
            refresher.idle_required = 0
        self.status["busy"] = "refresh"
        try:
            ok = refresher.refresh_all(names)
        finally:
            refresher.idle_required = saved
            self.status["busy"] = None
        self.status["last_refresh_at"] = int(time.time())
        self.status["last_refresh_result"] = f"{ok}/{len(names)}"
        if ok:
            # let WeChat write the downloaded posts into its database
            time.sleep(self.cfg["refresh"]["after_refresh_wait_sec"])
        return ok

    def run_pass(self, force_refresh: bool = False) -> int:
        with self._pass_lock:
            try:
                self.ensure_loaded()
                if not self.cfg["usernames"]:
                    raise ConfigError("no accounts selected")
                if self.tg is None:
                    raise ConfigError("telegram_bot_token and telegram_chat_id must be set")
                if force_refresh or (self.refresher and self.refresher.due()):
                    try:
                        self.refresh_moments(force=force_refresh)
                    except Exception as e:
                        log.exception("Moments refresh failed")
                        self.status["last_refresh_result"] = f"ошибка: {e}"
                self.status["weflow_ok"] = self.weflow.health()
                if not self.status["weflow_ok"]:
                    raise ConfigError(f"WeFlow API is not reachable at {self.cfg['weflow_url']} "
                                      "(is WeFlow running with API service on?)")
                self.status["busy"] = "poll"
                n = poll_once(self.cfg, self.weflow, self.tg, self.state, on_sent=self._on_sent)
                self.status["last_pass_sent"] = n
                self.status["last_error"] = None
                if n:
                    log.info("pass done, sent %d posts", n)
                return n
            except Exception as e:
                if isinstance(e, ConfigError):
                    log.warning("%s", e)
                else:
                    log.exception("poll pass failed")
                self.status["last_error"] = str(e)
                return 0
            finally:
                self.status["busy"] = None
                self.status["last_pass_at"] = int(time.time())

    def loop(self, once: bool = False):
        self.status.update(running=True, started_at=int(time.time()))
        try:
            while not self._stop.is_set():
                force, self._force_refresh = self._force_refresh, False
                self.run_pass(force_refresh=force)
                if once:
                    break
                interval = (self.cfg or {}).get("poll_interval_sec", 120)
                self.status["next_pass_at"] = int(time.time() + interval)
                self._wake.wait(interval)
                self._wake.clear()
        finally:
            self.status.update(running=False, next_pass_at=None)

    # ----- background control (panel)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._thread_main, name="moments2tg-worker", daemon=True)
        self._thread.start()

    def _thread_main(self):
        # the refresher drives WeChat through UI Automation, which is COM
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        except Exception:
            pass
        self.loop()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poll_now(self, force_refresh: bool = False):
        """Run a pass right away: wakes the loop, or runs one pass in the background if stopped."""
        if self.running:
            self._force_refresh = self._force_refresh or force_refresh
            self._wake.set()
        elif not self._pass_lock.locked():
            threading.Thread(target=self._one_shot, args=(force_refresh,), daemon=True).start()

    def _one_shot(self, force_refresh: bool):
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        except Exception:
            pass
        self.run_pass(force_refresh=force_refresh)


def setup_logging():
    # Chinese/emoji nicknames must not crash a cp866/cp1251 Windows console
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(BASE_DIR / "moments2tg.log", encoding="utf-8")])


def main():
    ap = argparse.ArgumentParser(description="WeChat Moments -> Telegram via WeFlow API")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--once", action="store_true", help="single polling pass")
    ap.add_argument("--list", action="store_true", help="list accounts that have Moments")
    ap.add_argument("--test", action="store_true", help="send a test message to Telegram")
    ap.add_argument("--refresh-test", action="store_true",
                    help="open Moments of the configured friends once via pywechat and exit")
    ap.add_argument("--panel", action="store_true", help="start the web control panel")
    ap.add_argument("--port", type=int, default=8765, help="control panel port")
    ap.add_argument("--no-browser", action="store_true", help="don't open the panel in a browser")
    args = ap.parse_args()

    setup_logging()

    if args.panel:
        import panel
        panel.serve(Path(args.config), args.port, open_browser=not args.no_browser)
        return

    try:
        cfg = load_config(Path(args.config))
        if args.list:
            cmd_list(WeFlow(cfg["weflow_url"], cfg["weflow_token"]))
            return

        worker = Worker(Path(args.config))
        if args.refresh_test:
            worker.ensure_loaded()
            print(f"Opening Moments of: {', '.join(worker.wechat_names()) or '-'}")
            worker.refresh_moments(force=True)
            return

        tg = Telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], cfg["telegram_proxy"],
                               cfg["telegram_api_base"])
        if args.test:
            tg.send_text("✅ moments2tg: тестовое сообщение")
            print("Test message sent.")
            return

        if not cfg["usernames"]:
            raise ConfigError("No usernames in config. Run with --list to see available wxids.")
    except ConfigError as e:
        sys.exit(str(e))

    worker.loop(once=args.once)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
