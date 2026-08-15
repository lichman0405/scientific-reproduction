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
* **IP-literal hosts** (IPv4/IPv6) are allowed unless the literal lies
  inside ``198.18.0.0/15``: an IP literal bypasses DNS, so a transparent
  proxy's fake-IP answer can never produce it for a legitimate scholarly
  host -- refusing the range for literals costs nothing legitimate and
  keeps the SSRF surface closed.

Hosts that are not valid IP literals are treated as domain names. The
policy is a documented guard for the fake-IP range, not a general SSRF
firewall: resolved-address attacks (DNS rebinding, a compromised public
host) are outside the guard's scope by design and are the adapter
layer's documented boundary.
"""

from __future__ import annotations

import ipaddress
import urllib.parse

from scientific_reproduction.adapters.research.base import (
    AdapterDataError,
    AdapterError,
)

__all__ = [
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


class AdapterNetworkPolicyError(AdapterError):
    """Raised when a fetch target is refused by the fake-IP DNS policy.

    A well-formed http(s) target whose host is an IP literal inside
    :data:`FAKE_IP_NETWORK` is a policy violation, not malformed data:
    it parses cleanly but is refused for safety (SSRF guard).
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
    """Validate one http(s) fetch target against the fake-IP DNS policy.

    Returns ``url`` unchanged when the target is permitted: domain-name
    hosts always are (fake-IP DNS tolerance; the guard never resolves),
    and IP literals outside :data:`FAKE_IP_NETWORK` are. Refuses IP
    literals inside the range -- they bypass DNS, so no fake-IP proxy
    ever produces them for a legitimate scholarly host (SSRF guard).

    Raises:
        TypeError: ``url`` is not a ``str``.
        AdapterDataError: the value is not an absolute http(s) URL
            (missing/unsupported scheme, empty, whitespace/control
            characters, userinfo, no host, invalid IPv6 bracket form).
        AdapterNetworkPolicyError: the host is an IP literal inside
            :data:`FAKE_IP_NETWORK`.
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
    if ipaddress.ip_address(hostname) in FAKE_IP_NETWORK:
        raise AdapterNetworkPolicyError(
            f"validate_fetch_url: refused IP literal {hostname!r} inside "
            "the fake-IP benchmarking range 198.18.0.0/15; IP-literal "
            "fetch targets must not address that range (domain-name "
            "hosts are allowed regardless of DNS)"
        )
    return url
