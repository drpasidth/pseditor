/* PSEditor front-end (vanilla JS) */
(() => {
"use strict";

// ---------------------------------------------------------------- utilities
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const enc = encodeURIComponent;
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";

async function api(url, opts = {}) {
  const o = { method: opts.method || "GET", headers: { "X-CSRFToken": csrf() } };
  if (opts.body !== undefined) {
    o.headers["Content-Type"] = "application/json";
    o.body = JSON.stringify(opts.body);
  }
  let r;
  try {
    r = await fetch(url, o);
  } catch (e) {
    throw new Error("Failed to reach the PSEditor server. Is it running at this address?");
  }
  let data = {};
  try { data = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

function toast(msg, isErr) {
  const d = document.createElement("div");
  d.className = "toast" + (isErr ? " err" : "");
  d.textContent = msg;
  $("#toast").appendChild(d);
  setTimeout(() => d.remove(), isErr ? 5000 : 2500);
}
function writeClipboardFallback(text) {
  const active = document.activeElement;
  const inEditor = !!(active && active.closest && active.closest("#editor"));
  const start = active && active.selectionStart;
  const end = active && active.selectionEnd;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  if (inEditor && Ed.ed) Ed.ed.focus();
  else if (active && typeof active.focus === "function") {
    active.focus();
    if (typeof start === "number" && active.setSelectionRange) {
      try { active.setSelectionRange(start, end); } catch (e) { /* input may be gone */ }
    }
  }
  return ok;
}
function writeClipboard(text) {
  if (!text) return Promise.resolve(false);
  const clip = navigator.clipboard;
  if (clip && clip.writeText && window.isSecureContext) {
    return clip.writeText(text).then(() => true).catch(() => writeClipboardFallback(text));
  }
  return Promise.resolve(writeClipboardFallback(text));
}
const fail = e => toast(e.message || String(e), true);
const setStatus = t => { $("#st-left").textContent = t; };

// ---------------------------------------------------------------- state
const S = {
  ws: null,
  agents: [],
  workspaces: [],
  platform: "",
  termEnabled: true,
  pty: false,          // server can run a real terminal (pywinpty / pty)
  ocrEndpoint: "",     // PSEDITOR_OCR_ENDPOINT, empty when OCR is off
  tree: {},            // dir path -> entries
  expanded: new Set(),
  tabs: [],
  active: null,
  session: null,
  sessions: [],
  cwd: "",
  abort: null,
};
const sep = () => (S.platform === "win32" ? "\\" : "/");

// ---------------------------------------------------------------- modal
function openModal(title, body, buttons = []) {
  const m = $("#modal");
  m.innerHTML = `<div class="m-head"><span>${esc(title)}</span><button id="m-x">&times;</button></div><div class="m-body"></div><div class="m-foot"></div>`;
  $(".m-body", m).appendChild(body);
  const foot = $(".m-foot", m);
  buttons.forEach(b => {
    const el = document.createElement("button");
    el.textContent = b.label;
    if (b.primary) el.className = "primary";
    el.onclick = b.onclick;
    foot.appendChild(el);
  });
  if (!buttons.length) foot.remove();
  $("#m-x").onclick = closeModal;
  $("#modal-bg").classList.remove("hidden");
}
function closeModal() { $("#modal-bg").classList.add("hidden"); $("#modal").innerHTML = ""; }
$("#modal-bg").addEventListener("mousedown", e => { if (e.target.id === "modal-bg") closeModal(); });
const modalOpen = () => !$("#modal-bg").classList.contains("hidden");

// ---------------------------------------------------------------- editor
const Ed = { monaco: null, ed: null, ta: null };

function initMonaco() {
  return new Promise(resolve => {
    if (typeof window.require === "undefined" || !window.require.config) return resolve(false);
    const base = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min";
    window.require.config({ paths: { vs: base + "/vs" } });
    window.MonacoEnvironment = {
      getWorkerUrl: () => "data:text/javascript;charset=utf-8," + encodeURIComponent(
        `self.MonacoEnvironment={baseUrl:'${base}/'};importScripts('${base}/vs/base/worker/workerMain.js');`),
    };
    const timer = setTimeout(() => resolve(false), 15000);
    window.require(["vs/editor/editor.main"], () => { clearTimeout(timer); resolve(true); },
      () => { clearTimeout(timer); resolve(false); });
  });
}

async function setupEditor() {
  const host = $("#editor");
  const ok = await initMonaco();
  if (ok) {
    Ed.monaco = window.monaco;
    Ed.ed = Ed.monaco.editor.create(host, {
      theme: "vs-dark", automaticLayout: true, fontSize: 14, minimap: { enabled: true },
      scrollBeyondLastLine: false, model: null, tabSize: 4, renderWhitespace: "selection",
    });
    const M = Ed.monaco;
    Ed.ed.addCommand(M.KeyMod.CtrlCmd | M.KeyCode.KeyS, () => saveActive());
    Ed.ed.addCommand(M.KeyMod.CtrlCmd | M.KeyCode.KeyP, () => quickOpen());
    Ed.ed.addCommand(M.KeyMod.CtrlCmd | M.KeyCode.KeyL, () => $("#prompt").focus());
    Ed.ed.addCommand(M.KeyMod.CtrlCmd | M.KeyCode.Backquote, () => toggleTerminal());
    // The desktop window does not run the browser's copy command, so copy the selection ourselves.
    Ed.ed.addCommand(M.KeyMod.CtrlCmd | M.KeyCode.KeyC, () => {
      const sel = Ed.ed.getSelection();
      const model = Ed.ed.getModel();
      if (!sel || !model || sel.isEmpty()) return;
      const text = model.getValueInRange(sel);
      writeClipboard(text).then(ok => { if (!ok) toast("Could not copy", true); Ed.ed.focus(); });
    });
  } else {
    toast("Monaco could not load (offline?). Using plain text editor.", true);
    const ta = document.createElement("textarea");
    ta.className = "fallback";
    ta.spellcheck = false;
    ta.addEventListener("input", () => {
      const t = S.active;
      if (t) { t.value = ta.value; t.dirty = t.value !== t.saved; renderTabs(); }
    });
    ta.addEventListener("keydown", e => {
      if (e.key === "Tab") {
        e.preventDefault();
        const s = ta.selectionStart;
        ta.setRangeText("    ", s, ta.selectionEnd, "end");
        ta.dispatchEvent(new Event("input"));
      }
    });
    host.appendChild(ta);
    Ed.ta = ta;
  }
}

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|bmp)$/i;
const isImagePath = path => IMAGE_EXT.test(path || "");
const tabValue = t => {
  if (!t || t.kind === "image") return "";
  return Ed.monaco ? t.model.getValue() : t.value;
};

function showImage(t) {
  const view = $("#image-view");
  const img = $("#image-view-img");
  const meta = $("#image-view-meta");
  if (!t || t.kind !== "image") {
    view.classList.add("hidden");
    img.removeAttribute("src");
    $("#editor").classList.remove("hidden");
    if (Ed.monaco && Ed.ed) requestAnimationFrame(() => Ed.ed.layout());
    return;
  }
  $("#editor").classList.add("hidden");
  view.classList.remove("hidden");
  const ocrBox = $("#image-ocr");
  ocrBox.textContent = "";
  ocrBox.classList.add("hidden");
  const ocrBtn = $("#image-ocr-btn");
  ocrBtn.classList.toggle("hidden", !S.ocrEndpoint);
  ocrBtn.title = /opentyphoon\.ai/i.test(S.ocrEndpoint || "")
    ? "Read Thai text with Typhoon OCR"
    : "Send this image to the OCR endpoint in settings";
  const name = t.path.split("/").pop();
  meta.textContent = name;
  img.alt = name;
  img.onload = () => {
    meta.textContent = `${name}  ·  ${img.naturalWidth}×${img.naturalHeight}`;
  };
  img.onerror = () => {
    meta.textContent = `${name}  ·  could not read this image`;
  };
  img.src = `/api/ws/${S.ws.id}/preview?path=${enc(t.path)}&v=${t.rev || 0}`;
}

function getSelection() {
  if (!S.active || S.active.kind === "image") return "";
  if (Ed.monaco) {
    const sel = Ed.ed.getSelection();
    return sel && !sel.isEmpty() ? S.active.model.getValueInRange(sel) : "";
  }
  return Ed.ta.value.substring(Ed.ta.selectionStart, Ed.ta.selectionEnd);
}

// ---------------------------------------------------------------- tabs
function renderTabs() {
  const box = $("#tabs");
  box.innerHTML = "";
  S.tabs.forEach(t => {
    const el = document.createElement("div");
    el.className = "tab" + (t === S.active ? " active" : "");
    el.title = t.path;
    el.innerHTML = `<span>${esc(t.path.split("/").pop())}</span>${t.dirty ? '<span class="dot">●</span>' : ""}<span class="x">&times;</span>`;
    el.onclick = e => {
      if (e.target.classList.contains("x")) closeTab(t); else activate(t);
    };
    el.onauxclick = e => { if (e.button === 1) closeTab(t); };
    box.appendChild(el);
  });
  $("#welcome").classList.toggle("hidden", S.tabs.length > 0);
  $("#st-right").textContent = S.active ? S.active.path : "";
  $$(".node").forEach(n => n.classList.toggle("active", !!S.active && n.dataset.path === S.active.path));
}

function activate(t, line) {
  S.active = t;
  if (t.kind === "image") {
    if (Ed.monaco) Ed.ed.setModel(null);
    showImage(t);
    renderTabs();
    return;
  }
  showImage(null);
  if (Ed.monaco) {
    Ed.ed.setModel(t.model);
    if (line) { Ed.ed.revealLineInCenter(line); Ed.ed.setPosition({ lineNumber: line, column: 1 }); }
    Ed.ed.focus();
  } else {
    Ed.ta.value = t.value;
    Ed.ta.style.display = "";
  }
  renderTabs();
}

async function openFile(path, line) {
  let t = S.tabs.find(x => x.path === path);
  if (!t) {
    if (isImagePath(path)) {
      t = { path, kind: "image", dirty: false, rev: Date.now() };
    } else {
      let r;
      try { r = await api(`/api/ws/${S.ws.id}/file?path=${enc(path)}`); } catch (e) { return fail(e); }
      t = { path, saved: r.content, dirty: false };
      if (Ed.monaco) {
        t.model = Ed.monaco.editor.createModel(r.content, undefined, Ed.monaco.Uri.file("/" + path));
        t.model.onDidChangeContent(() => {
          const d = t.model.getValue() !== t.saved;
          if (d !== t.dirty) { t.dirty = d; renderTabs(); }
        });
      } else {
        t.value = r.content;
      }
    }
    S.tabs.push(t);
  }
  activate(t, line);
}

function closeTab(t, force) {
  if (t.dirty && !force && !confirm(`Discard unsaved changes to ${t.path}?`)) return;
  const i = S.tabs.indexOf(t);
  S.tabs.splice(i, 1);
  if (t.model) t.model.dispose();
  if (S.active === t) {
    S.active = S.tabs[Math.min(i, S.tabs.length - 1)] || null;
    if (S.active) activate(S.active);
    else {
      if (Ed.monaco) Ed.ed.setModel(null); else if (Ed.ta) Ed.ta.value = "";
      showImage(null);
    }
  }
  renderTabs();
}

function closeAllTabs() {
  [...S.tabs].forEach(t => closeTab(t, true));
}

async function saveTab(t) {
  if (!t || t.kind === "image") return;
  const value = tabValue(t);
  const r = await api(`/api/ws/${S.ws.id}/file`, { method: "PUT", body: { path: t.path, content: value } });
  t.saved = value;
  t.dirty = false;
  renderTabs();
  return r;
}
async function saveActive() {
  if (!S.active || S.active.kind === "image") return;
  try {
    const r = await saveTab(S.active);
    setStatus("Saved to disk: " + (r.abs || S.active.path));
    toast("Saved to " + (r.abs || S.active.path));
  } catch (e) { fail(e); }
}
async function saveAll() {
  for (const t of S.tabs.filter(x => x.dirty)) await saveTab(t);
}
function openPreview(path) {
  if (!S.ws || !path) return;
  const url = `/api/ws/${S.ws.id}/preview?path=${enc(path)}`;
  // In the desktop app, open a native child window instead of an external browser.
  const desk = window.pywebview && window.pywebview.api && window.pywebview.api.open_window;
  if (desk) desk(url); else window.open(url, "_blank");
}

/** Reload open tabs after files changed on disk (agent, terminal, revert). */
async function onFilesChanged(paths) {
  for (const p of paths) {
    const t = S.tabs.find(x => x.path === p);
    if (!t) continue;
    if (t.kind === "image") {
      t.rev = Date.now();
      if (S.active === t) showImage(t);
      continue;
    }
    let r;
    try { r = await api(`/api/ws/${S.ws.id}/file?path=${enc(p)}`); } catch (e) { closeTab(t, true); continue; }
    if (t.dirty) { toast(`${p} changed on disk but has unsaved edits`, true); continue; }
    if (r.content !== t.saved) {
      t.saved = r.content;
      if (Ed.monaco) t.model.setValue(r.content);
      else { t.value = r.content; if (S.active === t) Ed.ta.value = r.content; }
      t.dirty = false;
    }
  }
  renderTabs();
  await refreshTree();
}

window.addEventListener("beforeunload", e => {
  if (S.tabs.some(t => t.dirty)) { e.preventDefault(); e.returnValue = ""; }
});

// ---------------------------------------------------------------- explorer
async function loadDir(p) {
  const r = await api(`/api/ws/${S.ws.id}/tree?path=${enc(p)}`);
  S.tree[p] = r.entries;
}
async function refreshTree() {
  if (!S.ws) return;
  const dirs = ["", ...S.expanded];
  for (const d of dirs) {
    try { await loadDir(d); } catch (e) { S.expanded.delete(d); delete S.tree[d]; }
  }
  renderTree();
}

function renderTree() {
  const root = $("#tree");
  root.innerHTML = "";
  if (!S.ws) { root.innerHTML = '<div class="hint" style="padding:8px">No folder open.</div>'; return; }
  const build = (dir, depth) => {
    (S.tree[dir] || []).forEach(e => {
      const n = document.createElement("div");
      n.className = `node ${e.type}` + (e.ignored ? " ignored" : "") + (e.root ? " root" : "");
      n.dataset.path = e.path;
      n.dataset.type = e.type;
      n.style.paddingLeft = 4 + depth * 14 + "px";
      const open = S.expanded.has(e.path);
      n.innerHTML = `<span class="ic">${e.type === "dir" ? (open ? "▾" : "▸") : "·"}</span><span class="nm">${esc(e.name)}</span>`;
      n.onclick = () => {
        if (e.type === "dir") return toggleDir(e.path);
        // HTML apps need the preview server to write to disk — open that by default.
        if (/\.(html?|htm)$/i.test(e.name)) return openPreview(e.path);
        return openFile(e.path);
      };
      n.oncontextmenu = ev => { ev.preventDefault(); ev.stopPropagation(); showMenu(ev, e); };
      root.appendChild(n);
      if (e.type === "dir" && open) build(e.path, depth + 1);
    });
  };
  build("", 0);
  renderTabs();
}

async function toggleDir(p) {
  if (S.expanded.has(p)) S.expanded.delete(p);
  else {
    try { await loadDir(p); } catch (e) { return fail(e); }
    S.expanded.add(p);
  }
  renderTree();
}

const parentOf = p => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
const joinPath = (a, b) => (a ? a + "/" + b : b);

function showMenu(ev, entry) {
  const items = [];
  const dir = entry ? (entry.type === "dir" ? entry.path : parentOf(entry.path)) : "";
  if (entry && entry.root) {
    items.push(["Remove from workspace", () => removeRootFolder(entry)]);
    items.push(["Copy path", () => {
      const f = (S.ws.folders || []).find(x => x.name === entry.path);
      if (f && navigator.clipboard) navigator.clipboard.writeText(f.path);
    }]);
  } else {
    items.push(["New file", () => createEntry(dir, "file")]);
    items.push(["New folder", () => createEntry(dir, "dir")]);
    if (entry) {
      if (entry.type === "file" && /\.(html?|htm)$/i.test(entry.name)) {
        items.push(["Open preview (writes to disk)", () => openPreview(entry.path)]);
        items.push(["Edit source", () => openFile(entry.path)]);
      }
      items.push(["Rename", () => renameEntry(entry)]);
      items.push(["Delete", () => deleteEntry(entry)]);
      items.push(["Copy relative path", () => navigator.clipboard && navigator.clipboard.writeText(entry.path)]);
    }
  }
  if (!entry) {
    items.push(["Add folder to workspace…", () => folderPicker("add")]);
  }
  const m = $("#ctxmenu");
  m.innerHTML = "";
  items.forEach(([label, fn]) => {
    const d = document.createElement("div");
    d.textContent = label;
    d.onclick = () => { hideMenu(); fn(); };
    m.appendChild(d);
  });
  m.style.left = Math.min(ev.clientX, innerWidth - 170) + "px";
  m.style.top = Math.min(ev.clientY, innerHeight - 160) + "px";
  m.classList.remove("hidden");
}
function hideMenu() { $("#ctxmenu").classList.add("hidden"); }
document.addEventListener("click", hideMenu);
$("#tree").addEventListener("contextmenu", e => { e.preventDefault(); showMenu(e, null); });

async function createEntry(dir, type) {
  if (!S.ws) return toast("Open a folder first", true);
  const name = prompt(`New ${type === "dir" ? "folder" : "file"} name` + (dir ? ` in ${dir}` : ""));
  if (!name) return;
  const path = joinPath(dir, name.trim());
  try {
    await api(`/api/ws/${S.ws.id}/file`, { method: "POST", body: { path, type } });
    if (dir) S.expanded.add(dir);
    await refreshTree();
    if (type === "file") openFile(path);
  } catch (e) { fail(e); }
}
async function renameEntry(entry) {
  const name = prompt("Rename to", entry.name);
  if (!name || name === entry.name) return;
  const np = joinPath(parentOf(entry.path), name.trim());
  try {
    await api(`/api/ws/${S.ws.id}/file`, { method: "PATCH", body: { path: entry.path, new_path: np } });
    S.tabs.filter(t => t.path === entry.path || t.path.startsWith(entry.path + "/")).forEach(t => closeTab(t, true));
    await refreshTree();
  } catch (e) { fail(e); }
}
async function deleteEntry(entry) {
  if (!confirm(`Delete ${entry.path}? This cannot be undone.`)) return;
  try {
    await api(`/api/ws/${S.ws.id}/file`, { method: "DELETE", body: { path: entry.path } });
    S.tabs.filter(t => t.path === entry.path || t.path.startsWith(entry.path + "/")).forEach(t => closeTab(t, true));
    await refreshTree();
  } catch (e) { fail(e); }
}

$("#new-file").onclick = () => createEntry(selectedDir(), "file");
$("#new-folder").onclick = () => createEntry(selectedDir(), "dir");
$("#add-root").onclick = () => folderPicker("add");
$("#refresh").onclick = () => refreshTree();
function selectedDir() {
  if (!S.active) {
    const folders = (S.ws && S.ws.folders) || [];
    return folders.length > 1 ? folders[0].name : "";
  }
  return parentOf(S.active.path);
}

async function removeRootFolder(entry) {
  if (!S.ws) return;
  if (((S.ws.folders || []).length || 1) <= 1) return toast("Cannot remove the last folder", true);
  if (!confirm(`Remove “${entry.name}” from this workspace?\n(Files on disk are not deleted.)`)) return;
  try {
    await saveAll();
    const w = await api(`/api/workspaces/${S.ws.id}/folders`, {
      method: "DELETE", body: { name: entry.path },
    });
    S.workspaces = S.workspaces.map(x => x.id === w.id ? w : x);
    closeAllTabs();
    S.tree = {}; S.expanded = new Set();
    await openWorkspace(w);
  } catch (e) { fail(e); }
}

// ---------------------------------------------------------------- sidebar tabs / search
$$(".side-tab").forEach(b => b.onclick = () => {
  $$(".side-tab").forEach(x => x.classList.toggle("active", x === b));
  $("#pane-explorer").classList.toggle("hidden", b.dataset.pane !== "explorer");
  $("#pane-search").classList.toggle("hidden", b.dataset.pane !== "search");
  if (b.dataset.pane === "search") $("#search-q").focus();
});

async function runSearch() {
  if (!S.ws) return;
  const q = $("#search-q").value;
  const box = $("#search-results");
  if (!q) { box.innerHTML = ""; return; }
  box.innerHTML = '<div class="hint">Searching…</div>';
  try {
    const r = await api(`/api/ws/${S.ws.id}/search`, { method: "POST", body: {
      query: q, regex: $("#search-regex").checked, case: $("#search-case").checked, glob: $("#search-glob").value.trim(),
    } });
    box.innerHTML = r.results.length ? "" : '<div class="hint">No results.</div>';
    let last = null;
    r.results.forEach(h => {
      if (h.path !== last) {
        const f = document.createElement("div");
        f.className = "hit-file"; f.textContent = h.path; box.appendChild(f); last = h.path;
      }
      const d = document.createElement("div");
      d.className = "hit";
      d.innerHTML = `<span class="l">${h.line}:</span> ${esc(h.text)}`;
      d.onclick = () => openFile(h.path, h.line);
      box.appendChild(d);
    });
  } catch (e) { box.innerHTML = ""; fail(e); }
}
$("#search-q").addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });

// ---------------------------------------------------------------- quick open
async function quickOpen() {
  if (!S.ws) return toast("Open a folder first", true);
  let files = [];
  try { files = (await api(`/api/ws/${S.ws.id}/files`)).files; } catch (e) { return fail(e); }
  const body = document.createElement("div");
  body.innerHTML = '<input class="quick" style="width:100%" placeholder="Type to search files…"><div class="dirlist"></div>';
  const input = $("input", body), list = $(".dirlist", body);
  let shown = [], sel = 0;
  const render = () => {
    const q = input.value.toLowerCase().replace(/\s+/g, "");
    shown = files.filter(f => { let i = 0; const l = f.toLowerCase(); for (const c of q) { i = l.indexOf(c, i); if (i < 0) return false; i++; } return true; })
      .sort((a, b) => (a.toLowerCase().includes(q) ? 0 : 1) - (b.toLowerCase().includes(q) ? 0 : 1) || a.length - b.length).slice(0, 80);
    sel = Math.min(sel, Math.max(shown.length - 1, 0));
    list.innerHTML = "";
    shown.forEach((f, i) => {
      const d = document.createElement("div");
      d.textContent = f;
      if (i === sel) d.style.background = "var(--accent2)";
      d.onclick = () => { closeModal(); openFile(f); };
      list.appendChild(d);
    });
  };
  input.oninput = () => { sel = 0; render(); };
  input.onkeydown = e => {
    if (e.key === "ArrowDown") { sel = Math.min(sel + 1, shown.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter" && shown[sel]) { closeModal(); openFile(shown[sel]); }
    else if (e.key === "Escape") closeModal();
  };
  openModal("Go to file", body);
  render();
  input.focus();
}

// ---------------------------------------------------------------- terminal
const termHistory = []; let termIdx = 0;
function toggleTerminal(show) {
  const t = $("#terminal");
  const want = show === undefined ? t.classList.contains("hidden") : show;
  t.classList.toggle("hidden", !want);
  $("#rz-term").classList.toggle("hidden", !want);
  if (!want) return;
  if (ptyMode()) { ptyAttach(); return; }
  updatePrompt(); loadTermState(); $("#term-in").focus();
}
// The terminal is a persistent real shell (PowerShell / bash): the server tells us the prompt,
// e.g. "(venv) C:\projects\app>", after every command.
function updatePrompt() {
  const fallback = S.ws ? (S.ws.path || S.ws.name) + ">" : ">";
  $("#term-prompt").textContent = S.termPrompt || fallback;
}
async function loadTermState() {
  if (ptyMode() || !S.ws || S.termPrompt || $("#terminal").classList.contains("hidden")) return;
  const wsId = S.ws.id;
  try {
    const r = await api(`/api/ws/${wsId}/terminal`, { method: "POST", body: {} });
    if (S.ws && S.ws.id === wsId) { S.termPrompt = r.prompt; updatePrompt(); }
  } catch (err) { termPrint(err.message + "\n", "bad"); }
}
function termPrint(text, cls) {
  const out = $("#term-out");
  const s = document.createElement("span");
  if (cls) s.className = cls;
  s.textContent = text;
  out.appendChild(s);
  out.scrollTop = out.scrollHeight;
}
let termBusy = false;
$("#term-in").addEventListener("keydown", async e => {
  const input = e.target;
  if (e.ctrlKey && (e.key === "c" || e.key === "C") && !(input.selectionStart !== input.selectionEnd)) {
    e.preventDefault();  // Ctrl+C: stop the running command (the shell restarts), or just clear the line
    if (termBusy && S.ws) {
      api(`/api/ws/${S.ws.id}/terminal`, { method: "POST", body: { interrupt: true } }).catch(() => {});
    } else {
      if (input.value) termPrint(`${$("#term-prompt").textContent} ${input.value}^C\n`, "cmd");
      input.value = "";
    }
    return;
  }
  if (e.key === "ArrowUp") { if (termIdx > 0) input.value = termHistory[--termIdx]; e.preventDefault(); return; }
  if (e.key === "ArrowDown") { input.value = termIdx < termHistory.length - 1 ? termHistory[++termIdx] : ""; termIdx = Math.min(termIdx + 1, termHistory.length); e.preventDefault(); return; }
  if (e.key !== "Enter") return;
  if (termBusy) return;  // one command at a time; Ctrl+C stops the current one
  const cmd = input.value.trim();
  input.value = "";
  if (!cmd) return;
  termHistory.push(cmd); termIdx = termHistory.length;
  if (cmd === "cls" || cmd === "clear") { $("#term-out").innerHTML = ""; return; }
  if (!S.ws) return termPrint("Open a folder first.\n", "bad");
  const wsId = S.ws.id;
  termPrint(`${$("#term-prompt").textContent} ${cmd}\n`, "cmd");
  termBusy = true;
  input.classList.add("busy");
  input.placeholder = "running… press Ctrl+C to stop";
  try {
    const r = await api(`/api/ws/${wsId}/terminal`, { method: "POST", body: { command: cmd } });
    if (r.output) termPrint(r.output.endsWith("\n") ? r.output : r.output + "\n", r.exit_code ? "bad" : "");
    if (S.ws && S.ws.id === wsId) { S.termPrompt = r.prompt; updatePrompt(); }
    refreshTree();
  } catch (err) { termPrint(err.message + "\n", "bad"); }
  termBusy = false;
  input.classList.remove("busy");
  input.placeholder = "";
  input.focus();
});

// ---- real terminal: xterm.js in the browser, a pseudo-terminal (ConPTY / pty) on the server.
// Falls back to the line-based terminal above when the server has no PTY or xterm.js did not load.
const T = {
  xterm: null, fit: null, wsId: null, sid: null, since: 0, gen: 0,
  running: false, exited: false, first: true, muted: false,
  pending: "", sending: false, dirty: false, lastOut: 0, refreshTimer: null,
  sentCols: 0, sentRows: 0, resizeTimer: null,
};
const ptyMode = () => !!(S.pty && window.Terminal && window.FitAddon);
const sleep = ms => new Promise(r => setTimeout(r, ms));

function createXterm() {
  T.xterm = new Terminal({
    fontFamily: "Consolas, 'Cascadia Mono', 'Courier New', monospace", fontSize: 13,
    cursorBlink: true, scrollback: 5000,
    theme: { background: "#181818", foreground: "#d4d4d4", cursor: "#d4d4d4", selectionBackground: "#264f78" },
  });
  T.fit = new FitAddon.FitAddon();
  T.xterm.loadAddon(T.fit);
  T.xterm.open($("#term-xterm"));
  T.xterm.onData(ptySend);
  T.xterm.attachCustomKeyEventHandler(e => {
    if (e.type !== "keydown") return true;
    if (e.ctrlKey && e.key === "`") return false;  // let the page toggle the terminal
    const copy = e.ctrlKey && e.key.toLowerCase() === "c" && (e.shiftKey || T.xterm.hasSelection());
    if (copy) {  // Ctrl+C copies when text is selected, otherwise it is a real Ctrl+C for the shell
      if (T.xterm.hasSelection()) writeClipboard(T.xterm.getSelection());
      return false;
    }
    return true;
  });
  new ResizeObserver(() => ptyFit()).observe($("#term-xterm"));
}

function ptyFit() {
  if (!T.xterm || $("#terminal").classList.contains("hidden") || $("#term-xterm").classList.contains("hidden")) return;
  try { T.fit.fit(); } catch (e) { return; }
  clearTimeout(T.resizeTimer);
  T.resizeTimer = setTimeout(() => {
    const { cols, rows } = T.xterm;
    if (!T.running || (cols === T.sentCols && rows === T.sentRows)) return;
    T.sentCols = cols; T.sentRows = rows;
    api(`/api/ws/${T.wsId}/pty/resize`, { method: "POST", body: { sid: T.sid, cols, rows } }).catch(() => {});
  }, 120);
}

async function ptyAttach() {
  if (!S.ws) { toast("Open a folder first", true); return; }
  $("#term-out").classList.add("hidden");
  $("#term-line").classList.add("hidden");
  $("#term-xterm").classList.remove("hidden");
  if (!T.xterm) createXterm();
  requestAnimationFrame(() => { ptyFit(); T.xterm.focus(); });
  if (T.running && T.wsId === S.ws.id) return;
  await ptyStart(false);
}

/** Drop the connection to the current workspace's terminal (the server-side shell keeps running). */
function ptyDetach() {
  T.gen++;
  T.running = false; T.sid = null; T.wsId = null; T.pending = ""; T.sending = false;
  if (T.xterm) T.xterm.reset();
}

async function ptyStart(restart) {
  if (!S.ws) return;
  const wsId = S.ws.id, gen = ++T.gen;
  T.running = false; T.pending = ""; T.sending = false;
  T.xterm.reset();
  try { T.fit.fit(); } catch (e) { /* hidden */ }
  try {
    const r = await api(`/api/ws/${wsId}/pty/open`, {
      method: "POST", body: { cols: T.xterm.cols, rows: T.xterm.rows, restart: !!restart },
    });
    if (gen !== T.gen) return;
    Object.assign(T, { wsId, sid: r.sid, since: 0, exited: false, running: true, first: true,
                       sentCols: T.xterm.cols, sentRows: T.xterm.rows });
    ptyPoll(gen);
    T.xterm.focus();
  } catch (e) {
    T.xterm.writeln("\x1b[31m" + e.message + "\x1b[0m");
  }
}

async function ptyPoll(gen) {
  let failures = 0;
  while (gen === T.gen) {
    let r;
    try {
      r = await api(`/api/ws/${T.wsId}/pty/read?sid=${T.sid}&since=${T.since}&wait=20`);
      failures = 0;
    } catch (e) {
      if (gen !== T.gen) return;
      if (/409/.test(e.message)) { ptyStart(false); return; }   // the server restarted: open a new session
      if (++failures > 30) { T.running = false; T.xterm.writeln("\r\n\x1b[31m[lost connection to the server]\x1b[0m"); return; }
      await sleep(1000);
      continue;
    }
    if (gen !== T.gen) return;
    if (r.reset) T.xterm.reset();
    if (r.data) {
      if (T.first) {
        // Replaying old output makes xterm answer old terminal queries; keep those replies out of the shell.
        T.muted = true;
        T.xterm.write(r.data, () => { T.muted = false; });
      } else {
        T.xterm.write(r.data);
      }
      T.lastOut = Date.now();
      if (T.dirty) scheduleTreeRefresh();
    }
    T.first = false;
    T.since = r.end;
    if (!r.alive) { T.exited = true; T.running = false; return; }
  }
}

function ptySend(data) {
  if (T.muted) return;
  if (T.exited) { ptyStart(false); return; }   // any key after the shell exited starts a new one
  if (!T.running) return;
  if (data.includes("\r")) T.dirty = true;      // a command was probably run: refresh the file tree afterwards
  T.pending += data;
  if (T.sending) return;
  T.sending = true;
  const gen = T.gen;
  (async () => {
    while (gen === T.gen && T.pending) {
      const chunk = T.pending; T.pending = "";
      try {
        await api(`/api/ws/${T.wsId}/pty/input`, { method: "POST", body: { sid: T.sid, data: chunk } });
      } catch (e) { /* a 409 is handled by the read loop */ }
    }
    if (gen === T.gen) T.sending = false;
  })();
}

function scheduleTreeRefresh() {
  if (T.refreshTimer) return;
  T.refreshTimer = setTimeout(() => {
    T.refreshTimer = null;
    if (S.ws) refreshTree().catch(() => {});
    if (Date.now() - T.lastOut > 1500) T.dirty = false;   // output has settled
    else if (T.dirty) scheduleTreeRefresh();
  }, 2000);
}

// ---------------------------------------------------------------- chat
function md(text) {
  return text.split("```").map((part, i) => {
    if (i % 2 === 1) {
      const nl = part.indexOf("\n");
      const code = nl >= 0 ? part.slice(nl + 1) : part;
      return `<pre><button class="copy-btn">copy</button><code>${esc(code.replace(/\n$/, ""))}</code></pre>`;
    }
    return esc(part)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
      .replace(/\n/g, "<br>");
  }).join("");
}
$("#messages").addEventListener("click", e => {
  if (e.target.classList.contains("copy-btn")) {
    const btn = e.target;
    writeClipboard($("code", btn.parentElement).textContent).then(ok => {
      btn.textContent = ok ? "copied" : "failed";
      setTimeout(() => (btn.textContent = "copy"), 1200);
    });
  }
});

// Sent prompts can be copied or run again. Up/Down in the box walks the same list.
const promptHistory = [];
let promptHist = 0;
let promptDraft = "";
function rememberPrompt(text) {
  const t = String(text || "").trim();
  if (!t) return;
  if (promptHistory[promptHistory.length - 1] !== t) promptHistory.push(t);
  if (promptHistory.length > 50) promptHistory.shift();
  promptHist = promptHistory.length;
  promptDraft = "";
}
function redoPrompt(text) {
  if ($("#prompt").disabled) return toast("Wait until the agent finishes", true);
  $("#prompt").value = text;
  $("#prompt").focus();
  sendChat();
}
function promptActions(text, attachments) {
  const row = document.createElement("div");
  row.className = "msg-actions";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "Copy";
  copy.title = "Copy this prompt";
  copy.onclick = () => writeClipboard(text).then(ok => toast(ok ? "Prompt copied" : "Could not copy", !ok));
  const redo = document.createElement("button");
  redo.type = "button";
  redo.className = "redo-prompt";
  redo.textContent = "Redo";
  redo.title = attachments && attachments.length
    ? "Run this prompt again. Attached files are not included."
    : "Run this prompt again";
  redo.onclick = () => redoPrompt(text);
  row.append(copy, redo);
  return row;
}

const msgBox = () => $("#messages");
function scrollMsgs() { const m = msgBox(); m.scrollTop = m.scrollHeight; }
const ATTACH_KIND = { image: "IMG", pdf: "PDF", excel: "XLS", word: "DOC" };
function msgFiles(list) {
  if (!list || !list.length) return null;
  const row = document.createElement("div");
  row.className = "msg-files";
  list.forEach(a => {
    const chip = document.createElement("span");
    chip.className = "msg-file";
    chip.title = a.name || "";
    if (a.kind === "image" && a.data && a.mime) {
      const img = document.createElement("img");
      img.src = `data:${a.mime};base64,${a.data}`;
      img.alt = a.name || "image";
      chip.appendChild(img);
    } else {
      const badge = document.createElement("span");
      badge.className = "attach-kind";
      badge.textContent = ATTACH_KIND[a.kind] || "FILE";
      chip.appendChild(badge);
    }
    const name = document.createElement("span");
    name.className = "attach-name";
    name.textContent = a.name || "file";
    chip.appendChild(name);
    row.appendChild(chip);
  });
  return row;
}
function addThinking(text) {
  if (!text) return;
  const d = document.createElement("details");
  d.className = "thinking";
  d.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "Thinking";
  const pre = document.createElement("pre");
  pre.textContent = text;
  d.append(summary, pre);
  msgBox().appendChild(d);
  scrollMsgs();
}
function addBubble(role, text, attachments) {
  const d = document.createElement("div");
  d.className = "msg " + role;
  if (role === "user") {
    const files = msgFiles(attachments);
    if (files) d.appendChild(files);
    if (text) {
      const t = document.createElement("div");
      t.className = "msg-text";
      t.textContent = text;
      d.appendChild(t);
    }
    if (text && text.trim()) d.appendChild(promptActions(text, attachments));
  } else d.innerHTML = md(text);
  msgBox().appendChild(d);
  scrollMsgs();
  return d;
}
function toolSummary(name, args) {
  if (!args) return "";
  if (args.path) return args.path;
  if (args.command) return args.command;
  if (args.query) return args.query;
  if (args.pattern) return args.pattern;
  return "";
}
function addToolCard(id, name, args) {
  const d = document.createElement("details");
  d.className = "tool";
  d.dataset.id = id;
  d.innerHTML = `<summary>🔧 <b>${esc(name)}</b> ${esc(toolSummary(name, args)).slice(0, 80)} <span class="st">…</span></summary><pre></pre>`;
  msgBox().appendChild(d);
  scrollMsgs();
  return d;
}
function fillToolCard(id, ok, text) {
  const d = $(`.tool[data-id="${CSS.escape(id)}"]`, msgBox());
  if (!d) return;
  $(".st", d).innerHTML = ok === null ? "" : ok ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>';
  $("pre", d).textContent = text || "";
}
// ---- human-in-the-loop: every agent action that changes something waits here for a click
function approvalPreview(name, a) {
  a = a || {};
  const clip = (s, n) => (s.length > n ? s.slice(0, n) + `\n… (${s.length - n} more characters)` : s);
  const lines = (s, mark) => String(s).split("\n").map(l => mark + l).join("\n");
  switch (name) {
    case "write_file":
      return `${a.path}  (${(a.content || "").length} characters; replaces the file if it exists)\n\n` + clip(String(a.content || ""), 6000);
    case "edit_file":
      return `${a.path}${a.replace_all ? "  (all occurrences)" : ""}\n\n` +
        clip(lines(a.old_string || "", "- "), 3000) + "\n" + clip(lines(a.new_string || "", "+ "), 3000);
    case "delete_file": return `Delete ${a.path}`;
    case "run_command": return `${a.cwd ? `[${a.cwd}] ` : ""}$ ${a.command}`;
    case "cli_agent": return `${a.command}\n\nIt will receive this prompt and may change files by itself:\n\n` + clip(String(a.prompt || ""), 2000);
    default: return clip(JSON.stringify(a, null, 2), 4000);
  }
}
const approvalTitle = {
  write_file: "Write file", edit_file: "Edit file", delete_file: "Delete",
  run_command: "Run command", cli_agent: "Run command-line agent",
};
function addApproval(ev) {
  const d = document.createElement("div");
  d.className = "approval";
  d.dataset.id = ev.id;
  d.innerHTML = `<div class="ap-title">✋ <b>${esc(approvalTitle[ev.name] || ev.name)}</b> — waiting for your approval</div>
    <pre class="ap-body"></pre>
    <input class="ap-reason" placeholder="Optional: reason, or what to do instead (sent to the agent if you deny)">
    <div class="row"><button class="primary ap-yes">Approve</button><button class="ap-no">Deny</button><span class="ap-msg"></span></div>`;
  $(".ap-body", d).textContent = approvalPreview(ev.name, ev.arguments);
  const answer = async decision => {
    $$("button, input", d).forEach(x => (x.disabled = true));
    try {
      await api(`/api/approvals/${ev.id}`, { method: "POST", body: { decision, reason: $(".ap-reason", d).value.trim() } });
    } catch (e) {
      $(".ap-msg", d).textContent = e.message;
      $$("button, input", d).forEach(x => (x.disabled = false));
    }
  };
  $(".ap-yes", d).onclick = () => answer("allow");
  $(".ap-no", d).onclick = () => answer("deny");
  msgBox().appendChild(d);
  scrollMsgs();
  $(".ap-yes", d).focus();
}
function resolveApproval(ev) {
  const d = $(`.approval[data-id="${CSS.escape(ev.id)}"]`, msgBox());
  if (!d) return;
  const reason = $(".ap-reason", d).value.trim();
  const label = { allow: "✓ Approved", deny: "✗ Denied" + (reason ? ": " + reason : ""),
                  cancelled: "Stopped", timeout: "Timed out — not run" }[ev.decision] || ev.decision;
  d.className = "approval done " + ev.decision;
  d.innerHTML = `<span>${esc(label)}</span>`;
}
function addChanges(messageId, changed) {
  if (!changed || !changed.length) return;
  const d = document.createElement("div");
  d.className = "changes";
  d.innerHTML = `<b>${changed.length} file(s) changed</b>`;
  changed.forEach(p => {
    const a = document.createElement("a");
    a.textContent = p;
    a.onclick = () => openFile(p).catch(() => {});
    d.appendChild(a);
  });
  const row = document.createElement("div");
  row.className = "row";
  const rv = document.createElement("button");
  rv.textContent = "Revert changes";
  rv.onclick = async () => {
    if (!confirm("Revert all files changed by this request to their previous content?")) return;
    try {
      const r = await api(`/api/messages/${messageId}/revert`, { method: "POST", body: {} });
      toast(`Reverted ${r.reverted.length} file(s)`);
      rv.remove();
      onFilesChanged(r.reverted);
    } catch (e) { fail(e); }
  };
  row.appendChild(rv);
  d.appendChild(row);
  msgBox().appendChild(d);
  scrollMsgs();
}

function renderSessionMessages(msgs) {
  const box = msgBox();
  box.innerHTML = "";
  if (!msgs.length) {
    box.innerHTML = '<div class="hint" style="padding:8px">Describe what you want, or attach an image, PDF, Excel or Word file. The selected agent can read, create, edit files and run commands in this workspace; you approve every change before it happens.</div>';
    return;
  }
  promptHistory.length = 0;
  promptHist = 0;
  promptDraft = "";
  let pending = null;
  const flush = () => { if (pending) { addChanges(pending.id, pending.changed); pending = null; } };
  msgs.forEach(m => {
    if (m.role === "user") {
      flush();
      rememberPrompt(m.text);
      addBubble("user", m.text, m.attachments);
      pending = { id: m.id, changed: m.changed };
    }
    else if (m.role === "assistant") {
      if (m.thinking) addThinking(m.thinking);
      (m.tool_calls || []).forEach(tc => addToolCard(tc.id, tc.name, tc.arguments));
      if (m.text) addBubble("assistant", m.text);
    } else if (m.role === "tool") fillToolCard(m.tool_call_id, !/^(Error:|The user denied)/.test(m.text), m.text);
  });
  flush();
}

async function loadSessions(selectId) {
  const r = await api(`/api/ws/${S.ws.id}/sessions`);
  S.sessions = r.sessions;
  if (!S.sessions.length) {
    const s = await api(`/api/ws/${S.ws.id}/sessions`, { method: "POST", body: {} });
    S.sessions = [s];
  }
  fillSessionSelect();
  const id = selectId || (S.session && S.sessions.find(s => s.id === S.session.id) ? S.session.id : S.sessions[0].id);
  await selectSession(id);
}
function fillSessionSelect() {
  const sel = $("#session-select");
  sel.innerHTML = S.sessions.map(s => `<option value="${s.id}">${esc(s.title)}</option>`).join("");
  if (S.session) sel.value = S.session.id;
}
async function selectSession(id) {
  const r = await api(`/api/sessions/${id}`);
  S.session = r.session;
  $("#session-select").value = id;
  renderSessionMessages(r.messages);
}
$("#session-select").onchange = e => selectSession(+e.target.value).catch(fail);
$("#chat-new").onclick = async () => {
  if (!S.ws) return toast("Open a folder first", true);
  try {
    const s = await api(`/api/ws/${S.ws.id}/sessions`, { method: "POST", body: {} });
    await loadSessions(s.id);
    $("#prompt").focus();
  } catch (e) { fail(e); }
};
$("#chat-del").onclick = async () => {
  if (!S.session || !confirm("Delete this conversation? (Files stay as they are.)")) return;
  try { await api(`/api/sessions/${S.session.id}`, { method: "DELETE" }); S.session = null; await loadSessions(); } catch (e) { fail(e); }
};

function fillAgentSelect() {
  const sel = $("#agent-select");
  sel.innerHTML = S.agents.length
    ? S.agents.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join("")
    : '<option value="">(no agents – click Agents)</option>';
  // the agent picked here is saved as the default agent, so the default is always the one to show
  const def = S.agents.find(a => a.is_default) || S.agents[0];
  sel.value = def ? def.id : "";
  if (typeof syncModelRow === "function") syncModelRow();
}

// ---- dropdown that always shows the full list when focused (datalist only shows matches)
function attachCombo(input, getOpts) {
  let pop = null;
  const close = () => { if (pop) { pop.remove(); pop = null; } };
  const open = filter => {
    const opts = getOpts();
    close();
    if (!opts.length) return;
    const q = (filter || "").toLowerCase();
    const list = opts.filter(o => !q || o.toLowerCase().includes(q)).slice(0, 300);
    pop = document.createElement("div");
    pop.className = "combo";
    if (!list.length) pop.innerHTML = '<div class="empty">No matching models</div>';
    list.forEach(o => {
      const d = document.createElement("div");
      d.textContent = o;
      d.title = o;
      if (o === input.value) d.className = "cur";
      d.onmousedown = e => {
        e.preventDefault();
        input.value = o;
        close();
        input.dispatchEvent(new Event("change", { bubbles: true }));
      };
      pop.appendChild(d);
    });
    const r = input.getBoundingClientRect();
    const width = Math.max(r.width, 300);
    pop.style.width = width + "px";
    pop.style.left = Math.max(8, Math.min(r.left, innerWidth - width - 8)) + "px";
    pop.style.top = r.bottom + 2 + "px";
    pop.style.maxHeight = Math.max(120, innerHeight - r.bottom - 20) + "px";
    document.body.appendChild(pop);
    const cur = $(".cur", pop);
    if (cur) cur.scrollIntoView({ block: "center" });
  };
  input.addEventListener("focus", () => { input.select(); open(""); });
  input.addEventListener("click", () => { if (!pop) open(""); });
  input.addEventListener("input", () => open(input.value));
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
  return { show: () => open(""), close };
}

// ---- model picker in the chat panel (OpenAI-compatible / Anthropic agents); the choice is saved on the agent
const currentAgent = () => S.agents.find(a => String(a.id) === $("#agent-select").value) || null;
const usesModel = a => !!a && (a.provider === "openai" || a.provider === "anthropic");
let chatModels = [], chatModelsFor = null;

async function loadChatModels(force) {
  const a = currentAgent();
  if (!usesModel(a)) return false;
  if (!force && chatModelsFor === a.id && chatModels.length) return true;
  const btn = $("#model-load");
  btn.disabled = true;
  try {
    const r = await api("/api/models", { method: "POST", body: { agent_id: a.id } });
    chatModels = r.models.filter(m => m.tools !== false).map(m => m.id);
    chatModelsFor = a.id;
    return true;
  } catch (e) { fail(e); return false; } finally { btn.disabled = false; }
}

function syncModelRow() {
  const a = currentAgent();
  $("#model-row").classList.toggle("hidden", !usesModel(a));
  if (!usesModel(a)) return;
  // Older versions kept the choice in localStorage, which the desktop window does not keep between runs.
  const legacy = localStorage.getItem("pseditor.model." + a.id);
  if (legacy) {
    localStorage.removeItem("pseditor.model." + a.id);
    saveAgentModel(a, legacy, true);
  }
  $("#model-input").value = a.model || "";
  if (chatModelsFor !== a.id) { chatModels = []; chatModelsFor = null; }
}
async function saveAgentModel(a, model, quiet) {
  if (!model || model === a.model) return;
  const before = a.model;
  a.model = model;
  try {
    await api(`/api/agents/${a.id}/model`, { method: "POST", body: { model } });
    if (!quiet) toast(`Saved: ${a.name} · ${model}`);
  } catch (e) {
    if (a.model === model) a.model = before;
    fail(e);
  }
}
const chatCombo = attachCombo($("#model-input"), () => chatModels);
$("#model-input").addEventListener("focus", async () => {
  if (chatModelsFor === (currentAgent() || {}).id && chatModels.length) return;
  if (await loadChatModels() && document.activeElement === $("#model-input")) chatCombo.show();
});
$("#agent-select").addEventListener("change", async () => {
  syncModelRow();
  const a = currentAgent();
  if (!a) return;
  try {
    await api(`/api/agents/${a.id}/default`, { method: "POST", body: {} });
    const now = currentAgent() || a;  // the user may have picked again while this was saving
    S.agents.forEach(x => { x.is_default = x.id === now.id; });
    toast(`Saved: ${a.name}${usesModel(a) && a.model ? " · " + a.model : ""}`);
  } catch (e) { fail(e); }
});
$("#model-input").addEventListener("change", e => {
  const a = currentAgent();
  if (!a) return;
  const model = e.target.value.trim();
  if (model) saveAgentModel(a, model); else e.target.value = a.model || "";
});
$("#model-load").onclick = async () => {
  if (await loadChatModels(true)) {
    toast(`${chatModels.length} models available`);
    $("#model-input").focus();
    chatCombo.show();
  }
};

function setBusy(b) {
  $("#send").classList.toggle("hidden", b);
  $("#stop").classList.toggle("hidden", !b);
  $("#prompt").disabled = b;
  $("#attach").disabled = b;
  $("#chat-status").textContent = b ? "Agent is working…" : "";
}

// ---- files dropped, pasted or picked for the next message
const ATTACH_EXT = {
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image", bmp: "image",
  pdf: "pdf", xlsx: "excel", xlsm: "excel", xls: "excel", docx: "word", doc: "word",
};
const ATTACH_MAX_FILES = 4, ATTACH_MAX_FILE = 8 * 1024 * 1024, ATTACH_MAX_TOTAL = 20 * 1024 * 1024;
let attachments = [];

function attachKind(name) {
  return ATTACH_EXT[(name.split(".").pop() || "").toLowerCase()] || "";
}
function renderAttachments() {
  const box = $("#attach-list");
  box.innerHTML = "";
  box.classList.toggle("hidden", !attachments.length);
  attachments.forEach(a => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    chip.title = a.name;
    if (a.kind === "image") {
      const img = document.createElement("img");
      img.src = `data:${a.mime};base64,${a.data}`;
      img.alt = "";
      chip.appendChild(img);
    } else {
      const badge = document.createElement("span");
      badge.className = "attach-kind";
      badge.textContent = ATTACH_KIND[a.kind] || "FILE";
      chip.appendChild(badge);
    }
    const name = document.createElement("span");
    name.className = "attach-name";
    name.textContent = a.name;
    chip.appendChild(name);
    const x = document.createElement("button");
    x.type = "button";
    x.className = "attach-x";
    x.title = "Remove";
    x.textContent = "×";
    x.onclick = () => { attachments = attachments.filter(t => t.id !== a.id); renderAttachments(); };
    chip.appendChild(x);
    box.appendChild(chip);
  });
}
function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(`Could not read ${file.name || "that file"}`));
    reader.readAsDataURL(file);
  });
}
async function addFiles(fileList) {
  if ($("#prompt").disabled) return;
  // Copy first: clearing <input type="file"> empties the live FileList mid-loop.
  const files = [...fileList];
  for (const file of files) {
    if (attachments.length >= ATTACH_MAX_FILES) { toast(`You can attach at most ${ATTACH_MAX_FILES} files`, true); break; }
    let name = file.name;
    if (!name) {
      const sub = ((file.type || "image/png").split("/")[1] || "png").replace("jpeg", "jpg");
      name = `pasted-${Date.now()}.${sub}`;
    }
    const kind = attachKind(name);
    if (!kind) { toast(`${name} is not an image, PDF, Excel or Word file`, true); continue; }
    if (file.size > ATTACH_MAX_FILE) { toast(`${name} is larger than 8 MB`, true); continue; }
    const total = attachments.reduce((n, a) => n + a.size, 0);
    if (total + file.size > ATTACH_MAX_TOTAL) { toast("Attachments are larger than 20 MB in total", true); break; }
    try {
      const url = await readAsDataURL(file);
      const comma = url.indexOf(",");
      const data = comma >= 0 ? url.slice(comma + 1) : "";
      if (!data) throw new Error(`${name} is empty`);
      const mime = file.type || (url.slice(5, comma).split(";")[0]) || "application/octet-stream";
      attachments.push({ id: Date.now() + Math.random(), name, mime, kind, data, size: file.size });
    } catch (e) { fail(e); }
  }
  renderAttachments();
}
$("#attach").onclick = () => { if (!$("#prompt").disabled) $("#attach-file").click(); };
$("#attach-file").onchange = () => {
  const picked = [...$("#attach-file").files];
  $("#attach-file").value = "";
  addFiles(picked);
};
$("#prompt").addEventListener("paste", e => {
  const found = [];
  const items = e.clipboardData && e.clipboardData.items;
  if (items) {
    [...items].forEach(it => {
      if (it.kind === "file") { const f = it.getAsFile(); if (f) found.push(f); }
    });
  }
  if (found.length) { e.preventDefault(); addFiles(found); }
});
const composer = $(".composer");
composer.addEventListener("dragover", e => {
  if (!e.dataTransfer || ![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault();
  composer.classList.add("drag");
});
composer.addEventListener("dragleave", e => {
  if (!composer.contains(e.relatedTarget)) composer.classList.remove("drag");
});
composer.addEventListener("drop", e => {
  e.preventDefault();
  composer.classList.remove("drag");
  if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
});

async function sendChat() {
  const text = $("#prompt").value.trim();
  const files = attachments.slice();
  if (!text && !files.length) return;
  if (!S.ws) return toast("Open a folder first", true);
  if (!S.agents.length) { toast("Add an agent first", true); return openAgentSettings(); }
  if (!S.session) await loadSessions();
  try { await saveAll(); } catch (e) { return fail(e); }
  const body = { message: text, agent_id: +$("#agent-select").value || null, thinking: $("#think-level").value };
  if (files.length) body.attachments = files.map(a => ({ name: a.name, mime: a.mime, data: a.data }));
  if (usesModel(currentAgent()) && $("#model-input").value.trim()) body.model = $("#model-input").value.trim();
  if ($("#ctx-file").checked && S.active) body.active_file = { path: S.active.path, selection: getSelection() };

  if ($(".hint", msgBox())) msgBox().innerHTML = "";
  rememberPrompt(text);
  addBubble("user", text, files);
  $("#prompt").value = "";
  attachments = [];
  renderAttachments();
  setBusy(true);
  S.abort = new AbortController();
  let messageId = null, changedSeen = 0, finalChanged = [], assistantBubble = null;
  try {
    const resp = await fetch(`/api/sessions/${S.session.id}/chat`, {
      method: "POST", signal: S.abort.signal,
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body),
    });
    if (!resp.ok) { const j = await resp.json().catch(() => ({})); throw new Error(j.error || resp.statusText); }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    const handle = ev => {
      switch (ev.type) {
        case "start": messageId = ev.message_id; break;
        case "thinking": addThinking(ev.content); break;
        case "text": addBubble("assistant", ev.content); break;
        case "tool_call": addToolCard(ev.id, ev.name, ev.arguments); $("#chat-status").textContent = `Running ${ev.name}…`; break;
        case "approval":
          addApproval(ev);
          $("#chat-status").textContent = "Waiting for your approval…";
          document.title = "✋ Approval needed – PSEditor";
          break;
        case "approval_result":
          resolveApproval(ev);
          document.title = baseTitle;
          $("#chat-status").textContent = "Agent is working…";
          break;
        case "tool_result":
          fillToolCard(ev.id, ev.ok, ev.result);
          if (ev.changed && ev.changed.length > changedSeen) { changedSeen = ev.changed.length; onFilesChanged(ev.changed); }
          $("#chat-status").textContent = "Agent is working…";
          break;
        case "error": addBubble("error", ev.message); break;
        case "files_changed": onFilesChanged(ev.paths); break;
        case "done":
          finalChanged = ev.changed || [];
          if (ev.title && S.session) {
            S.session.title = ev.title;
            const s = S.sessions.find(x => x.id === S.session.id);
            if (s) s.title = ev.title;
            fillSessionSelect();
          }
          break;
      }
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (line.trim()) handle(JSON.parse(line));
      }
    }
    addChanges(messageId, finalChanged);
  } catch (e) {
    if (e.name === "AbortError") addBubble("error", "Stopped.");
    else addBubble("error", e.message);
  } finally {
    S.abort = null;
    document.title = baseTitle;
    $$(".approval:not(.done)", msgBox()).forEach(d => d.remove());   // nobody is waiting for these any more
    setBusy(false);
    $("#prompt").focus();
    refreshTree();
  }
}
const baseTitle = document.title;
// Tell the server to release an action that is waiting for approval (otherwise it would wait until the timeout).
const releasePending = () => {
  if (!S.session) return;
  fetch(`/api/sessions/${S.session.id}/stop`, {
    method: "POST", keepalive: true,
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: "{}",
  }).catch(() => {});
};
const THINK_LEVELS = ["off", "low", "medium", "high"];
const thinkLevel = $("#think-level");
const thinkShow = $("#think-show");
const savedLevel = localStorage.getItem("pseditor.thinkLevel");
if (THINK_LEVELS.includes(savedLevel)) thinkLevel.value = savedLevel;
thinkShow.checked = localStorage.getItem("pseditor.thinkShow") !== "0";
function applyThinkView() {
  $("#messages").classList.toggle("hide-thinking", !thinkShow.checked);
}
thinkLevel.onchange = () => localStorage.setItem("pseditor.thinkLevel", thinkLevel.value);
thinkShow.onchange = () => {
  localStorage.setItem("pseditor.thinkShow", thinkShow.checked ? "1" : "0");
  applyThinkView();
};
applyThinkView();
$("#send").onclick = sendChat;
$("#stop").onclick = () => { if (S.abort) { releasePending(); S.abort.abort(); } };
window.addEventListener("pagehide", () => { if (S.abort) releasePending(); });
$("#prompt").addEventListener("keydown", e => {
  const el = e.target;
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "c") {
    const picked = el.value.substring(el.selectionStart, el.selectionEnd);
    if (picked) writeClipboard(picked);
    return;
  }
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); return; }
  const collapsed = el.selectionStart === el.selectionEnd;
  const onFirstLine = collapsed && !el.value.slice(0, el.selectionStart).includes("\n");
  const onLastLine = collapsed && !el.value.slice(el.selectionEnd).includes("\n");
  if (e.key === "ArrowUp" && onFirstLine && promptHist > 0) {
    e.preventDefault();
    if (promptHist === promptHistory.length) promptDraft = el.value;
    promptHist--;
    el.value = promptHistory[promptHist];
    el.setSelectionRange(el.value.length, el.value.length);
    return;
  }
  if (e.key === "ArrowDown" && onLastLine && promptHist < promptHistory.length) {
    e.preventDefault();
    promptHist++;
    el.value = promptHist === promptHistory.length ? promptDraft : promptHistory[promptHist];
    el.setSelectionRange(el.value.length, el.value.length);
  }
});

// ---------------------------------------------------------------- workspaces
function wsStatus(ws) {
  const folders = ws.folders || [{ path: ws.path }];
  if (folders.length === 1) return folders[0].path;
  return folders.map(f => f.name + ": " + f.path).join(" · ");
}

/** "Workspace" bar in the chat panel: the folders the model can read/write, plus an add button. */
function renderWsFolders() {
  const box = $("#ws-folders");
  if (!box) return;
  box.innerHTML = "";
  const folders = (S.ws && S.ws.folders) || [];
  if (!folders.length) { box.innerHTML = '<span class="hint">No workspace open</span>'; return; }
  const multi = folders.length > 1;
  folders.forEach(f => {
    const chip = document.createElement("span");
    chip.className = "ws-chip";
    chip.title = f.path;
    chip.innerHTML = `<span>📁 ${esc(f.name)}</span>` + (multi ? '<a title="Remove from workspace">✕</a>' : "");
    const x = chip.querySelector("a");
    if (x) x.onclick = () => removeRootFolder({ name: f.name, path: f.name });
    box.appendChild(chip);
  });
}
$("#chat-add-folder").onclick = () => folderPicker("add");

async function openWorkspace(ws) {
  try { await saveAll(); } catch (e) { return fail(e); }
  closeAllTabs();
  if (S.ws && S.ws.id !== ws.id && T.xterm) ptyDetach();   // each workspace has its own terminal
  S.ws = ws; S.tree = {}; S.expanded = new Set(); S.cwd = ""; S.termPrompt = ""; S.session = null;
  localStorage.setItem("pseditor.ws", ws.id);
  const n = (ws.folders || []).length;
  $("#ws-name").textContent = n > 1 ? `${ws.name} (${n})` : ws.name;
  renderWsFolders();
  document.title = ws.name + " – PSEditor";
  setStatus(wsStatus(ws));
  try {
    await refreshTree();
    await loadSessions();
  } catch (e) { fail(e); }
  updatePrompt();
  loadTermState();
  if (ptyMode() && !$("#terminal").classList.contains("hidden")) ptyAttach();
}

function folderPicker(mode = "open") {
  const adding = mode === "add";
  if (adding && !S.ws) return toast("Open a workspace first", true);
  const body = document.createElement("div");
  const recent = !adding ? S.workspaces.map(w => {
    const n = (w.folders || []).length;
    const hint = n > 1 ? `${n} folders` : w.path;
    return `<div data-id="${w.id}"><b>${esc(w.name)}</b> <span class="hint">${esc(hint)}</span> <a data-del="${w.id}" style="float:right;color:var(--err)">remove</a></div>`;
  }).join("") : "";
  const current = adding && S.ws ? `<div class="hint" style="margin-bottom:8px">Adding to <b>${esc(S.ws.name)}</b></div>` : "";
  body.innerHTML = `
    ${current}
    ${recent ? `<div><b>Recent</b></div><div class="dirlist" id="recent">${recent}</div><br>` : ""}
    <div><b>${adding ? "Add a folder to this workspace" : "Open a folder on this computer"}</b></div>
    <div class="row-flex" style="margin-top:6px"><input id="fp-path" placeholder="Path, e.g. D:\\projects\\myapp"><button id="fp-go">Browse</button><button id="fp-open" class="primary">${adding ? "Add" : "Open"}</button></div>
    ${!adding && S.ws ? `<div class="row-flex" style="margin-top:6px"><button id="fp-add-here">Add to current workspace instead…</button></div>` : ""}
    <div class="dirlist" id="fp-list"></div>
    <div class="row-flex" style="margin-top:8px"><button id="fp-new">+ New folder here…</button></div>`;
  openModal(adding ? "Add folder" : "Open folder", body);
  const input = $("#fp-path", body), list = $("#fp-list", body);
  let currentPath = "";
  const browse = async p => {
    try {
      const r = await api("/api/browse", { method: "POST", body: { path: p } });
      currentPath = r.path; input.value = r.path; list.innerHTML = "";
      if (r.parent !== null) {
        const up = document.createElement("div"); up.textContent = "⬑ ..";
        up.onclick = () => browse(r.parent); list.appendChild(up);
      }
      r.dirs.forEach(d => {
        const el = document.createElement("div"); el.textContent = "📁 " + d.name;
        el.onclick = () => browse(d.path); list.appendChild(el);
      });
    } catch (e) { fail(e); }
  };
  const open = async (path, create) => {
    try {
      let w;
      if (adding) {
        // Paths change when a folder is added (a.py -> Root/a.py): save edits first, then reload.
        await saveAll();
        w = await api(`/api/workspaces/${S.ws.id}/folders`, { method: "POST", body: { path, create } });
        S.workspaces = S.workspaces.map(x => x.id === w.id ? w : x);
        closeAllTabs();
        S.tree = {}; S.expanded = new Set();
      } else {
        w = await api("/api/workspaces", { method: "POST", body: { path, create } });
        const boot = await api("/api/bootstrap");
        S.workspaces = boot.workspaces;
      }
      closeModal();
      openWorkspace(w);
    } catch (e) { fail(e); }
  };
  $("#fp-go", body).onclick = () => browse(input.value.trim());
  $("#fp-open", body).onclick = () => open(input.value.trim());
  $("#fp-new", body).onclick = () => {
    const n = prompt("New folder name"); if (!n || !currentPath) return;
    open(currentPath.replace(/[\\/]$/, "") + sep() + n.trim(), true);
  };
  const addHere = $("#fp-add-here", body);
  if (addHere) addHere.onclick = () => { closeModal(); folderPicker("add"); };
  input.onkeydown = e => { if (e.key === "Enter") open(input.value.trim()); };
  const rec = $("#recent", body);
  if (rec) rec.onclick = async e => {
    const del = e.target.dataset.del;
    if (del) {
      await api(`/api/workspaces/${del}`, { method: "DELETE" });
      S.workspaces = S.workspaces.filter(w => w.id !== +del);
      return folderPicker();
    }
    const row = e.target.closest("[data-id]");
    if (row) { closeModal(); openWorkspace(S.workspaces.find(w => w.id === +row.dataset.id)); }
  };
  browse("");
}
$("#btn-open").onclick = () => folderPicker("open");

// ---------------------------------------------------------------- agents settings
const PRESETS = {
  "OpenRouter": { provider: "openai", base_url: "https://openrouter.ai/api/v1", model: "anthropic/claude-sonnet-5.5" },
  "OpenRouter (free model)": { provider: "openai", base_url: "https://openrouter.ai/api/v1", model: "nvidia/nemotron-3-super-120b-a12b:free" },
  "OpenAI": { provider: "openai", base_url: "https://api.openai.com/v1", model: "gpt-4.1" },
  "Ollama (local)": { provider: "openai", base_url: "http://localhost:11434/v1", model: "qwen2.5-coder:7b" },
  "LM Studio (local)": { provider: "openai", base_url: "http://localhost:1234/v1", model: "local-model" },
  "Anthropic Claude": { provider: "anthropic", base_url: "https://api.anthropic.com", model: "claude-sonnet-4-5" },
  "Custom HTTP agent (webhook)": { provider: "webhook", base_url: "http://localhost:9000/agent" },
  "Claude Code (CLI)": { provider: "cli", command: "claude -p --permission-mode acceptEdits" },
  "Codex (CLI)": { provider: "cli", command: "codex exec --full-auto -" },
  "Aider (CLI)": { provider: "cli", command: "aider --yes-always --message {prompt}" },
};

function openAgentSettings(selectedId) {
  let sel = S.agents.find(a => a.id === selectedId) || S.agents[0] || null;
  let draft = null; // unsaved new agent
  const body = document.createElement("div");
  body.className = "agents-layout";

  const render = () => {
    const cur = draft || sel;
    body.innerHTML = `
      <div class="agents-list">
        <select id="preset"><option value="">+ Add agent from preset…</option>${Object.keys(PRESETS).map(k => `<option>${esc(k)}</option>`).join("")}</select>
        ${S.agents.map(a => `<div class="agent-item ${!draft && sel && sel.id === a.id ? "sel" : ""}" data-id="${a.id}">${esc(a.name)}${a.is_default ? " ★" : ""}<small>${esc(a.provider)}${a.model ? " · " + esc(a.model) : ""}</small></div>`).join("")}
        ${draft ? '<div class="agent-item sel">(new agent)</div>' : ""}
      </div>
      <div style="flex:1">${cur ? formHtml(cur) : '<p class="hint">Pick a preset on the left to connect an agent.</p>'}</div>`;
    $("#preset", body).onchange = e => {
      const name = e.target.value; if (!name) return;
      draft = { id: null, name, is_default: !S.agents.length, temperature: "", extra_headers: "", system_prompt: "", api_key: "", base_url: "", model: "", command: "", ...PRESETS[name] };
      render();
    };
    $$(".agent-item[data-id]", body).forEach(el => el.onclick = () => { draft = null; sel = S.agents.find(a => a.id === +el.dataset.id); render(); });
    if (cur) wireForm(cur);
  };

  const formHtml = a => `
    <div class="form">
      <label>Name</label><input id="f-name" value="${esc(a.name || "")}">
      <label>Type</label><select id="f-provider">${[["openai", "OpenAI-compatible API"], ["anthropic", "Anthropic Messages API"], ["webhook", "Custom HTTP agent (webhook)"], ["cli", "Command-line agent"]].map(([v, l]) => `<option value="${v}" ${a.provider === v ? "selected" : ""}>${l}</option>`).join("")}</select>
      <label data-for="url">Base URL</label><input id="f-url" data-for="url" value="${esc(a.base_url || "")}">
      <label data-for="key">API key</label><input id="f-key" data-for="key" type="password" autocomplete="new-password" placeholder="${a.has_key ? "(saved – leave blank to keep)" : ""}">
      <label data-for="model">Model</label><div class="row-flex" data-for="model"><input id="f-model" autocomplete="off" value="${esc(a.model || "")}" placeholder="e.g. openai/gpt-4.1"><button id="f-load" type="button">Load models</button></div>
      <div class="help" data-for="model" id="f-modelhelp">Click “Load models” to list what this provider offers (uses the URL and key above).</div>
      <label data-for="cmd">Command</label><input id="f-cmd" data-for="cmd" value="${esc(a.command || "")}">
      <div class="help" data-for="cmd">Runs in the workspace folder. The prompt is piped to stdin, or inserted where <code>{prompt}</code> appears. File changes made by the CLI are detected and can be reverted.</div>
      <label data-for="temp">Temperature</label><input id="f-temp" data-for="temp" value="${a.temperature ?? ""}" placeholder="default">
      <label data-for="hdr">Extra headers</label><textarea id="f-hdr" data-for="hdr" placeholder='{"X-Title": "PSEditor"}'>${esc(a.extra_headers || "")}</textarea>
      <label>System prompt</label><textarea id="f-sys" placeholder="Optional extra instructions for this agent">${esc(a.system_prompt || "")}</textarea>
      <label></label><label style="color:var(--fg)"><input type="checkbox" id="f-def" ${a.is_default ? "checked" : ""}> Default agent</label>
      <label></label><div class="row-flex"><button id="f-save" class="primary">Save</button><button id="f-test" ${a.id ? "" : "disabled"}>Test connection</button><button id="f-del" ${a.id ? "" : "disabled"} style="color:var(--err)">Delete</button></div>
      <div class="help" id="f-msg"></div>
    </div>`;

  const wireForm = a => {
    const apply = () => {
      const p = $("#f-provider", body).value;
      const show = {
        url: p !== "cli", key: p !== "cli", model: p === "openai" || p === "anthropic",
        cmd: p === "cli", temp: p === "openai" || p === "anthropic", hdr: p !== "cli",
      };
      $$("[data-for]", body).forEach(el => el.classList.toggle("hidden", !show[el.dataset.for]));
      $$('label[data-for="url"]', body).forEach(l => l.textContent = p === "webhook" ? "Webhook URL" : "Base URL");
    };
    $("#f-provider", body).onchange = apply;
    apply();
    const collect = () => ({
      name: $("#f-name", body).value, provider: $("#f-provider", body).value, base_url: $("#f-url", body).value,
      api_key: $("#f-key", body).value, model: $("#f-model", body).value, command: $("#f-cmd", body).value,
      temperature: $("#f-temp", body).value, extra_headers: $("#f-hdr", body).value,
      system_prompt: $("#f-sys", body).value, is_default: $("#f-def", body).checked,
    });
    const msg = (t, bad) => { const m = $("#f-msg", body); m.textContent = t; m.style.color = bad ? "var(--err)" : "var(--ok)"; };
    let formModels = [];
    const formCombo = attachCombo($("#f-model", body), () => formModels);
    $("#f-load", body).onclick = async () => {
      const help = $("#f-modelhelp", body);
      help.textContent = "Loading models…"; help.style.color = "";
      try {
        const r = await api("/api/models", { method: "POST", body: {
          agent_id: a.id, provider: $("#f-provider", body).value,
          base_url: $("#f-url", body).value, api_key: $("#f-key", body).value,
        } });
        const usable = r.models.filter(m => m.tools !== false);
        formModels = usable.map(m => m.id);
        help.textContent = `${usable.length} models available` + (usable.length < r.models.length ? ` (${r.models.length - usable.length} hidden: no tool-calling support)` : "") + ". Click the Model box to choose, or type to filter.";
        $("#f-model", body).focus();
        formCombo.show();
      } catch (e) { help.textContent = e.message; help.style.color = "var(--err)"; }
    };
    $("#f-save", body).onclick = async () => {
      try {
        const saved = a.id
          ? await api(`/api/agents/${a.id}`, { method: "PUT", body: collect() })
          : await api("/api/agents", { method: "POST", body: collect() });
        S.agents = (await api("/api/agents")).agents;
        fillAgentSelect();
        draft = null; sel = S.agents.find(x => x.id === saved.id);
        render();
        toast("Agent saved");
      } catch (e) { msg(e.message, true); }
    };
    $("#f-test", body).onclick = async () => {
      msg("Testing…");
      try { const r = await api(`/api/agents/${a.id}/test`, { method: "POST", body: {} }); msg(r.message, !r.ok); } catch (e) { msg(e.message, true); }
    };
    $("#f-del", body).onclick = async () => {
      if (!confirm(`Delete agent "${a.name}"?`)) return;
      await api(`/api/agents/${a.id}`, { method: "DELETE" });
      S.agents = (await api("/api/agents")).agents;
      fillAgentSelect(); sel = S.agents[0] || null; render();
    };
  };
  openModal("Agents", body);
  render();
}
$("#btn-settings").onclick = () => openAgentSettings();

function openOcrSettings() {
  api("/api/ocr-settings").then(cfg => {
    const body = document.createElement("div");
    body.innerHTML = `
      <div class="form">
        <label>Endpoint</label><input id="ocr-url" value="${esc(cfg.endpoint || "")}" placeholder="https://api.opentyphoon.ai/v1/ocr">
        <div class="help">Typhoon reads the image through chat completions. A URL ending in /ocr is sent to /chat/completions. Leave the endpoint empty to turn OCR off.</div>
        <label>API key</label><input id="ocr-key" type="password" autocomplete="new-password" placeholder="${cfg.has_key ? "(saved – leave blank to keep)" : "Bearer token"}">
        <label>Model</label><input id="ocr-model" value="${esc(cfg.model || "")}" placeholder="typhoon-ocr">
        <label></label><div class="row-flex"><button id="ocr-test" type="button">Test</button><button id="ocr-save" class="primary">Save</button></div>
        <div class="help" id="ocr-msg"></div>
      </div>`;
    openModal("OCR endpoint", body);
    const msg = $("#ocr-msg", body);
    $("#ocr-test", body).onclick = async () => {
      msg.textContent = "Testing with a small image…";
      msg.style.color = "";
      try {
        const r = await api("/api/ocr-settings/test", { method: "POST", body: {
          endpoint: $("#ocr-url", body).value,
          api_key: $("#ocr-key", body).value,
          model: $("#ocr-model", body).value,
        }});
        msg.textContent = r.message;
        msg.style.color = r.ok ? "var(--ok)" : "var(--err)";
      } catch (e) { msg.textContent = e.message; msg.style.color = "var(--err)"; }
    };
    $("#ocr-save", body).onclick = async () => {
      try {
        const saved = await api("/api/ocr-settings", { method: "PUT", body: {
          endpoint: $("#ocr-url", body).value,
          api_key: $("#ocr-key", body).value,
          model: $("#ocr-model", body).value,
        }});
        S.ocrEndpoint = saved.endpoint || "";
        if (S.active && S.active.kind === "image") showImage(S.active);
        toast("OCR endpoint saved");
        closeModal();
      } catch (e) { msg.textContent = e.message; msg.style.color = "var(--err)"; }
    };
  }).catch(fail);
}
$("#btn-ocr").onclick = () => openOcrSettings();
$("#image-ocr-settings").onclick = () => openOcrSettings();

$("#image-ocr-btn").onclick = async () => {
  if (!S.ws || !S.active || S.active.kind !== "image") return;
  const btn = $("#image-ocr-btn");
  const box = $("#image-ocr");
  btn.disabled = true;
  box.classList.remove("hidden");
  box.textContent = "Reading text…";
  try {
    const r = await api(`/api/ws/${S.ws.id}/ocr`, { method: "POST", body: { path: S.active.path } });
    box.textContent = (r.text || "").trim() || "(no text found)";
  } catch (e) {
    box.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
};

// ---------------------------------------------------------------- layout / shortcuts
function makeResizer(id, apply) {
  const el = $(id);
  el.addEventListener("mousedown", e => {
    e.preventDefault();
    el.classList.add("drag");
    const move = ev => apply(ev);
    const up = () => { el.classList.remove("drag"); removeEventListener("mousemove", move); removeEventListener("mouseup", up); document.body.style.userSelect = ""; };
    document.body.style.userSelect = "none";
    addEventListener("mousemove", move);
    addEventListener("mouseup", up);
  });
}
makeResizer("#rz-left", e => { $("#sidebar").style.width = Math.max(140, Math.min(600, e.clientX)) + "px"; });
makeResizer("#rz-right", e => { $("#chat").style.width = Math.max(260, Math.min(800, innerWidth - e.clientX)) + "px"; });
makeResizer("#rz-term", e => {
  const bottom = $("#statusbar").getBoundingClientRect().top;
  $("#terminal").style.height = Math.max(80, Math.min(innerHeight - 200, bottom - e.clientY)) + "px";
});
$("#rz-term").classList.add("hidden");

$("#btn-quick").onclick = quickOpen;
$("#btn-term").onclick = () => toggleTerminal();
$("#btn-chat").onclick = () => $("#prompt").focus();

window.addEventListener("keydown", e => {
  // Inside the real terminal Ctrl+P / Ctrl+L / Ctrl+S ... belong to the shell; only Ctrl+` is ours.
  if (e.key !== "`" && e.target && e.target.closest && e.target.closest("#term-xterm")) return;
  const mod = e.ctrlKey || e.metaKey;
  if (!mod) { if (e.key === "Escape" && modalOpen()) closeModal(); return; }
  const k = e.key.toLowerCase();
  if (k === "s") { e.preventDefault(); saveActive(); }
  else if (k === "p") { e.preventDefault(); quickOpen(); }
  else if (k === "l") { e.preventDefault(); $("#prompt").focus(); }
  else if (e.key === "`") { e.preventDefault(); toggleTerminal(); }
  else if (k === "o" && e.shiftKey) { e.preventDefault(); folderPicker(); }
  else if (k === "f" && e.shiftKey) { e.preventDefault(); $$(".side-tab")[1].click(); }
});

// ---------------------------------------------------------------- boot
(async function init() {
  renderTabs();
  renderTree();
  await setupEditor();
  try {
    const boot = await api("/api/bootstrap");
    S.agents = boot.agents; S.workspaces = boot.workspaces;
    S.platform = boot.platform; S.termEnabled = boot.terminal_enabled; S.pty = !!boot.pty;
    S.ocrEndpoint = boot.ocr_endpoint || "";
    fillAgentSelect();
    if (!S.termEnabled) $("#btn-term").classList.add("hidden");
    const last = +localStorage.getItem("pseditor.ws");
    const ws = S.workspaces.find(w => w.id === last) || null;
    if (ws) await openWorkspace(ws); else folderPicker();
    if (!S.agents.length) setStatus("Tip: click “Agents” to connect an external agent.");
  } catch (e) { fail(e); }
})();
})();
