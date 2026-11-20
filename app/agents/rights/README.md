# Rights — BUILT (Prompt 04)

Rights Verification Agent — licence checking, permission evidence, commercial-use eligibility, source approval.

Modules: `licenses.py` (Phase 1 licence catalogue), `evidence.py` (Phases 1–3 evaluation), `gate.py` (Phase 4 server-side enforcement), `attribution.py` (Phase 5), `service.py` (Phases 2, 5, 6 persistence + manual review), `agent.py` (orchestrator executor).

Registered with `build_default_registry(engine)` / `register_real_agents`. A public URL is not proof of permission — only a known licence permitting commercial use, modification and redistribution, or a human reviewer asserting a permission grant with the evidence recorded, can approve a source. See `docs/RIGHTS.md`.
