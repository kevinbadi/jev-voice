"""macOS execution layer. Everything here is plain code: no model involved."""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

# ---------------------------------------------------------------- apps

APP_DIRS = [
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
]

# Apps that are almost always worth having in the choice set even if not installed
# in /Applications (they live in system locations or are aliases people say aloud).
ALWAYS_APPS = ["Finder", "Safari", "Terminal", "System Settings", "Notes", "Messages",
               "Mail", "Calendar", "Music", "Reminders", "Photos", "Calculator",
               "TextEdit", "Preview", "Activity Monitor", "FaceTime", "Maps"]


@lru_cache(maxsize=1)
def installed_apps() -> list[str]:
    names: set[str] = set(ALWAYS_APPS)
    for d in APP_DIRS:
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.suffix == ".app":
                names.add(p.stem)
    return sorted(names, key=str.lower)


def frontmost_pid() -> int:
    """Live focused application. NSWorkspace.frontmostApplication() goes stale in a process
    without a running main loop; the AX system-wide element is always current."""
    try:
        import ApplicationServices as AS  # type: ignore

        err, app = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
        if err == 0 and app is not None:
            err, pid = AS.AXUIElementGetPid(app, None)
            if err == 0:
                return int(pid)
    except Exception:
        pass
    try:
        from AppKit import NSWorkspace  # type: ignore

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(app.processIdentifier()) if app else 0
    except Exception:
        return 0


def frontmost_app() -> str:
    try:
        from AppKit import NSRunningApplication  # type: ignore

        pid = frontmost_pid()
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
        return str(app.localizedName()) if app else ""
    except Exception:
        return ""


def open_app(name: str) -> None:
    subprocess.Popen(["open", "-a", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _activate(name: str) -> bool:
    """Bring a running app to the front. `open -a`, AppleScript `activate` and NSRunningApplication
    are all deferred by macOS while the user is actively typing or clicking in another app.
    The Carbon SetFrontProcessWithOptions call is honoured immediately (it is what window
    managers use); AXFrontmost is the fallback."""
    try:
        import ctypes

        from AppKit import NSWorkspace  # type: ignore

        pid = next((int(a.processIdentifier()) for a in NSWorkspace.sharedWorkspace().runningApplications()
                    if str(a.localizedName()).lower() == name.lower()), None)
        if pid is None:
            return False
        try:
            lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")

            class PSN(ctypes.Structure):
                _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]

            psn = PSN()
            if lib.GetProcessForPID(ctypes.c_int(pid), ctypes.byref(psn)) == 0:
                if lib.SetFrontProcessWithOptions(ctypes.byref(psn), ctypes.c_uint32(1)) == 0:  # front window only
                    return True
        except Exception:
            pass
        import ApplicationServices as AS  # type: ignore

        return AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(pid), "AXFrontmost", True) == 0
    except Exception:
        return False


def focus_app(name: str, timeout: float = 2.0) -> bool:
    """Open/activate an app and wait until it is frontmost (so keystrokes land in it)."""
    if frontmost_app().lower() == name.lower():
        return True
    open_app(name)
    t0 = time.time()
    last_ax = 0.0
    while time.time() - t0 < timeout:
        if frontmost_app().lower() == name.lower():
            time.sleep(0.15)  # let the window take key focus
            return True
        if time.time() - last_ax > 0.25:
            _activate(name)
            last_ax = time.time()
        time.sleep(0.05)
    return False


def open_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- sites / search

SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google_calendar": "https://calendar.google.com",
    "google_drive": "https://drive.google.com",
    "google_docs": "https://docs.google.com",
    "google_maps": "https://maps.google.com",
    "github": "https://github.com",
    "twitter_x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "notion": "https://www.notion.so",
    "spotify_web": "https://open.spotify.com",
    "linkedin": "https://www.linkedin.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "facebook_marketplace": "https://www.facebook.com/marketplace",
    "wikipedia": "https://en.wikipedia.org",
    "hacker_news": "https://news.ycombinator.com",
    "twitch": "https://www.twitch.tv",
    "figma": "https://www.figma.com",
    "typesafe_console": "https://console.typesafe.ai",
}

SEARCH_ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "google_maps": "https://www.google.com/maps/search/{q}",
    "twitter_x": "https://x.com/search?q={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "perplexity": "https://www.perplexity.ai/search?q={q}",
}


def web_search(engine: str, query: str) -> None:
    tpl = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"])
    open_url(tpl.format(q=quote_plus(query)))


# ---------------------------------------------------------------- keyboard

def _osascript(script: str) -> str:
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    return out.stdout.strip()


def type_text(text: str) -> None:
    # System Events keystroke handles Unicode; escape for AppleScript string literal.
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    _osascript(f'tell application "System Events" to keystroke "{esc}"')


# name -> (key or key code, modifiers)
# Use "code:NN" for key codes, otherwise a literal character.
SHORTCUTS: dict[str, tuple[str, list[str]]] = {
    "enter": ("code:36", []),
    "escape": ("code:53", []),
    "tab": ("code:48", []),
    "space": ("code:49", []),
    "backspace": ("code:51", []),
    "delete_forward": ("code:117", []),
    "arrow_up": ("code:126", []),
    "arrow_down": ("code:125", []),
    "arrow_left": ("code:123", []),
    "arrow_right": ("code:124", []),
    "copy": ("c", ["command"]),
    "paste": ("v", ["command"]),
    "cut": ("x", ["command"]),
    "undo": ("z", ["command"]),
    "redo": ("z", ["command", "shift"]),
    "select_all": ("a", ["command"]),
    "save": ("s", ["command"]),
    "find": ("f", ["command"]),
    "new": ("n", ["command"]),
    "new_tab": ("t", ["command"]),
    "close_tab_or_window": ("w", ["command"]),
    "reopen_closed_tab": ("t", ["command", "shift"]),
    "quit_app": ("q", ["command"]),
    "minimize_window": ("m", ["command"]),
    "hide_app": ("h", ["command"]),
    "fullscreen": ("f", ["command", "control"]),
    "next_tab": ("code:48", ["control"]),
    "previous_tab": ("code:48", ["control", "shift"]),
    "browser_back": ("[", ["command"]),
    "browser_forward": ("]", ["command"]),
    "reload": ("r", ["command"]),
    "address_bar": ("l", ["command"]),
    "spotlight": ("code:49", ["command"]),
    "switch_app": ("code:48", ["command"]),
    "next_window": ("`", ["command"]),
    "select_line_start": ("code:123", ["command", "shift"]),
    "select_line_end": ("code:124", ["command", "shift"]),
    "delete_word": ("code:51", ["option"]),
    "delete_line": ("code:51", ["command"]),
    "zoom_in": ("=", ["command"]),
    "zoom_out": ("-", ["command"]),
    "bold": ("b", ["command"]),
    "italic": ("i", ["command"]),
    "send_message": ("code:36", ["command"]),
    "screenshot_region": ("4", ["command", "shift"]),
    "emoji_picker": ("code:49", ["command", "control"]),
    "lock_screen": ("q", ["command", "control"]),
    "show_desktop": ("code:103", []),
}


def press(shortcut: str, times: int = 1) -> None:
    key, mods = SHORTCUTS[shortcut]
    using = ""
    if mods:
        using = " using {" + ", ".join(f"{m} down" for m in mods) + "}"
    if key.startswith("code:"):
        cmd = f"key code {key[5:]}{using}"
    else:
        cmd = f'keystroke "{key}"{using}'
    body = "\n".join([cmd] * max(1, times))
    _osascript(f'tell application "System Events"\n{body}\nend tell')


# ---------------------------------------------------------------- scroll

def scroll(direction: str, amount: str = "page") -> None:
    """direction: up|down|top|bottom ; amount: little|page|a_lot."""
    if direction in ("top", "bottom"):
        press("arrow_up" if direction == "top" else "arrow_down")  # focus safety no-op
        _osascript(
            'tell application "System Events" to key code %d using {command down}'
            % (126 if direction == "top" else 125)
        )
        return
    lines = {"little": 5, "page": 15, "a_lot": 40}.get(amount, 15)
    sign = 1 if direction == "up" else -1
    try:
        import Quartz  # type: ignore

        for _ in range(lines):
            ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, sign * 3)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.004)
    except Exception:
        press("arrow_up" if direction == "up" else "arrow_down", times=lines)


# ---------------------------------------------------------------- volume / media

def get_volume() -> int:
    return int(_osascript("output volume of (get volume settings)"))


def set_volume(level: int) -> None:
    level = max(0, min(100, level))
    _osascript(f"set volume output volume {level}")


def volume(op: str) -> str:
    if op == "mute":
        _osascript("set volume with output muted")
        return "Muted."
    if op == "unmute":
        _osascript("set volume without output muted")
        return "Unmuted."
    cur = get_volume()
    if op == "up":
        set_volume(cur + 15)
        return "Louder."
    if op == "down":
        set_volume(cur - 15)
        return "Quieter."
    if op == "max":
        set_volume(100)
        return "Max volume."
    if op == "half":
        set_volume(50)
        return "Half volume."
    return ""


_NX_KEYS = {"play_pause": 16, "next": 17, "previous": 18}


def media(op: str) -> None:
    """Post a HID media key event (works for Music, Spotify, YouTube in browsers)."""
    from AppKit import NSEvent  # type: ignore
    import Quartz  # type: ignore

    key = _NX_KEYS[op]
    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        data1 = (key << 16) | ((0xA if down else 0xB) << 8)
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, None, 8, data1, -1
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())


# ---------------------------------------------------------------- misc

FOLDERS = {
    "home": Path.home(),
    "desktop": Path.home() / "Desktop",
    "downloads": Path.home() / "Downloads",
    "documents": Path.home() / "Documents",
    "pictures": Path.home() / "Pictures",
    "movies": Path.home() / "Movies",
    "applications": Path("/Applications"),
    "trash": Path.home() / ".Trash",
}


def open_folder(name: str) -> None:
    subprocess.Popen(["open", str(FOLDERS.get(name, Path.home()))])


def screenshot() -> Path:
    out = Path.home() / "Desktop" / f"Screenshot {time.strftime('%Y-%m-%d %H.%M.%S')}.png"
    subprocess.run(["screencapture", "-x", str(out)])
    return out


def system(op: str) -> str:
    if op == "lock":
        press("lock_screen")
        return "Locking."
    if op == "sleep_display":
        subprocess.Popen(["pmset", "displaysleepnow"])
        return "Sleeping the display."
    if op == "show_desktop":
        press("show_desktop")
        return "Showing desktop."
    if op == "toggle_dark_mode":
        _osascript('tell application "System Events" to tell appearance preferences to set dark mode to not dark mode')
        return "Toggled dark mode."
    if op == "empty_trash":
        _osascript('tell application "Finder" to empty trash')
        return "Emptied the trash."
    return ""


def accessibility_ok() -> bool:
    try:
        _osascript('tell application "System Events" to get name of first process')
        return True
    except Exception:
        return False
