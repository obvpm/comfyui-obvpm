"""The single site of frame <-> latent-step arithmetic for MiniMax H3.

Every node in this subpackage that converts between pixel frames, video
latent steps and audio latent steps goes through this module. One
implementation, not ad-hoc math per node (DESIGN.md section 6).

The grids, mirrored from ComfyUI core (comfy_extras/nodes_minimax_h3.py,
comfy/ldm/minimax/model.py):

  video    24 fps. Raw clip lengths sit on the 17k+5 frame grid
           (5, 22, 39, ... 3592). A clip of 17k+5 frames encodes to 5k+2
           latent steps; the steps cover (1, 4, 4, 4, 4) pixel frames,
           repeating every 5 steps.
  audio    40 latent steps per second, so FRAME_RESCALE = 5/3 audio steps
           per video frame. H3 rounds a clip's audio grid UP, so a clip
           carries up to one step (25 ms) more sound than picture.

Audio lengths are ALWAYS differences of cumulative totals, never converted
deltas: round(frames/24*40) does not distribute over addition, and the
per-seam error compounds down a chain (measured 1.48 s over four chunks by
MMH3Tools, whose boundary-difference approach and off-grid-safe
frame_at_latent this module adopts -- see THIRD_PARTY_NOTICES.md).

Standalone: no torch, no comfy imports. `python frames.py` runs the trap
tests.
"""

FPS = 24
AUDIO_LATENT_FPS = 40
FRAME_RESCALE = 5.0 / 3.0  # audio steps per video frame

FRAMES_PER_GROUP = 17
FRAME_BASE = 5
LATENTS_PER_GROUP = 5
LATENT_BASE = 2

# Frames covered by each latent step, indexed by step % 5. Every fifth
# step spans ONE frame, the rest span four.
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)

# Context windows that are whole numbers of latent steps, the ladder
# snap_window() descends. (56 exists upstream in the standalone
# Motion-Context pack; the Context-Loop fork removed it. We keep it.)
# All except 1 are 5j+2 steps and therefore tail-sliceable at phase 0
# from any on-grid clip. The 1-frame window is NOT: the last step of a
# 5k+2 latent spans 4 frames, so a 1-step tail starts at phase 1. A
# 1-frame window is only reachable via the pixel path (VAE-encode one
# frame) or an interior phase-0 slice.
WINDOW_LADDER = (124, 107, 90, 73, 56, 39, 22, 5, 1)
LATENT_TAIL_WINDOWS = tuple(w for w in WINDOW_LADDER if w != 1)

# The windows offered as widget choices (subset of the ladder).
WINDOW_CHOICES = ("22", "5", "39", "56", "1")


# ---------------------------------------------------------------------------
# video grid
# ---------------------------------------------------------------------------

def frames_to_latents(frame_count):
    """Latent steps the VAE produces for a clip of frame_count frames.

    Mirror of core's video_latent_t(): max(1, (n-5)//17*5+2) for n >= 5.
    Only meaningful for on-grid clip lengths; between grid points the VAE
    covers the FIRST latents_to_frames(result) frames of the input.
    """
    if frame_count <= FRAME_BASE:
        return LATENT_BASE
    return ((frame_count - FRAME_BASE) // FRAMES_PER_GROUP) * LATENTS_PER_GROUP + LATENT_BASE


def latents_to_frames(latent_t):
    """Pixel frames of an on-grid clip with latent_t latent steps.

    Inverse of frames_to_latents ON the 5k+2 grid only. For arbitrary
    step indices use frame_at_latent (this one reports nonsense off-grid:
    latents_to_frames(1) == -12).
    """
    return FRAMES_PER_GROUP * ((latent_t - LATENT_BASE) // LATENTS_PER_GROUP) + FRAME_BASE


def frame_at_latent(k):
    """First pixel frame of latent step k, valid for ANY k >= 0.

    The off-grid-safe general inverse (adopted from MMH3Tools common.py).
    Agrees with latents_to_frames wherever both are valid:
    frame_at_latent(37) == 124 == latents_to_frames(37).
    """
    k = int(k)
    if k <= 0:
        return 0
    full, rem = divmod(k, len(FRAME_PER_TOKEN))
    return full * sum(FRAME_PER_TOKEN) + sum(FRAME_PER_TOKEN[:rem])


def pixel_frames(latent_t):
    """Pixel frames covered by latent_t steps from cycle phase 0."""
    return frame_at_latent(latent_t)


def step_offsets(latent_t):
    """Pixel-frame index at which each of latent_t steps begins (phase 0)."""
    return [frame_at_latent(k) for k in range(latent_t)]


def steps_for_frames(n):
    """Latent steps covering EXACTLY n pixel frames from cycle phase 0.

    Returns None when no whole number of steps covers n. Reachable totals
    are 1, 5, 9, 13, 17, 22, ...; of the offered windows 1, 5, 22, 39 and
    56 land on 1, 2, 7, 12 and 17 steps.
    """
    k, covered = 0, 0
    while covered < n:
        covered += FRAME_PER_TOKEN[k % 5]
        k += 1
    return k if covered == n else None


def snap_frames_up(n):
    """Snap UP onto the 17k+5 clip-length grid (matches core's alignment)."""
    n = max(FRAME_BASE, int(n))
    while n % FRAMES_PER_GROUP != FRAME_BASE:
        n += 1
    return n


def snap_latents_down(n):
    """Snap DOWN onto the 5k+2 video-latent grid (minimum 2)."""
    if n < LATENT_BASE:
        return LATENT_BASE
    return LATENTS_PER_GROUP * ((n - LATENT_BASE) // LATENTS_PER_GROUP) + LATENT_BASE


def on_latent_grid(latent_t):
    return snap_latents_down(latent_t) == latent_t


def snap_window(n):
    """Largest ladder window <= n, or None when even 1 does not fit."""
    for g in WINDOW_LADDER:
        if g <= n:
            return g
    return None


# ---------------------------------------------------------------------------
# audio grid -- cumulative totals only
# ---------------------------------------------------------------------------

def audio_total(frame_count):
    """Cumulative audio latent steps at a frame count. THE conversion.

    Every audio length in this pack is a difference of two of these,
    never a directly converted delta (see module docstring).
    """
    return int(round(frame_count / float(FPS) * AUDIO_LATENT_FPS))


def audio_span(frame_a, frame_b):
    """Audio latent steps between two cumulative frame positions."""
    if frame_b < frame_a:
        raise ValueError("audio_span: frame_b %d < frame_a %d" % (frame_b, frame_a))
    return audio_total(frame_b) - audio_total(frame_a)


def audio_overhang(total_audio_t, frame_count):
    """Fraction of a step the audio grid extends past the last frame.

    H3 rounds the audio grid UP (124 frames want 206.67 steps, the layout
    allocates 207), so a clip's final audio step reaches ~overhang/40 s
    beyond its last pixel frame. Callers compensate window placement with
    it. Returns 0.0 (with no complaint) when the grid looks unexpected --
    the caller logs, this module stays torch- and logging-free.
    """
    overhang = total_audio_t - FRAME_RESCALE * frame_count
    if not (0.0 <= overhang < 1.0):
        return None
    return float(overhang)


# ---------------------------------------------------------------------------
# tail-slice soundness
# ---------------------------------------------------------------------------

def tail_slice_start(total_steps, window_steps):
    """Start step of a tail slice, verified to sit at cycle phase 0.

    A slice is only sound when it starts at phase 0, otherwise its
    (1,4,4,4,4) spans disagree with the positions written for it and the
    join lands at the wrong instant. On-grid totals (5k+2) minus the
    ladder's step counts are always multiples of 5, so this never fires
    for on-grid inputs; it is the guard for everything else.
    """
    if window_steps > total_steps:
        raise ValueError(
            "tail slice of %d steps from a %d step latent" % (window_steps, total_steps))
    start = total_steps - window_steps
    if start % LATENTS_PER_GROUP != 0:
        raise ValueError(
            "tail slice of %d steps from a %d step latent starts at cycle "
            "phase %d, not 0; the slice would be unsound"
            % (window_steps, total_steps, start % LATENTS_PER_GROUP))
    return start


# ---------------------------------------------------------------------------
# trap tests
# ---------------------------------------------------------------------------

def _self_test():
    # grid round trips
    for k in range(0, 40):
        n = FRAMES_PER_GROUP * k + FRAME_BASE
        t = frames_to_latents(n)
        assert t == LATENTS_PER_GROUP * k + LATENT_BASE, (n, t)
        assert latents_to_frames(t) == n, (n, t)
        assert on_latent_grid(t)

    # frame_at_latent: the off-grid trap latents_to_frames falls into
    assert latents_to_frames(1) == -12          # nonsense, documented
    assert frame_at_latent(1) == 1              # correct
    assert frame_at_latent(0) == 0
    assert frame_at_latent(2) == 5
    assert frame_at_latent(5) == 17
    assert frame_at_latent(7) == 22
    assert frame_at_latent(37) == 124 == latents_to_frames(37)

    # step cycle
    assert step_offsets(7) == [0, 1, 5, 9, 13, 17, 18]
    assert pixel_frames(7) == 22
    assert pixel_frames(2) == 5
    assert pixel_frames(12) == 39
    assert pixel_frames(17) == 56

    # steps_for_frames: exact covers only
    assert steps_for_frames(1) == 1
    assert steps_for_frames(5) == 2
    assert steps_for_frames(22) == 7
    assert steps_for_frames(39) == 12
    assert steps_for_frames(56) == 17
    assert steps_for_frames(2) is None
    assert steps_for_frames(10) is None

    # every ladder window is an exact cover; all but 1 are phase-0
    # tail-sliceable from every on-grid clip long enough to hold them
    for w in WINDOW_LADDER:
        assert steps_for_frames(w) is not None, w
    for w in LATENT_TAIL_WINDOWS:
        s = steps_for_frames(w)
        for k in range(1, 30):
            total = LATENTS_PER_GROUP * k + LATENT_BASE
            if s <= total:
                assert tail_slice_start(total, s) % 5 == 0, (w, total)
    # ...and the 1-frame window is NOT tail-sliceable (last step spans 4)
    try:
        tail_slice_start(7, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("1-step tail slice should be refused")

    # snap_window
    assert snap_window(22) == 22
    assert snap_window(30) == 22
    assert snap_window(4) == 1
    assert snap_window(200) == 124
    assert snap_window(0) is None

    # audio: round(f/24*40) does NOT distribute over addition -- the trap
    # that makes per-seam delta conversion drift down a chain. 22+22
    # frames: two converted deltas give 74 steps, the true total is 73.
    assert audio_total(22) + audio_total(22) == 74
    assert audio_total(44) == 73
    assert audio_total(22) + audio_total(22) != audio_total(44)

    # audio_span is exact by construction
    acc = 0
    prev = 0
    for boundary in (124, 226, 328, 430):
        acc += audio_span(prev, boundary)
        prev = boundary
    assert acc == audio_total(430)

    # overhang
    assert audio_overhang(207, 124) is not None
    assert abs(audio_overhang(207, 124) - (207 - FRAME_RESCALE * 124)) < 1e-9
    assert audio_overhang(999, 124) is None  # unexpected grid -> None

    # tail_slice_start refusal
    try:
        tail_slice_start(8, 2)  # 8 is off-grid; 8-2=6, phase 1
    except ValueError:
        pass
    else:
        raise AssertionError("off-phase tail slice not refused")

    return True


if __name__ == "__main__":
    _self_test()
    print("frames.py: all trap tests passed")
