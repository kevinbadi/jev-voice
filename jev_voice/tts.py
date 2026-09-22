"""Text to speech.

Engines (TTS_ENGINE in .env):
  say         macOS built-in, zero latency. Auto-picks a Premium/Enhanced voice if one
              is downloaded (System Settings → Accessibility → Spoken Content → Manage Voices).
  elevenlabs  ElevenLabs Flash v2.5 (~75 ms synthesis + network). Needs ELEVENLABS_API_KEY.
              Falls back to `say` on any error.

Every synthesized phrase is cached on disk keyed by (engine, voice, text), so
repeats ("Done.", "Opening Chrome.") play instantly with no network at all.
Speech is always asynchronous: the action has already run by the time we speak.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

import httpx

from . import config

CACHE_DIR = Path(os.environ.get("JEV_TTS_CACHE", Path.home() / ".cache" / "jev-voice" / "tts"))
ENGINE = os.environ.get("TTS_ENGINE", "elevenlabs" if os.environ.get("ELEVENLABS_API_KEY") else "say")
KOKORO_DIR = Path(os.environ.get("KOKORO_DIR", Path(__file__).resolve().parent.parent / "models" / "kokoro"))
KOKORO_VOICE = os.environ.get("KOKORO_VOICE", "af_heart")  # open-source Kokoro-82M voices: af_heart, af_bella, am_michael, bm_george…
_kokoro = None


def kokoro_available() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        return False
    return (KOKORO_DIR / "kokoro-v1.0.onnx").exists() and (KOKORO_DIR / "voices-v1.0.bin").exists()


def kokoro_synthesize(text: str, path: Path, speed: float = 1.05) -> Path | None:
    """Kokoro-82M (Apache-2.0) on CPU: near real-time, ElevenLabs-class voices, nothing leaves the machine."""
    global _kokoro
    try:
        import soundfile as sf
        from kokoro_onnx import Kokoro

        if _kokoro is None:
            _kokoro = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
        samples, rate = _kokoro.create(text, voice=KOKORO_VOICE, speed=speed, lang="en-us")
        tmp = path.with_suffix(".part.wav")
        sf.write(str(tmp), samples, rate)
        tmp.replace(path)
        return path
    except Exception as e:  # noqa: BLE001
        print(f"  (tts: kokoro failed, using say: {e})")
        return None
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
PERSONA = os.environ.get("PERSONA", "alfred")  # alfred | cowboy | plain
# ElevenLabs premade voices: George = warm British narrator (Alfred), Bill = old American male (cowboy)
_ELEVEN_DEFAULTS = {"alfred": "JBFqnCBsd6RMkjVDRZzb", "cowboy": "pqHfZKP75CvOlQylNhV4", "plain": "JBFqnCBsd6RMkjVDRZzb"}
ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", _ELEVEN_DEFAULTS.get(PERSONA, _ELEVEN_DEFAULTS["alfred"]))
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
ELEVEN_SPEED = os.environ.get("ELEVENLABS_SPEED", "1.1")

# Phrases worth having on disk before the first command.
PREWARM = ["At your service, sir.", "Ready when you are, sir.", "Done, sir.", "Very good, sir.", "As you wish.", "Quite so.",
           "I'm afraid I didn't quite catch that, sir.", "Beg your pardon, sir?", "Ready.", "Done.", "Bye.", "Not sure what you meant.", "That failed.",
           "I don't see that app.", "Louder.", "Quieter.", "Muted.", "Unmuted.",
           "Screenshot saved to the desktop.", "Opening Google Chrome.", "Opening Cursor.",
           "Opening youtube.", "Opening Finder.", "Close tab or window.", "Copy.", "Paste.",
           "Undo.", "Select all.", "Enter.", "Reload.", "New tab.", "Locking."]


@lru_cache(maxsize=1)
def best_say_voice() -> str:
    """Prefer a downloaded Premium/Enhanced English voice; else the configured default."""
    if os.environ.get("TTS_VOICE"):
        return os.environ["TTS_VOICE"]
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    except Exception:
        return config.TTS_VOICE
    # Rank: British premium > British enhanced > any premium > any enhanced > Daniel (en_GB) > default
    ranked: list[tuple[int, str]] = []
    for line in out.splitlines():
        if " en_" not in line:
            continue
        name = line.split("  ")[0].strip()
        gb = " en_GB" in line or " en_IE" in line
        if "(Premium)" in name:
            ranked.append((0 if gb else 2, name))
        elif "(Enhanced)" in name:
            ranked.append((1 if gb else 3, name))
        elif name == "Daniel":
            ranked.append((4, name))
    if ranked:
        ranked.sort()
        return ranked[0][1]
    return config.TTS_VOICE


class Speaker:
    def __init__(self, enabled: bool = True, engine: str | None = None) -> None:
        self.enabled = enabled
        self.engine = engine or ENGINE
        if self.engine == "elevenlabs" and not ELEVEN_KEY:
            self.engine = "say"
        if self.engine == "kokoro" and not kokoro_available():
            print("  (tts: kokoro not installed or model files missing; using say)")
            self.engine = "say"
        self.voice = ELEVEN_VOICE if self.engine == "elevenlabs" else KOKORO_VOICE if self.engine == "kokoro" else best_say_voice()
        self.rate = config.TTS_RATE
        self.proc: subprocess.Popen | None = None
        self.http = httpx.Client(timeout=8.0, headers={"xi-api-key": ELEVEN_KEY})
        self._lock = threading.Lock()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if self.engine == "elevenlabs":
            threading.Thread(target=self._prewarm, daemon=True).start()

    # ------------------------------------------------------------ public

    def say(self, text: str, wait: bool = False) -> float:
        """Speak asynchronously. Returns an estimated duration in seconds."""
        if not text or not self.enabled:
            return 0.0
        self.interrupt()
        est = 0.25 + len(text.split()) * 60.0 / self.rate
        if self.engine == "kokoro":
            path = self._key(text).with_suffix(".wav")
            if not (path.exists() and path.stat().st_size > 0):
                path = kokoro_synthesize(text, path)
            if path is not None:
                self.proc = subprocess.Popen(["afplay", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if wait:
                    self.proc.wait()
                return est
        if self.engine == "elevenlabs":
            path = self._cached(text)
            if path is None:
                path = self._synthesize(text)
            if path is not None:
                self.proc = subprocess.Popen(["afplay", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if wait:
                    self.proc.wait()
                return est
            # fall through to say
        self.proc = subprocess.Popen(["say", "-v", best_say_voice(), "-r", str(self.rate), text],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            self.proc.wait()
        return est

    def interrupt(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def speaking(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # ------------------------------------------------------------ cache / synth

    def _key(self, text: str) -> Path:
        h = hashlib.sha1(f"{self.engine}|{self.voice}|{ELEVEN_MODEL}|{ELEVEN_SPEED}|{text}".encode()).hexdigest()[:20]
        return CACHE_DIR / f"{h}.mp3"

    def _cached(self, text: str) -> Path | None:
        p = self._key(text)
        return p if p.exists() and p.stat().st_size > 0 else None

    def _synthesize(self, text: str) -> Path | None:
        p = self._key(text)
        try:
            r = self.http.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice}",
                params={"output_format": "mp3_22050_32"},
                json={"text": text, "model_id": ELEVEN_MODEL,
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "speed": float(ELEVEN_SPEED)}},
            )
            r.raise_for_status()
            tmp = p.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(p)
            return p
        except Exception as e:  # noqa: BLE001
            print(f"  (tts: elevenlabs failed, using say: {e})")
            return None

    def _prewarm(self) -> None:
        for phrase in PREWARM:
            if self._cached(phrase) is None:
                with self._lock:
                    self._synthesize(phrase)
