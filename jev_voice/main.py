"""Jev Voice: speak to your Mac.

    uv run jev-voice                     # hands-free: say "Alfred, ..." (or tap Caps Lock, then speak)
    uv run jev-voice --hold              # Caps Lock: hold to talk (tap = toggle), no wake word
    uv run jev-voice --always-on         # open mic, every utterance is a command (no wake word)
    uv run jev-voice --ptt               # press Enter to talk, Enter to stop
    uv run jev-voice --text "open chrome and go to youtube"      # no mic
    uv run jev-voice --text "..." --dry-run                        # plan only
    uv run jev-voice --goal "reply to the last email from Sam saying yes"   # multi-step agent, no mic
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time

import numpy as np

from . import actions, config
from .brain import Brain, Plan, split_compound
from .overlay import NullOverlay
from .persona import flavor
from .tts import Speaker

OVERLAY = NullOverlay()  # replaced with a real Overlay in run_voice unless --no-overlay

SOUND_START = "/System/Library/Sounds/Tink.aiff"
SOUND_STOP = "/System/Library/Sounds/Pop.aiff"
SOUND_FAIL = "/System/Library/Sounds/Basso.aiff"
SOUND_DONE = "/System/Library/Sounds/Glass.aiff"
# FEEDBACK=ding (default): chime when an action completes, no speech. FEEDBACK=voice: spoken butler replies.
FEEDBACK = os.environ.get("FEEDBACK", "ding")
IDLE_LABEL = "Listening"


def ding(path: str) -> None:
    subprocess.Popen(["afplay", "-v", "0.4", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_DESKTOP = None


TASK_DRIVER = os.environ.get("TASK_DRIVER", "browser")


SELLER_MESSAGE = os.environ.get("SELLER_MESSAGE", "Hi! Is this still available?")


def run_task(goal: str, speaker: Speaker | None = None, start_url: str | None = None, recommend: bool = False,
             message: str | None = None) -> str:
    """Multi-step goal: the jev-ultrafast loop. TASK_DRIVER=browser (default) drives Chrome over
    CDP exactly as jev-ultrafast does; TASK_DRIVER=desktop uses the experimental AX-tree port.
    One Jev request per step; the executor only ever touches observed controls."""
    global _DESKTOP
    from .agent import describe_step

    if speaker and FEEDBACK == "voice":
        speaker.say(flavor("Working on it."))
    else:
        ding(SOUND_START)

    def show(step: dict) -> None:
        line = describe_step(step["action"], step["operation"], step["text"])
        print(f"  ▶ {line}")
        OVERLAY.set("thinking", f"{step['step']}. {line}")

    if TASK_DRIVER == "browser":
        from .web import WebAgent

        agent_cm = WebAgent(goal, url=start_url, display=os.environ.get("AGENT_DISPLAY") or None, on_step=show)
    else:
        from .agent import Agent
        from .desktop import Desktop

        if _DESKTOP is None:
            _DESKTOP = Desktop()
        agent_cm = Agent(goal, desktop=_DESKTOP, on_step=show)
    with agent_cm as agent:
        try:
            for st in agent.run():
                pass
        except Exception as e:  # noqa: BLE001
            print(f"  ! {e}")
            return "I couldn't finish that."
        st = agent.state
        from .costs import run_costs, usd

        reply = "Done." if st["status"] == "done" else "I couldn't finish that."
        if recommend and TASK_DRIVER == "browser" and st["status"] == "done":
            try:
                rec = agent.recommend()
                OVERLAY.set("done", f"★ {rec['summary']}", revert_after=8.0)
                reply = rec["spoken"]
                if message:
                    OVERLAY.set("thinking", f"✉ {message}")
                    sent = agent.message_seller(message)
                    reply += " I've messaged the seller." if sent["status"] == "done" else " I couldn't send the message."
            except Exception as e:  # noqa: BLE001
                print(f"  ! recommendation: {e}")
                reply = "I found results but couldn't pick one."
        c = run_costs(st["decisions"], st["text_calls"])
        print(f"  {st['elapsed_ms']} ms  {len(st['history'])} actions  {st['status']}  ({c['jev']['requests']} Jev calls, {usd(c['total_usd'])})")
        return reply


def execute(plan: Plan, dry: bool = False, speaker: Speaker | None = None) -> str:
    """Run the plan. Returns the short spoken confirmation."""
    a = plan.args
    act = plan.action
    if act == "none":
        return ""
    if act == "stop":
        return "__stop__"
    if plan.confidence < config.ACTION_MIN_CONFIDENCE:
        return "Not sure what you meant."
    if dry:
        return f"[dry] {plan}"
    if act == "task":
        site = plan.answers.get("site", {}).get("choice") if plan.answers else None
        return run_task(plan.utterance, speaker, start_url=actions.SITES.get(site) if site else None,
                        recommend=bool(a.get("recommend")) or bool(a.get("message_seller")),
                        message=SELLER_MESSAGE if a.get("message_seller") else None)
    if a.get("in_app"):
        if not actions.focus_app(a["in_app"]):
            return f"I couldn't bring up {a['in_app']}."
    if act == "new_item":
        actions.press({"tab": "new_tab", "window": "new"}.get(a.get("kind", "item"), "new"))
        title = a.get("title")
        if title:
            time.sleep(0.35)
            actions.type_text(title)
            return f"New {title}."
        return "Done."
    if act == "open_app":
        if a["app"] == "none":
            return "I don't see that app."
        actions.open_app(a["app"])
        return f"Opening {a['app']}."
    if act == "open_website":
        actions.open_url(a["url"])
        return f"Opening {a['site'].replace('_', ' ')}."
    if act == "web_search":
        actions.web_search(a["engine"], a["query"])
        return f"Searching {a['engine'].replace('_', ' ')} for {a['query']}."
    if act == "type_text":
        actions.type_text(a["text"])
        if a["submit"]:
            actions.press("enter")
        return "Done."
    if act == "shortcut":
        actions.press(a["shortcut"])
        return a["shortcut"].replace("_", " ").capitalize() + "."
    if act == "scroll":
        actions.scroll(a["direction"], a["amount"])
        return ""
    if act == "volume":
        return actions.volume(a["op"])
    if act == "media":
        actions.media(a["op"])
        return ""
    if act == "screenshot":
        actions.screenshot()
        return "Screenshot saved to the desktop."
    if act == "open_folder":
        actions.open_folder(a["folder"])
        return f"Opening {a['folder']}."
    if act == "system":
        return actions.system(a["op"])
    return ""


def handle(brain: Brain, speaker: Speaker, utterance: str, dry: bool, depth: int = 0, plan: Plan | None = None) -> bool:
    """Returns False when the user asked to stop."""
    OVERLAY.set("thinking", f"{utterance}")
    plan = plan or brain.evaluate(utterance)
    print(f"  → {plan}")
    if plan.args.get("compound") and depth == 0:
        parts = split_compound(utterance)
        if len(parts) > 1:
            print(f"  compound: {parts}")
            for p in parts:
                if not handle(brain, speaker, p, dry, depth=1):
                    return False
                time.sleep(0.35)  # let the previous app/page come up
            return True
    try:
        reply = execute(plan, dry, speaker)
    except Exception as e:  # noqa: BLE001
        reply = "That failed."
        print(f"  ! {e}")
    if reply == "__stop__":
        if FEEDBACK == "voice":
            speaker.say(flavor("Bye."))
        else:
            ding(SOUND_STOP)
        return False
    failed = reply in ("That failed.", "Not sure what you meant.", "I don't see that app.") or reply.startswith("I couldn't")
    if plan.action == "task" and plan.confidence >= config.ACTION_MIN_CONFIDENCE and not dry:
        OVERLAY.set("error" if failed else "done", f"Task: {utterance}  ·  {reply}", revert_after=3.0)
    if reply:
        line = flavor(reply) if not dry else reply
        print(f"  ◀ {line}")
        if FEEDBACK == "voice":
            speaker.say(line)
        elif failed:
            ding(SOUND_FAIL)
        else:
            ding(SOUND_DONE)
    elif plan.action != "none" and not dry:
        ding(SOUND_DONE)
    if plan.action == "none":
        OVERLAY.set("idle", f"Not a command: {utterance}", revert_after=2.5)
    elif failed:
        OVERLAY.set("error", f"{describe(plan)}  ·  {reply}", revert_after=3.0)
    else:
        OVERLAY.set("done", describe(plan), revert_after=2.5)
    return True


def describe(plan: Plan) -> str:
    """Short human label for the overlay, e.g. 'Open Google Chrome'."""
    a = plan.args
    act = plan.action
    return {
        "open_app": lambda: f"Open {a.get('app')}",
        "open_website": lambda: f"Open {a.get('site', '').replace('_', ' ')}",
        "web_search": lambda: f"Search {a.get('engine', '').replace('_', ' ')}: {a.get('query')}",
        "type_text": lambda: f"Type “{a.get('text')}”" + (" ⏎" if a.get('submit') else ""),
        "new_item": lambda: f"New {a.get('kind', 'item')}" + (f" “{a['title']}”" if a.get('title') else ""),
        "shortcut": lambda: a.get("shortcut", "").replace("_", " ").capitalize(),
        "scroll": lambda: f"Scroll {a.get('direction')} ({a.get('amount')})",
        "volume": lambda: f"Volume {a.get('op')}",
        "media": lambda: f"Media {a.get('op', '').replace('_', '/')}",
        "screenshot": lambda: "Screenshot",
        "open_folder": lambda: f"Open {a.get('folder')}",
        "system": lambda: f"{a.get('op', '').replace('_', ' ').capitalize()}",
        "task": lambda: f"Task: {plan.utterance}",
        "stop": lambda: "Bye",
    }.get(act, lambda: act)() + (f"  ·  in {a['in_app']}" if a.get("in_app") else "")


def run_text(args: argparse.Namespace) -> None:
    brain = Brain()
    speaker = Speaker(enabled=not args.quiet)
    handle(brain, speaker, args.text, args.dry_run)


def run_goal(args: argparse.Namespace) -> None:
    speaker = Speaker(enabled=not args.quiet)
    reply = run_task(args.goal, speaker)
    print(f"  ◀ {reply}")
    if FEEDBACK == "voice":
        speaker.say(flavor(reply), wait=True)


class Session:
    """Shared runtime for all mic modes."""

    def __init__(self, args: argparse.Namespace) -> None:
        from .audio import Listener
        from .stt import WhisperServer

        if not actions.accessibility_ok():
            print("⚠ Accessibility permission missing: System Settings → Privacy & Security → Accessibility → add your terminal.")
        self.args = args
        self.stt = WhisperServer()
        self.stt.start()
        self.brain = Brain()
        self.speaker = Speaker(enabled=not args.quiet)
        self.listener = Listener(device=args.device)
        self.listener.start()

    def close(self) -> None:
        self.listener.stop()
        self.stt.stop()

    def process(self, pcm: np.ndarray) -> bool:
        """Transcribe + plan + execute. Returns False on 'stop'."""
        if len(pcm) < config.SAMPLE_RATE * 0.25:
            return True
        t0 = time.perf_counter()
        text = self.stt.transcribe(pcm)
        stt_ms = int((time.perf_counter() - t0) * 1000)
        if not text:
            print("  (heard nothing)")
            return True
        print(f"🗣  {text}   ({len(pcm)/config.SAMPLE_RATE:.1f}s audio, stt {stt_ms}ms)")
        self.listener.pause(0.3)
        ok = handle(self.brain, self.speaker, text, self.args.dry_run)
        if self.speaker.speaking():
            self.listener.pause(0.9)
        self.listener.drain()
        return ok


# ------------------------------------------------------------------ wake word

WAKE_WORDS = [w.strip().lower() for w in os.environ.get("WAKE_WORDS", "alfred,jarvis,alfie,alford,elfred").split(",") if w.strip()]
FOLLOWUP_SECONDS = float(os.environ.get("FOLLOWUP_SECONDS", "8"))
_WAKE_RE = re.compile(r"^\W*(?:hey|hi|ok|okay|yo)?\W*(?P<w>" + "|".join(map(re.escape, WAKE_WORDS)) + r")\b\W*", re.I)
_WAKE_ANY = re.compile(r"\W*\b(?:" + "|".join(map(re.escape, WAKE_WORDS)) + r")\b\W*", re.I)


UNNAMED_COMMANDS = os.environ.get("UNNAMED_COMMANDS", "1") not in ("0", "false", "no")
UNNAMED_MIN_ADDRESSED = float(os.environ.get("UNNAMED_MIN_ADDRESSED", "0.7"))
UNNAMED_MIN_CONFIDENCE = float(os.environ.get("UNNAMED_MIN_CONFIDENCE", "0.7"))


def _fuzzy_wake(word: str) -> bool:
    import difflib

    w = word.lower().strip("',.!?;:")
    if len(w) < 4:
        return False
    for target in WAKE_WORDS:
        if w == target or difflib.SequenceMatcher(None, w, target).ratio() >= 0.75:
            return True
    return False


def strip_wake(text: str) -> tuple[bool, str]:
    """Return (addressed_to_us, command_text). The name may lead or appear anywhere,
    and whisper's misspellings of it (Alfrid, Halford, Alford's) count too."""
    m = _WAKE_RE.match(text)
    if m:
        return True, text[m.end():].strip()
    if _WAKE_ANY.search(text):
        return True, _WAKE_ANY.sub(" ", text, count=1).strip(" ,.")
    words = text.split()
    for i, w in enumerate(words):
        if _fuzzy_wake(w):
            rest = " ".join(words[:i] + words[i + 1:]).strip(" ,.")
            rest = re.sub(r"^(?:hey|hi|ok|okay|yo)\W+", "", rest, flags=re.I)
            return True, rest
    return False, text


# ------------------------------------------------------------------ modes

def run_smart(s: Session) -> None:
    """Hands-free. Mic is always open; only utterances that name the assistant (or follow
    a command within FOLLOWUP_SECONDS, or follow a Caps Lock tap) are sent to Jev."""
    from .hotkey import CapsLockListener, capslock_remapped, remap_capslock

    armed = {"until": 0.0}

    def arm(seconds: float) -> None:
        armed["until"] = time.monotonic() + seconds

    def on_press() -> None:
        s.speaker.interrupt()
        ding(SOUND_START)
        arm(10.0)

    if not capslock_remapped():
        remap_capslock()
    tap = CapsLockListener(on_press, lambda: None)
    caps = tap.start()
    names = ", ".join(w.capitalize() for w in WAKE_WORDS[:2])
    print(f"🎙  Hands-free. Say \"{names.split(', ')[0]}, open chrome\"."
          + (" Or tap CAPS LOCK then speak." if caps else " (Caps Lock tap unavailable: no Accessibility/Input Monitoring.)")
          + f"  (Jev {s.brain.model}, whisper base.en, voice {s.speaker.engine}:{s.speaker.voice})")
    if FEEDBACK == "voice":
        s.speaker.say(flavor("Ready."))
    else:
        ding(SOUND_DONE)
    s.listener.pause(0.8)
    s.listener.on_speech_start = lambda: OVERLAY.set("listening", "Listening…")
    while True:
        pcm = s.listener.next_utterance()
        OVERLAY.set("heard", "Transcribing…")
        t0 = time.perf_counter()
        text = s.stt.transcribe(pcm)
        stt_ms = int((time.perf_counter() - t0) * 1000)
        if not text:
            OVERLAY.set("idle", IDLE_LABEL, revert_after=0.1)
            continue
        OVERLAY.set("heard", text)
        addressed, cmd = strip_wake(text)
        if not addressed and time.monotonic() < armed["until"]:
            addressed, cmd = True, text
        if not cmd and addressed:        # just the name: acknowledge and wait for the command
            ding(SOUND_START)
            arm(FOLLOWUP_SECONDS)
            continue
        gate = None
        if not addressed:
            if not UNNAMED_COMMANDS:
                print(f"   ·  {text}   (ignored: no name, stt {stt_ms}ms)")
                OVERLAY.set("idle", f"Ignored: {text}", revert_after=2.5)
                continue
            # No name: let Jev judge whether this is a command for the computer at all.
            gate = s.brain.evaluate(text)
            ok_cmd = (gate.args.get("addressed", 0) >= UNNAMED_MIN_ADDRESSED
                      and gate.confidence >= UNNAMED_MIN_CONFIDENCE)
            if ok_cmd and gate.action == "none":
                print(f"   ·  {text}   (Jev: none, addressed={gate.args.get('addressed')} conf={gate.confidence:.2f}, stt {stt_ms}ms)")
                continue
            if not ok_cmd:
                print(f"   ·  {text}   (ignored: addressed={gate.args.get('addressed')} {gate.action} conf={gate.confidence:.2f}, stt {stt_ms}ms)")
                OVERLAY.set("idle", f"Ignored: {text}", revert_after=2.5)
                continue
            cmd = text
        tag = f", addressed={gate.args.get('addressed')}" if gate else ""
        print(f"🗣  {cmd}   ({len(pcm)/config.SAMPLE_RATE:.1f}s audio, stt {stt_ms}ms{tag})")
        s.listener.pause(0.3)
        ok = handle(s.brain, s.speaker, cmd, s.args.dry_run, plan=gate)
        if s.speaker.speaking():
            s.listener.pause(0.9)
        s.listener.drain()
        arm(FOLLOWUP_SECONDS)
        if not ok:
            break


def run_capslock(s: Session) -> None:
    """Hold Caps Lock to talk, release to run. A short tap (<250 ms) toggles hands-free
    recording on; the next tap stops it."""
    from .hotkey import CapsLockListener, capslock_remapped, remap_capslock

    if not capslock_remapped():
        remap_capslock()
        if not capslock_remapped():
            print("⚠ Could not remap Caps Lock with hidutil. Run scripts/setup.sh.")
    recording = threading.Event()
    done: queue.Queue[np.ndarray] = queue.Queue()
    state = {"pressed_at": 0.0, "latched": False}

    def collector() -> None:
        while True:
            recording.wait()
            s.listener.drain()
            parts: list[np.ndarray] = []
            while recording.is_set():
                try:
                    parts.append(s.listener.q.get(timeout=0.05))
                except queue.Empty:
                    pass
            done.put(np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32))

    threading.Thread(target=collector, daemon=True).start()

    def on_press() -> None:
        state["pressed_at"] = time.monotonic()
        if state["latched"]:          # tap while latched: stop
            state["latched"] = False
            recording.clear()
            ding(SOUND_STOP)
            return
        s.speaker.interrupt()
        ding(SOUND_START)
        recording.set()

    def on_release() -> None:
        held = time.monotonic() - state["pressed_at"]
        if not recording.is_set():
            return
        if held < 0.25:               # short tap: latch hands-free
            state["latched"] = True
            return
        recording.clear()
        ding(SOUND_STOP)

    tap = CapsLockListener(on_press, on_release)
    if not tap.start():
        from .hotkey import open_permission_panes, request_permissions

        perms = request_permissions()
        print(f"✗ Global key tap refused. Permissions: {perms}")
        print("  Tick this app in System Settings → Privacy & Security → Accessibility AND Input Monitoring.")
        open_permission_panes()
        s.speaker.say("I need Accessibility and Input Monitoring permission. Please tick them in System Settings.")
        while True:
            time.sleep(2.0)
            tap = CapsLockListener(on_press, on_release)
            if tap.start():
                break
            if perms.get("accessibility") and perms.get("input_monitoring"):
                print("  Permissions granted but the tap still fails. Restart Jev Voice.")
                s.speaker.say("Permissions granted. Please restart me.")
                sys.exit(3)
            perms = request_permissions()
    print(f"⌨️  Hold CAPS LOCK and speak. Tap it to toggle hands-free. (Jev {s.brain.model}, whisper base.en, voice {s.speaker.engine}:{s.speaker.voice})")
    s.speaker.say(flavor("Ready."))
    while True:
        pcm = done.get()
        if not s.process(pcm):
            break


def run_always_on(s: Session) -> None:
    print(f"🎙  Listening (Jev {s.brain.model}, whisper base.en). Say 'stop listening' to quit.")
    s.speaker.say(flavor("Ready."))
    s.listener.pause(0.8)
    while True:
        pcm = s.listener.next_utterance()
        if not s.process(pcm):
            break


def run_ptt(s: Session) -> None:
    print("⏎  Push-to-talk: Enter to start, Enter to stop.")
    while True:
        input("  [Enter] to talk… ")
        s.listener.drain()
        print("  recording, [Enter] to stop")
        parts: list[np.ndarray] = []
        stop = threading.Event()

        def _collect() -> None:
            while not stop.is_set():
                try:
                    parts.append(s.listener.q.get(timeout=0.1))
                except queue.Empty:
                    pass

        th = threading.Thread(target=_collect, daemon=True)
        th.start()
        input()
        stop.set(); th.join()
        if parts and not s.process(np.concatenate(parts)):
            break


def run_voice(args: argparse.Namespace) -> None:
    global OVERLAY
    if not args.no_overlay and os.environ.get("OVERLAY", "1") not in ("0", "false", "no"):
        from .overlay import Overlay
        OVERLAY = Overlay()

    def worker() -> None:
        s = Session(args)
        try:
            if args.ptt:
                run_ptt(s)
            elif args.always_on:
                run_always_on(s)
            elif args.hold:
                run_capslock(s)
            else:
                run_smart(s)
        except KeyboardInterrupt:
            pass
        finally:
            s.close()

    OVERLAY.run(worker)


def main() -> None:
    p = argparse.ArgumentParser(prog="jev-voice", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--text", help="run one command from text instead of the microphone")
    p.add_argument("--goal", help="run one multi-step goal with the desktop agent instead of the microphone")
    p.add_argument("--dry-run", action="store_true", help="plan with Jev but do not touch the computer")
    p.add_argument("--hold", action="store_true", help="Caps Lock hold-to-talk only, no wake word")
    p.add_argument("--always-on", action="store_true", help="open mic, every utterance is a command (no wake word)")
    p.add_argument("--ptt", action="store_true", help="push-to-talk in the terminal (Enter to start/stop)")
    p.add_argument("--device", help="input device index or name substring (see `uv run python -m sounddevice`)")
    p.add_argument("--quiet", action="store_true", help="no spoken replies")
    p.add_argument("--no-overlay", action="store_true", help="no floating transcription pill")
    args = p.parse_args()
    if args.device and args.device.isdigit():
        args.device = int(args.device)
    if args.text:
        run_text(args)
    elif args.goal:
        run_goal(args)
    else:
        run_voice(args)


if __name__ == "__main__":
    sys.exit(main())
