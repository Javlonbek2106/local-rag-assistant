"""Roadmap M4.1a: the sign-in panel of the connector in the real page (Edge), against a scripted /api/v1.

What the page must do: stay hidden when the connector is off; pair with a one-time code; keep the session token in
sessionStorage only (never localStorage, a cookie or the address); send it to /api/v1 and to nothing else (the document
and ask endpoints never see it); say so when the session ends; never keep the code. Enable with RAG_E2E=1.
"""
import json
from urllib.parse import urlparse

import pytest

from tests.e2e.test_ui_focus_a11y import Api, base_url, browser  # noqa: F401

pytestmark = pytest.mark.e2e
GOOD_CODE, TOKEN = "12345678", "tok-session-1"


class ConnectorApi(Api):
    """The documents as before, plus a scripted /api/v1: `mode` is "off", "on" or "unavailable" (503)."""

    def __init__(self, mode="on"):
        super().__init__()
        self.mode, self.signed_in, self.expired = mode, False, False
        self.sent = []  # (method, path, Authorization header) of every request the page made

    def handle(self, route):
        request = route.request
        path = urlparse(request.url).path
        self.sent.append((request.method, path, request.headers.get("authorization")))
        if not path.startswith("/api/v1/"):
            return super().handle(route)
        if path == "/api/v1/connector":  # always answers 200: whether the connector is on
            return self.reply(route, 200, {"enabled": self.mode != "off"})
        if self.mode == "off":
            return route.fulfill(status=404, content_type="application/json", body='{"detail":"Not Found"}')
        if self.mode == "unavailable":
            return self.reply(route, 503, {"error": {"code": "connector_unavailable"}})
        if path == "/api/v1/session/pairing":
            return self.reply(route, 200, {"requested": True, "ttl_s": 60})
        if path == "/api/v1/session/exchange":
            if json.loads(request.post_data or "{}").get("code") == GOOD_CODE:
                self.signed_in, self.expired = True, False
                return self.reply(route, 200, {"token": TOKEN, "expires_in_s": 720, "idle_timeout_s": 120})
            return self.reply(route, 401, {"error": {"code": "pairing_invalid"}})
        if path == "/api/v1/session" and request.method == "DELETE":
            self.signed_in = False
            return route.fulfill(status=204)
        if path == "/api/v1/session":
            valid = self.signed_in and not self.expired and request.headers.get("authorization") == f"Bearer {TOKEN}"
            if not valid:
                return self.reply(route, 401, {"error": {"code": "unauthenticated"}})
            return self.reply(route, 200, {"principal": {"id": "x", "kind": "local-owner"}, "absolute_remaining_s": 600,
                                           "idle_remaining_s": 100, "step_up_remaining_s": 0})
        return self.reply(route, 404, {"detail": "not scripted"})

    @staticmethod
    def reply(route, status, body):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def open_page(browser, base_url, api, context=None):  # noqa: F811 - fixtures are imported from test_ui_focus_a11y
    context = context or browser.new_context(viewport={"width": 1280, "height": 760})
    page = context.new_page()
    page.route("**/api/**", api.handle)
    page.goto(base_url)
    page.wait_for_selector("#docs li")
    page.wait_for_function("document.getElementById('connector-state') !== null")
    return context, page


def settle(page):
    """Ask once more and wait: refreshSession() returns when the answer has been drawn, so the state is settled."""
    page.evaluate("refreshSession()")


def sign_in(page, code=GOOD_CODE):
    page.locator("#pair-request").click()
    page.locator("#pair-code").fill(code)
    page.locator("#pair-send").click()


@pytest.fixture
def make(browser, base_url):  # noqa: F811
    contexts = []

    def go(mode="on"):
        api = ConnectorApi(mode)
        context, page = open_page(browser, base_url, api)
        contexts.append(context)
        settle(page)
        return page, api, context

    yield go
    for context in contexts:
        context.close()


def test_with_the_connector_off_the_panel_is_hidden_and_nothing_is_stored(make):
    page, api, _ = make("off")
    assert page.locator("#connector-auth").is_hidden()
    assert page.evaluate("sessionStorage.length") == 0
    assert ("GET", "/api/v1/connector", None) in api.sent  # it asked once whether the connector is on
    assert not [s for s in api.sent if s[1] == "/api/v1/session"]  # and, told it is off, never asked for a session


def test_a_signed_out_page_offers_the_code_and_says_where_to_find_it(make):
    page, _, _ = make()
    assert page.locator("#connector-auth").is_visible() and page.locator("#pair-box").is_visible()
    assert page.locator("#session-box").is_hidden()
    field = page.locator("#pair-code")
    assert field.get_attribute("autocomplete") == "off" and field.get_attribute("inputmode") == "numeric"
    assert page.locator("label[for=pair-code]").count() == 1  # a name for screen readers
    assert "start.bat" in page.locator("#pair-box").inner_text()


def test_pairing_signs_in_keeps_the_token_in_session_storage_only_and_never_the_code(make):
    page, api, context = make()
    sign_in(page)
    page.locator("#session-box").wait_for(state="visible")
    assert page.evaluate("sessionStorage.getItem('rag.session')") == TOKEN
    assert page.evaluate("localStorage.length") == 0 and page.evaluate("document.cookie") == ""
    assert context.cookies() == []
    assert GOOD_CODE not in page.url and TOKEN not in page.url and "#" not in page.url
    assert page.locator("#pair-code").input_value() == ""  # the code is cleared, whatever the answer
    assert GOOD_CODE not in page.content() and TOKEN not in page.content()  # nowhere in the page
    assert "daqiqa qoldi" in page.locator("#session-info").inner_text()
    assert page.evaluate("document.activeElement.id") == "session-end"  # the focus moved to what is there to do


def test_a_wrong_code_is_said_in_an_alert_and_the_field_is_cleared(make):
    page, _, _ = make()
    sign_in(page, "00000000")
    alert = page.locator("#connector-msg")
    alert.wait_for(state="visible")
    assert alert.get_attribute("role") == "alert" and "noto'g'ri" in alert.inner_text()
    assert page.locator("#pair-code").input_value() == "" and page.evaluate("sessionStorage.length") == 0


def test_the_token_goes_to_the_connector_routes_and_to_nothing_else(make):
    page, api, _ = make()
    sign_in(page)
    page.locator("#session-box").wait_for(state="visible")
    page.evaluate("refreshDocs()")
    page.evaluate("refreshSession()")
    documents = [s for s in api.sent if s[1] == "/api/documents"]
    connector = [s for s in api.sent if s[1] == "/api/v1/session" and s[2]]
    assert documents and all(auth is None for _m, _p, auth in documents)  # the document routes never see it
    assert connector and all(auth == f"Bearer {TOKEN}" for _m, _p, auth in connector)
    with pytest.raises(Exception, match="only for /api/v1"):
        page.evaluate("connectorFetch('/api/documents')")  # the helper refuses anything else


def test_the_session_survives_a_reload_of_the_tab_but_not_a_new_tab(make, browser, base_url):  # noqa: F811
    page, api, context = make()
    sign_in(page)
    page.locator("#session-box").wait_for(state="visible")
    page.reload()
    page.locator("#session-box").wait_for(state="visible")  # same tab: sessionStorage stays
    other = context.new_page()
    other.route("**/api/**", api.handle)
    other.goto(base_url)
    other.locator("#pair-box").wait_for(state="visible")  # another tab does not share it
    assert other.evaluate("sessionStorage.length") == 0


def test_when_the_session_ends_the_page_says_so_forgets_the_token_and_asks_again(make):
    page, api, _ = make()
    sign_in(page)
    page.locator("#session-box").wait_for(state="visible")
    api.expired = True
    page.evaluate("refreshSession()")
    page.locator("#pair-box").wait_for(state="visible")
    assert "Sessiya tugadi" in page.locator("#connector-msg").inner_text()
    assert page.locator("#connector-msg").get_attribute("role") == "alert"
    assert page.evaluate("sessionStorage.length") == 0
    assert "Sessiya tugadi" in page.locator("#sr-status").inner_text()  # and it is announced


def test_signing_out_ends_the_session_on_the_server_and_forgets_the_token(make):
    page, api, _ = make()
    sign_in(page)
    page.locator("#session-box").wait_for(state="visible")
    page.locator("#session-end").click()
    page.locator("#pair-box").wait_for(state="visible")
    assert ("DELETE", "/api/v1/session", f"Bearer {TOKEN}") in api.sent
    assert page.evaluate("sessionStorage.length") == 0 and api.signed_in is False
    assert page.evaluate("document.activeElement.id") == "pair-request"


def test_when_the_connector_is_unavailable_the_page_says_so_and_offers_no_code_field(make):
    page, _, _ = make("unavailable")
    assert page.locator("#connector-auth").is_visible() and page.locator("#pair-box").is_hidden()
    assert "ishlamayapti" in page.locator("#connector-state").inner_text()
    assert page.locator("#connector-state").get_attribute("role") == "status"


def test_the_documents_part_of_the_page_is_unchanged_by_the_panel(make):
    page, _, _ = make("off")
    assert page.locator("#docs li").count() == 2 and page.locator("#question").is_visible()
    assert page.locator("meta[name=referrer]").get_attribute("content") == "no-referrer"
