# RANKFLOW AI — Rights Verification Agent (Prompt 04)

The second real agent. It takes the candidates Research discovered
for a job and decides, with evidence, whether each one may enter
the analysis and production pipeline. Its one rule, enforced in
code: **no video enters the pipeline without sufficient evidence
that its intended use is permitted** — and an unverified source is
never approved merely to keep production moving.

The intended use it checks against: taking a video, editing it
(cutting, reframing to 1080×1920, adding narration/captions) and
republishing the result commercially as a YouTube Short. A licence
must permit commercial use, modification AND redistribution.

## Components

| File | Role |
|---|---|
| `app/agents/rights/licenses.py` | Phase 1 — licence catalogue: CC BY / BY-SA / BY-NC / BY-ND / BY-NC-SA / BY-NC-ND (v1.0–4.0), CC0, Public Domain Mark, and recognised non-permitting markers (standard YouTube licence, all rights reserved, test-only) |
| `app/agents/rights/evidence.py` | Phases 1–3 — pure `evaluate()`: licence → verdict, expiry, manual evidence |
| `app/agents/rights/gate.py` | Phase 4 — server-side enforcement: `is_approved`, `require_approved`, `approved_candidate_ids` |
| `app/agents/rights/attribution.py` | Phase 5 — attribution text generation from stored evidence |
| `app/agents/rights/service.py` | Phases 2, 5, 6 — `RightsService`: verification run, manual review, reads, attribution listing, persistence |
| `app/agents/rights/agent.py` | Phase 6 — orchestrator executor for agent `rights` |
| `app/api/rights.py` | `GET /jobs/{id}/rights`, `GET /jobs/{id}/rights/{candidate_id}`, `POST …/review`, `GET /jobs/{id}/attribution` |
| `scripts/run_rights.py` | Demo: verify one job's candidates and print verdicts |
| `scripts/migrate_004_add_rights_fields.py` | Adds the Phase 2 evidence columns (idempotent) |
| `tests/test_rights.py` | 49 tests |

## Verdicts

| Verdict | Meaning |
|---|---|
| `approved` | Evidence supports the intended use: a known licence permitting commercial use, modification and redistribution (CC BY, CC BY-SA, CC0), not expired — or a human reviewer asserting a permission grant with the evidence recorded |
| `rejected` | Evidence affirmatively forbids the use: NonCommercial or NoDerivatives licence, standard YouTube licence, all rights reserved, test-only marker |
| `needs_review` | No usable evidence: no licence published, an unrecognised licence string, or a Public Domain Mark (a mark, not a licence) |
| `expired` | The permission had an expiry date and it has passed — expiry beats every other signal |
| `not_checked` | No verification run yet (pre-Prompt-04 rows only) |

A public video URL is not proof of permission. Standard viewing
terms are never assumed to permit downloading, editing or
commercial republication. When in doubt the verdict is
`needs_review` (a human may supply evidence) or `rejected` (the
evidence forbids it) — never `approved`.

## Evidence (Phase 2)

Every verification appends a `rights_evidence` row (a re-run with
nothing changed creates no duplicate): source/candidate id,
licence name and URL, permission evidence, verification date and
method (`licence_catalogue` or `manual_review`), commercial-use /
modification / redistribution flags, attribution requirement and
text, geographic/time restrictions, expiry, reviewer status,
reviewer and review date, and the human-readable reasons. The
candidate status follows the latest verdict (`rights_approved` /
`rights_rejected` / `rights_pending`), and approved candidates
get exactly one `approved_footage` row.

## Second-chance evidence

When a candidate's provider published no licence, the service
re-checks the Internet Archive metadata API for a `licenseurl`
(`RIGHTS_FETCH_IA_METADATA`, default on). That is
provider-published metadata, fetched live — never invented. A
failed lookup leaves the candidate `needs_review`. Verified live
2026-11-20: `youtube-8q8wKM7mHyA` publishes CC BY 3.0 in its
metadata; `macsdca-Parkour_Fails` publishes none.

## Manual review

`POST /jobs/{job_id}/rights/{candidate_id}/review` supplies
evidence; the agent re-evaluates and decides. A reviewer can:

- supply a licence URL — it is still parsed by the catalogue;
- assert a permission grant (`permission_granted: true`) — but
  only with `permission_evidence` saying where the grant is
  recorded (a URL, message or document reference), and the
  assertion and reviewer are stored on the row.

A reviewer **cannot** force an approval: a restrictive licence
still rejects, a bare assertion without evidence is a 422, and a
past expiry still expires. After a manual approval, resume the
job (`POST /jobs/{id}/resume`) — the rights stage re-runs and
now finds the approved candidate.

## Enforcement (Phase 4)

`require_approved(session, job_id, candidate_ids, purpose=...)`
is the server-side gate downstream agents must call before
analysing or producing a candidate. It reads the LATEST evidence
row from the database and raises 422 listing every unapproved
candidate and its verdict. `needs_review`, `rejected`,
`expired` and "never checked" all fail closed. There is no UI
warning to bypass — the check is in the service layer, and
Prompts 05 (Video Intelligence) and 08 (Production) are to call
it before touching any candidate.

## Attribution (Phase 5)

Generated from stored evidence — title/creator from Research,
licence name/URL from the parsed licence — and listed by
`GET /jobs/{id}/attribution` for APPROVED candidates only, for
the Creative Director and Production agents to render when it is
required (CC BY / BY-SA) and optionally when it is only
recommended (CC0). Unknown creators are written as "Unknown
creator", never guessed.

## Orchestrator connection

`build_default_registry(engine)` / `register_real_agents`
registers the rights executor (`test_double=False`).
`/agents/rights/health` reports `ready`. A plain job run now
gets past RESEARCH and RIGHTS_VERIFICATION and returns 501 at
VIDEO_ANALYSIS — provided at least one candidate is approved.
If none is approved, the rights stage raises after its retries
and the job FAILS with the counts stored
("approved 0 of N: … rejected, … need review, … expired"),
which is the honest outcome: resume it after a manual review
supplies evidence.

## Failure handling

* No candidates for the job → clear error (run Research first).
* Zero approved → `ExternalServiceError` with counts and reasons;
  evidence is still persisted, nothing is approved.
* IA metadata lookup fails → candidate stays `needs_review`.
* Manual review without evidence for a permission grant → 422.

## Running it

```bash
cd ~/workspace/rankflow_ai
.venv/bin/python scripts/migrate_004_add_rights_fields.py  # pre-Prompt-04 DBs only
.venv/bin/python scripts/run_rights.py <job_id>
.venv/bin/python -m pytest tests/test_rights.py -v
```

## Tests (49) and live measurements

Licence catalogue (permissive / restrictive / unknown / PDM /
canonical URLs), evaluation (approval, rejection, missing licence,
expiry past/future, manual grant with/without evidence, manual
evidence cannot override NC), attribution, service (mixed
candidates, honest zero-approved failure, no-candidate error,
unknown job, idempotent re-run, live IA metadata lookup),
manual review (grant, NC stays rejected, past expiry, unknown
candidate), attribution listing, gate (approved only, expired
approval fails closed, never-checked fails closed), orchestrator
integration (real rights → 501 at video analysis; zero approved
→ job failed), API (health, list, review 404s).

Live measurement on the real parkour job
`df50d17e-f44b-4d57-829f-6b8aa77fd5b9` (2026-11-20,
`scripts/run_rights.py`, IA metadata fetch on):

| Outcome | Count | Detail |
|---|---|---|
| APPROVED | 1 | `youtube-8q8wKM7mHyA` — CC BY 3.0, attribution required |
| NEEDS_REVIEW | 9 | No licence published by the provider (metadata API confirms none for the items checked) |
| REJECTED | 0 | — |
| EXPIRED | 0 | — |

## Known limitations

* Most Internet Archive items publish no licence — those
  candidates cannot proceed without a human supplying evidence,
  which is by design, not a bug. A job whose candidates are all
  unlicensed will fail at rights until reviewed.
* The catalogue covers Creative Commons licences v1.0–4.0, CC0
  and the Public Domain Mark. Other licences (e.g. GPL, MIT,
  bespoke creator licences) are `needs_review`, not guessed.
* No YouTube licence check runs live: YouTube discovery is
  disabled without a key, and a YouTube candidate with no licence
  is `needs_review` with a note that the standard licence permits
  viewing only.
* Expiry is only known when evidence carries an expiry date
  (manual review); licences themselves do not expire.
