# RANKFLOW AI — Shared Data Contracts (Prompt 01)

Source: `app/schemas/schemas.py` (Pydantic v2). Every agent exchanges
these models, never ad-hoc dicts. Validation happens on construction.

| Model | Used by | Notes |
|---|---|---|
| `JobCreate` / `JobRead` | API, orchestrator | topic 1–200 chars, stripped |
| `AgentTask` / `AgentResponse` | Orchestrator ↔ agents | `agent_name` must match `^[a-z_]+$` |
| `SourceCreate` / `SourceRead` | Research | platform + external_id |
| `CandidateVideoCreate` / `Read` | Research, Rights | `relevance_score` 0–1 |
| `RightsVerificationCreate` / `Read` | Rights | verdict: approved / rejected / needs_review / expired / not_checked; full Phase 2 evidence (licence name/URL, permission evidence, verification date/method, commercial/modification/redistribution flags, attribution, restrictions, expiry, reviewer) |
| `VideoMomentCreate` / `Read` | Video Intelligence | `end_seconds > start_seconds`, score 0–1 |
| `RankingResultCreate` / `Read` | Ranking | rank 1–5, score 0–1; `Read` also carries `score_breakdown` (Prompt 06: the five 0–100 component scores + total) |
| `StoryboardCreate` / `Read`, `StoryboardScene` | Creative Director | scenes carry rank + moment_id |
| `NarrationCreate` / `Read` | Creative Director / Production | status draft / generated / approved |
| `EditDecisionList`, `EDLClip` | Production | 1–5 clips, default 1080×1920 @ 30 fps |
| `EditInstructionRead` | Production | stored EDL + output size |
| `RenderedVideoRead` | Production / QA | status rendering / ready / failed |
| `QAReportCreate` / `Read`, `QACheck` | QA | verdict pass / fail / needs_review / not_tested |
| `QAOutput` | QA → orchestrator | verdict pass / needs_review (fail raises instead of returning); carries report_id, issues, warnings, repair_requests, recommendations, technical/content/rights validation |
| `AgentExecutionLogRead` | Orchestrator | status started / succeeded / failed / skipped |
| `HealthResponse` | API | status ok / degraded + db / ffmpeg / storage |

## Rules

- Unknown fields are rejected by Pydantic defaults where set; agents
  must not add fields without updating the contract.
- Scores are always 0–1 floats; ranks are always 1–5 ints.
- Timestamps are timezone-aware datetimes.
- A QA verdict of `not_tested` is honest: it means the check did
  not run, never a pass.
