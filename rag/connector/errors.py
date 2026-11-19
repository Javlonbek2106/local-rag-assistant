"""Errors of the connector. Every one carries a FIXED `code` and nothing else: no text of a driver, no host, no user
name, no password. A driver's own message can contain all of them (connection strings, server banners), so it is
turned into a code at the border (drivers) and is never kept, logged or shown."""


class ConnectorError(Exception):
    """A request the connector refuses or cannot complete. `status` is the HTTP status the API answers with."""

    def __init__(self, code, status=400, reason=None):
        super().__init__(code)
        self.code, self.status, self.reason = code, status, reason


class DriverError(Exception):
    """The external database could not do what was asked. `code` is one of DRIVER_CODES."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


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
        "driver_missing",  # the optional driver library is not installed
        "failed",  # anything else (no detail is kept)
    }
)
WARNING_CODES = frozenset({"account_can_write"})  # the account used can change data: it should be a read-only one
