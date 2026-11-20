"""Rights Verification Agent (Prompt 04) — licence checking, permission evidence, commercial-use eligibility, source approval.

BUILT (Prompt 04). Modules: `licenses.py` (Phase 1 licence
catalogue), `evidence.py` (Phases 1-3 evaluation), `gate.py`
(Phase 4 server-side enforcement), `attribution.py` (Phase 5),
`service.py` (Phases 2, 5, 6 persistence + manual review),
`agent.py` (orchestrator executor). Registered with
`build_default_registry(engine)` / `register_real_agents`.

Rules the agent never breaks: a public URL is not proof of
permission; only a known licence permitting commercial use,
modification and redistribution — or a human reviewer asserting a
permission grant with the evidence recorded — can approve a
source. Missing evidence is NEEDS_REVIEW, a restrictive licence
is REJECTED, an expired permission is EXPIRED. See `docs/RIGHTS.md`.
"""

from app.agents.rights.agent import make_rights_executor
from app.agents.rights.gate import is_approved, require_approved
from app.agents.rights.service import RightsService

__all__ = ["RightsService", "is_approved", "require_approved", "make_rights_executor"]
