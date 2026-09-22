"""Offline contracts for the browser-use modes page (jev-modes). No browser, no paid APIs."""

import os

import pytest
from jev_ultrafast import agent as uf_agent
from jev_ultrafast import model as uf_model

from jev_voice import modes


def test_stock_and_voice_patches_are_distinct():
    assert modes.STOCK["field_text"] is uf_model.field_text
    assert modes.VOICE["field_text"] is not modes.STOCK["field_text"]
    assert modes.STOCK["max_steps"] == 60


@pytest.mark.parametrize("mode", ["ultrafast", "jev", "agent"])
def test_configure_sets_module_globals(mode, monkeypatch):
    monkeypatch.setenv("ESCALATE", "1")
    monkeypatch.setattr(modes, "ESCALATE_ENV", "1")
    modes.configure(mode)
    stock = mode == "ultrafast"
    assert uf_agent.field_text is (modes.STOCK if stock else modes.VOICE)["field_text"]
    assert uf_agent.MAX_STEPS == (modes.STOCK if stock else modes.VOICE)["max_steps"]
    assert uf_model.NEXT_ACTION == modes.STOCK["next_action"]
    assert os.environ["ESCALATE"] == ("1" if mode == "agent" else "0")


def test_configure_rejects_unknown_mode():
    with pytest.raises(ValueError):
        modes.configure("desktop")


def test_normalise_web_snapshot_exposes_page():
    screen = {"url": "https://x", "title": "X", "text": "t", "screenshot": None, "actions": [{"id": "a"}],
              "fingerprint": "f", "view": {"x": 0, "y": 0, "w": 800, "h": 600}, "visited": 1}
    state = modes.normalise({"screen": screen, "status": "ready", "driver": "browser"})
    assert "screen" not in state
    assert state["page"] == {"url": "https://x", "title": "X", "text": "t", "screenshot": None, "actions": [{"id": "a"}],
                             "fingerprint": "f", "w": 800, "h": 600}


def test_normalise_leaves_stock_snapshot_alone():
    state = {"page": {"url": "u"}, "status": "ready"}
    assert modes.normalise(dict(state)) == state


def test_availability_reflects_keys(monkeypatch):
    monkeypatch.setattr(modes.config, "TYPESAFE_API_KEY", "k")
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ready = modes.availability()
    assert ready["ultrafast"]["ready"] and ready["ultrafast"]["warning"]
    assert ready["jev"]["ready"] and ready["jev"]["text"] == "Jev selects text from the goal"
    assert not ready["agent"]["ready"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setattr(modes, "ESCALATE_ENV", None)
    assert modes.availability()["agent"]["ready"]


def test_idle_state_and_reset_validation(monkeypatch):
    monkeypatch.setattr(modes, "AGENT", None)
    state = modes.response_state()
    assert state["status"] == "idle" and state["page"] is None
    assert set(state["modes"]) == {"ultrafast", "jev", "agent"}
    with pytest.raises(ValueError):
        modes.command("reset", {"mode": "ultrafast", "goal": ""})
    with pytest.raises(ValueError):
        modes.command("continue", {"goal": "again"})
    with pytest.raises(ValueError):
        modes.command("run", {})
    with pytest.raises(ValueError):
        modes.command("predict", {})
    with pytest.raises(ValueError):
        modes.command("nope", {})
