"use strict";
const $ = (id) => document.getElementById(id);
const STATE_TEXT = { queued: "Navbatda", processing: "O'qilmoqda…", done: "Tayyor", failed: "Xato" };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // textContent only: file names and answers are never parsed as HTML
  return node;
}

function showBanner(text) {
  $("banner").hidden = !text;
  $("banner").textContent = text || "";
}

// ---------- documents ----------
let docsTimer = null;

async function refreshDocs() {
  clearTimeout(docsTimer);
  let delay = 8000;
  try {
    const res = await fetch("/api/documents");
    const data = await res.json();
    renderDocs(data.documents);
    showBanner(data.error);
    if (data.documents.some((d) => d.state === "queued" || d.state === "processing")) delay = 1500;
  } catch {
    showBanner("Server bilan aloqa yo'q. start.bat oynasi ochiqmi?");
    delay = 4000;
  }
  docsTimer = setTimeout(refreshDocs, delay);
}

function renderDocs(documents) {
  const list = $("docs");
  list.replaceChildren();
  $("docs-empty").hidden = documents.length > 0;
  for (const doc of documents) {
    const row = el("li");
    row.append(el("span", "name", doc.name));
    row.append(deleteButton(doc.name));
    const label = doc.state === "done" ? `Tayyor · ${doc.chunks} bo'lak` : STATE_TEXT[doc.state] || doc.state;
    row.append(el("span", `state ${doc.state}`, label));
    if (doc.error) row.append(el("span", "err", doc.error));
    list.append(row);
  }
}

function deleteButton(name) {
  const button = el("button", "danger", "O'chirish");
  let armed = false;
  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = "Aniqmi?";
      setTimeout(() => { armed = false; button.textContent = "O'chirish"; }, 3000);
      return;
    }
    button.disabled = true;
    await fetch(`/api/documents/${encodeURIComponent(name)}`, { method: "DELETE" });
    refreshDocs();
  });
  return button;
}

async function uploadFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  const msg = $("upload-msg");
  try {
    const res = await fetch("/api/documents", { method: "POST", body: form });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const rejected = (await res.json()).results.filter((r) => !r.accepted);
    msg.hidden = rejected.length === 0;
    msg.textContent = rejected.map((r) => `${r.name}: ${r.error}`).join(" · ");
  } catch (error) {
    msg.hidden = false;
    msg.textContent = `Yuklab bo'lmadi: ${error.message}`;
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

async function ask(question) {
  addMessage("user", question);
  const bot = addMessage("bot", "…");
  let text = "";
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
          if (event.sources.length) renderSources(bot, event.sources);
        }
        $("log").scrollTop = $("log").scrollHeight;
      }
      if (done) break;
    }
  } catch (error) {
    bot.classList.add("error");
    bot.textContent = `Xato: ${error.message}`;
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
  $("question").focus();
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("ask-form").requestSubmit(); }
});

refreshDocs();
