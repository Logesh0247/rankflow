"""Momentrix AI — Video Production Agent, Phase 6: motion graphics (Prompt 08).

Full-bleed style (restyle 2026-11-21, replacing the Rankter style):
the render looks like the reference parkour ranking videos — a
vertical 1080x1920 Short where the clip fills the whole frame
edge to edge, with the title, the numbered list and the captions
drawn on top of the footage. There is no dark background, no
separate video zone, no intro/outro card and no black gap between
clips: the five clips are cut straight together.

Layout, measured from the reference frames (1080x1920):

* Top title bar (y 0..200, solid black): the title in two lines,
  bold stroked text. Line 1 is "RANKING" in white plus the
  topic's qualifier phrase in red (#ff2d2d, e.g. "CRAZIEST");
  line 2 is the topic's subject words in yellow (#ffd400, e.g.
  "PARKOUR FAILS"). The title comes from the job topic, split by
  rule — never hardcoded to parkour.
* Left-side numbered list, overlaid on the video: numbers "1."
  to "5." down the left edge (120px, 10px black stroke, x=36,
  first at y=260, 220px apart). The number of the clip currently
  playing is yellow; the rest are white. A clip's short caption
  sits next to its number (48px, white with a black outline);
  captions appear once their clip has started playing and stay
  visible, so the list fills in as the countdown runs #5 -> #1.
* Bottom caption: the clip's caption from the pipeline,
  uppercased, big bold white text with an 8px black stroke,
  centred across the bottom (y ~1660, wrapped to at most two
  lines). The narration captions (ASS) are rendered above it,
  raised so the two never collide.
* Source label: small, top-right just under the title bar.
* Licence attribution: when the licence requires it, a small
  line in the lower third for the last 2 seconds of the clip.
  The full attribution for every clip is also recorded in the
  render manifest and the report's description_attribution, for
  pasting into the video description — there is no end-credits
  screen in the video itself.

Text is measured with Pillow against the same DejaVu Sans Bold
font FFmpeg draws with, so titles wrap and centre exactly, and
labels are truncated to fit — no text ever runs off the frame.
Overlay text is passed through text files
(drawtext=textfile=...) so titles, source names and attribution
— which come from user topics and provider metadata — never break
the filter string with quotes or colons.

What the text is (no invented wording):
* Title: the job topic, split into qualifier phrase (red) and
  subject words (yellow) by rule; "RANKING" is always white.
* List captions: the Creative Director's per-moment caption
  (the first eight words of the moment narration), with the
  leading "Number N." prefix and any trailing score fragment
  stripped — the "#N" in the list already carries the rank.
* Bottom caption: the same per-moment caption, uppercased.
* Source label and licence attribution: as before, from the
  visual direction.

Transitions: straight cuts. No fade, no black gap segment — the
segments are concatenated directly, so the total duration is
exactly the sum of the five clip durations.
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

# --- Full-bleed style ----------------------------------------------------
FULLBLEED_WHITE = "white"
FULLBLEED_RED = "0xff2d2d"       # qualifier phrase, e.g. CRAZIEST
FULLBLEED_YELLOW = "0xffd400"    # subject words / active rank
FULLBLEED_TITLE_BAR_HEIGHT = 200
FULLBLEED_TITLE_SIZES = (84, 72, 56)
FULLBLEED_TITLE_STROKE = 8
FULLBLEED_TITLE_MAX_WIDTH = 952  # 1080 - 2 * 64px side margin
FULLBLEED_TITLE_LINE_HEIGHT_FACTOR = 1.25
FULLBLEED_NUMBERS_FONT_SIZE = 120
FULLBLEED_NUMBERS_STROKE = 10
FULLBLEED_NUMBERS_X = 36
FULLBLEED_NUMBERS_Y = 260        # first number, just under the title bar
FULLBLEED_NUMBERS_SPACING = 220  # 120px glyph + 100px gap
FULLBLEED_CAPTION_FONT_SIZE = 48
FULLBLEED_CAPTION_STROKE = 4
FULLBLEED_CAPTION_X = 300        # caption sits right of the number
FULLBLEED_CAPTION_MAX_WIDTH = 740  # 300..1040
FULLBLEED_BOTTOM_FONT_SIZE = 64
FULLBLEED_BOTTOM_STROKE = 8
FULLBLEED_BOTTOM_MAX_WIDTH = 952
FULLBLEED_BOTTOM_Y = 1660        # single line; two lines start at 1620
FULLBLEED_BOTTOM_MAX_LINES = 2
FULLBLEED_SOURCE_FONT_SIZE = 30
FULLBLEED_SOURCE_Y = 212         # just under the title bar, right-aligned
FULLBLEED_ATTRIBUTION_FONT_SIZE = 28
FULLBLEED_ATTRIBUTION_Y = 1420

# Words in a topic that are a qualifier (the red phrase), not the
# subject: "CRAZIEST" in "TOP 5 CRAZIEST PARKOUR FAILS". "THE" and
# "MOST" are connectors inside a qualifier phrase ("THE CRAZIEST",
# "MOST INSANE") and are coloured red with it.
TITLE_QUALIFIERS = {
    "the", "most", "craziest", "insane", "funniest", "funny", "best",
    "worst", "epic", "hilarious", "amazing", "greatest", "shocking",
    "wild", "crazy", "scariest", "weirdest", "coolest", "cutest",
    "saddest", "fastest", "biggest",
}
# Framing words: never part of either title line. "ranking" is
# framing too — line 1 always starts with "RANKING", so a topic
# that already contains it ("Ranking The Craziest Parkour
# Fails") must not repeat it in the subject line.
TITLE_FRAMING = {"top", "5", "five", "#5", "of", "ranking"}

_NUMBER_PREFIX = re.compile(r"^number\s+\d+\.\s*", re.IGNORECASE)
_SCORE_PHRASE = re.compile(r"\s*score:\s*\d+(?:\.\d+)?\s+out of 100\.?\s*$", re.IGNORECASE)
_SCORE_NOTE = re.compile(r"\s*score\s+\d+(?:\.\d+)?/100\.?\s*$", re.IGNORECASE)


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
    (full-bleed titles use an 8px black stroke, numbers 10px,
    no box).
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


def truncate_to_width(text: str, font_file: str, fontsize: int,
                      max_width: float) -> str:
    """Truncate `text` with an ellipsis so it fits `max_width` pixels."""
    if measure_text(text, font_file, fontsize) <= max_width:
        return text
    trimmed = text.rstrip()
    while trimmed and measure_text(trimmed + "…", font_file, fontsize) > max_width:
        trimmed = trimmed[:-1].rstrip()
    return trimmed + "…" if trimmed else "…"


def _clean_caption_text(text: str, rank: int) -> str:
    """The per-moment caption, cleaned for on-screen use.

    Strips the leading "Number N." (the "N." in the list already
    carries the rank) and any trailing score fragment ("Score: 70
    out of 100." / "Score 80/100"), keeps the first sentence.
    Falls back to "Moment #N" when nothing usable is left.
    """
    cleaned = _NUMBER_PREFIX.sub("", str(text or "").strip())
    cleaned = _SCORE_PHRASE.sub("", cleaned)
    cleaned = _SCORE_NOTE.sub("", cleaned)
    first_sentence = cleaned.split(". ")[0].strip()
    if first_sentence and not first_sentence.endswith("."):
        first_sentence += "."
    if not first_sentence:
        return f"Moment #{rank}"
    return first_sentence


def clean_clip_caption(caption_text: str, rank: int) -> str:
    """Short caption for one moment, from the pipeline's own text.

    `caption_text` is the Creative Director's per-moment caption
    (the first eight words of the moment narration). No word is
    invented: every word comes from that caption.
    """
    return _clean_caption_text(caption_text, rank)


# --- Full-bleed title ------------------------------------------------------
def split_fullbleed_title(topic: str) -> tuple[list[str], list[str]]:
    """Split a topic into the two title-bar lines.

    Returns (line1_words, line2_words), uppercased. Line 1 is
    "RANKING" plus the topic's qualifier words (at most three,
    in topic order); line 2 is the remaining subject words.

    "TOP 5 CRAZIEST PARKOUR FAILS" -> (["RANKING", "CRAZIEST"],
    ["PARKOUR", "FAILS"]); "TOP 5 CAT FUNNY MOMENTS" ->
    (["RANKING", "FUNNY"], ["CAT", "MOMENTS"]); a topic with no
    qualifier -> (["RANKING"], <all subject words>).
    """
    words: list[str] = []
    for raw in str(topic or "").upper().split():
        word = raw.strip(".,!?:;—\"'()[]")
        if word and word.lower() not in TITLE_FRAMING:
            words.append(word)
    qualifiers = [w for w in words if w.lower() in TITLE_QUALIFIERS][:3]
    subject = [w for w in words if w.lower() not in TITLE_QUALIFIERS]
    return ["RANKING"] + qualifiers, subject


def _line_width(words: list[str], font_file: str, fontsize: int) -> float:
    if not words:
        return 0.0
    space_w = measure_text(" ", font_file, fontsize)
    return (sum(measure_text(w, font_file, fontsize) for w in words)
            + space_w * (len(words) - 1))


def fullbleed_title_filters(topic: str, *, work_dir: str | Path,
                            font_file: str, tag: str = "title"
                            ) -> tuple[str, list[str]]:
    """Full-bleed title bar text: two centred lines over the bar.

    Line 1: "RANKING" white + qualifier words red; line 2:
    subject words yellow. The largest size (84, then 72, then
    56) whose lines both fit the frame wins. Each word is its
    own drawtext with a computed x so the colours land on
    exactly the right words and every line stays centred.
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = []
    line1, line2 = split_fullbleed_title(topic)
    lines = [(line1, [FULLBLEED_WHITE] + [FULLBLEED_RED] * (len(line1) - 1)),
             (line2, [FULLBLEED_YELLOW] * len(line2))]
    lines = [(words, colours) for words, colours in lines if words]
    fontsize = FULLBLEED_TITLE_SIZES[-1]
    for size in FULLBLEED_TITLE_SIZES:
        fontsize = size
        if all(_line_width(words, font_file, size) <= FULLBLEED_TITLE_MAX_WIDTH
               for words, _ in lines):
            break
    line_height = int(fontsize * FULLBLEED_TITLE_LINE_HEIGHT_FACTOR)
    cursor_y = (FULLBLEED_TITLE_BAR_HEIGHT - line_height * len(lines)) // 2
    for line_index, (words, colours) in enumerate(lines):
        widths = [measure_text(w, font_file, fontsize) for w in words]
        space_w = measure_text(" ", font_file, fontsize)
        line_w = sum(widths) + space_w * max(0, len(words) - 1)
        cursor_x = (TARGET_WIDTH - line_w) / 2
        for word_index, (word, word_w, colour) in enumerate(
                zip(words, widths, colours)):
            text_file = write_text_file(
                work, f"{tag}-word-{line_index}-{word_index}.txt", word)
            files.append(str(text_file))
            filters.append(drawtext_filter(
                text_file, x=f"{cursor_x:.0f}", y=str(cursor_y),
                fontsize=fontsize, font_file=font_file,
                fontcolor=colour, box=False,
                borderw=FULLBLEED_TITLE_STROKE, bordercolor="black"))
            cursor_x += word_w + space_w
        cursor_y += line_height
    return ",".join(filters), files


def fullbleed_title_bar_filter() -> str:
    """Solid black bar across the top of the frame (y 0..200)."""
    return (f"drawbox=x=0:y=0:w={TARGET_WIDTH}:h={FULLBLEED_TITLE_BAR_HEIGHT}"
            f":color=black:t=fill")


# --- Full-bleed numbered list ----------------------------------------------
def fullbleed_numbers_filters(items: list[tuple[int, str]], active_rank: int | None,
                              *, work_dir: str | Path, font_file: str,
                              tag: str = "list"
                              ) -> tuple[str, list[str]]:
    """Persistent numbered list down the left edge: "1." … "5.".

    `items` are (rank, caption) — displayed 1 first, as in the
    reference videos. The active rank's number is yellow
    #ffd400; the rest are white. A rank's caption is shown next
    to its number once that clip has started playing (in
    countdown order #5 -> #1, ranks >= the active rank have
    played or are playing); `active_rank=None` shows every
    caption. Captions are truncated to fit the frame width.
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = []
    ordered = sorted(items, key=lambda item: item[0])
    for rank, caption in ordered:
        y = FULLBLEED_NUMBERS_Y + (rank - 1) * FULLBLEED_NUMBERS_SPACING
        number_file = write_text_file(work, f"{tag}-{rank}.txt", f"{rank}.")
        files.append(str(number_file))
        filters.append(drawtext_filter(
            number_file, x=str(FULLBLEED_NUMBERS_X), y=str(y),
            fontsize=FULLBLEED_NUMBERS_FONT_SIZE, font_file=font_file,
            fontcolor=FULLBLEED_YELLOW if rank == active_rank else "white",
            box=False, borderw=FULLBLEED_NUMBERS_STROKE, bordercolor="black"))
        show_caption = (active_rank is None or rank >= active_rank)
        if show_caption and caption:
            label = truncate_to_width(
                caption, font_file, FULLBLEED_CAPTION_FONT_SIZE,
                FULLBLEED_CAPTION_MAX_WIDTH)
            caption_file = write_text_file(work, f"{tag}-{rank}-cap.txt", label)
            files.append(str(caption_file))
            filters.append(drawtext_filter(
                caption_file, x=str(FULLBLEED_CAPTION_X),
                y=str(y + 45), fontsize=FULLBLEED_CAPTION_FONT_SIZE,
                font_file=font_file, fontcolor="white",
                box=False, borderw=FULLBLEED_CAPTION_STROKE,
                bordercolor="black"))
    return ",".join(filters), files


# --- Full-bleed bottom caption ---------------------------------------------
def wrap_bottom_caption(text: str, font_file: str, *,
                        fontsize: int = FULLBLEED_BOTTOM_FONT_SIZE,
                        max_width: int = FULLBLEED_BOTTOM_MAX_WIDTH
                        ) -> list[str]:
    """Word-wrap the uppercased bottom caption into at most two lines.

    Words are dropped from the end (with an ellipsis) only when
    the text cannot fit in two lines at all.
    """
    words = str(text or "").upper().split()
    if not words:
        return []

    def _wrap(candidate: list[str]) -> list[str]:
        space_w = measure_text(" ", font_file, fontsize)
        lines: list[str] = []
        current: list[str] = []
        current_w = 0.0
        for word in candidate:
            word_w = measure_text(word, font_file, fontsize)
            add = word_w if not current else word_w + space_w
            if current and current_w + add > max_width:
                lines.append(" ".join(current))
                current = [word]
                current_w = word_w
            else:
                current.append(word)
                current_w += add
        if current:
            lines.append(" ".join(current))
        return lines

    lines = _wrap(words)
    if len(lines) <= FULLBLEED_BOTTOM_MAX_LINES:
        return lines
    kept = list(words)
    while len(kept) > 1 and len(_wrap(kept)) > FULLBLEED_BOTTOM_MAX_LINES:
        kept.pop()
    lines = _wrap(kept)
    if lines:
        last = lines[-1]
        if measure_text(last + "…", font_file, fontsize) <= max_width:
            lines[-1] = last + "…"
        else:
            lines[-1] = truncate_to_width(last, font_file, fontsize, max_width)
    return lines[:FULLBLEED_BOTTOM_MAX_LINES]


def fullbleed_bottom_caption_filters(caption: str, *, work_dir: str | Path,
                                     font_file: str, tag: str = "bottom"
                                     ) -> tuple[str, list[str]]:
    """Big bold uppercased bottom caption, centred, stroked black."""
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = []
    lines = wrap_bottom_caption(caption, font_file)
    if not lines:
        return "", files
    line_height = int(FULLBLEED_BOTTOM_FONT_SIZE * 1.25)
    start_y = FULLBLEED_BOTTOM_Y if len(lines) == 1 else FULLBLEED_BOTTOM_Y - 40
    for line_index, line in enumerate(lines):
        text_file = write_text_file(work, f"{tag}-line-{line_index}.txt", line)
        files.append(str(text_file))
        filters.append(drawtext_filter(
            text_file, x="(w-text_w)/2", y=str(start_y + line_index * line_height),
            fontsize=FULLBLEED_BOTTOM_FONT_SIZE, font_file=font_file,
            fontcolor="white", box=False,
            borderw=FULLBLEED_BOTTOM_STROKE, bordercolor="black"))
    return ",".join(filters), files


# --- Per-clip graphics ------------------------------------------------------
def clip_graphics_filters(*, rank: int, topic: str,
                          list_items: list[tuple[int, str]],
                          source_label: str, attribution_text: str | None,
                          attribution_required: bool, duration_seconds: float,
                          work_dir: str | Path, font_file: str,
                          bottom_caption: str | None = None) -> tuple[str, list[str]]:
    """Full-bleed graphics for one countdown clip segment.

    Returns (filter_string, [text files written]). The filter
    string starts with the solid black title bar, then the
    two-line title, the persistent left-side numbers (this clip's
    rank highlighted yellow, with the captions of the clips that
    have played so far), the small source label under the title
    bar, the licence attribution in the lower third for the last
    2 seconds of the clip when the licence requires it, and the
    big bottom caption. No pad (the clip already fills the
    1080x1920 frame), no fade, no black gap — straight cuts.
    `bottom_caption` defaults to this clip's list caption.
    """
    work = Path(work_dir)
    files: list[str] = []
    filters: list[str] = [fullbleed_title_bar_filter()]
    title_filters, title_files = fullbleed_title_filters(
        topic, work_dir=work, font_file=font_file, tag=f"clip-{rank}-title")
    files += title_files
    if title_filters:
        filters.append(title_filters)
    list_filters, list_files = fullbleed_numbers_filters(
        list_items, rank, work_dir=work, font_file=font_file,
        tag=f"clip-{rank}-list")
    files += list_files
    if list_filters:
        filters.append(list_filters)
    source_file = write_text_file(work, f"rank-{rank}-source.txt", source_label)
    files.append(str(source_file))
    source_w = measure_text(source_label, font_file, FULLBLEED_SOURCE_FONT_SIZE)
    filters.append(drawtext_filter(
        source_file, x=str(TARGET_WIDTH - 36 - int(source_w)),
        y=str(FULLBLEED_SOURCE_Y), fontsize=FULLBLEED_SOURCE_FONT_SIZE,
        font_file=font_file, fontcolor="white@0.85", box=False))
    if attribution_required and attribution_text:
        attr_file = write_text_file(work, f"rank-{rank}-attr.txt", attribution_text)
        files.append(str(attr_file))
        show_from = max(0.0, duration_seconds - 2.0)
        filters.append(drawtext_filter(
            attr_file, x="(w-text_w)/2", y=str(FULLBLEED_ATTRIBUTION_Y),
            fontsize=FULLBLEED_ATTRIBUTION_FONT_SIZE,
            font_file=font_file, fontcolor="white@0.9", box=False,
            enable=f"between(t,{show_from:.2f},{duration_seconds:.2f})"))
    caption = bottom_caption
    if caption is None:
        caption = dict(list_items).get(rank) or ""
    bottom_filters, bottom_files = fullbleed_bottom_caption_filters(
        caption, work_dir=work, font_file=font_file, tag=f"clip-{rank}-bottom")
    files += bottom_files
    if bottom_filters:
        filters.append(bottom_filters)
    return ",".join(filters), files
