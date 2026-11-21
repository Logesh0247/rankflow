"""Error-handling standards for Momentrix AI.

Agents and API routes raise these typed errors; the API layer maps
them to HTTP status codes. Error bodies never leak secrets, stack
traces or internal paths in production.
"""
from __future__ import annotations


class RankflowError(Exception):
    """Base error. `code` is a stable machine-readable string."""

    code = "rankflow_error"
    status_code = 500

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.message = message


class ValidationError(RankflowError):
    code = "validation_error"
    status_code = 422


class NotFoundError(RankflowError):
    code = "not_found"
    status_code = 404


class ConfigurationError(RankflowError):
    code = "configuration_error"
    status_code = 500


class StorageError(RankflowError):
    code = "storage_error"
    status_code = 500


class ExternalServiceError(RankflowError):
    code = "external_service_error"
    status_code = 502


class AgentNotImplementedError(RankflowError):
    """Raised when code calls an agent that Prompt 01 deliberately did not build."""

    code = "agent_not_implemented"
    status_code = 501


class DeterministicError(ExternalServiceError):
    """A stage failure that re-running the same stage cannot fix
    (Prompt 10): the input and the code are unchanged, so the
    result would be identical. It subclasses ExternalServiceError
    (a stage failed), with one extra meaning the orchestrator
    honours: it must NOT be retried — one attempt, then the job
    fails with the repair requests attached, and recovery happens
    via resume (which re-runs the owning stage) or human
    intervention, never by blind retry. QA's fail verdict is the
    first such error: re-inspecting the same render always fails
    the same way."""

    code = "deterministic_error"
    status_code = 422
