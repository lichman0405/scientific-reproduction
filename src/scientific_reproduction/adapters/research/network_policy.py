"""Fake-IP DNS fetch policy for network-capable research adapters.

When real fetch adapters land (content/file fetch, live metadata fetch),
every http(s) fetch target must be validated before a connection is
opened. The guard lives here so the policy is pure, deterministic and
offline-testable (AC-03): it inspects the *host form* of the URL only and
never resolves DNS.

Policy (normative, 09-RESEARCH-SUBSYSTEM.md section 4)
-------------------------------------------------------
Transparent proxies with fake-IP DNS (Clash-style tools) answer DNS
queries for public hosts with addresses in the IANA-reserved
benchmarking range ``198.18.0.0/15`` (RFC 2544 / RFC 5735). A guard that
blocks that range outright would break every legitimate fetch in such an
environment (domain names legitimately resolve inside it); allowing it
unconditionally -- including IP-literal URLs -- would weaken SSRF
protection. The resolution:

* **domain-name hosts** are always allowed: DNS may legitimately answer
  with a fake IP inside ``198.18.0.0/15``, and the guard never resolves
  (AC-03);
* **IP-literal hosts** are refused when the literal addresses any of
  :data:`BLOCKED_IP_LITERAL_NETWORKS`: the fake-IP benchmarking range,
  private-use and carrier-grade-NAT space, loopback, link-local (cloud
  metadata), multicast/reserved, and the IPv6 unspecified/loopback/
  unique-local/link-local ranges. An IP literal bypasses DNS, so no
  transparent proxy's fake-IP answer ever produces a blocked address for
  a legitimate scholarly host -- refusing these ranges for literals
  costs nothing legitimate and keeps the SSRF surface closed. All other
  IP literals (public address space) are allowed.

Hosts that are not valid IP literals are treated as domain names. The
guard inspects host forms only: resolved-address attacks (DNS rebinding,
a compromised public host) are outside its scope by design and are the
connecting adapter's documented boundary -- the adapter must re-validate
every resolved address against the blocked networks before opening a
connection.
"""

from __future__ import annotations

import ipaddress
import urllib.parse

from scientific_reproduction.adapters.research.base import (
    AdapterDataError,
    AdapterError,
)

__all__ = [
    "BLOCKED_IP_LITERAL_NETWORKS",
    "FAKE_IP_NETWORK",
    "AdapterNetworkPolicyError",
    "host_is_ip_literal",
    "validate_fetch_url",
]

#: IANA-reserved benchmarking range assigned by fake-IP DNS
#: (RFC 2544 / RFC 5735: 198.18.0.0/15). Clash-style transparent proxies
#: answer public-host DNS queries with addresses inside this range, so
#: domain-name hosts legitimately resolve here; IP literals in this range
#: are refused by :func:`validate_fetch_url` (SSRF guard).
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")

#: IP-literal networks an http(s) fetch target may never address
#: (:func:`validate_fetch_url` SSRF guard). Every range here is either
#: non-global (private-use, CGNAT, loopback, link-local/cloud metadata,
#: IPv6 unique-local/link-local), not a valid connection target
#: (unspecified, multicast, reserved), or the fake-IP benchmarking range
#: (an IP literal bypasses DNS, so no fake-IP proxy ever produces it for
#: a legitimate scholarly host). Literals in public address space remain
#: allowed; domain-name hosts are always allowed regardless of where DNS
#: resolves them (the connecting adapter re-validates resolved addresses).
BLOCKED_IP_LITERAL_NETWORKS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    # this-address / source address (RFC 6890)
    ipaddress.ip_network("0.0.0.0/8"),
    # private-use (RFC 1918) and shared CGNAT space (RFC 6598)
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    # loopback (RFC 1122) and link-local / cloud metadata (RFC 3927)
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    # the fake-IP benchmarking range (RFC 2544 / RFC 5735)
    ipaddress.ip_network("198.18.0.0/15"),
    # multicast (RFC 5771) and reserved (RFC 1112)
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    # IPv6: unspecified, loopback, unique-local, link-local
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


class AdapterNetworkPolicyError(AdapterError):
    """Raised when a fetch target is refused by the IP-literal policy.

    A well-formed http(s) target whose host is an IP literal inside one
    of :data:`BLOCKED_IP_LITERAL_NETWORKS` is a policy violation, not
    malformed data: it parses cleanly but is refused for safety (SSRF
    guard).
    """


def host_is_ip_literal(host: str) -> bool:
    """True iff ``host`` is an IPv4 or IPv6 literal.

    ``host`` must be the hostname portion of a URL (brackets and port
    stripped, as ``urllib.parse`` reports them). Anything
    ``ipaddress`` does not accept as an address -- including exotic
    numeric spellings -- counts as a domain name, so the fake-IP
    tolerance for domains wins over non-canonical IP forms.

    Raises:
        TypeError: ``host`` is not a ``str``.
    """
    if not isinstance(host, str):
        raise TypeError(
            f"host_is_ip_literal expects a str, got {type(host).__name__}"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def validate_fetch_url(url: str) -> str:
    """Validate one http(s) fetch target against the IP-literal policy.

    Returns ``url`` unchanged when the target is permitted: domain-name
    hosts always are (fake-IP DNS tolerance; the guard never resolves),
    and IP literals in public address space are. Refuses IP literals
    that address any of :data:`BLOCKED_IP_LITERAL_NETWORKS` (the fake-IP
    benchmarking range, private/CGNAT space, loopback, link-local/cloud
    metadata, multicast/reserved, IPv6 unspecified/loopback/unique-local/
    link-local) -- a literal bypasses DNS, so no fake-IP proxy ever
    produces a blocked address for a legitimate scholarly host (SSRF
    guard).

    Raises:
        TypeError: ``url`` is not a ``str``.
        AdapterDataError: the value is not an absolute http(s) URL
            (missing/unsupported scheme, empty, whitespace/control
            characters, userinfo, no host, invalid IPv6 bracket form).
        AdapterNetworkPolicyError: the host is an IP literal inside one
            of :data:`BLOCKED_IP_LITERAL_NETWORKS`.
    """
    if not isinstance(url, str):
        raise TypeError(
            f"validate_fetch_url expects a str, got {type(url).__name__}"
        )
    value = url.strip()
    if not value:
        raise AdapterDataError("validate_fetch_url: empty URL")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise AdapterDataError(
            "validate_fetch_url: URL must not contain whitespace or "
            "control characters"
        )
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        # Python >= 3.11 validates bracketed netlocs at parse time
        # (unbalanced brackets, bracketed IPv4, invalid IPvFuture).
        raise AdapterDataError(
            f"validate_fetch_url: invalid URL form {value!r}"
        ) from None
    if parts.scheme == "":
        raise AdapterDataError(
            "validate_fetch_url: expected an absolute http(s) URL "
            "(missing scheme)"
        )
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise AdapterDataError(
            f"validate_fetch_url: unsupported scheme {parts.scheme!r} "
            "(expected http or https)"
        )
    if "@" in parts.netloc:
        raise AdapterDataError(
            "validate_fetch_url: URL with userinfo is not a valid fetch "
            "target"
        )
    try:
        hostname = (parts.hostname or "").lower()
    except ValueError:
        raise AdapterDataError(
            f"validate_fetch_url: invalid IPv6 bracket form in URL {value!r}"
        ) from None
    if not hostname:
        raise AdapterDataError("validate_fetch_url: URL has no host")
    if not host_is_ip_literal(hostname):
        return url
    blocked = _blocked_network_for(hostname)
    if blocked is not None:
        raise AdapterNetworkPolicyError(
            f"validate_fetch_url: refused IP literal {hostname!r} inside "
            f"blocked network {blocked}; IP-literal fetch targets must "
            "not address internal/loopback/metadata or the fake-IP "
            "benchmarking range 198.18.0.0/15 (domain-name hosts are "
            "allowed regardless of DNS)"
        )
    return url


def _blocked_network_for(
    hostname: str,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """The first blocked network containing ``hostname``, or ``None``.

    An IPv4-mapped IPv6 literal (``::ffff:127.0.0.1``) addresses the
    mapped IPv4, so it is checked against the IPv4 networks too -- the
    literal cannot smuggle a blocked address through the IPv6 form.
    """
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    for network in BLOCKED_IP_LITERAL_NETWORKS:
        if address in network:
            return network
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        for network in BLOCKED_IP_LITERAL_NETWORKS:
            if (
                isinstance(network, ipaddress.IPv4Network)
                and address.ipv4_mapped in network
            ):
                return network
    return None
