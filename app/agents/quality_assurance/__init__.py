"""RANKFLOW AI — Quality Assurance Agent (Prompt 09).

Inspects the actual rendered video and decides whether it is
suitable for final user review. Built in Prompt 09; registered
with a real executor in the orchestrator. A `fail` verdict raises
DeterministicError (Prompt 10) — the orchestrator never retries
it; recovery is via resume, which applies its repair requests.
"""

from app.agents.quality_assurance.service import QAService

__all__ = ["QAService"]
