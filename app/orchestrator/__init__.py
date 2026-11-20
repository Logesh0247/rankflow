"""RANKFLOW AI orchestrator package (Prompt 02).

Exports the central orchestrator: workflow states, communication
contracts, agent registry, job management and the workflow engine.
The seven real agents are NOT built here — see
app/agents/*/README.md (Prompts 03-09). Test doubles live in
app/orchestrator/test_doubles.py and are labelled TEST-ONLY.
"""
from app.orchestrator.contracts import OrchestratorResponse, OrchestratorTask
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import AgentRegistry, build_default_registry
from app.orchestrator.states import AGENT_STAGES, WorkflowStage

__all__ = [
    "AGENT_STAGES",
    "AgentRegistry",
    "JobService",
    "OrchestratorResponse",
    "OrchestratorTask",
    "WorkflowEngine",
    "WorkflowStage",
    "build_default_registry",
]
