"""Rankter-style graphics tests (restyle 2026-11-21): title keyword
highlight, word wrapping, ranked list, list labels, frame layout.

Pure-function tests only — no engine, no rendering. The render
tests (frame pixels, full service render, QA pass) live in
tests/test_production.py, which has the footage/EDL fixtures.
"""
import pytest

from app.agents.production.graphics import (
    RANKTER_HIGHLIGHT, RANKTER_LIST_FONT_SIZE, RANKTER_LIST_MAX_WIDTH,
    RANKTER_TITLE_MAX_WIDTH, drawtext_filter, rankter_frame_filter,
    rankter_highlight_words, rankter_list_filters, rankter_list_label,
    rankter_title_filters, truncate_to_width, wrap_rankter_title,
)

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

ITEMS = [
    (5, "A sharp, fast segment. Score 80/100"),
    (4, "A steady segment. Score 75/100"),
    (3, "A dark segment. Score 70/100"),
    (2, "A bright segment. Score 65/100"),
    (1, "A sharp segment. Score 60/100"),
]


def test_highlight_words_are_the_topic_subject():
    assert rankter_highlight_words("top 5 cat funny moments") == {"CAT"}
    assert rankter_highlight_words("TOP 5 CRAZIEST PARKOUR FAILS") == {"PARKOUR", "FAILS"}
    assert rankter_highlight_words("top 5 moments") == set()
    assert rankter_highlight_words("") == set()


def test_title_wrap_fits_the_frame_width():
    for topic in ("top 5 cat funny moments", "TOP 5 CRAZIEST PARKOUR FAILS",
                  "TOP 5 MOST INSANE PARKOUR FAILS COMPILATION EVER MADE"):
        lines = wrap_rankter_title(topic, FONT)
        from app.agents.production.graphics import measure_text
        for line in lines:
            width = sum(measure_text(w, FONT, 96) for w, _ in line)
            width += measure_text(" ", FONT, 96) * max(0, len(line) - 1)
            assert width <= RANKTER_TITLE_MAX_WIDTH, (topic, line)


def test_title_filters_highlight_keywords_with_a_stroke(tmp_path):
    filters, files = rankter_title_filters(
        "top 5 cat funny moments", work_dir=tmp_path, font_file=FONT)
    assert filters.count("drawtext=") == 5  # TOP 5 CAT FUNNY MOMENTS
    assert "fontcolor=0xffd400" in filters  # CAT is yellow
    assert "borderw=8" in filters  # Rankter 8px black stroke
    assert "box=1" not in filters  # no black box behind the title
    assert len(files) == 5
    # Exactly one drawtext is yellow, and it is the CAT word.
    parts = filters.split("drawtext=")[1:]
    yellow = [part for part in parts if "0xffd400" in part]
    assert len(yellow) == 1
    cat_file = [f for f in files if open(f).read() == "CAT"]
    assert len(cat_file) == 1
    assert cat_file[0] in yellow[0]


def test_title_falls_back_to_a_smaller_size_for_long_topics(tmp_path):
    filters, _ = rankter_title_filters(
        "TOP 5 MOST INSANE PARKOUR FAILS COMPILATION EVER MADE",
        work_dir=tmp_path, font_file=FONT)
    assert "fontsize=72" in filters or "fontsize=56" in filters


def test_list_label_reuses_narration_text_and_score():
    label = rankter_list_label(
        "Number 5. A sharp, fast segment. Clear from start to finish. "
        "Score: 80 out of 100.", 5,
        "Score 80.0/100 (editorial heuristic, not a viral prediction)")
    assert label == "A sharp, fast segment. Score 80/100"
    # "Number N." is stripped, the score comes from the narration itself.
    assert "Number" not in label
    # Score note alone is enough when the narration has no score phrase.
    label = rankter_list_label("Number 2. A steady segment.", 2, "Score 65.0/100")
    assert label == "A steady segment. Score 65/100"
    # Empty narration falls back to the rank, never crashes.
    assert rankter_list_label("", 3) == "Moment #3"


def test_truncate_to_width_keeps_short_text_and_ellipsises_long():
    short = truncate_to_width("A sharp segment.", FONT, RANKTER_LIST_FONT_SIZE, 900)
    assert short == "A sharp segment."
    long_text = "A very long descriptive sentence that will never fit in one line."
    truncated = truncate_to_width(long_text, FONT, RANKTER_LIST_FONT_SIZE, 400)
    assert truncated.endswith("…")
    from app.agents.production.graphics import measure_text
    assert measure_text(truncated, FONT, RANKTER_LIST_FONT_SIZE) <= 400


def test_list_filters_highlight_only_the_active_rank(tmp_path):
    filters, files = rankter_list_filters(ITEMS, 5, work_dir=tmp_path, font_file=FONT)
    assert filters.count("drawtext=") == 5
    assert len(files) == 5
    parts = filters.split("drawtext=")[1:]
    assert len(parts) == 5
    # Only the #5 line is yellow; the rest are white.
    assert sum("0xffd400" in part for part in parts) == 1
    assert "0xffd400" in parts[0]
    assert "fontcolor=white" in parts[1]
    # Every line carries its rank number and label.
    for (rank, label), part in zip(ITEMS, parts):
        text_file = [f for f in files if open(f).read().startswith(f"#{rank}  ")]
        assert len(text_file) == 1
        assert label.split(".")[0] in open(text_file[0]).read()
    # No active rank (intro card): nothing is highlighted.
    intro_filters, _ = rankter_list_filters(ITEMS, None, work_dir=tmp_path, font_file=FONT)
    assert "0xffd400" not in intro_filters


def test_list_filters_truncate_labels_to_the_frame(tmp_path):
    long_items = [(5, "A very long descriptive sentence that will never fit in one line."),
                  (4, "Short."), (3, "Short."), (2, "Short."), (1, "Short.")]
    _, files = rankter_list_filters(long_items, 4, work_dir=tmp_path, font_file=FONT)
    first = open(files[0]).read()
    assert first.endswith("…")
    from app.agents.production.graphics import measure_text
    assert measure_text(first, FONT, RANKTER_LIST_FONT_SIZE) <= RANKTER_LIST_MAX_WIDTH


def test_frame_filter_pads_the_clip_into_the_lower_62_percent():
    assert rankter_frame_filter() == "pad=1080:1920:0:730:color=0x0f1016"


def test_drawtext_filter_supports_a_stroke_without_a_box(tmp_path):
    from app.agents.production.graphics import write_text_file
    text_file = write_text_file(tmp_path, "t.txt", "HELLO")
    filt = drawtext_filter(text_file, x="100", y="100", fontsize=96,
                           font_file=FONT, box=False, borderw=8)
    assert "borderw=8" in filt
    assert "box=1" not in filt
