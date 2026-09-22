"""Observed actions on the macOS desktop through the Accessibility (AX) tree.

The desktop analogue of jev-ultrafast's snapshot.js + browser.py:

* ``Desktop.observe()`` reads the frontmost application's focused window in one bounded walk
  and returns an indexed table of visible controls (role, label, current value, state), the
  visible text, freshness markers, and per-node guards. Every control keeps a code-owned
  identity: a small integer that maps to the actual ``AXUIElement`` reference, never a
  model-produced selector, coordinate, or script.
* ``Desktop.fresh()`` compares the semantic state again immediately before input. Clicks and
  typing use a scoped guard (the target's own state plus the window key: app, window title,
  every observed field value); scroll, wait, DONE and BLOCKED compare the whole control table.
* ``Desktop.act()`` re-resolves geometry, hit-tests for occlusion, and only then posts real
  mouse / keyboard events. Mutations are never retried.

Nothing here talks to a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ApplicationServices as AS  # type: ignore
import Quartz  # type: ignore
from AppKit import NSWorkspace  # type: ignore

from . import actions

# ------------------------------------------------------------------ constants

MAX_ACTIONS = 250          # candidates retained; truncated candidates cannot be selected
MAX_NODES = 3500           # AX elements visited per snapshot
MAX_DEPTH = 60
WALK_BUDGET_S = 0.9        # wall-clock cap for one snapshot
TEXT_LIMIT = 6000
SCROLL_PIXELS = 560
SETTLE_MAX_S = 2.0          # post-action: wait up to this long for two agreeing snapshots
SETTLE_STEP_S = 0.15

CLICK_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXMenuItem", "AXMenuBarItem", "AXMenuButton",
    "AXPopUpButton", "AXLink", "AXDisclosureTriangle", "AXComboBox", "AXTab", "AXCell", "AXRow",
    "AXDockItem", "AXSwitch", "AXToggle",
}
FILL_ROLES = {"AXTextField", "AXTextArea", "AXSearchField"}
TEXT_ROLES = {"AXStaticText", "AXHeading"}
SKIP_ROLES = {"AXMenuBar", "AXUnknown", "AXSplitter", "AXGrowArea", "AXScrollBar", "AXValueIndicator"}
CONTAINER_CLIP_ROLES = {"AXWindow", "AXScrollArea", "AXSheet", "AXDrawer", "AXPopover", "AXWebArea"}
DIALOG_SUBROLES = {"AXDialog", "AXSystemDialog", "AXFloatingWindow"}
BROWSERS = {"google chrome", "safari", "arc", "firefox", "brave browser", "microsoft edge", "chromium", "opera", "vivaldi"}

ATTRS = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXHelp",
    "AXEnabled", "AXFocused", "AXSelected", "AXPosition", "AXSize", "AXChildren", "AXVisibleRows",
    "AXVisibleChildren", "AXRoleDescription",
]
GUARD_ATTRS = ["AXRole", "AXTitle", "AXDescription", "AXValue", "AXEnabled", "AXSelected", "AXPosition", "AXSize"]

# Keys the policy may press. Small on purpose: everything else is a CLICK on a visible control.
KEYS: dict[str, str] = {
    "enter": "press enter / return: submit the focused field, confirm the default button, open the selected item",
    "escape": "press escape: dismiss a menu, popup, dialog, or autocomplete list",
    "tab": "press tab: move keyboard focus to the next control",
    "arrow_down": "press the down arrow: move to the next suggestion, row, or menu item",
    "arrow_up": "press the up arrow: move to the previous suggestion, row, or menu item",
    "backspace": "press backspace: delete the character before the caret",
    "send_message": "press command-enter: send the message or submit the form in chat and mail apps",
    "new_tab": "press command-T: open a new, empty browser tab (use this before navigating when the current tab shows the user's own unrelated page)",
    "address_bar": "press command-L: focus the browser address bar so a URL or search can be typed",
}


class StaleScreen(ValueError):
    """A decision no longer refers to the observed screen."""


# ------------------------------------------------------------------ AX helpers


def _is_axvalue(v: Any) -> bool:
    return type(v).__name__ == "AXValueRef"


def _point(v: Any) -> tuple[float, float] | None:
    if not _is_axvalue(v):
        return None
    try:
        ok, p = AS.AXValueGetValue(v, AS.kAXValueCGPointType, None)
        return (float(p.x), float(p.y)) if ok else None
    except Exception:  # noqa: BLE001
        return None


def _size(v: Any) -> tuple[float, float] | None:
    if not _is_axvalue(v):
        return None
    try:
        ok, s = AS.AXValueGetValue(v, AS.kAXValueCGSizeType, None)
        return (float(s.width), float(s.height)) if ok else None
    except Exception:  # noqa: BLE001
        return None


def _str(v: Any, limit: int = 200) -> str:
    if v is None or _is_axvalue(v):
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        return s if len(s) <= limit else s[:limit] + "…"
    return ""


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not _is_axvalue(v):
        return bool(v)
    return None


def _list(v: Any) -> list:
    if v is None or _is_axvalue(v) or isinstance(v, (str, int, float)):
        return []
    try:
        return list(v)
    except Exception:  # noqa: BLE001
        return []


def _multi(el: Any, attrs: list[str]) -> list | None:
    try:
        err, vals = AS.AXUIElementCopyMultipleAttributeValues(el, attrs, 0, None)
    except Exception:  # noqa: BLE001
        return None
    if err != 0 or vals is None:
        return None
    return list(vals)


def _attr(el: Any, name: str) -> Any:
    try:
        err, v = AS.AXUIElementCopyAttributeValue(el, name, None)
    except Exception:  # noqa: BLE001
        return None
    return v if err == 0 else None


def _intersect(a: tuple[float, float, float, float] | None, b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if a is None:
        return b
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def _inside(clip: tuple[float, float, float, float] | None, x: float, y: float) -> bool:
    if clip is None:
        return True
    return clip[0] <= x < clip[0] + clip[2] and clip[1] <= y < clip[1] + clip[3]


# ------------------------------------------------------------------ tree walk


@dataclass
class Node:
    el: Any
    role: str
    subrole: str
    title: str
    desc: str
    value: str
    placeholder: str
    help: str
    role_desc: str
    enabled: bool
    focused: bool
    selected: bool | None
    frame: tuple[float, float, float, float] | None
    visible: bool
    depth: int
    editable: bool = False
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None

    def child_text(self, limit: int = 120, depth: int = 3) -> str:
        parts: list[str] = []
        stack = [(c, 1) for c in self.children]
        while stack and sum(map(len, parts)) < limit:
            n, d = stack.pop(0)
            if n.role in TEXT_ROLES:
                t = n.value or n.title
                if t:
                    parts.append(t)
            elif n.role in ("AXImage", "AXButton", "AXLink") and (n.title or n.desc):
                parts.append(n.title or n.desc)
            if d < depth:
                stack.extend((c, d + 1) for c in n.children)
        return " ".join(parts).strip()[:limit]

    def name(self) -> str:
        if self.editable or self.role == "AXComboBox":
            return self.title or self.desc or self.placeholder or self.help or self.child_text()
        return self.title or self.desc or self.child_text() or self.help or (self.value if self.role in ("AXPopUpButton",) else "") or self.role_desc


class Walker:
    """One bounded, preorder walk of an application's focused window (+ open menus)."""

    @staticmethod
    def _editable_combo(el: Any) -> bool:
        """A combobox that carries a text selection range is an editable input (web search boxes)."""
        try:
            err, names = AS.AXUIElementCopyAttributeNames(el, None)
        except Exception:  # noqa: BLE001
            return False
        return err == 0 and names is not None and "AXSelectedTextRange" in list(names)

    def __init__(self) -> None:
        self.visited = 0
        self.truncated = False
        self.deadline = time.perf_counter() + WALK_BUDGET_S
        self.scroll_areas: list[Node] = []
        self.text_fields: list[Node] = []

    def walk(self, el: Any, depth: int, clip: tuple[float, float, float, float] | None, parent: Node | None) -> Node | None:
        if self.visited >= MAX_NODES or depth > MAX_DEPTH or time.perf_counter() > self.deadline:
            self.truncated = True
            return None
        vals = _multi(el, ATTRS)
        if vals is None:
            return None
        self.visited += 1
        role = _str(vals[0])
        if role in SKIP_ROLES:
            return None
        pos, size = _point(vals[10]), _size(vals[11])
        frame = (pos[0], pos[1], size[0], size[1]) if pos and size else None
        enabled = _bool(vals[7])
        enabled = True if enabled is None else enabled
        cx = cy = None
        visible = False
        if frame and frame[2] > 0 and frame[3] > 0:
            cx, cy = frame[0] + frame[2] / 2, frame[1] + frame[3] / 2
            visible = _inside(clip, cx, cy)
            if not visible and clip is not None:
                # Fully outside the clipping container: nothing below can be on screen.
                inter = _intersect(clip, frame)
                if inter[2] <= 0 or inter[3] <= 0:
                    return None
        node = Node(
            el=el, role=role, subrole=_str(vals[1]), title=_str(vals[2]), desc=_str(vals[3]),
            value=_str(vals[4]), placeholder=_str(vals[5]), help=_str(vals[6], 80), role_desc=_str(vals[15], 40),
            enabled=enabled, focused=bool(_bool(vals[8])), selected=_bool(vals[9]),
            frame=frame, visible=visible, depth=depth, parent=parent,
        )
        if role == "AXScrollArea" and visible:
            self.scroll_areas.append(node)
        node.editable = role in FILL_ROLES or (role == "AXComboBox" and visible and self._editable_combo(el))
        if node.editable and visible and node.subrole != "AXSecureTextField":
            self.text_fields.append(node)
        child_clip = clip
        if role in CONTAINER_CLIP_ROLES and frame and frame[2] > 0 and frame[3] > 0:
            child_clip = _intersect(clip, frame)
        kids = _list(vals[13]) or _list(vals[14]) or _list(vals[12])
        for child in kids:
            c = self.walk(child, depth + 1, child_clip, node)
            if c is not None:
                node.children.append(c)
        return node


# ------------------------------------------------------------------ snapshot → table


def _clickable(n: Node) -> bool:
    if n.role in ("AXRow", "AXCell"):
        # A row is a target only when selecting it means something and it holds no finer control.
        if n.selected is None:
            return False
        return not any(c.role in CLICK_ROLES - {"AXRow", "AXCell"} for c in n.children)
    if n.role == "AXCheckBox" or n.role == "AXRadioButton" or n.role in CLICK_ROLES:
        return True
    return False


def _state(n: Node) -> dict[str, Any]:
    s: dict[str, Any] = {}
    if n.role in ("AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle"):
        s["checked"] = "true" if n.value in ("1", "true", "2") else "false"
    if n.role == "AXDisclosureTriangle":
        s["expanded"] = "true" if n.value in ("1", "true") else "false"
    if n.selected is not None and n.role in ("AXRow", "AXCell", "AXTab", "AXRadioButton", "AXMenuItem"):
        s["selected"] = "true" if n.selected else "false"
    return s


def build_table(root: Node | None, extra_roots: list[Node], ids: "Identity") -> tuple[list[dict], list[str], dict[int, Node]]:
    acts: list[dict[str, Any]] = []
    words: list[str] = []
    length = 0
    nodes: dict[int, Node] = {}
    seen_text: str | None = None

    def visit(n: Node) -> None:
        nonlocal length, seen_text
        if n.visible and n.enabled:
            if n.role in TEXT_ROLES:
                t = n.value or n.title
                if t and t != seen_text and length < TEXT_LIMIT:
                    words.append(t)
                    length += len(t)
                    seen_text = t
            elif n.editable and n.value and length < TEXT_LIMIT:
                words.append(f"[{n.name() or n.role}: {n.value}]")
                length += len(n.value)
            fill = n.editable and n.subrole != "AXSecureTextField"
            click = _clickable(n)
            if fill or click:
                node_id = ids.get(n.el)
                nodes[node_id] = n
                label = n.name() or n.role_desc or n.role.removeprefix("AX")
                base: dict[str, Any] = {"node": node_id, "role": n.role.removeprefix("AX").lower(), "label": label,
                                        "rect": {"x": n.frame[0], "y": n.frame[1], "w": n.frame[2], "h": n.frame[3]},  # type: ignore[index]
                                        "value": n.value, **_state(n)}
                if n.subrole:
                    base["subrole"] = n.subrole.removeprefix("AX")
                if fill:
                    acts.append({**base, "kind": "fill"})
                    acts.append({**base, "kind": "click", "label": "Focus " + label})
                else:
                    acts.append({**base, "kind": "click"})
        for c in n.children:
            visit(c)

    for r in [root, *extra_roots]:
        if r is not None:
            visit(r)
    return acts, words, nodes


class Identity:
    """Code-owned identities for AX elements. Equality is CFEqual, so the same control keeps
    its id across observations; ids for controls not seen this cycle are dropped (pruning)."""

    def __init__(self) -> None:
        self.ids: dict[Any, int] = {}
        self.next = 1
        self.nodes: dict[int, Any] = {}
        self._seen: dict[Any, int] = {}

    def begin(self) -> None:
        self._seen = {}

    def get(self, el: Any) -> int:
        try:
            i = self.ids.get(el)
        except Exception:  # noqa: BLE001
            i = None
        if i is None:
            i = self.next
            self.next += 1
        self._seen[el] = i
        self.nodes[i] = el
        return i

    def commit(self) -> None:
        self.ids = dict(self._seen)
        self.nodes = {i: el for el, i in self.ids.items()}

    def reset(self) -> None:
        self.ids, self.nodes, self._seen = {}, {}, {}


# ------------------------------------------------------------------ desktop


def fingerprint(state: dict[str, Any]) -> str:
    semantics = [{k: v for k, v in a.items() if k != "rect"} for a in state["actions"]]
    content = {"app": state["app"], "title": state["title"], "text": state["text"], "actions": semantics}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def display_bounds(which: str | None) -> tuple[float, float, float, float] | None:
    """Screen rectangle of one monitor: 'left' / 'right' / 'main' / an index, or None for all."""
    if not which or which in ("all", "any"):
        return None
    _err, ids, _n = Quartz.CGGetActiveDisplayList(16, None, None)
    rects = []
    for d in ids or []:
        b = Quartz.CGDisplayBounds(d)
        rects.append(((b.origin.x, b.origin.y, b.size.width, b.size.height), bool(Quartz.CGDisplayIsMain(d))))
    if not rects:
        return None
    if which == "main":
        return next((r for r, main in rects if main), rects[0][0])
    if which == "left":
        return min((r for r, _ in rects), key=lambda r: r[0])
    if which == "right":
        return max((r for r, _ in rects), key=lambda r: r[0])
    if which.isdigit():
        ordered = sorted((r for r, _ in rects), key=lambda r: (r[0], r[1]))
        return ordered[min(int(which), len(ordered) - 1)]
    raise ValueError(f"Unknown display {which!r}: use left, right, main, or an index")


class Desktop:
    def __init__(self, apps: list[str] | None = None, display: str | None = None) -> None:
        if not AS.AXIsProcessTrusted():
            raise SystemExit("Accessibility permission missing: System Settings → Privacy & Security → Accessibility → add your terminal.")
        self.system = AS.AXUIElementCreateSystemWide()
        # Everything observed and touched stays on this monitor; windows are moved onto it first.
        self.display = display_bounds(display if display is not None else os.environ.get("AGENT_DISPLAY"))
        self.owned: set[int] = set()  # apps the agent opened or acted in; only their windows may be moved
        self.identity = Identity()
        self.pid: int | None = None
        self.apps = apps if apps is not None else actions.installed_apps()
        self.after_input: dict[str, Any] | None = None
        self._enabled_ax: set[int] = set()
        self._pending: dict[str, Any] | None = None

    # -------------------------------------------------------------- observe

    def _front(self) -> tuple[str, int, Any]:
        pid = actions.frontmost_pid()
        name = actions.frontmost_app() if pid else ""
        el = AS.AXUIElementCreateApplication(pid) if pid else None
        if pid and pid not in self._enabled_ax:
            # Chromium/Electron apps expose web content only when an assistive client asks.
            for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
                try:
                    AS.AXUIElementSetAttributeValue(el, attr, True)
                except Exception:  # noqa: BLE001
                    pass
            self._enabled_ax.add(pid)
        return name, pid, el

    @staticmethod
    def _pid_of(name: str) -> int:
        for a in NSWorkspace.sharedWorkspace().runningApplications():
            if str(a.localizedName()).lower() == name.lower():
                return int(a.processIdentifier())
        return 0

    def _settle(self) -> None:
        if not self.after_input:
            return
        action, self.after_input = self.after_input, None
        kind = action["kind"]
        # Read-only waits, after execution was logged. AppKit menus/sheets animate ~100 ms.
        time.sleep({"click": 0.08, "fill": 0.12, "key": 0.08, "scroll": 0.15, "open_app": 0.4}.get(kind, 0.05))

    def observe(self, screenshot: bool = False) -> dict[str, Any]:
        settling = self.after_input is not None
        self._settle()
        if self._pending is not None:
            page, self._pending = self._pending, None
            return page
        state = self._snapshot(screenshot)
        if settling:
            # Wait for useful state: after an action, keep reading until two consecutive
            # snapshots agree (page loads, menus, sheets), capped so a live page cannot stall us.
            deadline = time.perf_counter() + SETTLE_MAX_S
            while time.perf_counter() < deadline:
                time.sleep(SETTLE_STEP_S)
                again = self._snapshot(screenshot)
                if again["marker"] == state["marker"]:
                    return again
                state = again
        return state

    def _snapshot(self, screenshot: bool = False) -> dict[str, Any]:
        name, pid, app_el = self._front()
        if pid != self.pid:
            self.identity.reset()
            self.pid = pid
        self.identity.begin()
        walker = Walker()
        root: Node | None = None
        title = ""
        extra: list[Node] = []
        if app_el is not None:
            win = _attr(app_el, "AXFocusedWindow")
            if win is not None:
                if pid in self.owned:
                    self._confine(win)
                root = walker.walk(win, 0, self.display, None)
                title = root.title if root else ""
            # Open menus (context menus, popup buttons) hang off the application, not the window.
            for child in _list(_attr(app_el, "AXChildren")):
                r = _str(_attr(child, "AXRole"))
                dialog = r == "AXWindow" and win is not None and child != win and _str(_attr(child, "AXSubrole")) in DIALOG_SUBROLES
                if r == "AXMenu" or dialog:
                    n = walker.walk(child, 0, self.display, None)
                    if n is not None:
                        extra.append(n)
        acts, words, node_map = build_table(root, extra, self.identity)
        self.identity.commit()
        omitted = max(0, len(acts) - MAX_ACTIONS)
        acts = acts[:MAX_ACTIONS]
        for i, a in enumerate(acts):
            a["id"] = f"e{i + 1}"
        # Controls: scroll (when we can tell there is room), wait, keys. A window that is not on the
        # confined display offers nothing to press into: only OPEN_APP (including this app) and wait.
        on_screen = root is not None
        if on_screen:
            scroll_target, position = self._scroll_target(walker.scroll_areas, root)
            if position is None or position < 0.999:
                acts.append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": SCROLL_PIXELS, "at": scroll_target})
            if position is None or position > 0.001:
                acts.append({"id": "scroll_up", "kind": "scroll", "label": "Scroll up", "delta": -SCROLL_PIXELS, "at": scroll_target})
        acts.append({"id": "wait", "kind": "wait", "label": "Wait for the screen to update"})
        if on_screen:
            for key, desc in KEYS.items():
                acts.append({"id": f"key:{key}", "kind": "key", "label": desc, "key": key})
        for app in self.apps:
            if app != name or not on_screen:
                acts.append({"id": f"app:{app}", "kind": "open_app", "label": app, "app": app})
        text = "\n".join(words)[:TEXT_LIMIT]
        page_key = self._page_key(pid, title, walker.text_fields)
        guards = {str(a["node"]): self._guard_of(node_map[a["node"]]) for a in acts if "node" in a}
        semantics = [{k: v for k, v in a.items() if k not in ("rect", "at")} for a in acts]
        marker = [pid, title, semantics]
        state = {
            "app": name, "pid": pid, "title": title, "url": f"{name} · {title}", "text": text,
            "actions": acts, "marker": marker, "page_key": page_key, "guards": guards,
            "omitted_actions": omitted, "truncated": walker.truncated, "visited": walker.visited,
            "observed_at": time.time(),
        }
        state["fingerprint"] = fingerprint(state)
        state["view"] = self.view()
        if screenshot:
            state["screenshot"] = self.screenshot(state["view"])
        return state

    def _confine(self, win: Any) -> None:
        """Move a window onto the confined display if its centre is elsewhere (read-only otherwise)."""
        if self.display is None:
            return
        pos, size = _point(_attr(win, "AXPosition")), _size(_attr(win, "AXSize"))
        if not pos or not size:
            return
        dx, dy, dw, dh = self.display
        if _inside(self.display, pos[0] + size[0] / 2, pos[1] + size[1] / 2):
            return
        w, h = min(size[0], dw - 40), min(size[1], dh - 60)
        try:
            AS.AXUIElementSetAttributeValue(win, "AXPosition", AS.AXValueCreate(AS.kAXValueCGPointType, (dx + 20, dy + 40)))
            if (w, h) != size:
                AS.AXUIElementSetAttributeValue(win, "AXSize", AS.AXValueCreate(AS.kAXValueCGSizeType, (w, h)))
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.15)

    def _scroll_target(self, areas: list[Node], root: Node | None) -> tuple[dict[str, float] | None, float | None]:
        best = max(areas, key=lambda n: n.frame[2] * n.frame[3], default=None) if areas else None  # type: ignore[index]
        node = best or root
        if node is None or node.frame is None:
            return None, None
        f = node.frame
        at = {"x": f[0] + f[2] / 2, "y": f[1] + f[3] / 2}
        position = None
        if best is not None:
            bar = _attr(best.el, "AXVerticalScrollBar")
            if bar is not None:
                v = _attr(bar, "AXValue")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    position = float(v)
        return at, position

    def _page_key(self, pid: int, title: str, fields: list[Node]) -> list:
        return [pid, title, [[self.identity.get(n.el), n.value] for n in fields[:60]]]

    @staticmethod
    def _guard_of(n: Node) -> list:
        return [n.role, n.title, n.desc, n.value, n.enabled, n.selected]

    def _guard_now(self, el: Any) -> list | None:
        vals = _multi(el, GUARD_ATTRS)
        if vals is None:
            return None
        pos, size = _point(vals[6]), _size(vals[7])
        if not pos or not size or size[0] <= 0 or size[1] <= 0:
            return None
        enabled = _bool(vals[4])
        return [_str(vals[0]), _str(vals[1]), _str(vals[2]), _str(vals[3]), True if enabled is None else enabled, _bool(vals[5])]

    # -------------------------------------------------------------- freshness

    def fresh(self, page: dict[str, Any], action: dict[str, Any] | None = None) -> bool:
        if action is not None and action["kind"] in ("click", "fill"):
            node = action.get("node")
            if type(node) is not int:
                return False
            el = self.identity.nodes.get(node)
            if el is None:
                return False
            name, pid, _ = self._front()
            if pid != page["pid"]:
                return False
            win_title = ""
            app_el = AS.AXUIElementCreateApplication(pid)
            win = _attr(app_el, "AXFocusedWindow")
            if win is not None:
                win_title = _str(_attr(win, "AXTitle"))
            fields = [[i, _str(_attr(self.identity.nodes[i], "AXValue"))] for i, _ in page["page_key"][2] if i in self.identity.nodes]
            return [pid, win_title, fields] == page["page_key"] and self._guard_now(el) == page["guards"].get(str(node))
        current = self.observe()
        if current["marker"] == page["marker"]:
            return True
        self._pending = current
        return False

    # -------------------------------------------------------------- execute

    def _resolve(self, action: dict[str, Any]) -> tuple[float, float]:
        node = action["node"]
        if type(node) is not int:
            raise ValueError("Invalid observed node")
        el = self.identity.nodes.get(node)
        if el is None:
            raise StaleScreen("Target is no longer observed. Observe again.")
        vals = _multi(el, ["AXEnabled", "AXPosition", "AXSize"])
        if vals is None:
            raise StaleScreen("Target disappeared. Observe again.")
        enabled = _bool(vals[0])
        pos, size = _point(vals[1]), _size(vals[2])
        if enabled is False or not pos or not size or size[0] <= 0 or size[1] <= 0:
            raise StaleScreen("Target is disabled or has no geometry. Observe again.")
        x, y = pos[0] + size[0] / 2, pos[1] + size[1] / 2
        if not _inside(self.display, x, y):
            raise StaleScreen("Target is outside the confined display. Observe again.")
        if action["kind"] == "fill":
            # Keystrokes go to the focused field whatever is drawn over it: a browser's address-bar
            # suggestion popup is a separate window of the same app that overlays the field itself.
            if _bool(_attr(el, "AXFocused")) is True or self._hit_ok(el, x, y, same_app_ok=True):
                return x, y
            raise StaleScreen("Field is covered by another application's window. Observe again.")
        if not self._hit_ok(el, x, y):
            raise StaleScreen("Target is covered by another element or window. Observe again.")
        return x, y

    def _hit_ok(self, el: Any, x: float, y: float, same_app_ok: bool = False) -> bool:
        try:
            err, hit = AS.AXUIElementCopyElementAtPosition(self.system, x, y, None)
        except Exception:  # noqa: BLE001
            return True
        if err != 0 or hit is None:
            return True  # the app does not support hit-testing; geometry already validated
        if hit == el:
            return True
        if same_app_ok:
            try:
                return AS.AXUIElementGetPid(hit, None)[1] == AS.AXUIElementGetPid(el, None)[1]
            except Exception:  # noqa: BLE001
                return True
        for start, other in ((hit, el), (el, hit)):
            cur = start
            for _ in range(20):
                cur = _attr(cur, "AXParent")
                if cur is None:
                    break
                if cur == other:
                    return True
        return False

    def act(self, action: dict[str, Any], page: dict[str, Any], text: str | None = None, force: bool = False) -> dict[str, Any]:
        if force and action["kind"] not in ("scroll", "wait"):
            raise ValueError("Only scroll and wait may skip the freshness check")
        if not force and not self.fresh(page, action):
            raise StaleScreen("Screen changed since this decision. Observe again.")
        kind = action["kind"]
        if kind == "wait":
            time.sleep(0.25)
        elif kind == "scroll":
            at = action.get("at") or {}
            x, y = at.get("x"), at.get("y")
            if x is None or y is None:
                if self.display is not None:
                    dx, dy, dw, dh = self.display
                else:
                    f = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
                    dx, dy, dw, dh = f.origin.x, f.origin.y, f.size.width, f.size.height
                x, y = dx + dw / 2, dy + dh / 2
            Quartz.CGWarpMouseCursorPosition((x, y))
            time.sleep(0.02)
            step = 112 if action["delta"] > 0 else -112
            for _ in range(abs(int(action["delta"])) // 112):
                ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 1, -step)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.01)
        elif kind == "key":
            actions.press(action["key"])
        elif kind == "open_app":
            running = self._pid_of(action["app"])
            actions.open_app(action["app"])
            actions.focus_app(action["app"], timeout=4.0)
            pid = self._pid_of(action["app"])
            self.owned.add(pid)
            if self.display is not None and running and action["app"].lower() in BROWSERS:
                # Leave the user's browser window alone: work in a new window, then confine it.
                win = _attr(AS.AXUIElementCreateApplication(pid), "AXFocusedWindow")
                pos, size = (_point(_attr(win, "AXPosition")), _size(_attr(win, "AXSize"))) if win is not None else (None, None)
                if pos and size and not _inside(self.display, pos[0] + size[0] / 2, pos[1] + size[1] / 2):
                    time.sleep(0.2)
                    actions.press("new")
                    deadline = time.perf_counter() + 1.0
                    while time.perf_counter() < deadline:
                        fresh = _attr(AS.AXUIElementCreateApplication(pid), "AXFocusedWindow")
                        if fresh is not None and fresh != win:
                            self._confine(fresh)
                            break
                        time.sleep(0.05)
        elif kind in ("click", "fill"):
            x, y = self._resolve(action)
            self.owned.add(page["pid"])
            focused = kind == "fill" and _bool(_attr(self.identity.nodes.get(action["node"]), "AXFocused")) is True
            if not focused:
                self._click(x, y)
            if kind == "fill":
                if text is None:
                    raise ValueError("TYPE_TEXT needs a value")
                time.sleep(0.05)
                if action.get("role") == "textarea":
                    # Documents/notes: append at the end instead of replacing the whole body.
                    self._chord(125, Quartz.kCGEventFlagMaskCommand)  # ⌘↓ moves the caret to the end
                else:
                    self._chord(0, Quartz.kCGEventFlagMaskCommand)    # ⌘A replaces the field's contents
                time.sleep(0.02)
                self.type_unicode(text)
        else:
            raise ValueError("Unknown action kind")
        self.after_input = action if kind != "wait" else None
        return {"executed": action["id"]}

    # -------------------------------------------------------------- input primitives

    @staticmethod
    def _click(x: float, y: float) -> None:
        move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
        time.sleep(0.01)
        for t in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            ev = Quartz.CGEventCreateMouseEvent(None, t, (x, y), Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.01)

    @staticmethod
    def _chord(keycode: int, flags: int) -> None:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
            Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.005)

    @staticmethod
    def type_unicode(text: str, chunk: int = 20) -> None:
        for line_no, line in enumerate(text.split("\n")):
            if line_no:
                for down in (True, False):
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateKeyboardEvent(None, 36, down))
                time.sleep(0.01)
            for i in range(0, len(line), chunk):
                part = line[i:i + chunk]
                for down in (True, False):
                    ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(part), part)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.008)

    def view(self) -> dict[str, float]:
        """The rectangle the inspector shows: the confined display, else the main display."""
        if self.display is not None:
            x, y, w, h = self.display
        else:
            f = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            x, y, w, h = f.origin.x, f.origin.y, f.size.width, f.size.height
        return {"x": x, "y": y, "w": w, "h": h}

    @staticmethod
    def screenshot(view: dict[str, float] | None = None) -> str:
        """JPEG screenshot of the view as base64 (optional; the policy never consumes it)."""
        import base64
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            path = Path(f.name)
        region = ["-R", f"{view['x']:.0f},{view['y']:.0f},{view['w']:.0f},{view['h']:.0f}"] if view else []
        subprocess.run(["screencapture", "-x", "-t", "jpg", *region, str(path)], check=False)
        data = path.read_bytes() if path.exists() else b""
        path.unlink(missing_ok=True)
        return base64.b64encode(data).decode()

    def close(self) -> None:
        self.identity.reset()
