# Momentrix AI — Creative Director Agent (Prompt 07)

Transforms the five ranked moments into a cohesive, entertaining,
original YouTube Shorts concept: a #5-to-#1 countdown with a
hook, signal-grounded narration, storyboard, caption plan,
visual direction and a structured Edit Decision List.

## How it works

1. **Load** the job's five `RankingResult` rows (ranks 1–5, five
   different sources). Fewer than five fails honestly — run
   Ranking first; no moment is invented.
2. **Rights gate, again.** Each ranked candidate is re-checked
   server-side before it is used — an approval that expired or
   was revoked after ranking never reaches the story.
3. **Hook.** Five hook candidates are generated from the
   user-supplied topic only and scored deterministically on
   attention, topic relevance, curiosity, clarity and natural
   delivery; the strongest wins (ties break by template order).
   No hook makes a misleading claim or describes a clip.
4. **Countdown storyboard.** Seven scenes: intro (hook), five
   moment scenes #5 → #1, outro (closing). Each moment scene
   carries its narration, its measured-signal basis, its
   caption lines and its transition.
5. **Narration.** Deterministic templates (`deterministic_template_v1`),
   built from measured frame signals only — see below.
6. **Visual direction + captions.** Countdown graphics, caption
   placement (bottom third, 10% safe margins, ≤40 chars/line),
   text emphasis, transitions (hard cut for high-motion,
   cross-dissolve otherwise), SFX cues, licensed-music
   requirements, and attribution display per clip.
7. **Edit Decision List.** Five clips in #5 → #1 order, with
   the verified moment timestamps, source paths, narration
   cues, graphic instructions, caption timing, audio cues and
   transitions. Validated against the shared `EditDecisionList`
   contract (1080×1920 @ 30 fps).
8. **Phase 7 validation.** All five moments represented
   exactly once, EDL timestamps equal the verified moments,
   no forbidden word in the narration, narration fits the
   timeline, total duration is a Short. Failures reject the
   plan, never quietly edit it.
9. **Persist** `Storyboard` + `Narration` (draft) +
   `EditInstruction` rows. Re-running replaces them, so a
   re-run never duplicates.

## Narration — what it may and may not say

There is no vision-language model in this environment, so the
narration never names what happens in a scene. It describes
only what Video Intelligence measured for the moment:

- rank ("Number five.")
- motion word from mean frame difference (fast / high / moderate / low-motion)
- sharpness / brightness ("A sharp, fast-moving segment.")
- context clarity ("Clear from start to finish." / "cut at the edge of the clip" when timestamp verification flagged truncation / "still, unchanging" when frozen)
- the ranking component that carried its score ("Fast, clear, and hard to look away from." for entertainment, etc.)

The source title/creator are shown on screen as a lower third,
never spoken as if they described the footage. The candidate's
provider-published description is not used as footage evidence.

A forbidden-word guard (`FORBIDDEN_WORDS` in `narration.py`)
rejects any moment narration containing a semantic-event word
(fall, jump, flip, crash, slip, trip, parkour, stunt, …). The
topic itself may contain such a word — that is the user's
framing in the hook/title, not a claim about a clip, so the
hook is checked separately and moment narration against the
list. Every scene records its `narration_basis`: the measured
signals the wording rests on, so any line can be audited back
to its numbers.

Narration is a continuous voiceover cued at each clip's start,
not a hard per-clip limit: the validator checks the total
voiceover against the total timeline, and flags (as a warning)
any moment whose narration runs more than 2s past its clip.

## Timing

Timeline = intro (3s) + five clips + outro (3s) + transitions
(0.5s between each of the seven segments). Narration words are
budgeted to 95% of the timeline at 150 wpm, shared between the
five moments in proportion to clip length (min 4 words each, so
every moment still gets its rank intro). Hook and closing take
their words first.

Example: five 6s clips → 3 + 30 + 3 + 3 = 39s total.

## Files

- `app/agents/creative_director/signals.py` — planning inputs from ranked rows
- `app/agents/creative_director/hooks.py` — hook candidates + deterministic scoring
- `app/agents/creative_director/narration.py` — signal-grounded narration, forbidden-word guard, caption splitting
- `app/agents/creative_director/visual.py` — visual direction, transitions, music requirements
- `app/agents/creative_director/service.py` — CreativeDirectorService, Phase 7 validation, persistence
- `app/agents/creative_director/agent.py` — orchestrator executor
- `app/api/creative_director.py` — `GET /jobs/{id}/storyboard`, `/narration`, `/edl`, `/creative`
- `scripts/run_creative_director.py` — demo

## Configuration

`CD_TARGET_DURATION_SECONDS=60.0`, `CD_INTRO_SECONDS=3.0`,
`CD_OUTRO_SECONDS=3.0`, `CD_TRANSITION_SECONDS=0.5`,
`CD_NARRATION_WPM=150.0` (see `.env.example`).

## Verification (Prompt 07)

Full suite: `.venv/bin/python -m pytest tests/ -q` — **176 passed,
0 failed** (158 before this prompt + 18 new creative-director
tests). The creative tests in `tests/test_creative_director.py`
cover hook scoring/selection, signal-grounded narration and its
word budget, the forbidden-word guard, caption splitting, the
countdown storyboard and EDL (order, timestamps, 1080×1920),
Phase 7 validation (wrong order, forbidden word, over-long
narration, timestamp mismatch), the rights gate (revoked
approval rejected), insufficient ranking, idempotency, the API,
and a full orchestrator run through real research → rights →
video intelligence → ranking → creative director → production →
quality assurance (ends `awaiting_approval` with a QA report of
`needs_review`, because no narration voice exists yet).

## Known limitations

- No LLM is configured, so hooks and narration are templates —
  varied by signal, but not written fresh per topic. A model
  could replace the templates later without changing the
  storyboard/EDL schema, provided it stays inside the same
  signal-only narration contract.
- No licensed music or SFX track is selected: no licensed music /
  SFX library is configured. Production must supply licensed assets.
- EDL source paths are the approved footage file when on record,
  otherwise the source URL as the retrieval reference — Production
  must resolve/download before rendering.
- The orchestrator has no loop back to Research: if Ranking
  cannot supply five moments, Creative Director cannot start.
