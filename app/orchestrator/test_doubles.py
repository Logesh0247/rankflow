"""Momentrix AI — TEST-ONLY test doubles (Prompt 02, Phase 6).

These are minimal deterministic stand-ins used ONLY to test the
orchestrator. They are NOT the seven real agents and must never be
presented as such:

- outputs are derived deterministically from the job id, so the
  same job always produces the same (fake) candidate/moment ids;
- no external API, video file or model is touched;
- failure behaviour is configured explicitly per agent so the
  orchestrator's retry / timeout / invalid-output handling can be
  verified.

Register them with ``register_test_doubles(registry, config)``.
Production code must never call this module.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from app.orchestrator.registry import AgentRegistry

AGENT_IDS = ["research", "rights", "video_intelligence", "ranking",
             "creative_director", "production", "quality_assurance"]


class DoubleConfig:
    """Per-agent failure behaviour for orchestrator tests.

    fail_attempts: attempt numbers (1-based) that raise an error.
    invalid_attempts: attempt numbers that return output that fails
        the stage schema (e.g. ranking with duplicate sources).
    slow_seconds: seconds the executor sleeps (for timeout tests).
    """

    def __init__(self, fail_attempts: set[int] | None = None,
                 invalid_attempts: set[int] | None = None,
                 slow_seconds: float = 0.0):
        self.fail_attempts = fail_attempts or set()
        self.invalid_attempts = invalid_attempts or set()
        self.slow_seconds = slow_seconds


def _maybe_fail(config: DoubleConfig, task: dict) -> None:
    if config.slow_seconds:
        time.sleep(config.slow_seconds)
    attempt = task["attempt"]
    if attempt in config.fail_attempts:
        raise RuntimeError(f"TEST DOUBLE: simulated failure on attempt {attempt}")
    if attempt in config.invalid_attempts:
        # Deliberately schema-invalid output for this stage; the
        # engine must reject it and retry.
        raise _InvalidOutput(task["agent_id"])


class _InvalidOutput(Exception):
    """Marker: the double returns invalid output on this attempt."""

    def __init__(self, agent_id: str):
        super().__init__(f"TEST DOUBLE: invalid output for {agent_id}")
        self.agent_id = agent_id


def _invalid_output(agent_id: str) -> dict:
    if agent_id == "ranking":
        # Five ranks but only one source video — violates the
        # five-unique-sources rule in RankingOutput.
        return {"ranks": [{"rank": i + 1, "moment_id": f"m{i}", "candidate_video_id": "same",
                           "score": 0.9} for i in range(5)]}
    return {"not_a_real_field": True}


def make_double(agent_id: str, config: DoubleConfig | None = None) -> Callable[[dict], dict]:
    """Build a deterministic test-double executor for one agent."""
    config = config or DoubleConfig()

    def executor(task: dict) -> dict:
        job_id = task["job_id"]
        try:
            _maybe_fail(config, task)
        except _InvalidOutput:
            return _invalid_output(agent_id)
        refs = task.get("input_references", {})
        if agent_id == "research":
            ids = [f"{job_id}-cand-{i}" for i in range(1, 6)]
            return {"candidate_ids": ids, "candidate_video_ids": ids}
        if agent_id == "rights":
            approved = refs.get("approved_candidate_ids") or refs.get("candidate_ids") or []
            approved = list(approved)[:5] or [f"{job_id}-cand-{i}" for i in range(1, 6)]
            return {"approved_candidate_ids": approved, "rejected_candidate_ids": []}
        if agent_id == "video_intelligence":
            approved = refs.get("approved_candidate_ids") or [f"{job_id}-cand-{i}" for i in range(1, 6)]
            return {"moment_ids": [f"{job_id}-moment-{i}" for i in range(1, 6)],
                    "candidate_video_ids": list(approved)}
        if agent_id == "ranking":
            moment_ids = refs.get("moment_ids") or [f"{job_id}-moment-{i}" for i in range(1, 6)]
            return {"ranks": [{"rank": i + 1, "moment_id": moment_ids[i],
                               "candidate_video_id": f"{job_id}-cand-{i + 1}",
                               "score": round(0.95 - i * 0.05, 2)} for i in range(5)]}
        if agent_id == "creative_director":
            ranked = refs.get("ranked_moment_ids") or [f"{job_id}-moment-{i}" for i in range(1, 6)]
            return {"storyboard_id": f"{job_id}-storyboard", "scene_count": 5,
                    "ranked_moment_ids": list(ranked)}
        if agent_id == "production":
            return {"rendered_video_id": f"{job_id}-render",
                    "file_path": f"data/storage/{job_id}/short.mp4",
                    "duration_seconds": 60.0, "width": 1080, "height": 1920}
        if agent_id == "quality_assurance":
            return {"verdict": "pass", "issues": []}
        raise RuntimeError(f"TEST DOUBLE: unknown agent {agent_id}")

    return executor


def register_test_doubles(registry: AgentRegistry,
                          configs: dict[str, DoubleConfig] | None = None) -> AgentRegistry:
    """Register a test double for every agent. Labelled test-only."""
    configs = configs or {}
    for agent_id in AGENT_IDS:
        registry.register_executor(agent_id, make_double(agent_id, configs.get(agent_id)),
                                   test_double=True)
    return registry


def build_test_registry(configs: dict[str, DoubleConfig] | None = None) -> AgentRegistry:
    from app.orchestrator.registry import build_default_registry
    return register_test_doubles(build_default_registry(), configs)
