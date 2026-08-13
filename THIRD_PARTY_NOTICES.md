# Third-party notices

## ComfyUI-H3-Motion-Context (GPL-3.0)

Copyright (C) 2026 NikoDemon80
https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context

Vendored verbatim into `h3/vendor/`:

- `patch_layout.py` — runtime patch generalizing H3 keyframe anchors to
  arbitrary positions (position-id rewrite after the stock constructor),
  with self-test and cross-pack co-patch detection.
- `patch_payload.py` — runtime patch letting keyframe and reference
  conditioning latents coexist (concatenate instead of overwrite).

Additional mechanism knowledge in `h3/` (tail slicing soundness rules,
audio window end-alignment, trim behavior) derives from the same
project's `nodes.py`. This pack is GPL-3.0, compatible with the vendored
code's license.

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
  the downscale-evenness rule (valid factors divide gcd(lh//2, lw//2)),
  and the divergence-check design (contiguous diagonal-run scoring).
- Chunked VAE encode/decode traps for H3 (token_drop applied once,
  view-stride materialization, non-causal decoder slice recipe).
