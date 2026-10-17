"""
Optional Moments refresher: makes desktop WeChat download fresh Moments of the
selected friends by opening their Moments page through UI automation
(pywechat / pyweixin, WeChat 4.1+). WeChat stores what it downloaded in its
local sns database, where WeFlow (and therefore moments2tg) can see it.

This drives the real mouse and keyboard, so it only works in an unlocked,
interactive desktop session.
"""

import ctypes
import logging
import time

log = logging.getLogger("moments2tg.refresher")


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def seconds_since_user_input() -> float:
    """Time since the last keyboard/mouse input on this Windows session."""
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return float("inf")
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


class MomentsRefresher:
    def __init__(self, cfg: dict):
        self._ui = None
        self.interval = cfg.get("interval_sec", 900)
        self.load_wait = cfg.get("load_wait_sec", 6)
        self.scroll_pages = cfg.get("scroll_pages", 0)
        self.pause_between = cfg.get("pause_between_friends_sec", 3)
        self.idle_required = cfg.get("skip_if_user_active_sec", 60)
        self.search_pages = cfg.get("search_pages", 0)
        self.is_maximize = cfg.get("is_maximize", False)
        self.minimize_after = cfg.get("minimize_weixin_after", True)
        self.last_run = 0.0

    def _load(self):
        """pyweixin detects the WeChat UI language at import time, so import it
        only when WeChat is (most likely) already running."""
        if self._ui is not None:
            return self._ui
        try:
            from packaging import version
            from pyweixin import GlobalConfig, Navigator, Tools
            from pyweixin.Uielements import Buttons, Lists
        except ImportError as e:
            raise RuntimeError(
                f"refresh.enabled is true but pywechat is not installed ({e}).\n"
                "Install it: venv\\Scripts\\python.exe -m pip install -r moments2tg\\requirements-refresh.txt"
            )
        self._ui = dict(version=version, GlobalConfig=GlobalConfig, Navigator=Navigator,
                        Tools=Tools, Buttons=Buttons, Lists=Lists)
        log.info("pyweixin loaded: WeChat %s, language %s", GlobalConfig.Version, GlobalConfig.language)
        return self._ui

    def due(self) -> bool:
        return time.time() - self.last_run >= self.interval

    def user_is_active(self) -> bool:
        return self.idle_required > 0 and seconds_since_user_input() < self.idle_required

    def refresh_all(self, friends: list[str]) -> int:
        """Opens Moments of every friend; returns how many were opened successfully."""
        if self.user_is_active():
            log.info("user is active at the computer, postponing Moments refresh")
            return 0
        self.last_run = time.time()
        self._load()
        ok = 0
        main_window = None
        for i, friend in enumerate(friends):
            if i and self.user_is_active():
                log.info("user became active, stopping Moments refresh")
                break
            try:
                main_window = self._refresh_one(friend) or main_window
                ok += 1
            except Exception as e:
                log.warning("could not open Moments of %r: %s", friend, e)
            time.sleep(self.pause_between)
        if main_window is not None and self.minimize_after:
            try:
                main_window.minimize()
            except Exception:
                pass
        log.info("Moments refresh: %d/%d friends opened", ok, len(friends))
        return ok

    def _refresh_one(self, friend: str):
        moments_window, main_window = self._ui['Navigator'].open_friend_moments(
            friend=friend, search_pages=self.search_pages, is_maximize=self.is_maximize)
        try:
            # give WeChat time to download the feed from the server
            time.sleep(self.load_wait)
            if self.scroll_pages:
                moments_list = moments_window.child_window(**self._ui['Lists'].MomentsList)
                self._ui['Tools'].activate_momentsList(moments_list)
                for _ in range(self.scroll_pages):
                    moments_list.type_keys("{PGDN}", pause=0.3)
                    time.sleep(self.load_wait / 2)
        finally:
            self._close(moments_window, main_window)
        return main_window

    def _close(self, moments_window, main_window):
        # same logic as pyweixin's Moments.dump_friend_posts:
        # <=4.1.8 friend Moments is a separate window, newer versions embed it in the main window
        try:
            v = self._ui["version"]
            if v.parse(self._ui["GlobalConfig"].Version) <= v.parse("4.1.8"):
                moments_window.close()
            else:
                main_window.child_window(**self._ui['Buttons'].CloseButton).click()
        except Exception as e:
            log.debug("closing Moments view failed: %s", e)
