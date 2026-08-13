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
project's `nodes.py`.

### Licensing of this pack

This pack's own code is licensed under **Apache-2.0** (see `LICENSE`).
The two files under `h3/vendor/` remain **GPL-3.0** (their license text
is kept beside them at `h3/vendor/LICENSE`). Because the pack is
distributed with those files included, the combined work as distributed
must be conveyed under GPL-3.0 terms; Apache-2.0 is one-way compatible
with GPL-3.0, so this combination is permitted. If the vendored files
are ever replaced by an independent implementation or an external
dependency, the pack's own Apache-2.0 licensing stands alone.

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
