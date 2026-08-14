# Third-party notices

This pack's own code is licensed under **Apache-2.0** (see `LICENSE`).
No third-party code is vendored; the acknowledgments below credit
projects whose published findings and designs informed `h3/`.

## ComfyUI-H3-Motion-Context

Copyright (C) 2026 NikoDemon80
https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context

The first pack to generalize H3 keyframe anchoring to arbitrary
positions (a capability ComfyUI core has since merged natively, PR
#15439, which `h3/` now uses directly). Mechanism findings from that
project informed the pins design: tail slicing soundness rules, audio
window end-alignment on the shared timeline, and trim behavior.

## ComfyUI-MMH3Tools (MIT)

Copyright (c) ckinpdx
https://github.com/ckinpdx/ComfyUI-MMH3Tools

No code vendored verbatim; the following designs and findings are
adopted in `h3/` (primarily `frames.py`), with thanks:

- The off-grid-safe `frame_at_latent` general inverse over the
  (1,4,4,4,4) frame-per-token cycle.
- The cumulative-total ("boundary difference") audio arithmetic rule and
  its rationale (round(frames/24*40) does not distribute over addition).
- The trim-dilemma rationale (latent-domain concatenation is unsound),
  which is why trimming here happens on decoded frames.
