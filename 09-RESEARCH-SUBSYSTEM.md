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
