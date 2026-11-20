"""RANKFLOW AI — Quality Assurance Agent, Phase 5: rights validation (Prompt 09).

Re-checks, server-side and at QA time, that every source used
in the render — and every audio asset mixed into it — still has
sufficient permission evidence, and that attribution
requirements are met in the delivered video. This is a fresh
read of the Rights gate, not a copy of the verdict Production
saw: an approval that expired between production and QA must
fail here.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.rights.gate import is_approved, latest_verdict


class RightsValidation(BaseModel):
    """Results of the rights re-check for the rendered video."""

    candidates_checked: int = 0
    approved: list[str] = Field(default_factory=list)
    not_approved: list[str] = Field(default_factory=list)
    attribution_missing: list[str] = Field(default_factory=list)
    music_status: str = ""
    sfx_status: str = ""
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def validate_rights(session, job_id: str, candidate_ids: list[str],
                    attributions: list[dict], *, manifest: dict | None = None) -> RightsValidation:
    """Phase 5: gate re-check per candidate + attribution check.
    ``attributions`` are the manifest attributions (candidate id,
    attribution text, attribution_required, licence name)."""
    issues: list[str] = []
    warnings: list[str] = []
    approved: list[str] = []
    not_approved: list[str] = []
    for candidate_id in candidate_ids:
        if is_approved(session, candidate_id):
            approved.append(candidate_id)
        else:
            verdict = latest_verdict(session, candidate_id) or "not_checked"
            not_approved.append(candidate_id)
            issues.append(
                f"candidate {candidate_id} is not approved for use "
                f"(latest verdict: {verdict}) — the render contains "
                "footage without sufficient permission evidence")
    attribution_missing: list[str] = []
    by_candidate = {a.get("candidate_video_id"): a for a in attributions}
    for candidate_id in candidate_ids:
        entry = by_candidate.get(candidate_id) or {}
        if entry.get("attribution_required") and not (entry.get("attribution_text") or "").strip():
            attribution_missing.append(candidate_id)
            issues.append(
                f"candidate {candidate_id} requires attribution but the "
                "render manifest carries no attribution text for it")
    manifest = manifest or {}
    music = manifest.get("music") or {}
    sfx_chime = manifest.get("sfx_chime") or {}
    sfx_whoosh = manifest.get("sfx_whoosh") or {}
    music_status = str(music.get("status") or "unknown")
    sfx_status = str(sfx_chime.get("status") or sfx_whoosh.get("status") or "unknown")
    if music_status == "included":
        if not music.get("file_path"):
            issues.append("music is recorded as included but no licensed music file is named")
    elif music_status.startswith("not_included"):
        warnings.append("no licensed background music is in the render — "
                        "the video is silent except for source-clip audio")
    if sfx_status.startswith("not_included"):
        warnings.append("no licensed sound effects are mixed into the render")
    return RightsValidation(
        candidates_checked=len(candidate_ids), approved=approved,
        not_approved=not_approved, attribution_missing=attribution_missing,
        music_status=music_status, sfx_status=sfx_status,
        issues=issues, warnings=warnings)
