# Dynamic operation + target, on the macOS desktop

This is the [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) design applied to
native applications. The input is a natural-language goal. Every observation builds an
indexed table of the visible controls of the frontmost application's focused window (plus any
open menu or dialog). One AX node receives one index, even when it supports both clicking and
typing.

One TypeSafe request asks which operation to perform and which target would be appropriate
for each available operation. The executor consumes only the target head corresponding to the
selected operation. Operation and target questions receive the same next-step rules; a target
question cannot read the operation answer, so its premise names the operation it assumes.

| Browser version | Desktop version |
| --- | --- |
| `snapshot.js` evaluated over CDP | `desktop.py` walks the AX tree with batched attribute reads |
| DOM node identity in a `WeakMap` | `AXUIElement` refs are `CFEqual`/`CFHash` stable; `Identity` maps them to ints |
| `CLICK`, `TYPE_TEXT`, `SELECT` | `CLICK`, `TYPE_TEXT`, `OPEN_APP`, `PRESS_KEY` (no native `SELECT`: popups open as menus and are clicked) |
| Text helper LLM required | Text helper LLM, or a speculative `text_value` head where Jev selects a code-cut span of the goal |
| Element hit-test via `elementFromPoint` | `AXUIElementCopyElementAtPosition` on the system-wide element; ancestor in either direction passes |
| `selectAll` + `insertText` replaces a field | Single-line fields are replaced (⌘A); `AXTextArea` documents append at the end (⌘↓) |

## Runtime

`Desktop.observe()` performs one bounded preorder walk (3 500 nodes, 0.9 s, depth 60) of the
focused window. Each node is read with `AXUIElementCopyMultipleAttributeValues` (one IPC per
node): role, subrole, title, description, value, placeholder, enabled, focused, selected,
position, size, children. Windows, scroll areas, sheets, popovers and web areas clip their
descendants; a subtree entirely outside the clip is not visited. Tables and outlines are read
through `AXVisibleRows`. Chromium and Electron apps expose web content only when asked, so
`AXManualAccessibility` / `AXEnhancedUserInterface` is set once per process.

Names resolve like the browser version's accessible-name approximation: title, description,
placeholder, then the concatenated static text of the first few descendant levels, then help
and role description. Comboboxes that carry a text selection range are treated as editable
fields. Rows are targets only when they can be selected and contain no finer control.

Up to 250 element candidates are retained; truncated candidates cannot be selected. Controls
are appended: scroll up/down when the largest visible scroll area's scroll bar says there is
room (both when unknown), wait, seven keys, and every installed app except the frontmost one.

Freshness compares semantic state, never pixel or mutation counts:

* **Click / type** use a scoped guard. Immediately before input the executor re-reads the
  target's role, title, description, value, enabled and selected state and compares them to the
  observation, together with the window key (pid, window title, the value of every observed
  text field). Then it re-resolves geometry and hit-tests the centre. Unrelated visible content
  may change; this is a practical heuristic, not proof that arbitrary changes are irrelevant.
* **Scroll, wait, DONE, BLOCKED** compare the whole control table (`marker`: pid, window title,
  every action minus geometry). Visible text is excluded from the marker and included in the
  fingerprint, so a ticking clock does not invalidate a scroll but does count as "the screen
  changed" for the no-progress rule. After four consecutive stale decisions a scroll or wait
  executes without the full comparison; element actions are never forced.

Mutations are not retried. Execution is logged before the next observation. Typing posts real
keyboard events (`CGEventKeyboardSetUnicodeString`), clicks post real mouse events at the
re-resolved centre, scrolling warps the cursor to the scroll area first because macOS routes
wheel events to the control under the pointer. Opening an app sets `AXFrontmost`; the usual
LaunchServices activation is deferred by macOS while the user is actively typing or clicking.

The next observation waits 80 ms after a click or key, 120 ms after typing, 150 ms after a
scroll, and 400 ms after switching apps, so menus and sheets finish animating before they
are read.

## Boundaries

Forty actions and eighty decision requests bound a run. The service is a local process; the
only network calls are the TypeSafe request and, optionally, the text helper. Credentials stay
in `.env`. Screenshots are optional (`--record`) and never reach the model.

The tree reader covers common AppKit, Catalyst, SwiftUI and Chromium roles, not the full
accessibility semantics. Menu bar menus are not enumerated (keyboard shortcuts cover most
of them through the single-command path). Canvas-drawn apps (Google Docs, games), apps that
refuse accessibility, sheets attached to non-focused windows, and drag interactions remain out
of scope. Only the focused window is observed; a goal that spans two windows of one app
needs the app to switch focus itself. A valid action can still be wrong. A `DONE` choice is
the model's claim; verify outcomes independently.
