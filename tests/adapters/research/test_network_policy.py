"""Tests for the fake-IP DNS fetch policy of future network adapters.

Acceptance coverage (09-RESEARCH-SUBSYSTEM.md section 4, fetch-target
validation policy; ``adapters/research/network_policy.py``):
  * domain-name hosts are always allowed -- transparent-proxy fake-IP
    DNS (Clash-style tools) legitimately resolves public hosts into
    198.18.0.0/15 (RFC 2544 / RFC 5735), and the guard never resolves
    DNS (AC-03 offline determinism: every socket/DNS entry point is
    patched to raise, and the guard still validates);
  * IP-literal hosts inside 198.18.0.0/15 are refused (SSRF guard: an
    IP literal bypasses DNS, so a fake-IP proxy never produces one for
    a legitimate scholarly host);
  * IP-literal hosts outside the range -- including loopback and IPv6,
    locking the documented scope: the policy is a guard for the fake-IP
    range, not a general SSRF firewall -- are allowed;
  * error discipline follows the frozen adapter paradigm: ``TypeError``
    at public boundaries, ``AdapterDataError`` for malformed URLs,
    ``AdapterNetworkPolicyError`` (an ``AdapterError`` subclass) for
    the policy violation, all with stable messages.

No test in this file touches the network: every DNS/socket entry point
is patched to raise in the offline tests.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from scientific_reproduction.adapters.research import (
    FAKE_IP_NETWORK,
    AdapterDataError,
    AdapterNetworkPolicyError,
    validate_fetch_url,
)
from scientific_reproduction.adapters.research.network_policy import (
    host_is_ip_literal,
)

#: One domain host per shipped public-source family
#: (09-RESEARCH-SUBSYSTEM.md section 4): DOI/publisher pages,
#: metadata services, public repositories, public databases.
DOMAIN_HOST_URLS: tuple[str, ...] = (
    "https://doi.org/10.1039/D5TA00771B",
    "https://api.crossref.org/works/10.1/x",
    "https://arxiv.org/abs/2406.12345",
    "https://www.crystallography.net:443/cod/2110001.html?q=1#sec",
)

#: IP literals inside 198.18.0.0/15 -- refused regardless of scheme,
#: port, path or query (they bypass DNS, so no fake-IP proxy produces
#: them for a legitimate host).
FAKE_IP_LITERAL_URLS: tuple[str, ...] = (
    "http://198.18.0.1/",
    "http://198.18.0.0/",  # network address: still inside the range
    "http://198.19.255.255/",  # last address of the range
    "https://198.18.1.1:8443/path?q=1",
)

#: IP literals outside 198.18.0.0/15 -- allowed; loopback and IPv6 lock
#: the documented scope (the policy is the fake-IP range, not a general
#: SSRF firewall).
NON_FAKE_IP_LITERAL_URLS: tuple[str, ...] = (
    "http://198.17.255.255/",  # just below the range
    "http://198.20.0.0/",  # just above the range
    "https://1.1.1.1/",
    "http://127.0.0.1:8000/",
    "http://[2001:db8::1]/",
    "http://[::1]/",
)


def _refuse_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any DNS/socket attempt raise inside the test."""

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted in offline test path")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


# ---------------------------------------------------------------------------
# The documented constant
# ---------------------------------------------------------------------------


def test_fake_ip_network_is_the_documented_benchmarking_range() -> None:
    """FAKE_IP_NETWORK is exactly 198.18.0.0/15 (RFC 2544 / RFC 5735)."""
    assert FAKE_IP_NETWORK == ipaddress.ip_network("198.18.0.0/15")
    assert str(FAKE_IP_NETWORK) == "198.18.0.0/15"


# ---------------------------------------------------------------------------
# Domain-name hosts: always allowed, never resolved (fake-IP tolerance)
# ---------------------------------------------------------------------------


def test_domain_hosts_are_allowed_without_resolving_dns(monkeypatch) -> None:
    """Domain-name hosts pass the guard even though DNS is refused: the
    guard inspects the host form only and never resolves, so a fake-IP
    answer inside 198.18.0.0/15 can never break a legitimate fetch
    (AC-03 offline determinism)."""
    _refuse_network(monkeypatch)
    for url in DOMAIN_HOST_URLS:
        assert validate_fetch_url(url) == url


def test_domain_hosts_that_look_numeric_are_treated_as_domains(
    monkeypatch,
) -> None:
    """Hosts that are not valid IP literals count as domain names, so
    the fake-IP tolerance wins over exotic numeric spellings."""
    _refuse_network(monkeypatch)
    for url in (
        "http://198.18.0.1.example.com/",  # subdomain: a name, not a literal
        "http://999.1.1.1/",  # invalid IPv4: treated as a name
    ):
        assert validate_fetch_url(url) == url


# ---------------------------------------------------------------------------
# IP-literal hosts inside the range: refused (SSRF guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", FAKE_IP_LITERAL_URLS)
def test_ip_literals_inside_fake_ip_range_are_refused(monkeypatch, url: str) -> None:
    """An IP literal inside 198.18.0.0/15 is a policy violation: it
    bypasses DNS, so no fake-IP proxy produces it for a legitimate
    scholarly host (SSRF guard)."""
    _refuse_network(monkeypatch)
    with pytest.raises(AdapterNetworkPolicyError, match="fake-IP benchmarking range"):
        validate_fetch_url(url)


def test_policy_violation_is_an_adapter_error_with_stable_message() -> None:
    """The refusal is an AdapterError subclass with a stable message
    naming the offending literal and the policy."""
    assert issubclass(AdapterNetworkPolicyError, ValueError)
    with pytest.raises(AdapterNetworkPolicyError) as excinfo:
        validate_fetch_url("http://198.18.0.1/")
    message = str(excinfo.value)
    assert "198.18.0.1" in message
    assert "198.18.0.0/15" in message
    assert "domain-name hosts are allowed" in message


# ---------------------------------------------------------------------------
# IP-literal hosts outside the range: allowed (documented scope)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", NON_FAKE_IP_LITERAL_URLS)
def test_ip_literals_outside_fake_ip_range_are_allowed(
    monkeypatch, url: str
) -> None:
    """Literals outside 198.18.0.0/15 -- loopback and IPv6 included --
    pass: the policy is a guard for the fake-IP range, not a general
    SSRF firewall (09-RESEARCH-SUBSYSTEM.md section 4)."""
    _refuse_network(monkeypatch)
    assert validate_fetch_url(url) == url


# ---------------------------------------------------------------------------
# Error discipline (frozen adapter paradigm)
# ---------------------------------------------------------------------------


def test_validate_fetch_url_rejects_wrong_types() -> None:
    """Non-str URLs raise TypeError at the public boundary."""
    with pytest.raises(TypeError, match="validate_fetch_url expects a str"):
        validate_fetch_url(19818001)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "url",
    (
        "",
        "   ",
        "doi.org/10.1/x",  # missing scheme
        "ftp://198.18.0.1/file.cif",  # non-http(s) scheme
        "file:///etc/passwd",
        "data:text/plain;base64,AAAA",
        "http://user:pass@198.18.0.1/",  # userinfo
        "http:///path",  # no host
        "http://[::1",  # unbalanced IPv6 bracket form
        "http://[198.18.0.5]/",  # bracketed IPv4: a malformed form, never
        # a policy verdict (Python's URL parser rejects it outright)
    ),
)
def test_validate_fetch_url_rejects_malformed_urls(monkeypatch, url: str) -> None:
    """Malformed fetch targets raise AdapterDataError with a stable
    message (never a policy verdict on an unparseable URL)."""
    _refuse_network(monkeypatch)
    with pytest.raises(AdapterDataError, match="validate_fetch_url:"):
        validate_fetch_url(url)


def test_host_is_ip_literal_distinguishes_literals_from_names() -> None:
    """The literal test accepts IPv4/IPv6 literals and rejects every
    name spelling (including trailing-dot and invalid-octet forms)."""
    for literal in ("198.18.0.1", "1.1.1.1", "2001:db8::1", "::1"):
        assert host_is_ip_literal(literal) is True
    for name in (
        "doi.org",
        "example.com",
        "example.com.",
        "198.18.0.1.example.com",
        "999.1.1.1",
    ):
        assert host_is_ip_literal(name) is False
    with pytest.raises(TypeError, match="host_is_ip_literal expects a str"):
        host_is_ip_literal(123)  # type: ignore[arg-type]
