# Jev Voice

Read README.md and docs/design.md before editing. Two paths share one executor vocabulary:

- **Single command** (`brain.py` → `main.execute`): one utterance, one Jev fan-out, one action. Jev selects; code produces every candidate value.
- **Task** (`agent.py`): the jev-ultrafast loop. Screen → indexed AX elements → operation + speculative target heads in one request → execute against an observed node → log → observe.

Invariants for the task loop:

- The input is one natural-language goal. No app-specific plans or hardcoded field values.
- Consume only the selected operation's target head. Targets must map to observed elements, offered apps, or offered keys. The model never emits selectors, coordinates, scripts, or shell.
- TYPE_TEXT values come from the text model, or from a Jev choice over code-cut spans of the goal. The executor never guesses. Cache a stale retry's value only while its entire helper input is identical.
- Never retry a mutation. Log execution before observing its result.
- Re-read the target's state, re-resolve geometry, and hit-test immediately before input.
- Screenshots are optional; the model does not consume them.
- Tests are offline: no screen, no paid APIs. Do not commit or push unless asked.

Checks: `uv run ruff check .`, `uv run pytest`.
