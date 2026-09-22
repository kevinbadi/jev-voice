"""Jev chooses an operation and its target; a small OpenAI-compatible model writes field values.

Port of jev-ultrafast/model.py to the desktop. One TypeSafe request per decision cycle carries:

* ``operation``      Choice over the operations that are actually possible on this screen.
* ``click_target``   Choice over the observed clickable elements (speculative).
* ``type_text_target`` Choice over the observed editable fields (speculative).
* ``open_app_target`` Choice over installed applications (speculative).
* ``press_key_target`` Choice over a small closed key set (speculative).
* ``text_value``     Only without a text model: Choice over spans cut from the goal by code,
                     so the typed value is *selected*, never generated.

The executor consumes only the head that matches the chosen operation.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

import httpx

from . import config
from .questions import APP_TARGET, KEY_TARGET, NEXT_ACTION, TARGET, TEXT_SELECT, TEXT_VALUE

CLIENT = httpx.Client(http2=False, timeout=25)

OPERATIONS = {"click": "CLICK", "fill": "TYPE_TEXT", "open_app": "OPEN_APP", "key": "PRESS_KEY"}
LABELS = {
    "CLICK": "Click a visible button, link, checkbox, radio, tab, menu item, row, or field on the current screen.",
    "TYPE_TEXT": "Enter text into a visible editable field. The value is supplied separately from the goal.",
    "OPEN_APP": "Open or switch to a different installed application; the screen then shows that app.",
    "PRESS_KEY": "Press one key: enter, escape, tab, arrows, backspace, or command-enter.",
}


def post_json(url: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer: Any, ids: Any) -> dict[str, Any]:
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions: list[dict[str, Any]]) -> tuple[list[dict], dict[str, dict[str, dict]], dict[str, dict]]:
    """One index per observed element; each operation has its own valid target choices.

    Screen elements (click / fill) share one index per AX node. Apps and keys are targets too,
    keyed by their own names, so the model can only ever pick something the executor offered.
    """
    elements: list[dict[str, Any]] = []
    indices: dict[int, str] = {}
    targets: dict[str, dict[str, dict]] = {}
    controls: dict[str, dict] = {}
    for action in actions:
        kind = action["kind"]
        if kind not in OPERATIONS:
            controls[action["id"].upper()] = action
            continue
        operation = OPERATIONS[kind]
        group = targets.setdefault(operation, {})
        if kind == "open_app":
            group[action["app"]] = action
            continue
        if kind == "key":
            group[action["key"]] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "subrole", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].removeprefix("Focus "), operations=[])
            elements.append(element)
        index = indices[node]
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        group[index] = action
    return elements, targets, controls


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NUMBER = re.compile(r"[$€£]?\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:k|K|CAD|USD|EUR))?")
_LABELLED = re.compile(
    r"\b(?:postal\s*code|postcode|zip(?:\s*code)?|email|e-mail|username|user\s*name|password|phone|search\s+(?:term|query)|query|keyword|address|name)"
    r"\s*(?:is|:|=|of)?\s*(?P<v>[A-Za-z0-9@._+-]+(?:\s+[A-Za-z0-9@._+-]+){0,3}?)(?=[,.;]|\s+(?:and|then|in|on|for|to)\b|$)",
    re.I,
)
_LEAD_VERB = re.compile(
    r"^(?:please\s+)?(?:find|search(?:\s+for)?|look(?:\s+up|\s+for)?|google|show(?:\s+me)?|get(?:\s+me)?|pull\s+up|open|go\s+to)\s+(?:me\s+)?",
    re.I,
)


def text_candidates(goal: str) -> dict[str, str]:
    """Spans cut from the goal by code. Jev selects one; nothing is generated.

    Besides the single-command cuts (quoted text, 'type X', 'search for X'), every sentence of a
    multi-sentence goal is a candidate, with and without its leading verb, so a paragraph goal
    still yields a usable query such as 'a used 2020 CLA for sale in Ontario under $15,000'."""
    from .brain import text_candidates as cut

    spans: list[str] = list(cut(goal).values())

    def add(span: str) -> None:
        span = span.strip(" .,;:")
        if len(span) >= 2 and span not in spans:
            spans.append(span)

    # Labelled values: "postal code M5V 3L9", "zip 94107", "email x@y.z", "username kev", "search term ..."
    for m in _LABELLED.finditer(goal):
        add(m.group("v"))
    # Amounts, years and numbers, as written and as plain digits: "$15,000 CAD" → "$15,000", "15,000", "15000".
    for m in _NUMBER.finditer(goal):
        raw = m.group(0)
        add(raw)
        digits = re.sub(r"[^\d.]", "", raw).rstrip(".")
        if digits:
            add(digits)
            if "," in raw:
                add(raw.replace("$", "").strip())
    for sentence in _SENTENCE.split(goal.strip()):
        sentence = sentence.strip(" .")
        if len(sentence) < 3:
            continue
        add(sentence)
        add(_LEAD_VERB.sub("", sentence))
        # Comma clauses of a long sentence are often the individual field values.
        if "," in sentence:
            for clause in sentence.split(","):
                add(_LEAD_VERB.sub("", clause))
    return {f"c{i}": span for i, span in enumerate(spans[:18])}


def choose(state: dict[str, Any], goal: str, history: list[dict[str, Any]], *, text_model: bool | None = None) -> dict[str, Any]:
    elements, targets, controls = action_space(state["actions"])
    operations = {key: LABELS[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions: dict[str, Any] = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        if operation == "OPEN_APP":
            questions["open_app_target"] = {
                "type": "choice",
                "criteria": {name: None for name in candidates},
                "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, APP_TARGET]},
            }
        elif operation == "PRESS_KEY":
            questions["press_key_target"] = {
                "type": "choice",
                "criteria": {key: a["label"] for key, a in candidates.items()},
                "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, KEY_TARGET]},
            }
        else:
            questions[operation.lower() + "_target"] = {
                "type": "choice",
                "criteria": {
                    index: {
                        "element": f"[{index}] {a['label']}",
                        "current_value": a.get("value", ""),
                        **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                    }
                    for index, a in candidates.items()
                },
                "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
            }
    use_llm = bool(os.environ.get("TEXT_MODEL_API_KEY")) if text_model is None else text_model
    candidates: dict[str, str] = {}
    if "TYPE_TEXT" in targets and not use_llm:
        candidates = text_candidates(goal)
        questions["text_value"] = {"type": "choice", "criteria": dict(candidates), "instructions": TEXT_SELECT}
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", config.JEV_MODEL),
        "state": {
            "screen": {"app": state["app"], "window": state["title"], "text": state["text"]},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "screen_changed")} for h in history[-10:]
            ],
            **({"candidates": candidates} if candidates else {}),
        },
        "questions": questions,
    }
    started = time.perf_counter()
    key = os.environ.get("TYPESAFE_API_KEY") or config.TYPESAFE_API_KEY
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY is not set (put it in .env)")
    result = post_json(config.TYPESAFE_URL, key, body)
    answers = result["answers"]
    operation_answer = validate_choice(answers.get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities: dict[str, float] = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate only the head the operation selected.
        head = operation.lower() + "_target"
        target_answer = validate_choice(answers.get(head, {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    selected_text = None
    if operation == "TYPE_TEXT" and candidates:
        text_answer = validate_choice(answers.get("text_value", {}), candidates)
        selected_text = candidates[text_answer["choice"]]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "selected_text": selected_text,
        "raw_answers": answers,
        "model": result.get("model"),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal: str, action: dict[str, Any], screen: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "screen": {"app": screen["app"], "window": screen["title"], "text": screen["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Generate a field value with the small text model. Requires TEXT_MODEL_API_KEY."""
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY (or a Jev-selected candidate); nothing is guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "inception/mercury-2.5")
    reasoning: dict[str, Any] = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {"role": "user", "content": json.dumps(context)},
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
