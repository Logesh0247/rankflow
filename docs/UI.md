# Prompt 11 — User Interface

The RANKFLOW space: a Creator Studio dashboard in the style of the
user's reference image (dark theme, purple accent), served by the
same FastAPI backend it talks to — same origin, no separate host,
no simulated data.

## Where it lives

- UI: `app/ui/index.html` (single-page app, vanilla JS, no build step)
  + `app/ui/poster.jpg` — a real frame (t=1s, intro title card) from
  the demo render `data/outputs/rankflow-short-demo.mp4`, cropped to
  16:9 for the hero card.
- Served at `GET /ui/` (and `/ui/index.html`) via `StaticFiles` in
  `main.py`. Run the backend, then open `http://127.0.0.1:8000/ui/`.
- API base is same-origin by default; `?api=<base-url>` overrides it
  (used by the hosted copy of the UI — see "Hosted copy" below).

## Pages (all five, plus the reference sidebar)

Sidebar (reference order, reference labels): Overview, Projects,
Campaigns, Content Library, Templates, Brand Kit, Avatars,
Workflows, Analytics, Team, Settings.

| Sidebar item | RANKFLOW page | Real data |
|---|---|---|
| Overview | Home | Hero, topic input + Generate, Recent Projects (`GET /jobs`), Activity from job history, agent tools with live health (`GET /agents`), asset cards (hook / top moment / video / narration / QA verdict), 5-step workflow strip (Brief & Plan → Create & Generate → Review & Approve → Publish & Distribute → Measure & Optimise) with the latest job's stage highlighted |
| Projects | Job History | `GET /jobs` table: topic, status, stage, created, error |
| Campaigns / Workflows | Production Dashboard | Job picker, progress from `current_stage` (7 stages), active agent, stage checklist, completed/failed tasks from `GET /jobs/{id}/history`, QA warnings, Resume / Cancel |
| Content Library | Final Video Review | `<video>` from `GET /jobs/{id}/rendered-video/download`, topic, duration, resolution, QA verdict/issues/warnings, ranking #1–#5 with scores, narration script, Download / Approve / Reject & Regenerate |
| Templates | Home | Topic input with three example topics (real topics used in research tests) |
| Brand Kit / Avatars / Analytics / Team | Honest scope page | States plainly that RANKFLOW has no brand kit, avatar studio, analytics, or team features — no fake data is shown |
| Settings | Settings | `GET /settings/status` + `GET /health` |

## New backend endpoints (Prompt 11)

- `POST /jobs/{id}/approve` — approves only an `awaiting_approval`
  job (`JobService.approve_job`); anything else → 422.
- `POST /jobs/{id}/reject` — Reject & Regenerate: invalidates the
  VIDEO_PRODUCTION and QUALITY_ASSURANCE checkpoints and re-runs
  them from the approved EDL (`WorkflowEngine.regenerate_job`).
  Research → Creative Director checkpoints are kept, so only the
  render + QA re-run. A user rejection is **not** a QA repair round
  (`invalidate_checkpoints(..., count_as_repair_round=False)`) and
  does not consume the `qa_max_repair_attempts` cap. Runs
  synchronously; the UI polls `GET /jobs/{id}` while it runs.
- `GET /settings/status` — configuration status only: provider
  configured (bool), TTS/music/SFX configured (bool), video output
  (1080×1920 @ 30 fps, H.264/AAC, CRF), storage, per-agent health.
  Never returns a secret or API key — see
  `tests/test_ui.py::test_settings_status_never_leaks_secrets`.
- `JobRead.current_stage` — added to the schema and to
  `JobService._read` and `app/api/jobs.py`, so the dashboard can
  show real progress instead of guessing from status.

## Generate flow (UI path, verified by tests)

`POST /jobs {topic}` → `POST /jobs/{id}/run` (async from the UI;
the run takes minutes on CPU) → dashboard polls `GET /jobs/{id}`
every 4 s → job reaches `awaiting_approval` → Review page plays
the render, Approve or Reject & Regenerate.

## Hosted copy

The same UI exists as a Muse space (static artifact). It is the
same code, with the API base overridable via `?api=`. It can only
show live data when the RANKFLOW backend is reachable from the
browser it runs in; otherwise it shows "Backend unreachable" and
never invents jobs, videos, or progress. The authoritative UI is
the one served by the backend at `/ui/`.

## Known limitations

- No auto-publish: Approve marks the job `approved`; publishing
  stays manual (Prompt 12 covers deployment).
- The demo render in the live DB is a file only
  (`data/outputs/rankflow-short-demo.mp4`); no live job row points
  at it, so the Review page shows the honest "no rendered video
  yet" state for the existing live jobs until a real job completes
  in the live DB. The full pipeline + regenerate flow is proven by
  `tests/test_ui.py::test_reject_regenerates_real_completed_job`.
- The reference image's "Team Activity" names and "Recent Projects"
  thumbnails are not reproduced — Activity shows real agent logs,
  Recent Projects shows real jobs, and the hero image is a real
  frame from the demo render.
- Mobile: sidebar collapses behind a ☰ button; hero/assets/workflow
  stack to one column.
