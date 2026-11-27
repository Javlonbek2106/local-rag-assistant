"""Where the connector may connect to (SSRF and connection-string injection; roadmap SR-9, SR-10).

A host is text a person typed, and the connector will open a network connection to it from the machine that holds the
documents. The decision recorded in docs/connector.md:

- BLOCKED always: addresses that are never a database a person means to read, and that are the classic SSRF targets:
  the unspecified address (0.0.0.0, ::), link-local (169.254.0.0/16, which holds the cloud metadata services, and
  fe80::/10), multicast, reserved and broadcast. IPv4-mapped IPv6 is judged by the IPv4 address inside it. A name is
  resolved and EVERY address it resolves to is judged (a name with one blocked address is blocked).
- BLOCKED always: this application's own database (same address, same port and the same database name, or one of its
  own roles; any two addresses of this computer, loopback or its network interfaces, count as the same): the connector
  must never be pointed at the source of truth (SR-9). Not seen through: another computer's forwarder or a published
  port with another number.
- ALLOWED by default: loopback and private ranges (10/8, 172.16/12, 192.168/16, fc00::/7). The people who use this
  application keep their other projects' databases on this computer or on the office network; a blanket "no private
  addresses" rule would make the connector useless for them. This is a deliberate, documented decision, not an
  oversight: the connector is a local, single-owner tool behind a one-time pairing code, and the protection against it
  being turned on the internal network is the owner's own authority plus the optional allow-list below.
- OPTIONAL allow-list: RAG_CONNECTOR_ALLOWED_HOSTS (names or "*.suffix"): when set, nothing else may be connected to.
- The address that was judged is the address that is connected to (the driver gets the pinned IP and keeps the name
  only for the certificate check), so a name that resolves differently a moment later (DNS rebinding) cannot change
  the target.

Host syntax is strict (letters, digits, dot, hyphen, colon for an IPv6 literal): nothing that could be read as another
connection setting, a user, a path or a second host.
"""

import concurrent.futures
import fnmatch
import ipaddress
import re
import socket
from typing import NamedTuple

from rag import config
from rag.connector.errors import ConnectorError

LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")
IPV6_TEXT = re.compile(r"[0-9a-f:.]{2,45}")
RESOLVE_TIMEOUT_S = 5.0
INTERNAL_ROLES = frozenset({"rag", "rag_app", "rag_migrator", "rag_bootstrap"})


class Target(NamedTuple):
    host: str  # as typed (normalised): used for the certificate check
    port: int
    addresses: tuple  # the judged addresses, as text; the first is connected to
    loopback: bool  # every address is loopback


def normalise_host(host):
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        raise ConnectorError("invalid_host")
    if ":" in host:  # an IPv6 literal
        if not IPV6_TEXT.fullmatch(host):
            raise ConnectorError("invalid_host")
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ConnectorError("invalid_host") from None
        return host
    if len(host) > 253 or not all(LABEL.fullmatch(label) for label in host.split(".")):
        raise ConnectorError("invalid_host")
    return host


def _judge(address):
    """The address as ipaddress object (IPv4-mapped IPv6 unwrapped), or ConnectorError("host_blocked")."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback:  # (::1 sits in a range Python calls "reserved")
        return address
    if address.is_unspecified or address.is_multicast or address.is_link_local or address.is_reserved:
        raise ConnectorError("host_blocked")
    return address


def _resolve(host, port, resolver):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        found = pool.submit(resolver, host, port, 0, socket.SOCK_STREAM).result(timeout=RESOLVE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - not found, no answer, odd answer: one fixed word
        raise ConnectorError("host_unresolvable") from None
    finally:
        pool.shutdown(wait=False)  # a resolver that hangs is left behind; the request does not wait for it
    addresses = []
    for family, _kind, _proto, _name, sockaddr in found:
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    if not addresses:
        raise ConnectorError("host_unresolvable")
    return addresses


def _allowed_by_list(host):
    patterns = config.CONNECTOR_ALLOWED_HOSTS
    return not patterns or any(fnmatch.fnmatchcase(host, pattern) for pattern in patterns)


def internal_database():
    """(host, port, database name) of this application's own PostgreSQL, whichever mode it runs in."""
    if config.DB_HARDENED:
        return config.DB_HOST, config.DB_PORT, config.DB_NAME
    from urllib.parse import urlsplit

    parts = urlsplit(config.DATABASE_URL)
    return parts.hostname or "", parts.port or 5432, parts.path.lstrip("/")


def _on_this_machine(address):
    """Is this an address of this computer (loopback or one of its network interfaces)? Only its own can be bound."""
    if address.is_loopback:
        return True
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((str(address), 0))
    except OSError:
        return False
    return True


def _same_machine(a, b):
    return a == b or (_on_this_machine(a) and _on_this_machine(b))


def check_target(host, port, database, username, *, resolver=socket.getaddrinfo, internal=internal_database):
    """Judge a host and port. Returns Target; raises ConnectorError (invalid_host, host_not_allowed, host_unresolvable,
    host_blocked, internal_database)."""
    host = normalise_host(host)
    if not _allowed_by_list(host):
        raise ConnectorError("host_not_allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    # IPv4 first
    texts = [host] if literal is not None else sorted(_resolve(host, port, resolver), key=lambda a: ":" in a)
    judged = []
    for text in texts:
        try:
            judged.append(_judge(ipaddress.ip_address(text)))
        except ValueError:
            raise ConnectorError("host_blocked") from None
    own_host, own_port, own_db = internal()
    if int(port) == int(own_port):
        try:
            own_addresses = [
                _judge(ipaddress.ip_address(a))
                for a in ([own_host] if _is_ip(own_host) else _resolve(own_host, own_port, resolver))
            ]
        except ConnectorError:
            own_addresses = []
        same_server = any(_same_machine(a, o) for a in judged for o in own_addresses)
        if same_server and (str(database).lower() == str(own_db).lower() or str(username).lower() in INTERNAL_ROLES):
            raise ConnectorError("internal_database")
    return Target(host, int(port), tuple(str(a) for a in judged), all(a.is_loopback for a in judged))


def _is_ip(text):
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False
