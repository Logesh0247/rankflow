"""Momentrix AI — Rights Verification Agent, Phase 5: attribution (Prompt 04).

Attribution is generated from the stored evidence, never invented:
title and creator come from the Research candidate, the licence
name/URL from the parsed licence. When the creator is unknown the
text says so instead of guessing a name.

The attribution is stored on the RightsEvidence row and made
available to the Creative Director and Video Production agents
through the rights service / API — the production agent must be
able to render it in the Short's credits/caption when it is
required, and may include it when it is only recommended (CC0).
"""
from __future__ import annotations

from app.agents.rights.licenses import LicenceInfo


def build_attribution(*, title: str | None, creator: str | None, url: str | None,
                      licence: LicenceInfo | None) -> str | None:
    """Attribution text for one approved candidate, or None when
    there is no licence to attribute to."""
    if licence is None:
        return None
    who = creator.strip() if creator and creator.strip() else "Unknown creator"
    what = f'"{title.strip()}"' if title and title.strip() else "Untitled video"
    source = f" ({url})" if url else ""
    return f"{what} by {who}{source}, licensed under {licence.name} ({licence.url})"


def attribution_required(licence: LicenceInfo | None) -> bool:
    return bool(licence and licence.attribution_required)
