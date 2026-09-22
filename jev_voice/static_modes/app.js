const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false,
  mode = "ultrafast";

const COPY = {
  ultrafast: {
    eyebrow: "A BROWSER THAT CHOOSES",
    headline: "Every page is a set of possibilities.",
    lede: "jev-ultrafast as it shipped: Jev picks the next action, a small language model handles the words. Nothing else.",
    note: "Operation and target are separate choices in one request.<br />Text is generated only for TYPE_TEXT.",
  },
  jev: {
    eyebrow: "JEV WITH GUARDS",
    headline: "Same choices, safer executor.",
    lede: "Jev alone, but the window is confined to one monitor and code withdraws covered, repeated or futile controls before Jev sees them.",
    note: "Claude is off in this mode.<br />TYPE_TEXT is selected from spans of the goal when no text model is set.",
  },
  agent: {
    eyebrow: "JEV → HAIKU → FABLE",
    headline: "Jev runs; Claude plans, breaks ties and verifies.",
    lede: "A planner turns the goal into a checklist. Weak or BLOCKED choices go to Haiku, then Fable. DONE is verified against the page.",
    note: "Escalations are listed on the right.<br />Jev still makes every routine decision.",
  },
};
const GOALS = {
  flights: "Find one-way flights from Zurich to London on September 20, 2026, for one adult in economy. Stop when matching flight options are visible. Do not select or book a flight.",
  travel: "Find a Design stay in Lisbon with Free cancellation and open Casa Flora.",
  research: "Open the article about using finite choices to control browser agents.",
  custom: "Search for lofi hip hop and stop when results are visible.",
  web: "Find a used 2020 Mercedes-Benz CLA under $15,000 CAD near Toronto. Postal code M5V 3L9. Stop when the filtered listings are visible.",
};
const LABELS = {
  idle: "Ready to explore",
  ready: "Page observed · ready for a decision",
  predicted: "Choice ready · inspect or execute",
  done: "Reports complete · inspect the page",
  blocked: "Stopped · no supported next action",
};

const escape = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
const usd = (v) => (v == null ? "—" : v === 0 ? "$0" : v < 0.001 ? `$${v.toFixed(5)}` : v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`);
const shortModel = (m) => String(m || "").replace("claude-", "").replace("-4-5", "").replace("-5-1", "");

async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  render();
  if (data.notice) $("status").textContent = `↻ ${data.notice}`;
  return data;
}
async function refresh() {
  state = await fetch("/api/state").then((r) => r.json());
  render();
  return state;
}
function live() {
  return state?.page && !["done", "blocked", "idle"].includes(state.status);
}
function controls() {
  const running = busy || automatic;
  const isWeb = state?.page && state?.mode !== "ultrafast";
  $("start").disabled = running;
  $("goal").disabled = running;
  for (const id of ["scenario", "url", "display", "record"]) $(id).disabled = running;
  document.querySelectorAll(".mode").forEach((b) => (b.disabled = running));
  $("choose").disabled = running || !live();
  $("execute").disabled = running || !state?.decision || !live();
  $("auto").disabled = running || !live();
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.history?.length;
  $("continue-row").hidden = !isWeb;
  $("continue").disabled = running || !isWeb;
}
function selectMode(next, { keepGoal = false } = {}) {
  mode = next;
  const copy = COPY[mode];
  $("eyebrow").textContent = copy.eyebrow;
  $("headline").textContent = copy.headline;
  $("lede").textContent = copy.lede;
  $("aside-note").innerHTML = copy.note;
  const stock = mode === "ultrafast";
  $("scenario-label").hidden = !stock;
  $("record-label").hidden = !stock;
  $("display-label").hidden = stock;
  $("url-label").hidden = stock && $("scenario").value !== "custom";
  if (!keepGoal) $("goal").value = stock ? GOALS[$("scenario").value] : GOALS.web;
  if (!stock && !$("url").value) $("url").value = state?.start_url || "https://www.google.com";
  if (stock && $("scenario").value === "custom" && !$("url").value) $("url").value = "https://www.youtube.com";
  const info = state?.modes?.[mode];
  $("mode-warning").hidden = !info?.warning;
  $("mode-warning").textContent = info?.warning || "";
  $("pipeline").textContent = info?.pipeline || "";
  $("helper").textContent = info ? `${mode === "agent" ? "Escalation" : "Text"} · ${info.text}` : "";
  $("model-tag").innerHTML = `${escape(state?.jev_model || "jev-latest")} <span>${escape(info?.text || "")}</span>`;
  renderModes();
}
function renderModes() {
  const modes = state?.modes || {};
  $("modes").innerHTML = Object.entries(modes)
    .map(
      ([key, m]) =>
        `<button type="button" role="tab" class="mode ${key === mode ? "active" : ""} ${m.ready ? "" : "unavailable"}" data-mode="${key}" aria-selected="${key === mode}">
          <em>${escape(m.ready ? m.tagline : "not configured")}</em><b>${escape(m.title)}</b><p>${escape(m.summary)}</p><small>${escape(m.pipeline)}</small>
        </button>`,
    )
    .join("");
  document.querySelectorAll(".mode").forEach((b) => (b.disabled = busy || automatic));
}
async function perform(fn, label) {
  if (busy) return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      await refresh();
    } catch {
      /* keep the original failure if the server went away */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "Paused · needs attention";
  } finally {
    busy = false;
    controls();
  }
}
async function runJob() {
  await call("run", {});
  await pollJob();
}
async function pollJob() {
  automatic = true;
  controls();
  while (automatic) {
    await new Promise((r) => setTimeout(r, 600));
    try {
      await refresh();
    } catch {
      continue;
    }
    const d = state.decision || state.decisions?.at(-1);
    if (d?.escalated) $("status").textContent = `${shortModel(d.escalated.model)} chose ${d.operation} · ${d.escalated.latency_ms} ms`;
    else if (d) $("status").textContent = `Jev chose ${d.operation} · ${d.latency_ms} ms`;
    if (state.job?.error) throw Error(state.job.error);
    if (!state.job?.running) break;
  }
  automatic = false;
  await refresh();
}
function render() {
  if (!state) return;
  if (state.page && state.mode !== mode) selectMode(state.mode, { keepGoal: true });
  else renderModes();
  const page = state.page,
    d = state.decision || (["done", "blocked"].includes(state.status) ? state.decisions?.at(-1) : null);
  $("status").textContent = state.last_error && state.status === "blocked" ? `Stopped · ${state.last_error}` : LABELS[state.status] || state.status;
  const plan = state.plan || [];
  $("plan-block").hidden = !(state.page && state.mode !== "ultrafast" && plan.length > 1);
  $("plan-heading").textContent = state.mode === "agent" ? "Plan (Claude)" : "Plan";
  const remaining = state.remaining_steps || [];
  $("plan").innerHTML = plan
    .map((step, i) => {
      const done = remaining.length ? !remaining.includes(step) : i < state.plan_index;
      const current = remaining.length ? step === remaining[0] : i === state.plan_index;
      return `<div class="plan-step ${current ? "current" : ""}"><span>${done ? "✓" : i + 1}</span>${escape(step)}</div>`;
    })
    .join("");
  const costs = state.costs || {};
  $("cost-total").textContent = usd(costs.total_usd ?? costs.total);
  $("cost-jev").textContent = costs.jev?.requests ?? (state.decisions || []).length;
  const escalations = state.escalations || [];
  const textCalls = (state.text_calls || []).filter((t) => !String(t.model || "").startsWith("jev:"));
  $("cost-llm").textContent = state.mode === "agent" ? escalations.length : textCalls.length;
  $("cost-llm-label").textContent = state.mode === "agent" ? "Claude calls" : "text calls";
  $("elapsed").textContent = `${((state.elapsed_ms || 0) / 1000).toFixed(2)} s`;
  $("esc").hidden = !escalations.length;
  $("esc-rows").innerHTML = escalations
    .slice(-12)
    .reverse()
    .map((e) => `<div class="esc-row"><span class="slot">${escape(e.slot)}</span><span class="value">${escape(e.value)}</span><span class="meta">${escape(shortModel(e.model))} · ${e.latency_ms} ms</span></div>`)
    .join("");
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  if (page.screenshot) $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url-bar").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${(state.elements || []).length} elements`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").innerHTML = d
    ? `${escape(chosen?.label || d.choice)}${d.escalated ? `<span class="badge llm">${escape(shortModel(d.escalated.model))}</span>` : ""}`
    : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : d?.confidence != null ? percent(d.confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? (d.escalated ? "Ranked by Jev · overridden" : "Ranked by Jev") : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a, b) => b[1] - a[1]);
  $("operation-choices").innerHTML = op
    .map(([name, p]) => `<span class="operation-choice ${name === d.operation ? "best" : ""}">${escape(name)} <b>${percent(p)}</b></span>`)
    .join("");
  const lastText = (state.text_calls || []).at(-1);
  $("text-line").hidden = !(d?.operation === "TYPE_TEXT" && lastText);
  if (lastText) $("text-line").innerHTML = `Text for <b>${escape(lastText.field)}</b>: “${escape(lastText.value)}” <small>${escape(lastText.model)}</small>`;
  const probability = (e) => d?.target_probabilities?.[e.index] ?? Math.max(-1, ...(e.options || []).map((o) => d?.target_probabilities?.[o.index] ?? -1));
  const selectedIndex = d?.target?.split(":")[0];
  const elements = [...(state.elements || [])];
  if (d) elements.sort((a, b) => probability(b) - probability(a));
  $("choices").innerHTML = elements
    .map((e) => {
      const p = probability(e);
      return `<div class="choice ${selectedIndex === e.index ? "best" : ""}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape((e.operations || []).join(" / "))}${e.value ? " · " + escape(e.value) : ""}${e.checked !== undefined ? " · checked " + escape(e.checked) : ""}</small>${p >= 0 ? `<div class="bar" style="--probability:${p * 100}%"></div>` : ""}</div><span class="probability">${p >= 0 ? percent(p) : "—"}</span></div>`;
    })
    .join("");
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()]
    .map((a, i) => {
      const index = String(i + 1);
      return `<div class="target ${index === selectedIndex ? "selected" : ""}" data-action="${index}" style="left:${(100 * a.rect.x) / page.w}%;top:${(100 * a.rect.y) / page.h}%;width:${(100 * a.rect.w) / page.w}%;height:${(100 * a.rect.h) / page.h}%"><span>${index}</span></div>`;
    })
    .join("");
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b><small>${escape(h.text_helper)}</small>` : ""}${h.escalated ? `<span class="badge llm">${escape(shortModel(h.escalated.model))}</span>` : ""}</div><span class="time">${h.latency_ms} ms · ${percent(h.probability)}</span><span class="effect">${h.page_changed ? "Page changed" : "No change observed"}</span></div>`,
        )
        .join("")
    : '<p class="muted">Each executed action leaves an observed result.</p>';
  $("step-count").textContent = `${state.history.length} actions · ${(state.elapsed_ms / 1000).toFixed(2)} s`;
  $("model-state").textContent = JSON.stringify(
    d?.request || { goal: state.goal, url: page.url, text: page.text, actions: page.actions.map(({ rect, node, ...rest }) => rest) },
    null,
    2,
  );
  controls();
}

$("modes").addEventListener("click", (event) => {
  const next = event.target.closest("[data-mode]")?.dataset.mode;
  if (next && next !== mode) selectMode(next);
});
$("scenario").addEventListener("change", () => {
  $("goal").value = GOALS[$("scenario").value];
  $("url-label").hidden = $("scenario").value !== "custom";
  if ($("scenario").value === "custom" && !$("url").value) $("url").value = "https://www.youtube.com";
});
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  perform(
    () =>
      call("reset", {
        mode,
        goal: $("goal").value,
        scenario: $("scenario").value,
        url: $("url").value,
        display: $("display").value,
        record: $("record").checked,
      }),
    mode === "ultrafast" ? "Opening a fresh browser…" : "Opening a confined Chrome window…",
  );
});
$("continue").addEventListener("click", () =>
  perform(() => call("continue", { goal: $("continue-goal").value }), "Re-observing the page for the follow-up goal…"),
);
$("choose").addEventListener("click", () => perform(() => call("predict"), "Jev is comparing the actions…"));
$("execute").addEventListener("click", () =>
  perform(() => call("act", { fingerprint: state.page.fingerprint }), "Executing the choice…"),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    if ($("pace").checked) {
      automatic = true;
      controls();
      for (let i = 0; i < state.max_steps * 2 && automatic; i++) {
        $("status").textContent = "Running slowly…";
        await call("predict");
        await new Promise((resolve) => setTimeout(resolve, 450));
        if (!automatic || !state.decision) continue;
        await call("act", { fingerprint: state.page.fingerprint });
        if (["done", "blocked"].includes(state.status)) break;
      }
      automatic = false;
    } else {
      await runJob();
    }
  }, "Running the browser…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  call("stop").catch(() => {});
  $("status").textContent = "Pausing after the current request…";
  controls();
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document.querySelectorAll(".target").forEach((t) =>
    t.classList.toggle("selected", t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(":")[0]),
  );
});
$("choices").addEventListener("pointerleave", () =>
  document.querySelectorAll(".target").forEach((t) => t.classList.toggle("selected", t.dataset.action === state?.decision?.target?.split(":")[0])),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob([JSON.stringify({ ...rest, page: { ...page, screenshot: undefined } }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `jev-${state.mode}-trace.json`;
  a.click();
  URL.revokeObjectURL(url);
});

refresh()
  .then(() => selectMode(state.page ? state.mode : "ultrafast", { keepGoal: false }))
  .then(() => {
    if (state.job?.running) pollJob().catch(() => {});
  })
  .catch(() => {
    $("status").textContent = "Cannot reach the local server";
  });
