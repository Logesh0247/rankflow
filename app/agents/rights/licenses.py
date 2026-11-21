"""Momentrix AI — Rights Verification Agent, Phase 1: licence catalogue (Prompt 04).

The catalogue is the single source of truth for what a licence
permits for Momentrix's intended use: taking a video, editing it
(cutting, reframing to 1080x1920, adding narration/captions) and
republishing the result commercially as a YouTube Short.

Only a licence whose permissions cover ALL THREE of commercial
use, modification and redistribution can be approved on licence
evidence alone. A licence that forbids any one of them is rejected
for that reason — the agent never approves "close enough".

A public video URL is not a licence. "Standard YouTube licence",
"all rights reserved", and the test provider's "test-only" marker
are recognised as *not* permitting the intended use.

The Public Domain Mark is NOT a licence: it is a statement that
the item has been identified as public domain, not a legal grant.
Items carrying only the Mark are NEEDS_REVIEW, never auto-approved,
so a human confirms the public-domain status before use.

Licence strings come from the provider (Research stores the
provider-published `licence` verbatim, None when the provider
published none — never invented). Parsing is deterministic and
rule-based: no LLM, no network call. Unrecognised strings are
NEEDS_REVIEW, never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LicenceInfo:
    """What one licence permits for the intended use."""

    key: str
    name: str
    url: str
    commercial_use: bool
    modification: bool
    redistribution: bool
    attribution_required: bool
    share_alike: bool = False
    # True only when the string is a real licence grant. The Public
    # Domain Mark and similar markers are known but not grants.
    is_licence_grant: bool = True
    conditions: list[str] = field(default_factory=list)


def _cc(version: str, variant: str, slug: str, *, commercial: bool, modification: bool,
        attribution: bool = True, share_alike: bool = False) -> LicenceInfo:
    name = f"Creative Commons {variant} {version}"
    conditions: list[str] = []
    if not commercial:
        conditions.append("non-commercial only — commercial republication is not permitted")
    if not modification:
        conditions.append("no derivatives — editing, reframing and remixing are not permitted")
    if share_alike:
        conditions.append("share-alike — the Short must be released under the same licence")
    if attribution:
        conditions.append("attribution required")
    return LicenceInfo(
        key=f"cc-{slug}-{version}",
        name=name,
        url=f"https://creativecommons.org/licenses/{slug}/{version}/",
        commercial_use=commercial,
        modification=modification,
        redistribution=True,
        attribution_required=attribution,
        share_alike=share_alike,
        conditions=conditions,
    )


# ---------------------------------------------------------------- catalogue
def _catalogue() -> dict[str, LicenceInfo]:
    out: dict[str, LicenceInfo] = {}
    for version in ("4.0", "3.0", "2.5", "2.0", "1.0"):
        out[f"cc-by-{version}"] = _cc(version, "Attribution", "by",
                                       commercial=True, modification=True)
        out[f"cc-by-sa-{version}"] = _cc(version, "Attribution-ShareAlike", "by-sa",
                                         commercial=True, modification=True, share_alike=True)
        out[f"cc-by-nc-{version}"] = _cc(version, "Attribution-NonCommercial", "by-nc",
                                         commercial=False, modification=True)
        out[f"cc-by-nd-{version}"] = _cc(version, "Attribution-NoDerivatives", "by-nd",
                                         commercial=True, modification=False)
        out[f"cc-by-nc-sa-{version}"] = _cc(version, "Attribution-NonCommercial-ShareAlike", "by-nc-sa",
                                            commercial=False, modification=True, share_alike=True)
        out[f"cc-by-nc-nd-{version}"] = _cc(version, "Attribution-NonCommercial-NoDerivatives", "by-nc-nd",
                                            commercial=False, modification=False)
    out["cc0-1.0"] = LicenceInfo(
        key="cc0-1.0", name="CC0 1.0 Universal (Public Domain Dedication)",
        url="https://creativecommons.org/publicdomain/zero/1.0/",
        commercial_use=True, modification=True, redistribution=True,
        attribution_required=False,
        conditions=["attribution not required, but recommended"],
    )
    out["public-domain-mark"] = LicenceInfo(
        key="public-domain-mark", name="Public Domain Mark 1.0",
        url="https://creativecommons.org/publicdomain/mark/1.0/",
        commercial_use=True, modification=True, redistribution=True,
        attribution_required=False, is_licence_grant=False,
        conditions=["this is a mark, not a licence — public-domain status must be confirmed by a human"],
    )
    # Stock-library platform licences (added after Prompt 12, for the
    # Pexels / Pixabay providers). Both platforms release every video
    # under their platform licence, which permits free commercial use,
    # modification and redistribution without attribution — so they
    # are real licence grants and can be approved on licence evidence
    # alone, exactly like CC BY. The providers record the platform
    # licence URL (the published terms); neither API publishes a
    # per-video licence field. Pixabay's API terms ask users to credit
    # Pixabay when displaying search results — a courtesy request,
    # not a licence condition, so attribution is not required.
    out["pexels-license"] = LicenceInfo(
        key="pexels-license", name="Pexels License",
        url="https://www.pexels.com/license/",
        commercial_use=True, modification=True, redistribution=True,
        attribution_required=False,
        conditions=["attribution not required, but appreciated"],
    )
    out["pixabay-license"] = LicenceInfo(
        key="pixabay-license", name="Pixabay Content License",
        url="https://pixabay.com/service/license/",
        commercial_use=True, modification=True, redistribution=True,
        attribution_required=False,
        conditions=["attribution not required; Pixabay asks API users to credit Pixabay when displaying search results"],
    )
    return out


CATALOGUE: dict[str, LicenceInfo] = _catalogue()

# Strings that are recognised as affirmatively NOT permitting the
# intended use (not "unknown" — known to forbid it).
_REJECT_MARKERS: list[tuple[str, str]] = [
    ("test-only", "test-only marker — not a real source and not a licence"),
    ("standard youtube", "standard YouTube licence — permits viewing only, not downloading, editing or commercial republication"),
    ("youtube standard", "standard YouTube licence — permits viewing only, not downloading, editing or commercial republication"),
    ("all rights reserved", "all rights reserved — no permission granted"),
]

_CC_URL_RE = re.compile(
    r"creativecommons\.org/licenses/(by(?:-nc)?(?:-nd)?(?:-sa)?)/(\d+\.\d+)", re.IGNORECASE)
_CC_TEXT_RE = re.compile(
    r"\bcc[\s\-]?(by(?:[\s\-]?nc)?(?:[\s\-]?nd)?(?:[\s\-]?sa)?)[\s\-]?(\d+\.\d+)?\b", re.IGNORECASE)
_CC0_RE = re.compile(r"\bcc0\b|publicdomain/zero", re.IGNORECASE)
_PDM_RE = re.compile(r"publicdomain/mark|public\s+domain\s+mark", re.IGNORECASE)
# Stock-library platform licences: the provider records the platform
# licence URL (e.g. https://www.pexels.com/license/), so match the
# platform name in the string. Checked after the CC rules — a CC URL
# never contains "pexels" or "pixabay".
_PEXELS_RE = re.compile(r"pexels", re.IGNORECASE)
_PIXABAY_RE = re.compile(r"pixabay", re.IGNORECASE)


def _normalise_variant(raw: str) -> str:
    """'by-nc-sa' / 'by nc sa' / 'BY-NC-SA' -> 'by-nc-sa'."""
    parts = [p for p in re.split(r"[\s\-]+", raw.lower()) if p]
    if not parts or parts[0] != "by":
        return "by"
    return "-".join(parts)


def parse_licence(raw: str | None) -> LicenceInfo | None:
    """Parse a provider-published licence string into catalogue info.

    Returns None when the string is missing or unrecognised — the
    caller must treat that as "no usable licence evidence", never
    as a permissive licence.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    lowered = text.lower()

    for marker, _reason in _REJECT_MARKERS:
        if marker in lowered:
            return LicenceInfo(
                key="not-permitted", name=text[:120],
                url=text if text.startswith("http") else "",
                commercial_use=False, modification=False, redistribution=False,
                attribution_required=False, is_licence_grant=False,
                conditions=[_reason],
            )
    if _CC0_RE.search(text):
        return CATALOGUE["cc0-1.0"]
    if _PDM_RE.search(text):
        return CATALOGUE["public-domain-mark"]

    match = _CC_URL_RE.search(text) or _CC_TEXT_RE.search(text)
    if match:
        variant = _normalise_variant(match.group(1))
        version = match.group(2) or "4.0"
        key = f"cc-{variant}-{version}"
        if key in CATALOGUE:
            return CATALOGUE[key]
        # A CC licence variant/version we do not catalogue: known
        # family, unknown exact terms — do not guess its permissions.
        return None
    if _PEXELS_RE.search(text):
        return CATALOGUE["pexels-license"]
    if _PIXABAY_RE.search(text):
        return CATALOGUE["pixabay-license"]
    return None


def rejection_reason(info: LicenceInfo) -> str | None:
    """Why this licence forbids the intended use, or None if it does not."""
    if info.key == "not-permitted":
        return info.conditions[0] if info.conditions else "licence does not permit the intended use"
    reasons: list[str] = []
    if not info.commercial_use:
        reasons.append("licence forbids commercial use")
    if not info.modification:
        reasons.append("licence forbids modification / derivatives")
    if not info.redistribution:
        reasons.append("licence forbids redistribution")
    return "; ".join(reasons) if reasons else None
