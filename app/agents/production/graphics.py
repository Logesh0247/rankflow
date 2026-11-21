"""Momentrix AI — Video Production Agent, Phase 6: motion graphics (Prompt 08).

Rankter-style graphics (restyle 2026-11-21): the render looks like
a rankter.app Top 5 video. The 1080x1920 frame is split into two
zones:

* Top zone (y 0..730, dark background #0f1016): the title in big
  bold stroked white text (~96px, 8px black stroke) with the
  topic's key word(s) highlighted yellow #ffd400, and below it the
  ranked list of all 5 moments (~40px, starting ~24% from the
  top, 10px item spacing). The list stays visible while its clip
  plays; the item currently playing is highlighted yellow, the
  others are white.
* Video zone (y 730..1920, the lower ~62% of the frame): the clip
  itself, reframed to 1080x1190 with the same motion-centroid
  subject-aware crop as before, padded into the full frame.

Text is measured with Pillow against the same DejaVu Sans Bold
font FFmpeg draws with, so titles wrap and centre exactly, and
list labels are truncated to fit — no text ever runs off the
frame. Overlay text is passed through text files
(drawtext=textfile=...) so titles, source names and attribution
— which come from user topics and provider metadata — never break
the filter string with quotes or colons.

What the text is (no invented wording):
* Title: the job topic, uppercased, word-wrapped. Highlighted
  words are the topic's content words (stopwords such as "top",
  "5", "best", "moments" are never highlighted).
* List labels: the Creative Director's per-moment narration text
  (signal-grounded, never a description of what happens in the
  clip), with the leading "Number N." prefix stripped (the "#N"
  in the list already carries the rank) and the narration's own
  score appended ("Score 80/100"), truncated to fit the line.
* Source label and licence attribution: as before, from the
  visual direction; attribution in the lower third for the last
  2 seconds of the clip when the licence requires it, and in the
  outro credits for every clip.

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

import re
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
SAFE_MARGIN_X = 108  # 10% of 1080
SAFE_MARGIN_Y = 192  # 10% of 1920

# --- Rankter style -------------------------------------------------------
RANKTER_BG = "0x0f1016"          # dark background behind title/list
RANKTER_HIGHLIGHT = "0xffd400"   # yellow #ffd400 for keywords / active item
RANKTER_TITLE_FONT_SIZE = 96
RANKTER_TITLE_STROKE = 8
RANKTER_TITLE_Y = 48
RANKTER_TITLE_MAX_WIDTH = 952    # 1080 - 2 * 64px side margin
RANKTER_TITLE_MAX_LINES = 2
# Sizes tried in order: the largest that wraps the topic into at
# most RANKTER_TITLE_MAX_LINES lines wins.
RANKTER_TITLE_SIZES = (96, 72, 56)
RANKTER_LIST_FONT_SIZE = 40
RANKTER_LIST_Y = 460              # ~24% of 1920
RANKTER_LIST_LINE_HEIGHT = 50     # 40px text + 10px spacing
RANKTER_LIST_MAX_WIDTH = 952
RANKTER_VIDEO_AREA_Y = 730        # video starts here: lower ~62% of frame
RANKTER_VIDEO_AREA_HEIGHT = 1190  # 1920 - 730
RANKTER_MARGIN_X = 64

# Words in a topic that are framing, not subject: never highlighted.
TITLE_STOPWORDS = {
    "top", "5", "five", "best", "most", "funniest", "funny", "craziest",
    "insane", "epic", "hilarious", "amazing", "greatest", "worst",
    "moments", "moment", "videos", "video", "clips", "clip", "ranking",
    "ranked", "countdown", "of", "the", "and", "a", "an", "in", "on",
    "ever", "all", "time", "ever", "world", "worlds",
}

_NUMBER_PREFIX = re.compile(r"^number\s+\d+\.\s*", re.IGNORECASE)
_SCORE_PHRASE = re.compile(r"score:\s*(\d+)\s+out of 100", re.IGNORECASE)
_SCORE_NOTE = re.compile(r"score\s+(\d+(?:\.\d+)?)/100", re.IGNORECASE)


@lru_cache(maxsize=8)
def _pil_font(font_file: str, fontsize: int) -> ImageFont.FreeTypeFont:
    """Pillow font for measuring text with the same font FFmpeg draws."""
    return ImageFont.truetype(font_file, fontsize)


def measure_text(text: str, font_file: str, fontsize: int) -> float:
    """Width of `text` in pixels at `fontsize` in `font_file`."""
    return _pil_font(font_file, fontsize).getlength(text)


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
                    boxborderw: int = 12, borderw: int = 0,
                    bordercolor: str = "black",
                    enable: str | None = None) -> str:
    """One drawtext filter using a text file (no inline-text escaping).

    `borderw` draws a stroke of that width around the glyphs
    (Rankter-style titles use an 8px black stroke, no box).
    """
    parts = [
        f"fontfile={_escape_drawtext_value(font_file)}",
        f"textfile={_escape_drawtext_value(str(text_file))}",
        f"x={x}", f"y={y}", f"fontsize={fontsize}",
        f"fontcolor={fontcolor}",
    ]
    if box:
        parts.append(f"box=1:boxcolor={boxcolor}:boxborderw={boxborderw}")
    if borderw > 0:
        parts.append(f"borderw={borderw}:bordercolor={bordercolor}")
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


# --- Rankter title --------------------------------------------------------
def rankter_highlight_words(topic: str) -> set[str]:
    """Content words of the topic (uppercased), minus framing stopwords.

    "top 5 cat funny moments" -> {"CAT"}; "TOP 5 CRAZIEST PARKOUR
    FAILS" -> {"PARKOUR", "FAILS"}. Returns an empty set when the
    topic has no content word left — then nothing is highlighted.
    """
    words = set()
    for raw in str(topic or "").upper().split():
        word = raw.strip(".,!?:;—\"'()[]")
        if word and word.lower() not in TITLE_STOPWORDS:
            words.add(word)
    return words


def wrap_rankter_title(topic: str, font_file: str, *,
                       fontsize: int = RANKTER_TITLE_FONT_SIZE,
                       max_width: int = RANKTER_TITLE_MAX_WIDTH
                       ) -> list[list[tuple[str, bool]]]:
    """Word-wrap the uppercased topic into lines that fit `max_width`.

    Returns lines of (word, highlighted) pairs, measured with the
    real font so a line never overflows the frame.
    """
    keywords = rankter_highlight_words(topic)
    font = _pil_font(font_file, fontsize)
    space_w = font.getlength(" ")
    lines: list[list[tuple[str, bool]]] = []
    current: list[tuple[str, bool]] = []
    current_w = 0.0
    for raw in str(topic or "").upper().split():
        word = raw.strip()
        if not word:
            continue
        word_w = font.getlength(word)
        add = word_w if not current else word_w + space_w
        if current and current_w + add > max_width:
            lines.append(current)
            current = [(word, word.strip(".,!?:;—\"'()[]") in keywords)]
            current_w = word_w
        else:
            current.append((word, word.strip(".,!?:;—\"'()[]") in keywords))
            current_w += add
    if current:
        lines.append(current)
    return lines


def rankter_title_filters(topic: str, *, work_dir: str | Path,
                          font_file: str, tag: str = "title",
                          y: int = RANKTER_TITLE_Y
                          ) -> tuple[str, list[str]]:
    """Rankter-style title: big bold stroked white text, keywords yellow.

    The title is drawn word by word (each word its own drawtext
    with a computed x) so the yellow highlight lands on exactly
    the keyword words and every line stays centred. The largest
    size that wraps the topic into at most RANKTER_TITLE_MAX_LINES
    lines wins (96, then 72, then 56).
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = []
    fontsize = RANKTER_TITLE_SIZES[-1]
    lines: list[list[tuple[str, bool]]] = []
    for size in RANKTER_TITLE_SIZES:
        lines = wrap_rankter_title(topic, font_file, fontsize=size)
        fontsize = size
        if len(lines) <= RANKTER_TITLE_MAX_LINES:
            break
    line_height = int(fontsize * 1.25)
    cursor_y = y
    for line_index, line in enumerate(lines):
        widths = [measure_text(word, font_file, fontsize) for word, _ in line]
        space_w = measure_text(" ", font_file, fontsize)
        line_w = sum(widths) + space_w * max(0, len(line) - 1)
        cursor_x = (TARGET_WIDTH - line_w) / 2
        for word_index, ((word, highlighted), word_w) in enumerate(zip(line, widths)):
            text_file = write_text_file(
                work, f"{tag}-word-{line_index}-{word_index}.txt", word)
            files.append(str(text_file))
            filters.append(drawtext_filter(
                text_file, x=f"{cursor_x:.0f}", y=str(cursor_y),
                fontsize=fontsize, font_file=font_file,
                fontcolor=RANKTER_HIGHLIGHT if highlighted else "white",
                box=False, borderw=RANKTER_TITLE_STROKE, bordercolor="black"))
            cursor_x += word_w + space_w
        cursor_y += line_height
    return ",".join(filters), files


# --- Rankter ranked list ---------------------------------------------------
def rankter_list_label(narration_text: str, rank: int,
                       score_note: str | None = None) -> str:
    """List label for one moment, from the Creative Director's own text.

    Takes the moment's narration, strips the leading "Number N."
    (the "#N" in the list already carries the rank), keeps the
    first sentence, and appends the narration's own score
    ("Score 80/100"). No word is invented: every word comes from
    the narration text or the score note the Creative Director
    generated. Falls back to "Moment #N" when the narration has no
    usable sentence (never happens with a real plan — the rank
    intro is always kept — but the function must not crash).
    """
    text = str(narration_text or "").strip()
    text = _NUMBER_PREFIX.sub("", text)
    first_sentence = text.split(". ")[0].strip()
    if first_sentence and not first_sentence.endswith("."):
        first_sentence += "."
    score: str | None = None
    match = _SCORE_PHRASE.search(text)
    if match:
        score = match.group(1)
    elif score_note:
        note = _SCORE_NOTE.search(score_note)
        if note:
            score = str(int(float(note.group(1))))
    if not first_sentence:
        return f"Moment #{rank}"
    if score:
        return f"{first_sentence} Score {score}/100"
    return first_sentence


def truncate_to_width(text: str, font_file: str, fontsize: int,
                      max_width: float) -> str:
    """Truncate `text` with an ellipsis so it fits `max_width` pixels."""
    if measure_text(text, font_file, fontsize) <= max_width:
        return text
    trimmed = text.rstrip()
    while trimmed and measure_text(trimmed + "…", font_file, fontsize) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return trimmed + "…" if trimmed else "…"


def rankter_list_filters(items: list[tuple[int, str]], active_rank: int | None,
                         *, work_dir: str | Path, font_file: str,
                         tag: str = "list",
                         y: int = RANKTER_LIST_Y
                         ) -> tuple[str, list[str]]:
    """Persistent ranked list: "#5 label" … "#1 label", one per line.

    `items` are (rank, label) in display order (#5 first, matching
    the countdown). The active rank's whole line is yellow
    #ffd400; the rest are white. `active_rank=None` (intro card)
    leaves every line white. Labels are truncated to fit the
    frame width.
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = []
    for index, (rank, label) in enumerate(items):
        number_w = measure_text(f"#{rank}  ", font_file, RANKTER_LIST_FONT_SIZE)
        label = truncate_to_width(
            label, font_file, RANKTER_LIST_FONT_SIZE,
            RANKTER_LIST_MAX_WIDTH - number_w)
        line = f"#{rank}  {label}"
        text_file = write_text_file(work, f"{tag}-item-{index}.txt", line)
        files.append(str(text_file))
        filters.append(drawtext_filter(
            text_file, x=str(RANKTER_MARGIN_X), y=str(y + index * RANKTER_LIST_LINE_HEIGHT),
            fontsize=RANKTER_LIST_FONT_SIZE, font_file=font_file,
            fontcolor=RANKTER_HIGHLIGHT if rank == active_rank else "white",
            box=False))
    return ",".join(filters), files


def rankter_frame_filter() -> str:
    """Pad the 1080x1190 reframed clip into the 1080x1920 Rankter frame.

    The clip sits in the lower ~62% of the frame; the top zone is
    the dark #0f1016 background the title and list are drawn on.
    Must be the first filter applied to a clip segment (before
    any drawtext), so the overlays land on the padded frame.
    """
    return (f"pad={TARGET_WIDTH}:{TARGET_HEIGHT}:0:{RANKTER_VIDEO_AREA_Y}"
            f":color={RANKTER_BG}")


def clip_graphics_filters(*, rank: int, topic: str,
                          list_items: list[tuple[int, str]],
                          source_label: str, attribution_text: str | None,
                          attribution_required: bool, duration_seconds: float,
                          transition_seconds: float, transition_in: str,
                          work_dir: str | Path, font_file: str,
                          score_note: str | None = None) -> tuple[str, list[str]]:
    """Rankter-style graphics for one countdown clip segment.

    Returns (filter_string, [text files written]). The filter
    string starts with the frame pad (clip into the lower 62% of
    the frame), then the Rankter title and the persistent ranked
    list (this clip's rank highlighted), then the source label and
    licence attribution in the lower third of the video area, then
    the fade-through-black filters. `score_note` is accepted for
    call compatibility and ignored — the score now lives in the
    ranked-list label, taken from the Creative Director's own
    narration/score note.
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = [rankter_frame_filter()]
    title_filters, title_files = rankter_title_filters(
        topic, work_dir=work, font_file=font_file, tag=f"clip-{rank}-title")
    files += title_files
    if title_filters:
        filters.append(title_filters)
    list_filters, list_files = rankter_list_filters(
        list_items, rank, work_dir=work, font_file=font_file,
        tag=f"clip-{rank}-list")
    files += list_files
    if list_filters:
        filters.append(list_filters)
    source_file = write_text_file(work, f"rank-{rank}-source.txt", source_label)
    files.append(str(source_file))
    # Lower third of the video area, above the caption area
    # (captions sit at the very bottom, ~150px from the edge).
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
    first line, 52 for the rest). Used for the outro credits card.
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
