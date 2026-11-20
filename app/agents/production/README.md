# Production — BUILT (Prompt 08)

Video Production Agent — clip extraction, vertical reframing,
narration, captions, graphics, sound, rendering.

Renders the Creative Director's approved Edit Decision List to a
real MP4 with FFmpeg: 1080x1920 @ 30fps, H.264 video, AAC audio.
Each clip is extracted at its verified timestamps, reframed to
9:16 (motion-centroid subject-aware crop, centre fallback),
burned in with countdown graphics, captions and attribution,
joined with intro/outro cards, normalised with loudnorm, and
validated with ffprobe plus a full decode check. The render
manifest records exactly what is in the video — and what is not.

Honest limits (Prompt 08): no TTS provider is configured and no
local TTS binary is installed, so narration audio is NOT
generated — the audio track is silence plus the clips' own
audio, and the manifest says so. No licensed music library is
configured, so there is no background music unless PROD_MUSIC_DIR
holds a licensed file (then it is looped under the mix at low
volume). SFX files are detected when configured but never mixed
in — SFX mixing is not implemented in this prompt. Transitions
are fade-through-black over the EDL transition seconds, not true
xfades, so the total duration matches the EDL exactly.
