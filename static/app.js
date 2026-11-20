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
  onConnectorMode(mode);
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

// ---------- external database connections (roadmap M4.1b, docs/connector.md) ----------
// Everything here talks only to /api/v1/connections through connectorFetch (the session token never goes anywhere else). The
// password is typed into a field, sent once and cleared; it is never shown, stored in the page or read back (the server never
// returns it). All texts are set with textContent: names, hosts and descriptions of external databases are not trusted.
const CN_TEXT = {
  invalid_provider: "Baza turi noto'g'ri.", invalid_host: "Host noto'g'ri.", invalid_port: "Port noto'g'ri (1–65535).",
  invalid_database: "Baza nomi noto'g'ri.", invalid_username: "Foydalanuvchi nomi noto'g'ri.",
  invalid_credential: "Parol noto'g'ri (1–1024 belgi).", invalid_ssl_mode: "Shifrlash rejimi noto'g'ri yoki bu bazada mavjud emas.",
  invalid_options: "Qo'shimcha sozlama noto'g'ri.", invalid_name: "Nom noto'g'ri (1–100 belgi).",
  invalid_description: "Tavsif noto'g'ri.", invalid_target: "Tavsif uchun obyekt noto'g'ri.", invalid_request: "So'rov noto'g'ri.",
  tls_required: "Boshqa kompyuterdagi baza uchun shifrlash va sertifikat tekshiruvi kerak. Zaif rejimni tanlasangiz, xavfni qabul qilganingizni belgilang.",
  ca_required: "SQL Server uchun serverning CA sertifikati (PEM) kerak.", host_blocked: "Bu manzilga ulanishga ruxsat yo'q.",
  host_not_allowed: "Bu host ruxsat etilgan ro'yxatda yo'q.", host_unresolvable: "Host topilmadi.",
  internal_database: "Bu ilovaning o'z bazasi: uni tashqi baza sifatida ulab bo'lmaydi.", name_taken: "Bu nom band.",
  limit_reached: "Ulanishlar soni chegarasiga yetildi.", not_found: "Topilmadi.", object_not_found: "Bunday jadval yoki ustun topilmadi.",
  connection_disabled: "Ulanish o'chirilgan. Avval yoqing.", needs_credentials: "Parolni qayta kiriting (shifrlash kaliti bilan ochilmadi).",
  key_unavailable: "Shifrlash kaliti yo'q yoki o'qib bo'lmaydi. Ilova oynasida: python -m rag connector key-init",
  busy: "Band. Biroz kutib, qayta urining.", security_log_unavailable: "Xavfsizlik jurnaliga yozib bo'lmadi: o'zgartirish qilinmadi.",
  discovery_failed: "Sxemani aniqlab bo'lmadi.", connector_unavailable: "Ulagich hozir ishlamayapti.",
  step_up_required: "Tasdiqlash kerak.",
  auth_failed: "Foydalanuvchi yoki parol noto'g'ri.", database_not_found: "Baza topilmadi.", timeout: "Javob kelmadi (vaqt tugadi).",
  unreachable: "Serverga ulanib bo'lmadi.", tls_failed: "Shifrlash yoki sertifikat xatosi.", permission_denied: "Ruxsat yo'q.",
  too_large: "Sxema juda katta.", driver_missing: "Kerakli drayver o'rnatilmagan (requirements-connector.txt).", failed: "Xato.",
  context_stale: "Sxema konteksti hozir yangi emas.", selection_conflict: "Tanlov boshqa joyda o'zgartirilgan. Sahifani yangilab, qayta urining.",
  snapshot_changed: "Sxema yangilangan. Sahifani yangilang.", snapshot_not_found: "Bunday sxema versiyasi yo'q.",
  invalid_selection: "Tanlov noto'g'ri.", description_contains_secret: "Tavsifda parol yoki kalit bor: yozish mumkin emas.",
  request_too_large: "So'rov juda katta.", not_in_snapshot: "Bu obyekt joriy sxemada yo'q.", stale: "Sxema eskirgan.",
  failed: "Xato.", in_progress: "Sxema aniqlanmoqda.", never_discovered: "Sxema hali aniqlanmagan.",
  exclude_without_scope: "Jadvalni chiqarib tashlash uchun uning sxemasi tanlangan bo'lishi kerak.",
  exclude_without_table: "Ustunni chiqarib tashlash uchun butun jadval tanlangan bo'lishi kerak.",
  account_can_write: "Diqqat: bu hisob ma'lumotni o'zgartira oladi. Faqat o'qish huquqli hisobdan foydalaning.",
};
const CN_STATUS = { ready: "Tayyor", disabled: "O'chirilgan", needs_credentials: "Parol kerak" };
const CN_PROVIDER = { postgresql: "PostgreSQL", mssql: "SQL Server" };
const CN_PORT = { postgresql: 5432, mssql: 1433 };
let cnConnections = [];
let cnLoaded = false;
let cnEditing = null; // the connection being edited, or null for a new one
let cnPending = null; // the action that waits for a step-up confirmation
let cnSchema = null; // {connection, schema, query, offset}
let cnSel = null; // {revision, items, dirty}: the selection being edited (what may appear in AI context)
let cnFresh = null; // the last freshness answer
const cnArmed = new Set();

function cnCodeText(code, reason) {
  const base = CN_TEXT[code] || "Xato.";
  return reason && CN_TEXT[reason] ? `${base} ${CN_TEXT[reason]}` : base;
}

function cnMessage(text, kind) {
  const box = $("cn-msg");
  box.hidden = !text;
  setText(box, text || "");
  setClass(box, `upload-msg${kind === "ok" ? " ok" : ""}`);
}

function cnWhen(value) {
  return value ? new Date(value).toLocaleString() : "";
}

async function cnApi(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await connectorFetch(`/api/v1/connections${path}`, options);
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  return { status: res.status, data };
}

// Runs an action; 401 ends the session, 403 step_up_required asks for a fresh code and runs the action again afterwards.
async function cnRun(action, report = cnMessage) {
  let result;
  try {
    result = await action();
  } catch {
    report("Server bilan aloqa yo'q.");
    return null;
  }
  const code = result.data && result.data.error ? result.data.error.code : null;
  if (result.status === 401) {
    storeToken(null);
    await refreshSession();
    return null;
  }
  if (result.status === 403 && code === "step_up_required") {
    cnPending = action;
    $("cn-stepup").hidden = false;
    cnMessage("");
    announce("Tasdiqlash uchun yangi kod kerak.");
    $("cn-stepup-request").focus();
    return null;
  }
  if (result.status >= 400) {
    report(cnCodeText(code, result.data && result.data.error ? result.data.error.reason : null));
    return null;
  }
  return result;
}

$("cn-stepup-request").addEventListener("click", async () => {
  try {
    const res = await connectorFetch("/api/v1/session/pairing", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ purpose: "step-up" }),
    });
    if (res.status === 200) {
      announce("Kod ilova oynasida ko'rsatildi.");
      $("cn-stepup-code").focus();
    } else {
      cnMessage(res.status === 429 ? "Juda tez. Bir necha soniyadan keyin qayta so'rang." : "Kod so'rab bo'lmadi.");
    }
  } catch {
    cnMessage("Server bilan aloqa yo'q.");
  }
});

$("cn-stepup-cancel").addEventListener("click", () => {
  cnPending = null;
  $("cn-stepup").hidden = true;
  $("cn-stepup-code").value = "";
});

$("cn-stepup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("cn-stepup-code").value.trim();
  $("cn-stepup-code").value = ""; // never kept, whatever the answer
  if (!code) return;
  try {
    const res = await connectorFetch("/api/v1/session/step-up", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
    });
    if (res.status === 200) {
      storeToken((await res.json()).token); // the confirmation replaces the token
      $("cn-stepup").hidden = true;
      const again = cnPending;
      cnPending = null;
      if (again) await cnPerform(again);
    } else if (res.status === 401) {
      cnMessage("Kod noto'g'ri yoki eskirgan. Yangi kod so'rang.");
    } else {
      cnMessage(res.status === 429 ? "Juda ko'p urinish. Biroz kutib, yangi kod so'rang." : "Tasdiqlab bo'lmadi.");
    }
  } catch {
    cnMessage("Server bilan aloqa yo'q.");
  }
});

// Runs an action and, when it succeeded, what it wants done afterwards (`action.after`). A step-up that interrupts it makes
// it run again later through here, so the follow-up is never lost.
async function cnPerform(action) {
  const result = await cnRun(action);
  if (result && action.after) await action.after(result);
  return result;
}

// ----- the list -----

let cnLoading = false; // a second look while the first is still on its way would draw the list twice
async function refreshConnections() {
  if (cnLoading) return;
  cnLoading = true;
  try {
    const result = await cnRun(() => cnApi("GET", ""));
    if (!result) return;
    cnConnections = result.data.connections;
    cnLoaded = true;
    renderConnections();
  } finally {
    cnLoading = false;
  }
}

function connectionSummary(c) {
  return `${CN_PROVIDER[c.provider] || c.provider} · ${c.host}:${c.port} / ${c.database_name} · ${c.username}`;
}

function testLine(c) {
  if (!c.last_tested_at) return "Hali sinalmagan";
  const when = cnWhen(c.last_tested_at);
  return c.last_test_status === "ok" ? `Oxirgi sinov: muvaffaqiyatli (${when})` : `Oxirgi sinov: xato, ${cnCodeText(c.last_test_error)} (${when})`;
}

function cnButton(label, onClick, extra) {
  const button = el("button", extra || "", label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function connectionRow(c) {
  const row = el("li", "cn-item");
  row.dataset.id = c.id;
  const head = el("div", "cn-head");
  head.append(el("strong", "cn-name", c.name), el("span", `cn-status ${c.status}`, CN_STATUS[c.status] || c.status));
  const lines = [el("div", "muted", connectionSummary(c)), el("div", "muted", testLine(c))];
  if (c.description) lines.push(el("div", "cn-desc", c.description));
  if (c.last_test_warning) lines.push(el("div", "update warn", cnCodeText(c.last_test_warning)));
  lines.push(el("div", "muted", c.schema_discovered_at ? `Sxema aniqlangan: ${cnWhen(c.schema_discovered_at)}` : "Sxema hali aniqlanmagan"));
  const actions = el("div", "cn-actions");
  actions.append(
    cnButton("Sinash", () => testConnection(c)),
    cnButton(c.is_enabled ? "O'chirib qo'yish" : "Yoqish", () => toggleConnection(c)),
    cnButton("Tahrirlash", () => editConnection(c)),
    cnButton("Sxemani aniqlash", () => discoverSchema(c)),
    cnButton("Sxemani ko'rish", () => openSchema(c)),
    deleteConnectionButton(c),
  );
  row.append(head, ...lines, actions);
  setAttr(row, "aria-label", `Ulanish: ${c.name}`);
  return row;
}

function renderConnections() {
  const list = $("cn-list");
  list.replaceChildren(...cnConnections.map(connectionRow));
  $("cn-empty").hidden = cnConnections.length > 0;
  sgRender();
}

async function testConnection(c) {
  cnMessage("Sinalmoqda…", "ok");
  const result = await cnRun(() => cnApi("POST", `/${c.id}/test`));
  if (!result) return;
  const t = result.data;
  cnMessage(t.status === "ok" ? `Ulanish ishlayapti${t.server_version ? ` (server ${t.server_version})` : ""}.${t.warning ? " " + cnCodeText(t.warning) : ""}`
    : cnCodeText(t.error), t.status === "ok" ? "ok" : undefined);
  announce(t.status === "ok" ? "Ulanish ishlayapti." : "Ulanish sinovi muvaffaqiyatsiz.");
  await refreshConnections();
}

async function toggleConnection(c) {
  const result = await cnRun(() => cnApi("POST", `/${c.id}/${c.is_enabled ? "disable" : "enable"}`));
  if (result) await refreshConnections();
}

function deleteConnectionButton(c) {
  const button = el("button", "danger", "O'chirish");
  button.type = "button";
  button.addEventListener("click", async () => {
    if (!cnArmed.has(c.id)) {
      cnArmed.add(c.id);
      setText(button, "Aniqmi?");
      announce("Tasdiqlash uchun yana bir marta bosing");
      setTimeout(() => { cnArmed.delete(c.id); setText(button, "O'chirish"); }, 3000);
      return;
    }
    cnArmed.delete(c.id);
    const result = await cnRun(() => cnApi("DELETE", `/${c.id}`));
    if (result) {
      if (cnSchema && cnSchema.connection.id === c.id) closeSchema();
      await refreshConnections();
    }
  });
  return button;
}

async function discoverSchema(c) {
  cnMessage("Sxema aniqlanmoqda…", "ok");
  const result = await cnRun(() => cnApi("POST", `/${c.id}/schema/discover`));
  if (!result) return;
  const snap = result.data.snapshot;
  const what = !snap ? "" : snap.first ? " Birinchi versiya saqlandi." : snap.changed ? ` Yangi versiya ${snap.version}: ${snap.changes} o'zgarish.` : ` O'zgarish yo'q (versiya ${snap.version}).`;
  cnMessage(`Sxema aniqlandi: ${result.data.objects} jadval va ko'rinish, ${result.data.columns} ustun.${what}`, "ok");
  announce("Sxema aniqlandi.");
  await refreshConnections();
  if (cnSchema && cnSchema.connection.id === c.id) await openSchema(cnConnections.find((x) => x.id === c.id) || c);
}

// ----- the form -----

function showForm(connection) {
  cnEditing = connection;
  const form = $("cn-form");
  form.hidden = false;
  $("cn-form-title").textContent = connection ? `Tahrirlash: ${connection.name}` : "Yangi ulanish";
  $("cn-name").value = connection ? connection.name : "";
  $("cn-description").value = connection ? connection.description : "";
  $("cn-provider").value = connection ? connection.provider : "postgresql";
  $("cn-host").value = connection ? connection.host : "";
  $("cn-port").value = connection ? connection.port : CN_PORT.postgresql;
  $("cn-database").value = connection ? connection.database_name : "";
  $("cn-username").value = connection ? connection.username : "";
  $("cn-password").value = "";
  setAttr($("cn-password"), "placeholder", connection ? "(o'zgartirilmaydi)" : "");
  $("cn-ssl").value = connection ? connection.ssl_mode : "verify-full";
  $("cn-insecure").checked = Boolean(connection && connection.options.accept_insecure_tls);
  $("cn-ca").value = "";
  setAttr($("cn-ca"), "placeholder", connection && connection.ca_certificate_set ? "(sertifikat saqlangan; yangisini kiritsangiz almashadi)" : "-----BEGIN CERTIFICATE-----");
  $("cn-timeout").value = connection && connection.options.connect_timeout_s ? connection.options.connect_timeout_s : "";
  cnMessage("");
  $("cn-name").focus();
}

function hideForm() {
  $("cn-form").hidden = true;
  $("cn-password").value = "";
  cnEditing = null;
}

function editConnection(c) {
  showForm(c);
}

$("cn-new").addEventListener("click", () => showForm(null));
$("cn-cancel").addEventListener("click", () => { hideForm(); $("cn-new").focus(); });
$("cn-provider").addEventListener("change", () => {
  if (!cnEditing) $("cn-port").value = CN_PORT[$("cn-provider").value];
});

function formValues() {
  const options = {};
  const timeout = $("cn-timeout").value.trim();
  if (timeout) options.connect_timeout_s = Number(timeout);
  if ($("cn-insecure").checked) options.accept_insecure_tls = true;
  const ca = $("cn-ca").value.trim();
  if (ca) options.ca_certificate_pem = ca;
  return {
    name: $("cn-name").value, description: $("cn-description").value, provider: $("cn-provider").value, host: $("cn-host").value,
    port: Number($("cn-port").value), database_name: $("cn-database").value, username: $("cn-username").value,
    ssl_mode: $("cn-ssl").value, options,
  };
}

// The fields that differ from the stored connection. A stored CA certificate is not sent back (the server keeps it unless a new
// one is given), and the password only when something was typed.
function changedFields(values, stored) {
  const changed = {};
  for (const key of ["name", "description", "provider", "host", "port", "database_name", "username", "ssl_mode"]) {
    if (values[key] !== stored[key]) changed[key] = values[key];
  }
  const keep = { ...values.options };
  const had = { ...stored.options };
  if (JSON.stringify(Object.entries(keep).filter(([k]) => k !== "ca_certificate_pem").sort())
      !== JSON.stringify(Object.entries(had).sort()) || keep.ca_certificate_pem) {
    changed.options = keep;
  }
  return changed;
}

$("cn-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const values = formValues();
  const password = $("cn-password").value;
  const editing = cnEditing;
  let action;
  if (editing) {
    const body = changedFields(values, editing);
    if (password) body.password = password;
    if (!Object.keys(body).length) {
      hideForm();
      cnMessage("Hech narsa o'zgarmadi.");
      return;
    }
    action = () => cnApi("PATCH", `/${editing.id}`, body);
  } else {
    action = () => cnApi("POST", "", { ...values, password });
  }
  action.after = async () => {
    hideForm();
    cnMessage(editing ? "Ulanish yangilandi." : "Ulanish saqlandi.", "ok");
    announce(editing ? "Ulanish yangilandi." : "Ulanish saqlandi.");
    await refreshConnections();
  };
  const sent = await cnPerform(action);
  if (sent) $("cn-password").value = ""; // the password is gone from the page as soon as it was sent
});

// ----- the schema browser -----

function closeSchema() {
  cnSchema = null;
  cnSel = null;
  cnFresh = null;
  $("cn-schema").hidden = true;
  for (const id of ["cn-changes", "cn-context", "cn-unavailable", "cn-orphans"]) { $(id).hidden = true; $(id).replaceChildren(); }
  $("cn-fresh").replaceChildren();
}

async function openSchema(c) {
  const [schemas, descriptions] = await Promise.all([
    cnRun(() => cnApi("GET", `/${c.id}/schema`)), cnRun(() => cnApi("GET", `/${c.id}/descriptions`)),
  ]);
  if (!schemas || !descriptions) return;
  cnSchema = { connection: c, schemas: schemas.data.schemas, descriptions: descriptions.data.descriptions, schema: null, query: "", offset: 0 };
  $("cn-schema").hidden = false;
  $("cn-schema-title").textContent = `Sxema: ${c.name}`;
  for (const id of ["cn-changes", "cn-context"]) { $(id).hidden = true; $(id).replaceChildren(); }
  await loadSelection();
  await loadFreshness();
  renderSchemaTop();
  renderOrphans();
  await loadObjects(true);
  $("cn-schema").scrollIntoView({ block: "nearest" });
}

function descriptionOf(kind, schema, object, column) {
  const found = cnSchema.descriptions.find((d) => d.target_kind === kind && d.schema_name === (schema || "")
    && d.object_name === (object || "") && d.column_name === (column || ""));
  return found ? found.description : "";
}

async function saveDescription(kind, schema, object, column, text, field) {
  const connection = cnSchema.connection;
  const result = await cnRun(() => cnApi("PUT", `/${connection.id}/descriptions`, {
    target_kind: kind, schema_name: schema || "", object_name: object || "", column_name: column || "", description: text,
  }));
  if (!result) return;
  cnSchema.descriptions = cnSchema.descriptions.filter((d) => !(d.target_kind === kind && d.schema_name === (schema || "")
    && d.object_name === (object || "") && d.column_name === (column || "")));
  if (text) cnSchema.descriptions.push(result.data);
  cnMessage(text ? "Tavsif saqlandi." : "Tavsif o'chirildi.", "ok");
  announce(text ? "Tavsif saqlandi." : "Tavsif o'chirildi.");
  if (field) field.dataset.saved = text;
}

function descriptionEditor(label, kind, schema, object, column) {
  const wrap = el("div", "cn-desc-edit");
  const id = `cn-d-${Math.random().toString(36).slice(2, 9)}`;
  const text = el("label", "muted", label);
  text.htmlFor = id;
  const field = el("textarea");
  field.id = id;
  field.rows = 2;
  field.maxLength = 4000;
  field.value = descriptionOf(kind, schema, object, column);
  field.dataset.saved = field.value;
  const save = cnButton("Saqlash", () => saveDescription(kind, schema, object, column, field.value.trim(), field));
  wrap.append(text, field, save);
  return wrap;
}

function renderSchemaTop() {
  const top = $("cn-schema-top");
  const items = [descriptionEditor("Baza tavsifi", "database", "", "", "")];
  const bar = el("div", "cn-actions");
  const all = cnButton("Hamma sxema", () => selectSchema(null), cnSchema.schema === null ? "primary" : "");
  bar.append(all);
  for (const s of cnSchema.schemas) {
    bar.append(selectionBox(`${s.schema_name} sxemasini AI kontekstiga qo'shish`, { kind: "schema", schema: s.schema_name, object: "", column: "" }),
      cnButton(`${s.schema_name} (${s.tables} + ${s.views})`, () => selectSchema(s.schema_name), cnSchema.schema === s.schema_name ? "primary" : ""));
  }
  items.push(bar);
  if (cnSchema.schema) items.push(descriptionEditor(`Sxema tavsifi: ${cnSchema.schema}`, "schema", cnSchema.schema, "", ""));
  if (!cnSchema.schemas.length) items.push(el("p", "muted", "Sxema hali aniqlanmagan yoki hech narsa ko'rinmaydi. «Sxemani aniqlash» ni bosing."));
  top.replaceChildren(...items);
  syncChecks();
}

async function selectSchema(name) {
  cnSchema.schema = name;
  cnSchema.offset = 0;
  renderSchemaTop();
  await loadObjects(true);
}

async function loadObjects(reset) {
  const c = cnSchema.connection;
  if (reset) cnSchema.offset = 0;
  const params = new URLSearchParams({ limit: "100", offset: String(cnSchema.offset) });
  if (cnSchema.schema) params.set("schema", cnSchema.schema);
  if (cnSchema.query) params.set("q", cnSchema.query);
  const result = await cnRun(() => cnApi("GET", `/${c.id}/schema/objects?${params}`));
  if (!result) return;
  const list = $("cn-objects");
  const rows = result.data.objects.map((o) => {
    const li = el("li");
    const button = cnButton(`${o.schema_name}.${o.object_name}`, () => showObject(o.schema_name, o.object_name), "link");
    li.append(selectionBox(`${o.schema_name}.${o.object_name} ni AI kontekstiga qo'shish`, { kind: "table", schema: o.schema_name, object: o.object_name, column: "" }), button, el("span", "muted", ` ${o.object_type === "table" ? "jadval" : "ko'rinish"} · ${o.columns} ustun`));
    return li;
  });
  if (reset) list.replaceChildren(...rows); else list.append(...rows);
  syncChecks();
  cnSchema.offset += result.data.objects.length;
  $("cn-more").hidden = cnSchema.offset >= result.data.total;
  setText($("cn-objects-total"), `${result.data.total} ta obyekt`);
  if (reset) $("cn-object").replaceChildren();
}

$("cn-search").addEventListener("input", () => {
  if (!cnSchema) return;
  cnSchema.query = $("cn-search").value.trim();
  loadObjects(true);
});
$("cn-more").addEventListener("click", () => loadObjects(false));
$("cn-schema-close").addEventListener("click", closeSchema);

function keyMarks(detail) {
  const marks = new Map();
  const add = (column, mark) => marks.set(column, [...(marks.get(column) || []), mark]);
  for (const k of detail.constraints) {
    for (const column of k.columns) add(column, { primary_key: "PK", unique: "U", foreign_key: "FK" }[k.kind]);
  }
  return marks;
}

async function showObject(schema, name) {
  const c = cnSchema.connection;
  const params = new URLSearchParams({ schema, name });
  const result = await cnRun(() => cnApi("GET", `/${c.id}/schema/object?${params}`));
  if (!result) return;
  const d = result.data;
  const marks = keyMarks(d);
  const box = $("cn-object");
  const title = el("h3", "", `${d.schema}.${d.name} (${d.type === "table" ? "jadval" : "ko'rinish"})`);
  const table = el("table", "cn-table");
  const head = el("tr");
  for (const h of ["AI", "Ustun", "Tur", "Bo'sh bo'lishi", "Kalit", "Tavsif"]) head.append(el("th", "", h));
  table.append(head);
  for (const col of d.columns) {
    const tr = el("tr");
    const pick = el("td");
    pick.append(selectionBox(`${d.name}.${col.column_name} ustunini AI kontekstiga qo'shish`, { kind: "column", schema: d.schema, object: d.name, column: col.column_name }));
    tr.append(pick, el("td", "", col.column_name), el("td", "", col.data_type), el("td", "", col.is_nullable ? "ha" : "yo'q"),
      el("td", "", (marks.get(col.column_name) || []).join(", ")));
    const cell = el("td");
    cell.append(descriptionEditor(`${col.column_name} tavsifi`, "column", d.schema, d.name, col.column_name));
    tr.append(cell);
    table.append(tr);
  }
  const parts = [title, descriptionEditor("Jadval tavsifi", "table", d.schema, d.name, ""), table];
  const keys = el("ul", "cn-keys");
  for (const k of d.constraints) {
    const label = { primary_key: "Birlamchi kalit", unique: "Noyob", foreign_key: "Tashqi kalit" }[k.kind];
    const target = k.kind === "foreign_key" ? ` → ${k.ref_schema}.${k.ref_object} (${k.ref_columns.join(", ")})` : "";
    keys.append(el("li", "", `${label} ${k.constraint_name}: (${k.columns.join(", ")})${target}`));
  }
  if (!d.constraints.length) keys.append(el("li", "muted", "Kalit yo'q."));
  parts.push(el("h4", "", "Kalitlar"), keys);
  if (d.indexes.length) {
    const indexes = el("ul", "cn-keys");
    for (const i of d.indexes) indexes.append(el("li", "", `${i.index_name}: (${i.columns.join(", ")})${i.is_unique ? " · noyob" : ""}${i.is_primary ? " · birlamchi" : ""}`));
    parts.push(el("h4", "", "Indekslar"), indexes);
  }
  box.replaceChildren(...parts);
  syncChecks();
}

// ----- freshness, changes, selection and the context preview (roadmap M4.2) -----
// The backend decides everything that matters here: the rules of the selection are checked again by PUT …/schema/selections and
// the context is built by GET …/schema/context. This code only edits a list of items, shows the answers and never builds the
// context itself. Items of the selection are sent without the "available" flag the server adds.
const FRESH_TEXT = {
  current: "Yangi", stale: "Eskirgan", failed: "Oxirgi aniqlash xato bilan tugadi", in_progress: "Aniqlanmoqda…", never_discovered: "Hali aniqlanmagan",
};
const REASON_TEXT = {
  unreviewed_changes: "tanlangan obyektlar o'zgargan", removed_selected: "tanlangan obyekt endi yo'q", age: "uzoq vaqt tasdiqlanmagan",
  last_discovery_failed: "oxirgi aniqlash xato bilan tugagan", target_changed: "ulanish boshqa bazaga yo'naltirilgan: sxemani qayta aniqlang",
};
const CHANGE_HOW = { added: "qo'shildi", removed: "o'chdi", modified: "o'zgardi" };
const CHANGE_KIND = { schema: "sxema", table: "jadval", view: "ko'rinish", column: "ustun", relationship: "tashqi kalit", constraint: "cheklov", index: "indeks" };

function selItem(i) { return { kind: i.kind, schema: i.schema, object: i.object || "", column: i.column || "", mode: i.mode || "include" }; }
function selFind(kind, schema, object, column, mode) {
  return cnSel.items.find((i) => i.kind === kind && i.schema === schema && i.object === (object || "") && i.column === (column || "") && i.mode === mode);
}
function selDrop(item) { if (item) cnSel.items = cnSel.items.filter((i) => i !== item); }
function selAdd(kind, schema, object, column, mode) {
  if (!selFind(kind, schema, object, column, mode)) cnSel.items.push({ kind, schema, object: object || "", column: column || "", mode });
}
function tableWhole(schema, object) {
  if (selFind("table", schema, object, "", "include")) return true;
  return !!selFind("schema", schema, "", "", "include") && !selFind("table", schema, object, "", "exclude");
}
function tablePartial(schema, object) {
  return !tableWhole(schema, object) && cnSel.items.some((i) => i.kind === "column" && i.schema === schema && i.object === object && i.mode === "include");
}
function columnChecked(schema, object, column) {
  return tableWhole(schema, object) ? !selFind("column", schema, object, column, "exclude") : !!selFind("column", schema, object, column, "include");
}

// What the rules refuse is removed from the list before it is sent (the server refuses it as well).
function selPrune() {
  const keep = (i) => {
    if (i.kind === "table" && i.mode === "exclude") return !!selFind("schema", i.schema, "", "", "include");
    if (i.kind === "column" && i.mode === "exclude") return tableWhole(i.schema, i.object);
    if (i.kind === "column" && i.mode === "include") return !tableWhole(i.schema, i.object) && !selFind("table", i.schema, i.object, "", "exclude");
    return true;
  };
  for (let again = true; again;) {
    const before = cnSel.items.length;
    cnSel.items = cnSel.items.filter(keep);
    again = cnSel.items.length !== before;
  }
}

function selToggle(spec) {
  const { kind, schema, object, column } = spec;
  if (kind === "schema") {
    const found = selFind("schema", schema, "", "", "include");
    if (found) selDrop(found); else selAdd("schema", schema, "", "", "include");
  } else if (kind === "table") {
    const picked = tableWhole(schema, object) || tablePartial(schema, object);
    const inSchema = !!selFind("schema", schema, "", "", "include");
    cnSel.items = cnSel.items.filter((i) => !(i.schema === schema && i.object === object && (i.kind === "column" || i.kind === "table")));
    if (picked) { if (inSchema) selAdd("table", schema, object, "", "exclude"); } else if (!inSchema) selAdd("table", schema, object, "", "include");
  } else if (kind === "column") {
    const mode = tableWhole(schema, object) ? "exclude" : "include";
    const found = selFind("column", schema, object, column, mode);
    if (found) selDrop(found); else selAdd("column", schema, object, column, mode);
  }
  selPrune();
  cnSel.dirty = true;
  syncChecks();
  renderSelectionNote();
}

function selectionBox(label, spec) {
  const wrap = el("label", "cn-pick");
  const box = el("input");
  box.type = "checkbox";
  box.dataset.kind = spec.kind;
  box.dataset.schema = spec.schema;
  box.dataset.object = spec.object;
  box.dataset.column = spec.column;
  setAttr(box, "aria-label", label);
  box.addEventListener("change", () => selToggle(spec));
  wrap.append(box);
  return wrap;
}

function syncChecks() {
  if (!cnSel) return;
  for (const box of document.querySelectorAll("#cn-schema input[data-kind]")) {
    const { kind, schema, object, column } = box.dataset;
    if (kind === "schema") {
      box.checked = !!selFind("schema", schema, "", "", "include");
      box.indeterminate = false;
    } else if (kind === "table") {
      const whole = tableWhole(schema, object);
      box.checked = whole;
      box.indeterminate = !whole && tablePartial(schema, object);
    } else {
      box.checked = columnChecked(schema, object, column);
    }
  }
}

function renderSelectionNote() {
  if (!cnSel) return;
  const count = cnSel.items.length;
  setText($("cn-selection-note"), `${count ? `Tanlov: ${count} element.` : "Hech narsa tanlanmagan: AI kontekstida hech qanday jadval yo'q."}${cnSel.dirty ? " Saqlanmagan o'zgarish bor." : ""}`);
  const gone = cnSel.items.filter((i) => i.available === false);
  const list = $("cn-unavailable");
  list.hidden = !gone.length;
  list.replaceChildren(...gone.map((i) => {
    const li = el("li", "", `Endi mavjud emas: ${[i.schema, i.object, i.column].filter(Boolean).join(".")} (${i.mode === "exclude" ? "chiqarib tashlash" : "qo'shish"}) `);
    li.append(cnButton("Olib tashlash", () => { selDrop(i); cnSel.dirty = true; renderSelectionNote(); }));
    return li;
  }));
}

async function loadSelection() {
  const c = cnSchema.connection;
  const result = await cnRun(() => cnApi("GET", `/${c.id}/schema/selections`));
  if (!result) return;
  cnSel = { revision: result.data.revision, items: result.data.items.map((i) => ({ ...selItem(i), available: i.available })), dirty: false };
  renderSelectionNote();
}

async function saveSelection() {
  if (!cnSel) return;
  const c = cnSchema.connection;
  const result = await cnRun(() => cnApi("PUT", `/${c.id}/schema/selections`, { items: cnSel.items.map(selItem), base_revision: cnSel.revision }));
  if (!result) return;
  cnMessage("Tanlov saqlandi.", "ok");
  announce("Tanlov saqlandi.");
  await loadSelection();
  await loadFreshness();
  syncChecks();
}

function renderFreshness() {
  const f = cnFresh;
  const box = $("cn-fresh");
  if (!f) { box.replaceChildren(); return; }
  const head = el("div", "");
  head.append(el("span", `cn-fresh-state ${f.state}`, `Holat: ${FRESH_TEXT[f.state] || f.state}`));
  const lines = [head];
  if (f.reasons.length) lines.push(el("div", "muted", `Sabab: ${f.reasons.map((r) => REASON_TEXT[r] || r).join("; ")}.`));
  if (f.snapshot) {
    lines.push(el("div", "muted", `Versiya ${f.snapshot.version} · barmoq izi ${f.snapshot.fingerprint.slice(0, 12)} · tasdiqlangan ${cnWhen(f.snapshot.verified_at)} · ${f.snapshot.objects} obyekt, ${f.snapshot.columns} ustun`));
  }
  if (f.last_error) lines.push(el("div", "update warn", `Oxirgi xato: ${cnCodeText(f.last_error)}`));
  if (f.unreviewed_changes) lines.push(el("div", "muted", `Ko'rib chiqilmagan o'zgarishlar: ${f.unreviewed_changes} (tanlanganlarga tegishlisi: ${f.affected_selected})`));
  if (f.orphaned_descriptions) lines.push(el("div", "muted", `Obyekti yo'q tavsiflar: ${f.orphaned_descriptions}`));
  if (f.snapshot && f.unreviewed_changes) lines.push(cnButton("Ko'rib chiqdim", () => acknowledgeChanges(f.snapshot.version)));
  box.replaceChildren(...lines);
}

async function loadFreshness() {
  const c = cnSchema.connection;
  const result = await cnRun(() => cnApi("GET", `/${c.id}/schema/freshness`));
  if (!result) return;
  cnFresh = result.data;
  renderFreshness();
}

async function acknowledgeChanges(version) {
  const c = cnSchema.connection;
  const result = await cnRun(() => cnApi("POST", `/${c.id}/schema/changes/acknowledge`, { version }));
  if (!result) return;
  cnMessage("O'zgarishlar ko'rib chiqildi.", "ok");
  announce("O'zgarishlar ko'rib chiqildi.");
  cnFresh = result.data;
  renderFreshness();
  $("cn-changes").hidden = true;
}

async function showChanges() {
  const c = cnSchema.connection;
  const since = cnFresh ? cnFresh.confirmed_version : 0;
  const result = await cnRun(() => cnApi("GET", `/${c.id}/schema/changes?since_version=${since}&limit=200`));
  if (!result) return;
  const box = $("cn-changes");
  const list = el("ul");
  for (const ch of result.data.changes) {
    const name = [ch.schema_name, ch.object_name, ch.sub_name].filter(Boolean).join(".");
    list.append(el("li", "", `v${ch.to_version}: ${CHANGE_KIND[ch.object_type] || ch.object_type} ${name} ${CHANGE_HOW[ch.change_type] || ch.change_type}`));
  }
  const title = el("div", "muted", result.data.total ? `Ko'rib chiqilmagan o'zgarishlar: ${result.data.total}` : "Ko'rib chiqilmagan o'zgarish yo'q.");
  box.replaceChildren(title, list);
  box.hidden = false;
}

function renderOrphans() {
  const list = $("cn-orphans");
  const orphans = cnSchema.descriptions.filter((d) => d.orphaned);
  list.hidden = !orphans.length;
  list.replaceChildren(...orphans.map((d) => {
    const li = el("li", "", `Obyekti yo'q: ${[d.schema_name, d.object_name, d.column_name].filter(Boolean).join(".") || "baza"} — ${d.description} `);
    li.append(cnButton("O'chirish", async () => { await saveDescription(d.target_kind, d.schema_name, d.object_name, d.column_name, "", null); renderOrphans(); }));
    return li;
  }));
}

async function showContext() {
  const c = cnSchema.connection;
  const result = await cnRun(() => cnApi("GET", `/${c.id}/schema/context?allow_stale=true`));
  if (!result) return;
  const d = result.data;
  const box = $("cn-context");
  const status = el("div", d.usable ? "muted" : "update warn",
    `Holat: ${d.status} · ${d.usable ? "AI uchun yaroqli" : "AI uchun yaroqsiz"} · ${d.budget.used_chars}/${d.budget.max_chars} belgi · ${d.selection.included_objects}/${d.selection.selected_objects} obyekt${d.complete ? "" : " (hammasi sig'madi)"}`);
  const pre = el("pre", "", JSON.stringify(d.context, null, 1)); // the backend's JSON, as it is
  box.replaceChildren(status, pre);
  box.hidden = false;
}

$("cn-show-changes").addEventListener("click", () => { if (cnSchema) showChanges(); });
$("cn-save-selection").addEventListener("click", saveSelection);
$("cn-show-context").addEventListener("click", () => { if (cnSchema) showContext(); });

// ----- SQL proposals from a question (roadmap M4.3, docs/sql-generation.md) -----
// The backend does everything that matters (the schema context, the prompt, the model, the reading of its answer); this code
// sends a question and shows the answer as TEXT (textContent). The proposed SQL is NOT checked and is NEVER run by the
// application: the page says so wherever the SQL is shown, and offers no way to run it.
const SG_TEXT = {
  invalid_question: "Savol yaroqsiz.", context_empty: "Hech narsa tanlanmagan: avval sxema oynasida jadvallarni tanlang va saqlang.",
  context_unusable: "Tanlangan sxema kontekstga sig'madi.", generation_unavailable: "SQL yaratish hozir ishlamayapti.",
  generation_timeout: "Model vaqtida javob bermadi.", generation_failed: "Model xato qildi.", generation_cancelled: "Bekor qilindi.",
  generation_unsupported: "Bu baza turi uchun SQL yaratib bo'lmaydi.", empty_model_output: "Model bo'sh javob berdi.",
  malformed_model_output: "Model javobini o'qib bo'lmadi (kutilgan shakl emas).", model_output_truncated: "Model javobi yarim qoldi (chegaraga yetdi).",
  model_output_too_large: "Model juda uzun so'rov yozdi.", prompt_too_large: "Savol va sxema modelga sig'maydi.",
  llm_unavailable: "Model serveri (Ollama) ishlamayapti.", llm_timeout: "Model juda sekin.", llm_error: "Model xatosi.",
  not_configured: "Model sozlanmagan.", budget_too_small: "Hech bir obyekt sig'madi.", empty: "Savol bo'sh.", too_long: "Savol juda uzun.",
  bad_characters: "Savolda yaroqsiz belgi bor.", not_text: "Savol matn emas.", not_json: "JSON emas.", not_an_object: "JSON obyekt emas.",
  bad_field: "Maydon turi noto'g'ri.",
};
Object.assign(CN_TEXT, SG_TEXT);
let sgBusy = false;

function sgError(text) {
  const box = $("sg-error");
  box.hidden = !text;
  setText(box, text || "");
}

function sgReset() {
  $("sg-result").hidden = true;
  $("sg-result").replaceChildren();
  $("sg-copy").hidden = true;
  sgError("");
}

function sgSelected() {
  return $("sg-connection").value;
}

function sgRender() {
  const select = $("sg-connection");
  const usable = cnConnections.filter((c) => c.is_enabled);
  const previous = select.value;
  select.replaceChildren(...usable.map((c) => {
    const option = el("option", "", `${c.name} (${CN_PROVIDER[c.provider] || c.provider})`);
    option.value = c.id;
    return option;
  }));
  if (usable.some((c) => c.id === previous)) select.value = previous;
  $("cn-sql").hidden = !usable.length;
  if (!usable.length) { sgReset(); setText($("sg-fresh"), ""); return; }
  if (select.value !== previous) { sgReset(); sgFreshness(); }
}

async function sgFreshness() {
  const id = sgSelected();
  if (!id) return;
  const result = await cnRun(() => cnApi("GET", `/${id}/schema/freshness`), () => {});
  if (!result || id !== sgSelected()) return;
  const f = result.data;
  const version = f.snapshot ? `, versiya ${f.snapshot.version}` : "";
  const reasons = f.reasons.length ? ` (${f.reasons.map((r) => REASON_TEXT[r] || r).join("; ")})` : "";
  setText($("sg-fresh"), `Sxema holati: ${FRESH_TEXT[f.state] || f.state}${version}${reasons}.`);
  setClass($("sg-fresh"), f.state === "current" ? "muted" : "update warn");
}

function sgList(title, items) {
  if (!items.length) return null;
  const box = el("div", "");
  box.append(el("strong", "", title));
  const list = el("ul", "cn-keys");
  for (const item of items) list.append(el("li", "", item));
  box.append(list);
  return box;
}

function sgShow(d) {
  const parts = [];
  const proposed = d.status === "generated";
  parts.push(el("div", proposed ? "muted" : "update warn",
    `${proposed ? "SQL taklif qilindi" : "Model aniqlik so'radi"} · ${d.dialect} · model ${d.model} · ${d.duration_ms} ms`));
  parts.push(el("div", "update warn", "Bu SQL tekshirilmagan va hech qachon bajarilmaydi: faqat taklif. Ishlatishdan oldin o'zingiz tekshiring."));
  if (d.sql) {
    const pre = el("pre", "sg-sql", d.sql);
    pre.id = "sg-sql";
    parts.push(pre);
  }
  if (d.explanation) parts.push(el("p", "", d.explanation));
  for (const block of [sgList("Taxminlar", d.assumptions), sgList("Aniqlanmagan joylar", d.ambiguities)]) if (block) parts.push(block);
  const c = d.context;
  let note = `Sxema: versiya ${c.snapshot_version}, ${FRESH_TEXT[c.state] || c.state}, ${c.included_objects}/${c.selected_objects} obyekt modelga berildi`;
  if (!c.complete) note += `; ${c.omitted_objects} obyekt sig'madi, shuning uchun modelga ko'rinmadi`;
  parts.push(el("div", c.complete ? "muted" : "update warn", `${note}.`));
  $("sg-result").replaceChildren(...parts);
  $("sg-result").hidden = false;
  $("sg-copy").hidden = !d.sql;
}

async function sgGenerate() {
  if (sgBusy) return;
  const id = sgSelected();
  const question = $("sg-question").value.trim();
  sgReset();
  if (!id) return;
  if (!question) { sgError("Savolni yozing."); $("sg-question").focus(); return; }
  sgBusy = true;
  $("sg-generate").disabled = true;
  setText($("sg-generate"), "Yaratilmoqda…");
  announce("SQL yaratilmoqda");
  try {
    const result = await cnRun(() => cnApi("POST", `/${id}/sql/generate`, { question }), sgError);
    if (id !== sgSelected()) return; // the person chose another connection meanwhile: the answer is not for the page any more
    if (result) { sgShow(result.data); announce("SQL taklifi tayyor"); } else { await sgFreshness(); }
  } finally {
    sgBusy = false;
    $("sg-generate").disabled = false;
    setText($("sg-generate"), "SQL yaratish");
  }
}

async function sgCopy() {
  const text = $("sg-sql") ? $("sg-sql").textContent : "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const range = document.createRange(); // no clipboard permission: select the text so that Ctrl+C works
    range.selectNodeContents($("sg-sql"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    sgError("Nusxa olib bo'lmadi: matn belgilandi, Ctrl+C bosing.");
    return;
  }
  sgError("");
  announce("SQL nusxalandi");
  const button = $("sg-copy");
  setText(button, "Nusxalandi");
  setTimeout(() => setText(button, "Nusxa olish"), 1500);
}

$("sg-generate").addEventListener("click", sgGenerate);
$("sg-copy").addEventListener("click", sgCopy);
$("sg-connection").addEventListener("change", () => { sgReset(); sgFreshness(); });
$("sg-question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); sgGenerate(); }
});

// ----- when the session starts and ends -----

function onConnectorMode(mode) {
  const panel = $("connections");
  panel.hidden = mode !== "session";
  if (mode === "session" && !cnLoaded) {
    refreshConnections();
  }
  if (mode !== "session" && cnLoaded) { // signed out or expired: nothing of it stays on the page
    cnLoaded = false;
    cnConnections = [];
    hideForm();
    closeSchema();
    $("cn-stepup").hidden = true;
    renderConnections();
    cnMessage("");
  }
}

refreshDocs();
refreshSession();
