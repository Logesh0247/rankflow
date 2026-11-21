"""Full-bleed style tests (restyle 2026-11-21): the render looks
like the reference parkour ranking videos — full-bleed 1080x1920
video, black title bar on top, numbered list down the left edge,
big bottom caption, straight cuts, no end-credits screen, voiceover
off. These tests pin the layout geometry and the title/caption
rules; the rendered-frame checks live in tests/test_production.py.
"""
from pathlib import Path

from app.agents.production.captions import build_ass
from app.agents.production.graphics import (
    FULLBLEED_ATTRIBUTION_Y, FULLBLEED_BOTTOM_MAX_WIDTH,
    FULLBLEED_BOTTOM_Y, FULLBLEED_CAPTION_X, FULLBLEED_NUMBERS_SPACING,
    FULLBLEED_NUMBERS_X, FULLBLEED_NUMBERS_Y, FULLBLEED_TITLE_BAR_HEIGHT,
    FULLBLEED_TITLE_MAX_WIDTH, TARGET_HEIGHT, TARGET_WIDTH,
    clean_clip_caption, clip_graphics_filters, fullbleed_numbers_filters,
    fullbleed_title_bar_filter, fullbleed_title_filters, measure_text,
    split_fullbleed_title, wrap_bottom_caption,
)

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
ITEMS = [(5, "A sharp, fast segment"),
         (4, "A steady segment"),
         (3, "A dark segment"),
         (2, "A bright segment"),
         (1, "A sharp segment")]


def test_frame_is_vertical_1080x1920():
    assert TARGET_WIDTH == 1080 and TARGET_HEIGHT == 1920


def test_title_bar_is_200px_black_across_the_top():
    filt = fullbleed_title_bar_filter()
    assert filt == "drawbox=x=0:y=0:w=1080:h=200:color=black:t=fill"
    assert FULLBLEED_TITLE_BAR_HEIGHT == 200


def test_title_split_colours_qualifier_red_and_subject_yellow():
    line1, line2 = split_fullbleed_title("TOP 5 CRAZIEST PARKOUR FAILS")
    assert line1 == ["RANKING", "CRAZIEST"]
    assert line2 == ["PARKOUR", "FAILS"]
    line1, line2 = split_fullbleed_title("top 5 cat funny moments")
    assert line1 == ["RANKING", "FUNNY"]
    assert line2 == ["CAT", "MOMENTS"]
    # No qualifier: line 1 is just RANKING, everything else is the subject.
    line1, line2 = split_fullbleed_title("TOP 5 PARKOUR FAILS")
    assert line1 == ["RANKING"]
    assert line2 == ["PARKOUR", "FAILS"]
    # "THE" joins the qualifier phrase ("THE CRAZIEST").
    line1, line2 = split_fullbleed_title("Ranking The Craziest Parkour Fails")
    assert line1 == ["RANKING", "THE", "CRAZIEST"]
    assert line2 == ["PARKOUR", "FAILS"]


def test_title_filters_use_white_red_yellow_with_thick_stroke(tmp_path):
    filters, files = fullbleed_title_filters(
        "TOP 5 CRAZIEST PARKOUR FAILS", work_dir=tmp_path, font_file=FONT)
    assert "fontcolor=white" in filters          # RANKING
    assert "fontcolor=0xff2d2d" in filters        # CRAZIEST (red)
    assert "fontcolor=0xffd400" in filters        # PARKOUR FAILS (yellow)
    assert "borderw=8" in filters                # thick black outline
    assert "fontsize=84" in filters              # fits at the largest size
    # Every word is its own drawtext file; the title fits the frame.
    assert len(files) == 4  # RANKING, CRAZIEST, PARKOUR, FAILS
    for path in files:
        assert Path(path).read_text()
    # A long title falls back to a smaller size but still fits.
    long_filters, _ = fullbleed_title_filters(
        "TOP 5 MOST INSANE BACKFLIP FAILS ON A TRAMPOLINE",
        work_dir=tmp_path, font_file=FONT)
    assert "fontsize=56" in long_filters or "fontsize=72" in long_filters


def test_title_lines_fit_inside_the_frame(tmp_path):
    # The widest line at the chosen size must fit the 952px max width.
    filters, files = fullbleed_title_filters(
        "TOP 5 CRAZIEST PARKOUR FAILS", work_dir=tmp_path, font_file=FONT)
    assert "fontsize=84" in filters
    line1, line2 = split_fullbleed_title("TOP 5 CRAZIEST PARKOUR FAILS")
    assert measure_text(" ".join(line1), FONT, 84) <= FULLBLEED_TITLE_MAX_WIDTH
    assert measure_text(" ".join(line2), FONT, 84) <= FULLBLEED_TITLE_MAX_WIDTH


def test_numbers_down_the_left_edge_active_rank_yellow(tmp_path):
    filters, files = fullbleed_numbers_filters(
        ITEMS, 5, work_dir=tmp_path, font_file=FONT)
    # Five numbers, 1 first at y=260, 220px apart, x=36.
    assert filters.count("fontsize=120") == 5
    assert f"x={FULLBLEED_NUMBERS_X}" in filters
    assert f"y={FULLBLEED_NUMBERS_Y}" in filters
    assert f"y={FULLBLEED_NUMBERS_Y + 4 * FULLBLEED_NUMBERS_SPACING}" in filters
    assert "fontcolor=0xffd400" in filters  # active #5 is yellow
    assert "borderw=10" in filters          # thick black outline
    # Only the active rank's caption and the ones already played
    # (#5, the active clip) are shown; #1–#4 captions are hidden.
    shown = [Path(f).read_text() for f in files if f.endswith("-cap.txt")]
    assert shown == ["A sharp, fast segment"]
    # With no active rank (title card state) every caption shows.
    _, all_files = fullbleed_numbers_filters(
        ITEMS, None, work_dir=tmp_path, font_file=FONT)
    shown_all = [Path(f).read_text() for f in all_files if f.endswith("-cap.txt")]
    assert len(shown_all) == 5


def test_numbers_captions_accumulate_as_countdown_runs(tmp_path):
    # When clip #3 is playing, #5, #4 and #3 captions are visible
    # (files are written in rank order 1..5, so they read 3, 4, 5).
    _, files = fullbleed_numbers_filters(
        ITEMS, 3, work_dir=tmp_path, font_file=FONT)
    shown = [Path(f).read_text() for f in files if f.endswith("-cap.txt")]
    assert shown == ["A dark segment", "A steady segment",
                     "A sharp, fast segment"]


def test_clean_clip_caption_strips_number_prefix_and_score():
    assert clean_clip_caption(
        "Number 5. A sharp, fast segment. Score: 80 out of 100.", 5) == \
        "A sharp, fast segment."
    assert clean_clip_caption("Number 1. Wait for it. Score 60/100", 1) == \
        "Wait for it."
    assert clean_clip_caption("", 3) == "Moment #3"
    # Only the first sentence is kept.
    assert clean_clip_caption(
        "Number 4. Parkour gone wrong. He gets up again.", 4) == \
        "Parkour gone wrong."


def test_bottom_caption_is_uppercased_and_at_most_two_lines():
    lines = wrap_bottom_caption("Spidey! Fail", FONT)
    assert lines == ["SPIDEY! FAIL"]
    assert measure_text(lines[0], FONT, 64) <= FULLBLEED_BOTTOM_MAX_WIDTH
    two_lines = wrap_bottom_caption("Bro thought he was Spider-Man", FONT)
    assert len(two_lines) == 2
    assert all(measure_text(line, FONT, 64) <= FULLBLEED_BOTTOM_MAX_WIDTH
               for line in two_lines)
    long_lines = wrap_bottom_caption(
        "Bro thought he was Spider-Man and then he fell off the roof "
        "into the bushes below while everyone watched", FONT)
    assert 1 <= len(long_lines) <= 2
    assert all(measure_text(line, FONT, 64) <= FULLBLEED_BOTTOM_MAX_WIDTH
               for line in long_lines)
    assert long_lines[-1].endswith("…")  # words dropped from the end
    assert FULLBLEED_BOTTOM_Y == 1660


def test_clip_graphics_have_title_bar_numbers_bottom_caption_no_pad_no_fade(tmp_path):
    filters, files = clip_graphics_filters(
        rank=5, topic="TOP 5 CRAZIEST PARKOUR FAILS", list_items=ITEMS,
        source_label="Parkour Fails 5 by Creator 5",
        attribution_text=None, attribution_required=False,
        duration_seconds=6.0, work_dir=tmp_path, font_file=FONT,
        bottom_caption="A sharp, fast segment")
    # Title bar first, then title, numbers, source, bottom caption.
    assert filters.startswith("drawbox=x=0:y=0:w=1080:h=200:color=black:t=fill")
    assert "pad=" not in filters          # full-bleed: no video area
    assert "fade=" not in filters         # straight cuts, no fades
    assert "fontcolor=0xffd400" in filters  # yellow subject + active #5
    assert "A SHARP, FAST SEGMENT" in "".join(
        Path(f).read_text() for f in files)  # bottom caption, uppercased
    assert "x=300" in filters            # list captions sit right of the numbers
    assert "fontsize=48" in filters       # per-clip list captions


def test_clip_graphics_attribution_only_in_last_two_seconds(tmp_path):
    filters, _ = clip_graphics_filters(
        rank=5, topic="TOP 5 CRAZIEST PARKOUR FAILS", list_items=ITEMS,
        source_label="Parkour Fails 5 by Creator 5",
        attribution_text="Parkour Fails 5 by Creator 5 (CC BY 4.0)",
        attribution_required=True, duration_seconds=6.0,
        work_dir=tmp_path, font_file=FONT)
    assert "enable='between(t,4.00,6.00)'" in filters
    assert f"y={FULLBLEED_ATTRIBUTION_Y}" in filters
    # No attribution line at all when the licence does not require it.
    plain, _ = clip_graphics_filters(
        rank=5, topic="TOP 5 CRAZIEST PARKOUR FAILS", list_items=ITEMS,
        source_label="Parkour Fails 5 by Creator 5",
        attribution_text="Parkour Fails 5 by Creator 5 (CC BY 4.0)",
        attribution_required=False, duration_seconds=6.0,
        work_dir=tmp_path, font_file=FONT)
    assert "between(t," not in plain


def test_build_ass_margin_v_raises_captions_above_bottom_caption(tmp_path):
    cues = [{"start_seconds": 0.0, "end_seconds": 2.0,
             "text": "Number 5. A sharp segment."}]
    default = build_ass(cues, tmp_path / "default.ass")
    assert "108,108,200,1" in default.read_text()
    raised = build_ass(cues, tmp_path / "raised.ass", margin_v=380)
    assert "108,108,380,1" in raised.read_text()
