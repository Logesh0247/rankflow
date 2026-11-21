"""Momentrix AI — agent registry (Prompt 02, Phase 2).

The registry is the single place that knows the seven agents: their
id, description, input/output schemas, execution method, status and
health. All seven agents are built (Prompts 03-09) and registered
with real executors. Test doubles (Prompt 02, Phase 6) are registered explicitly
and labelled as such; they never masquerade as real agents.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.errors import AgentNotImplementedError, NotFoundError
from app.orchestrator.contracts import STAGE_OUTPUT_SCHEMAS, AgentDescriptor, AgentHealth
from app.orchestrator.states import STAGE_AGENT, WorkflowStage

# agent_id -> (display name, description, input schema name, prompt that builds it)
AGENT_SPECS: dict[str, tuple[str, str, str, str]] = {
    "research": (
        "Research Agent",
        "Discovers candidate videos for a topic and returns structured source information.",
        "JobCreate / topic + search strategy",
        "Prompt 03",
    ),
    "rights": (
        "Rights Verification Agent",
        "Checks licences and permission evidence and approves or rejects candidates for commercial use.",
        "CandidateVideoRead list",
        "Prompt 04",
    ),
    "video_intelligence": (
        "Video Intelligence Agent",
        "Understands video content, detects events, extracts frames and identifies moments with timestamps.",
        "ApprovedFootage list",
        "Prompt 05",
    ),
    "ranking": (
        "Ranking Agent",
        "Scores moments, compares them, removes duplicates and selects five moments from five unique sources.",
        "VideoMomentRead list",
        "Prompt 06",
    ),
    "creative_director": (
        "Creative Director Agent",
        "Writes the story, hooks, narration and countdown structure and produces the storyboard and edit plan.",
        "RankingResultRead list",
        "Prompt 07",
    ),
    "production": (
        "Video Production Agent",
        "Extracts clips, reframes vertically, adds narration, captions and graphics and renders the Short.",
        "EditDecisionList",
        "Prompt 08",
    ),
    "quality_assurance": (
        "Quality Assurance Agent",
        "Inspects the rendered video, validates it technically and for content and rights, and corrects or rejects it.",
        "RenderedVideoRead",
        "Prompt 09",
    ),
}

Executor = Callable[[dict], dict]


@dataclass
class AgentEntry:
    agent_id: str
    name: str
    description: str
    input_schema: str
    output_schema: str
    built_by_prompt: str
    executor: Executor | None = None
    is_test_double: bool = False

    @property
    def status(self) -> str:
        if self.is_test_double:
            return "test_double"
        return "ready" if self.executor is not None else "not_implemented"

    @property
    def execution_method(self) -> str:
        if self.is_test_double:
            return "test_double (orchestrator testing only, Prompt 02 Phase 6)"
        if self.executor is not None:
            return "in_process_callable"
        return f"not_implemented — built in {self.built_by_prompt}"

    def descriptor(self) -> AgentDescriptor:
        health_status = "test_double" if self.is_test_double else (
            "ready" if self.executor is not None else "not_implemented"
        )
        return AgentDescriptor(
            agent_id=self.agent_id,
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            execution_method=self.execution_method,
            status=self.status,  # type: ignore[arg-type]
            health=AgentHealth(
                agent_id=self.agent_id,
                status=health_status,  # type: ignore[arg-type]
                description=self.description,
                detail=("Test double only — not a real agent." if self.is_test_double
                        else ("No executor registered." if self.executor is None else "Executor registered.")),
            ),
        )


class AgentRegistry:
    """Holds the seven agents. Starts with no executors at all."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentEntry] = {}
        for agent_id, (name, description, input_schema, prompt) in AGENT_SPECS.items():
            stage = next(s for s, a in STAGE_AGENT.items() if a == agent_id)
            self._agents[agent_id] = AgentEntry(
                agent_id=agent_id,
                name=name,
                description=description,
                input_schema=input_schema,
                output_schema=STAGE_OUTPUT_SCHEMAS[stage.value].__name__,
                built_by_prompt=prompt,
            )

    def list(self) -> list[AgentDescriptor]:
        return [self._agents[aid].descriptor() for aid in AGENT_SPECS]

    def get(self, agent_id: str) -> AgentEntry:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise NotFoundError(f"unknown agent: {agent_id}") from None

    def health(self, agent_id: str) -> AgentHealth:
        return self.get(agent_id).descriptor().health

    def register_executor(self, agent_id: str, executor: Executor, *, test_double: bool = False) -> None:
        """Attach an executor. Production use must pass test_double=False
        with a real agent; orchestrator tests pass test_double=True."""
        entry = self.get(agent_id)
        entry.executor = executor
        entry.is_test_double = test_double

    def executor_for(self, agent_id: str) -> Executor:
        """Return the executor or raise: never run an unbuilt agent."""
        entry = self.get(agent_id)
        if entry.executor is None:
            raise AgentNotImplementedError(
                f"agent '{agent_id}' is not implemented yet "
                f"(built in {entry.built_by_prompt}); no executor registered"
            )
        return entry.executor


def build_default_registry(engine=None, settings=None) -> AgentRegistry:
    """Build the registry. With an engine, the real agents that exist
    so far are registered (Prompt 03: research; Prompt 04: rights;
    Prompt 05: video_intelligence; Prompt 06: ranking; Prompt 07:
    creative_director; Prompt 08: production; Prompt 09:
    quality_assurance); without one, every agent stays
    not_implemented, as in Prompt 02."""
    registry = AgentRegistry()
    if engine is not None:
        register_real_agents(registry, engine, settings)
    return registry


def register_real_agents(registry: AgentRegistry, engine, settings=None) -> AgentRegistry:
    """Attach executors for the real agents built so far (Prompt 03:
    research; Prompt 04: rights; Prompt 05: video_intelligence;
    Prompt 06: ranking; Prompt 07: creative_director; Prompt 08:
    production; Prompt 09: quality_assurance). Other agents stay not_implemented until their
    prompt. Imported lazily so the registry does not depend on
    agent code."""
    from app.agents.creative_director.agent import make_creative_director_executor
    from app.agents.production.agent import make_production_executor
    from app.agents.quality_assurance.agent import make_quality_assurance_executor
    from app.agents.ranking.agent import make_ranking_executor
    from app.agents.research.agent import make_research_executor
    from app.agents.rights.agent import make_rights_executor
    from app.agents.video_intelligence.agent import make_video_intelligence_executor

    registry.register_executor(
        "research", make_research_executor(engine, settings), test_double=False)
    registry.register_executor(
        "rights", make_rights_executor(engine, settings), test_double=False)
    registry.register_executor(
        "video_intelligence", make_video_intelligence_executor(engine, settings),
        test_double=False)
    registry.register_executor(
        "ranking", make_ranking_executor(engine, settings), test_double=False)
    registry.register_executor(
        "creative_director", make_creative_director_executor(engine, settings),
        test_double=False)
    registry.register_executor(
        "production", make_production_executor(engine, settings), test_double=False)
    registry.register_executor(
        "quality_assurance", make_quality_assurance_executor(engine, settings),
        test_double=False)
    return registry
