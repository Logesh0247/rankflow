"""Research Agent (Prompt 03) — BUILT.

Discovers candidate videos for a topic and returns structured source
information, connected to the central orchestrator.

Modules:
  topic.py      Phase 1 — topic understanding (classification, keywords,
                query expansion, related concepts, search strategy)
  providers.py  Phase 2 — discovery providers: Internet Archive (real,
                no key), YouTube (implemented, disabled without a key),
                test (TEST-ONLY deterministic provider)
  scoring.py    Phase 4 — relevance / completeness / reliability scoring
                and duplicate detection
  service.py    Phases 5-6 — persistence (Source + CandidateVideo rows
                linked to the job) and the ResearchReport
  agent.py      Phase 6 — orchestrator executor for agent 'research'

Register it with:
    from app.orchestrator.registry import build_default_registry
    registry = build_default_registry(engine)   # registers research
or:
    from app.orchestrator.registry import register_real_agents
    register_real_agents(registry, engine)

A search result is discovery only: it is never presented as
downloadable or reusable. Reuse permission is the Rights
Verification Agent's job (Prompt 04).
"""
from app.agents.research.agent import make_research_executor
from app.agents.research.service import ResearchReport, ResearchService
from app.agents.research.topic import SearchPlan, understand_topic

__all__ = [
    "make_research_executor",
    "ResearchReport",
    "ResearchService",
    "SearchPlan",
    "understand_topic",
]
