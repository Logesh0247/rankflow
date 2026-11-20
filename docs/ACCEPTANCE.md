# RANKFLOW AI — Final Acceptance Test (Prompt 12, Phase 7)

Topic: **TOP 5 CRAZIEST PARKOUR FAILS**
Job: `3659f94a-d639-439f-ad69-40763be6c492` — status `awaiting_approval`
Total pipeline time: **178.3 s** (stage seconds: {'creative_director': 0.1, 'production': 123.3, 'quality_assurance': 49.6, 'ranking': 0.1, 'research': 0.1, 'rights': 0.2, 'video_intelligence': 2.6})
Estimated cost: **$0.0049** (175.97 measured CPU-seconds; see docs/DEPLOYMENT.md for rates)
QA verdict: **needs_review**

## Checks

- **[PASS]** (1) relevant candidates discovered — 15 candidate videos persisted for the topic
- **[PASS]** (2) usage permissions verified — 5 approved rights-evidence rows (CC BY 4.0, attribution required)
- **[PASS]** (3) approved videos analyzed — 5 analyzed moments across the approved footage
- **[PASS]** (4) accurate moments extracted — 5 moments, all within source bounds, timestamps re-verified against the original files by Video Intelligence
- **[PASS]** (5) five different sources selected — 5 ranked moments from 5 distinct sources
- **[PASS]** (6) moments ranked — ranks 1-5 with heuristic editorial scores [0.814, 0.757, 0.644, 0.569, 0.476]
- **[PASS]** (7) original narration generated — hook + 5 moment narrations + closing, 138 words, deterministic templates from measured signals (no LLM)
- **[PASS]** (8) video professionally rendered — /tmp/rankflow-acceptance-kqx0kgov/storage/jobs/3659f94a-d639-439f-ad69-40763be6c492/production/2ade504554f9/rankflow-short.mp4 — 1080x1920, 74.1s, ffprobe valid=True
- **[PARTIAL]** (9) QA passed — QA verdict needs_review, NOT pass: narration audio was never generated (no TTS provider configured) and the audio track is effectively silent; technical, content, visual and rights checks all passed. A pass is not reachable until a TTS provider exists — see docs/ACCEPTANCE.md.
- **[PASS]** (10) final video displayed for review — download=200, QA endpoint=200, UI=200; job status=awaiting_approval (awaiting_approval)

## Live parkour job (real data)

```json
{
  "checked": true,
  "job_id": "df50d17e-f44b-4d57-829f-6b8aa77fd5b9",
  "topic": "TOP 5 CRAZIEST PARKOUR FAILS",
  "status": "created",
  "approved_candidates": 1,
  "needs_review_evidence": 9,
  "can_complete_end_to_end": false
}
```

The live parkour job cannot complete end to end: it has only 1 Rights-approved candidate and 9 `needs_review`. That is why this acceptance run uses five seeded CC BY clips with local footage — the same honest input the integration tests use. No rights check was bypassed and no source was invented.

## What QA did not pass, and why

Check (9) is PARTIAL, not PASS: the render's narration audio was never generated because no TTS provider is configured, and the audio track is effectively silent. QA correctly reports `needs_review` instead of passing it. A `pass` verdict is reachable only when a TTS provider (and licensed audio, for the music checks) is configured — that is the single remaining blocker to a fully passing acceptance run, and it is a missing integration, not a code defect.

Nothing was published automatically. The completed video waits at `awaiting_approval` for a human to approve it in the Review page.
