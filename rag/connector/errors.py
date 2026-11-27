"""Errors of the connector. Every one carries a FIXED `code` and nothing else: no text of a driver, no host, no user
name, no password. A driver's own message can contain all of them (connection strings, server banners), so it is
turned into a code at the border (drivers) and is never kept, logged or shown."""


class ConnectorError(Exception):
    """A request the connector refuses or cannot complete. `status` is the HTTP status the API answers with."""

    def __init__(self, code, status=400, reason=None, details=None):
        super().__init__(code)
        self.code, self.status, self.reason, self.details = code, status, reason, details


class DriverError(Exception):
    """The external database could not do what was asked. `code` is one of DRIVER_CODES."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ModelError(Exception):
    """The language model could not produce an answer (M4.3). `code` is one of MODEL_CODES; no text of the model server
    is kept: it can contain paths, model names and fragments of the prompt."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


MODEL_CODES = frozenset({"llm_timeout", "llm_unavailable", "llm_error", "llm_cancelled"})


# What a connection test or a discovery can fail with (stored as `last_test_error`; the UI turns each into a sentence).
DRIVER_CODES = frozenset(
    {
        "auth_failed",  # wrong user or password
        "database_not_found",  # the server answered, the database does not exist (or is not open to this user)
        "timeout",  # no answer in time
        "unreachable",  # refused, no route, closed
        "tls_failed",  # encryption could not be set up or the certificate is not accepted
        "permission_denied",  # the account may not read the metadata it was asked for
        "too_large",  # more objects or columns than the limits allow
        "account_can_write",  # M4.4: a query is only run with an account that cannot change anything
        "query_error",  # M4.4: the database refused the query itself (a value, a type, a name, a syntax)
        "cancelled",  # M4.4: the caller went away and the query was stopped
        "driver_missing",  # the optional driver library is not installed
        "failed",  # anything else (no detail is kept)
    }
)
WARNING_CODES = frozenset({"account_can_write"})  # the account used can change data: it should be a read-only one
