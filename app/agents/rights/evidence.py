"""RANKFLOW AI — Rights Verification Agent, Phases 1-3: evaluation (Prompt 04).

One pure function, `evaluate()`, turns the evidence for a candidate
into a verdict. It is deliberately pure (no database, no network)
so every rule can be tested without a job.

Verdict vocabulary (stored lowercase, matching the database CHECK;
the brief's APPROVED / REJECTED / NEEDS_REVIEW / EXPIRED are the
same four values in upper case):

* ``approved``    — evidence supports the intended use: a known
  licence that permits commercial use, modification and
  redistribution, not expired, or a human reviewer who asserted a
  permission grant and supplied the evidence for it.
* ``rejected``    — evidence affirmatively forbids the intended
  use: a NonCommercial or NoDerivatives licence, a standard
  platform licence, "all rights reserved", or a test-only marker.
* ``needs_review``— no usable evidence: no licence published, an
  unrecognised licence string, or a Public Domain Mark (which is a
  mark, not a licence). A public video URL is not proof of
  permission, and a platform's standard viewing terms are never
  assumed to permit downloading, editing or commercial republication.
* ``expired``     — the permission had an expiry date and it has
  passed. Expiry beats every other signal: an expired CC BY
  licence is not approved.

The agent NEVER approves a source merely to keep production
moving: when in doubt the verdict is ``needs_review`` (a human
may supply evidence) or ``rejected`` (the evidence forbids it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.agents.rights.attribution import attribution_required, build_attribution
from app.agents.rights.licenses import LicenceInfo, parse_licence, rejection_reason


class ManualEvidence(BaseModel):
    """Evidence supplied by a human reviewer (API review endpoint).

    `permission_granted` is the reviewer's own assertion that the
    creator/rightsholder granted permission, with `permission_evidence`
    describing where that grant is recorded (a URL, a message
    reference, a licence document). The agent records the assertion
    and the reviewer — it does not invent the grant.
    """

    licence_name: str | None = None
    licence_url: str | None = None
    permission_evidence: str | None = None
    permission_granted: bool = False
    expires_at: datetime | None = None
    geographic_restrictions: str | None = None
    time_restrictions: str | None = None
    notes: str | None = None
    reviewed_by: str = "reviewer"


@dataclass
class RightsDecision:
    """The outcome of one evaluation, ready to persist."""

    verdict: str  # approved | rejected | needs_review | expired
    licence: LicenceInfo | None = None
    licence_name: str | None = None
    licence_url: str | None = None
    permission_evidence: str | None = None
    commercial_use: bool = False
    modification: bool = False
    redistribution: bool = False
    attribution_required: bool = False
    attribution_text: str | None = None
    geographic_restrictions: str | None = None
    time_restrictions: str | None = None
    expires_at: datetime | None = None
    verification_method: str = "licence_catalogue"
    reviewer_status: str = "auto"
    reasons: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def evaluate(*, licence_raw: str | None, title: str | None, creator: str | None,
             url: str | None, platform: str,
             manual: ManualEvidence | None = None) -> RightsDecision:
    """Evaluate one candidate's permission evidence.

    `licence_raw` is the provider-published licence string (None =
    the provider published none). `manual` is reviewer-supplied
    evidence, if any — it can only add evidence, never override a
    licence that forbids the intended use.
    """
    manual = manual or ManualEvidence()
    reasons: list[str] = []

    # --- expiry first: an expired permission is expired, whatever it was.
    expires_at = _as_aware(manual.expires_at)
    if expires_at is not None and expires_at <= _now():
        return RightsDecision(
            verdict="expired", expires_at=expires_at,
            licence_name=manual.licence_name or licence_raw,
            licence_url=manual.licence_url or licence_raw,
            permission_evidence=manual.permission_evidence,
            geographic_restrictions=manual.geographic_restrictions,
            time_restrictions=manual.time_restrictions,
            verification_method="manual_review" if manual.reviewed_by != "reviewer" or manual.permission_evidence else "licence_catalogue",
            reviewer_status="reviewed" if manual.permission_evidence else "auto",
            reasons=[f"permission expired on {expires_at.date().isoformat()}"],
        )

    # --- licence from the manual evidence takes precedence as the
    #     evidence string, but it is still parsed, never trusted blind.
    licence_text = manual.licence_url or manual.licence_name or licence_raw
    info = parse_licence(licence_text)
    method = "manual_review" if manual.permission_evidence or manual.licence_url or manual.licence_name else "licence_catalogue"

    if info is not None and info.key == "not-permitted":
        return RightsDecision(
            verdict="rejected", licence=info,
            licence_name=info.name,
            licence_url=licence_text if licence_text.startswith("http") else None,
            permission_evidence=manual.permission_evidence,
            geographic_restrictions=manual.geographic_restrictions,
            time_restrictions=manual.time_restrictions,
            expires_at=expires_at, verification_method=method,
            reviewer_status="reviewed" if manual.permission_evidence else "auto",
            reasons=[rejection_reason(info) or "licence does not permit the intended use"],
        )

    if info is not None and info.is_licence_grant:
        reason = rejection_reason(info)
        if reason is not None:
            # A restrictive licence forbids the use even if a reviewer
            # supplied it: manual evidence cannot override a licence.
            return RightsDecision(
                verdict="rejected", licence=info,
                licence_name=info.name, licence_url=info.url,
                permission_evidence=manual.permission_evidence,
                commercial_use=info.commercial_use, modification=info.modification,
                redistribution=info.redistribution,
                attribution_required=info.attribution_required,
                attribution_text=build_attribution(
                    title=title, creator=creator, url=url, licence=info),
                geographic_restrictions=manual.geographic_restrictions,
                time_restrictions=manual.time_restrictions,
                expires_at=expires_at, verification_method=method,
                reviewer_status="reviewed" if manual.permission_evidence else "auto",
                reasons=[reason],
            )
        # Permissive, known licence: approve on the licence alone.
        return RightsDecision(
            verdict="approved", licence=info,
            licence_name=info.name, licence_url=info.url,
            permission_evidence=manual.permission_evidence,
            commercial_use=True, modification=True, redistribution=True,
            attribution_required=info.attribution_required,
            attribution_text=build_attribution(
                title=title, creator=creator, url=url, licence=info),
            geographic_restrictions=manual.geographic_restrictions,
            time_restrictions=manual.time_restrictions,
            expires_at=expires_at, verification_method=method,
            reviewer_status="reviewed" if manual.permission_evidence else "auto",
            reasons=[f"licence {info.name} permits commercial use, modification and redistribution"]
            + ([c for c in info.conditions] if info.conditions else []),
        )

    if info is not None and not info.is_licence_grant:
        # Public Domain Mark: known marker, not a licence grant.
        return RightsDecision(
            verdict="needs_review", licence=info,
            licence_name=info.name, licence_url=info.url,
            permission_evidence=manual.permission_evidence,
            geographic_restrictions=manual.geographic_restrictions,
            time_restrictions=manual.time_restrictions,
            expires_at=expires_at, verification_method=method,
            reviewer_status="pending",
            reasons=["Public Domain Mark is a mark, not a licence — a human must confirm public-domain status before use"],
        )

    # --- no usable licence. A human permission grant is the only
    #     remaining route to approval, and only with evidence.
    if manual.permission_granted:
        if not (manual.permission_evidence and manual.permission_evidence.strip()):
            raise ValueError(
                "permission_granted requires permission_evidence: "
                "record where the grant is (a URL, message or document reference)")
        return RightsDecision(
            verdict="approved", licence=None,
            licence_name=manual.licence_name or "permission granted by rightsholder",
            licence_url=manual.licence_url,
            permission_evidence=manual.permission_evidence,
            commercial_use=True, modification=True, redistribution=True,
            attribution_required=False,
            attribution_text=build_attribution(
                title=title, creator=creator, url=url, licence=None),
            geographic_restrictions=manual.geographic_restrictions,
            time_restrictions=manual.time_restrictions,
            expires_at=expires_at, verification_method="manual_review",
            reviewer_status="reviewed",
            reasons=[f"permission granted by rightsholder, asserted by reviewer {manual.reviewed_by}"],
        )

    if licence_text:
        reasons.append(
            f"licence {licence_text!r} is not a recognised licence for the intended use — "
            "it was not guessed, and no permission was assumed")
    else:
        reasons.append(
            "no licence metadata published by the provider — a public video URL "
            "is not proof of permission, and standard viewing terms do not "
            "permit downloading, editing or commercial republication")
    if platform == "youtube":
        reasons.append(
            "YouTube standard licence permits viewing only unless the video "
            "publishes a Creative Commons licence or the creator grants permission")
    return RightsDecision(
        verdict="needs_review", licence=None,
        licence_name=manual.licence_name or licence_raw,
        licence_url=manual.licence_url or licence_raw,
        permission_evidence=manual.permission_evidence,
        geographic_restrictions=manual.geographic_restrictions,
        time_restrictions=manual.time_restrictions,
        expires_at=expires_at, verification_method=method,
        reviewer_status="pending",
        reasons=reasons,
    )
