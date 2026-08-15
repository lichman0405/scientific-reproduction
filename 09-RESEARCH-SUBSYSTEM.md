# 09 — Research / Literature Subsystem

## 1. Role

Research is a project-persistent evidence service, not an ad-hoc browser.

## 2. Mandatory bootstrap research

Before Plan v1, Research must systematically acquire and index:

- primary paper;
- supplementary information;
- CIF/CCDC/structure files;
- linked data repository files;
- data-availability statements;
- key method references cited by the paper;
- relevant same-material papers;
- closely related materials/methods;
- same-author prior/subsequent methods;
- independent reproduction work if available;
- public database records;
- computational method sources.

## 3. Research Requests

Only Supervisor may issue formal Research Requests. Workers report anomalies to Supervisor; Supervisor decides whether a research question is warranted.

Research Request should include:

- request ID;
- originating Goal/decision;
- scientific question;
- required search families;
- minimum directness/reliability needed;
- time/scope constraints;
- expected output type.

## 4. Source adapters

v0.1 must support public/open sources and define optional adapters for commercial sources.

Public/open examples:

- DOI/publisher public pages;
- Crossref/OpenAlex-like metadata services;
- public repositories;
- public crystallographic/materials databases;
- public standards/manuals where legally accessible.

Optional adapters:

- CSD/CCDC subscription functions;
- SciFinder;
- Web of Science;
- Scopus;
- institutional search systems.

Missing paid access must degrade gracefully rather than block the whole project.

Fetch-target validation policy (fake-IP DNS tolerance):

When network fetch leaves fixture mode (content/file fetch, live metadata fetch), every http(s) fetch target must be validated before a connection is opened, using the adapter-layer guard (`adapters/research/network_policy.py`). Transparent proxies with fake-IP DNS (Clash-style tools) answer DNS queries for public hosts with addresses in the IANA-reserved benchmarking range `198.18.0.0/15` (RFC 2544 / RFC 5735), so the policy is:

- domain-name hosts are always allowed: DNS may legitimately answer with a fake IP inside `198.18.0.0/15`, and the guard never resolves DNS;
- IP-literal hosts are refused when the literal addresses any blocked network (`network_policy.BLOCKED_IP_LITERAL_NETWORKS`): the fake-IP benchmarking range, private-use and CGNAT space, loopback, link-local (cloud metadata), multicast/reserved, and the IPv6 unspecified/loopback/unique-local/link-local ranges. An IP literal bypasses DNS, so a fake-IP proxy never produces a blocked address for a legitimate scholarly host; refusing these ranges for literals keeps SSRF protection intact without breaking fake-IP environments. Literals in public address space are allowed.

The guard inspects host forms only; resolved-address attacks (DNS rebinding, compromised public hosts) are out of scope and remain the adapter layer's documented boundary: the connecting adapter must re-validate every resolved address against the blocked networks before opening a connection.

## 5. Evidence extraction

Research must not merely store PDFs. It must produce structured source and evidence records, including exact claims, locations, limitations and A/R/D assessments.

## 6. Search families for recovery

Suggested materials-chemistry default families:

1. exact target material/name/identifier;
2. target author/team;
3. exact node/linker/precursor chemistry;
4. analogous framework family;
5. exact failed operation (activation, crystallization, adsorption, etc.);
6. instrument/method standard literature;
7. cited references;
8. citing references;
9. public database records;
10. negative/failed reproduction reports where available.

## 7. Search saturation

Store each search cycle and the number of new eligible Recovery hypotheses. Default closure support requires two consecutive zero-novelty cycles after all required search families have been covered.

## 8. Author contact

`author_contact_policy = disabled_by_default`.

When a critical ambiguity remains after public sources are exhausted, Supervisor may open an `EXTERNAL_CONTACT_GATE`. No autonomous external email/contact is permitted.
