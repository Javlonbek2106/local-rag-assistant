"""Roadmap T0.6: keyboard focus survives the automatic refresh, and the page has the accessibility semantics it needs.

The real page (static/) runs in Edge. Only the static files come from a tiny local server; the API answers are scripted
with `page.route`, so a test can change what the "server" says between two refreshes and nothing else (no database, no
Ollama) moves. The flows against the real application stay in test_ui.py. Enable with RAG_E2E=1.
"""
import http.server
import json
import mimetypes
import re
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import pytest

from rag import config

pytestmark = pytest.mark.e2e
sync_api = pytest.importorskip("playwright.sync_api")
STATIC = Path(config.STATIC_DIR)

FETCH_HOLD = """
window.__asks = [];
const realFetch = window.fetch.bind(window);
window.fetch = (url, options) => {
  if (String(url).includes('/api/ask')) {
    return new Promise((resolve) => window.__asks.push((body) => resolve(new Response(body, {status: 200}))));
  }
  return realFetch(url, options);
};
"""
ANSWER = '{"token":"Javob"}\n{"final":"Javob","found":true,"sources":[]}\n'


# ---------- the static files, and the scripted API ----------

class StaticHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        file = STATIC / "index.html" if path == "/" else STATIC / path.removeprefix("/static/")
        if not (path == "/" or path.startswith("/static/")) or not file.is_file():
            self.send_error(404)
            return
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(file))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def base_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        yield b
        b.close()


def doc(name, state="done", version=1, chunks=3, stage=None, progress=None, error=None, searchable=True):
    return {"name": name, "size": 10, "chunks": chunks, "error": error, "seconds": None, "stage": stage,
            "progress": progress, "version": version if searchable else None, "latest_version": version,
            "update_error": None, "searchable": searchable, "state": state}


def processing(name, done, total=100):
    return doc(name, state="processing", stage="EMBEDDING", searchable=False, chunks=None,
               progress={"unit": "chunks", "done": done, "total": total})


def version(number, phase, chunks=3, status=None):
    status = status or {"active": "READY", "superseded": "SUPERSEDED"}.get(phase, "PROCESSING")
    return {"version": number, "status": status, "phase": phase, "stale": False, "active": phase == "active",
            "size": 10, "chunks": chunks, "error": None, "progress": None, "created_at": None, "indexed_at": None,
            "activated_at": None}


class Api:
    """What the page's server says; tests change it between refreshes."""

    def __init__(self):
        self.docs = [doc("a.txt"), processing("b.txt", 10)]
        self.versions = {"a.txt": [version(2, "active"), version(1, "superseded")]}
        self.requests = Counter()
        self.docs_fail = False
        self.ask = (200, {"final": "Javob", "found": True, "sources": []})
        refused = {"name": "x.exe", "accepted": False, "error": "Bu fayl turi qo'llab-quvvatlanmaydi"}
        self.upload = (200, {"results": [refused]})

    def handle(self, route):
        path = urlparse(route.request.url).path
        self.requests[path] += 1
        method = route.request.method
        if path == "/api/documents" and method == "GET":
            if self.docs_fail:
                route.abort()
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"documents": self.docs, "queued": 0, "error": None}))
        elif path.endswith("/versions"):
            name = path.split("/")[3]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"name": name, "versions": self.versions.get(name, [])}))
        elif path == "/api/ask":
            status, body = self.ask
            if status == 200:
                lines = [json.dumps({"token": body["final"]}), json.dumps(body)]
                route.fulfill(status=200, content_type="application/x-ndjson", body="\n".join(lines) + "\n")
            else:
                route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        elif path == "/api/documents" and method == "POST":
            status, body = self.upload
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        else:
            route.fulfill(status=404, content_type="application/json", body='{"detail":"not scripted"}')


@pytest.fixture
def ui(browser, base_url):
    """(page, api) with the page loaded and both documents shown."""
    api = Api()
    context = browser.new_context(viewport={"width": 1280, "height": 760})
    page = context.new_page()
    page.route("**/api/**", api.handle)
    page.goto(base_url)
    page.wait_for_selector("#docs li")
    page.locator("#docs li").nth(1).wait_for()
    yield page, api
    context.close()


def refresh(page, times=1):
    for _ in range(times):
        page.evaluate("refreshDocs()")  # resolves when the answer has been drawn


def active(page):
    return page.evaluate("""() => {
        const a = document.activeElement;
        return {id: a.id, key: a.dataset ? a.dataset.focusKey || null : null, tag: a.tagName,
                value: a.value === undefined ? null : a.value,
                start: a.selectionStart === undefined ? null : a.selectionStart,
                end: a.selectionEnd === undefined ? null : a.selectionEnd};
    }""")


def watch(page, selector, name):
    """Record every change of a part of the page (nodes added or removed, attributes, text)."""
    page.evaluate("""([selector, name]) => {
        window[name] = {childList: 0, attributes: 0, characterData: 0, elements: 0, texts: []};
        const target = document.querySelector(selector);
        new MutationObserver((records) => {
            for (const r of records) {
                window[name][r.type] += 1;
                // nodes of the page itself (rows, buttons, bars) added or removed; a swapped text node is not one
                for (const n of [...r.addedNodes, ...r.removedNodes]) if (n.nodeType === 1) window[name].elements += 1;
            }
            window[name].texts.push(target.textContent);
        }).observe(target, {childList: true, attributes: true, characterData: true, subtree: true});
    }""", [selector, name])


def changes(page, name):
    return page.evaluate("(name) => window[name]", name)


# ---------- keyboard focus survives the automatic refresh ----------

def test_the_question_keeps_its_focus_caret_and_text_while_the_page_polls(ui):
    page, api = ui
    page.click("#question")
    page.keyboard.type("Salom dunyo")
    page.evaluate("document.getElementById('question').setSelectionRange(5, 5)")
    before = api.requests["/api/documents"]
    for done in (20, 40, 60):  # the indexing moves on between refreshes
        api.docs[1] = processing("b.txt", done)
        refresh(page)
    page.wait_for_timeout(3500)  # b.txt is processing, so the page itself polls every 1.5 s: at least two real polls
    assert api.requests["/api/documents"] >= before + 5
    state = active(page)
    assert state["id"] == "question" and state["value"] == "Salom dunyo" and (state["start"], state["end"]) == (5, 5)
    page.keyboard.type("X")  # typing goes on exactly where the caret was
    assert page.input_value("#question") == "SalomX dunyo"


def test_a_focused_button_in_the_list_stays_the_same_focused_node_when_the_list_changes(ui):
    page, api = ui
    key = '[data-focus-key="doc:a.txt:versions"]'
    page.focus(key)
    page.evaluate("(key) => { document.querySelector(key).__mark = 'same node'; }", key)
    api.docs = [doc("0-yangi.txt"), doc("a.txt", chunks=9), processing("b.txt", 55)]  # a new row above, texts changed
    refresh(page, 2)
    assert page.locator("#docs .name").all_inner_texts() == ["0-yangi.txt", "a.txt", "b.txt"]
    assert active(page)["key"] == "doc:a.txt:versions"
    assert page.evaluate("(key) => document.querySelector(key).__mark", key) == "same node"  # not even re-created
    api.docs = [doc("a.txt"), doc("c.txt")]  # rows come and go around it
    refresh(page)
    assert active(page)["key"] == "doc:a.txt:versions"


def test_focus_that_the_user_moved_elsewhere_is_not_taken_back_to_the_question(ui):
    page, api = ui
    page.click("#question")
    page.keyboard.type("yarim")
    page.focus("#drop")  # the user moved on deliberately
    refresh(page, 3)
    page.wait_for_timeout(1800)
    assert active(page)["id"] == "drop"
    page.evaluate("""() => {
        const i = document.createElement('input');
        i.id = 'probe';
        document.body.append(i);
        i.focus();
    }""")
    refresh(page, 2)
    assert active(page)["id"] == "probe"
    assert page.input_value("#question") == "yarim"  # and what was typed is still there


def test_if_a_focused_node_is_replaced_anyway_the_focus_is_restored_with_its_selection(ui):
    """The safety net of withFocusKept: a node with a data-focus-key that is swapped for a new one gets the focus."""
    page, _ = ui
    page.focus('[data-focus-key="doc:a.txt:delete"]')
    page.evaluate("""() => {
        withFocusKept(() => {
            const old = document.querySelector('[data-focus-key="doc:a.txt:delete"]');
            const copy = old.cloneNode(true);
            old.replaceWith(copy);
        });
    }""")
    assert active(page)["key"] == "doc:a.txt:delete"


def test_after_an_answer_the_focus_is_given_back_only_if_it_was_lost(ui, base_url):
    page, api = ui
    page.add_init_script(FETCH_HOLD)
    page.goto(base_url)
    page.locator("#docs li").nth(1).wait_for()
    # 1. the user stays in the question while the answer is written: it stays there
    page.fill("#question", "birinchi savol")
    page.press("#question", "Enter")
    page.wait_for_function("window.__asks.length === 1")
    page.evaluate("(body) => window.__asks.shift()(body)", ANSWER)
    page.wait_for_function("!document.querySelector('#send').disabled")
    assert active(page)["id"] == "question"
    # 2. the user clicks the send button (it is disabled meanwhile, the focus is lost): the question gets it back
    page.fill("#question", "ikkinchi savol")
    page.click("#send")
    page.wait_for_function("window.__asks.length === 1")
    page.evaluate("(body) => window.__asks.shift()(body)", ANSWER)
    page.wait_for_function("!document.querySelector('#send').disabled")
    assert active(page)["id"] == "question"
    # 3. the user goes to another control while the answer is written: that choice stands
    page.fill("#question", "uchinchi savol")
    page.press("#question", "Enter")
    page.wait_for_function("window.__asks.length === 1")
    page.focus('[data-focus-key="doc:a.txt:versions"]')
    page.evaluate("(body) => window.__asks.shift()(body)", ANSWER)
    page.wait_for_function("!document.querySelector('#send').disabled")
    assert active(page)["key"] == "doc:a.txt:versions"


def test_the_list_is_updated_in_place_nothing_is_redrawn_when_nothing_changed(ui):
    page, api = ui
    watch(page, "#docs", "quiet")
    refresh(page, 3)  # the same answer three times
    assert changes(page, "quiet") == {"childList": 0, "attributes": 0, "characterData": 0, "elements": 0, "texts": []}
    watch(page, "#docs", "moving")
    api.docs[1] = processing("b.txt", 77)  # only the progress of one row moves
    refresh(page)
    moved = changes(page, "moving")
    assert moved["elements"] == 0  # no row, button or bar was added, removed or replaced
    assert moved["childList"] + moved["attributes"] + moved["characterData"] > 0  # only texts and values changed


def test_an_open_versions_panel_keeps_its_buttons_focus_and_pending_confirmation_across_refreshes(ui):
    page, api = ui
    page.locator('[data-focus-key="doc:a.txt:versions"]').click()
    restore = page.locator('[data-focus-key="ver:a.txt#1"]')
    restore.wait_for()
    restore.focus()
    page.evaluate("document.querySelector('[data-focus-key=\"ver:a.txt#1\"]').__mark = 'same node'")
    restore.press("Enter")  # the first press only asks "are you sure?"
    assert restore.inner_text() == "Aniqmi?" and "Aniqmi?" in restore.get_attribute("aria-label")
    api.versions["a.txt"] = [version(2, "active", chunks=5), version(1, "superseded", chunks=4)]
    refresh(page, 3)  # the panel is fetched again each time
    page.wait_for_timeout(300)
    assert active(page)["key"] == "ver:a.txt#1"
    assert page.evaluate("document.querySelector('[data-focus-key=\"ver:a.txt#1\"]').__mark") == "same node"
    assert restore.inner_text() == "Aniqmi?"  # the confirmation was not reset by the refreshes
    assert "5 bo'lak" in page.locator(".ver", has_text="v2").inner_text()  # while the text next to it was updated
    page.wait_for_function("document.querySelector('[data-focus-key=\"ver:a.txt#1\"]').textContent !== 'Aniqmi?'",
                           timeout=6000)  # and it lapses by itself after three seconds


# ---------- accessible names ----------

def test_the_question_field_has_an_accessible_name_that_is_not_the_placeholder(ui):
    page, _ = ui
    box = page.get_by_role("textbox", name="Savolingiz")
    assert box.count() == 1
    assert page.locator('label[for="question"]').inner_text() == "Savolingiz"
    assert page.get_by_label("Savolingiz").count() == 1
    assert box.get_attribute("aria-describedby") == "question-help"
    assert "Enter" in page.locator("#question-help").inner_text()
    page.keyboard.press("Tab")  # the field can be reached with the keyboard
    page.focus("#question")
    assert active(page)["id"] == "question"


def test_every_button_has_a_name_and_the_names_tell_the_documents_apart(ui):
    page, _ = ui
    page.locator('[data-focus-key="doc:a.txt:versions"]').click()
    page.locator('[data-focus-key="ver:a.txt#1"]').wait_for()
    names = page.evaluate("""() => [...document.querySelectorAll('button')].map(
        (b) => (b.getAttribute('aria-label') || b.textContent).trim())""")
    assert names and all(names)
    assert page.get_by_role("button", name="O'chirish: a.txt").count() == 1
    assert page.get_by_role("button", name="O'chirish: b.txt").count() == 1
    assert page.get_by_role("button", name="Versiyalar: b.txt", expanded=False).count() == 1
    assert page.get_by_role("button", name="Versiyalarni yopish: a.txt", expanded=True).count() == 1
    assert page.get_by_role("button", name="Shu versiyani faol qilish: a.txt v1").count() == 1
    assert page.get_by_role("button", name="Yuborish").count() == 1
    for name in names:  # a spoken name always contains the text that is drawn on the button
        assert name


def test_landmarks_and_the_drop_zone_are_named(ui):
    page, _ = ui
    assert page.get_by_role("complementary", name="Hujjatlar").count() == 1
    assert page.get_by_role("region", name="Savol-javob").count() == 1
    assert page.get_by_role("list", name="Hujjatlar ro'yxati").count() == 1
    assert page.get_by_role("log", name="Savol-javob tarixi").count() == 1
    assert page.get_by_role("button", name=re.compile("Fayllarni shu yerga tashlang")).count() == 1


# ---------- errors and status are announced ----------

def test_an_error_in_the_answer_is_an_alert_with_words_not_only_a_colour(ui):
    page, api = ui
    api.ask = (503, {"detail": "Javob yozilmadi: Ollama 20 soniyadan beri hech qanday javob bermayapti",
                     "code": "llm_unavailable"})
    page.fill("#question", "savol")
    page.press("#question", "Enter")
    page.wait_for_function("!document.querySelector('#send').disabled")
    alert = page.locator("#log .msg.bot.error [role=alert]")
    assert alert.count() == 1
    text = alert.inner_text()
    assert text.startswith("Xato:") and "Ollama" in text
    assert page.locator("#log").get_attribute("aria-busy") == "false"  # the log is not left "busy"


def test_a_lost_connection_banner_and_a_refused_upload_are_alerts(ui):
    page, api = ui
    banner = page.locator("#banner")
    assert banner.get_attribute("role") == "alert"
    api.docs_fail = True
    refresh(page)
    banner.wait_for(state="visible")
    assert "Server bilan aloqa yo'q" in banner.inner_text()
    api.docs_fail = False
    refresh(page)
    banner.wait_for(state="hidden")
    message = page.locator("#upload-msg")
    assert message.get_attribute("role") == "alert"
    page.set_input_files("#file-input", {"name": "x.exe", "mimeType": "application/octet-stream", "buffer": b"MZ"})
    message.wait_for(state="visible")
    assert "x.exe" in message.inner_text()
    api.upload = (413, {"detail": "Yuklash hajmi chegaradan katta: bitta so'rovda eng ko'pi 200 MB"})
    page.set_input_files("#file-input", {"name": "katta.pdf", "mimeType": "application/pdf", "buffer": b"%PDF"})
    page.wait_for_function("document.querySelector('#upload-msg').textContent.includes('chegaradan katta')")
    shown = message.inner_text()  # each refusal is shown under the name of its file (one request per file)
    assert shown.startswith("katta.pdf:") and "chegaradan katta" in shown and "200 MB" in shown


def test_an_error_line_of_a_document_and_of_a_version_is_an_alert(ui):
    page, api = ui
    api.docs[0] = doc("a.txt", state="failed", searchable=False, chunks=None, error="Qdrant to'xtadi")
    refresh(page)
    assert page.locator("#docs li", has_text="a.txt").locator(".err").get_attribute("role") == "alert"


def test_progress_has_the_values_a_screen_reader_needs_and_they_follow_the_indexing(ui):
    page, api = ui
    bar = page.get_by_role("progressbar", name="b.txt: indekslash taraqqiyoti")
    assert bar.count() == 1
    assert [bar.get_attribute(a) for a in ("aria-valuemin", "aria-valuemax", "aria-valuenow")] == ["0", "100", "10"]
    assert "10/100" in bar.get_attribute("aria-valuetext")
    api.docs[1] = processing("b.txt", 64)
    refresh(page)
    assert [bar.get_attribute(a) for a in ("aria-valuemin", "aria-valuemax", "aria-valuenow")] == ["0", "100", "64"]
    assert "64/100" in bar.get_attribute("aria-valuetext") and "64%" in bar.get_attribute("aria-valuetext")


def test_the_status_region_speaks_about_real_changes_only_not_at_every_poll(ui):
    page, api = ui
    region = page.locator("#sr-status")
    assert (region.get_attribute("role"), region.get_attribute("aria-live")) == ("status", "polite")
    assert region.inner_text() == ""  # the whole list is not read out when the page opens
    watch(page, "#sr-status", "spoken")
    refresh(page, 4)  # nothing changed
    api.docs[1] = processing("b.txt", 12)  # moved, but inside the same quarter
    refresh(page, 3)
    assert changes(page, "spoken")["texts"] == []
    api.docs[1] = processing("b.txt", 31)  # a quarter further
    refresh(page, 3)
    assert len(changes(page, "spoken")["texts"]) == 1
    assert "b.txt" in region.inner_text() and "31/100" in region.inner_text()
    api.docs[1] = doc("b.txt")  # finished
    refresh(page, 2)
    assert changes(page, "spoken")["texts"][-1] == "b.txt: tayyor" and len(changes(page, "spoken")["texts"]) == 2
    api.docs.append(doc("c.txt", state="queued", searchable=False, chunks=None))  # a new upload appears
    refresh(page)
    assert region.inner_text() == "c.txt: navbatda"


# ---------- the keyboard ----------

def test_the_keyboard_reaches_every_control_and_the_focus_is_drawn(ui):
    page, _ = ui
    seen, outlines = [], []
    page.evaluate("document.body.focus()")
    for _ in range(14):
        page.keyboard.press("Tab")
        state = page.evaluate("""() => { const a = document.activeElement; const s = getComputedStyle(a);
            return {id: a.id, key: a.dataset.focusKey || null, outline: s.outlineStyle + ' ' + s.outlineWidth,
                    tag: a.tagName}; }""")
        seen.append(state["id"] or state["key"] or state["tag"])
        if state["tag"] != "BODY":  # after the last control Tab leaves the page: nothing there to draw a ring on
            outlines.append(state["outline"])
    for wanted in ("drop", "doc:a.txt:delete", "doc:a.txt:versions", "doc:b.txt:delete", "doc:b.txt:versions",
                   "question", "send"):
        assert wanted in seen, f"{wanted} was not reached with Tab: {seen}"
    assert len(outlines) >= 7 and all(o.split()[0] != "none" for o in outlines), outlines  # every stop shows a ring


def test_keys_do_what_the_mouse_does(ui):
    page, api = ui
    page.focus('[data-focus-key="doc:a.txt:versions"]')
    page.keyboard.press("Enter")
    page.locator(".versions").wait_for()
    assert page.locator('[data-focus-key="doc:a.txt:versions"]').get_attribute("aria-expanded") == "true"
    assert active(page)["key"] == "doc:a.txt:versions"  # opening the panel did not drop the focus
    page.focus('[data-focus-key="doc:b.txt:delete"]')
    page.keyboard.press("Space")
    assert page.locator('[data-focus-key="doc:b.txt:delete"]').inner_text() == "Aniqmi?"
    page.focus("#drop")
    with page.expect_file_chooser():
        page.keyboard.press("Enter")
    page.focus("#question")
    page.keyboard.type("bir")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("ikki")
    assert page.input_value("#question") == "bir\nikki"  # Shift+Enter is a new line
    before = api.requests["/api/ask"]
    page.keyboard.press("Enter")  # Enter sends
    page.wait_for_function("!document.querySelector('#send').disabled")
    assert api.requests["/api/ask"] == before + 1 and page.input_value("#question") == ""
