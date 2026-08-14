"""H3ResultPreview: a run's take shown beside its lineage neighbor.

Wire the Save node's `path` output in; after the run the node's widget
shows a MINI timeline -- the fresh take together with the parent it
extends (parent first) or the clip it prepends (take first) -- and plays
the pair seamlessly through the same server-built smart-cut route the
Timeline node uses. The answer it exists for: "did this take actually
connect?", one glance, right where the generation happened.

Lineage is resolved server-side from the take's sidecar (parent_id ->
scan the take's folder for the matching sidecar); everything visual
happens in web/h3_mctx_ui.js from the ui payload returned here.
"""

import logging
import os

import folder_paths

from . import frames as fr
from . import mctx

_LOG = logging.getLogger("obvpm.h3")


def _measure_seam(side):
    """Measured seam quality at the generated<->pinned boundary.

    The same metric as seam_report: L2 of the adjacent-latent-step
    difference AT the boundary, as a ratio of the clip's median step
    difference. Metadata can only say the join coordinates line up;
    this says whether motion actually flows through them. ~1x =
    seamless, >>1x = the model planted the anchor and cut to it.
    Returns {"ratio", "verdict"} or None when not measurable.
    """
    import torch
    video, _audio, meta = mctx.load_sidecar(side)
    video = video.float()
    rel = meta.get("relation")
    head = int(meta.get("pinned_head_frames", 0) or 0)
    tail = int(meta.get("pinned_tail_frames", 0) or 0)
    lt = video.shape[2]
    if rel == "extends" and head:
        seam = fr.frames_to_latents(head) - 1
    elif rel == "prepends" and tail:
        seam = lt - fr.frames_to_latents(tail) - 1
    else:
        return None
    if not 0 <= seam < lt - 1:
        return None
    d = video[:, :, 1:] - video[:, :, :-1]
    d = ((d ** 2).mean(dim=(0, 1, 3, 4))) ** 0.5
    typical = d[torch.arange(len(d)) != seam].median().item()
    if not typical:
        return None
    ratio = d[seam].item() / typical
    verdict = ("seamless" if ratio < 1.2 else
               "soft bump" if ratio < 1.8 else "hard cut")
    return {"ratio": round(ratio, 2), "verdict": verdict}


class H3ResultPreview:
    CATEGORY = "obvpm/h3"
    FUNCTION = "show"
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    DESCRIPTION = (
        "Mini timeline of a just-saved take and the clip it continues: "
        "wire a Save node's path output in, and after each run this "
        "shows the take beside its parent (extend) or its target "
        "(prepend), seam derived from the sidecars, playable as one "
        "seamless preview."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "path": ("STRING", {
                    "forceInput": True,
                    "tooltip": "The saved clip's path -- wire the path "
                               "output of an H3 MCtx Save node."}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, path):
        try:
            st = os.stat(cls._abs(path))
            return "%d:%d" % (st.st_size, st.st_mtime_ns)
        except (OSError, ValueError):
            return "absent"

    @staticmethod
    def _abs(path):
        p = str(path or "").strip()
        if not os.path.isabs(p):
            p = os.path.join(folder_paths.get_output_directory(), p)
        return os.path.abspath(p)

    def show(self, path):
        root = os.path.abspath(folder_paths.get_output_directory())
        ap = self._abs(path)
        if os.path.commonpath([root, ap]) != root:
            raise ValueError(
                "H3ResultPreview: %s is outside the output folder" % path)
        if not os.path.isfile(ap):
            raise ValueError("H3ResultPreview: clip not found: %s" % path)
        rel = os.path.relpath(ap, root).replace(os.sep, "/")

        relation, parent_rel, seam = "", None, None
        side = mctx.sidecar_path(ap)
        if os.path.isfile(side):
            try:
                header = mctx.read_header(side)
                relation = header.get("relation") or ""
                pid = header.get("parent_id") or ""
                if relation in ("extends", "prepends"):
                    try:
                        seam = _measure_seam(side)
                    except Exception:
                        _LOG.exception(
                            "obvpm.h3: seam measurement failed for %s", rel)
                if relation in ("extends", "prepends") and pid:
                    pside = mctx.scan_for_parent(os.path.dirname(ap), pid)
                    if pside:
                        pclip = (pside[:-len(mctx.SIDECAR_SUFFIX)]
                                 + ".mp4")
                        if os.path.isfile(pclip):
                            parent_rel = os.path.relpath(
                                pclip, root).replace(os.sep, "/")
            except Exception:
                _LOG.exception(
                    "H3ResultPreview: unreadable sidecar for %s", rel)

        if parent_rel and relation == "extends":
            sequence = parent_rel + "\n" + rel
        elif parent_rel and relation == "prepends":
            sequence = rel + "\n" + parent_rel
        else:
            sequence = rel
        if parent_rel is None and relation in ("extends", "prepends"):
            _LOG.warning("obvpm.h3: %s %s a parent whose clip is not in "
                         "its folder; previewing the take alone",
                         rel, relation)
        return {"ui": {"h3_result": [{
            "clip": rel, "parent": parent_rel, "relation": relation,
            "sequence": sequence, "seam": seam,
        }]}}
