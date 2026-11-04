"""Drives the real UI in Edge against a real server on a private port and collection. Enable with RAG_E2E=1."""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import pytest

from rag import config, store
from tests.fixtures.make_samples import make_samples

pytestmark = pytest.mark.e2e
sync_api = pytest.importorskip("playwright.sync_api")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    samples = make_samples(tmp_path_factory.mktemp("samples"))
    docs = tmp_path_factory.mktemp("documents")
    for name in ("01_loyiha.md", "11_skan_rus.pdf", "02_ombor.docx"):
        shutil.copy(samples / name, docs / name)
    port, collection = free_port(), f"rag_e2e_{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "RAG_PORT": str(port), "RAG_COLLECTION": collection, "RAG_DOCS": str(docs),
           "HF_HUB_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "rag"], env=env, cwd=config.ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 120
        while True:
            try:
                urllib.request.urlopen(f"{url}/api/documents", timeout=2)
                break
            except OSError:
                assert proc.poll() is None and time.time() < deadline, "server did not start"
                time.sleep(1)
        yield url, samples
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        try:
            store.get_client().delete_collection(collection)
        except Exception:  # noqa: BLE001 - cleanup must not hide the real failure
            pass


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        yield b
        b.close()


def test_page_loads_under_the_strict_content_security_policy(server, browser):
    url, _ = server
    messages = []
    page = browser.new_page()
    page.on("console", lambda m: messages.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: messages.append(f"pageerror: {e}"))
    page.goto(url)
    page.wait_for_selector("#docs li")
    page.close()
    assert [m for m in messages if m.startswith(("error", "pageerror")) or "Content Security Policy" in m] == []


def test_upload_ask_delete_flow(server, browser):
    url, samples = server
    # CSP is bypassed only here so Playwright's own `wait_for_function` (which uses eval) can run; the test above
    # proves the page itself works under the strict policy.
    ctx = browser.new_context(viewport={"width": 1280, "height": 760}, bypass_csp=True)
    page = ctx.new_page()
    page.goto(url)
    page.wait_for_selector("#docs li")
    assert "01_loyiha.md" in page.locator("#docs .name").all_inner_texts()

    unsupported = samples / "x.exe"
    unsupported.write_bytes(b"MZ")
    page.set_input_files("#file-input", [str(samples / "05_release.pptx"), str(unsupported)])
    page.wait_for_function("!document.querySelector('#upload-msg').hidden")
    assert "x.exe" in page.inner_text("#upload-msg")
    page.wait_for_function(
        "[...document.querySelectorAll('#docs li')].some(li => li.innerText.includes('05_release.pptx')"
        " && li.innerText.includes('Tayyor'))", timeout=180000)

    page.fill("#question", "What is the codename for the Falcon release?")
    page.press("#question", "Enter")
    page.wait_for_function("!document.querySelector('#send').disabled", timeout=180000)
    bot = page.locator(".msg.bot").last
    assert "Blue Heron" in bot.inner_text()
    page.locator(".msg.bot summary").last.click()
    assert "05_release.pptx" in page.locator(".msg.bot .source").last.inner_text()

    page.fill("#question", "Direktorning telefon raqami nechchi?")
    page.click("#send")
    page.wait_for_function("!document.querySelector('#send').disabled", timeout=180000)
    last = page.locator(".msg.bot").last
    assert "topilmadi" in last.inner_text() and last.locator("details").count() == 0

    page.fill("#question", "<img src=x onerror=window.__xss=1> test")  # HTML must stay text
    page.click("#send")
    page.wait_for_function("!document.querySelector('#send').disabled", timeout=180000)
    assert page.evaluate("window.__xss === undefined") and page.locator(".msg.user img").count() == 0

    row = page.locator("#docs li", has_text="05_release.pptx")
    row.locator("button").click()
    assert row.locator("button").inner_text() == "Aniqmi?"  # deleting takes two clicks
    row.locator("button").click()
    page.wait_for_function(
        "![...document.querySelectorAll('#docs .name')].some(n => n.innerText === '05_release.pptx')", timeout=30000)

    page.set_viewport_size({"width": 390, "height": 780})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no sideways scroll on a phone
    ctx.close()
