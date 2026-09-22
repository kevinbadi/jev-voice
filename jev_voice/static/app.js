const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false,
  runStartedAt = null,   // page-owned clock: starts on click, stops when the whole flow ends
  runStoppedAt = null,
  phase = "",
  phaseTimes = [],
  ticker = null;
function clockStart() {
  runStartedAt = performance.now();
  runStoppedAt = null;
  phaseTimes = [];
}
function clockStop() {
  if (runStartedAt && !runStoppedAt) runStoppedAt = performance.now();
}
function clockMark(label) {
  if (!runStartedAt) return;
  phaseTimes.push(`${label} ${((performance.now() - runStartedAt) / 1000).toFixed(1)}s`);
}
function setPhase(kind, text) {
  phase = text;
  const el = $("phase");
  el.textContent = text;
  el.className = "hero-phase " + kind;
}
const escape = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
const seconds = (ms) => (ms / 1000).toFixed(2).padStart(5, "0");
const usd = (v) => (v == null ? "—" : v === 0 ? "$0" : v < 0.001 ? `$${v.toFixed(5)}` : v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`);
const jevUsd = (usage, price) => ((usage?.input_tokens || 0) * (price ?? 0.042)) / 1e6;

async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  if (data.workflows) renderWorkflows();
  const d = data.decision || data.decisions?.at(-1);
  if (name === "tick" || name === "predict" || name === "act") {
    if (d?.escalated) setPhase("llm", `${d.escalated.model.replace("claude-", "").replace("-4-5", "").replace("-5-1", "")} chose ${d.operation} · ${d.escalated.latency_ms} ms`);
    else if (d) setPhase("jev", `Jev chose ${d.operation} · ${d.latency_ms} ms`);
  }
  render();
  if (data.notice) $("status").textContent = `↻ ${data.notice}`;
  else if (data.last_error && ["blocked"].includes(data.status)) $("status").textContent = `Stopped · ${data.last_error}`;
  return data;
}
function live() {
  return state?.screen && !["done", "blocked", "preview", "idle"].includes(state.status);
}
async function runJob(mode, label) {
  await call("run", { mode, message: $("message").value, sellers: $("sellers").value });
  automatic = true;
  controls();
  $("status").textContent = label;
  let lastPhase = null;
  while (true) {
    await new Promise((r) => setTimeout(r, 700));
    let s;
    try {
      s = await fetch("/api/state").then((r) => r.json());
    } catch {
      continue; // the server is mid-step; poll again
    }
    state = s;
    const d = s.decision || s.decisions?.at(-1);
    if (d?.escalated) setPhase("llm", `${d.escalated.model.replace("claude-", "").replace("-4-5", "").replace("-5-1", "")} chose ${d.operation} · ${d.escalated.latency_ms} ms`);
    else if (d && s.job?.phase === "filters") setPhase("jev", `Jev chose ${d.operation} · ${d.latency_ms} ms`);
    if (s.job?.phase && s.job.phase !== lastPhase) {
      if (lastPhase) clockMark(lastPhase);
      lastPhase = s.job.phase;
      if (s.job.phase === "pick") setPhase("exec", "reading listings · Jev picking");
      if (s.job.phase === "message") setPhase("exec", "messaging the seller");
    }
    render();
    const lastMove = (s.chess_moves || []).slice(-1)[0];
    $("status").textContent = s.job?.running ? { filters: "Getting to the board…", pick: "Reading the listings and asking Jev to pick one…", message: "Messaging the seller…", chess: `Playing… ${lastMove ? (typeof lastMove === "string" ? lastMove : lastMove.label) : ""}` }[s.job.phase] || label : "";
    if (s.job?.phase === "chess") setPhase("exec", `chess · ${(s.chess_moves || []).filter((m) => typeof m === "string" || m.who === "us").length} moves · Stockfish`);
    if (!s.job?.running) break;
  }
  automatic = false;
  clockStop();
  if (state.job?.error) throw Error(state.job.error);
  setPhase(state.status === "done" ? "jev" : "exec", state.status === "done" ? "complete" : "stopped");
  render();
}
// Keyed list rendering: existing entries are updated in place, only new entries enter (and animate).
function renderList(container, items, keyOf, htmlOf) {
  const existing = new Map([...container.children].map((el) => [el.dataset.key, el]));
  const keys = items.map(keyOf);
  let prefixOk = true;
  [...container.children].forEach((el, i) => { if (el.dataset.key !== keys[i]) prefixOk = false; });
  if (!prefixOk) container.innerHTML = "";
  items.forEach((item, i) => {
    const key = keys[i];
    const html = htmlOf(item);
    let el = prefixOk ? existing.get(key) : null;
    if (!el) {
      const tpl = document.createElement("template");
      tpl.innerHTML = html.trim();
      el = tpl.content.firstElementChild;
      el.dataset.key = key;
      el.classList.add("enter");
      container.appendChild(el);
    } else if (el.dataset.html !== html) {
      const tpl = document.createElement("template");
      tpl.innerHTML = html.trim();
      const fresh = tpl.content.firstElementChild;
      el.className = fresh.className;
      el.innerHTML = fresh.innerHTML;
    }
    el.dataset.html = html;
  });
  while (container.children.length > items.length) container.lastElementChild.remove();
}
function renderWorkflows() {
  const sel = $("workflow");
  const names = Object.keys(state?.workflows || {});
  const current = sel.value;
  sel.innerHTML = '<option value="">— custom —</option>' + names.map((n) => `<option value="${escape(n)}">${escape(n)}</option>`).join("");
  if (names.includes(current)) sel.value = current;
  $("wf-delete").disabled = !sel.value;
}
function applyWorkflow(name) {
  const w = state?.workflows?.[name];
  if (!w) return;
  if (w.goal) $("goal").value = w.goal;
  if (w.url) $("url").value = w.url;
  if (w.message != null) $("message").value = w.message;
  if (w.sellers) $("sellers").value = w.sellers;
  if (w.driver) $("driver").value = w.driver;
  if (w.display) $("display").value = w.display;
}
$("workflow").addEventListener("change", () => {
  applyWorkflow($("workflow").value);
  $("wf-delete").disabled = !$("workflow").value;
  try { localStorage.setItem("jev-workflow", $("workflow").value); } catch {}
});
$("wf-save").addEventListener("click", () =>
  perform(async () => {
    const name = prompt("Save this goal, start URL, message and seller count as:", $("workflow").value || "");
    if (!name) return;
    await call("workflow_save", { name, goal: $("goal").value, url: $("url").value, message: $("message").value,
      sellers: Number($("sellers").value), driver: $("driver").value, display: $("display").value });
    renderWorkflows();
    $("workflow").value = name;
    $("wf-delete").disabled = false;
  }, "Saving workflow…"),
);
$("wf-delete").addEventListener("click", () =>
  perform(async () => {
    const name = $("workflow").value;
    if (!name || !confirm(`Delete workflow “${name}”?`)) return;
    await call("workflow_delete", { name });
    renderWorkflows();
  }, "Deleting workflow…"),
);
function controls() {
  $("start").disabled = busy;
  $("go").disabled = busy;
  $("preview").disabled = busy;
  $("display").disabled = busy;
  $("goal").disabled = busy;
  $("choose").disabled = busy || !live();
  $("execute").disabled = busy || !state?.decision || !live();
  $("auto").disabled = busy || !live();
  const running = automatic || !!state?.job?.running;
  $("auto").hidden = running;
  $("stop").hidden = !running;
  $("download").disabled = !state?.history?.length;
  const browserLive = state?.screen && state.driver === "browser" && !["idle", "preview"].includes(state.status);
  $("recommend").disabled = busy || !browserLive;
  $("send").disabled = busy || !browserLive || !state?.recommendation;
  $("full").disabled = busy || !browserLive;
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
      state = await fetch("/api/state").then((r) => r.json());
      render();
    } catch {
      /* keep the original failure */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "Paused · needs attention";
  } finally {
    busy = false;
    controls();
  }
}
function describe(h) {
  if (h.operation === "TYPE_TEXT") return `Type “${h.text}” into ${h.action}`;
  if (h.operation === "OPEN_APP") return `Open ${h.action}`;
  if (h.operation === "PRESS_KEY") return `Press ${h.choice.replace("key:", "").replace(/_/g, " ")}`;
  if (h.operation === "CLICK") return `Click ${h.action}`;
  return h.action;
}
function tick() {
  if (!state) return;
  let ms = state.elapsed_ms || 0;
  if (runStartedAt) ms = (runStoppedAt || performance.now()) - runStartedAt;
  $("seconds").textContent = seconds(ms);
  const hero = document.querySelector(".hero");
  hero.classList.toggle("running", !!runStartedAt && !runStoppedAt);
  hero.classList.toggle("finished", !!runStoppedAt && state.status === "done");
  hero.classList.toggle("stopped", !!runStoppedAt && state.status === "blocked");
  $("phase-times").textContent = phaseTimes.join(" · ");
  $("viewport").classList.toggle("busy", busy);
}
let treeIndex = null; // null = latest
function renderTree() {
  const moves = (state.chess_moves || []).filter((m) => typeof m === "object" && m.decision && m.decision.candidates);
  const tree = $("tree");
  if (!moves.length) { tree.hidden = true; return; }
  tree.hidden = false;
  const idx = treeIndex == null || treeIndex >= moves.length ? moves.length - 1 : treeIndex;
  const m = moves[idx], d = m.decision;
  const sig = `${moves.length}:${idx}:${m.narration || ""}`;
  if (tree.dataset.sig === sig) return;  // unchanged: do not rebuild (that was the flicker)
  tree.dataset.sig = sig;
  $("tree-move").textContent = `${idx + 1}. ${d.chosen}`;
  $("tree-nav").innerHTML = `${idx + 1} / ${moves.length} <button type="button" id="tree-prev" ${idx === 0 ? "disabled" : ""}>‹</button><button type="button" id="tree-next" ${idx >= moves.length - 1 ? "disabled" : ""}>›</button>`;
  $("tree-prev").onclick = () => { treeIndex = idx - 1; renderTree(); };
  $("tree-next").onclick = () => { treeIndex = idx + 1 >= moves.length - 1 ? null : idx + 1; renderTree(); };
  $("tree-q").textContent = `Jev was asked: ${d.question}` + (d.jev_confidence != null ? ` · confidence ${percent(d.jev_confidence)} · ${d.jev_ms} ms` : "");
  $("tree-cands").innerHTML = d.candidates.map((c) => `<div class="cand ${c.san === d.chosen ? "chosen" : ""}"><span class="rank">stockfish #${c.rank}</span><span class="san">${escape(c.san)}</span><span class="eval">${escape(c.eval)}</span><span class="line">${escape(c.line || "")}</span>${c.p != null ? `<div class="jev">jev ${percent(c.p)}<i style="--p:${c.p * 100}%"></i></div>` : ""}</div>`).join("");
  const why = m.why || {};
  $("tree-say").innerHTML = m.narration ? `${escape(m.narration)}<small>${why.chosen_fact ? `Jev chose the fact “${escape(why.chosen_fact)}” from ${Object.keys(why.facts || {}).length} true facts computed in code` : ""}</small>` : "";
}
function renderHero() {
  renderTree();
  const history = state.history || [];
  const recent = history.slice(-6);
  const stepItems = recent.map((h) => ({ key: `s${h.step}`, cls: "hero-step done", html: `<span>✓</span>${escape(describe(h))}` }));
  const cm = state.chess_moves || [];
  cm.slice(-8).forEach((m, i, arr) => {
    const label = typeof m === "string" ? m : m.label;
    const them = typeof m === "object" && m.who === "them";
    stepItems.push({ key: `c${cm.length - arr.length + i}`, cls: `hero-step done ${them ? "them" : ""}`, html: `<span>${them ? "♚" : "♟"}</span>${escape(label)}` });
  });
  if (state.chess_result) stepItems.push({ key: "chess-result", cls: "hero-step done", html: `<span>★</span>Game: ${escape(state.chess_result)}` });
  if (state.decision && live()) stepItems.push({ key: "current", cls: "hero-step current", html: `<span></span>${escape(state.decision.operation)}${state.decision.target ? ` · ${escape(state.decision.target)}` : ""}` });
  renderList($("hero-steps"), stepItems, (x) => x.key, (x) => `<div class="${x.cls}">${x.html}</div>`);
  const card = $("hero-card");
  card.classList.toggle("final", state.status === "done");
  card.classList.toggle("blocked", state.status === "blocked");
  const titles = {
    done: ["Task complete", "Jev chose DONE · verify the screen"],
    blocked: ["Stopped", "No supported operation could progress"],
    predicted: ["Choice ready", "Inspect the target, then execute"],
    ready: ["Choose. Act. Repeat.", "One Jev request per step"],
    preview: ["Previewing the monitor", "Start a task to begin"],
    idle: ["Choose. Act. Repeat.", "One Jev request per step"],
  };
  const [t, s] = titles[state.status] || titles.ready;
  $("hero-title").textContent = runStartedAt && !runStoppedAt ? (phase.includes("messag") ? "Messaging…" : phase.includes("pick") ? "Choosing a car…" : "Working…") : t;
  $("hero-sub").textContent = s;
  const latencies = (state.decisions || []).map((d) => d.latency_ms).filter((n) => n != null).sort((a, b) => a - b);
  $("median").textContent = latencies.length ? `${latencies[Math.floor(latencies.length / 2)]} ms` : "—";
  const rec = state.recommendation;
  $("hero-rec").hidden = !rec;
  if (rec) {
    $("rec-title").textContent = rec.summary;
    $("rec-reason").textContent = rec.reason.charAt(0).toUpperCase() + rec.reason.slice(1) + ".";
    $("rec-meta").textContent = `${rec.considered} listings read · pick confidence ${percent(rec.confidence)} · ${rec.harvest_ms} ms harvest`;
    const ranked = rec.ranked.slice(0, 5).map((r, i) => ({ key: `r${i}`, html: `<div><b>${percent(r.p)}</b><span>${r.href ? `<a href="${escape(r.href)}" target="_blank" rel="noopener">${escape(r.summary)}</a>` : escape(r.summary)}</span></div>` }));
    if (rec.saved) ranked.push({ key: "saved", html: `<div><b>saved</b><span>${escape(rec.saved)}</span></div>` });
    renderList($("rec-ranked"), ranked, (x) => x.key, (x) => x.html);
    const m = state.message_result;
    $("msg-meta").hidden = !m;
    if (m) $("msg-meta").textContent = m.results ? `✉ ${m.sent.length}/${m.results.length} sent: ` + m.results.map((r) => `${r.status === "done" ? "✓" : "✗"} ${r.listing || ""}`).join(" · ") : m.status === "done" ? `✉ Sent: “${m.text}”` : `✉ Message not confirmed (${m.status})`;
  }
  const c = state.costs;
  if (c) {
    $("cost-total").textContent = usd(c.total_usd);
    $("cost-total").title = `${c.jev.requests} Jev requests at $${c.jev.price_per_mtok_in}/M input tokens (output free)` + (c.text.requests ? ` + ${c.text.requests} text-helper calls` : "");
    $("cost-jev").textContent = `${c.jev.input_tokens.toLocaleString()}`;
    $("cost-text").textContent = c.text.requests
      ? c.text.usd != null ? usd(c.text.usd) : `${(c.text.prompt_tokens + c.text.completion_tokens).toLocaleString()} tok · unpriced`
      : "—";
  }
  tick();
}
function render() {
  if (!state) return;
  $("helper").textContent = `Text helper · ${state.text_model}`;
  $("model-tag").innerHTML = `${escape(state.jev_model)} <span>+ ${escape(state.text_model)}</span>`;
  const screen = state.screen,
    d = state.decision || (["done", "blocked"].includes(state.status) ? state.decisions?.at(-1) : null);
  const labels = {
    idle: "Ready",
    preview: "Monitor previewed · start a task",
    ready: "Screen observed · click Choose next or Run automatically",
    predicted: "Choice ready · Execute choice, or Run automatically",
    done: "Jev reports complete · verify the screen",
    blocked: "Stopped · no supported next action",
  };
  $("status").textContent = labels[state.status] || state.status;
  renderHero();
  if (!screen) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  if (screen.screenshot) $("screenshot").src = `data:image/jpeg;base64,${screen.screenshot}`;
  const view = screen.view || state.view || { x: 0, y: 0, w: 1920, h: 1080 };
  $("viewport").style.aspectRatio = `${view.w}/${view.h}`;
  $("url-bar").textContent = state.driver === "browser" ? screen.url : `${screen.app || "—"} · ${screen.title || "no window on this monitor"}`;
  $("pipeline").textContent = state.driver === "browser" ? "DOM snapshot → typed choices → CDP" : "AX tree → typed choices → CGEvent";
  $("page-title").textContent = `${screen.app || "—"} · ${screen.visited || 0} ${state.driver === "browser" ? "DOM controls" : "AX nodes"}${screen.truncated ? " (truncated)" : ""}${screen.omitted_actions ? ` · ${screen.omitted_actions} omitted` : ""}`;
  const elements = state.elements || [];
  $("action-count").textContent = `${elements.length} elements`;
  const chosen = screen.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d ? chosen?.label || d.choice : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : d ? percent(d.confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("cost-step").textContent = d ? `${usd(jevUsd(d.usage, state.costs?.jev?.price_per_mtok_in))} · ${(d.usage?.input_tokens || 0).toLocaleString()} in / ${(d.usage?.output_tokens || 0).toLocaleString()} out` : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a, b) => b[1] - a[1]);
  $("operation-choices").innerHTML =
    (d?.escalated ? `<span class="operation-choice llm">⚡ ${escape(d.escalated.model)} · ${escape(d.escalated.why)} · ${d.escalated.latency_ms} ms (Jev said ${escape(d.escalated.jev_choice)})</span>` : "") +
    op.map(([name, p]) => `<span class="operation-choice ${name === d.operation ? "best" : ""}">${escape(name)} <b>${percent(p)}</b></span>`).join("");
  const esc = state.escalations || [];
  $("hero-esc").hidden = !esc.length;
  renderList($("hero-esc"), esc.slice(-6).map((e, i) => ({ ...e, key: `${esc.length - 6 + i}` })), (e) => e.key,
    (e) => `<div><b class="${e.slot === "second opinion" || e.slot === "rule" ? "strong" : ""}">${escape(e.slot === "second opinion" ? "fable" : e.slot === "tie-break" ? "haiku" : e.slot)}</b><span>${escape(e.value)}</span><em>${e.latency_ms} ms</em></div>`);
  const rules = state.site_rules || [];
  $("hero-rules").hidden = !rules.length && state.trust_jev == null;
  $("hero-rules").innerHTML = `<span>JEV RULES FOR THIS SITE${state.trust_jev != null ? ` · trust ${percent(state.trust_jev)}` : ""}</span>` + rules.map((r) => `<div>• ${escape(r)}</div>`).join("");
  const tv = $("text-value");
  if (d?.operation === "TYPE_TEXT" && (d.selected_text || state.text_calls?.length)) {
    const last = state.text_calls?.at(-1);
    tv.hidden = false;
    tv.innerHTML = d.selected_text
      ? `<span>Text · selected by Jev</span><b>“${escape(d.selected_text)}”</b>`
      : `<span>Text · ${escape(last?.model)} · ${last?.latency_ms} ms</span><b>“${escape(last?.value)}”</b>`;
  } else tv.hidden = true;
  const probability = (e) => d?.target_probabilities?.[e.index] ?? -1;
  const selectedIndex = d && ["CLICK", "TYPE_TEXT"].includes(d.operation) ? d.target : null;
  const sorted = [...elements];
  if (d) sorted.sort((a, b) => probability(b) - probability(a));
  const appKeys = d && ["OPEN_APP", "PRESS_KEY"].includes(d.operation)
    ? Object.entries(d.target_probabilities || {}).sort((a, b) => b[1] - a[1]).slice(0, 8)
        .map(([k, p]) => `<div class="choice ${k === d.target ? "best" : ""}"><span class="choice-id">${d.operation === "OPEN_APP" ? "app" : "key"}</span><div class="choice-label">${escape(k)}<div class="bar" style="--probability:${p * 100}%"></div></div><span class="probability">${percent(p)}</span></div>`)
        .join("")
    : "";
  $("choices").innerHTML =
    appKeys +
    sorted
      .map((e) => {
        const p = probability(e);
        return `<div class="choice ${selectedIndex === e.index ? "best" : ""}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(" / "))}${e.value ? " · " + escape(e.value) : ""}${e.checked !== undefined ? " · checked " + escape(e.checked) : ""}</small>${p >= 0 ? `<div class="bar" style="--probability:${p * 100}%"></div>` : ""}</div><span class="probability">${p >= 0 ? percent(p) : "—"}</span></div>`;
      })
      .join("");
  const targets = new Map();
  for (const a of screen.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()]
    .map((a, i) => {
      const index = String(i + 1);
      return `<div class="target ${index === selectedIndex ? "selected" : ""}" data-action="${index}" style="left:${(100 * (a.rect.x - view.x)) / view.w}%;top:${(100 * (a.rect.y - view.y)) / view.h}%;width:${(100 * a.rect.w) / view.w}%;height:${(100 * a.rect.h) / view.h}%"><span>${index}</span></div>`;
    })
    .join("");
  $("targets").hidden = !$("overlays").checked;
  if (state.history?.length) {
    if ($("history").querySelector("p")) $("history").innerHTML = "";
    renderList($("history"), state.history, (h) => `h${h.step}`,
      (h) => `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(describe(h))}${h.text ? `<small>${escape(h.text_helper)}</small>` : ""}</div><span class="time">${h.latency_ms} ms · ${percent(h.probability)}</span><span class="cost">${usd(jevUsd(h.usage, state.costs?.jev?.price_per_mtok_in))}</span><span class="effect">${(h.screen_changed ?? h.page_changed) ? "Screen changed" : "No change observed"}</span></div>`);
  } else {
    $("history").innerHTML = '<p class="muted">Each executed action leaves an observed result.</p>';
  }
  $("step-count").textContent = `${state.history?.length || 0} actions · ${((state.elapsed_ms || 0) / 1000).toFixed(2)} s · ${state.decisions?.length || 0} Jev calls · ${usd(state.costs?.total_usd)}`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      screen: { app: screen.app, window: screen.title, text: screen.text },
      actions: screen.actions.map(({ rect, node, at, ...rest }) => rest),
    },
    null,
    2,
  );
  controls();
}
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  runStartedAt = null;
  runStoppedAt = null;
  phaseTimes = [];
  setPhase("", "");
  perform(async () => {
    await call("reset", { goal: $("goal").value, display: $("display").value, driver: $("driver").value, url: $("url").value });
    $("status").textContent = "Jev is comparing the actions…";
    await call("predict");
  }, "Observing the monitor…");
});
async function go() {
  automatic = false;
  runStartedAt = null;
  runStoppedAt = null;
  phaseTimes = [];
  setPhase("", "");
  await perform(async () => {
    clockStart();
    setPhase("exec", "opening the tab · planning");
    $("status").textContent = "Opening the tab and planning the checklist…";
    const chessy = state?.workflows?.[$("workflow").value]?.mode === "chess" || /chess\.com/i.test($("url").value) || /\bchess\b/i.test($("goal").value);
    if (chessy && !/chess\.com/i.test($("url").value)) $("url").value = "https://www.chess.com";
    await call("reset", { goal: $("goal").value, display: $("display").value, driver: $("driver").value, url: $("url").value });
    clockMark("ready");
    await runJob(chessy ? "chess" : "full", chessy ? "Getting to the board, then Stockfish + Jev play…" : "Running the full flow…");
  }, "Starting…");
}
$("go").addEventListener("click", go);
$("goal").addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    go();
  }
});
$("preview").addEventListener("click", () => perform(() => call("preview", { display: $("display").value }), "Reading the monitor…"));
$("choose").addEventListener("click", () => perform(() => call("predict"), "Jev is comparing the actions…"));
$("execute").addEventListener("click", () =>
  perform(() => call("act", { fingerprint: state.screen.fingerprint }), "Executing the choice…"),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    if (!runStartedAt || runStoppedAt) clockStart();
    await runJob("auto", "Running…");
  }, "Running…"),
);
$("recommend").addEventListener("click", () =>
  perform(async () => {
    if (!runStartedAt || runStoppedAt) clockStart();
    await runJob("recommend", "Reading the listings and asking Jev to pick one…");
  }, "Reading the listings and asking Jev to pick one…"),
);
$("send").addEventListener("click", () =>
  perform(async () => {
    if (!runStartedAt || runStoppedAt) clockStart();
    await runJob("message", "Opening the conversation and sending the message…");
  }, "Opening the conversation and sending the message…"),
);
$("full").addEventListener("click", () =>
  perform(async () => {
    clockStart();
    await runJob("full", "Running the full flow…");
  }, "Running the full flow…"),
);
$("stop").addEventListener("click", async () => {
  $("status").textContent = "Pausing after the current step…";
  try {
    await fetch("/api/stop", { method: "POST", headers: { "Content-Type": "application/json", "X-Demo-Token": token }, body: "{}" });
  } catch {
    /* the poll loop will notice */
  }
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document.querySelectorAll(".target").forEach((t) => t.classList.toggle("selected", t.dataset.action === id || t.dataset.action === state?.decision?.target));
});
$("choices").addEventListener("pointerleave", () =>
  document.querySelectorAll(".target").forEach((t) => t.classList.toggle("selected", t.dataset.action === state?.decision?.target)),
);
$("download").addEventListener("click", () => {
  const { screen, ...rest } = state;
  const blob = new Blob([JSON.stringify({ ...rest, screen: { ...screen, screenshot: undefined } }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "jev-voice-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
ticker = setInterval(tick, 50);
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    state = s;
    if (s.display) $("display").value = s.display;
    if (s.driver) $("driver").value = s.driver;
    if (s.start_url) $("url").value = s.start_url;
    renderWorkflows();
    let remembered = null;
    try { remembered = localStorage.getItem("jev-workflow"); } catch {}
    const first = Object.keys(s.workflows || {})[0];
    const pick = remembered && s.workflows?.[remembered] ? remembered : first;
    if (pick) { $("workflow").value = pick; applyWorkflow(pick); $("wf-delete").disabled = false; }
    render();
  })
  .catch(() => {
    $("status").textContent = "Cannot reach the local inspector";
  });
