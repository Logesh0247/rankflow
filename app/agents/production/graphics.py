"""RANKFLOW AI — Video Production Agent, Phase 6: motion graphics (Prompt 08).

Reusable graphics drawn with FFmpeg drawtext, in one consistent
visual style: white bold text with a black box, inside the 10%
safe margins of the 1080x1920 frame. Text is passed through
text files (drawtext=textfile=...) so titles, source names and
attribution — which come from user topics and provider metadata —
never break the filter string with quotes or colons.

Per countdown clip the graphics are:
* a large "#N" countdown number, top centre, for the whole clip;
* a "No. N of 5" rank label under it;
* the source title / creator as a lower third, above the captions;
* the licence attribution, lower third, for the last 2 seconds of
  the clip when the licence requires it (and in the outro credits
  for every clip).

Transitions: the Creative Director's timeline adds transition
seconds between segments, so each boundary is rendered as an
explicit black segment of that length, with the neighbouring
clips fading into and out of it when their direction is a
cross-dissolve (a "hard cut" clip cuts to black and back
instead). This preserves the EDL total duration exactly — a
true xfade would overlap the segments and shorten the total
below the EDL total, so it is not used, and the manifest says so.
"""
from __future__ import annotations

from pathlib import Path

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
SAFE_MARGIN_X = 108  # 10% of 1080
SAFE_MARGIN_Y = 192  # 10% of 1920


def _escape_drawtext_value(value: str) -> str:
    """Escape a drawtext option value (colons and backslashes)."""
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def write_text_file(directory: str | Path, name: str, text: str) -> Path:
    """Write overlay text to a file for drawtext=textfile=..."""
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def drawtext_filter(text_file: str | Path, *, x: str, y: str, fontsize: int,
                    font_file: str, fontcolor: str = "white",
                    box: bool = True, boxcolor: str = "black@0.6",
                    boxborderw: int = 12, enable: str | None = None) -> str:
    """One drawtext filter using a text file (no inline-text escaping)."""
    parts = [
        f"fontfile={_escape_drawtext_value(font_file)}",
        f"textfile={_escape_drawtext_value(str(text_file))}",
        f"x={x}", f"y={y}", f"fontsize={fontsize}",
        f"fontcolor={fontcolor}",
    ]
    if box:
        parts.append(f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}")
    if enable:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


def fade_filters(duration_seconds: float, transition_seconds: float,
                 *, fade_in: bool, fade_out: bool) -> str:
    """Fade-through-black filters for one segment.

    fade_in: fade from black over `transition_seconds` at the start.
    fade_out: fade to black over `transition_seconds` at the end.
    Returns "" when neither applies.
    """
    filters: list[str] = []
    if fade_in and transition_seconds > 0:
        filters.append(f"fade=t=in:st=0:d={transition_seconds:.2f}")
    if fade_out and transition_seconds > 0:
        start = max(0.0, duration_seconds - transition_seconds)
        filters.append(f"fade=t=out:st={start:.2f}:d={transition_seconds:.2f}")
    return ",".join(filters)


def clip_graphics_filters(*, rank: int, source_label: str, attribution_text: str | None,
                          attribution_required: bool, duration_seconds: float,
                          transition_seconds: float, transition_in: str,
                          work_dir: str | Path, font_file: str,
                          score_note: str | None = None) -> tuple[str, list[str]]:
    """drawtext + fade filters for one countdown clip segment.

    Returns (filter_string, [text files written]). All overlays
    stay inside the safe margins. The attribution is shown only
    in the last 2 seconds of the segment, when required.
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = []
    number_file = write_text_file(work, f"rank-{rank}-number.txt", f"#{rank}")
    files.append(str(number_file))
    filters.append(drawtext_filter(
        number_file, x="(w-text_w)/2", y="220", fontsize=170,
        font_file=font_file))
    label_file = write_text_file(work, f"rank-{rank}-label.txt", f"No. {rank} of 5")
    files.append(str(label_file))
    filters.append(drawtext_filter(
        label_file, x="(w-text_w)/2", y="420", fontsize=64,
        font_file=font_file))
    if score_note:
        score_file = write_text_file(work, f"rank-{rank}-score.txt", score_note)
        files.append(str(score_file))
        filters.append(drawtext_filter(
            score_file, x="(w-text_w)/2", y="500", fontsize=36,
            font_file=font_file, fontcolor="white@0.85"))
    source_file = write_text_file(work, f"rank-{rank}-source.txt", source_label)
    files.append(str(source_file))
    # Lower third, above the caption area (captions sit at the very
    # bottom, ~150px from the edge).
    filters.append(drawtext_filter(
        source_file, x="(w-text_w)/2", y="h-360", fontsize=40,
        font_file=font_file))
    if attribution_required and attribution_text:
        attr_file = write_text_file(work, f"rank-{rank}-attr.txt", attribution_text)
        files.append(str(attr_file))
        show_from = max(0.0, duration_seconds - 2.0)
        filters.append(drawtext_filter(
            attr_file, x="(w-text_w)/2", y="h-460", fontsize=32,
            font_file=font_file, fontcolor="white@0.9",
            enable=f"between(t,{show_from:.2f},{duration_seconds:.2f})"))
    is_dissolve = "cross-dissolve" in (transition_in or "")
    fade = fade_filters(duration_seconds, transition_seconds,
                        fade_in=is_dissolve, fade_out=is_dissolve)
    if fade:
        filters.append(fade)
    return ",".join(filters), files


def card_graphics_filters(*, lines: list[str], work_dir: str | Path,
                          font_file: str, fade_in: bool = True,
                          fade_out: bool = False,
                          duration_seconds: float = 3.0,
                          transition_seconds: float = 0.5,
                          sizes: list[int] | None = None) -> tuple[str, list[str]]:
    """Centred title/outro card text, one line per drawtext.

    Lines are centred vertically around the middle of the frame.
    `sizes` gives the font size per line (default: 96 for the
    first line, 52 for the rest).
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = []
    sizes = list(sizes) if sizes else [96 if i == 0 else 52 for i in range(len(lines))]
    total_height = sum(sizes) + 30 * max(0, len(lines) - 1)
    y = (TARGET_HEIGHT - total_height) // 2
    for i, (line, size) in enumerate(zip(lines, sizes)):
        text_file = write_text_file(work, f"card-line-{i}.txt", line)
        files.append(str(text_file))
        filters.append(drawtext_filter(
            text_file, x="(w-text_w)/2", y=str(y), fontsize=size,
            font_file=font_file))
        y += size + 30
    fade = fade_filters(duration_seconds, transition_seconds,
                        fade_in=fade_in, fade_out=fade_out)
    if fade:
        filters.append(fade)
    return ",".join(filters), files
