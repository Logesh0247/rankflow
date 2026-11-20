# RANKFLOW AI — Ranking Agent (Prompt 06)

Selects the five most entertaining and compelling moments from
five different source videos.

## How it works

1. **Load** every `AnalyzedMoment` for the job.
2. **Rights gate, again.** Each moment's candidate is re-checked
   against the Rights gate, server-side, before it is ranked —
   an approval that expired or was revoked after Video
   Intelligence ran never reaches the ranking. Unapproved
   moments are skipped and listed in the report, never ranked.
3. **Score** every approved moment from its measured signals
   (scorer `heuristic_editorial_v1`).
4. **Deduplicate** — same moment twice, overlapping moments
   from the same source, same event from different uploads.
5. **Floor** — moments below `RANKING_MIN_SCORE` (default 20/100)
   are ineligible.
6. **One moment per source** — the best-scoring moment of each
   source video; a source never contributes twice.
7. **Select five, rank #1–#5** (best first) and persist
   `RankingResult` rows with the component breakdown and a
   written explanation. The five chosen candidates are marked
   `selected`.

## Scoring — S = 0.25E + 0.25U + 0.20V + 0.15C + 0.15Q

Each component is 0–100, from a different measured signal:

| Component | From | Meaning |
|---|---|---|
| E Entertainment | mean motion + event type | high-motion segment beats a static scene |
| U Unexpectedness | event type + burst (short = sharper) | sudden change in a short burst beats a long unchanging shot |
| V Visual impact | visual quality + motion | how readable the picture is |
| C Context clarity | analyser context clarity, capped | truncated (video edge) caps at 70, frozen frames at 50 |
| Q Technical quality | visual quality + resolution + audio | 1080p with audio scores above 160×120 silent footage |

**Scores are editorial heuristics, not viral-performance
probabilities.** No semantic event is claimed: Video
Intelligence never named what happens in a scene, so ranking
cannot score "how funny the fall was" — it scores how much
measurable motion, change, sharpness and clarity the moment
has. A vision-language model can replace the signal mapping
later without changing the moment schema or the formula.

Tie-breaks are deterministic: total desc, then
unexpectedness, then entertainment, then earlier start, then
moment id — identical moments always rank the same way.

## Duplicate detection

1. Same moment id twice — never ranked twice.
2. Same source, overlapping timestamps (IoU > 0.5, the same
   rule Video Intelligence uses) — keep the higher score.
3. Same event, different uploads — heuristic fingerprint:
   same normalised title AND creator, and the moments agree
   within `RANKING_FINGERPRINT_TOLERANCE` (default 10%) on
   duration, mean motion, mean sharpness and mean brightness.
   All four must agree. This is a proxy — a re-upload with
   different metadata is not recognised — and every
   fingerprint drop is recorded in the report.

## Insufficient candidates

Fewer than five eligible sources after dedup and the floor
fails the job honestly (`ExternalServiceError`, retried by the
orchestrator, then the job is FAILED with the counts stored).
No moment is invented to fill the five, and rights approval
is never bypassed to reach five. Recovery: request more
candidates from Research (a broader topic, or more approved
footage) and re-run ranking. Note the orchestrator has no
loop back to Research — recovery is a new / resumed run with
more approved candidates, documented here rather than hidden.

## Files

- `app/agents/ranking/scoring.py` — pure scoring + duplicates
- `app/agents/ranking/service.py` — RankingService, persistence
- `app/agents/ranking/agent.py` — orchestrator executor
- `app/api/ranking.py` — `GET /jobs/{id}/ranking`,
  `GET /jobs/{id}/ranking/{rank}`
- `scripts/run_ranking.py` — demo
- `scripts/migrate_005_add_ranking_breakdown.py` — adds the
  `score_breakdown` JSON column to pre-Prompt-06 databases

## Configuration

`RANKING_MIN_SCORE=20.0`, `RANKING_DUPLICATE_IOU=0.5`,
`RANKING_FINGERPRINT_TOLERANCE=0.10` (see `.env.example`).

## Verification (Prompt 06)

Full suite: `.venv/bin/python -m pytest tests/ -q` — **158 passed,
0 failed** (132 before this prompt + 26 new ranking tests). The
ranking tests in `tests/test_ranking.py` cover the formula,
component caps, all three duplicate rules, tied scores,
weak-quality footage, unique-source enforcement, insufficient
candidates, the rights gate, idempotency, the API, and a full
orchestrator run through real research → rights → video
intelligence → ranking (then 501 at creative planning, Prompt 07).
