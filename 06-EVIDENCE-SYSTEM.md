# 06 — Evidence System

## 1. Core rule

Evidence is assessed as **Source × Claim**, never as one global score for an entire paper.

The same paper may be direct and strong evidence for one claim and indirect/weak evidence for another.

## 2. Evidence axes

Each claim-specific evidence item carries three 0–4 axes:

### Authority (A)
What type of source is this?

Suggested rubric:

- 4: target paper/SI, primary deposited data, authoritative official database/standard.
- 3: strong peer-reviewed scholarly source, independent same-material paper, detailed thesis.
- 2: preprint, incomplete scholarly source, vendor application note, limited secondary compilation.
- 1: informal technical source, lab page, forum, GitHub issue, blog.
- 0: unverifiable / no traceable source.

Authority does **not** imply reproducibility.

### Reliability (R)
How trustworthy is this specific claim support?

Reliability must be produced from a checklist and rule engine, not an LLM gut score.

Required checklist dimensions should include at least:

- original/raw data available?
- method sufficiently complete?
- independent replication performed?
- uncertainty/variation reported?
- independent external validation?
- data internally consistent?
- conclusion supported by data?
- material/sample identity controlled?
- known retraction/correction/methodological defect?

A versioned rule maps checklist answers to 0–4 Reliability.

### Directness (D)
How directly does the source address the current claim/problem?

Suggested rubric:

- 4: exact material/system/method/condition/result.
- 3: same material and near-identical condition; one noncritical difference.
- 2: analogous material or directly transferable method with justified similarity.
- 1: general methodological background.
- 0: not materially relevant.

## 3. Composite score

A composite score may be computed for **search ranking only**. It must not replace hard gates (architecture decision 19).

The versioned default rule (`ranking_rule_v1`, weights `(0.25, 0.45, 0.30)`) maps the three 0–4 axes to a 0–100 display number:

```text
ranking_score = (0.25*A + 0.45*R + 0.30*D) / 4 * 100
```

Example derivation (the axes of `examples/fdm-201/evidence.example.yaml`, A=4, R=2, D=4):

```text
(0.25*4 + 0.45*2 + 0.30*4) / 4 * 100 = 3.1 / 4 * 100 = 77.5
```

Weights are configurable and versioned. The reference implementation is
`core/rules/evidence.py` (`ranking_score`, `RANKING_RULE_VERSION`,
`RankingWeights`); `schemas/evidence.schema.yaml` allows `ranking_score` to be
a number or `null`, so a record may omit the composite when it is not
computed.

## 4. Hard gates

Examples:

### Acceptance-criterion changes
Typically require `R >= 3` and preferably at least two independent qualifying sources unless the source is an authoritative standard or the target paper itself defines the claimed parameter.

### Recovery hypothesis eligibility
v0.1 default:

- `R >= 3`
- `D >= 2`
- `scientifically_actionable = true`

Lower-quality evidence may be stored as hypothesis context but cannot independently trigger a formal Recovery modification.

## 5. Source priority is question-specific

Target paper/SI has highest directness for “what did the authors report?”. It may have only moderate reliability for “is this procedure independently reproducible?”. Independent studies may be more important for the second question.

## 6. Evidence record requirements

Each evidence record must store:

- evidence ID;
- source ID and bibliographic/provenance data;
- claim ID/text;
- exact location in source where possible;
- extracted finding;
- checklist answers;
- A/R/D scores;
- role (`protocol_definition`, `acceptance_support`, `recovery_hypothesis`, `background`, etc.);
- limitations;
- Goals/decisions using the evidence;
- assessment model/version and timestamp.

## 7. Search deduplication

Research must maintain source identity using DOI/identifier/hash and avoid treating multiple mirrors of the same paper as independent evidence.
