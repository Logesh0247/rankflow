# Momentrix AI — Research Agent (Prompt 03)

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
| `tests/test_youtube.py` | YouTube CC filter, licence mapping, multi-provider, yt-dlp download (24 tests) |
| `tests/test_duration.py` | Duration preference: boost, cap, unknown kept, enrichment backfill (13 tests) |
| `tests/test_stock_providers.py` | Pexels / Pixabay search, licences, file selection, direct download (24 tests) |

## Providers (verified 2026-11-20, this VM; YouTube wired after Prompt 12)

| Provider | Status | Notes |
|---|---|---|
| `internet_archive` (default) | **Real, no key** | archive.org AdvancedSearch (`mediatype:movies`, title/description-fielded) + metadata API for durations. Live: 1,869 parkour items; top hit for "parkour fails" is `macsdca-Parkour_Fails`. |
| `youtube` | **Creative Commons only, needs a key** | YouTube Data API v3. Search is filtered to `videoLicense=creativeCommon` and each video's `status.license` is fetched: YouTube's only CC option is CC BY 3.0, so a `creativeCommon` video is recorded with the CC BY 3.0 licence URL and Rights can approve it; a standard-licence video is recorded as such and Rights rejects it. Needs `YOUTUBE_API_KEY`; without it, `search()` raises `ProviderUnavailable` — results are never fabricated. Free quota is 10,000 units/day (one search = 100 units, ≈ 100 searches/day). **Unverified live on this VM**: the egress proxy blocks googleapis.com and youtube.com, so live verification must happen on the deploy host. |
| `pexels` | **Needs a free key (no card)** | Pexels video search (`api.pexels.com/videos/search`, bare `Authorization: <key>` header, 200 requests/hour). Free key at pexels.com/api. Every video is released under the Pexels License (free commercial use, modification, redistribution, no attribution required) — the provider records the platform licence URL and Rights approves it. Short clips with durations. Pexels publishes no title: the title is derived from the landing-page URL slug, or "Pexels video {id}". **Unverified live** (no key configured). |
| `pixabay` | **Needs a free key (no card)** | Pixabay video search (`pixabay.com/api/videos/`, `key` query param, 100 requests/60 s). Free key at pixabay.com/api/docs. Every video is released under the Pixabay Content License (same permissive terms as Pexels) — the provider records the platform licence URL and Rights approves it. Short clips with durations; `safesearch=true` is sent. Pixabay publishes no title: the title is the provider-published tags. **Unverified live** (no key configured). |
| `test` | **TEST-ONLY** | Deterministic in-memory candidates, `test-only` licence, `example.invalid` URLs. Only when `RESEARCH_PROVIDER=test` or `test` is in `RESEARCH_PROVIDERS`. Never the default. |

Set `RESEARCH_PROVIDERS` (comma list, in search order, e.g.
`youtube,internet_archive` or `pexels,pixabay,youtube,internet_archive`)
to search several providers together; results are merged, scored
and deduplicated exactly as for one provider (same title/creator/
duration re-uploads collapse across platforms). When it is empty,
the legacy single `RESEARCH_PROVIDER` is used with the Archive as
fallback. A listed provider with no key fails per query and the
run falls back to the next provider, with the failure noted in the
report.

TikTok, Facebook, Instagram and Pinterest are **out of scope**:
none of them offers an official API to search and download
arbitrary videos, so they can never be discovery providers here.
Pinterest is discovery-only in the loosest sense and its clips
are almost never rights-clean — it is not a footage source.

Evaluated and NOT used: Openverse (images + audio only, no video —
verified live), Wikimedia Commons (mostly stills; inconsistent
duration/licence metadata).

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
* **Reliability** — provider base (Archive 0.8, Pexels / Pixabay
  0.75, YouTube 0.7, test 0.3) + capped log-scaled popularity
  bonus. Not content quality, not reuse permission.
* **Combined** (prioritisation only) = (0.6·relevance +
  0.2·completeness + 0.2·reliability) × duration factor.

## Duration preference (2026-11-21)

A Short is built from short clips, so Research prefers them and
excludes feature films. The duration factor (`scoring.py`,
`duration_factor`, shared by every provider) multiplies the
combined score: under 10 minutes → ×1.15 boost; 10 minutes to the
cap → ×1.0; over the cap → ×0.0, and the service excludes the
candidate. Unknown duration → ×1.0: kept, never excluded, just
not boosted. The cap is `RESEARCH_MAX_CANDIDATE_MINUTES`
(default 15).

The cap is applied twice: once on durations known at search time
(YouTube, Pexels, Pixabay always publish one), and again after
Archive duration enrichment — an Archive movie's length is often
only known then. Enrichment runs on a pool larger than the final
cut (`max_candidates + 5`), so an excluded movie can be replaced
by a short candidate from the same run. If every candidate is
excluded, the run fails honestly (`ExternalServiceError` naming
the cap and the excluded count) instead of persisting an empty
result. Exclusions are counted in `metrics.duration_excluded`
and noted in the report limitations.

Live proof that motivated it: the cat job's only approved
candidate was "Hard Boiled & The Killer [Director's Cut]", a
~2-hour movie whose files all exceed the 500 MB download cap.
With the preference in place that movie is excluded at Research
instead of failing later at VIDEO_ANALYSIS.

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
  (hours) — those are now excluded by the duration cap, so an
  Archive-only run can return fewer candidates, or none, on topics
  the Archive mostly holds as movies/collections.
* Stock clips (Pexels / Pixabay) are generic, staged footage: they
  suit many topics ("a cat playing", "city at night") but not
  specific viral "funny fail" moments — a stock clip of a cat
  falling over is staged, not the real moment. They are a
  supplement to real sources, not a replacement for them. Their
  titles are derived metadata (URL slug / tags), so relevance
  scoring for them is weaker than for titled videos.
* YouTube discovery is Creative Commons only: the search filter and
  the licence check mean only CC BY 3.0 videos can be approved and
  used. A standard-licence video is never usable, however relevant
  it looks. Live YouTube search is unverified on this VM (the
  egress proxy blocks googleapis.com); it must be verified by a
  job on the deploy host. One honest caveat: the CC filter relies
  on the uploader tagging the video correctly — Rights approves on
  the licence the video publishes, it cannot verify the underlying
  content.
* Duration enrichment fetches the metadata API for the kept pool
  (`max_candidates + 5` candidates, Archive only); if it fails,
  duration stays unknown and the candidate is kept.
* No LLM topic understanding until a Muse AI endpoint/key exists.
