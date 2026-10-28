"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Escaped text with `inline code` spans, for model-written prose.
const prose = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");

const icon = {
  arrow: '<svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true"><path d="M4 10 10 4M5 4h5v5" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  chev: '<svg class="chev" width="10" height="10" viewBox="0 0 10 10"><path d="M3.5 2l3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>',
  folder: '<svg width="14" height="14" viewBox="0 0 16 16"><path d="M1.5 3.5h4.5l1.5 1.5h7v8h-13z" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
  file: '<svg width="14" height="14" viewBox="0 0 16 16"><path d="M3.5 1.5h6l3 3v10h-9z M9.5 1.5v3h3" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
  ext: '<svg width="10" height="10" viewBox="0 0 12 12"><path d="M5 2H2v8h8V7M7 1.5h3.5V5M10.5 1.5 5.5 6.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',
  check: '<svg width="12" height="12" viewBox="0 0 12 12"><path d="M2 6.5 5 9l5-6" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>',
  warn: '<svg width="12" height="12" viewBox="0 0 12 12"><path d="M6 1.5 11 10.5H1z M6 5v2.5 M6 8.6v.1" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',
};

const KIND_LABEL = {
  commit: "Commit message",
  pr: "Pull request",
  review_comment: "Review comment",
  review: "Review",
  pr_comment: "PR comment",
  issue: "Issue",
  issue_comment: "Issue comment",
};

const VERDICT = {
  found: { label: "Rationale recorded", note: "Every statement below is backed by a cited source." },
  partial: { label: "Partially recorded", note: "The record explains part of this. Gaps are listed as open questions." },
  none: { label: "No recorded rationale", note: "Nothing written down explains this code. Treat the reason as unknown and ask before changing it." },
  error: { label: "Could not complete", note: "" },
};

const state = {
  repo: null,
  files: [],
  path: null,
  lines: [],
  sel: null, // {start, end}
  anchor: null,
  openDirs: new Set(),
  trace: null, // {status: running|done|error, steps, warnings, result}
  tab: "evidence",
  showNoise: false,
  source: null,
};

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    if (!s.allow_local) $("#sourceInput").placeholder = "GitHub URL or owner/name";
    const models = s.providers.length
      ? s.providers.map((p, i) => {
          const back = p.paused_until ? new Date(p.paused_until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
          const title = back ? `Limit reached, back around ${back}` : i ? "Used when the model before it hits its limit" : "Primary model";
          return `<span class="chip" title="${esc(title)}"><span class="dot ${back ? "warn" : "ok"}"></span>${i ? "Fallback: " : ""}${esc(p.label)} · ${esc(p.model)}${back ? ` · back ${esc(back)}` : ""}</span>`;
        }).join("")
      : `<span class="chip" title="Add GROQ_API_KEY or GEMINI_API_KEY to .env"><span class="dot warn"></span>No model configured</span>`;
    $("#status").innerHTML = `${models}
      <span class="chip" title="${s.github_token ? "Authenticated" : "Unauthenticated: 60 requests/hour"}">
        <span class="dot ${s.github_token ? "ok" : ""}"></span>GitHub ${s.github_token ? "token" : "public"}
      </span>`;
  } catch { /* server not ready */ }
}

async function loadRecent() {
  const list = await api("/api/repos").catch(() => []);
  $("#recent").hidden = !list.length;
  $("#recentList").innerHTML = list.slice(0, 6).map((r) => `
    <li><button data-source="${esc(sourceOf(r))}">
      <span class="r-name">${esc(r.name)}</span><span class="path">${esc(sourceOf(r))}</span>
      <span class="r-meta">${r.ephemeral ? "Temporary clone, removed when closed" : "Local folder"} · ${(r.head?.commits ?? 0).toLocaleString()} commits</span>
    </button></li>`).join("");
}

const sourceOf = (r) => r.source || r.root;

// Tell the server we're done so it deletes a temporary clone (local folders are kept).
function closeClone(repo) {
  if (repo?.ephemeral) fetch(`/api/repos/${repo.id}/clone`, { method: "DELETE" }).catch(() => {});
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

async function openRepo(source, { initialFile = true } = {}) {
  const btn = $("#openBtn");
  const err = $("#openError");
  err.hidden = true;
  btn.disabled = true;
  $("#openLabel").textContent = "Opening…";
  try {
    const repo = await api("/api/repos", { method: "POST", body: JSON.stringify({ source }) });
    const { files } = await api(`/api/repos/${repo.id}/files`);
    if (state.repo && state.repo.id !== repo.id) closeClone(state.repo);
    state.repo = repo;
    state.files = files;
    state.path = null;
    state.sel = null;
    state.trace = null;
    state.openDirs = new Set();
    try { localStorage.setItem("code-history.repo", source); } catch {}
    showWorkspace();
    renderTree();
    renderInspector();
    const initial = initialFile && files.find((f) => /^readme/i.test(f));
    if (initial) openFile(initial);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
    $("#openLabel").textContent = "Open repository";
  }
}

function showWorkspace() {
  const r = state.repo;
  $("#layout").classList.remove("empty");
  $("#welcome").hidden = true;
  $("#sidebar").hidden = false;
  $("#viewer").hidden = false;
  $("#inspector").hidden = false;
  $("#repoSwitch").hidden = false;
  $("#repoName").textContent = r.name;
  $("#repoRef").textContent = `${r.head.branch} · ${r.head.sha.slice(0, 7)}`;
}

function showWelcome() {
  $("#layout").classList.add("empty");
  $("#welcome").hidden = false;
  $("#sidebar").hidden = true;
  $("#viewer").hidden = true;
  $("#inspector").hidden = true;
  $("#repoSwitch").hidden = true;
  loadRecent();
  $("#sourceInput").focus();
}

// ---------------------------------------------------------------------------
// File tree
// ---------------------------------------------------------------------------

function buildTree(paths) {
  const root = { dirs: new Map(), files: [] };
  for (const p of paths) {
    const parts = p.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!node.dirs.has(parts[i])) node.dirs.set(parts[i], { dirs: new Map(), files: [] });
      node = node.dirs.get(parts[i]);
    }
    node.files.push(p);
  }
  return root;
}

function renderTree() {
  const q = $("#fileFilter").value.trim().toLowerCase();
  const el = $("#tree");
  if (q) {
    const hits = state.files.filter((f) => f.toLowerCase().includes(q)).slice(0, 300);
    el.innerHTML = hits.length
      ? hits.map((f) => {
          const i = f.lastIndexOf("/");
          return `<div class="node ${f === state.path ? "active" : ""}" data-file="${esc(f)}" style="padding-left:8px">
            ${icon.file}<span class="label">${esc(f.slice(i + 1))}</span><span class="dir-path">${esc(f.slice(0, i))}</span></div>`;
        }).join("")
      : `<div class="code-msg">No matching files</div>`;
    return;
  }
  const out = [];
  const walk = (node, prefix, depth) => {
    const dirs = [...node.dirs.keys()].sort((a, b) => a.localeCompare(b));
    for (const d of dirs) {
      const full = prefix + d;
      const open = state.openDirs.has(full);
      out.push(`<div class="node ${open ? "open" : ""}" data-dir="${esc(full)}" style="padding-left:${8 + depth * 14}px">
        ${icon.chev}${icon.folder}<span class="label">${esc(d)}</span></div>`);
      if (open) walk(node.dirs.get(d), full + "/", depth + 1);
    }
    for (const f of node.files.sort((a, b) => a.localeCompare(b))) {
      out.push(`<div class="node ${f === state.path ? "active" : ""}" data-file="${esc(f)}" style="padding-left:${22 + depth * 14}px">
        ${icon.file}<span class="label">${esc(f.slice(f.lastIndexOf("/") + 1))}</span></div>`);
    }
  };
  walk(buildTree(state.files), "", 0);
  el.innerHTML = out.join("");
}

// ---------------------------------------------------------------------------
// Code viewer
// ---------------------------------------------------------------------------

const LANG = {
  py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
  go: "go", rs: "rust", java: "java", kt: "kotlin", rb: "ruby", php: "php", c: "c", h: "c", cc: "cpp", cpp: "cpp", hpp: "cpp",
  cs: "csharp", swift: "swift", scala: "scala", sh: "bash", bash: "bash", zsh: "bash", ps1: "powershell", sql: "sql",
  json: "json", yml: "yaml", yaml: "yaml", toml: "ini", ini: "ini", md: "markdown", html: "xml", xml: "xml", css: "css", scss: "scss",
  lua: "lua", r: "r", dart: "dart", ex: "elixir", exs: "elixir", hs: "haskell", clj: "clojure", m: "objectivec", vue: "xml",
};

function highlightLine(line, lang) {
  if (!window.hljs || !lang || !hljs.getLanguage(lang)) return esc(line);
  try { return hljs.highlight(line, { language: lang, ignoreIllegals: true }).value; } catch { return esc(line); }
}

async function openFile(path, sel = null) {
  state.path = path;
  state.sel = sel;
  state.anchor = sel ? sel.start : null;
  state.trace = null;
  // reveal in tree
  const parts = path.split("/");
  for (let i = 1; i < parts.length; i++) state.openDirs.add(parts.slice(0, i).join("/"));
  renderTree();
  const crumbs = parts.map((p, i) => (i === parts.length - 1 ? `<b>${esc(p)}</b>` : esc(p))).join(" / ");
  $("#crumbs").innerHTML = crumbs;
  const code = $("#code");
  code.innerHTML = `<div class="code-msg">Loading…</div>`;
  try {
    const { content } = await api(`/api/repos/${state.repo.id}/file?path=${encodeURIComponent(path)}`);
    state.lines = content.replace(/\n$/, "").split("\n");
    const lang = LANG[(path.split(".").pop() || "").toLowerCase()];
    code.innerHTML = state.lines.map((l, i) =>
      `<div class="ln" data-n="${i + 1}"><span class="num">${i + 1}</span><span class="src">${highlightLine(l.replace(/\r$/, ""), lang) || " "}</span></div>`
    ).join("");
    paintSelection();
    if (sel) code.querySelector(`.ln[data-n="${sel.start}"]`)?.scrollIntoView({ block: "center" });
    else code.scrollTop = 0;
  } catch (e) {
    state.lines = [];
    code.innerHTML = `<div class="code-msg">${esc(e.message)}</div>`;
  }
  updateHash();
  renderInspector();
}

function paintSelection() {
  const { sel } = state;
  for (const el of $("#code").querySelectorAll(".ln.sel")) el.classList.remove("sel");
  if (!sel) return;
  for (let n = sel.start; n <= sel.end; n++) $("#code").querySelector(`.ln[data-n="${n}"]`)?.classList.add("sel");
}

function selectLine(n, extend) {
  if (extend && state.anchor) {
    state.sel = { start: Math.min(state.anchor, n), end: Math.max(state.anchor, n) };
  } else {
    state.anchor = n;
    state.sel = { start: n, end: n };
  }
  if (state.sel.end - state.sel.start > 60) state.sel.end = state.sel.start + 60;
  if (state.trace?.status === "running") state.source?.close();
  state.trace = null;
  paintSelection();
  updateHash();
  renderInspector();
}

function updateHash() {
  if (!state.repo || !state.path) return;
  let h = `#${state.repo.id}:${state.path}`;
  if (state.sel) h += `:L${state.sel.start}${state.sel.end !== state.sel.start ? "-L" + state.sel.end : ""}`;
  history.replaceState(null, "", h);
}

// ---------------------------------------------------------------------------
// Trace
// ---------------------------------------------------------------------------

function startTrace() {
  if (!state.sel) return;
  const question = $("#question")?.value.trim() || "";
  state.source?.close();
  state.trace = { status: "running", steps: [], warnings: [], result: null, question };
  state.tab = "evidence";
  state.showNoise = false;
  renderInspector();

  const qs = new URLSearchParams({
    repo: state.repo.id, path: state.path, start: state.sel.start, end: state.sel.end, question,
  });
  const src = new EventSource(`/api/trace?${qs}`);
  state.source = src;
  const t = state.trace;
  src.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "step") t.steps.push(ev);
    else if (ev.type === "warning") t.warnings.push(ev.message);
    else if (ev.type === "error") { t.status = "error"; t.error = ev.message; src.close(); }
    else if (ev.type === "result") { t.status = "done"; t.result = ev; src.close(); }
    if (state.trace === t) renderInspector();
  };
  src.onerror = () => {
    if (t.status === "running") { t.status = "error"; t.error = "Lost connection to the server."; }
    src.close();
    if (state.trace === t) renderInspector();
  };
}

// ---------------------------------------------------------------------------
// Inspector
// ---------------------------------------------------------------------------

function renderInspector() {
  const body = $("#inspectorBody");
  if (!state.sel) {
    body.innerHTML = `
      <div class="hint-block">
        <div class="section-label">Trace a line</div>
        <h3>Select a line to see why it exists</h3>
        <p>Click any line in the file. Shift-click to select a range.</p>
        <ol class="how">
          <li><b>Blame</b> the line, ignoring whitespace and moved code.</li>
          <li><b>Walk its history</b>, skipping formatting-only commits.</li>
          <li><b>Collect discussion</b> from pull requests, reviews and linked issues.</li>
          <li><b>Filter out noise</b> such as approvals, CI chatter and bots.</li>
          <li><b>Answer with citations</b>, each claim checked against its source, or report that nothing was recorded.</li>
        </ol>
      </div>`;
    return;
  }

  const { start, end } = state.sel;
  const span = start === end ? `Line ${start}` : `Lines ${start}–${end}`;
  const preview = state.lines.slice(start - 1, end).map((l) => esc(l.replace(/\r$/, ""))).join("\n");
  const t = state.trace;
  const running = t?.status === "running";

  let html = `
    <div class="target">
      <div class="target-head"><b>${span}</b><span>${esc(state.path.split("/").pop())}</span></div>
      <pre>${preview || " "}</pre>
    </div>`;

  if (!t || running) {
    html += `
      <textarea class="question" id="question" placeholder="Optional: ask something specific, e.g. why 3 retries instead of 5?" ${running ? "disabled" : ""}>${esc(t?.question || "")}</textarea>
      <div class="actions">
        <span class="kbd">Ctrl + Enter</span>
        <button class="btn primary" id="traceBtn" ${running ? "disabled" : ""}>${running ? "Tracing…" : "Trace rationale " + icon.arrow}</button>
      </div>`;
  } else {
    html += `<div class="actions">
      <span class="muted" style="font-size:12px">${t.question ? "Asked: " + esc(t.question) : ""}</span>
      <button class="btn" id="retraceBtn">New question</button></div>`;
  }

  if (t) {
    if (running) html += renderSteps(t.steps, true);
    if (t.status === "error") html += renderSteps(t.steps, false) + `<div class="error-box">${esc(t.error)}</div>`;
    if (t.status === "done") html += renderResult(t);
  }
  body.innerHTML = html;
  $("#question")?.focus();
}

function renderSteps(steps, running) {
  const items = steps.map((s) => `
    <li><span class="mark">${icon.check}</span>
      <div><div class="title">${esc(s.title)}</div>${s.detail ? `<div class="detail">${esc(s.detail)}</div>` : ""}</div></li>`);
  if (running) items.push(`<li><span class="mark"><span class="spinner"></span></span><div class="title muted">Working…</div></li>`);
  return `<ul class="steps">${items.join("")}</ul>`;
}

function renderResult(t) {
  const r = t.result;
  const a = r.answer || { status: "error", summary: "No answer was produced." };
  const v = VERDICT[a.status] || VERDICT.error;
  const evidence = r.evidence || [];

  let html = `
    <div class="verdict ${esc(a.status)}">
      <div class="verdict-label">${esc(v.label)}</div>
      <p>${prose(a.summary)}</p>
      ${v.note ? `<div class="note">${a.unverified ? "Each statement cites a source, but the check that sources back it did not run. Read the cited sources before relying on it." : esc(v.note)}${a.dropped ? ` ${a.dropped} unsupported statement${a.dropped > 1 ? "s were" : " was"} removed during checking.` : ""}${a.trimmed ? ` ${a.trimmed} statement${a.trimmed > 1 ? "s were" : " was"} narrowed to what the source says.` : ""}</div>` : ""}
    </div>`;

  if (a.claims?.length) {
    html += `<div class="section-label">${a.status === "none" ? "What changed, with no reason given" : "What the record says"}</div><ol class="claims">` +
      a.claims.map((c, i) => `<li><span class="n">${i + 1}</span><div>${prose(c.statement)}${c.evidence_ids.map((id) => `<span class="cite" data-cite="${esc(id)}">${esc(id)}</span>`).join("")}</div></li>`).join("") +
      `</ol>`;
  }
  if (a.open_questions?.length) {
    html += `<div class="section-label">Open questions</div><ul class="questions">${a.open_questions.map((q) => `<li>${prose(q)}</li>`).join("")}</ul>`;
  }
  const calls = r.llm_calls || [];
  if (calls.length) {
    const n = (who) => calls.filter((c) => c === who).length;
    const parts = [["groq", "Groq"], ["gemini", "Gemini"]]
      .filter(([who]) => n(who))
      .map(([who, label]) => `${n(who)} ${label} call${n(who) > 1 ? "s" : ""}`);
    if (n("cached")) parts.push(`${n("cached")} answered from cache`);
    if (!n("groq") && !n("gemini")) parts.unshift("No model calls");
    html += `<div class="usage">${parts.join(" · ")}</div>`;
  }
  for (const w of new Set(t.warnings)) if (w !== a.summary) html += `<div class="notice">${esc(w)}</div>`;

  const hidden = (e) => e.label === "noise" || e.label === "skipped";
  const kept = evidence.filter((e) => !hidden(e));
  const noise = evidence.filter(hidden);
  const hist = r.history || [];
  html += `
    <div class="tabs">
      <button class="tab ${state.tab === "evidence" ? "active" : ""}" data-tab="evidence">Sources<span class="count">${kept.length}</span></button>
      <button class="tab ${state.tab === "history" ? "active" : ""}" data-tab="history">History<span class="count">${hist.length}</span></button>
      <button class="tab ${state.tab === "steps" ? "active" : ""}" data-tab="steps">Steps</button>
    </div>`;

  if (state.tab === "evidence") {
    const order = { rationale: 0, context: 1, unclassified: 2 };
    const cited = new Set((a.claims || []).flatMap((c) => c.evidence_ids));
    kept.sort((x, y) => (cited.has(y.id) - cited.has(x.id)) || (order[x.label] - order[y.label]));
    html += kept.length ? kept.map(renderEvidence).join("") : `<p class="muted">No relevant discussion was found.</p>`;
    if (noise.length) {
      html += `<button class="toggle-noise" id="toggleNoise">${state.showNoise ? "Hide" : "Show"} ${noise.length} item${noise.length > 1 ? "s" : ""} filtered out</button>`;
      if (state.showNoise) html += `<div class="noise-list" style="margin-top:10px">${noise.map(renderEvidence).join("")}</div>`;
    }
  } else if (state.tab === "history") {
    html += hist.length ? `<ol class="timeline">${hist.map(renderCommit).join("")}</ol>` : `<p class="muted">No history found.</p>`;
  } else {
    html += renderSteps(t.steps, false);
  }
  return html;
}

function fmtDate(s) {
  if (!s) return "";
  const d = new Date(s);
  return isNaN(d) ? "" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function renderEvidence(e) {
  const long = (e.text || "").length > 420;
  return `
    <article class="ev" id="ev-${esc(e.id)}">
      <div class="ev-head">
        <span class="ev-id">${esc(e.id)}</span>
        <span class="ev-kind">${esc(KIND_LABEL[e.kind] || e.kind)}</span>
        <span class="tag ${esc(e.label)}">${esc(e.label)}</span>
      </div>
      ${e.title && e.kind !== "commit" ? `<div class="ev-title" title="${esc(e.title)}">${esc(e.title)}</div>` : ""}
      <div class="ev-meta">
        <span>${esc(e.author || "unknown")}</span>
        ${e.date ? `<span>·</span><span>${fmtDate(e.date)}</span>` : ""}
        ${e.sha ? `<span>·</span><span class="sha">${esc(e.sha.slice(0, 7))}</span>` : ""}
        ${e.url ? `<span>·</span><a href="${esc(e.url)}" target="_blank" rel="noopener">Open ${icon.ext}</a>` : ""}
      </div>
      <div class="ev-body ${long ? "clamped" : ""}">${esc(e.text)}</div>
      ${long ? `<button class="more" data-more>Show more</button>` : ""}
      ${e.code ? `<div class="ev-code">${esc(e.code)}</div>` : ""}
      ${e.reason ? `<div class="ev-reason">${esc(e.reason)}</div>` : ""}
    </article>`;
}

function renderCommit(c) {
  const kind = { cosmetic: "Formatting only · skipped", introduced: "Introduced", modified: "Modified", untouched: "Did not change this line" }[c.kind] || c.kind;
  return `
    <li class="commit ${c.examined ? "examined" : ""} ${c.kind === "cosmetic" || c.kind === "untouched" ? esc(c.kind) : ""}">
      <div class="commit-top">
        <span class="sha">${esc(c.sha.slice(0, 7))}</span>
        <span class="commit-subject" data-diff="${esc(c.sha)}">${esc(c.subject)}</span>
      </div>
      <div class="commit-meta">
        <span>${esc(c.author)}</span><span>·</span><span>${fmtDate(c.date)}</span>
        <span class="tag ${esc(c.kind)}">${kind}</span>
        ${c.blamed ? `<span class="tag blame">blame</span>` : ""}
      </div>
      <div class="diff" id="diff-${esc(c.sha)}" hidden>
        ${(c.removed || []).map((l) => `<div class="d">- ${esc(l)}</div>`).join("")}
        ${(c.added || []).map((l) => `<div class="a">+ ${esc(l)}</div>`).join("")}
      </div>
    </li>`;
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

$("#openForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#sourceInput").value.trim();
  if (v) openRepo(v);
});

$("#recentList").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-source]");
  if (b) { $("#sourceInput").value = b.dataset.source; openRepo(b.dataset.source); }
});

$("#repoSwitch").addEventListener("click", () => {
  state.source?.close();
  closeClone(state.repo);
  state.repo = null;
  history.replaceState(null, "", location.pathname);
  showWelcome();
});

$("#fileFilter").addEventListener("input", renderTree);

$("#tree").addEventListener("click", (e) => {
  const n = e.target.closest(".node");
  if (!n) return;
  if (n.dataset.dir) {
    const d = n.dataset.dir;
    state.openDirs.has(d) ? state.openDirs.delete(d) : state.openDirs.add(d);
    renderTree();
  } else if (n.dataset.file && n.dataset.file !== state.path) {
    openFile(n.dataset.file);
  }
});

$("#code").addEventListener("mousedown", (e) => {
  if (e.shiftKey) e.preventDefault(); // avoid text selection on range-select
});
$("#code").addEventListener("click", (e) => {
  const ln = e.target.closest(".ln");
  if (!ln || window.getSelection().toString()) return;
  selectLine(Number(ln.dataset.n), e.shiftKey);
});

$("#inspectorBody").addEventListener("click", (e) => {
  const t = e.target;
  if (t.closest("#traceBtn")) return startTrace();
  if (t.closest("#retraceBtn")) { state.trace = null; return renderInspector(); }
  const tab = t.closest(".tab");
  if (tab) { state.tab = tab.dataset.tab; return renderInspector(); }
  if (t.closest("#toggleNoise")) { state.showNoise = !state.showNoise; return renderInspector(); }
  const cite = t.closest(".cite");
  if (cite) {
    if (state.tab !== "evidence") { state.tab = "evidence"; renderInspector(); }
    let card = document.getElementById(`ev-${cite.dataset.cite}`);
    if (!card) { state.showNoise = true; renderInspector(); card = document.getElementById(`ev-${cite.dataset.cite}`); }
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 1400);
    }
    return;
  }
  if (t.closest("[data-more]")) {
    const body = t.closest(".ev").querySelector(".ev-body");
    body.classList.toggle("clamped");
    t.textContent = body.classList.contains("clamped") ? "Show more" : "Show less";
    return;
  }
  const subj = t.closest("[data-diff]");
  if (subj) {
    const d = document.getElementById(`diff-${subj.dataset.diff}`);
    if (d) d.hidden = !d.hidden;
  }
});

$("#inspectorBody").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && e.target.id === "question") {
    e.preventDefault();
    startTrace();
  }
});

document.addEventListener("keydown", (e) => {
  if (!state.sel || !state.lines.length || e.target.matches("input, textarea")) return;
  if (e.key === "Escape") { state.sel = null; state.trace = null; paintSelection(); updateHash(); renderInspector(); }
  if (e.key === "Enter" && !state.trace) startTrace();
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

(async function boot() {
  await loadStatus();
  const m = location.hash.match(/^#([0-9a-f]+):(.+?)(?::L(\d+)(?:-L(\d+))?)?$/);
  if (m) {
    const repos = await api("/api/repos").catch(() => []);
    const repo = repos.find((r) => r.id === m[1]);
    if (repo) {
      await openRepo(sourceOf(repo), { initialFile: false });
      const sel = m[3] ? { start: +m[3], end: +(m[4] || m[3]) } : null;
      openFile(decodeURIComponent(m[2]), sel);
      return;
    }
  }
  let last = null;
  try { last = localStorage.getItem("code-history.repo"); } catch {}
  if (last) $("#sourceInput").value = last;
  showWelcome();
})();
