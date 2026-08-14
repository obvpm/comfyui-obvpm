"""obvpm/h3: non-linear MiniMax H3 clip composition (see DESIGN.md).

Takes are clip pairs (MP4 + .mctx.safetensors latent sidecar) with
content-addressed lineage; continuation runs through the pins pipeline
(spec -> prepare -> apply) on core's native keyframe anchoring. Imported
by the pack root behind a guard: failure here must never take down the
rest of obvpm.
"""

from .nodes_assemble import H3Assemble, H3Timeline
from .nodes_load import H3LoadMCtx, H3LoadVideoWithMCtx
from .nodes_pins import (
    H3MCtxApplyPins,
    H3MCtxPinSpec,
    H3TrimPinned,
)
from .nodes_save import (
    H3SaveMCtxForVideo,
    H3SaveVideoWithMCtx,
    H3TrimAndSaveVideoWithMCtx,
)

try:
    from . import preview_route
    preview_route.register()
except Exception:  # headless/test runs have no PromptServer; nodes still work
    import logging
    logging.getLogger("obvpm.h3").info(
        "obvpm.h3: preview route not registered (no server)", exc_info=True)

NODE_CLASS_MAPPINGS = {
    "H3SaveVideoWithMCtx": H3SaveVideoWithMCtx,
    "H3TrimAndSaveVideoWithMCtx": H3TrimAndSaveVideoWithMCtx,
    "H3SaveMCtxForVideo": H3SaveMCtxForVideo,
    "H3LoadVideoWithMCtx": H3LoadVideoWithMCtx,
    "H3LoadMCtx": H3LoadMCtx,
    "H3MCtxPinSpec": H3MCtxPinSpec,
    "H3MCtxApplyPins": H3MCtxApplyPins,
    "H3TrimPinned": H3TrimPinned,
    "H3Assemble": H3Assemble,
    "H3Timeline": H3Timeline,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3SaveVideoWithMCtx": "H3 MCtx Save Video",
    "H3TrimAndSaveVideoWithMCtx": "H3 MCtx Trim and Save Video",
    "H3SaveMCtxForVideo": "H3 MCtx Save",
    "H3LoadVideoWithMCtx": "H3 MCtx Load Video",
    "H3LoadMCtx": "H3 MCtx Load",
    "H3MCtxPinSpec": "H3 MCtx Pin Spec",
    "H3MCtxApplyPins": "H3 MCtx Apply Pins",
    "H3TrimPinned": "H3 MCtx Trim Pinned",
    "H3Assemble": "H3 MCtx Assemble",
    "H3Timeline": "H3 MCtx Timeline",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
