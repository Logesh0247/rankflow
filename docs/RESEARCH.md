# RANKFLOW AI — Research Agent (Prompt 03)

The first real agent. It takes a topic, understands it, discovers
candidate videos from permitted providers, scores and deduplicates
them, persists them linked to the job, and returns their ids to the
orchestrator as a validated `ResearchOutput`.

## Components

| File | Role |
|---|---|
| `app/agents/research/topic.py` | Phase 1 — topic understanding: classification, keyword extraction, synonym/plural expansion, related concepts, event + content-type identification, search strategy |
| `app/agents/research/providers.py` | Phase 2 — discovery providers (see table below) |
| `app/agents/research/scoring.py` | Phase 4 — relevance / completeness / reliability scoring, duplicate detection |
| `app/agents/research/service.py` | Phases 5–6 — `ResearchService`: discovery loop, duration enrichment, idempotent persistence, `ResearchReport` |
| `app/agents/research/agent.py` | Phase 6 — orchestrator executor for agent `research` |
| `app/api/research.py` | `GET /jobs/{job_id}/candidates` |
| `scripts/run_research.py` | Demo: one topic -> plan + candidates + metrics |
| `scripts/migrate_003_add_candidate_metadata.py` | Adds `candidate_videos.creator/description/licence` (idempotent) |
| `tests/test_research.py` | 19 tests |

## Providers (verified 2026-11-20, this VM)

| Provider | Status | Notes |
|---|---|---|
| `internet_archive` (default) | **Real, no key** | archive.org AdvancedSearch (`mediatype:movies`, title/description-fielded) + metadata API for durations. Live: 1,869 parkour items; top hit for "parkour fails" is `macsdca-Parkour_Fails`. |
| `youtube` | Implemented, **not verified, disabled without a key** | YouTube Data API v3. No `YOUTUBE_API_KEY` is configured, so `search()` raises `ProviderUnavailable` — results are never fabricated. Falls back to Archive when a key is set but YouTube fails. |
| `test` | **TEST-ONLY** | Deterministic in-memory candidates, `test-only` licence, `example.invalid` URLs. Only when `RESEARCH_PROVIDER=test`. Never the default. |

Evaluated and NOT used: Openverse (images + audio only, no video —
verified live), Wikimedia Commons (mostly stills; inconsistent
duration/licence metadata), Pexels / Pixabay (require keys not
configured).

A search result is **discovery only**. It is never presented as
downloadable or reusable — that is the Rights Verification Agent's
job (Prompt 04). Most Archive items publish no licence URL; those
candidates are kept with `licence=None` and flagged in the report.

## Topic understanding

Deterministic and rule-based (no Muse AI key/endpoint is configured,
so no LLM call): a 13-category taxonomy classifies the topic,
stopwords / rank words / numbers are stripped to keywords, synonyms
and singular/plural variants expand the query, and a strategy is
generated: exact keywords -> expanded synonyms -> broad category
fallback. Example — "TOP 5 CRAZIEST PARKOUR FAILS": category
`sports_action`, keywords `[parkour, fails]`, expanded includes
`free running, freerunning, traceur, falls, bloopers…`, content type
`ranking_compilation`, event `fails`.

## Scoring (kept separate, on purpose)

* **Relevance** — keyword overlap with title (0.65) / description
  (0.30) / creator (0.05), +0.10 exact-phrase bonus in the title.
  Stored as `relevance_score`. Search relevance only.
* **Completeness** — fraction of title, creator, description, URL,
  duration, publication date, licence present. Missing metadata is
  marked, not dropped.
* **Reliability** — provider base (Archive 0.8, YouTube 0.7, test 0.3)
  + capped log-scaled popularity bonus. Not content quality, not
  reuse permission.
* **Combined** (prioritisation only) = 0.6·relevance + 0.2·completeness
  + 0.2·reliability.

Duplicates are removed by (platform, external_id), then normalised
URL, then normalised title + creator (+ duration within 10 s).

## Persistence

`Source` is upserted by (platform, external_id); `CandidateVideo`
is upserted by (job, source, external_video_id), so re-running the
agent for the same job never duplicates rows. New in Prompt 03:
`candidate_videos.creator / description / licence` (migration 003;
run it once on pre-Prompt-03 databases — `create_all()` never adds
columns).

## Orchestrator connection

`build_default_registry(engine)` / `register_real_agents(registry,
engine)` registers the research executor (`test_double=False`).
`/agents` and `/agents/research/health` now report `ready`; the
other six agents stay `not_implemented`. A plain job run gets past
RESEARCH and returns **501** at RIGHTS_VERIFICATION (Prompt 04),
leaving the job `running` at stage RESEARCH with a checkpoint —
it is not marked failed.

## Failure handling

* Provider unavailable → retried inside the provider (3 attempts,
  exponential backoff), then the next authorised provider is tried.
* No relevant results → next query in the strategy is tried.
* Duplicates → removed, counted in `metrics.duplicate_rate`.
* Missing metadata → candidate kept, completeness lowered.
* No eligible candidates at all → `ExternalServiceError` with a
  clear limitation (queries tried + provider outcomes). The
  orchestrator retries the stage, then fails the job with that
  message stored. An empty `ResearchOutput` is never returned
  (its schema requires ≥1 candidate id).

## Environment quirk

Outbound traffic in this VM goes through an egress proxy that MITMs
TLS with a self-signed certificate, and httpx's `trust_env` parsing
chokes on the IPv6 entries in `NO_PROXY`. The provider HTTP client
therefore uses `trust_env=False`, takes the proxy explicitly from
`HTTPS_PROXY`, and verifies TLS against the bundle in `SSL_CERT_FILE`
(the egress CA bundle) or the system bundle — certificates are still
verified, never disabled.

## Running it

```bash
cd ~/workspace/rankflow_ai
.venv/bin/python scripts/migrate_003_add_candidate_metadata.py  # pre-Prompt-03 DBs only
.venv/bin/python scripts/run_research.py "TOP 5 CRAZIEST PARKOUR FAILS"
.venv/bin/python scripts/run_research.py "funny cats" --provider test
.venv/bin/python -m pytest tests/test_research.py -v
```

## Tests (19) and live measurements

Topic understanding (3 topics + empty topic), relevance scoring,
dedup (same id / same URL / re-upload), test-provider
determinism + labelling, YouTube-without-key raises
`ProviderUnavailable`, live Archive search returns real records,
persistence + idempotency, unknown job, no-candidates limitation,
orchestrator integration (real research, then 501 at rights),
registry without an engine, API (`/agents` ready, candidates empty
before a run).

Live measurement against the real Internet Archive provider
(2026-11-20, `research_max_candidates=5`, durations enriched):

| Topic | Raw | Kept | Mean relevance | Mean completeness | Mean reliability | Duplicate rate | Traceable |
|---|---|---|---|---|---|---|---|
| TOP 5 CRAZIEST PARKOUR FAILS | 13 | 5 | 0.84 | 0.86 | 0.85 | 0.0 | 5/5 |
| TOP 5 FUNNIEST CAT VIDEOS | 25 | 5 | 0.52 | 0.86 | 0.90 | 0.0 | 5/5 |
| HOW ROCKETS LAND IN SPACE | 25 | 5 | 0.52 | 0.91 | 0.88 | 0.0 | 5/5 |

Every kept candidate traces to its source (URL contains its
identifier). The standing limitation on all three runs: some
candidates publish no licence metadata — reuse permission is NOT
established by this agent.

## Known limitations

* Internet Archive is a public archive, not a YouTube replacement:
  relevance is lower for broad topics (cats / rockets ≈ 0.52) than
  for exact ones (parkour fails 0.84), and items are often long-form
  (hours), not Shorts-length — moments are extracted later, by
  Prompt 05.
* No YouTube discovery until a `YOUTUBE_API_KEY` is configured and
  verified (the provider code exists; it has never run live).
* Duration enrichment fetches the metadata API for up to 5 kept
  candidates; if it fails, duration stays unknown.
* No LLM topic understanding until a Muse AI endpoint/key exists.
