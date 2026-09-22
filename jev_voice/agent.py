"""The complete desktop agent loop. Typed choices, observable state, bounded execution.

Port of jev-ultrafast/agent.py: observe → one Jev request (operation + speculative targets)
→ consume the matching target → execute against a code-owned AX node → log → observe.

    uv run jev-agent --goal "open notes and write buy milk"
    uv run jev-agent --goal "..." --choose      # pause before each execution
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .costs import run_costs, usd
from .desktop import Desktop, StaleScreen
from .policy import action_space, choose, field_context, field_text
from .questions import MAX_STEPS

STALE_STREAK_LIMIT = 4  # a screen that never settles must not livelock benign, non-element actions
SAME_ACTION_FAILURES = 3  # the same choice rejected this many times on an unchanged screen stops the run
BLOCKED_MIN_CONFIDENCE = 0.3  # a hesitant BLOCKED waits first (at most BLOCKED_GRACE times)
BLOCKED_GRACE = 2


class Agent:
    def __init__(
        self,
        goals: str | list[str],
        *,
        desktop: Desktop | None = None,
        record_dir: str | Path | None = None,
        screenshots: bool = False,
        on_step: Callable[[dict[str, Any]], None] | None = None,
        text_model: bool | None = None,
        display: str | None = None,
    ) -> None:
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        self.pending_text: tuple[dict, str, dict | None] | None = None
        self.desktop = desktop or Desktop(display=display)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        self.on_step = on_step
        self.text_model = text_model
        self.cancel = threading.Event()
        self.stale_streak = 0
        self.blocked_grace = 0
        self.last_decision: dict[str, Any] | None = None   # reused, free of charge, while the screen is unchanged
        self.failures: tuple[str, int] = ("", 0)            # (fingerprint+choice, consecutive stale rejections)
        screen = self.desktop.observe(screenshot=self.screenshots)
        self.state: dict[str, Any] = dict(
            goal=task,
            screen=screen,
            decision=None,
            history=[],
            status="ready",
            decisions=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(screen.get("screenshot", "")))

    # -------------------------------------------------------------- state

    def snapshot(self) -> dict[str, Any]:
        return {**self.state, "elements": action_space(self.state["screen"]["actions"])[0]}

    def _elapsed(self) -> int:
        return round((time.perf_counter() - self.state["started_at"]) * 1000) if self.state["started_at"] else 0

    # -------------------------------------------------------------- commands

    def command(self, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["screen"]["fingerprint"]})
            except StaleScreen as stale:
                self.stale(stale)
                return self.snapshot()
        elif name == "predict":
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if self.cancel.is_set():
                state["status"] = "blocked"
                raise ValueError("Cancelled")
            if not self.desktop.fresh(state["screen"]):
                state["screen"] = self.desktop.observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh one.")
            if len(state["decisions"]) >= MAX_STEPS * 2:
                raise ValueError("Reached the model-call budget")
            cached = self.last_decision
            if cached and cached["fingerprint"] == state["screen"]["fingerprint"] and cached["history_len"] == len(state["history"]):
                # Same screen, nothing executed since: the previous answer still applies. No new request.
                state["decision"] = cached["decision"]
                state["status"] = "predicted"
                return self.snapshot()
            state["decision"] = choose(state["screen"], state["goal"], state["history"], text_model=self.text_model)
            self.last_decision = {"fingerprint": state["screen"]["fingerprint"], "history_len": len(state["history"]), "decision": state["decision"]}
            state["decisions"].append(
                {
                    **{k: v for k, v in state["decision"].items() if k != "request"},
                    "fingerprint": state["screen"]["fingerprint"],
                    "elapsed_ms": self._elapsed(),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, screen = state["decision"], state["screen"]
            if not decision or body.get("fingerprint") != screen["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected == "BLOCKED" and decision["confidence"] < BLOCKED_MIN_CONFIDENCE and self.blocked_grace < BLOCKED_GRACE:
                wait = next((a for a in screen["actions"] if a["id"] == "wait"), None)
                if wait is not None:
                    self.blocked_grace += 1
                    selected = "wait"
                    decision = {**decision, "choice": "wait", "operation": "WAIT", "target": None,
                                "probabilities": {"wait": decision["probabilities"].get("BLOCKED", 0.0)}}
            if selected in {"DONE", "BLOCKED"}:
                if self.stale_streak < STALE_STREAK_LIMIT and not self.desktop.fresh(screen):
                    state["status"] = "ready"
                    raise StaleScreen("Screen changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["elapsed_ms"] = self._elapsed()
                self._notify(None, selected)
                return self.snapshot()
            action = next(a for a in screen["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not self.desktop.fresh(screen, action):
                    raise StaleScreen("Screen changed before text generation. Choose again.")
                context = field_context(state["goal"], action, screen, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                elif decision.get("selected_text") is not None:
                    text, helper = decision["selected_text"], {"model": "jev:select", "latency_ms": 0}
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Desktop.act checks freshness immediately before input, including after text generation.
            # A screen that never settles (live badges, tickers) may still be scrolled or waited on.
            force = action["kind"] in ("scroll", "wait") and self.stale_streak >= STALE_STREAK_LIMIT
            self.desktop.act(action, screen, text=text, force=force)
            self.pending_text = None
            self.stale_streak = 0
            self.failures = ("", 0)
            self.last_decision = None
            state["elapsed_ms"] = self._elapsed()
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"][selected],
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "screen_changed": None,
                    "app": screen["app"],
                    "usage": decision["usage"],
                    "executed_ms": self._elapsed(),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            self._notify(action, decision["operation"], text)
            state["screen"] = self.desktop.observe(screenshot=self.screenshots)
            state["elapsed_ms"] = self._elapsed()
            state["history"][-1].update(
                screen_changed=state["screen"]["fingerprint"] != screen["fingerprint"],
                app=state["screen"]["app"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(  # type: ignore[operator]
                    base64.b64decode(state["screen"].get("screenshot", ""))
                )
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["screen_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def stale(self, error: StaleScreen) -> None:
        """A decision was rejected before input. Re-observe; give up on a choice that keeps failing."""
        state = self.state
        self.stale_streak += 1
        key = f"{state['screen']['fingerprint']}:{(self.last_decision or {}).get('decision', {}).get('choice')}"
        self.failures = (key, self.failures[1] + 1) if self.failures[0] == key else (key, 1)
        state["decision"] = None
        state["status"] = "ready"
        state["last_error"] = str(error)
        state["screen"] = self.desktop.observe(screenshot=self.screenshots)
        state["elapsed_ms"] = self._elapsed()
        if self.failures[1] >= SAME_ACTION_FAILURES and state["screen"]["fingerprint"] == key.split(":")[0]:
            state["status"] = "blocked"
            state["last_error"] = f"Execution kept failing on an unchanged screen: {error}"
            raise ValueError(state["last_error"])

    def _notify(self, action: dict[str, Any] | None, operation: str, text: str | None = None) -> None:
        if self.on_step:
            self.on_step({"action": action, "operation": operation, "text": text, "step": len(self.state["history"])})

    def run(self) -> Iterator[dict[str, Any]]:
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self) -> None:
        self.desktop.close()

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


# ------------------------------------------------------------------ CLI


def describe_step(action: dict[str, Any] | None, operation: str, text: str | None) -> str:
    if action is None:
        return operation
    label = action["label"]
    if operation == "TYPE_TEXT":
        return f"Type “{text}” into {label}"
    if operation == "OPEN_APP":
        return f"Open {label}"
    if operation == "PRESS_KEY":
        return f"Press {action['key'].replace('_', ' ')}"
    if operation == "CLICK":
        return f"Click {label}"
    return label


def run_browser(args: argparse.Namespace) -> None:
    from .web import WebAgent

    def show(step: dict[str, Any]) -> None:
        print(f"  ▶ {describe_step(step['action'], step['operation'], step['text'])}")

    display = args.display or os.environ.get("AGENT_DISPLAY") or None
    with WebAgent(args.goal, url=args.url, display=display, record_dir=args.record, on_step=show) as agent:
        print(f"page: {agent.state['page']['url']}")
        for st in agent.run():
            d = st["decisions"][-1] if st["decisions"] else None
            if d:
                print(f"{st['elapsed_ms']:>6} ms  {d['operation']:<9} target={d['target']}  conf={d['confidence']:.2f}  jev {d['latency_ms']} ms")
        st = agent.state
        if args.chess and st["status"] == "done":
            from . import chess_play

            print("\n  ♟ playing…")
            summary = chess_play.play(agent.browser, on_step=show)
            print(f"  ♟ {summary['result']} after {len(summary['moves'])} of our moves")
        rec = None
        if args.recommend and st["status"] != "done":
            print("\n  ✗ no recommendation: the run did not finish (status " + st["status"] + ")")
        elif args.recommend:
            try:
                rec = agent.recommend()
            except ValueError as e:
                print(f"\n  ✗ no recommendation: {e}")
        if args.recommend and rec:
            print(f"\n  ★ {rec['spoken']}")
            print(f"    {rec['considered']} listings read in {rec['harvest_ms']} ms · pick confidence {rec['confidence']:.2f}")
            for r in rec["ranked"][:5]:
                print(f"      {r['p']:.0%}  {r['summary']}\n           {r['href'] or '(no direct link)'}")
            import json as _json
            from datetime import datetime

            runs = Path("runs")
            runs.mkdir(exist_ok=True)
            out = runs / f"{datetime.now():%Y%m%d-%H%M%S}.json"
            out.write_text(_json.dumps({"goal": args.goal, "status": st["status"], "recommendation": rec["summary"], "reason": rec["reason"],
                                        "listing_url": rec["listing"].get("href") or agent.state["page"]["url"],
                                        "candidates": [{"summary": r["summary"], "p": r["p"], "url": r["href"]} for r in rec["ranked"]],
                                        "elapsed_ms": st["elapsed_ms"]}, indent=2))
            print(f"    saved: {out}")
            print(f"    opened: {agent.state['page']['url'][:120]}")
            if args.message:
                print(f"\n  ✉ messaging up to {args.max_messages} seller(s): {args.message!r}")
                for r in agent.message_sellers(args.message, args.max_messages):
                    print(f"  ✉ {r['status'].upper()} after {r['actions']} actions · {r['listing'][:60]}")
        c = run_costs(st["decisions"], st["text_calls"])
        print(f"{st['elapsed_ms']:>6} ms  {len(st['history'])} actions  {st['status'].upper()}  ({c['jev']['requests']} Jev calls, "
              f"{c['jev']['input_tokens']:,} input tokens, {usd(c['total_usd'])})  {st['page']['url'][:100]}")


def main() -> None:
    p = argparse.ArgumentParser(prog="jev-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--goal", action="append", required=True, help="Natural-language goal. Repeat for an ordered list.")
    p.add_argument("--choose", action="store_true", help="pause for Enter before each execution")
    p.add_argument("--record", help="directory for screenshots after every step")
    p.add_argument("--elements", action="store_true", help="print the element table before each decision")
    p.add_argument("--display", default=None, help="confine to one monitor: left, right, main, or an index (default AGENT_DISPLAY)")
    p.add_argument("--driver", choices=["browser", "desktop"], default=os.environ.get("TASK_DRIVER", "browser"),
                   help="browser: Chrome via CDP (jev-ultrafast, default); desktop: Accessibility tree (experimental)")
    p.add_argument("--url", default=None, help="browser driver: page to start on (default TASK_START_URL or Google)")
    p.add_argument("--recommend", action="store_true", help="browser driver: after the run, harvest the listings, let Jev pick one, open it")
    p.add_argument("--message", default=None, help="browser driver: after opening the recommended listing, send the seller this exact message")
    p.add_argument("--max-messages", type=int, default=1, help="message up to this many of the recommended sellers, best first")
    p.add_argument("--chess", action="store_true",
                   help="after the goal (a live chess.com board), play the game: code reads the board, Stockfish/search picks moves")
    args = p.parse_args()
    if args.message:
        args.recommend = True
    if args.driver == "browser":
        return run_browser(args)

    def show(step: dict[str, Any]) -> None:
        print(f"  ▶ {describe_step(step['action'], step['operation'], step['text'])}")

    with Agent(args.goal, record_dir=args.record, on_step=show, display=args.display) as agent:
        print(f"screen: {agent.state['screen']['app']} · {agent.state['screen']['title'][:70]}  display={agent.desktop.display}")
        while agent.state["status"] not in {"done", "blocked"}:
            try:
                agent.command("predict")
            except StaleScreen as e:
                try:
                    agent.stale(e)
                except ValueError as stop:
                    print(f"  ✗ {stop}")
                    break
                continue
            d = agent.state["decision"]
            s = agent.state["screen"]
            if args.elements:
                for e in action_space(s["actions"])[0]:
                    print(f"     [{e['index']}] {e['role']:12} {e['label'][:60]!r} {e.get('value', '')[:30]!r} {','.join(e['operations'])}")
            top = sorted(d["probabilities"].items(), key=lambda kv: -kv[1])[:3]
            print(f"{agent.state['elapsed_ms']:>6} ms  {d['operation']:<9} target={d['target']}  conf={d['confidence']:.2f}  jev {d['latency_ms']} ms  {top}")
            if args.choose:
                if input("  Enter to execute, q to stop: ").strip().lower() == "q":
                    break
            try:
                agent.command("act", {"fingerprint": s["fingerprint"]})
            except StaleScreen as e:
                print(f"  ↻ {e}")
                try:
                    agent.stale(e)
                except ValueError as stop:
                    print(f"  ✗ {stop}")
                    break
        st = agent.state
        c = run_costs(st["decisions"], st["text_calls"])
        print(f"{st['elapsed_ms']:>6} ms  {len(st['history'])} actions  {st['status'].upper()}  ({len(st['decisions'])} Jev calls, "
              f"{c['jev']['input_tokens']:,} input tokens, {usd(c['total_usd'])})")


if __name__ == "__main__":
    sys.exit(main())
