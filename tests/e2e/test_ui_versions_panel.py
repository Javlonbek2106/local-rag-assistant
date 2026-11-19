"""The versions panel and the header of a document always tell the same story, also after a rollback and when answers
arrive late or out of order (found by test_ui.py::test_versions_are_visible...: after a rollback the header showed the
restored version while the panel, still waiting for its own answer, showed the old one).

Real page in Edge; the answers of the server are scripted. The page's requests for the documents and for the versions
can be held and answered by the test one at a time, in any order, so that the order in which the page asks and the order
in which answers arrive are exactly what each test says, and the test waits for what the page has done with an answer
(window.__handled), never for a delay. Enable with RAG_E2E=1.
"""
import json
from urllib.parse import urlparse

import pytest

from tests.e2e.test_ui_focus_a11y import Api, base_url, browser, doc, refresh, version  # noqa: F401

pytestmark = pytest.mark.e2e

HOLD = """
window.__hold = {versions: false, docs: false};
window.__pending = [];  // held requests, oldest first: {kind, answer(body, label)}
window.__order = [];    // the kind of every request for the documents / the versions, in the order the page made them
window.__handled = [];  // labels of the answers that the page has finished working on
const realFetch = window.fetch.bind(window);
window.fetch = (url, options) => {
  const text = String(url);
  const get = !options || !options.method || options.method === 'GET';
  let kind = null;
  if (get && text.includes('/versions')) kind = 'versions';
  else if (get && text.endsWith('/api/documents')) kind = 'docs';
  if (kind) window.__order.push(kind);
  if (kind && window.__hold[kind]) {
    return new Promise((resolve) => window.__pending.push({kind, answer: (body, label) => {
      const response = new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}});
      const read = response.json.bind(response);
      // the page goes on in microtasks after json(); this timer runs after them: the answer is then fully dealt with
      response.json = async () => {
        const data = await read();
        setTimeout(() => window.__handled.push(label), 0);
        return data;
      };
      resolve(response);
    }}));
  }
  return realFetch(url, options);
};
"""


class RollbackApi(Api):
    """One document with two versions; a rollback to version 1 changes what the server says from then on."""

    def __init__(self):
        super().__init__()
        self.docs = [doc("a.txt", version=2)]
        self.versions = {"a.txt": [version(2, "active"), version(1, "superseded")]}

    def handle(self, route):
        if route.request.method == "POST" and urlparse(route.request.url).path.endswith("/rollback"):
            self.docs = [{**doc("a.txt", version=1), "latest_version": 2}]
            self.versions["a.txt"] = [version(2, "superseded"), version(1, "active")]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"name": "a.txt", "version": 1, "active": True}))
            return
        super().handle(route)


@pytest.fixture
def ui(browser, base_url):  # noqa: F811 - the fixtures are imported from test_ui_focus_a11y
    api = RollbackApi()
    context = browser.new_context(viewport={"width": 1280, "height": 760})
    page = context.new_page()
    page.add_init_script(HOLD)
    page.route("**/api/**", api.handle)
    page.goto(base_url)
    page.locator("#docs li").first.wait_for()
    page.evaluate("clearTimeout(docsTimer)")  # no automatic polling: only what the test does makes the page ask
    yield page, api
    context.close()


def body_of(versions):
    """What the server answers to a request for the versions of a.txt."""
    return {"name": "a.txt", "versions": versions}


def open_panel(page):
    page.locator('[data-focus-key="doc:a.txt:versions"]').click()
    page.locator('[data-focus-key="ver:a.txt#1"]').wait_for()  # the panel has its first answer (v1 can be restored)


def answer(page, kind, body, label, nth=0):
    """Answer the nth held request of that kind."""
    page.evaluate("""([kind, body, label, nth]) => {
        const held = window.__pending.filter((p) => p.kind === kind)[nth];
        window.__pending.splice(window.__pending.indexOf(held), 1);
        held.answer(body, label);
    }""", [kind, body, label, nth])
    page.wait_for_function("(label) => window.__handled.includes(label)", arg=label)  # the page has dealt with it


def held(page, kind):
    return page.evaluate("(kind) => window.__pending.filter((p) => p.kind === kind).length", kind)


def panel(page):
    """What the open panel shows right now: version -> {text, button}."""
    return page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#docs li .ver')].map((row) => [
        row.querySelector('.vnum').textContent,
        {text: row.querySelector('.phase').textContent, button: !!row.querySelector('button')}]))""")


# ---------- after a rollback ----------

def test_after_a_rollback_the_panel_is_asked_for_first_and_the_header_never_gets_ahead_of_it(ui):
    page, api = ui
    open_panel(page)
    page.evaluate("() => { window.__hold.versions = true; window.__hold.docs = true; window.__order = []; }")
    restore = page.locator('[data-focus-key="ver:a.txt#1"]')
    restore.click()  # the first press asks "are you sure?"
    restore.click()  # the second one restores
    page.wait_for_function("window.__pending.length >= 1")  # the page has asked for something after the rollback
    assert page.evaluate("window.__order") == ["versions"]  # the panel at once; the list (the header) not before it
    answer(page, "versions", body_of(api.versions["a.txt"]), "panel")
    page.wait_for_function("window.__pending.some((p) => p.kind === 'docs')")  # only now is the list refreshed
    shown = panel(page)  # the header still says v2 here, and the panel already says v1: never the other way round
    assert shown["v1"]["text"].startswith("Faol") and not shown["v1"]["button"]
    answer(page, "docs", {"documents": api.docs, "queued": 0, "error": None}, "list")
    page.wait_for_function("document.querySelector('#docs li').innerText.includes('Tayyor · v1')")
    shown = panel(page)  # the moment the header says v1 the panel says it too
    assert shown["v1"]["text"].startswith("Faol") and not shown["v1"]["button"]
    assert shown["v2"]["text"].startswith("Indekslangan, faol emas") and shown["v2"]["button"]  # v2 can be restored now


def test_a_rollback_without_any_holding_shows_the_restored_version_in_the_panel_as_soon_as_the_header_does(ui):
    page, _ = ui
    open_panel(page)
    restore = page.locator('[data-focus-key="ver:a.txt#1"]')
    restore.click()
    restore.click()
    page.wait_for_function("document.querySelector('#docs li').innerText.includes('Tayyor · v1')")
    shown = panel(page)  # read in the very next step: nothing is waited for, nothing is retried
    assert shown["v1"]["text"].startswith("Faol") and not shown["v1"]["button"]
    assert shown["v2"]["text"].startswith("Indekslangan, faol emas")


# ---------- late and out-of-order answers ----------

def test_an_older_versions_answer_never_overwrites_a_newer_one(ui):
    page, api = ui
    open_panel(page)
    stale = body_of(api.versions["a.txt"])  # v2 active, v1 can be restored
    fresh = body_of([version(2, "superseded", chunks=4), version(1, "active", chunks=4)])
    page.evaluate("window.__hold.versions = true")
    refresh(page, 2)  # each refresh asks for the versions again: an older and a newer request are now in flight
    page.wait_for_function("window.__pending.filter((p) => p.kind === 'versions').length === 2")
    answer(page, "versions", fresh, "newer", nth=1)  # the newer one is answered first ...
    assert panel(page)["v1"]["text"].startswith("Faol")
    answer(page, "versions", stale, "older", nth=0)  # ... and the older one arrives late
    shown = panel(page)  # the page has finished with the late answer: it must have been put aside
    assert shown["v1"]["text"].startswith("Faol") and "4 bo'lak" in shown["v1"]["text"]
    assert shown["v2"]["text"].startswith("Indekslangan, faol emas") and shown["v2"]["button"]


def test_an_older_answer_does_not_replace_a_newer_one_in_the_other_order_either(ui):
    page, api = ui
    open_panel(page)
    stale = body_of(api.versions["a.txt"])
    fresh = body_of([version(2, "superseded", chunks=4), version(1, "active", chunks=4)])
    page.evaluate("window.__hold.versions = true")
    refresh(page, 2)
    page.wait_for_function("window.__pending.filter((p) => p.kind === 'versions').length === 2")
    answer(page, "versions", stale, "older", nth=0)  # in order: the older answer first (it is shown for a moment) ...
    answer(page, "versions", fresh, "newer", nth=0)  # ... then the newer one replaces it
    shown = panel(page)
    assert shown["v1"]["text"].startswith("Faol") and "4 bo'lak" in shown["v1"]["text"]


def test_an_answer_asked_for_before_the_panel_was_closed_and_opened_again_is_ignored(ui):
    page, api = ui
    open_panel(page)
    stale = body_of(api.versions["a.txt"])
    fresh = body_of([version(2, "superseded", chunks=4), version(1, "active", chunks=4)])
    page.evaluate("window.__hold.versions = true")
    refresh(page)  # a request for the versions is in flight ...
    page.wait_for_function("window.__pending.filter((p) => p.kind === 'versions').length === 1")
    toggle = page.locator('[data-focus-key="doc:a.txt:versions"]')
    toggle.click()  # ... the panel is closed ...
    page.locator(".versions").wait_for(state="detached")
    toggle.click()  # ... and opened again, which asks once more
    page.wait_for_function("window.__pending.filter((p) => p.kind === 'versions').length === 2")
    answer(page, "versions", fresh, "newer", nth=1)
    answer(page, "versions", stale, "older", nth=0)  # the answer of the first request reaches the new panel late
    shown = panel(page)
    assert shown["v1"]["text"].startswith("Faol") and "4 bo'lak" in shown["v1"]["text"]


def test_an_old_failed_request_cannot_make_room_for_an_even_older_answer(ui):
    """Three requests are in flight. The newest is answered, then the oldest fails, then the middle one arrives with the
    state that has been left: a failure of an old request must not turn the numbering back."""
    page, api = ui
    open_panel(page)
    stale = body_of(api.versions["a.txt"])
    fresh = body_of([version(2, "superseded", chunks=4), version(1, "active", chunks=4)])
    page.evaluate("window.__hold.versions = true")
    refresh(page, 3)
    page.wait_for_function("window.__pending.filter((p) => p.kind === 'versions').length === 3")
    answer(page, "versions", fresh, "newest", nth=2)
    answer(page, "versions", {"no": "versions here"}, "failed", nth=0)  # the oldest request: not a list of versions
    answer(page, "versions", stale, "middle", nth=0)  # the middle one, with the old state, arrives last
    shown = panel(page)
    assert shown["v1"]["text"].startswith("Faol") and "4 bo'lak" in shown["v1"]["text"]
