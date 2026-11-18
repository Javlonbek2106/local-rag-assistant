"use strict";
const $ = (id) => document.getElementById(id);
const STATE_TEXT = { queued: "Navbatda", processing: "O'qilmoqda…", done: "Tayyor", failed: "Xato" };
// What one version is doing (the server decides the phase; see PHASES in rag/api.py).
const PHASE_TEXT = {
  active: "Faol (savollar shundan javob oladi)",
  processing: "Ishlanmoqda",
  queued: "Navbatda",
  superseded: "Indekslangan, faol emas",
  failed: "Xato",
};

// What a version being indexed is doing, from its stage and the position the worker reports ({unit, done, total}).
const STAGE_TEXT = {
  PROCESSING: "Tahlil qilinmoqda",
  EMBEDDING: "Embedding hisoblanmoqda",
  INDEXING: "Indekslanmoqda",
  VERIFYING: "Tekshirilmoqda",
};

function percent(p) {
  return p && p.total ? Math.min(100, Math.floor((p.done * 100) / p.total)) : null;
}

function workText(stage, p) {
  const base = STAGE_TEXT[stage] || "Ishlanmoqda";
  if (!p) return `${base}…`;
  const pct = percent(p) === null ? "" : ` (${percent(p)}%)`;
  if (p.unit === "pages") return p.total ? `${base}: ${p.done}/${p.total} sahifa${pct}` : `${base}: ${p.done} sahifa`;
  if (p.unit === "assembling") return `${base}: hujjat yig'ilmoqda…`;
  if (stage === "PROCESSING") return `Bo'laklarga bo'linmoqda: ${p.done} ta`; // the total is not known while cutting
  return p.total ? `${base}: ${p.done}/${p.total} bo'lak${pct}` : `${base}: ${p.done} bo'lak`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // textContent only: file names and answers are never parsed as HTML
  return node;
}

// The page is refreshed by polling. Updating a node only when something about it really changed keeps the nodes (and the
// keyboard focus on them) alive and does not make a screen reader read the same thing again.
function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function setClass(node, name) {
  if (node.className !== name) node.className = name;
}

function setAttr(node, name, value) {
  const text = String(value);
  if (node.getAttribute(name) !== text) node.setAttribute(name, text);
}

// ---------- screen readers and keyboard focus ----------
// One polite live region for the few things worth saying aloud (a document became ready, failed, passed a quarter of its
// indexing). The visible texts are not live regions: a progress text that changes every second would be read every second.
function announce(text) {
  const region = $("sr-status");
  if (text && region.textContent !== text) region.textContent = text;
}

// Run a DOM update and make sure the keyboard focus is where it was. The updates below change nodes in place, so the focused
// node normally survives and nothing needs to be done; if it was replaced anyway (it has a data-focus-key), the node with the
// same key is focused again, with its text selection. Focus that the user has moved elsewhere is never taken back.
function withFocusKept(update) {
  const active = document.activeElement;
  const key = active && active.dataset ? active.dataset.focusKey : undefined;
  const selection = active && typeof active.selectionStart === "number"
    ? [active.selectionStart, active.selectionEnd, active.selectionDirection] : null;
  update();
  const now = document.activeElement;
  if (key === undefined || (now && now !== document.body)) return; // nothing was lost, or the user moved on
  const next = [...document.querySelectorAll("[data-focus-key]")].find((node) => node.dataset.focusKey === key);
  if (!next) return;
  next.focus({ preventScroll: true });
  if (selection && typeof next.setSelectionRange === "function") {
    try { next.setSelectionRange(...selection); } catch { /* not a text field */ }
  }
}

function showBanner(text) {
  const banner = $("banner");
  banner.hidden = !text;
  setText(banner, text || "");
}

// ---------- documents ----------
let docsTimer = null;
let lastDocs = [];

async function refreshDocs() {
  clearTimeout(docsTimer);
  let delay = 8000;
  try {
    const res = await fetch("/api/documents");
    const data = await res.json();
    lastDocs = data.documents;
    renderDocs(data.documents);
    showBanner(data.error);
    if (data.documents.some((d) => d.state === "queued" || d.state === "processing")) delay = 1500;
  } catch {
    showBanner("Server bilan aloqa yo'q. start.bat oynasi ochiqmi?");
    delay = 4000;
  }
  docsTimer = setTimeout(refreshDocs, delay);
}

const openPanels = new Set(); // names of the documents whose versions are shown
const armed = new Map(); // two-click confirmations: key -> time until which the second click counts
const views = new Map(); // document name -> the row that shows it
let firstRender = true;

// A document that has an active version can always be asked about, whatever else is happening to it: it is shown as
// ready, and what happens to its newer version goes on a line of its own.
function statusOf(doc) {
  if (doc.searchable) return { cls: "done", text: `Tayyor · v${doc.version} · ${doc.chunks} bo'lak` };
  if (doc.state === "processing") return { cls: "processing", text: workText(doc.stage, doc.progress) };
  return { cls: doc.state, text: doc.state === "done" ? `Tayyor · ${doc.chunks} bo'lak` : STATE_TEXT[doc.state] || doc.state };
}

function updateLine(doc) {
  if (!doc.searchable) return null;
  const newer = `v${doc.latest_version}`;
  if (doc.update_error) return { cls: "bad", text: `${newer} yangilanmadi: ${doc.update_error} (v${doc.version} ishlashda davom etadi)` };
  if (doc.state === "processing") return { cls: "warn", text: `${newer}: ${workText(doc.stage, doc.progress)} (hozircha v${doc.version} dan javob beriladi)` };
  if (doc.state === "queued") return { cls: "warn", text: `${newer} navbatda (hozircha v${doc.version} dan javob beriladi)` };
  return null;
}

// Add or remove an optional part of a row; returns it (or null). `before` is the node it must come in front of.
function optional(holder, field, want, make, before) {
  if (want && !holder[field]) {
    holder[field] = make();
    holder.li.insertBefore(holder[field], before || null);
  } else if (!want && holder[field]) {
    holder[field].remove();
    holder[field] = null;
  }
  return holder[field];
}

function createRow(name) {
  const view = { name, li: el("li"), bar: null, update: null, err: null, panel: null, last: undefined, asked: 0, shown: 0 };
  view.nameEl = el("span", "name", name);
  view.del = deleteButton(name);
  view.state = el("span", "state");
  view.toggle = versionsToggle(view);
  view.li.append(view.nameEl, view.del, view.state, view.toggle);
  return view;
}

function updateRow(view, doc) {
  const status = statusOf(doc);
  setText(view.state, status.text);
  setClass(view.state, `state ${status.cls}`);

  const total = doc.state === "processing" && doc.progress && doc.progress.total ? doc.progress.total : 0;
  const bar = optional(view, "bar", total > 0, () => el("progress", "bar"), view.toggle);
  if (bar) {
    const done = Math.min(doc.progress.done, total);
    setAttr(bar, "max", total);
    setAttr(bar, "value", done);
    setAttr(bar, "aria-label", `${doc.name}: indekslash taraqqiyoti`);
    setAttr(bar, "aria-valuemin", 0);
    setAttr(bar, "aria-valuemax", total);
    setAttr(bar, "aria-valuenow", done);
    setAttr(bar, "aria-valuetext", workText(doc.stage, doc.progress));
  }

  const open = openPanels.has(view.name);
  setText(view.toggle, open ? "Versiyalarni yopish" : "Versiyalar");
  setAttr(view.toggle, "aria-expanded", open);
  setAttr(view.toggle, "aria-label", `${open ? "Versiyalarni yopish" : "Versiyalar"}: ${view.name}`);

  const update = updateLine(doc);
  const line = optional(view, "update", !!update, () => el("span"), view.err || view.panel);
  if (line) {
    setText(line, update.text);
    setClass(line, `update ${update.cls}`);
  }
  const err = optional(view, "err", !!doc.error, () => {
    const node = el("span", "err");
    node.setAttribute("role", "alert");
    return node;
  }, view.panel);
  if (err) setText(err, doc.error);

  const panel = optional(view, "panel", open, () => makePanel(view));
  if (panel) refreshPanel(view);
  announceChange(view, doc);
}

// Say it aloud when something worth knowing happens to a document, not at every poll and not for the whole list at load.
function announceChange(view, doc) {
  const stage = doc.state === "processing" ? doc.stage : null;
  const pct = percent(doc.progress);
  const key = `${doc.state}|${stage}|${doc.version}|${pct === null ? "-" : Math.floor(pct / 25)}`;
  const previous = view.last;
  view.last = key;
  if (firstRender || previous === key) return;
  if (doc.state === "processing") announce(`${doc.name}: ${workText(doc.stage, doc.progress)}`);
  else if (doc.state === "done") announce(`${doc.name}: tayyor`);
  else if (doc.state === "failed") announce(`${doc.name}: xato. ${doc.error || ""}`);
  else if (doc.state === "queued") announce(`${doc.name}: navbatda`);
}

function renderDocs(documents) {
  withFocusKept(() => {
    const list = $("docs");
    $("docs-empty").hidden = documents.length > 0;
    const names = new Set(documents.map((d) => d.name));
    for (const [name, view] of views) {
      if (!names.has(name)) { view.li.remove(); views.delete(name); }
    }
    documents.forEach((doc, index) => {
      let view = views.get(doc.name);
      if (!view) { view = createRow(doc.name); views.set(doc.name, view); }
      updateRow(view, doc);
      // Only a row that is not where it belongs is moved (a moved node loses focus); a new row goes in without moving the others.
      if (list.children[index] !== view.li) list.insertBefore(view.li, list.children[index] || null);
    });
  });
  firstRender = false;
}

function versionsToggle(view) {
  const button = el("button", "link", "Versiyalar");
  button.dataset.focusKey = `doc:${view.name}:versions`;
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => {
    if (openPanels.has(view.name)) openPanels.delete(view.name); else openPanels.add(view.name);
    renderDocs(lastDocs);
  });
  return button;
}

// Every version of a document with its own state. A version that was indexed but is not active can be made active.
function makePanel(view) {
  const panel = el("div", "versions");
  panel.setAttribute("role", "group");
  panel.setAttribute("aria-label", `Versiyalar: ${view.name}`);
  view.message = el("span", "muted", "Yuklanmoqda…");
  view.list = el("div", "vlist");
  view.rows = new Map();
  view.notice = null;
  view.shown = view.asked; // what was asked for before this panel existed is older than the panel and never shown in it
  panel.append(view.message, view.list);
  return panel;
}

// Every request gets a number; an answer is shown only if it is newer than what the panel shows already. Answers can come
// back in any order (a poll that was asked a moment before a rollback may be answered after the request that follows it),
// and an older one must never put the panel back to a state that has been left.
async function refreshPanel(view) {
  const number = ++view.asked;
  try {
    const res = await fetch(`/api/documents/${encodeURIComponent(view.name)}/versions`);
    const data = await res.json();
    if (!res.ok || !data.versions) throw new Error("versions");
    if (view.panel && number > view.shown) {
      view.shown = number;
      withFocusKept(() => syncVersions(view, data.versions));
    }
  } catch {
    if (view.panel && number > view.shown) {
      view.shown = number;
      setText(view.message, "Versiyalarni o'qib bo'lmadi");
      setClass(view.message, "err");
      view.message.setAttribute("role", "alert");
    }
  }
}

function syncVersions(view, versions) {
  view.message.remove();
  if (view.notice) { view.notice.remove(); view.notice = null; }
  const wanted = new Set(versions.map((v) => v.version));
  for (const [number, row] of view.rows) {
    if (!wanted.has(number)) { row.el.remove(); view.rows.delete(number); }
  }
  versions.forEach((v, index) => {
    let row = view.rows.get(v.version);
    if (!row) {
      row = { el: el("div"), li: null, number: v.version, err: null, button: null };
      row.li = row.el; // `optional` adds parts to `holder.li`
      row.vnum = el("span", "vnum");
      row.phase = el("span");
      row.el.append(row.vnum, row.phase);
      view.rows.set(v.version, row);
    }
    updateVersionRow(view, row, v);
    if (view.list.children[index] !== row.el) view.list.insertBefore(row.el, view.list.children[index] || null);
  });
}

function updateVersionRow(view, row, v) {
  setClass(row.el, `ver ${v.phase}`);
  setText(row.vnum, `v${v.version}`);
  let text = v.phase === "processing" ? workText(v.status, v.progress) : PHASE_TEXT[v.phase] || v.phase;
  if (v.stale) text += " · rollbackdan keyin faol bo'lmaydi";
  if (v.chunks) text += ` · ${v.chunks} bo'lak`;
  setText(row.phase, text);
  setClass(row.phase, `phase ${v.phase}`);
  const err = optional(row, "err", !!v.error, () => {
    const node = el("span", "err");
    node.setAttribute("role", "alert");
    return node;
  }, row.button);
  if (err) setText(err, v.error);
  optional(row, "button", v.phase === "superseded", () => restoreButton(view, v.version));
}

function restoreButton(view, version) {
  const name = view.name;
  const key = `${name}#${version}`;
  const label = "Shu versiyani faol qilish";
  const isArmed = () => (armed.get(key) || 0) > Date.now();
  const button = el("button", "");
  button.dataset.focusKey = `ver:${key}`;
  const show = () => {
    setText(button, isArmed() ? "Aniqmi?" : label);
    setAttr(button, "aria-label", `${isArmed() ? "Aniqmi? " : ""}${label}: ${name} v${version}`);
  };
  show();
  button.addEventListener("click", async () => {
    if (!isArmed()) {
      armed.set(key, Date.now() + 3000);
      show();
      announce("Tasdiqlash uchun yana bir marta bosing");
      setTimeout(() => { armed.delete(key); show(); }, 3000);
      return;
    }
    armed.delete(key);
    button.disabled = true;
    const res = await fetch(`/api/documents/${encodeURIComponent(name)}/rollback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail;
      if (view.notice) view.notice.remove();
      view.notice = el("span", "err", `Qaytib bo'lmadi: ${detail || `HTTP ${res.status}`}`);
      view.notice.setAttribute("role", "alert");
      view.panel.insertBefore(view.notice, view.list);
      button.disabled = false;
      return;
    }
    // The panel first, then the list: the panel is asked for at once and the header (which comes with the list) is
    // updated only after the panel shows the restored version, so the header can never be ahead of the panel.
    await refreshPanel(view);
    refreshDocs();
  });
  return button;
}

function deleteButton(name) {
  const button = el("button", "danger", "O'chirish");
  button.dataset.focusKey = `doc:${name}:delete`;
  const show = (armedNow) => {
    setText(button, armedNow ? "Aniqmi?" : "O'chirish");
    setAttr(button, "aria-label", `${armedNow ? "Aniqmi? " : ""}O'chirish: ${name}`);
  };
  show(false);
  let armedNow = false;
  button.addEventListener("click", async () => {
    if (!armedNow) {
      armedNow = true;
      show(true);
      announce("Tasdiqlash uchun yana bir marta bosing");
      setTimeout(() => { armedNow = false; show(false); }, 3000);
      return;
    }
    button.disabled = true;
    await fetch(`/api/documents/${encodeURIComponent(name)}`, { method: "DELETE" });
    refreshDocs();
  });
  return button;
}

// Every file is sent in a request of its own, one after the other (the server limits the size of a whole request, and a
// failure of one file must not take the others with it). A file is handed to the server, and so to the indexing, as soon
// as its own request has finished; whatever goes wrong with one file is reported under its name and the rest go on.
let uploadQueue = Promise.resolve(); // a selection made while an upload is running waits for it: never two at once
let uploadRun = 0;

function uploadFiles(files) {
  if (!files.length) return uploadQueue;
  uploadQueue = uploadQueue.then(() => uploadSequence(files));
  return uploadQueue;
}

async function sendOne(file) {
  const form = new FormData();
  form.append("files", file);
  try {
    const res = await fetch("/api/documents", { method: "POST", body: form });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail; // e.g. 413: larger than the limit, 411: no length
      return { name: file.name, ok: false, error: detail || `HTTP ${res.status}` };
    }
    const result = (await res.json()).results[0]; // one file was sent: one result
    if (!result || !result.accepted) return { name: file.name, ok: false, error: (result && result.error) || "Qabul qilinmadi" };
    return { name: file.name, ok: true };
  } catch (error) {
    return { name: file.name, ok: false, error: `Server bilan aloqa uzildi (${error.message})` };
  }
}

function showUploadProgress(done, total, text) {
  const bar = $("upload-progress");
  bar.hidden = false;
  setAttr(bar, "max", total);
  setAttr(bar, "value", done);
  setAttr(bar, "aria-valuemin", 0);
  setAttr(bar, "aria-valuemax", total);
  setAttr(bar, "aria-valuenow", done);
  setAttr(bar, "aria-valuetext", text);
  $("upload-status").hidden = false;
  setText($("upload-status"), text); // a polite live region: it is read when it changes (once per file)
}

async function uploadSequence(files) {
  const run = ++uploadRun;
  const msg = $("upload-msg");
  msg.hidden = true;
  msg.textContent = "";
  const results = [];
  for (const [index, file] of files.entries()) {
    showUploadProgress(index, files.length, `${index + 1}/${files.length} fayl yuklanmoqda: ${file.name}`);
    results.push(await sendOne(file));
    refreshDocs(); // the document appears in the list (and the worker can start on it) without waiting for the others
  }
  const failed = results.filter((r) => !r.ok);
  const summary = failed.length === 0
    ? `${files.length}/${files.length} fayl yuklandi`
    : `${results.length - failed.length} ta fayl muvaffaqiyatli, ${failed.length} ta faylda xatolik`;
  showUploadProgress(files.length, files.length, summary);
  if (failed.length) {
    msg.hidden = false;
    msg.textContent = failed.map((r) => `${r.name}: ${r.error}`).join(" · "); // each error under the file's own name
  } else {
    setTimeout(() => { // a success needs no permanent line; a newer upload keeps its own
      if (run === uploadRun) { $("upload-status").hidden = true; $("upload-progress").hidden = true; }
    }, 6000);
  }
  refreshDocs();
}

const drop = $("drop");
drop.addEventListener("click", (e) => { if (e.target.tagName !== "INPUT") $("file-input").click(); });
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file-input").click(); } });
$("file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
for (const type of ["dragenter", "dragover"]) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
for (const type of ["dragleave", "drop"]) drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove("over"); });
drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
// A file dropped outside the drop zone must not make the browser navigate away from the page.
for (const type of ["dragover", "drop"]) window.addEventListener(type, (e) => e.preventDefault());

// ---------- chat ----------
function addMessage(kind, text) {
  $("hint")?.remove();
  const node = el("div", `msg ${kind}`, text);
  $("log").append(node);
  $("log").scrollTop = $("log").scrollHeight;
  return node;
}

function renderSources(node, sources) {
  const details = el("details", "sources");
  details.append(el("summary", "", `Manbalar (${sources.length})`));
  for (const s of sources) {
    const box = el("div", "source");
    const pages = s.pages.length ? ` · sahifa ${s.pages.join(", ")}` : "";
    box.append(el("div", "meta", `[${s.n}] ${s.file}${pages}`));
    box.append(el("div", "", s.text));
    details.append(box);
  }
  node.append(details);
}

// While an answer is being written the log is "busy": a screen reader waits and reads it once when it is finished,
// instead of reading every few words as they arrive.
function setBusy(busy) {
  $("log").setAttribute("aria-busy", String(busy));
}

async function ask(question) {
  addMessage("user", question);
  const bot = addMessage("bot", "…");
  let text = "";
  setBusy(true);
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail;
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = done ? "" : lines.pop();
      for (const line of lines.filter(Boolean)) {
        const event = JSON.parse(line);
        if (event.token !== undefined) {
          text += event.token;
          bot.textContent = text;
        } else if (event.error) {
          throw new Error(event.error);
        } else if (event.final !== undefined) {
          bot.textContent = event.final;
          if (event.truncated) bot.append(el("div", "muted", "Javob uzunlik chegarasiga yetdi va shu yerda uzildi."));
          if (event.sources.length) renderSources(bot, event.sources);
        }
        $("log").scrollTop = $("log").scrollHeight;
      }
      if (done) break;
    }
  } catch (error) {
    setBusy(false); // before the alert is added, so that it is spoken at once
    bot.classList.add("error");
    const alert = el("span", "", `Xato: ${error.message}`); // "Xato:" in words: the red colour is not the only sign
    alert.setAttribute("role", "alert");
    bot.replaceChildren(alert);
  } finally {
    setBusy(false);
  }
}

$("ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  $("question").value = "";
  $("send").disabled = true;
  await ask(question);
  $("send").disabled = false;
  // Give the focus back only if it was lost (it sat on the send button, which was disabled meanwhile) or is still on the
  // question. If the user has moved to something else while the answer was written, that choice stands.
  const now = document.activeElement;
  if (!now || now === document.body || now === $("send") || now === $("question")) $("question").focus();
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("ask-form").requestSubmit(); }
});

// ---------- connector sign-in (roadmap M4.1a, ADR-P16) ----------
// Only the connector's own requests (/api/v1/...) carry the session token. It lives in sessionStorage (this tab only, gone
// when the tab is closed): never in localStorage, a cookie or the address. The document and ask requests above never see
// it. When the application runs with the connector off, /api/v1/session does not exist (404) and this section stays hidden.
const SESSION_KEY = "rag.session";
let sessionTimer = null;
let connectorEnabled = null; // learned once from GET /api/v1/connector, which always answers 200

function storedToken() {
  try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; }
}

function storeToken(token) {
  try {
    if (token) sessionStorage.setItem(SESSION_KEY, token); else sessionStorage.removeItem(SESSION_KEY);
  } catch { /* storage unavailable: the user will have to pair again */ }
}

function connectorFetch(path, options = {}) {
  if (!path.startsWith("/api/v1/")) throw new Error("the session token is only for /api/v1");
  const headers = { ...(options.headers || {}) };
  const token = storedToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return fetch(path, { ...options, headers });
}

function connectorMessage(text) {
  const box = $("connector-msg");
  box.hidden = !text;
  setText(box, text || "");
}

// mode: "hidden" | "pair" | "session" | "unavailable"
function showConnector(mode, info) {
  $("connector-auth").hidden = mode === "hidden";
  $("pair-box").hidden = mode !== "pair";
  $("session-box").hidden = mode !== "session";
  const states = {
    unavailable: "Ulagich hozir ishlamayapti (xavfsizlik shartlari bajarilmagan). Ilova oynasidagi xabarga qarang.",
    pair: "Ulagichdan foydalanish uchun kod bilan kiring.",
    session: "Ulagichga kirilgan.",
  };
  setText($("connector-state"), states[mode] || "");
  if (mode === "session") setText($("session-info"), `Sessiya: ${Math.ceil(info.absolute_remaining_s / 60)} daqiqa qoldi`);
}

// Asking where the session stands does not keep it alive: the server counts it as a look, not a use.
async function refreshSession() {
  clearTimeout(sessionTimer);
  let delay = 15000;
  try {
    if (connectorEnabled === null) {
      const found = await fetch("/api/v1/connector"); // public; says only whether the connector is on
      connectorEnabled = found.ok && (await found.json()).enabled === true;
    }
    if (!connectorEnabled) {
      showConnector("hidden"); // off: nothing to show, nothing to poll
      return;
    }
    const hadToken = Boolean(storedToken());
    const res = await connectorFetch("/api/v1/session");
    if (res.status === 200) {
      showConnector("session", await res.json());
    } else if (res.status === 401) {
      storeToken(null);
      showConnector("pair");
      if (hadToken) {
        connectorMessage("Sessiya tugadi. Qayta kod bilan kiring.");
        announce("Sessiya tugadi. Qayta kod bilan kiring.");
      }
    } else if (res.status === 503) {
      showConnector("unavailable");
    } else {
      showConnector("hidden"); // anything else: nothing usable here
      return;
    }
  } catch {
    delay = 30000; // no answer: keep what is shown and ask again later
  }
  sessionTimer = setTimeout(refreshSession, delay);
}

$("pair-request").addEventListener("click", async () => {
  connectorMessage("");
  try {
    const res = await connectorFetch("/api/v1/session/pairing", { method: "POST" });
    if (res.status === 200) {
      announce("Kod ilova oynasida ko'rsatildi.");
      $("pair-code").focus();
    } else if (res.status === 429) {
      connectorMessage("Juda tez. Bir necha soniyadan keyin qayta so'rang.");
    } else if (res.status === 503) {
      showConnector("unavailable");
    } else {
      connectorMessage("Kod so'rab bo'lmadi.");
    }
  } catch {
    connectorMessage("Server bilan aloqa yo'q.");
  }
});

$("pair-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("pair-code").value.trim();
  $("pair-code").value = ""; // the code is never kept, whatever the answer
  if (!code) return;
  connectorMessage("");
  try {
    const res = await connectorFetch("/api/v1/session/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (res.status === 200) {
      storeToken((await res.json()).token);
      announce("Ulagichga kirildi.");
      await refreshSession();
      if (!$("session-box").hidden) $("session-end").focus();
    } else if (res.status === 401) {
      connectorMessage("Kod noto'g'ri yoki eskirgan. Yangi kod so'rang.");
    } else if (res.status === 429) {
      connectorMessage("Juda ko'p urinish. Biroz kutib, yangi kod so'rang.");
    } else if (res.status === 503) {
      showConnector("unavailable");
    } else {
      connectorMessage("Kirib bo'lmadi.");
    }
  } catch {
    connectorMessage("Server bilan aloqa yo'q.");
  }
});

$("session-end").addEventListener("click", async () => {
  try {
    await connectorFetch("/api/v1/session", { method: "DELETE" });
  } catch { /* the server forgets the session at the latest when it expires */ }
  storeToken(null);
  connectorMessage("");
  announce("Sessiya tugatildi.");
  await refreshSession();
  if (!$("pair-box").hidden) $("pair-request").focus();
});

refreshDocs();
refreshSession();
