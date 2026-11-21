"""RANKFLOW AI — Video Production Agent, Phase 3: vertical reframing (Prompt 08).

Converts any source frame into the 9:16 target (1080x1920) by
cropping to the target aspect ratio, then scaling. The crop window
is centred by default; when a subject position is known (the
motion centroid measured from the clip itself, see
``motion_centroid_x``) the window is shifted so the subject stays
in frame, clamped inside the source frame with the configured
safe margin. Nothing is ever cropped outside the source frame,
and the crop window is always even-sized (required by libx264).

This is the "subject-aware reframing" the brief asks for: the
subject is the moving object measured by frame differencing, not
a guessed or claimed detection. When no motion is found (a
static shot) the window falls back to centre crop, and that
fallback is recorded by the caller.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_ASPECT = TARGET_WIDTH / TARGET_HEIGHT  # 0.5625


class CropWindow(BaseModel):
    """A crop window inside the source frame, in source pixels."""

    width: int
    height: int
    x: int
    y: int
    subject_aware: bool = False
    subject_x: float | None = None  # normalised subject centre, 0..1


def _even(n: int) -> int:
    return n - (n % 2)


def compute_vertical_crop(source_width: int, source_height: int,
                          *, subject_x: float | None = None,
                          subject_y: float | None = None,
                          safe_margin_fraction: float = 0.10,
                          target_aspect: float = TARGET_ASPECT) -> CropWindow:
    """Crop window that turns the source aspect into `target_aspect`.

    Default target is 9:16 (1080x1920). The Rankter layout crops
    to the video-area aspect instead (1080x1190) — the same
    subject-aware rule, a different target rectangle.

    Wide source (aspect > target): crop the width, keep full height.
    Tall source (aspect < target): crop the height, keep full width.
    The subject position, when given, centres the window on it,
    clamped so the window stays inside the frame. (The 10% safe
    margins in the brief apply to captions and graphics, which
    Production places inside them — not to the crop window, which
    must use the full frame height/width on one axis.)
    """
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source dimensions must be positive")
    aspect = source_width / source_height
    if aspect > target_aspect:
        crop_h = source_height
        crop_w = _even(int(source_height * target_aspect))
        if subject_x is not None:
            centre = max(0.0, min(1.0, subject_x)) * source_width
            x = int(centre - crop_w / 2)
        else:
            x = (source_width - crop_w) // 2
        # Clamp so the window stays entirely inside the frame.
        hi = max(0, source_width - crop_w)
        x = _even(max(0, min(hi, x)))
        return CropWindow(width=crop_w, height=crop_h, x=x, y=0,
                          subject_aware=subject_x is not None, subject_x=subject_x)
    if aspect < target_aspect:
        crop_w = source_width
        crop_h = _even(int(source_width / target_aspect))
        if subject_y is not None:
            centre = max(0.0, min(1.0, subject_y)) * source_height
            y = int(centre - crop_h / 2)
        else:
            y = (source_height - crop_h) // 2
        hi = max(0, source_height - crop_h)
        y = _even(max(0, min(hi, y)))
        return CropWindow(width=crop_w, height=crop_h, x=0, y=y,
                          subject_aware=subject_y is not None, subject_x=subject_x)
    # Already at the target aspect.
    return CropWindow(width=_even(source_width), height=_even(source_height),
                      x=0, y=0, subject_aware=False)


def reframe_filter(source_width: int, source_height: int,
                   *, subject_x: float | None = None,
                   subject_y: float | None = None,
                   safe_margin_fraction: float = 0.10,
                   fps: int = 30,
                   target_width: int = TARGET_WIDTH,
                   target_height: int = TARGET_HEIGHT) -> tuple[str, CropWindow]:
    """FFmpeg filter chain for one clip: crop -> scale -> fps.

    Scales to `target_width`x`target_height` (default 1080x1920;
    the Rankter layout reframes clips to 1080x1190, the video
    area of the frame). Returns (filter_string, crop_window).
    The caller appends format / overlay filters after this.
    """
    window = compute_vertical_crop(
        source_width, source_height, subject_x=subject_x, subject_y=subject_y,
        safe_margin_fraction=safe_margin_fraction,
        target_aspect=target_width / target_height)
    chain = (f"crop={window.width}:{window.height}:{window.x}:{window.y},"
             f"scale={target_width}:{target_height},fps={fps}")
    return chain, window


def motion_centroid_x(path: str | Path, start_seconds: float, end_seconds: float,
                      *, sample_fps: float = 2.0) -> float | None:
    """Measure the horizontal centre of motion in a clip segment.

    Samples frames with OpenCV, differences consecutive frames,
    thresholds the difference, and averages the x-centroid of the
    moving pixels, weighted by how much motion each sample had.
    Returns the normalised centre (0..1) or None when no motion
    above the noise floor was found (static shot -> centre crop).
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, int(round(fps / sample_fps)))
        prev: np.ndarray | None = None
        total_weight = 0.0
        weighted_x = 0.0
        frame_index = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            t = frame_index / fps
            frame_index += 1
            if t < start_seconds:
                continue
            if t > end_seconds:
                break
            if frame_index % step != 0:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            if prev is None:
                prev = gray
                continue
            diff = cv2.absdiff(prev, gray)
            prev = gray
            _, mask = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            weight = float(np.count_nonzero(mask))
            if weight < 50:  # noise floor: fewer than 50 changed pixels
                continue
            ys, xs = np.nonzero(mask)
            if len(xs) == 0:
                continue
            total_weight += weight
            weighted_x += weight * float(xs.mean()) / frame.shape[1]
        if total_weight <= 0:
            return None
        return round(weighted_x / total_weight, 3)
    finally:
        cap.release()
