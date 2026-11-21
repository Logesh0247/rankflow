"""RANKFLOW AI — Video Production Agent, Phase 5: captions (Prompt 08).

Synchronised captions rendered with FFmpeg's ASS filter (libass).
The caption plan comes from the Creative Director: one line per
cue, each with timeline-absolute start/end seconds and text of
at most 40 characters. Production converts those cues to
segment-relative times and writes a real .ass subtitle file —
no caption is invented, and no cue is allowed to run past its
own scene.

Style: DejaVu Sans, 48pt at 1080x1920, white with a black
outline, bottom-centre, raised above the very bottom of the
frame (MarginV 200) so captions stay inside the 10% safe
margins and do not collide with the source lower third.
Emphasis: the rank intro ("Number 5.") is bolded, and the
ranking score phrase is bolded — the important words, not the
whole line.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.errors import ValidationError

MAX_CHARS_PER_LINE = 40
DEFAULT_FONT_SIZE = 48


def _ass_time(seconds: float) -> str:
    total = max(0.0, seconds)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _escape_ass_text(text: str) -> str:
    return (text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}"))


def emphasise_caption(text: str) -> str:
    """Bold the rank intro and the score phrase in one caption line.

    "Number 5. A sharp segment. Score: 70 out of 100." becomes
    "{\\b1}Number 5.{\\b0} A sharp segment. {\\b1}Score: 70 out of
    100.{\\b0}" — the important words stand out, the rest stays
    regular weight.
    """
    escaped = _escape_ass_text(text)
    escaped = re.sub(r"^(Number \d+\.)", r"{\\b1}\1{\\b0}", escaped)
    escaped = re.sub(r"(Score: \d+ out of 100\.)", r"{\\b1}\1{\\b0}", escaped)
    return escaped


def build_ass(caption_cues: list[dict], output_path: str | Path, *,
              font_name: str = "DejaVu Sans",
              font_size: int = DEFAULT_FONT_SIZE,
              time_offset_seconds: float = 0.0,
              margin_v: int = 200) -> Path:
    """Write an .ass subtitle file from caption cues.

    Each cue needs start_seconds, end_seconds and text. Times are
    shifted by `time_offset_seconds` (timeline-absolute minus the
    segment start) so the file is segment-relative. A cue longer
    than 40 characters, or one that ends before it starts, is a
    validation error — captions are never silently truncated.
    `margin_v` is the distance from the bottom edge in pixels
    (default 200, the bottom third); the full-bleed style raises
    it so the narration captions sit above the big bottom
    caption instead of colliding with it.
    """
    output = Path(output_path)
    events: list[str] = []
    for i, cue in enumerate(caption_cues):
        text = str(cue.get("text") or "")
        if len(text) > MAX_CHARS_PER_LINE:
            raise ValidationError(
                f"caption line {i} is {len(text)} characters, over the "
                f"{MAX_CHARS_PER_LINE}-character line limit: {text!r}")
        start = float(cue["start_seconds"]) - time_offset_seconds
        end = float(cue["end_seconds"]) - time_offset_seconds
        if end <= start:
            raise ValidationError(
                f"caption line {i} ends ({end:.2f}s) before it starts "
                f"({start:.2f}s) in segment-relative time")
        if start < 0:
            raise ValidationError(
                f"caption line {i} starts before its segment ({start:.2f}s)")
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
            f"{emphasise_caption(text)}")
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,108,108,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return output


def ass_filter(ass_path: str | Path) -> str:
    """FFmpeg filter string that burns an .ass file into the video."""
    path = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"ass='{path}'"
