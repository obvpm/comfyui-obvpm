"""Seam-quality report for mctx lineage clips. Standalone, no ComfyUI.

For every sidecar with an extends/prepends relation, measures two things
straight from the stored latents (no VAE, no decode):

  fidelity  cosine similarity between the pinned region of this clip and
            the parent slice it was pinned to (parent must be in the same
            folder). ~0.99+ means the conditioning was delivered and
            obeyed; low values mean the mechanism failed.
  seam      L2 of the adjacent-latent-step difference at the boundary
            where generated content meets the pinned run, as a ratio of
            the clip's median step difference. ~1x means motion flows
            through the seam; >>1x is a hard cut -- the model planted the
            anchor but did not steer toward it.

Usage:
    python -s h3/seam_report.py <folder-with-clips> [more folders...]
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from h3 import frames as fr, mctx
else:
    from . import frames as fr, mctx


def _stepdiffs(video):
    d = video[:, :, 1:] - video[:, :, :-1]
    return ((d ** 2).mean(dim=(0, 1, 3, 4))) ** 0.5


def _cos(x, y):
    import torch
    return torch.nn.functional.cosine_similarity(
        x.flatten(), y.flatten(), dim=0).item()


def _parent_slice(parent_video, spec):
    start = int(spec.get("source_start", 0))
    frames = int(spec.get("source_frames", 0))
    if start % fr.FRAMES_PER_GROUP != 0:
        return None
    k = start // fr.FRAMES_PER_GROUP * fr.LATENTS_PER_GROUP
    return parent_video[:, :, k:k + fr.frames_to_latents(frames)]


def report(folder):
    import torch  # noqa: F401  (import check before any work)

    names = sorted(n for n in os.listdir(folder)
                   if n.endswith(mctx.SIDECAR_SUFFIX))
    by_id = {}
    for n in names:
        try:
            by_id[mctx.read_header(os.path.join(folder, n))["self_id"]] = n
        except Exception:
            pass

    print(f"{'clip':16} {'relation':9} {'pin':>4} {'fidelity':>9} "
          f"{'seam':>7} {'verdict'}")
    for n in names:
        try:
            meta = mctx.read_header(os.path.join(folder, n))
        except Exception as e:
            print(f"{n:16} unreadable: {e}")
            continue
        rel = meta.get("relation")
        if rel not in ("extends", "prepends"):
            continue
        video, _audio, meta = mctx.load_sidecar(os.path.join(folder, n))
        video = video.float()
        head = int(meta.get("pinned_head_frames", 0))
        tail = int(meta.get("pinned_tail_frames", 0))
        lt = video.shape[2]
        if rel == "extends":
            seam = fr.frames_to_latents(head) - 1
            pinned = video[:, :, :fr.frames_to_latents(head)]
        else:
            seam = lt - fr.frames_to_latents(tail) - 1
            pinned = video[:, :, lt - fr.frames_to_latents(tail):]

        d = _stepdiffs(video)
        import torch
        typical = d[torch.arange(len(d)) != seam].median().item()
        ratio = d[seam].item() / typical if typical else float("nan")

        fidelity = ""
        pins = mctx.parse_pins(meta)
        parent_name = by_id.get(meta.get("parent_id"))
        if parent_name and len(pins) == 1:
            pv, _pa, _pm = mctx.load_sidecar(os.path.join(folder, parent_name))
            sl = _parent_slice(pv.float(), pins[0])
            if sl is not None and sl.shape == pinned.shape:
                fidelity = "%.4f" % _cos(pinned, sl)

        verdict = ("seamless" if ratio < 1.2 else
                   "soft bump" if ratio < 1.8 else "HARD CUT")
        parent = (parent_name or "<absent>").split(".")[0]
        print(f"{n.split('.')[0]:16} {rel:9} {head or tail:>4} "
              f"{fidelity or '-':>9} {ratio:>6.2f}x {verdict}"
              f"  (parent {parent})")


if __name__ == "__main__":
    folders = sys.argv[1:] or ["."]
    for f in folders:
        print(f"== {f}")
        report(f)
