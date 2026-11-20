# Ranking — BUILT (Prompt 06)

Ranking Agent (Prompt 06) — moment scoring, comparison, duplicate detection, selection of five unique sources.

**What it does:** for one job, it re-checks the Rights gate per candidate, scores every analysed moment from its measured frame signals with `S = 0.25E + 0.25U + 0.20V + 0.15C + 0.15Q` (scorer `heuristic_editorial_v1`), drops duplicates (same moment, overlapping same-source moments, same-event re-uploads by metadata + signal fingerprint), drops moments below the minimum score, keeps the best moment per source video, and persists exactly five ranked moments (#1 best … #5), each from a different source, with the component breakdown and a written explanation. See `docs/RANKING.md`.

**Honest limit:** scores are editorial heuristics from measured signals only — NOT viral-performance probabilities, and no semantic event is claimed (Video Intelligence never named what happens in a scene). Fewer than five eligible sources fails the job honestly: no moment is invented and rights approval is never bypassed to reach five.
