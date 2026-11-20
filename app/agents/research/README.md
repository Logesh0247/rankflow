# Research — BUILT (Prompt 03)

Research Agent — topic understanding, search strategy, source discovery,
metadata collection, candidate management.

Modules: `topic.py` (Phase 1), `providers.py` (Phase 2: Internet Archive
real, YouTube disabled without a key, test TEST-ONLY), `scoring.py`
(Phase 4), `service.py` (Phases 5–6), `agent.py` (orchestrator executor).

Registered with `build_default_registry(engine)` / `register_real_agents`.
A search result is discovery only — reuse permission is the Rights
Verification Agent's job (Prompt 04). See `docs/RESEARCH.md`.
