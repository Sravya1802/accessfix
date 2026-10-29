"use strict";

const $ = (sel) => document.querySelector(sel);
const RING = 326.73;
const TIER_BADGE = {
  tavily: ["badge-tavily", "Tavily"],
  nano: ["badge-nano", "Nemotron Nano"],
  super: ["badge-super", "Nemotron Super"],
  ultra: ["badge-ultra", "Nemotron Ultra"],
};
const CHECK = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CROSS = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></svg>';

let config = { demo_url: "", self_url: "" };
let source = null;
let fixCount = 0;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function badge(cls, text) {
  return el("span", { class: `badge ${cls}` }, text);
}

function announce(msg) {
  const s = $("#sr-status");
  s.textContent = "";
  setTimeout(() => { s.textContent = msg; }, 50);
}

async function loadConfig() {
  try {
    config = await (await fetch("/api/config")).json();
    $("#demo-link").href = config.demo_url;
    const pill = $("#mode-pill");
    const mocks = [config.mock_llm && "Nemotron", config.mock_tavily && "Tavily"].filter(Boolean);
    if (mocks.length) {
      pill.textContent = `Mock mode: canned ${mocks.join(" + ")} responses`;
      pill.hidden = false;
    }
  } catch (e) {
    /* config is optional */
  }
}

// ---------- timeline ----------

function renderStep(ev) {
  const list = $("#timeline");
  let li = document.getElementById(`step-${ev.id}`);
  if (!li) {
    li = el("li", { id: `step-${ev.id}` });
    list.append(li);
  }
  li.className = `is-${ev.status}` + (ev.id.startsWith("retry") ? " is-retry" : "");
  const icon = el("span", { class: "t-icon", html: ev.status === "done" ? CHECK : ev.status === "error" ? CROSS : "" });
  const label = el("div", { class: "t-label" }, ev.label);
  if (ev.tier && TIER_BADGE[ev.tier]) label.append(badge(...TIER_BADGE[ev.tier]));
  if (ev.id.startsWith("verify") || ev.id === "scan" || ev.id === "final") label.append(badge("badge-axe", "axe-core"));
  if (ev.id.startsWith("retry")) label.append(badge("badge-retry", "Retry loop"));
  const status = el("span", { class: "sr-only" }, ev.status === "running" ? "in progress" : ev.status === "done" ? "done" : "failed");
  li.replaceChildren(icon, el("div", {}, label, status, ev.detail ? el("div", { class: "t-detail" }, ev.detail) : null));

  if (ev.id === "scan" && ev.status === "done") {
    const m = ev.detail.match(/^(\d+) issues.*score (\d+)/);
    if (m) {
      setRing(+m[2], false);
      $("#stat-issues").textContent = m[1];
      $("#score-line").textContent = `Before: ${m[2]}/100. Fixing…`;
    }
  }
}

function renderLog(ev) {
  if (ev.message === "Pages chosen") return;
  $("#timeline").append(el("li", {}, el("span", { "aria-hidden": "true" }), el("div", { class: "t-log" }, ev.message)));
}

// ---------- score ring ----------

function scoreColor(v) {
  return v >= 90 ? "#15803d" : v >= 60 ? "#b45309" : "#b91c1c";
}

function setRing(value, animateFrom) {
  const ring = $("#ring-value");
  const num = $("#ring-number");
  const draw = (v) => {
    ring.style.strokeDashoffset = String(RING * (1 - v / 100));
    ring.style.stroke = scoreColor(v);
    num.textContent = String(Math.round(v));
  };
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (animateFrom === false || reduce) return draw(value);
  const start = performance.now();
  const dur = 1400;
  const tick = (t) => {
    const p = Math.min(1, (t - start) / dur);
    const eased = 1 - Math.pow(1 - p, 3);
    draw(animateFrom + (value - animateFrom) * eased);
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// ---------- results ----------

function diffParts(a, b) {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  let j = 0;
  while (j < a.length - i && j < b.length - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
  return [
    [a.slice(0, i), a.slice(i, a.length - j), a.slice(a.length - j)],
    [b.slice(0, i), b.slice(i, b.length - j), b.slice(b.length - j)],
  ];
}

function diffLine(cls, [pre, mid, post], label) {
  return el("pre", { class: cls }, el("span", { class: "sr-only" }, label), pre, mid ? el("mark", {}, mid) : null, post);
}

function renderFix(p) {
  const head = el("div", { class: "fix-head" },
    p.wcag ? badge("badge-plain", `WCAG ${p.wcag}`) : null,
    el("code", {}, p.selector),
    p.verified ? badge("badge-ok", "Verified by axe ✓") : badge("badge-bad", "Still failing"),
    p.verified && p.needs_review ? badge("badge-review", "Needs review") : null,
    p.attempt > 1 ? badge("badge-retry", `Fixed on attempt ${p.attempt}`) : badge("badge-plain", `Attempt ${p.attempt || 1}`),
  );
  const parts = [head, el("p", {}, p.explanation || "")];
  if (!p.verified && p.reason) parts.push(el("p", { class: "help" }, `Why it still fails: ${p.reason}`));
  if (p.after_html) {
    const [before, after] = diffParts(p.before_html || "", p.after_html || "");
    parts.push(el("div", { class: "diff" }, diffLine("del", before, "Before: "), diffLine("add", after, "After: ")));
  }
  if (p.sources && p.sources.length) {
    parts.push(el("div", { class: "sources" }, el("span", {}, "Sources (Tavily):"),
      p.sources.map((s) => el("a", { href: s.url, target: "_blank", rel: "noopener", title: s.title }, s.title || s.url))));
  }
  return el("div", { class: "fix" + (p.verified ? "" : " unverified") }, parts);
}

function renderFixes(patches) {
  const box = $("#fixes");
  box.replaceChildren();
  if (!patches.length) {
    box.append(el("p", {}, "No automatic fixes were needed."));
    return;
  }
  const groups = new Map();
  for (const p of patches) {
    if (!groups.has(p.rule_id)) groups.set(p.rule_id, []);
    groups.get(p.rule_id).push(p);
  }
  // Retried rules first (the loop is the story), the long contrast list last.
  const rank = ([rule, list]) => (list.some((p) => p.attempt > 1) ? 0 : rule === "color-contrast" ? 2 : 1);
  const ordered = [...groups].sort((a, b) => rank(a) - rank(b));
  let first = true;
  for (const [rule, list] of ordered) {
    const ok = list.filter((p) => p.verified).length;
    const retried = list.some((p) => p.attempt > 1);
    const details = el("details", { class: "rule-group", open: first || retried });
    details.append(el("summary", {},
      el("span", {}, list[0].help || rule),
      el("span", { class: "rule-id" }, rule),
      badge(ok === list.length ? "badge-ok" : "badge-review", `${ok}/${list.length} verified`),
      retried ? badge("badge-retry", "Retry loop fixed some") : null));
    list.forEach((p) => details.append(renderFix(p)));
    box.append(details);
    first = false;
  }
}

function renderShots(pages) {
  const box = $("#shots");
  box.replaceChildren();
  const p = pages[0];
  if (!p) return;
  const fig = (src, cap, score, cls) => el("figure", {},
    src ? el("img", { src: `data:image/jpeg;base64,${src}`, alt: `Screenshot of ${p.title || p.url} ${cap.toLowerCase()}`, loading: "lazy" }) : el("p", {}, "No screenshot"),
    el("figcaption", {}, cap, badge(cls, `Score ${score}`)));
  box.append(fig(p.screenshot_before, "Before fixes", p.score_before, "badge-bad"),
             fig(p.screenshot_after, "After fixes (patched in the browser)", p.score_after, "badge-ok"));
  if (pages.length > 1) {
    box.append(el("p", { class: "help" }, `Scanned ${pages.length} pages: `,
      pages.map((pg, i) => `${i ? " · " : ""}${pg.url} (${pg.score_before} → ${pg.score_after})`).join("")));
  }
}

function renderHuman(items, unfixed) {
  const list = $("#human");
  list.replaceChildren();
  const all = [...items.map((n) => ({ ...n, kind: "Needs a human" })),
               ...unfixed.filter((u) => u.reason.startsWith("Over the")).map((u) => ({ ...u, kind: "Not attempted" }))];
  $("#human-section").hidden = !all.length;
  for (const n of all) {
    const where = n.selector || (Array.isArray(n.target) ? n.target.flat().join(" → ") : "");
    list.append(el("li", {},
      el("div", { class: "fix-head" }, badge("badge-review", n.kind), n.wcag ? badge("badge-plain", `WCAG ${n.wcag}`) : null,
         el("strong", {}, n.help || n.rule_id), where ? el("code", {}, where) : null),
      el("p", { class: "help" }, n.reason)));
  }
}

function renderTrace(calls, tools) {
  const body = $("#trace-body");
  body.replaceChildren();
  let lat = 0, pin = 0, pout = 0;
  for (const c of calls) {
    lat += c.latency_ms || 0;
    pin += c.prompt_tokens || 0;
    pout += c.completion_tokens || 0;
    body.append(el("tr", {},
      el("td", {}, badge(...TIER_BADGE[c.tier])),
      el("td", { class: "mono" }, c.model),
      el("td", {}, c.purpose, c.error ? el("div", { class: "help" }, `Error: ${c.error}`) : null),
      el("td", { class: "num" }, `${(c.latency_ms / 1000).toFixed(2)} s`),
      el("td", { class: "num" }, c.prompt_tokens ?? "–"),
      el("td", { class: "num" }, c.completion_tokens ?? "–")));
  }
  $("#trace-foot").replaceChildren(el("tr", {},
    el("td", { colspan: "3" }, `${calls.length} model calls`),
    el("td", { class: "num" }, `${(lat / 1000).toFixed(2)} s`),
    el("td", { class: "num" }, pin || "–"),
    el("td", { class: "num" }, pout || "–")));

  const tb = $("#tools-body");
  tb.replaceChildren();
  for (const t of tools) {
    const cls = t.tool.startsWith("Tavily") ? "badge-tavily" : t.tool.startsWith("axe") ? "badge-axe" : "badge-tool";
    const noteText = t.error ? `Error: ${t.error}` : t.cached ? "cached" : t.mock ? "mock" : "";
    tb.append(el("tr", {}, el("td", {}, badge(cls, t.tool)), el("td", { class: "mono" }, t.detail),
      el("td", { class: "num" }, `${(t.latency_ms / 1000).toFixed(2)} s`), el("td", {}, noteText)));
  }
}

function renderReport(md) {
  const box = $("#report");
  if (window.marked && window.DOMPurify) {
    box.innerHTML = DOMPurify.sanitize(marked.parse(md));
    box.querySelectorAll("h1, h2, h3").forEach((h) => {
      const h4 = el("h4", {}, h.textContent);
      h.replaceWith(h4);
    });
  } else {
    box.replaceChildren(el("pre", {}, md));
  }
}

function renderResult(r, runId) {
  const verified = r.patches.filter((p) => p.verified).length;
  setRing(r.score_after, r.score_before);
  $("#ring-label").textContent = `Score improved from ${r.score_before} to ${r.score_after} out of 100`;
  $("#score-line").textContent = `${r.score_before} → ${r.score_after}`;
  $("#stat-issues").textContent = `${r.issues_before} → ${r.issues_after}`;
  $("#stat-fixed").textContent = String(verified);
  $("#stat-human").textContent = String(r.needs_human.length);
  renderShots(r.pages);
  renderFixes(r.patches);
  renderHuman(r.needs_human, r.unfixed);
  renderReport(r.report_md);
  renderTrace(r.model_calls, r.tool_calls);
  $("#dl-patch").href = `/api/runs/${runId}/download?type=patch`;
  $("#dl-report").href = `/api/runs/${runId}/download?type=report`;
  $("#results").hidden = false;
  announce(`Run complete. Score went from ${r.score_before} to ${r.score_after}. ${verified} fixes verified by axe-core.`);
}

// ---------- run lifecycle ----------

function resetRun(url) {
  fixCount = 0;
  $("#run").hidden = false;
  $("#results").hidden = true;
  $("#run-error").hidden = true;
  $("#timeline").replaceChildren();
  $("#run-url").textContent = url;
  $("#ring-number").textContent = "–";
  $("#ring-label").textContent = "Score not available yet";
  $("#ring-value").style.strokeDashoffset = String(RING);
  $("#score-line").textContent = "Waiting for the first scan…";
  $("#stat-issues").textContent = "–";
  $("#stat-fixed").textContent = "0";
  $("#stat-human").textContent = "–";
}

function setBusy(busy) {
  const b = $("#scan-button");
  b.disabled = busy;
  b.textContent = busy ? "Scanning…" : "Scan & Fix";
}

function showError(msg) {
  const box = $("#run-error");
  box.textContent = msg;
  box.hidden = false;
}

async function startRun(rawUrl) {
  const err = $("#url-error");
  err.textContent = "";
  if (!rawUrl.trim()) {
    err.textContent = "Enter a URL to scan.";
    $("#url-input").focus();
    return;
  }
  if (source) source.close();
  setBusy(true);
  let res;
  try {
    res = await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: rawUrl }) });
  } catch (e) {
    err.textContent = "Couldn't reach the AccessFix server.";
    setBusy(false);
    return;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    err.textContent = data.detail || "That URL can't be scanned.";
    setBusy(false);
    $("#url-input").focus();
    return;
  }
  resetRun(data.url);
  $("#run").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  $("#run-title").setAttribute("tabindex", "-1");
  $("#run-title").focus({ preventScroll: true });
  announce("Scan started.");

  source = new EventSource(`/api/runs/${data.run_id}/events`);
  source.addEventListener("step", (e) => renderStep(JSON.parse(e.data)));
  source.addEventListener("log", (e) => renderLog(JSON.parse(e.data)));
  source.addEventListener("fix", () => { fixCount += 1; $("#stat-fixed").textContent = String(fixCount); });
  source.addEventListener("result", (e) => renderResult(JSON.parse(e.data), data.run_id));
  source.addEventListener("error", (e) => {
    if (e.data) {
      showError(`The run failed: ${JSON.parse(e.data).message}`);
      announce("The run failed.");
    }
  });
  source.addEventListener("end", () => { source.close(); setBusy(false); });
}

document.addEventListener("DOMContentLoaded", () => {
  loadConfig();
  $("#scan-form").addEventListener("submit", (e) => {
    e.preventDefault();
    startRun($("#url-input").value);
  });
  $("#demo-button").addEventListener("click", () => {
    $("#url-input").value = config.demo_url || `${location.origin}/demo/sunny-crumb.html`;
    $("#url-error").textContent = "";
    $("#scan-button").focus();
  });
  $("#self-button").addEventListener("click", () => {
    $("#url-input").value = config.self_url || `${location.origin}/`;
    startRun($("#url-input").value);
  });
});
