"""Roadmap T0.5: the page sends every selected file in a request of its own, one after the other.

Real page in Edge. Every upload request is held in the page (window.__posts) until the test answers it, so the tests see
exactly what was sent, in which order, and that the next request does not leave before the previous one has been
answered. The API answers for everything else are scripted (no database, no Ollama). The flow against the real
application is in test_ui.py. Enable with RAG_E2E=1.
"""
import pytest

from tests.e2e.test_ui_focus_a11y import Api, active, base_url, browser, doc, refresh  # noqa: F401

pytestmark = pytest.mark.e2e

HOLD = """
window.__posts = [];   // uploads that were sent and have not been answered yet, oldest first
window.__sent = [];    // every upload that was ever sent: the names of the files in it
const realFetch = window.fetch.bind(window);
window.fetch = (url, options) => {
  if (String(url).endsWith('/api/documents') && options && options.method === 'POST') {
    const names = [...options.body.getAll('files')].map((f) => f.name);
    window.__sent.push(names);
    return new Promise((resolve, reject) => window.__posts.push({
      names,
      answer: (status, body) => resolve(new Response(JSON.stringify(body),
                                                     {status, headers: {'Content-Type': 'application/json'}})),
      fail: () => reject(new TypeError('Failed to fetch')),
    }));
  }
  return realFetch(url, options);
};
"""


@pytest.fixture
def ui(browser, base_url):  # noqa: F811 - the fixtures are imported from test_ui_focus_a11y
    api = Api()
    context = browser.new_context(viewport={"width": 1280, "height": 760})
    page = context.new_page()
    page.add_init_script(HOLD)
    page.route("**/api/**", api.handle)
    page.goto(base_url)
    page.locator("#docs li").nth(1).wait_for()
    yield page, api
    context.close()


def pick(page, *names):
    files = [{"name": n, "mimeType": "text/plain", "buffer": (n * 20).encode()} for n in names]
    page.set_input_files("#file-input", files)


def wait_pending(page, count):
    page.wait_for_function(f"window.__posts.length === {count}")


def sent(page):
    return page.evaluate("window.__sent")


def accepted(name):
    return {"results": [{"name": name, "accepted": True, "version": 1, "unchanged": False}]}


def answer_ok(page):
    name = page.evaluate("window.__posts[0].names[0]")
    page.evaluate("([name, body]) => window.__posts.shift().answer(200, body)", [name, accepted(name)])


def answer(page, status, body):
    page.evaluate("([status, body]) => window.__posts.shift().answer(status, body)", [status, body])


def status_text(page):
    return page.locator("#upload-status").inner_text()


# ---------- one request per file, one after the other ----------

def test_three_files_are_three_requests_sent_one_after_the_other_in_the_order_they_were_chosen(ui):
    page, _ = ui
    pick(page, "a.txt", "b.txt", "c.txt")
    wait_pending(page, 1)
    page.wait_for_timeout(400)  # the others must NOT have left meanwhile
    assert sent(page) == [["a.txt"]]
    answer_ok(page)
    wait_pending(page, 1)
    assert sent(page) == [["a.txt"], ["b.txt"]]  # the second left only after the first was answered
    page.wait_for_timeout(300)
    assert sent(page) == [["a.txt"], ["b.txt"]]
    answer_ok(page)
    wait_pending(page, 1)
    answer_ok(page)
    page.wait_for_function("document.querySelector('#upload-status').textContent === '3/3 fayl yuklandi'")
    assert sent(page) == [["a.txt"], ["b.txt"], ["c.txt"]]  # exactly three requests, one file each, in order
    assert all(len(names) == 1 for names in sent(page))
    assert page.locator("#upload-msg").is_hidden()


def test_the_page_says_which_file_is_being_sent_and_the_progress_follows(ui):
    page, _ = ui
    status = page.locator("#upload-status")
    bar = page.locator("#upload-progress")
    assert (status.get_attribute("role"), status.get_attribute("aria-live")) == ("status", "polite")
    assert status.is_hidden() and bar.is_hidden()  # nothing is shown before an upload
    pick(page, "a.txt", "b.txt", "c.txt")
    wait_pending(page, 1)
    assert status_text(page) == "1/3 fayl yuklanmoqda: a.txt"
    assert [bar.get_attribute(a) for a in ("aria-valuemin", "aria-valuemax", "aria-valuenow")] == ["0", "3", "0"]
    answer_ok(page)
    wait_pending(page, 1)
    assert status_text(page) == "2/3 fayl yuklanmoqda: b.txt"
    assert bar.get_attribute("aria-valuenow") == "1" and bar.get_attribute("aria-valuetext") == status_text(page)
    answer_ok(page)
    wait_pending(page, 1)
    assert status_text(page) == "3/3 fayl yuklanmoqda: c.txt" and bar.get_attribute("aria-valuenow") == "2"
    answer_ok(page)
    page.wait_for_function("document.querySelector('#upload-status').textContent === '3/3 fayl yuklandi'")
    assert bar.get_attribute("aria-valuenow") == "3"
    assert bar.get_attribute("aria-label") == "Fayllarni yuklash taraqqiyoti"


def test_a_failed_file_does_not_stop_the_rest_and_the_first_result_is_kept(ui):
    page, api = ui
    page.evaluate("clearTimeout(docsTimer)")  # no automatic polling: only the upload's own refresh can update the list
    names = page.locator("#docs .name")
    assert "n1.txt" not in names.all_inner_texts()
    pick(page, "n1.txt", "n2.txt", "n3.txt")
    wait_pending(page, 1)
    api.docs.append(doc("n1.txt"))  # what the server's list says once n1.txt has been taken over
    answer_ok(page)
    wait_pending(page, 1)
    # n1.txt is already handed over (it is in the list) while n2.txt is still being sent: nobody waited for the others
    page.wait_for_function("[...document.querySelectorAll('#docs .name')].some((n) => n.textContent === 'n1.txt')",
                           timeout=3000)
    answer(page, 413, {"detail": "Yuklash hajmi chegaradan katta: bitta so'rovda eng ko'pi 200 MB"})
    wait_pending(page, 1)  # n3.txt is sent although n2.txt failed
    assert sent(page) == [["n1.txt"], ["n2.txt"], ["n3.txt"]]
    answer_ok(page)
    page.wait_for_function(
        "document.querySelector('#upload-status').textContent === '2 ta fayl muvaffaqiyatli, 1 ta faylda xatolik'")
    message = page.locator("#upload-msg")
    assert message.is_visible() and message.get_attribute("role") == "alert"
    assert message.inner_text() == "n2.txt: Yuklash hajmi chegaradan katta: bitta so'rovda eng ko'pi 200 MB"  # by name
    assert "n1.txt" in names.all_inner_texts()  # the first file's result was kept


def test_every_kind_of_failure_is_reported_under_the_name_of_its_file(ui):
    page, _ = ui
    pick(page, "a.txt", "x.exe", "b.txt", "d.txt", "e.txt")
    wait_pending(page, 1)
    answer_ok(page)
    wait_pending(page, 1)
    refused = {"results": [{"name": "x.exe", "accepted": False, "error": "Bu fayl turi qo'llab-quvvatlanmaydi"}]}
    answer(page, 200, refused)  # the server refuses this one file
    wait_pending(page, 1)
    page.evaluate("window.__posts.shift().fail()")  # the connection breaks for b.txt
    wait_pending(page, 1)
    answer(page, 411, {"detail": "Content-Length sarlavhasi kerak"})  # d.txt: refused with 411
    wait_pending(page, 1)
    page.evaluate("window.__posts.shift().answer(500, '<html>not json</html>')")  # e.txt: an error page, not JSON
    page.wait_for_function(
        "document.querySelector('#upload-status').textContent === '1 ta fayl muvaffaqiyatli, 4 ta faylda xatolik'")
    lines = page.locator("#upload-msg").inner_text().split(" · ")
    assert [line.split(":")[0] for line in lines] == ["x.exe", "b.txt", "d.txt", "e.txt"]
    assert "qo'llab-quvvatlanmaydi" in lines[0] and "aloqa uzildi" in lines[1] and "Content-Length" in lines[2]
    assert sent(page) == [["a.txt"], ["x.exe"], ["b.txt"], ["d.txt"], ["e.txt"]]  # all five were tried


def test_one_file_works_as_before(ui):
    page, _ = ui
    pick(page, "yagona.txt")
    wait_pending(page, 1)
    assert status_text(page) == "1/1 fayl yuklanmoqda: yagona.txt"
    answer_ok(page)
    page.wait_for_function("document.querySelector('#upload-status').textContent === '1/1 fayl yuklandi'")
    assert sent(page) == [["yagona.txt"]] and page.locator("#upload-msg").is_hidden()
    # a success does not linger on the page
    page.wait_for_function("document.querySelector('#upload-status').hidden", timeout=10000)


def test_a_second_selection_made_during_an_upload_waits_its_turn(ui):
    page, _ = ui
    pick(page, "a.txt", "b.txt")
    wait_pending(page, 1)
    pick(page, "c.txt")  # chosen while a.txt is still being sent
    page.wait_for_timeout(400)
    assert sent(page) == [["a.txt"]]  # never two uploads at the same time
    answer_ok(page)
    wait_pending(page, 1)
    answer_ok(page)
    wait_pending(page, 1)
    answer_ok(page)
    page.wait_for_function("document.querySelector('#upload-status').textContent === '1/1 fayl yuklandi'")
    assert sent(page) == [["a.txt"], ["b.txt"], ["c.txt"]]


# ---------- focus, caret and the rest of the page during an upload ----------

def test_focus_caret_and_text_are_kept_during_the_whole_upload(ui):
    page, api = ui
    page.click("#question")
    page.keyboard.type("Salom dunyo")
    page.evaluate("document.getElementById('question').setSelectionRange(5, 5)")
    pick(page, "a.txt", "b.txt", "c.txt")  # choosing files does not move the focus away from the question either
    for step in range(3):
        wait_pending(page, 1)
        state = active(page)
        assert state["id"] == "question" and state["value"] == "Salom dunyo"
        assert (state["start"], state["end"]) == (5, 5)
        refresh(page)
        if step == 1:
            api.docs[1] = {**api.docs[1], "progress": {"unit": "chunks", "done": 50, "total": 100}}
        answer_ok(page)
    page.wait_for_function("document.querySelector('#upload-status').textContent === '3/3 fayl yuklandi'")
    state = active(page)
    assert state["id"] == "question" and (state["start"], state["end"]) == (5, 5)
    page.keyboard.type("X")
    assert page.input_value("#question") == "SalomX dunyo"


def test_an_upload_never_takes_the_focus_back_from_where_the_user_put_it(ui):
    page, _ = ui
    pick(page, "a.txt", "b.txt")
    wait_pending(page, 1)
    page.focus('[data-focus-key="doc:a.txt:versions"]')  # the user moves on while the upload goes
    answer_ok(page)
    wait_pending(page, 1)
    assert active(page)["key"] == "doc:a.txt:versions"
    answer(page, 500, {"detail": "xato"})
    page.wait_for_function("document.querySelector('#upload-status').textContent.includes('xatolik')")
    page.wait_for_timeout(300)
    assert active(page)["key"] == "doc:a.txt:versions"  # neither a file result nor the final alert moved it


def test_the_other_functions_of_the_page_work_during_an_upload(ui):
    page, api = ui
    pick(page, "a.txt", "b.txt")
    wait_pending(page, 1)
    page.locator('[data-focus-key="doc:a.txt:versions"]').click()  # the versions panel opens meanwhile
    page.locator(".versions").wait_for()
    page.fill("#question", "savol")
    page.press("#question", "Enter")  # and a question is asked
    page.wait_for_function("!document.querySelector('#send').disabled")
    assert "Javob" in page.locator(".msg.bot").last.inner_text()
    answer_ok(page)
    wait_pending(page, 1)
    answer_ok(page)
    page.wait_for_function("document.querySelector('#upload-status').textContent === '2/2 fayl yuklandi'")
