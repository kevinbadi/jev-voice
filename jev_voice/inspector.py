"""Loopback-only inspector for the Jev desktop agent (port of jev-ultrafast/demo.py).

    uv run jev-inspect            # then open http://127.0.0.1:8766

Shows the monitor the agent is confined to with numbered element badges, the operation and
target probabilities of every Jev request, the text-helper calls, and the decision trail.
Screenshots are taken for the page only; the policy never consumes them.
"""
from __future__ import annotations

import atexit
import json
import os
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config, escalate
from .agent import Agent
from .costs import run_costs
from .desktop import Desktop, StaleScreen, display_bounds
from .questions import MAX_STEPS
from .web import DEFAULT_URL, StalePage, WebAgent

ROOT = Path(__file__).parent
PORT = int(os.environ.get("JEV_INSPECT_PORT", "8766"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
AGENT: Agent | WebAgent | None = None
JOB: dict[str, Any] = {"running": False, "mode": None, "phase": None, "error": None, "started_at": None, "stop": False}
MAX_SECONDS = float(os.environ.get("TASK_MAX_SECONDS", "900"))
WORKFLOWS = Path("runs/workflows.json")


def load_workflows() -> dict[str, Any]:
    try:
        return json.loads(WORKFLOWS.read_text())
    except (OSError, ValueError):
        return {}


def save_workflows(data: dict[str, Any]) -> None:
    WORKFLOWS.parent.mkdir(exist_ok=True)
    WORKFLOWS.write_text(json.dumps(data, indent=2))
DESKTOP: Desktop | None = None
DISPLAY: str = os.environ.get("AGENT_DISPLAY", "main") or "main"
DRIVER: str = os.environ.get("TASK_DRIVER", "browser") or "browser"


def _job(mode: str, message: str | None) -> None:
    """Background job: the page polls /api/state instead of holding one long request open."""
    import time

    JOB.update(running=True, mode=mode, phase="filters", error=None, started_at=time.time(), stop=False)
    try:
        agent = AGENT
        if agent is None:
            raise ValueError("Start a task first")
        if mode in ("auto", "full"):
            for _ in range(MAX_STEPS * 2):
                if JOB["stop"] or agent.state["status"] in {"done", "blocked"}:
                    break
                if time.time() - JOB["started_at"] > MAX_SECONDS:
                    agent.state["status"] = "blocked"
                    agent.state["last_error"] = f"Stopped at the {MAX_SECONDS:.0f} s wall-clock budget"
                    break
                with LOCK:
                    try:
                        agent.command("tick")
                    except (StaleScreen, StalePage) as stale:
                        agent.stale(stale)
        if mode in ("full", "recommend") and not JOB["stop"] and (mode == "recommend" or agent.state["status"] == "done"):
            JOB["phase"] = "pick"
            with LOCK:
                command("recommend", {})
        if mode in ("full", "message") and not JOB["stop"] and message and (mode == "message" or agent.state.get("recommendation")):
            JOB["phase"] = "message"
            with LOCK:
                command("message", {"text": message, "sellers": JOB.get("sellers", 1)})
        JOB["phase"] = "finished"
    except Exception as error:  # noqa: BLE001
        JOB["error"] = str(error)
        print(f"job error: {error}", flush=True)
    finally:
        JOB["running"] = False


def response_state() -> dict[str, Any]:
    if AGENT:
        state = AGENT.snapshot()
        screen = {k: v for k, v in state["screen"].items() if k not in ("guards", "marker", "page_key")}
        state = {**state, "screen": screen}
    else:
        state = {"screen": None, "status": "idle", "history": [], "decision": None, "decisions": [], "text_calls": [], "elapsed_ms": 0}
    return {
        **state,
        "text_model": (escalate.MODEL if escalate.enabled() else
                       os.environ.get("TEXT_MODEL", "inception/mercury-2.5") if os.environ.get("TEXT_MODEL_API_KEY") else "jev selects from the goal"),
        "jev_model": os.environ.get("TYPESAFE_MODEL", config.JEV_MODEL),
        "max_steps": MAX_STEPS,
        "display": DISPLAY,
        "driver": DRIVER,
        "start_url": DEFAULT_URL,
        "view": (DESKTOP.view() if DESKTOP else None),
        "costs": run_costs(state.get("decisions") or [], state.get("text_calls") or []),
        "job": {k: v for k, v in JOB.items() if k != "stop"},
        "workflows": load_workflows(),
        "max_seconds": MAX_SECONDS,
    }


def close_agent() -> None:
    global AGENT
    if AGENT:
        if isinstance(AGENT, WebAgent):
            AGENT.close()
        else:
            AGENT.cancel.set()
        AGENT = None


def command(name: str, body: dict[str, Any]) -> dict[str, Any]:
    global AGENT, DESKTOP, DISPLAY, DRIVER
    if name == "reset":
        goal = (body.get("goal") or "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        display = (body.get("display") or DISPLAY or "main").strip()
        display_bounds(display)  # validates
        driver = (body.get("driver") or DRIVER or "browser").strip()
        if driver not in {"browser", "desktop"}:
            raise ValueError("driver must be browser or desktop")
        close_agent()
        DISPLAY, DRIVER = display, driver
        if driver == "browser":
            url = (body.get("url") or DEFAULT_URL).strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
                JOB["phase"] = "planning"
            AGENT = WebAgent(goal, url=url, display=None if display == "all" else display, screenshots=True)
            JOB["phase"] = None
        else:
            if DESKTOP is None or DESKTOP.display != display_bounds(display):
                DESKTOP = Desktop(display=display)
            AGENT = Agent(goal, desktop=DESKTOP, screenshots=True)
    elif name == "preview":
        display = (body.get("display") or DISPLAY or "main").strip()
        display_bounds(display)
        if DESKTOP is None or display != DISPLAY:
            DESKTOP = Desktop(display=display)
            DISPLAY = display
        if AGENT is None:
            screen = DESKTOP.observe(screenshot=True)
            return {**response_state(), "screen": {k: v for k, v in screen.items() if k not in ("guards", "marker", "page_key")},
                    "status": "preview", "elements": []}
    elif name == "recommend":
        if not isinstance(AGENT, WebAgent):
            raise ValueError("Recommendations need the browser driver")
        rec = AGENT.recommend(pages=int(body.get("pages") or 0) or None)
        runs = Path.cwd() / "runs"
        runs.mkdir(exist_ok=True)
        out = runs / f"{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps({"goal": AGENT.state["original_goal"], "recommendation": rec["summary"], "reason": rec["reason"],
                                   "listing_url": rec["listing"].get("href") or AGENT.state["page"]["url"],
                                   "candidates": [{"summary": r["summary"], "p": r["p"], "url": r["href"]} for r in rec["ranked"]]}, indent=2))
        AGENT.state["recommendation"]["saved"] = str(out)
    elif name == "message":
        if not isinstance(AGENT, WebAgent):
            raise ValueError("Messaging needs the browser driver")
        text = (body.get("text") or "").strip()
        if not text or len(text) > 500:
            raise ValueError("Enter a message of 1–500 characters")
        sellers = max(1, min(10, int(body.get("sellers") or 1)))
        results = AGENT.message_sellers(text, sellers)
        AGENT.state["message_result"] = {"status": "done" if results and all(r["status"] == "done" for r in results) else "partial",
                                         "text": text, "sent": [r for r in results if r["status"] == "done"], "results": results}
    elif name == "workflow_save":
        wf_name = (body.get("name") or "").strip()
        if not wf_name or len(wf_name) > 80:
            raise ValueError("Give the workflow a name (1–80 characters)")
        data = load_workflows()
        data[wf_name] = {k: body.get(k) for k in ("goal", "url", "message", "sellers", "driver", "display")}
        save_workflows(data)
    elif name == "workflow_delete":
        data = load_workflows()
        data.pop((body.get("name") or "").strip(), None)
        save_workflows(data)
    elif name == "run":
        if AGENT is None:
            raise ValueError("Start a task first")
        if JOB["running"]:
            raise ValueError("A run is already in progress")
        mode = body.get("mode") or "auto"
        if mode not in {"auto", "full", "recommend", "message"}:
            raise ValueError("Unknown run mode")
        JOB["sellers"] = max(1, min(10, int(body.get("sellers") or 1)))
        threading.Thread(target=_job, args=(mode, (body.get("message") or "").strip() or None), daemon=True).start()
    elif name == "stop":
        JOB["stop"] = True
        if AGENT and not JOB["running"]:
            if not isinstance(AGENT, WebAgent):
                AGENT.cancel.set()
            AGENT.state["status"] = "blocked"
    else:
        if AGENT is None:
            raise ValueError("Start a task first")
        try:
            AGENT.command(name, body)
        except (StaleScreen, StalePage) as stale:
            # The screen moved between the choice and execution. Nothing ran; observe and choose again.
            print(f"stale: {stale}", flush=True)
            AGENT.stale(stale)  # desktop: raises ValueError (→ 400) once the same choice keeps failing
            return {**response_state(), "notice": str(stale)}
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
        files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        name, mime = files[path]
        content = (ROOT / "static" / name).read_text().replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def do_POST(self) -> None:
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local inspector requests only"}))
        name = self.path.removeprefix("/api/")
        if name in ("run", "stop", "preview", "workflow_save", "workflow_delete"):
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
            result = command(name, body)
            self.send(200, json.dumps(result, default=str))
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
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev Voice inspector: {ORIGIN}  (driver={DRIVER}, display={DISPLAY})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
