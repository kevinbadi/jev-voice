"""Browser goals run on jev-ultrafast, unchanged: Chrome over CDP through browser-harness.

This module only adds what Jev Voice needs around it:

* ``ConfinedBrowser``   the ultrafast ``Browser``, but its tab opens in a new Chrome window placed
                        on the confined monitor (``AGENT_DISPLAY``) instead of the user's window.
* ``select_field_text`` the TYPE_TEXT fallback when no text model is configured: one Jev request
                        selects the value from spans cut out of the goal. Nothing is generated.
* ``WebAgent``          the ultrafast ``Agent`` with those two plugged in, a step callback, and a
                        ``screen`` view of its state so the inspector renders it like the desktop.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp
from jev_ultrafast import agent as uf_agent
from jev_ultrafast import model as uf_model
from jev_ultrafast.browser import Browser, StalePage
from jev_ultrafast.model import action_space, choose, field_context, field_text, post_json, validate_choice
from jev_ultrafast.questions import MAX_STEPS
from jev_ultrafast.questions import NEXT_ACTION as BASE_NEXT_ACTION

from . import config, escalate
from .desktop import display_bounds
from .policy import text_candidates
from .questions import TEXT_SELECT

DEFAULT_URL = os.environ.get("TASK_START_URL", "https://www.google.com")
CHROME_UI_HEIGHT = 120  # tab strip + toolbar, roughly
RULES_FILE = Path(os.environ.get("JEV_RULES_FILE", "runs/rules.json"))


def _site(url: str) -> str:
    return (url or "").split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")


def load_rules(url: str) -> list[str]:
    try:
        return list(json.loads(RULES_FILE.read_text()).get(_site(url), []))[:12]
    except (OSError, ValueError):
        return []


def save_rule(url: str, rule: str) -> list[str]:
    try:
        data = json.loads(RULES_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    rules = data.setdefault(_site(url), [])
    if rule not in rules:
        rules.append(rule)
        rules[:] = rules[-12:]
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        RULES_FILE.write_text(json.dumps(data, indent=2))
    return rules


def load_trust(url: str) -> float | None:
    try:
        value = json.loads(RULES_FILE.read_text()).get("_trust", {}).get(_site(url))
        return float(value) if value is not None else None
    except (OSError, ValueError, TypeError):
        return None


def save_trust(url: str, trust: float) -> None:
    try:
        data = json.loads(RULES_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("_trust", {})[_site(url)] = round(float(trust), 2)
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(json.dumps(data, indent=2))


def apply_rules(rules: list[str]) -> None:
    """Learned site rules become part of Jev's own instructions (ultrafast reads NEXT_ACTION at request time)."""
    uf_model.NEXT_ACTION = BASE_NEXT_ACTION + ("\nRules learned for this site:\n" + "\n".join(f"- {r}" for r in rules) if rules else "")


def viewport_for(bounds: tuple[float, float, float, float] | None) -> tuple[int, int]:
    """TASK_VIEWPORT=fill (default when confined to a monitor) sizes the page to the monitor;
    WxH pins it (jev-ultrafast's demo uses 1120x780)."""
    setting = os.environ.get("TASK_VIEWPORT", "fill" if bounds else "1120x780")
    if setting != "fill":
        w, h = setting.lower().split("x")
        return int(w), int(h)
    if bounds is None:
        return 1120, 780
    return int(bounds[2] - 80), int(bounds[3] - 80 - CHROME_UI_HEIGHT)


# Styled checkboxes/radios: the input is visually hidden and its <label> is what the user clicks.
# The snapshot lists neither, so the label becomes an observed element with a code-owned id
# in the same node cache the executor uses (real element, real click, same guards).
LABEL_TOGGLES = """(() => {
  const cache=window.__jevFast; if (!cache) return [];
  const vis=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const out=[];
  for (const label of document.querySelectorAll('label')) {
    const input=label.querySelector('input[type="checkbox"],input[type="radio"]') || (label.htmlFor ? document.getElementById(label.htmlFor) : null);
    if (!input || !['checkbox','radio'].includes(input.type) || input.disabled) continue;
    if (!vis(label) || vis(input)) continue;
    const r=label.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
    if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (!cache.ids.has(label)) cache.ids.set(label,cache.next++);
    const id=cache.ids.get(label); cache.nodes.set(id,label);
    out.push({node:id, role:input.type, label:(label.innerText||'').replace(/\\s+/g,' ').trim().slice(0,80),
      checked:String(input.checked), rect:{x:r.x,y:r.y,w:r.width,h:r.height}, guard:cache.guard(label)});
  }
  return out;
})()"""

# Scrollable regions (sidebars, dropdown lists, panels) the page-level scroll cannot reach.
SCROLL_REGIONS = """(() => {
  const vis=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const out=[];
  for (const e of document.querySelectorAll('*')) {
    if (e===document.documentElement || e===document.body) continue;
    const st=getComputedStyle(e);
    if (!/(auto|scroll)/.test(st.overflowY)) continue;
    if (e.scrollHeight <= e.clientHeight + 40 || !vis(e)) continue;
    const r=e.getBoundingClientRect();
    if (r.width<120 || r.height<120 || r.bottom<0 || r.top>innerHeight || r.right<0 || r.left>innerWidth) continue;
    const items=[...e.querySelectorAll('a,button,[role="option"],[role="menuitem"],[role="radio"],label')]
      .map(x=>(x.innerText||x.getAttribute('aria-label')||'').trim().split('\\n')[0]).filter(t=>t&&t.length<40);
    const L=Math.max(r.left,0), T=Math.max(r.top,0), H=Math.min(r.height,innerHeight-T);
    out.push({el:e, x:L+Math.min(r.width,innerWidth-L)/2, y:T+H/2, step:Math.round(Math.max(200, Math.min(e.clientHeight*0.8, 700))),
      top:e.scrollTop, more:e.scrollTop+e.clientHeight<e.scrollHeight-2,
      label:(e.getAttribute('aria-label')||'').slice(0,40), items:items.slice(0,6), count:items.length});
  }
  // Prefer the innermost scrollers (a dropdown list inside a sidebar), then the largest.
  const inner=out.filter(o=>!out.some(p=>p!==o && o.el.contains(p.el)));
  return inner.slice(0,3).map(({el,...o})=>o);
})()"""

# Generic labels ("Minimum Range") get the nearest section heading ("Price", "Year") appended.
CONTEXT_LABELS = """(ids => {
  const cache=window.__jevFast; if (!cache) return {};
  const ok=t=>t && t.length>=3 && t.length<=30 && /[a-z]{3,}/i.test(t) && !/^(to|and|or|from)$/i.test(t);
  const short=t=>{t=(t||'').replace(/\\s+/g,' ').trim(); return ok(t) ? t : null;};
  const out={};
  for (const id of ids) {
    const e=cache.nodes.get(id); if (!e) continue;
    let node=e, found=null;
    for (let depth=0; depth<6 && node && !found; depth++) {
      // Look at earlier siblings of this ancestor for a heading-like text.
      let sib=node.previousElementSibling, hops=0;
      while (sib && hops<4 && !found) {
        const h=sib.querySelector('h1,h2,h3,h4,h5,h6,[role="heading"],strong,b,span') || sib;
        const t=short(h.innerText); if (t && !/range|minimum|maximum/i.test(t)) found=t;
        sib=sib.previousElementSibling; hops++;
      }
      node=node.parentElement;
    }
    if (found) out[id]=found;
  }
  return out;
})"""

# With a modal open, only its contents can receive input; everything behind it is withdrawn.
MODAL_NODES = """(ids => {
  const modals=[...document.querySelectorAll('dialog[open],[aria-modal="true"],[role="dialog"]')]
    .filter(e=>{const r=e.getBoundingClientRect(); return e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) && r.width>0 &&
      r.right>0 && r.left<innerWidth && r.bottom>0 && r.top<innerHeight;});
  if (!modals.length) return null;
  // The dialog that owns focus (or is aria-modal) is the active one; otherwise the last in the document.
  const top=modals.find(m=>m.contains(document.activeElement)) || modals.find(m=>m.getAttribute('aria-modal')==='true') || modals[modals.length-1];
  // Popup layers (listbox/menu options opened from inside the modal) render outside it; they stay offered.
  const popups=[...document.querySelectorAll('[role="listbox"],[role="menu"],[role="option"],[role="menuitem"],[role="menuitemradio"]')]
    .filter(e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
  return ids.filter(id=>{const e=window.__jevFast?.nodes.get(id); return e && (top.contains(e) || popups.some(p=>p===e || p.contains(e) || e.contains(p)));});
})"""

# Points inside a control worth trying when its exact centre is covered by a decorative layer.
UNCOVERED_POINT = """(action => new Promise(resolve => {
  const e=window.__jevFast?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return resolve(null);
  // A control at the edge of a scroller sits under a sticky header: centre it in its scroller first.
  e.scrollIntoView({block:'center', inline:'nearest'});
  setTimeout(() => {
  const boxes=[e, ...(e.labels ? [...e.labels] : [])].map(el=>el.getBoundingClientRect());
  for (const r of boxes) {
    if (!r.width || !r.height) continue;
    for (const [fx,fy] of [[0.5,0.5],[0.15,0.5],[0.85,0.5],[0.5,0.2],[0.5,0.8],[0.1,0.1]]) {
      const x=r.x+r.width*fx, y=r.y+r.height*fy;
      if (x<0||y<0||x>=innerWidth||y>=innerHeight) continue;
      const hit=document.elementFromPoint(x,y);
      if (hit && (e.contains(hit) || hit.contains(e) || (e.labels && [...e.labels].some(l=>l.contains(hit))))) return resolve({x,y});
    }
  }
  resolve(null);
  }, 120);
}))"""


class ConfinedBrowser(Browser):
    def __init__(self, url: str, display: str | None = None) -> None:
        ensure_daemon()
        bounds = display_bounds(display) if display else None
        vw, vh = viewport_for(bounds)
        self.target = cdp("Target.createTarget", url="about:blank", newWindow=bounds is not None, background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        if bounds is not None:
            # Keep the agent's window on its own monitor; the user's Chrome windows are untouched.
            x, y, _w, _h = bounds
            try:
                window = cdp("Browser.getWindowForTarget", targetId=self.target)["windowId"]
                cdp("Browser.setWindowBounds", windowId=window,
                    bounds={"left": int(x + 40), "top": int(y + 40), "width": vw + 20, "height": vh + CHROME_UI_HEIGHT, "windowState": "normal"})
            except RuntimeError:
                pass
        self.call("Emulation.setDeviceMetricsOverride", width=vw, height=vh, deviceScaleFactor=1, mobile=False)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)


    def observe(self, screenshot: bool = True) -> dict[str, Any]:
        page = super().observe(screenshot=screenshot)
        # Code-owned controls the DOM snapshot lacks: Enter, and scrolling inside scrollable regions.
        page["actions"].append({"id": "press_enter", "kind": "key", "key": "Enter",
                                "label": "Press Enter: submit the focused search box or field, or send the focused message"})
        page["actions"].append({"id": "press_escape", "kind": "key", "key": "Escape",
                                "label": "Press Escape: close an open dropdown list, menu, or dialog without choosing"})
        try:
            regions = self.evaluate(SCROLL_REGIONS) or []
        except StalePage:
            regions = []
        for i, r in enumerate(regions):
            what = r["label"] or ("list: " + ", ".join(r["items"]) + ("…" if r["count"] > len(r["items"]) else "")) or "panel"
            if r["more"]:
                page["actions"].append({"id": f"scroll_region_down_{i}", "kind": "scroll_region", "delta": r["step"], "at": {"x": r["x"], "y": r["y"]},
                                        "label": f"Scroll down inside the {what} (to reveal more of its {r['count']} items)"})
            if r["top"] > 0:
                page["actions"].append({"id": f"scroll_region_up_{i}", "kind": "scroll_region", "delta": -r["step"], "at": {"x": r["x"], "y": r["y"]},
                                        "label": f"Scroll up inside the {what}"})
        return page

    COUNT_CONTROLS = ("[...document.querySelectorAll('a[href],button,input,select,textarea,[role=\"button\"],[role=\"option\"],"
                      "[role=\"menuitem\"],[role=\"radio\"],[role=\"link\"]')].filter(e=>e.checkVisibility()).length")

    def _await_controls_change(self, cap_s: float = 0.8) -> None:
        """Opening a dropdown/panel renders its items a beat later; observe them, not the pre-click page."""
        try:
            before = self.evaluate(self.COUNT_CONTROLS)
        except StalePage:
            return
        deadline = time.monotonic() + cap_s
        while time.monotonic() < deadline:
            time.sleep(0.08)
            try:
                if self.evaluate(self.COUNT_CONTROLS) != before:
                    time.sleep(0.15)
                    return
            except StalePage:
                return

    def act(self, action: dict[str, Any], page: dict[str, Any], text: str | None = None) -> dict[str, Any]:
        if action["kind"] == "scroll_region":
            if not self.fresh(page):
                raise StalePage("Page changed since this decision. Observe again.")
            at = action["at"]
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=at["x"], y=at["y"])
            self.call("Input.dispatchMouseEvent", type="mouseWheel", x=at["x"], y=at["y"], deltaX=0, deltaY=action["delta"])
            self.after_input = action
            return {"executed": action["id"]}
        if action["kind"] == "key":
            if not self.fresh(page):
                raise StalePage("Page changed since this decision. Observe again.")
            key = action.get("key", "Enter")
            code = {"Enter": 13, "Escape": 27}[key]
            for event in ("keyDown", "keyUp"):
                self.call("Input.dispatchKeyEvent", type=event, key=key, code=key, windowsVirtualKeyCode=code, nativeVirtualKeyCode=code,
                          text=("\r" if key == "Enter" and event == "keyDown" else ""))
            self.after_input = action
            return {"executed": action["id"]}
        try:
            result = super().act(action, page, text=text)
            if action["kind"] == "click" and action.get("role") in ("button", "combobox", "checkbox", "radio", "link"):
                self._await_controls_change()
            return result
        except StalePage as stale:
            covered = "covered" in str(stale) and action["kind"] == "click" and type(action.get("node")) is int
            if not covered:
                raise
        # The centre is covered by a decorative layer of the control itself (ripple, styled checkbox,
        # label). Nothing has been dispatched yet. Try other points inside the observed node or its
        # label; still a real click on the observed element, still preceded by the freshness check.
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        response = self.call("Runtime.evaluate", expression=UNCOVERED_POINT + "(" + json.dumps({"node": action["node"]}) + ")",
                             awaitPromise=True, returnByValue=True)
        point = None if response.get("exceptionDetails") else response.get("result", {}).get("value")
        if not point:
            raise StalePage("Target changed or is covered. Observe again.")
        for event in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=event, x=point["x"], y=point["y"], button="left", clickCount=1)
        self.after_input = action
        return {"executed": action["id"], "point": point}


def select_field_text(context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """TYPE_TEXT without a text model: Jev selects one code-cut span of the goal."""
    candidates = text_candidates(context["goal"])
    key = os.environ.get("TYPESAFE_API_KEY") or config.TYPESAFE_API_KEY
    if not key:
        raise ValueError("TYPESAFE_API_KEY is not set")
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", config.JEV_MODEL),
        "state": {
            "goal": context["goal"],
            "field": context["field"],
            "page": {"title": context["page"]["title"], "text": context["page"]["text"][:2000]},
            "recent_actions": context["recent_actions"],
            "candidates": candidates,
        },
        "questions": {"text_value": {"type": "choice", "criteria": dict(candidates), "instructions": TEXT_SELECT}},
    }
    started = time.perf_counter()
    result = post_json(config.TYPESAFE_URL, key, body)
    answer = validate_choice(result["answers"].get("text_value", {}), candidates)
    return candidates[answer["choice"]], {
        "model": "jev:select",
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }


def resolve_field_text(context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if escalate.enabled():
        try:
            value, meta = escalate.text_value(context)
        except Exception as error:  # noqa: BLE001  (overloaded / network): Jev still selects a span rather than stopping the run
            print(f"  ! text model unavailable ({str(error)[:60]}); Jev selects from the goal", flush=True)
            return select_field_text(context)
        if value is None:
            raise ValueError("The text model found no value for this field in the goal; nothing typed.")
        return value, meta
    if os.environ.get("TEXT_MODEL_API_KEY"):
        return field_text(context)
    return select_field_text(context)


# The ultrafast loop looks the helper up by name at call time; route it through the fallback.
uf_agent.field_text = resolve_field_text
uf_agent.MAX_STEPS = int(os.environ.get("TASK_MAX_STEPS", "80"))


class WebAgent(uf_agent.Agent):
    """jev_ultrafast.Agent on a confined Chrome window, with a step callback."""

    def __init__(
        self,
        goals: str | list[str],
        *,
        url: str | None = None,
        display: str | None = None,
        record_dir: str | Path | None = None,
        screenshots: bool = False,
        on_step: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        self.on_step = on_step
        self.pending_text = None
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        self.site_rules = load_rules(url or DEFAULT_URL)
        apply_rules(self.site_rules)
        trust = load_trust(url or DEFAULT_URL)
        if trust is not None:
            self.TIE_BREAK_BELOW = round(max(0.35, min(0.75, 0.75 - 0.4 * trust)), 2)
        self.browser = ConfinedBrowser(url or DEFAULT_URL, display)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser, goal=task, page=page, decision=None, history=[], status="ready",
            plan=[task], plan_index=0, decisions=[], text_calls=[], elapsed_ms=0, started_at=None,
            record=bool(self.record_dir), escalations=[], original_goal=task, site_rules=list(self.site_rules),
            trust_jev=trust,
        )
        if escalate.enabled() and os.environ.get("ESCALATE_PLAN", "1") not in ("0", "false", "no"):
            try:
                steps, meta = escalate.plan(task, page, self._control_labels())
                self.state["plan"] = steps
                self.state["remaining_steps"] = list(steps)
                self.state["notes"] = []
                self._compose_goal()
                self._record_llm("plan", meta, " | ".join(steps))
            except Exception as e:  # noqa: BLE001
                print(f"  ! planner: {e}", flush=True)
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def _control_labels(self) -> list[str]:
        out = []
        for e in action_space(self.state["page"]["actions"])[0]:
            out.append(f"{e['role']}: {e['label'][:60]}" + (f" = {e['value'][:30]!r}" if e.get("value") else ""))
        return out

    def _compose_goal(self) -> None:
        st = self.state
        remaining = st.get("remaining_steps")
        if remaining is None:
            return
        base = st["original_goal"]
        if remaining:
            base += "\nChecklist, still to do in order (items not listed are already done, do not redo them):\n" + \
                "\n".join(f"{i + 1}. {x}" for i, x in enumerate(remaining))
        else:
            base += "\nEvery checklist item is already done. Choose DONE if the requested final state is visible; do not redo filters."
        for note in st.get("notes", []):
            base += "\n" + note
        st["goal"] = base

    def _progress(self, why: str) -> None:
        """Re-check the checklist against the page; drop completed items from the goal (evidence required)."""
        steps = self.state.get("plan") or []
        if not escalate.enabled() or not steps or len(steps) == 1 and steps[0] == self.state["original_goal"]:
            return
        if not any(h.get("page_changed") for h in self.state["history"]):
            return  # nothing has happened yet; nothing can be done
        current = self.state.get("remaining_steps")
        if current is None:
            current = list(steps)
        try:
            remaining, meta = escalate.progress(self.state["original_goal"], current, self.state["page"], self.state["history"],
                                                self.state["page"].get("screenshot"))
        except Exception as e:  # noqa: BLE001
            print(f"  ! progress: {e}", flush=True)
            return
        dropped = [x for x in current if x not in remaining]
        if len(dropped) > 2:
            # A checker that clears half the list at once is guessing: keep the order, drop only the first two.
            remaining = [x for x in current if x not in dropped[:2]]
        self.state["remaining_steps"] = remaining
        self._record_llm("progress", meta, f"({why}) remaining: " + (" | ".join(remaining) or "none"))
        self._compose_goal()
        self._cached = None

    def _cycle(self) -> list[str]:
        """Choices that keep repeating without leaving the page (A,B,A,B or A,B,C,A,B,C)."""
        h = self.state["history"]
        if len(h) < 4:
            return []
        recent = h[-12:]
        if len({x["url"].split("?")[0] for x in recent}) != 1:
            return []
        recent = [x for x in recent if not (x["kind"] in ("scroll", "scroll_region") and x.get("page_changed"))]
        if len(recent) < 4:
            return []
        labels = [x["choice"] for x in recent]
        for period in (1, 2, 3, 4, 5, 6):
            if len(labels) >= 2 * period and labels[-period:] == labels[-2 * period:-period]:
                return sorted(set(labels[-period:]))
        return []

    def _record_llm(self, slot: str, meta: dict[str, Any], value: str) -> None:
        for call in meta.get("tiers") or [meta]:
            if call.get("usage"):
                self.state["text_calls"].append({**{k: call[k] for k in ("model", "latency_ms", "usage")}, "field": slot, "value": value[:300]})
        self.state["escalations"].append({"slot": slot, "model": meta["model"], "latency_ms": meta["latency_ms"], "value": value[:300],
                                          "step": len(self.state["history"])})
        print(f"  ⚡ {meta['model']} {slot} ({meta['latency_ms']} ms): {value[:120]}", flush=True)

    # ------------------------------------------------------------ escalation slots

    TIE_BREAK_BELOW = float(os.environ.get("ESCALATE_BELOW", "0.6"))
    VERIFY_MAX = 2

    TARGETS_PER_OPERATION = 4

    def _candidates(self, d: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        """Jev's top operations, each with its top few (already validated) targets → id: (description, patch)."""
        _elements, targets, controls = action_space(self.state["page"]["actions"])
        out: dict[str, tuple[str, dict[str, Any]]] = {}
        ranked = sorted(d["operation_probabilities"].items(), key=lambda kv: -kv[1])
        for operation, p in ranked[:6]:
            if operation in targets:
                try:
                    answer = validate_choice(d["raw_answers"].get(operation.lower() + "_target", {}), targets[operation])
                except ValueError:
                    continue
                top = sorted(answer["probabilities"].items(), key=lambda kv: -kv[1])[: self.TARGETS_PER_OPERATION]
                for index, tp in top:
                    action = targets[operation][index]
                    label = f"{operation} [{index}] {action['label']}"
                    if action.get("value"):
                        label += f" (current value: {action['value'][:40]!r})"
                    if action.get("checked") is not None:
                        label += f" (checked: {action['checked']})"
                    patch = {"choice": action["id"], "operation": operation, "target": index,
                             "probabilities": {a["id"]: answer["probabilities"][i] for i, a in targets[operation].items()},
                             "target_probabilities": answer["probabilities"], "target_confidence": answer["confidence"]}
                    out[f"{operation}:{index}"] = (f"{label} (Jev {p:.0%} × target {tp:.0%})", patch)
                continue
            elif operation in controls:
                continue  # controls are added below regardless of Jev's ranking
            elif operation in ("DONE", "BLOCKED"):
                label = "DONE: every requirement is visibly satisfied" if operation == "DONE" else "BLOCKED: nothing offered can progress"
                patch = {"choice": operation, "operation": operation, "target": None, "probabilities": {operation: p},
                         "target_probabilities": {}, "target_confidence": None}
            else:
                continue
            out[operation] = (f"{label} (Jev {p:.0%})", patch)
        for operation, control in controls.items():
            p = d["operation_probabilities"].get(operation, 0.0)
            out[operation] = (f"{operation}: {control['label']} (Jev {p:.0%})",
                              {"choice": control["id"], "operation": operation, "target": None,
                               "probabilities": {control["id"]: p}, "target_probabilities": {}, "target_confidence": None})
        return out

    def _tie_break(self) -> None:
        """Escalate a weak or BLOCKED decision to the fast model; it picks among Jev's own candidates."""
        d = self.state.get("decision")
        if not d or not escalate.enabled():
            return
        weak = d["confidence"] < self.TIE_BREAK_BELOW
        trouble = bool(self._last_cycle) and self.state["history"] and self.state["history"][-1]["choice"] in self._last_cycle
        trouble = trouble or self._failures[1] > 0
        incident, self._incident = self._incident, None
        if d["choice"] != "BLOCKED" and not weak and not trouble and not incident:
            return  # Jev handles it alone
        candidates = self._candidates(d)
        if len(candidates) < 2:
            return
        try:
            choice, why, text, meta = escalate.tie_break(self.state["goal"], self.state["page"], {k: v[0] for k, v in candidates.items()},
                                                         self.state["history"], self.site_rules, force_strong=bool(incident), incident=incident)
        except Exception as e:  # noqa: BLE001
            print(f"  ! tie-break: {e}", flush=True)
            return
        tier = meta.get("tier", "fast")
        slot = "tie-break" if tier == "fast" else "second opinion"
        detail = f"{choice}: {why}" + (f" → type {text!r}" if text else "")
        if tier == "strong":
            detail += f" (overrode {meta['fast_choice']})" if meta.get("fast_choice") != choice else " (agreed with the fast tier)"
        self._record_llm(slot, meta, detail)
        if tier == "strong":
            trust = meta.get("trust_jev")
            if isinstance(trust, (int, float)):
                # Fable's read on Jev: the more it trusts Jev here, the less often we interrupt it.
                self.TIE_BREAK_BELOW = round(max(0.35, min(0.75, 0.75 - 0.4 * float(trust))), 2)
                self.state["trust_jev"] = float(trust)
                save_trust(self.state["page"]["url"], float(trust))
                print(f"  ◎ trust in Jev on this site: {float(trust):.2f} → escalate below {self.TIE_BREAK_BELOW}", flush=True)
            if meta.get("rule"):
                # Used for the rest of this run; written to disk only if the run ends in a verified DONE.
                self._pending_rules.append(meta["rule"])
                apply_rules(self.site_rules + self._pending_rules)
                self.state["site_rules"] = self.site_rules + self._pending_rules
                self._record_llm("rule", {"model": meta["model"], "latency_ms": 0, "usage": {}}, meta["rule"] + " (kept if this run succeeds)")
        patch = candidates[choice][1]
        if patch["operation"] == "TYPE_TEXT" and text:
            # The model that chose the field also supplies its value: cache it under the exact helper
            # context the executor will compute, so no second text call is made.
            action = next(a for a in self.state["page"]["actions"] if a["id"] == patch["choice"])
            context = field_context(self.state["goal"], action, self.state["page"], self.state["history"])
            helper = {"model": meta["model"] + " (tie-break)", "latency_ms": 0, "usage": {}}
            self.pending_text = (context, text, helper)
            self.state["text_calls"].append({**helper, "field": action["label"], "value": text})
        self.state["decision"] = {**d, **patch, "escalated": {"model": meta["model"], "why": why, "latency_ms": meta["latency_ms"],
                                                              "jev_choice": d["operation"]}}
        if self.state["decisions"]:
            self.state["decisions"][-1]["escalated"] = self.state["decision"]["escalated"]

    def _verify_done(self) -> bool:
        """DONE chosen: ask the fast model to check the page against the goal. Returns True to accept."""
        if not escalate.enabled():
            return True
        verifications = [e for e in self.state["escalations"] if e["slot"] == "verify"]
        if len(verifications) >= self.VERIFY_MAX:
            return True
        page = self.state["page"]
        shot = page.get("screenshot")
        if not shot:
            try:
                shot = self.browser.call("Page.captureScreenshot", format="jpeg", quality=60)["data"]
            except Exception:  # noqa: BLE001
                shot = None
        try:
            done, unmet, meta = escalate.verify_done(self.state["goal"], page, shot)
        except Exception as e:  # noqa: BLE001
            print(f"  ! verifier: {e}", flush=True)
            return True
        self._record_llm("verify", meta, "done" if done else f"not done: {unmet}")
        if done:
            return True
        note = f"Verified NOT yet done: {unmet}. Do that before choosing DONE."
        self.state.setdefault("notes", [])
        if note not in self.state["notes"]:
            self.state["notes"].append(note)
        if self.state.get("remaining_steps") is not None:
            self._compose_goal()
        else:
            self.state["goal"] = self.state["original_goal"] + "\n" + "\n".join(self.state["notes"])
        self._cached = None
        return False

    # ------------------------------------------------------------ state for the inspector

    def snapshot(self) -> dict[str, Any]:
        base = super().snapshot()
        page = self.state["page"]
        screen = {
            "app": "Google Chrome", "title": page["title"], "url": page["url"], "text": page["text"],
            "actions": page["actions"], "fingerprint": page["fingerprint"], "screenshot": page.get("screenshot"),
            "view": {"x": 0, "y": 0, "w": page["w"], "h": page["h"]},
            "visited": len(page["actions"]), "omitted_actions": page.get("omitted_actions", 0), "truncated": False,
        }
        return {**{k: v for k, v in base.items() if k != "page"}, "screen": screen, "driver": "browser"}

    _depth = 0
    blocked_grace = 0
    BLOCKED_MIN_CONFIDENCE = 0.5  # a terminal choice needs more than a plurality among 8+ operations
    BLOCKED_GRACE = 3  # per screen: reset whenever an action changes the page

    FUTILE_REPEATS = 2
    RECENT_WINDOW = 6

    def _prune_recent(self) -> None:
        """A control clicked within the last few steps on this same URL is withdrawn for this decision
        (re-opening a panel that was just set discards the selection; the apply button is not affected
        because applying changes the URL)."""
        page = self.state["page"]
        path = page["url"].split("?")[0]
        used = {node for node, p in self._recent_nodes if p == path} | self._dead_nodes
        if self._last_cycle:
            used |= {a.get("node") for a in page["actions"] if a["id"] in self._last_cycle and type(a.get("node")) is int}
        if not used:
            return
        kept = [a for a in page["actions"] if a.get("node") not in used or a["kind"] != "click"]
        if len(kept) < len(page["actions"]):
            page["actions"] = kept

    GENERIC_LABEL = re.compile(r"^(?:Open )?(?:Minimum|Maximum)\s*(?:Range|value)?$|^(?:Open )?(?:Min|Max)$|^(?:Open )?From$|^(?:Open )?To$", re.I)

    def _augment_context_labels(self) -> None:
        """'Maximum Range' → 'Maximum Range (Price)': disambiguate identical field labels by section."""
        page = self.state["page"]
        targets = {a["node"] for a in page["actions"] if type(a.get("node")) is int and self.GENERIC_LABEL.match(a["label"].removeprefix("Open ").strip())}
        if not targets:
            return
        try:
            names = self.browser.evaluate(CONTEXT_LABELS + "(" + json.dumps(sorted(targets)) + ")") or {}
        except StalePage:
            return
        for a in page["actions"]:
            ctx = names.get(str(a.get("node")))
            if ctx and f"({ctx})" not in a["label"]:
                a["label"] = f"{a['label']} ({ctx})"

    def _augment_label_toggles(self) -> None:
        """Add label-wrapped checkboxes/radios to the offered elements for this decision."""
        page = self.state["page"]
        try:
            found = self.browser.evaluate(LABEL_TOGGLES) or []
        except StalePage:
            return
        known = {a.get("node") for a in page["actions"]}
        n = sum(1 for a in page["actions"] if a["id"].startswith("e"))
        added = 0
        for t in found:
            if t["node"] in known:
                continue
            n += 1
            page["actions"].append({"id": f"e{n}", "kind": "click", "node": t["node"], "role": t["role"], "label": t["label"] or t["role"],
                                    "value": "", "checked": t["checked"], "rect": t["rect"]})
            page["guards"][str(t["node"])] = t["guard"]
            added += 1
        if added:
            print(f"  + {added} label-wrapped toggles offered", flush=True)

    def _prune_behind_modal(self) -> None:
        """Offer only the open modal's controls while one is showing (code owns the action space)."""
        page = self.state["page"]
        ids = sorted({a["node"] for a in page["actions"] if type(a.get("node")) is int})
        if not ids:
            return
        try:
            inside = self.browser.evaluate(MODAL_NODES + "(" + json.dumps(ids) + ")")
        except StalePage:
            return
        if inside is None:
            return
        keep = set(inside)
        kept = [a for a in page["actions"] if type(a.get("node")) is not int or a["node"] in keep]
        if len(kept) < len(page["actions"]):
            print(f"  ⊘ modal open: withdrew {len(page['actions']) - len(kept)} controls behind it", flush=True)
            page["actions"] = kept

    def _prune_futile(self) -> None:
        """An action repeated FUTILE_REPEATS times in a row without leaving the page is withdrawn
        from the offered set for this decision (code owns the action space; the model only picks
        from it). Typical case: re-clicking a filled field whose autocomplete never appears."""
        history = self.state["history"]
        if len(history) < self.FUTILE_REPEATS:
            return
        recent = history[-self.FUTILE_REPEATS:]
        first = recent[0]
        if first["kind"] not in {"click", "fill", "select"}:
            return
        if any(h["choice"] != first["choice"] or h["kind"] != first["kind"] or h["url"] != first["url"] for h in recent):
            return
        page = self.state["page"]
        kept = [a for a in page["actions"] if a["id"] != first["choice"]]
        if len(kept) < len(page["actions"]):
            page["actions"] = kept
            print(f"  ⊘ withdrew {first['action']!r} for this decision ({self.FUTILE_REPEATS} futile repeats)", flush=True)

    def _hesitant_blocked(self) -> None:
        """A low-confidence BLOCKED executes the runner-up operation instead. Every target head was
        answered speculatively in the same request, so the runner-up's target is already validated
        against the observed page; nothing new is asked of the model."""
        d = self.state.get("decision")
        if not d or d["choice"] != "BLOCKED" or d["confidence"] >= self.BLOCKED_MIN_CONFIDENCE or self.blocked_grace >= self.BLOCKED_GRACE:
            return
        _elements, targets, controls = action_space(self.state["page"]["actions"])
        ranked = sorted(d["operation_probabilities"].items(), key=lambda kv: -kv[1])
        for operation, p in ranked:
            if operation in {"BLOCKED", "DONE"} or p <= 0:
                continue
            if operation in targets:
                try:
                    answer = validate_choice(d["raw_answers"].get(operation.lower() + "_target", {}), targets[operation])
                except ValueError:
                    continue
                choice = targets[operation][answer["choice"]]["id"]
                probabilities = {a["id"]: answer["probabilities"][i] for i, a in targets[operation].items()}
                target, target_answer = answer["choice"], answer
            elif operation in controls:
                choice, probabilities, target, target_answer = controls[operation]["id"], {controls[operation]["id"]: p}, None, None
            else:
                continue
            self.blocked_grace += 1
            self.state["decision"] = {**d, "choice": choice, "operation": operation, "target": target, "probabilities": probabilities,
                                      "target_probabilities": target_answer["probabilities"] if target_answer else {},
                                      "target_confidence": target_answer["confidence"] if target_answer else None,
                                      "confidence": p}
            print(f"  ↷ hesitant BLOCKED ({d['confidence']:.2f}); taking runner-up {operation} {target or ''}", flush=True)
            return

    SAME_ACTION_FAILURES = 3
    _dead_nodes: set[int] = set()
    _pending_rules: list[str] = []
    _incident: str | None = None
    _incidents = 0
    _recent_nodes: list[tuple[int, str]] = []
    _unblocked: set[str] = set()
    _last_cycle: list[str] = []
    _cycles = 0
    _cached: dict[str, Any] | None = None
    _failures: tuple[str, int] = ("", 0)

    def command(self, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        before = len(self.state["history"])
        self._depth += 1
        try:
            if name == "tick":
                result = self._tick()
            else:
                if name == "predict":
                    cached = self._cached
                    page = self.state["page"]
                    dead_choice = bool(cached) and any(a["id"] == cached["decision"]["choice"] and a.get("node") in self._dead_nodes
                                                       for a in page["actions"])
                    if cached and not dead_choice and self._failures[1] < 2 and not self._incident \
                            and cached["fingerprint"] == page["fingerprint"] and cached["history_len"] == len(self.state["history"]) \
                            and self.browser.fresh(page):
                        # Same page, nothing executed since: the previous answer still applies. No new request.
                        self.state["decision"] = cached["decision"]
                        self.state["status"] = "predicted"
                        return self.snapshot()
                if name == "predict":
                    cycle = self._cycle()
                    if cycle and cycle != self._last_cycle:
                        self._last_cycle = cycle
                        self._cycles += 1
                        print(f"  ⟲ cycle detected: {cycle}; withdrawing them and re-checking progress", flush=True)
                        page = self.state["page"]
                        page["actions"] = [a for a in page["actions"] if a["id"] not in cycle]
                        self._progress("cycle")
                        if self._cycles >= 3:
                            self.state["status"] = "blocked"
                            self.state["last_error"] = f"Kept cycling through {cycle}"
                            raise ValueError(self.state["last_error"])
                    self._augment_context_labels()
                    self._augment_label_toggles()
                    self._prune_futile()
                    self._prune_recent()
                    self._prune_behind_modal()
                if name == "act":
                    self._hesitant_blocked()
                result = self._command_with_retry(name, body)
                if name == "predict" and self.state.get("decision"):
                    self._tie_break()
                    self._cached = {"fingerprint": self.state["page"]["fingerprint"], "history_len": len(self.state["history"]),
                                    "decision": self.state["decision"]}
                if name == "act" and len(self.state["history"]) > before:
                    self._cached, self._failures = None, ("", 0)
                    h = self.state["history"][-1]
                    node = next((a.get("node") for a in self.state["page"]["actions"] if a["id"] == h["choice"]), None)
                    if type(node) is int and h["kind"] == "click":
                        self._recent_nodes = (self._recent_nodes + [(node, h["url"].split("?")[0])])[-self.RECENT_WINDOW:]
                    if self.state["status"] == "blocked" and escalate.enabled():
                        # ultrafast's no-progress stop (three unchanged actions). With a tie-breaker available,
                        # withdraw what was tried and let it choose differently, once per screen.
                        recent = self.state["history"][-3:]
                        fp = self.state["page"]["fingerprint"]
                        if len(recent) == 3 and all(not x.get("page_changed") for x in recent) and fp not in self._unblocked:
                            self._unblocked.add(fp)
                            self.state["status"] = "ready"
                            self._last_cycle = sorted({x["choice"] for x in recent})
                            print(f"  ⟲ no progress from {self._last_cycle}; withdrawing them and escalating", flush=True)
                    if h.get("page_changed"):
                        self.blocked_grace = 0
                    navigated = len(self.state["history"]) < 2 or h["url"] != self.state["history"][-2]["url"] or \
                        self.state["page"]["url"] != h["url"]
                    if navigated and self.state.get("remaining_steps"):
                        self._progress("navigated")
        finally:
            self._depth -= 1
        if self.on_step and self._depth == 0:
            if len(self.state["history"]) > before:
                h = self.state["history"][-1]
                self.on_step({"action": {"label": h["action"], "kind": h["kind"], "key": None}, "operation": h["operation"],
                              "text": h["text"], "step": h["step"]})
            elif self.state["status"] in {"done", "blocked"} and name in {"act", "tick"}:
                self.on_step({"action": None, "operation": self.state["status"].upper(), "text": None, "step": len(self.state["history"])})
        return result

    _done_streak = 0

    def _command_with_retry(self, name: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """Transient model-provider failures (5xx, malformed answer) must not end a run: back off and re-ask.
        Only 'predict' is retried; a mutation is never re-issued."""
        attempt = 0
        while True:
            try:
                return super().command(name, body)
            except (RuntimeError, ValueError) as error:
                text = str(error)
                transient = name == "predict" and ("HTTP 5" in text or "Invalid TypeSafe" in text or "Model unavailable" in text
                                                   or "connection failed" in text)
                if not transient or attempt >= 5:
                    raise
                attempt += 1
                delay = min(8.0, 1.5 * attempt)
                print(f"  ! {text[:80]} — retrying in {delay:.0f}s ({attempt}/5)", flush=True)
                time.sleep(delay)
                self.state["page"] = self.browser.observe(screenshot=self.screenshots)

    def _tick(self) -> dict[str, Any]:
        try:
            self.command("predict", {})
            d = self.state.get("decision")
            if d and d["choice"] == "DONE" and not self._verify_done():
                self.state["decision"] = None
                self.state["status"] = "ready"
                self._done_streak = 0
                return self.snapshot()
            if d and d["choice"] == "DONE":
                self._done_streak += 1
                if self._done_streak >= 2 or escalate.enabled():
                    # A live page (ads, counters) never passes the strict marker check. A second
                    # consecutive DONE stands; the outcome is verified independently, not by the choice.
                    self.state["decision"] = None
                    self.state["status"] = "done"
                    self.state["results_url"] = self.state["page"]["url"]  # the verified, filtered results: always return here
                    for rule in self._pending_rules:
                        self.site_rules = save_rule(self.state["page"]["url"], rule)
                    self._pending_rules = []
                    self.state["elapsed_ms"] = round((time.perf_counter() - self.state["started_at"]) * 1000)
                    if self.on_step:
                        self.on_step({"action": None, "operation": "DONE", "text": None, "step": len(self.state["history"])})
                    return self.snapshot()
            else:
                self._done_streak = 0
            return self.command("act", {"fingerprint": self.state["page"]["fingerprint"]})
        except StalePage as stale:
            self.stale(stale)
            return self.snapshot()

    def stale(self, error: Exception) -> None:
        """A decision was rejected before input: observe again and choose again.
        The same choice rejected repeatedly on an unchanged page stops the run with the reason."""
        state = self.state
        key = f"{state['page']['fingerprint']}:{(self._cached or {}).get('decision', {}).get('choice')}"
        self._failures = (key, self._failures[1] + 1) if self._failures[0] == key else (key, 1)
        print(f"  ↻ stale: {error}", flush=True)
        state["decision"] = None
        state["status"] = "ready"
        state["last_error"] = str(error)
        state["page"] = self.browser.observe(screenshot=self.screenshots)
        if self._failures[1] >= self.SAME_ACTION_FAILURES and state["page"]["fingerprint"] == key.split(":")[0]:
            # The executor refused this action repeatedly (covered target, vanished node). Withdraw it and hand
            # the incident to the strong tier before ever giving up; only a second incident on the same page stops.
            choice = key.split(":", 1)[1]
            action = next((a for a in state["page"]["actions"] if a["id"] == choice), None)
            label = action["label"] if action else choice
            if action and type(action.get("node")) is int:
                self._dead_nodes.add(action["node"])
            self._incidents += 1
            self._failures = ("", 0)
            if self._incidents > 2 or not escalate.enabled():
                state["status"] = "blocked"
                state["last_error"] = f"Execution kept failing on an unchanged page: {error}"
                raise ValueError(state["last_error"])
            self._incident = (f"The executor rejected '{label}' {self.SAME_ACTION_FAILURES} times on this page: {error} "
                              "It has been withdrawn. Something (an open list, overlay, or sticky header) is probably covering it.")
            self._cached = None
            print(f"  ⚑ incident: {self._incident[:120]}", flush=True)

    def run(self) -> Iterator[dict[str, Any]]:
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def continue_with(self, goal: str) -> None:
        """Start a new goal on the same tab (history and decisions reset; the page is re-observed)."""
        st = self.state
        st.update(goal=goal, original_goal=goal, decision=None, history=[], status="ready", plan=[goal], plan_index=0,
                  remaining_steps=None, notes=[], started_at=None)
        self.pending_text, self._cached, self._failures, self._last_cycle, self._cycles = None, None, ("", 0), [], 0
        self._dead_nodes, self._incident, self._incidents = set(), None, 0
        self.blocked_grace, self._done_streak = 0, 0
        st["escalations"] = [e for e in st["escalations"] if e["slot"] != "verify"]
        st["page"] = self.browser.observe(screenshot=self.screenshots)
        if escalate.enabled() and os.environ.get("ESCALATE_PLAN", "1") not in ("0", "false", "no"):
            try:
                steps, meta = escalate.plan(goal, st["page"], self._control_labels())
                st["plan"], st["remaining_steps"] = steps, list(steps)
                self._compose_goal()
                self._record_llm("plan", meta, " | ".join(steps))
            except Exception as e:  # noqa: BLE001
                print(f"  ! planner: {e}", flush=True)

    def message_seller(self, text: str) -> dict[str, Any]:
        """Phase three: on the open listing, send the seller exactly ``text`` (one message, then stop)."""
        goal = (
            f"On this listing page, send the seller one message with exactly this text: \"{text}\". "
            "Use the listing's Message / Send message / Contact seller control, type the message into the message box "
            "(replace any prefilled text), then press the send button or enter. Send it once. "
            "Stop when the message shows as sent (it appears in the conversation, or a 'sent' confirmation is visible)."
        )
        self.continue_with(goal)
        for _ in self.run():
            pass
        return {"status": self.state["status"], "text": text, "url": self.state["page"]["url"], "actions": len(self.state["history"])}

    def message_sellers(self, text: str, max_sellers: int = 1) -> list[dict[str, Any]]:
        """Message up to ``max_sellers`` of the recommended candidates, best first. Each candidate is opened by its
        saved URL (no searching again), messaged once, and the outcome recorded in state['messages']."""
        from . import recommend as rc

        rec = self.state.get("recommendation")
        if not rec:
            raise ValueError("Recommend first")
        required = rc.must_match(self.state["original_goal"])
        candidates = [c for c in [rec["listing"]] + [c for c in rec.get("ranked_full", []) if c is not rec["listing"]]
                      if rc.matches_goal(c, required)]
        results: list[dict[str, Any]] = self.state.setdefault("messages", [])
        done_urls = {m["url"] for m in results}
        for candidate in candidates:
            if len(results) >= max_sellers:
                break
            url = candidate.get("href")
            if not url or url in done_urls:
                continue
            if self.state["page"]["url"].split("?")[0] != url.split("?")[0]:
                rc.open_listing(self.browser, candidate)
                time.sleep(0.8)
            outcome = self.message_seller(text)
            outcome["listing"] = rc._summary(candidate)
            results.append(outcome)
            done_urls.add(url)
            print(f"  ✉ {outcome['status'].upper()} · {outcome['listing'][:60]}", flush=True)
        if self.state.get("results_url"):
            try:
                self.browser.call("Page.navigate", url=self.state["results_url"])
                time.sleep(1.0)
                self.state["page"] = self.browser.observe(screenshot=self.screenshots)
            except Exception:  # noqa: BLE001
                pass
        return results

    def recommend(self, pages: int | None = None, open_result: bool = True) -> dict[str, Any]:
        """After the run: harvest listings, let Jev pick one, open it. Stored in state['recommendation']."""
        from . import recommend as rc

        started = time.perf_counter()
        results_url = self.state.get("results_url")
        if results_url and self.state["page"]["url"] != results_url:
            self.browser.call("Page.navigate", url=results_url)
            time.sleep(1.5)
        listings = rc.harvest(self.browser, pages or rc.MAX_PAGES)
        rec = rc.pick(self.state["original_goal"], listings)
        rec["harvest_ms"] = round((time.perf_counter() - started) * 1000) - sum(c["latency_ms"] for c in rec["calls"])
        self.state["text_calls"].extend({**c, "field": "recommendation", "value": rec["summary"]} for c in rec["calls"])
        self.state["recommendation"] = {k: v for k, v in rec.items() if k != "request"}
        self.state["recommendation"]["ranked_full"] = [x for x in rec.get("ranked_listings", []) if x is not rec["listing"]]
        if open_result:
            rc.open_listing(self.browser, rec["listing"])
            time.sleep(0.8)
            try:
                self.state["page"] = self.browser.observe(screenshot=self.screenshots)
            except StalePage:
                pass
        if self.on_step:
            self.on_step({"action": {"label": rec["summary"], "kind": "recommend", "key": None}, "operation": "RECOMMEND",
                          "text": rec["reason"], "step": len(self.state["history"])})
        return rec


__all__ = ["WebAgent", "ConfinedBrowser", "StalePage", "MAX_STEPS", "action_space", "choose", "field_context", "DEFAULT_URL"]
