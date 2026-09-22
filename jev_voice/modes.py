"""Browser-use modes: one page, three ways to drive Chrome with Jev.

    uv run jev-modes            # then open http://127.0.0.1:8767

``ultrafast``  jev-ultrafast exactly as the repo shipped: the stock ``Agent`` on a plain CDP tab,
               Jev chooses, the OpenAI-compatible text helper writes TYPE_TEXT values, 60-step budget,
               no guards, no Claude. The Google Flights / fixture scenarios of its demo are kept.
``jev``        Jev Voice's ``WebAgent`` with the Claude escalation switched off: a window confined to one
               monitor, code-owned guards (modal pruning, cycle withdrawal, hesitant BLOCKED, retries),
               and TYPE_TEXT values selected by Jev from spans of the goal when no text model is set.
``agent``      The full agent: ``WebAgent`` with the planner, Jev → Haiku → Fable tie-breaks, the DONE
               verifier, learned site rules and per-site trust. Needs ``ANTHROPIC_API_KEY``.

This server is separate from the inspector (``jev-inspect``): its own port, its own static page.
"""
from __future__ import annotations

import atexit
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jev_ultrafast import agent as uf_agent
from jev_ultrafast import model as uf_model
from jev_ultrafast import questions as uf_questions
from jev_ultrafast.browser import StalePage

# Stock jev-ultrafast behaviour, captured before jev_voice.web patches the module globals.
STOCK = {"field_text": uf_model.field_text, "max_steps": uf_questions.MAX_STEPS, "next_action": uf_questions.NEXT_ACTION}

from . import config, escalate  # noqa: E402  (config loads .env)
from .costs import run_costs  # noqa: E402
from .desktop import display_bounds  # noqa: E402
from .web import DEFAULT_URL, WebAgent  # noqa: E402

# jev_voice.web's patches (text value resolution with Jev-select fallback, TASK_MAX_STEPS).
VOICE = {"field_text": uf_agent.field_text, "max_steps": uf_agent.MAX_STEPS}

ROOT = Path(__file__).parent
STATIC = ROOT / "static_modes"
UF_STATIC = Path(uf_agent.__file__).parent / "static"
PORT = int(os.environ.get("JEV_MODES_PORT", "8767"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
MAX_SECONDS = float(os.environ.get("TASK_MAX_SECONDS", "900"))
ESCALATE_ENV = os.environ.get("ESCALATE")  # the user's own setting, restored for agent mode

FIXTURE_URL = {"travel": f"{ORIGIN}/fixture.html?scenario=travel", "research": f"{ORIGIN}/fixture.html?scenario=research"}
FLIGHTS_URL = "https://www.google.com/travel/flights?hl=en"

MODES: dict[str, dict[str, Any]] = {
    "ultrafast": {
        "title": "Ultrafast",
        "tagline": "jev-ultrafast as it shipped",
        "summary": "Stock Agent on a plain CDP tab. Jev picks the operation and target; a small text model writes TYPE_TEXT. "
                   "No guards, no Claude, 60-step budget.",
        "pipeline": "DOM snapshot → Jev → CDP",
    },
    "jev": {
        "title": "Jev + guards",
        "tagline": "Jev alone, code-owned guards",
        "summary": "Jev Voice's browser driver with Claude switched off: a window confined to one monitor, modal and cycle pruning, "
                   "hesitant BLOCKED, provider retries, TYPE_TEXT selected from the goal when no text model is set.",
        "pipeline": "DOM snapshot → guards → Jev → CDP",
    },
    "agent": {
        "title": "Agent",
        "tagline": "Jev → Haiku → Fable",
        "summary": "The full agent: a Claude planner, tie-breaks on weak or BLOCKED choices, a DONE verifier, learned site rules "
                   "and per-site trust in Jev. Needs ANTHROPIC_API_KEY.",
        "pipeline": "plan → DOM snapshot → guards → Jev ⇄ Claude → CDP",
    },
}

AGENT: uf_agent.Agent | WebAgent | None = None
MODE: str = "ultrafast"
JOB: dict[str, Any] = {"running": False, "error": None, "started_at": None, "stop": False}


def configure(mode: str) -> None:
    """Module globals decide what an ultrafast Agent does at request time; set them for the chosen mode.
    One agent lives at a time, so a mode's settings hold until the next reset."""
    if mode not in MODES:
        raise ValueError("mode must be ultrafast, jev or agent")
    stock = mode == "ultrafast"
    uf_agent.field_text = STOCK["field_text"] if stock else VOICE["field_text"]
    uf_agent.MAX_STEPS = STOCK["max_steps"] if stock else VOICE["max_steps"]
    uf_model.NEXT_ACTION = STOCK["next_action"]  # WebAgent re-applies learned site rules itself
    if mode == "agent":
        if ESCALATE_ENV is None:
            os.environ.pop("ESCALATE", None)
        else:
            os.environ["ESCALATE"] = ESCALATE_ENV
    else:
        os.environ["ESCALATE"] = "0"


def availability() -> dict[str, dict[str, Any]]:
    text_key = bool(os.environ.get("TEXT_MODEL_API_KEY"))
    text_model = os.environ.get("TEXT_MODEL", "inception/mercury-2.5")
    claude = bool(os.environ.get("ANTHROPIC_API_KEY")) and (ESCALATE_ENV or "1") not in ("0", "false", "no")
    return {
        "ultrafast": {"ready": bool(config.TYPESAFE_API_KEY),
                      "text": f"text helper · {text_model}" if text_key else "TYPE_TEXT needs TEXT_MODEL_API_KEY",
                      "warning": None if text_key else "Without TEXT_MODEL_API_KEY the stock agent refuses to type."},
        "jev": {"ready": bool(config.TYPESAFE_API_KEY),
                "text": f"text helper · {text_model}" if text_key else "Jev selects text from the goal", "warning": None},
        "agent": {"ready": bool(config.TYPESAFE_API_KEY) and claude,
                  "text": f"{escalate.FAST_MODEL} → {escalate.MODEL}" if claude else "needs ANTHROPIC_API_KEY",
                  "warning": None if claude else "Set ANTHROPIC_API_KEY (and ESCALATE≠0) to enable the agent mode."},
    }


def normalise(state: dict[str, Any]) -> dict[str, Any]:
    """Both agents render on one page: expose the observed page under ``page`` with the same keys."""
    if "screen" in state:  # WebAgent
        screen = state.pop("screen")
        view = screen.get("view") or {}
        state["page"] = {"url": screen.get("url"), "title": screen.get("title"), "text": screen.get("text"),
                         "screenshot": screen.get("screenshot"), "actions": screen.get("actions") or [],
                         "fingerprint": screen.get("fingerprint"), "w": view.get("w"), "h": view.get("h")}
    return state


def response_state() -> dict[str, Any]:
    if AGENT:
        state = normalise(dict(AGENT.snapshot()))
    else:
        state = {"page": None, "status": "idle", "history": [], "decision": None, "decisions": [], "text_calls": [], "elapsed_ms": 0}
    return {
        **state,
        "mode": MODE,
        "modes": {k: {**v, **a} for k, v in MODES.items() for a in [availability()[k]]},
        "jev_model": os.environ.get("TYPESAFE_MODEL", config.JEV_MODEL),
        "max_steps": STOCK["max_steps"] if MODE == "ultrafast" else VOICE["max_steps"],
        "max_seconds": MAX_SECONDS,
        "start_url": DEFAULT_URL,
        "display": os.environ.get("AGENT_DISPLAY", "main") or "main",
        "costs": run_costs(state.get("decisions") or [], state.get("text_calls") or []),
        "job": {k: v for k, v in JOB.items() if k != "stop"},
    }


def close_agent() -> None:
    global AGENT
    if AGENT:
        try:
            AGENT.close()
        except Exception:  # noqa: BLE001
            pass
        AGENT = None


def _step(name: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """One agent command. WebAgent recovers from a stale page itself; the stock agent reports it (its demo does the same)."""
    assert AGENT is not None
    if isinstance(AGENT, WebAgent):
        try:
            AGENT.command(name, body)
        except StalePage as stale:
            print(f"stale: {stale}", flush=True)
            AGENT.stale(stale)
            return {**response_state(), "notice": str(stale)}
    else:
        AGENT.command(name, body)
    return None


def _job() -> None:
    """Background run: the page polls /api/state instead of holding one long request open."""
    JOB.update(running=True, error=None, started_at=time.time(), stop=False)
    try:
        agent = AGENT
        if agent is None:
            raise ValueError("Start a task first")
        budget = (STOCK["max_steps"] if MODE == "ultrafast" else VOICE["max_steps"]) * 2
        for _ in range(budget):
            if JOB["stop"] or agent.state["status"] in {"done", "blocked"}:
                break
            if time.time() - JOB["started_at"] > MAX_SECONDS:
                agent.state["status"] = "blocked"
                agent.state["last_error"] = f"Stopped at the {MAX_SECONDS:.0f} s wall-clock budget"
                break
            with LOCK:
                _step("tick", {})
    except Exception as error:  # noqa: BLE001
        JOB["error"] = str(error)
        print(f"job error: {error}", flush=True)
    finally:
        JOB["running"] = False


def command(name: str, body: dict[str, Any]) -> dict[str, Any]:
    global AGENT, MODE
    if name == "reset":
        mode = (body.get("mode") or MODE).strip()
        goal = (body.get("goal") or "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        if JOB["running"]:
            raise ValueError("A run is in progress; pause it first")
        if not availability()[mode]["ready"]:
            raise ValueError(availability()[mode]["warning"] or "This mode is not configured")
        close_agent()
        configure(mode)
        MODE = mode
        if mode == "ultrafast":
            scenario = body.get("scenario") or "flights"
            if scenario == "flights":
                url = FLIGHTS_URL
            elif scenario in FIXTURE_URL:
                url = FIXTURE_URL[scenario]
            elif scenario == "custom":
                url = (body.get("url") or DEFAULT_URL).strip()
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
            else:
                raise ValueError("Unknown demo scenario")
            record = Path.cwd() / "artifacts" / "frames" if body.get("record") else None
            AGENT = uf_agent.Agent(url, goal, screenshots=True, record_dir=record)
            AGENT.state["scenario"] = scenario
        else:
            url = (body.get("url") or DEFAULT_URL).strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            display = (body.get("display") or os.environ.get("AGENT_DISPLAY") or "main").strip()
            display_bounds(display)  # validates
            AGENT = WebAgent(goal, url=url, display=None if display == "all" else display, screenshots=True)
    elif name == "continue":
        if not isinstance(AGENT, WebAgent):
            raise ValueError("Ultrafast mode has no follow-up goals; start a new run")
        goal = (body.get("goal") or "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        if JOB["running"]:
            raise ValueError("A run is in progress; pause it first")
        AGENT.continue_with(goal)
    elif name == "run":
        if AGENT is None:
            raise ValueError("Start a task first")
        if JOB["running"]:
            raise ValueError("A run is already in progress")
        if AGENT.state["status"] in {"done", "blocked"}:
            raise ValueError("This run has stopped. Start a fresh task.")
        threading.Thread(target=_job, daemon=True).start()
    elif name == "stop":
        JOB["stop"] = True
    elif name in {"predict", "act", "tick"}:
        if AGENT is None:
            raise ValueError("Start a task first")
        result = _step(name, body)
        if result is not None:
            return result
    else:
        raise ValueError("Unknown command")
    return response_state()


class Handler(BaseHTTPRequestHandler):
    def send(self, status: int, content: str | bytes, mime: str = "application/json") -> None:
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/state":
            for _ in range(3):
                try:
                    return self.send(200, json.dumps(response_state(), default=str))
                except RuntimeError:  # a step mutated the state mid-serialisation; try again
                    continue
            return self.send(503, json.dumps({"error": "state busy"}))
        files = {
            "/": (STATIC / "index.html", "text/html"),
            "/app.js": (STATIC / "app.js", "text/javascript"),
            "/style.css": (STATIC / "style.css", "text/css"),
            "/fixture.html": (UF_STATIC / "fixture.html", "text/html"),  # jev-ultrafast's demo fixtures, unchanged
        }
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        file, mime = files[path]
        content = file.read_text().replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def do_POST(self) -> None:
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local requests only"}))
        name = self.path.removeprefix("/api/")
        if name in ("run", "stop"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if 0 < length < 8192 else {}
                return self.send(200, json.dumps(command(name, body), default=str))
            except (ValueError, RuntimeError) as error:
                return self.send(400, json.dumps({"error": str(error)}))
        if JOB["running"]:
            return self.send(409, json.dumps({"error": "A run is in progress; pause it first"}))
        if not LOCK.acquire(blocking=False):
            return self.send(409, json.dumps({"error": "A step is already running"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 8192:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            self.send(200, json.dumps(command(name, body), default=str))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception as error:  # noqa: BLE001
            self.send(500, json.dumps({"error": f"Local step failed; no automatic retry. {error}"}))
        finally:
            LOCK.release()

    def log_message(self, *_args: Any) -> None:
        pass


def main() -> None:
    atexit.register(close_agent)
    configure(MODE)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev browser-use modes: {ORIGIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
