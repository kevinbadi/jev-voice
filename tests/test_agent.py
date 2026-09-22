"""Offline contracts for the desktop operation/target policy. No paid APIs, no real screen."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_voice import agent as loop
from jev_voice import policy
from jev_voice.desktop import StaleScreen, fingerprint


def screen():
    state = {
        "app": "Notes",
        "pid": 1,
        "title": "Notes",
        "url": "Notes · Notes",
        "text": "Search",
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textfield", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Focus Search", "role": "textfield", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
            {"id": "key:enter", "kind": "key", "label": "press enter", "key": "enter"},
            {"id": "app:Google Chrome", "kind": "open_app", "label": "Google Chrome", "app": "Google Chrome"},
        ],
        "marker": [1, "Notes", []],
        "page_key": [1, "Notes", []],
        "guards": {},
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1", operation="TYPE_TEXT", selected_text=None):
    return {
        "choice": action,
        "operation": operation,
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
        "selected_text": selected_text,
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        policy.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = policy.action_space(screen()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert targets["PRESS_KEY"]["enter"]["id"] == "key:enter"
    assert targets["OPEN_APP"]["Google Chrome"]["id"] == "app:Google Chrome"
    assert "WAIT" in controls


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
                "open_app_target": {"choice": "invented"},
                "press_key_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(policy, "post_json", post)
    d = policy.choose(screen(), "Search for milk", [], text_model=True)
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target", "open_app_target", "press_key_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(policy, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        policy.choose(screen(), "Search for milk", [], text_model=True)


def test_app_and_key_heads_resolve_to_offered_actions(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "OPEN_APP"),
                "open_app_target": choice(["Google Chrome"], "Google Chrome"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(policy, "post_json", post)
    d = policy.choose(screen(), "Open chrome", [], text_model=True)
    assert d["choice"] == "app:Google Chrome" and d["target"] == "Google Chrome"


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = screen()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Pinned", "node": 30,
        "role": "checkbox", "checked": "true", "selected": "false",
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] == "false"
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(policy, "post_json", post)
    d = policy.choose(p, "Press go", [], text_model=True)
    assert d["choice"] == "e3"


def test_without_text_model_jev_selects_a_code_cut_candidate(monkeypatch):
    def post(_url, _key, body):
        candidates = body["questions"]["text_value"]["criteria"]
        assert "buy milk" in candidates.values()
        assert body["state"]["candidates"] == candidates
        key = next(k for k, v in candidates.items() if v == "buy milk")
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "text_value": choice(candidates, key),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.setattr(policy, "post_json", post)
    d = policy.choose(screen(), "type buy milk in notes", [])
    assert d["selected_text"] == "buy milk"


def test_with_text_model_no_candidate_head_is_sent(monkeypatch):
    def post(_url, _key, body):
        assert "text_value" not in body["questions"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "WAIT"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(policy, "post_json", post)
    d = policy.choose(screen(), "type buy milk", [], text_model=True)
    assert d["choice"] == "wait" and d["selected_text"] is None


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(policy, "post_json", post)
    context = policy.field_context('Fly from "Zurich" to London', screen()["actions"][0], screen(), [])
    assert policy.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        policy.field_text({"goal": 'Enter "Zurich"'})


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(policy, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        policy.field_text({"goal": "Find a flight"})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.on_step = None
    a.text_model = True
    a.stale_streak = 0
    a.blocked_grace = 0
    a.last_decision = None
    a.failures = ("", 0)
    a.record_dir = None
    import threading

    a.cancel = threading.Event()
    p = screen()
    a.desktop = Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p))
    a.state = {
        "screen": p,
        "decision": decision(),
        "goal": "Search for milk",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
        "elapsed_ms": 0,
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.desktop.fresh.return_value = False
    with pytest.raises(StaleScreen):
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    runner.desktop.act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("milk", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.desktop.act.side_effect = [StaleScreen("Changed before input"), None]
    with pytest.raises(StaleScreen):
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.desktop.act.call_count == 2  # The first call rejects before any input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("milk", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.desktop.act.side_effect = [StaleScreen("Changed before input"), None]
    with pytest.raises(StaleScreen):
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    runner.state["screen"]["text"] = "Different screen context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert helper.call_count == 2


def test_jev_selected_text_skips_the_text_model(runner, monkeypatch):
    helper = Mock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["decision"] = decision(selected_text="milk")
    runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert runner.desktop.act.call_args.kwargs["text"] == "milk"
    assert runner.state["history"][-1]["text_helper"] == "jev:select"


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait", "WAIT")
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_three_unchanged_actions_block(runner):
    for _ in range(3):
        runner.state["decision"] = decision("e3", "CLICK")
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert runner.state["status"] == "blocked"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3", "CLICK")
    runner.desktop.observe.side_effect = StaleScreen("changed")
    with pytest.raises(StaleScreen):
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.desktop.act.assert_called_once()


def test_unsettled_screen_still_allows_benign_scroll_after_streak(runner):
    runner.stale_streak = loop.STALE_STREAK_LIMIT
    runner.state["screen"]["actions"].append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560, "at": None})
    runner.state["decision"] = decision("scroll_down", "SCROLL_DOWN")
    runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert runner.desktop.act.call_args.kwargs["force"] is True
    runner.state["decision"] = decision("e3", "CLICK")
    runner.stale_streak = loop.STALE_STREAK_LIMIT
    runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert runner.desktop.act.call_args.kwargs["force"] is False  # element actions are never forced


def test_executor_rejects_a_stale_screen_before_input(monkeypatch):
    from jev_voice import desktop

    d = desktop.Desktop.__new__(desktop.Desktop)
    d.fresh = Mock(return_value=False)
    d._resolve = Mock()
    with pytest.raises(StaleScreen):
        d.act(screen()["actions"][0], screen(), "milk")
    d._resolve.assert_not_called()


def test_force_is_limited_to_scroll_and_wait():
    from jev_voice import desktop

    d = desktop.Desktop.__new__(desktop.Desktop)
    d.fresh = Mock(return_value=True)
    with pytest.raises(ValueError, match="Only scroll and wait"):
        d.act(screen()["actions"][2], screen(), force=True)


def test_fingerprint_tracks_values_and_identity_not_geometry():
    p = screen()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    other["actions"][0]["rect"] = {"x": 1, "y": 2, "w": 3, "h": 4}
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.desktop.fresh.side_effect = StaleScreen("Window closing")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.desktop.act.assert_not_called()


def test_hesitant_blocked_waits_before_giving_up(runner):
    for _ in range(loop.BLOCKED_GRACE):
        runner.state["decision"] = {**decision("BLOCKED", "BLOCKED"), "confidence": 0.2, "probabilities": {"BLOCKED": 0.2}}
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
        assert runner.state["status"] == "ready" and runner.state["history"][-1]["kind"] == "wait"
    runner.state["decision"] = {**decision("BLOCKED", "BLOCKED"), "confidence": 0.2, "probabilities": {"BLOCKED": 0.2}}
    runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert runner.state["status"] == "blocked"


def test_confident_blocked_stops_immediately(runner):
    runner.state["decision"] = {**decision("BLOCKED", "BLOCKED"), "confidence": 0.9, "probabilities": {"BLOCKED": 0.9}}
    runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    assert runner.state["status"] == "blocked" and not runner.state["history"]


def test_run_costs_charge_jev_input_only_and_text_when_priced(monkeypatch):
    from jev_voice import costs

    monkeypatch.setattr(costs, "JEV_PRICE_IN", 0.042)
    monkeypatch.setattr(costs, "JEV_PRICE_OUT", 0.0)
    monkeypatch.setattr(costs, "TEXT_PRICE_IN", None)
    monkeypatch.setattr(costs, "TEXT_PRICE_OUT", None)
    decisions = [{"usage": {"input_tokens": 1_000_000, "output_tokens": 500_000}}, {"usage": {"input_tokens": 500_000, "output_tokens": 0}}]
    c = costs.run_costs(decisions, [{"usage": {"prompt_tokens": 10, "completion_tokens": 5}}])
    assert c["jev"]["usd"] == pytest.approx(0.063)
    assert c["text"]["usd"] is None and c["text"]["priced"] is False
    assert c["total_usd"] == pytest.approx(0.063)
    monkeypatch.setattr(costs, "TEXT_PRICE_IN", "1")
    monkeypatch.setattr(costs, "TEXT_PRICE_OUT", "2")
    c = costs.run_costs(decisions, [{"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}}])
    assert c["text"]["usd"] == pytest.approx(3.0) and c["total_usd"] == pytest.approx(3.063)
    assert costs.usd(0.00007) == "$0.00007" and costs.usd(None) == "—"


def test_unchanged_screen_reuses_the_decision_without_a_new_request(runner, monkeypatch):
    calls = []

    def fake_choose(screen, goal, history, text_model=None):
        calls.append(1)
        return decision("e3", "CLICK")

    monkeypatch.setattr(loop, "choose", fake_choose)
    runner.state["decision"] = None
    runner.state["status"] = "ready"
    runner.command("predict")
    runner.desktop.act.side_effect = StaleScreen("covered")
    with pytest.raises(StaleScreen):
        runner.command("act", {"fingerprint": runner.state["screen"]["fingerprint"]})
    runner.stale(StaleScreen("covered"))
    runner.command("predict")  # same fingerprint, nothing executed → cached
    assert len(calls) == 1 and runner.state["decision"]["choice"] == "e3"
    runner.state["screen"] = {**runner.state["screen"], "text": "changed"}
    runner.state["screen"]["fingerprint"] = fingerprint(runner.state["screen"])
    runner.command("predict")
    assert len(calls) == 2


def test_repeated_rejection_of_one_choice_stops_the_run(runner, monkeypatch):
    monkeypatch.setattr(loop, "choose", lambda *a, **k: decision("e3", "CLICK"))
    runner.state["decision"] = None
    runner.state["status"] = "ready"
    runner.command("predict")
    for i in range(loop.SAME_ACTION_FAILURES):
        if i == loop.SAME_ACTION_FAILURES - 1:
            with pytest.raises(ValueError, match="kept failing"):
                runner.stale(StaleScreen("covered"))
        else:
            runner.stale(StaleScreen("covered"))
    assert runner.state["status"] == "blocked"


def test_recommendation_refuses_listings_that_do_not_match_the_goal():
    from jev_voice import recommend as rc

    goal = "On Facebook Marketplace vehicles, set Make to Mercedes-Benz, Model to CLA, minimum year 2020 and maximum price $15,000."
    assert rc.must_match(goal) == {"make": "Mercedes-Benz", "model": "CLA"}
    tesla = {"title": "2020 Tesla model 3", "text": "2020 Tesla model 3 CA$12,995", "year": 2020, "price": 12995, "km": 50000, "href": "x"}
    boat = {"title": "2020 Glen l thunderbolt", "text": "boat", "year": 2020, "price": 13000, "km": None, "href": "y"}
    cla = {"title": "2020 Mercedes-Benz cla-class", "text": "CLA 250", "year": 2020, "price": 14000, "km": 40000, "href": "z"}
    assert not rc.matches_goal(tesla, rc.must_match(goal)) and not rc.matches_goal(boat, rc.must_match(goal))
    assert rc.matches_goal(cla, rc.must_match(goal))
    with pytest.raises(ValueError, match="Nothing recommended, nobody messaged"):
        rc.pick(goal, [tesla, boat])
    assert rc.filter_constraints(goal, [tesla, boat, cla]) == [cla]
    assert rc.must_match("Find a used 2020 Mercedes-Benz CLA under $15,000 near Toronto") == {"make": "Mercedes-Benz", "model": "CLA"}


def test_goal_max_year_and_price_are_enforced():
    from jev_voice import recommend as rc

    goal = "type 'Mercedes-Benz CLA' into Search Marketplace, set the Year maximum to 2020 and the Price maximum to 15000"
    older = {"title": "2018 Mercedes-Benz cla", "text": "", "year": 2018, "price": 14000, "km": None, "href": "a"}
    newer = {"title": "2022 Mercedes-Benz cla 250", "text": "", "year": 2022, "price": 14500, "km": None, "href": "b"}
    pricey = {"title": "2019 Mercedes-Benz cla", "text": "", "year": 2019, "price": 16000, "km": None, "href": "c"}
    assert rc.filter_constraints(goal, [older, newer, pricey]) == [older]


def test_chess_position_replays_figurine_moves_and_detects_turn():
    from jev_voice import chess_play as c

    snap = {"sans": ["e4", "e5", "Nf3", "Nc6"], "moves": "1. e4 e5 2. f3 c6", "pieces": [], "flipped": False, "rect": {"x": 0, "y": 0, "w": 800, "h": 800}}
    board = c.position(snap)
    assert board.fen().startswith("r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w")
    assert board.turn is True and c.our_color(snap) is True
    assert c.moves_of({"sans": [], "moves": "1. e4 e5 2. Nf3"}) == ["e4", "e5", "Nf3"]
    x, y = c.square_center({"rect": {"x": 229, "y": 66, "w": 744, "h": 744}, "flipped": False}, c.chess.E2)
    assert (round(x), round(y)) == (648, 670)
    xf, yf = c.square_center({"rect": {"x": 229, "y": 66, "w": 744, "h": 744}, "flipped": True}, c.chess.E2)
    assert (round(xf), round(yf)) == (554, 205)
