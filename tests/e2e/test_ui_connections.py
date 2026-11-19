"""M4.1b: the connections panel in the real page (Edge), against a scripted /api/v1/connections.

What the page must do: show the panel only to a signed-in session; create, edit, test, enable/disable and delete
connections;
ask for a fresh code when the server wants a step-up and then finish what was interrupted; send only the fields that
changed;
clear the password the moment it is sent and never show it again; browse a discovered schema and save descriptions;
treat every
name that came from an external database as text. Enable with RAG_E2E=1.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from tests.e2e.test_ui_connector_auth import TOKEN, ConnectorApi, open_page, settle, sign_in
from tests.e2e.test_ui_focus_a11y import base_url, browser  # noqa: F401

pytestmark = pytest.mark.e2e
PASSWORD = "Sentinel-Pa55word!-zq81"
STEP_UP_CODE = "87654321"
HOSTILE = '<img src=x onerror="window.pwned=1">'

SCHEMA = {
    "schemas": [{"schema_name": "hr", "tables": 1, "views": 0}, {"schema_name": "sales", "tables": 2, "views": 1}],
    "objects": [
        {"schema_name": "hr", "object_name": "employee", "object_type": "table", "columns": 2},
        {"schema_name": "sales", "object_name": "big_orders", "object_type": "view", "columns": 2},
        {"schema_name": "sales", "object_name": "order_line", "object_type": "table", "columns": 3},
        {"schema_name": "sales", "object_name": HOSTILE, "object_type": "table", "columns": 1},
    ],
}
DETAIL = {
    "schema": "sales",
    "name": "order_line",
    "type": "table",
    "columns": [
        {"ordinal": 1, "column_name": "order_id", "data_type": "integer", "is_nullable": False},
        {"ordinal": 2, "column_name": "line_no", "data_type": "integer", "is_nullable": False},
        {"ordinal": 3, "column_name": "customer_id", "data_type": "integer", "is_nullable": False},
    ],
    "constraints": [
        {
            "kind": "primary_key",
            "constraint_name": "pk_ol",
            "columns": ["order_id", "line_no"],
            "ref_schema": None,
            "ref_object": None,
            "ref_columns": None,
        },
        {
            "kind": "foreign_key",
            "constraint_name": "fk_ol_customer",
            "columns": ["customer_id"],
            "ref_schema": "sales",
            "ref_object": "customer",
            "ref_columns": ["id"],
        },
    ],
    "indexes": [{"index_name": "ix_ol_customer", "columns": ["customer_id"], "is_unique": False, "is_primary": False}],
}


def connection(**changes):
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Sales",
        "description": "the sales db",
        "provider": "postgresql",
        "host": "db.example.org",
        "port": 5432,
        "database_name": "sales",
        "username": "reader",
        "ssl_mode": "verify-full",
        "options": {},
        "ca_certificate_set": False,
        "is_enabled": True,
        "status": "ready",
        "credential_stored": True,
        "created_at": "2026-11-19T05:00:00Z",
        "updated_at": "2026-11-19T05:00:00Z",
        "last_tested_at": None,
        "last_test_status": None,
        "last_test_error": None,
        "last_test_warning": None,
        "schema_discovered_at": None,
        **changes,
    }


class ConnectionsApi(ConnectorApi):
    """The sign-in routes of M4.1a, plus a scripted /api/v1/connections. `step_up` makes the next create/target change
    ask for a
    confirmation first; `body_seen` keeps every request body of the connection routes."""

    def __init__(self, connections=None):
        super().__init__("on")
        self.connections = list(connections or [])
        self.step_up, self.confirmed, self.body_seen = False, False, []
        self.test_result = {"status": "ok", "error": None, "warning": None, "server_version": "17.0"}
        self.descriptions = []
        self.fail = None  # (status, code) the next connection request gets instead
        # M4.2: a small stateful stand-in for snapshots, freshness, changes, selections and the context
        self.version, self.confirmed_version, self.revision = 1, 1, 0
        self.selection, self.changes, self.unavailable = [], [], set()
        self.fresh_extra = {}  # fields that replace those of the freshness answer (state, reasons, last_error ...)
        self.next_discover = None  # a list of changes the next discovery finds (then it makes a new version)
        self.context_extra = {}

    def freshness(self):
        unreviewed = [c for c in self.changes if c["to_version"] > self.confirmed_version]
        state = {
            "state": "stale" if unreviewed else "current",
            "reasons": ["unreviewed_changes"] if unreviewed else [],
            "in_progress": False,
            "has_snapshot": True,
            "last_error": None,
            "unreviewed_changes": len(unreviewed),
            "affected_selected": len(unreviewed),
            "removed_selected": 0,
            "orphaned_descriptions": sum(1 for d in self.descriptions if d.get("orphaned")),
            "snapshot": {
                "id": "s1",
                "version": self.version,
                "fingerprint": "f" * 64,
                "created_at": "2026-11-19T05:20:00Z",
                "verified_at": "2026-11-19T05:20:00Z",
                "objects": 4,
                "columns": 8,
            },
            "confirmed_version": self.confirmed_version,
        }
        state.update(self.fresh_extra)
        return state

    def handle(self, route):
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/v1/session/pairing":
            self.sent.append((request.method, path, request.headers.get("authorization")))
            self.body_seen.append(("POST", path, json.loads(request.post_data) if request.post_data else None))
            return self.reply(route, 200, {"requested": True, "ttl_s": 60})
        if path == "/api/v1/session/step-up":
            self.sent.append((request.method, path, request.headers.get("authorization")))
            if json.loads(request.post_data or "{}").get("code") == STEP_UP_CODE:
                self.confirmed = True
                return self.reply(
                    route, 200, {"token": TOKEN, "expires_in_s": 700, "idle_timeout_s": 120, "step_up_remaining_s": 600}
                )
            return self.reply(route, 401, {"error": {"code": "pairing_invalid"}})
        if not path.startswith("/api/v1/connections"):
            return super().handle(route)
        self.sent.append((request.method, path, request.headers.get("authorization")))
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return self.reply(route, 401, {"error": {"code": "unauthenticated"}})
        body = json.loads(request.post_data) if request.post_data else None
        self.body_seen.append((request.method, path, body))
        if self.fail:
            status, code = self.fail
            self.fail = None
            return self.reply(route, status, {"error": {"code": code}})
        rest = path[len("/api/v1/connections") :].strip("/").split("/")
        query = parse_qs(urlparse(request.url).query)
        method = request.method
        if rest == [""] and method == "GET":
            return self.reply(route, 200, {"connections": self.connections})
        if rest == [""] and method == "POST":
            if self.step_up and not self.confirmed:
                return self.reply(route, 403, {"error": {"code": "step_up_required"}})
            made = connection(
                id=f"2222222222-{len(self.connections)}", **{k: v for k, v in body.items() if k not in ("password",)}
            )
            self.connections.append(made)
            return self.reply(route, 201, made)
        cid = rest[0]
        current = next((c for c in self.connections if c["id"] == cid), None)
        if current is None:
            return self.reply(route, 404, {"error": {"code": "not_found"}})
        tail = "/".join(rest[1:])
        if tail == "" and method == "PATCH":
            if (
                {"provider", "host", "port", "database_name", "username", "ssl_mode", "options", "password"} & set(body)
                and self.step_up
                and not self.confirmed
            ):
                return self.reply(route, 403, {"error": {"code": "step_up_required"}})
            current.update({k: v for k, v in body.items() if k != "password"})
            return self.reply(route, 200, current)
        if tail == "" and method == "DELETE":
            self.connections.remove(current)
            return self.reply(route, 200, {"deleted": True})
        if tail in ("enable", "disable"):
            current["is_enabled"] = tail == "enable"
            current["status"] = "ready" if current["is_enabled"] else "disabled"
            return self.reply(route, 200, current)
        if tail == "test":
            current["last_tested_at"] = "2026-11-19T05:10:00Z"
            current["last_test_status"] = self.test_result["status"]
            current["last_test_error"] = self.test_result["error"]
            current["last_test_warning"] = self.test_result["warning"]
            return self.reply(route, 200, self.test_result)
        if tail == "schema/discover":
            current["schema_discovered_at"] = "2026-11-19T05:20:00Z"
            snapshot = {
                "version": self.version,
                "first": False,
                "changed": False,
                "changes": 0,
                "fingerprint": "f" * 64,
            }
            if self.next_discover:
                self.version += 1
                self.changes += [{**c, "to_version": self.version} for c in self.next_discover]
                snapshot.update(version=self.version, changed=True, changes=len(self.next_discover))
                self.next_discover = None
            return self.reply(
                route,
                200,
                {"objects": 4, "columns": 8, "schema_discovered_at": "2026-11-19T05:20:00Z", "snapshot": snapshot},
            )
        if tail == "schema/freshness":
            return self.reply(route, 200, self.freshness())
        if tail == "schema/selections" and method == "GET":
            items = [
                {**i, "available": (i["kind"], i["schema"], i["object"], i["column"]) not in self.unavailable}
                for i in self.selection
            ]
            return self.reply(
                route,
                200,
                {"revision": self.revision, "items": items, "unavailable": sum(1 for i in items if not i["available"])},
            )
        if tail == "schema/selections" and method == "PUT":
            if body.get("base_revision") != self.revision:
                return self.reply(route, 409, {"error": {"code": "selection_conflict"}})
            self.selection, self.revision = body["items"], self.revision + 1
            self.confirmed_version = self.version
            return self.reply(route, 200, {"revision": self.revision, "items": len(self.selection)})
        if tail == "schema/changes":
            since = int(query.get("since_version", ["0"])[0])
            rows = [c for c in self.changes if c["to_version"] > since]
            return self.reply(route, 200, {"changes": rows, "total": len(rows), "summary": {}})
        if tail == "schema/changes/acknowledge":
            self.confirmed_version = body["version"]
            return self.reply(route, 200, self.freshness())
        if tail == "schema/context":
            fresh = self.freshness()
            context = {
                "format": 1,
                "status": "ok" if fresh["state"] == "current" else fresh["state"],
                "usable": fresh["state"] == "current" and bool(self.selection),
                "complete": True,
                "selection": {
                    "revision": self.revision,
                    "selected_objects": len(self.selection),
                    "included_objects": len(self.selection),
                },
                "budget": {"max_chars": 12000, "used_chars": 321, "context_fingerprint": "c" * 64},
                "context": {
                    "database": {"name": "sales"},
                    "schemas": [{"name": "sales", "objects": [{"name": HOSTILE, "columns": []}]}],
                },
                **self.context_extra,
            }
            return self.reply(route, 200, context)
        if tail == "schema":
            return self.reply(route, 200, {"schemas": SCHEMA["schemas"]})
        if tail == "schema/objects":
            wanted = query.get("schema", [None])[0]
            text = (query.get("q", [""])[0] or "").lower()
            rows = [
                o
                for o in SCHEMA["objects"]
                if (not wanted or o["schema_name"] == wanted) and text in o["object_name"].lower()
            ]
            return self.reply(route, 200, {"objects": rows, "total": len(rows)})
        if tail == "schema/object":
            return self.reply(route, 200, DETAIL)
        if tail == "descriptions" and method == "GET":
            return self.reply(route, 200, {"descriptions": self.descriptions})
        if tail == "descriptions" and method == "PUT":
            self.descriptions = [
                d
                for d in self.descriptions
                if (d["target_kind"], d["schema_name"], d["object_name"], d["column_name"])
                != (body["target_kind"], body["schema_name"], body["object_name"], body["column_name"])
            ]
            if body["description"]:
                self.descriptions.append({**body, "updated_at": "2026-11-19T05:30:00Z"})
            return self.reply(route, 200, body)
        return self.reply(route, 404, {"error": {"code": "not_found"}})


@pytest.fixture
def make(browser, base_url):  # noqa: F811
    contexts, consoles = [], []

    def go(api=None, signed_in=True):
        api = api or ConnectionsApi()
        context, page = open_page(browser, base_url, api)
        contexts.append(context)
        page.on("console", lambda message: consoles.append((message.type, message.text)))
        settle(page)
        if signed_in:
            sign_in(page)
            page.locator("#session-box").wait_for(state="visible")
            page.locator("#connections").wait_for(state="visible")
            page.locator("#connections > summary").click()  # open the panel
        return page, api, context

    go.consoles = consoles
    yield go
    for context in contexts:
        context.close()


def fill_form(page, **values):
    fields = {
        "name": "#cn-name",
        "description": "#cn-description",
        "host": "#cn-host",
        "port": "#cn-port",
        "database": "#cn-database",
        "username": "#cn-username",
        "password": "#cn-password",
        "ca": "#cn-ca",
    }
    for key, value in values.items():
        page.locator(fields[key]).fill(str(value))


def open_new(page):
    page.locator("#cn-new").click()
    page.locator("#cn-form").wait_for(state="visible")


def result_message(page):
    """The text of the message box once the action has finished (the "in progress" text is replaced by the result)."""
    page.locator("#cn-msg").wait_for(state="visible")
    page.wait_for_function(
        "(() => { const t = document.getElementById('cn-msg').textContent;"
        " return !t.startsWith('Sinalmoqda') && !t.startsWith('Sxema aniqlanmoqda'); })()"
    )
    return page.locator("#cn-msg").inner_text()


NEW = dict(name="Sales", host="db.example.org", port=5432, database="sales", username="reader", password=PASSWORD)


# ---------- who sees the panel ----------


def test_the_panel_is_hidden_until_a_session_exists_and_after_it_ends(make):
    page, api, _ = make(signed_in=False)
    assert page.locator("#connections").is_hidden()
    sign_in(page)
    page.locator("#connections").wait_for(state="visible")
    page.locator("#session-end").click()
    page.locator("#pair-box").wait_for(state="visible")
    assert page.locator("#connections").is_hidden() and page.locator("#cn-list li").count() == 0


def test_the_panel_is_not_there_when_the_connector_is_off(browser, base_url):  # noqa: F811
    api = ConnectorApi("off")
    context, page = open_page(browser, base_url, api)
    settle(page)
    assert page.locator("#connections").is_hidden()
    assert not [s for s in api.sent if s[1].startswith("/api/v1/connections")]
    context.close()


def test_an_empty_list_says_so(make):
    page, _, _ = make()
    page.locator("#cn-empty").wait_for(state="visible")
    assert page.locator("#cn-list li").count() == 0


# ---------- creating ----------


def test_a_connection_is_created_after_a_step_up_and_the_password_is_gone_from_the_page(make):
    api = ConnectionsApi()
    api.step_up = True
    page, api, _ = make(api)
    open_new(page)
    fill_form(page, **NEW)
    page.locator("#cn-save").click()
    page.locator("#cn-stepup").wait_for(state="visible")  # the server asked for a fresh confirmation
    assert api.connections == [] and page.locator("#cn-form").is_visible()  # nothing yet, and the form is kept
    page.locator("#cn-stepup-request").click()
    page.locator("#cn-stepup-code").fill(STEP_UP_CODE)
    page.get_by_role("button", name="Tasdiqlash").click()
    page.locator("#cn-form").wait_for(state="hidden")  # finished by itself, with no further click
    page.locator("#cn-list li").first.wait_for()
    assert page.locator("#cn-stepup").is_hidden()
    assert len(api.connections) == 1 and api.connections[0]["host"] == "db.example.org"
    posts = [b for m, p, b in api.body_seen if m == "POST" and p == "/api/v1/connections"]
    assert len(posts) == 2 and all(b["password"] == PASSWORD for b in posts)  # sent twice, the same
    assert ("POST", "/api/v1/session/pairing", {"purpose": "step-up"}) in api.body_seen
    assert page.locator("#cn-password").input_value() == "" and page.locator("#cn-stepup-code").input_value() == ""
    shown = page.content() + page.evaluate("JSON.stringify([sessionStorage, localStorage])") + page.url
    assert PASSWORD not in shown and STEP_UP_CODE not in shown
    assert page.evaluate("document.cookie") == ""


def test_a_wrong_step_up_code_keeps_the_form_and_says_so(make):
    api = ConnectionsApi()
    api.step_up = True
    page, api, _ = make(api)
    open_new(page)
    fill_form(page, **NEW)
    page.locator("#cn-save").click()
    page.locator("#cn-stepup").wait_for(state="visible")
    page.locator("#cn-stepup-code").fill("00000000")
    page.get_by_role("button", name="Tasdiqlash").click()
    page.locator("#cn-msg").wait_for(state="visible")
    assert "noto'g'ri" in page.locator("#cn-msg").inner_text() and api.connections == []
    assert page.locator("#cn-stepup-code").input_value() == ""
    page.get_by_role("button", name="Bekor qilish").first.click()
    assert page.locator("#cn-stepup").is_hidden()


def test_the_form_has_a_label_for_every_field_and_a_password_field_that_does_not_autofill(make):
    page, _, _ = make()
    open_new(page)
    for field in (
        "cn-name",
        "cn-description",
        "cn-provider",
        "cn-host",
        "cn-port",
        "cn-database",
        "cn-username",
        "cn-password",
        "cn-ssl",
        "cn-ca",
        "cn-timeout",
    ):
        assert page.locator(f"label[for={field}]").count() == 1, field
    password = page.locator("#cn-password")
    assert password.get_attribute("type") == "password" and password.get_attribute("autocomplete") == "new-password"
    assert page.locator("#cn-ssl").input_value() == "verify-full"  # the strict mode is the default


def test_choosing_the_provider_sets_its_default_port(make):
    page, _, _ = make()
    open_new(page)
    page.locator("#cn-provider").select_option("mssql")
    assert page.locator("#cn-port").input_value() == "1433"
    page.locator("#cn-provider").select_option("postgresql")
    assert page.locator("#cn-port").input_value() == "5432"


def test_a_refusal_of_the_server_is_told_in_words_and_the_form_stays(make):
    page, api, _ = make()
    open_new(page)
    fill_form(page, **NEW)
    api.fail = (400, "tls_required")
    page.locator("#cn-save").click()
    page.locator("#cn-msg").wait_for(state="visible")
    assert "TLS" in page.locator("#cn-msg").inner_text() or "shifrlash" in page.locator("#cn-msg").inner_text()
    assert page.locator("#cn-msg").get_attribute("role") == "alert" and page.locator("#cn-form").is_visible()
    assert page.locator("#cn-password").input_value() == PASSWORD  # it was not accepted: the person may fix and retry


# ---------- the list and its actions ----------


def listed(**changes):
    return ConnectionsApi([connection(**changes)])


def test_a_connection_is_listed_with_what_matters_and_without_any_credential(make):
    api = listed(
        last_tested_at="2026-11-19T05:10:00Z", last_test_status="ok", schema_discovered_at="2026-11-19T05:20:00Z"
    )
    page, _, _ = make(api)
    row = page.locator("#cn-list li").first
    row.wait_for()
    text = row.inner_text()
    for part in (
        "Sales",
        "PostgreSQL",
        "db.example.org:5432",
        "sales",
        "reader",
        "Tayyor",
        "muvaffaqiyatli",
        "Sxema aniqlangan",
    ):
        assert part in text, part
    assert "password" not in page.content().lower().replace('autocomplete="new-password"', "").replace(
        'type="password"', ""
    ).replace('id="cn-password"', "").replace('for="cn-password"', "")


def test_the_status_words_for_disabled_and_missing_credentials(make):
    page, _, _ = make(
        ConnectionsApi(
            [
                connection(name="off", is_enabled=False, status="disabled"),
                connection(id="3", name="lost", status="needs_credentials"),
            ]
        )
    )
    page.locator("#cn-list li").nth(1).wait_for()
    assert "O'chirilgan" in page.locator("#cn-list li").nth(0).inner_text()
    assert "Parol kerak" in page.locator("#cn-list li").nth(1).inner_text()


def test_testing_reports_success_with_the_version_and_a_warning_for_an_account_that_can_write(make):
    api = listed()
    api.test_result = {"status": "ok", "error": None, "warning": "account_can_write", "server_version": "17.0"}
    page, api, _ = make(api)
    page.get_by_role("button", name="Sinash").wait_for()
    page.get_by_role("button", name="Sinash").click()
    message = result_message(page)
    assert "ishlayapti" in message and "17.0" in message and "o'zgartira oladi" in message
    assert "muvaffaqiyatli" in page.locator("#cn-list li").first.inner_text()


@pytest.mark.parametrize(
    "code,words",
    [
        ("auth_failed", "parol"),
        ("timeout", "vaqt"),
        ("unreachable", "ulanib bo'lmadi"),
        ("tls_failed", "sertifikat"),
        ("database_not_found", "Baza topilmadi"),
    ],
)
def test_a_failed_test_is_told_by_its_code_in_words(make, code, words):
    api = listed()
    api.test_result = {"status": "failed", "error": code, "warning": None, "server_version": None}
    page, _, _ = make(api)
    page.get_by_role("button", name="Sinash").click()
    assert words.lower() in result_message(page).lower()


def test_a_disabled_connection_is_enabled_again_with_the_button(make):
    page, api, _ = make(listed())
    page.get_by_role("button", name="O'chirib qo'yish").click()
    page.locator(".cn-status.disabled").wait_for()
    page.get_by_role("button", name="Yoqish").click()
    page.locator(".cn-status.ready").wait_for()
    assert [p for m, p, b in api.body_seen if p.endswith(("/disable", "/enable"))] == [
        "/api/v1/connections/11111111-1111-1111-1111-111111111111/disable",
        "/api/v1/connections/11111111-1111-1111-1111-111111111111/enable",
    ]


def test_deleting_needs_a_second_click(make):
    page, api, _ = make(listed())
    delete = page.locator("#cn-list li").first.locator("button.danger")  # (its name changes once it is armed)
    delete.click()
    assert "Aniqmi" in delete.inner_text() and len(api.connections) == 1
    delete.click()
    page.locator("#cn-empty").wait_for(state="visible")
    assert api.connections == []


def test_editing_sends_only_what_changed_and_a_rename_needs_no_step_up(make):
    api = listed()
    api.step_up = True
    page, api, _ = make(api)
    page.get_by_role("button", name="Tahrirlash").click()
    assert page.locator("#cn-form-title").inner_text().startswith("Tahrirlash")
    assert (
        page.locator("#cn-host").input_value() == "db.example.org" and page.locator("#cn-password").input_value() == ""
    )
    assert "o'zgartirilmaydi" in page.locator("#cn-password").get_attribute("placeholder")
    page.locator("#cn-name").fill("Renamed")
    page.locator("#cn-save").click()
    page.locator("#cn-list li").first.get_by_text("Renamed").wait_for()
    patch = [b for m, p, b in api.body_seen if m == "PATCH"][-1]
    assert patch == {"name": "Renamed"} and page.locator("#cn-stepup").is_hidden()


def test_changing_the_host_needs_a_step_up_and_then_sends_just_the_host(make):
    api = listed()
    api.step_up = True
    page, api, _ = make(api)
    page.get_by_role("button", name="Tahrirlash").click()
    page.locator("#cn-host").fill("other.example.org")
    page.locator("#cn-save").click()
    page.locator("#cn-stepup").wait_for(state="visible")
    page.locator("#cn-stepup-code").fill(STEP_UP_CODE)
    page.get_by_role("button", name="Tasdiqlash").click()
    page.locator("#cn-list li").first.get_by_text("other.example.org").wait_for()
    patches = [b for m, p, b in api.body_seen if m == "PATCH"]
    assert patches == [{"host": "other.example.org"}, {"host": "other.example.org"}]


def test_a_new_password_is_sent_only_when_typed(make):
    page, api, _ = make(listed())
    page.get_by_role("button", name="Tahrirlash").click()
    page.locator("#cn-password").fill("typed-new-secret-0001")
    page.locator("#cn-save").click()
    page.locator("#cn-form").wait_for(state="hidden")
    assert [b for m, p, b in api.body_seen if m == "PATCH"] == [{"password": "typed-new-secret-0001"}]
    assert "typed-new-secret-0001" not in page.content()
    page.get_by_role("button", name="Tahrirlash").click()
    page.locator("#cn-save").click()  # nothing changed
    assert "o'zgarmadi" in page.locator("#cn-msg").inner_text()
    assert len([1 for m, p, b in api.body_seen if m == "PATCH"]) == 1


def test_names_that_came_from_outside_are_text_never_markup(make):
    page, api, _ = make(
        ConnectionsApi([connection(name=HOSTILE, description=HOSTILE, host="h.example.org", database_name=HOSTILE)])
    )
    page.locator("#cn-list li").first.wait_for()
    assert page.evaluate("window.pwned") is None and page.locator("#cn-list img").count() == 0
    assert HOSTILE in page.locator("#cn-list li").first.inner_text()


# ---------- the schema browser ----------


def open_schema(page):
    page.get_by_role("button", name="Sxemani ko'rish").click()
    page.locator("#cn-schema").wait_for(state="visible")
    page.locator("#cn-objects li").first.wait_for()


def test_discovery_reports_what_it_found(make):
    page, api, _ = make(listed())
    page.get_by_role("button", name="Sxemani aniqlash").click()
    message = result_message(page)
    assert "4 jadval" in message and "8 ustun" in message
    assert "Sxema aniqlangan" in page.locator("#cn-list li").first.inner_text()


def test_the_schema_browser_lists_schemas_and_objects_and_filters_them(make):
    page, api, _ = make(listed())
    open_schema(page)
    assert (
        page.locator("#cn-objects li").count() == 4 and "4 ta obyekt" in page.locator("#cn-objects-total").inner_text()
    )
    page.get_by_role("button", name="hr (1 + 0)").click()
    page.wait_for_function("document.querySelectorAll('#cn-objects li').length === 1")
    assert "hr.employee" in page.locator("#cn-objects li").first.inner_text()
    page.get_by_role("button", name="Hamma sxema").click()
    page.locator("#cn-search").fill("order")
    page.wait_for_function("document.querySelectorAll('#cn-objects li').length === 2")
    # the hostile name is text
    assert page.locator("#cn-objects img").count() == 0 and page.evaluate("window.pwned") is None


def test_a_table_shows_columns_keys_and_relationships(make):
    page, _, _ = make(listed())
    open_schema(page)
    page.get_by_role("button", name="sales.order_line").click()
    page.locator("#cn-object table").wait_for()
    rows = page.locator("#cn-object table tr")
    assert rows.count() == 4  # the header and three columns
    text = page.locator("#cn-object").inner_text()
    assert "order_id" in text and "integer" in text and "PK" in text and "FK" in text
    assert "Birlamchi kalit pk_ol: (order_id, line_no)" in text
    assert "→ sales.customer (id)" in text and "ix_ol_customer" in text


def test_descriptions_are_saved_for_the_database_a_table_and_a_column(make):
    page, api, _ = make(listed())
    open_schema(page)
    page.locator("#cn-schema-top textarea").first.fill("the whole sales database")
    page.locator("#cn-schema-top").get_by_role("button", name="Saqlash").first.click()
    page.get_by_role("button", name="sales.order_line").click()
    page.locator("#cn-object table").wait_for()
    page.locator("#cn-object > .cn-desc-edit textarea").fill("one row per order line")
    page.locator("#cn-object > .cn-desc-edit").get_by_role("button", name="Saqlash").click()
    page.locator("#cn-object table tr").nth(1).locator("textarea").fill("the order")
    page.locator("#cn-object table tr").nth(1).get_by_role("button", name="Saqlash").click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('saqlandi')")
    puts = [b for m, p, b in api.body_seen if m == "PUT"]
    assert {
        (b["target_kind"], b["schema_name"], b["object_name"], b["column_name"], b["description"]) for b in puts
    } == {
        ("database", "", "", "", "the whole sales database"),
        ("table", "sales", "order_line", "", "one row per order line"),
        ("column", "sales", "order_line", "order_id", "the order"),
    }
    page.locator("#cn-schema-close").click()
    open_schema(page)  # reopened: what was written is shown again
    assert page.locator("#cn-schema-top textarea").first.input_value() == "the whole sales database"


def test_a_description_emptied_is_removed(make):
    api = listed()
    api.descriptions = [
        {
            "target_kind": "database",
            "schema_name": "",
            "object_name": "",
            "column_name": "",
            "description": "old text",
            "updated_at": "x",
        }
    ]
    page, api, _ = make(api)
    open_schema(page)
    box = page.locator("#cn-schema-top textarea").first
    assert box.input_value() == "old text"
    box.fill("")
    page.locator("#cn-schema-top").get_by_role("button", name="Saqlash").first.click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('o\\'chirildi')")
    assert api.descriptions == []


# ---------- sessions, tokens, quiet console ----------


def test_the_token_goes_only_to_api_v1_and_the_page_logs_no_error(make):
    page, api, _ = make(listed())
    page.locator("#cn-list li").first.wait_for()
    quiet_from = len(make.consoles)  # (the 401 of the first, signed-out look at the session is expected and was before)
    page.get_by_role("button", name="Sinash").click()
    result_message(page)
    mine = [s for s in api.sent if s[1].startswith("/api/v1/connections")]
    assert mine and all(auth == f"Bearer {TOKEN}" for _m, _p, auth in mine)
    assert all(auth is None for _m, p, auth in api.sent if p.startswith("/api/documents"))
    assert not [c for c in make.consoles[quiet_from:] if c[0] == "error"]


def test_an_expired_session_during_an_action_returns_to_the_code_box(make):
    page, api, _ = make(listed())
    api.signed_in, api.expired = False, True
    api.fail = (401, "unauthenticated")
    page.get_by_role("button", name="Sinash").click()
    page.locator("#pair-box").wait_for(state="visible")
    assert page.locator("#connections").is_hidden() and page.evaluate("sessionStorage.length") == 0
    assert page.locator("#cn-list li").count() == 0  # nothing of the connections stays on the page


# ---------- M4.2: freshness, changes, selection and the context preview ----------


def table_box(page, name):
    return page.get_by_label(f"{name} ni AI kontekstiga qo'shish", exact=True)


def test_the_schema_panel_shows_the_freshness_the_version_and_a_short_fingerprint(make):
    page, api, _ = make(listed())
    open_schema(page)
    text = page.locator("#cn-fresh").inner_text()
    assert "Holat: Yangi" in text and "Versiya 1" in text and "f" * 12 in text and "f" * 13 not in text
    assert page.locator("#cn-fresh .cn-fresh-state.current").count() == 1
    assert page.get_by_role("button", name="Ko'rib chiqdim").count() == 0  # nothing to acknowledge


def test_a_failed_or_never_discovered_connection_says_so_and_shows_the_reason_apart(make):
    api = listed()
    api.fresh_extra = {"state": "failed", "reasons": ["last_discovery_failed"], "last_error": "timeout"}
    page, _, _ = make(api)
    open_schema(page)
    text = page.locator("#cn-fresh").inner_text()
    assert "Oxirgi aniqlash xato bilan tugadi" in text and "oxirgi aniqlash xato bilan tugagan" in text
    assert "Oxirgi xato: " in text and "vaqt tugadi" in text
    assert page.locator("#cn-fresh .cn-fresh-state.failed").count() == 1


def test_a_rediscovery_that_found_changes_makes_it_stale_lists_them_and_acknowledging_makes_it_current(make):
    api = listed()
    api.selection = [{"kind": "table", "schema": "sales", "object": "order_line", "column": "", "mode": "include"}]
    api.revision = 1
    page, api, _ = make(api)
    open_schema(page)
    api.next_discover = [
        {
            "object_type": "column",
            "schema_name": "sales",
            "object_name": "order_line",
            "sub_name": "qty",
            "change_type": "added",
        },
        {"object_type": "table", "schema_name": "hr", "object_name": "gone", "sub_name": "", "change_type": "removed"},
    ]
    page.get_by_role("button", name="Sxemani aniqlash").click()
    assert "Yangi versiya 2: 2 o'zgarish" in result_message(page)
    page.get_by_role("button", name="Sxemani ko'rish").click()
    page.locator("#cn-fresh .cn-fresh-state.stale").wait_for()
    text = page.locator("#cn-fresh").inner_text()
    assert "Holat: Eskirgan" in text and "tanlangan obyektlar o'zgargan" in text and "Versiya 2" in text
    page.get_by_role("button", name="O'zgarishlarni ko'rish").click()
    page.locator("#cn-changes").wait_for(state="visible")
    listing = page.locator("#cn-changes").inner_text()
    assert "v2: ustun sales.order_line.qty qo'shildi" in listing and "v2: jadval hr.gone o'chdi" in listing
    page.get_by_role("button", name="Ko'rib chiqdim").click()
    page.locator("#cn-fresh .cn-fresh-state.current").wait_for()
    assert page.locator("#cn-changes").is_hidden()
    assert (
        "POST",
        f"/api/v1/connections/{api.connections[0]['id']}/schema/changes/acknowledge",
        {"version": 2},
    ) in api.body_seen


def test_ticking_tables_and_columns_saves_a_valid_selection_with_the_revision_it_was_read_at(make):
    page, api, _ = make(listed())
    open_schema(page)
    page.get_by_label("sales sxemasini AI kontekstiga qo'shish", exact=True).check()
    assert table_box(page, "sales.order_line").is_checked()  # inherited from its schema
    table_box(
        page, "sales.big_orders"
    ).uncheck()  # leaves the schema selection: an exclusion, not a removal of the schema
    assert not table_box(page, "sales.big_orders").is_checked() and table_box(page, "sales.order_line").is_checked()
    page.get_by_role("button", name="sales.order_line").click()
    page.locator("#cn-object table").wait_for()
    page.get_by_label(
        "order_line.customer_id ustunini AI kontekstiga qo'shish", exact=True
    ).uncheck()  # a column exclusion
    assert "Saqlanmagan o'zgarish bor" in page.locator("#cn-selection-note").inner_text()
    page.get_by_role("button", name="Tanlovni saqlash").click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('Tanlov saqlandi')")
    put = [b for m, p, b in api.body_seen if m == "PUT" and p.endswith("/schema/selections")][-1]
    assert put["base_revision"] == 0
    assert {(i["kind"], i["schema"], i["object"], i["column"], i["mode"]) for i in put["items"]} == {
        ("schema", "sales", "", "", "include"),
        ("table", "sales", "big_orders", "", "exclude"),
        ("column", "sales", "order_line", "customer_id", "exclude"),
    }
    assert api.revision == 1 and "Saqlanmagan" not in page.locator("#cn-selection-note").inner_text()


def test_columns_of_a_table_not_otherwise_selected_make_it_partial_and_selecting_the_table_replaces_them(make):
    page, api, _ = make(listed())
    open_schema(page)
    page.get_by_role("button", name="sales.order_line").click()
    page.locator("#cn-object table").wait_for()
    page.get_by_label("order_line.order_id ustunini AI kontekstiga qo'shish", exact=True).check()
    assert page.evaluate("document.querySelector('#cn-objects input[data-object=order_line]').indeterminate") is True
    page.get_by_role("button", name="Tanlovni saqlash").click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('Tanlov saqlandi')")
    assert [(i["kind"], i["column"], i["mode"]) for i in api.selection] == [("column", "order_id", "include")]
    table_box(page, "sales.order_line").click()  # a click on a partly selected table clears it
    page.get_by_role("button", name="Tanlovni saqlash").click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('Tanlov saqlandi')")
    assert api.selection == []
    table_box(page, "sales.order_line").click()  # and on an unselected one selects all of it
    page.get_by_role("button", name="Tanlovni saqlash").click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('Tanlov saqlandi')")
    assert [(i["kind"], i["mode"]) for i in api.selection] == [("table", "include")]


def test_a_selection_changed_elsewhere_is_refused_with_a_clear_message_and_nothing_is_lost_silently(make):
    page, api, _ = make(listed())
    open_schema(page)
    api.revision = 5  # someone else saved meanwhile
    table_box(page, "hr.employee").check()
    page.get_by_role("button", name="Tanlovni saqlash").click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('boshqa joyda')")
    assert api.selection == [] and table_box(page, "hr.employee").is_checked()  # the user's edit is still on the page


def test_the_stored_selection_is_shown_and_items_whose_target_is_gone_can_be_removed(make):
    api = listed()
    api.selection = [
        {"kind": "table", "schema": "hr", "object": "employee", "column": "", "mode": "include"},
        {"kind": "table", "schema": "hr", "object": "vanished", "column": "", "mode": "include"},
    ]
    api.unavailable = {("table", "hr", "vanished", "")}
    api.revision = 3
    page, api, _ = make(api)
    open_schema(page)
    assert table_box(page, "hr.employee").is_checked()
    gone = page.locator("#cn-unavailable")
    assert "Endi mavjud emas: hr.vanished" in gone.inner_text()
    gone.get_by_role("button", name="Olib tashlash").click()
    assert page.locator("#cn-unavailable").is_hidden()
    page.get_by_role("button", name="Tanlovni saqlash").click()
    page.wait_for_function("document.getElementById('cn-msg').textContent.includes('Tanlov saqlandi')")
    assert [i["object"] for i in api.selection] == ["employee"] and api.revision == 4


def test_descriptions_of_vanished_objects_are_listed_and_can_be_removed(make):
    api = listed()
    api.descriptions = [
        {
            "target_kind": "table",
            "schema_name": "hr",
            "object_name": "gone",
            "column_name": "",
            "description": "old notes",
            "orphaned": True,
            "updated_at": "x",
        }
    ]
    page, api, _ = make(api)
    open_schema(page)
    assert "Obyekti yo'q: hr.gone — old notes" in page.locator("#cn-orphans").inner_text()
    assert "Obyekti yo'q tavsiflar: 1" in page.locator("#cn-fresh").inner_text()
    page.locator("#cn-orphans").get_by_role("button", name="O'chirish").click()
    page.wait_for_function("document.getElementById('cn-orphans').hidden")
    assert api.descriptions == []


def test_the_context_preview_shows_what_the_backend_returned_as_text_and_its_status(make):
    page, api, _ = make(listed())
    api.selection = [{"kind": "schema", "schema": "sales", "object": "", "column": "", "mode": "include"}]
    api.revision = 1
    open_schema(page)
    page.get_by_role("button", name="AI konteksti ko'rinishi").click()
    page.locator("#cn-context pre").wait_for()
    head = page.locator("#cn-context").inner_text()
    assert "Holat: ok" in head and "AI uchun yaroqli" in head and "321/12000 belgi" in head
    assert '"name": "sales"' in page.locator("#cn-context pre").inner_text()
    assert (
        page.locator("#cn-context img").count() == 0 and page.evaluate("window.pwned") is None
    )  # a hostile name is text
    asked = [p for m, p, b in api.body_seen if m == "GET" and p.endswith("/schema/context")]
    assert asked and any("schema/context" in p for p in asked)


def test_a_stale_context_is_shown_as_not_usable(make):
    api = listed()
    api.changes = [
        {
            "object_type": "table",
            "schema_name": "sales",
            "object_name": "x",
            "sub_name": "",
            "change_type": "added",
            "to_version": 2,
        }
    ]
    api.version, api.confirmed_version = 2, 1
    page, _, _ = make(api)
    open_schema(page)
    page.get_by_role("button", name="AI konteksti ko'rinishi").click()
    page.locator("#cn-context pre").wait_for()
    assert (
        "Holat: stale" in page.locator("#cn-context").inner_text()
        and "AI uchun yaroqsiz" in page.locator("#cn-context").inner_text()
    )


def test_closing_the_schema_clears_what_it_showed_and_the_console_stays_quiet(make):
    page, api, _ = make(listed())
    quiet_from = len(make.consoles)
    open_schema(page)
    page.get_by_role("button", name="AI konteksti ko'rinishi").click()
    page.locator("#cn-context pre").wait_for()
    page.locator("#cn-schema-close").click()
    assert page.locator("#cn-context").is_hidden() and page.locator("#cn-fresh").inner_text() == ""
    assert not [c for c in make.consoles[quiet_from:] if c[0] == "error"]
