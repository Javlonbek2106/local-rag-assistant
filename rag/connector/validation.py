"""What a connection may look like. Pure functions: no network, no database (the network judgement is network.py).

Every field is checked against a narrow shape. Nothing a person types is ever put into a connection STRING: the
drivers get separate parameters (host, port, database, user, password), so a value like `x; sslmode=disable` or `a
b=c` is just a strange database name, never another setting. Even so the shapes are strict: no control character
anywhere, and options are a closed set (an unknown option is refused, not ignored).
"""

import re

from rag.connector.errors import ConnectorError
from rag.connector.network import normalise_host

PROVIDERS = {"postgresql": 5432, "mssql": 1433}  # provider -> its default port
SSL_MODES = ("disable", "require", "verify-ca", "verify-full")
DEFAULT_SSL_MODE = "verify-full"  # the safe one: anything weaker is chosen on purpose
OPTION_KEYS = frozenset({"connect_timeout_s", "accept_insecure_tls", "ca_certificate_pem"})
MAX_CA_PEM_BYTES = 65536
MAX_CA_CERTIFICATES = 5
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _text(value, code, *, maximum, minimum=1, multiline=False):
    if not isinstance(value, str):
        raise ConnectorError(code)
    value = value.strip()
    bad = CONTROL.search(value.replace("\n", "") if multiline else value)
    if bad or not minimum <= len(value) <= maximum:
        raise ConnectorError(code)
    return value


def name(value):
    return _text(value, "invalid_name", maximum=100)


def description(value):
    return _text(value if value is not None else "", "invalid_description", maximum=2000, minimum=0, multiline=True)


def provider(value):
    if value not in PROVIDERS:
        raise ConnectorError("invalid_provider")
    return value


def port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConnectorError("invalid_port")
    return value


def database_name(value):
    return _text(value, "invalid_database", maximum=128)


def username(value):
    return _text(value, "invalid_username", maximum=128)


def password(value):
    """The credential as typed. Spaces belong to a password, so it is not stripped; only NUL and length are refused."""
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or "\x00" in value:
        raise ConnectorError("invalid_credential")
    return value


def ssl_mode(value):
    if value is None:
        return DEFAULT_SSL_MODE
    if value not in SSL_MODES:
        raise ConnectorError("invalid_ssl_mode")
    return value


def _ca_pem(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > MAX_CA_PEM_BYTES:
        raise ConnectorError("invalid_options")
    try:
        from cryptography import x509

        certificates = x509.load_pem_x509_certificates(value.encode("utf-8"))
    except ImportError:
        raise ConnectorError("driver_missing", 503) from None
    except Exception:  # noqa: BLE001 - what is wrong with the text is not for the caller
        raise ConnectorError("invalid_options") from None
    if not 1 <= len(certificates) <= MAX_CA_CERTIFICATES:
        raise ConnectorError("invalid_options")
    return value.strip() + "\n"


def options(value):
    """The closed set of options: connect_timeout_s (1..30), accept_insecure_tls (bool), ca_certificate_pem (PEM text of
    public certificates, as many as 5)."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - OPTION_KEYS:
        raise ConnectorError("invalid_options")
    clean = {}
    if "connect_timeout_s" in value:
        timeout = value["connect_timeout_s"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30:
            raise ConnectorError("invalid_options")
        clean["connect_timeout_s"] = timeout
    if "accept_insecure_tls" in value:
        if not isinstance(value["accept_insecure_tls"], bool):
            raise ConnectorError("invalid_options")
        if value["accept_insecure_tls"]:
            clean["accept_insecure_tls"] = True
    if value.get("ca_certificate_pem") not in (None, ""):
        clean["ca_certificate_pem"] = _ca_pem(value["ca_certificate_pem"])
    return clean


def connection_fields(raw):
    """All the fields of a connection (without the password), validated and normalised, from a dict that has
    every one of
    them (a new connection, or an existing one with the changes applied)."""
    chosen = provider(raw.get("provider"))
    return {
        "name": name(raw.get("name")),
        "description": description(raw.get("description")),
        "provider": chosen,
        "host": normalise_host(raw.get("host")),
        "port": port(raw.get("port", PROVIDERS[chosen])),
        "database_name": database_name(raw.get("database_name")),
        "username": username(raw.get("username")),
        "ssl_mode": ssl_mode(raw.get("ssl_mode")),
        "options": options(raw.get("options")),
    }


def tls_policy(fields, loopback):
    """Encryption is mandatory (with a checked certificate) unless the host is this computer or the owner has said, in
    the request itself, that they accept a weaker setting for a host elsewhere (SR-10)."""
    weak = fields["ssl_mode"] in ("disable", "require")
    if weak and not loopback and not fields["options"].get("accept_insecure_tls"):
        raise ConnectorError("tls_required")
    if fields["options"].get("ca_certificate_pem") and fields["ssl_mode"] in ("disable", "require"):
        raise ConnectorError("invalid_options")  # a CA only means something when the certificate is checked
    if fields["provider"] == "mssql":  # what the SQL Server driver can do (drivers/mssql.py)
        if fields["ssl_mode"] == "require":
            raise ConnectorError("invalid_ssl_mode")  # encrypt-without-checking does not exist there
        if fields["ssl_mode"] != "disable" and not fields["options"].get("ca_certificate_pem"):
            raise ConnectorError("ca_required")
