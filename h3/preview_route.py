"""Stateless preview endpoint: build a temp smart-cut of a sequence.

POST /obvpm/h3/preview_cut  {"sequence": "<the Assemble node's text>",
                             "crf": 19}
->  {"filename", "subfolder", "type": "temp", "starts": [f...], "total"}

The timeline widget plays the returned file as ONE continuous stream --
the only way a preview can be truly gapless (two <video> elements can
never be sample-locked). For all-copyable sequences this is a packet
copy, sub-second; seams needing bridges decode only the bridged clips.
Results are cached in the temp folder keyed by the resolved plan + the
source files' size/mtime, so replays and repeated tweaks are instant.

This is a stateless utility route, NOT a review gate: it drives no
generation, holds no state, and keeps the loop-less design intact
(DESIGN.md section 1). Temp files vanish with ComfyUI's normal temp
cleanup.
"""

import hashlib
import json
import logging
import os
import tempfile

import folder_paths

from . import frames as fr
from . import nodes_assemble as na
from .nodes_save import H3SaveVideoWithMCtx

_LOG = logging.getLogger("obvpm.h3")

SUBFOLDER = "obvpm_h3_preview"


def _decode_audio(path):
    """Audio-only decode: (waveform [1,C,T] float tensor, rate) or None."""
    import av
    import torch
    with av.open(path) as c:
        if not c.streams.audio:
            return None
        s = c.streams.audio[0]
        sr = int(s.codec_context.sample_rate or 0)
        if not sr:
            return None
        ch = int(s.codec_context.channels or 2)
        layout = {1: "mono", 2: "stereo", 6: "5.1"}.get(ch, "stereo")
        res = av.audio.resampler.AudioResampler(
            format="fltp", layout=layout, rate=sr)
        chunks = []
        for frame in c.decode(s):
            for rf in res.resample(frame):
                chunks.append(torch.from_numpy(rf.to_ndarray()))
        for rf in res.resample(None):  # flush the resampler tail
            chunks.append(torch.from_numpy(rf.to_ndarray()))
        if not chunks:
            return None
        return {"waveform": torch.cat(chunks, dim=1).unsqueeze(0),
                "sample_rate": sr}


def _decode_images(path):
    from comfy_api.input_impl import VideoFromFile
    return VideoFromFile(path).get_components().images


def _clip_frames(e):
    if e["header"]:
        d = int(e["header"].get("delivered_frames", 0) or 0)
        if d:
            return d
    return na._scan_keyframes(e["path"])[1]


def build_preview(sequence, crf, probe=False):
    """Returns (filename, starts, total, cached).

    probe=True never builds: it resolves the sequence, computes the cache
    key and reports whether the preview already exists -- the widget's
    "full preview built?" state indicator.
    """
    import torch

    entries = na.resolve_sequence(sequence)

    played = []
    for e in entries:
        n = _clip_frames(e)
        hi = n if e["exit"] is None else min(e["exit"], n)
        if hi <= e["enter"]:
            raise ValueError(
                "preview: the seam cuts leave nothing of %s" % e["clip"])
        e["frames"] = n
        played.append(hi - e["enter"])
    starts, total = [], 0
    for p in played:
        starts.append(total)
        total += p

    key_src = [[e["clip"], os.stat(e["path"]).st_size,
                os.stat(e["path"]).st_mtime_ns, e["enter"], e["exit"]]
               for e in entries]
    key = hashlib.sha256(
        json.dumps([key_src, int(crf)]).encode()).hexdigest()[:16]
    tdir = os.path.join(folder_paths.get_temp_directory(), SUBFOLDER)
    os.makedirs(tdir, exist_ok=True)
    name = "preview_%s.mp4" % key
    out_path = os.path.join(tdir, name)
    if os.path.isfile(out_path):
        return name, starts, total, True
    if probe:
        return name, starts, total, False

    audio_parts, have_audio, sample_rate = [], True, None
    for e in entries:
        a = _decode_audio(e["path"]) if have_audio else None
        if a is None:
            have_audio = False
            continue
        if sample_rate is None:
            sample_rate = a["sample_rate"]
        elif a["sample_rate"] != sample_rate:
            have_audio = False
            continue
        hi = e["frames"] if e["exit"] is None else e["exit"]
        lo_s = round(e["enter"] / fr.FPS * sample_rate)
        hi_s = round(hi / fr.FPS * sample_rate)
        wav = a["waveform"]
        audio_parts.append(wav[..., lo_s:min(hi_s, wav.shape[-1])])
    audio = None
    if have_audio and audio_parts:
        audio = {"waveform": torch.cat(audio_parts, dim=-1),
                 "sample_rate": sample_rate}

    tmp = out_path + ".tmp.mp4"
    try:
        with tempfile.TemporaryDirectory() as btd:
            pieces = []
            for i, e in enumerate(entries):
                if e["enter"] == 0 and e["exit"] is None:
                    pieces.append({"path": e["path"], "from": 0,
                                   "to": e["frames"]})
                    continue
                kfs, n_total = na._scan_keyframes(e["path"])
                imgs = None
                for kind, a, b in na._plan_pieces(e["enter"], e["exit"],
                                                  kfs, n_total):
                    if kind == "copy":
                        pieces.append({"path": e["path"],
                                       "from": a, "to": b})
                        continue
                    if imgs is None:  # decode only clips that bridge
                        imgs = _decode_images(e["path"])
                    bp = os.path.join(btd, "bridge_%d_%d.mp4" % (i, a))
                    H3SaveVideoWithMCtx._encode_mp4(bp, imgs[a:b], None, crf)
                    pieces.append({"path": bp, "from": 0, "to": b - a})
            na._mux_pieces(pieces, tmp, audio)
            os.replace(tmp, out_path)
        _LOG.info("obvpm.h3: preview %s built (%d clips, %d frames)",
                  name, len(entries), total)
    except na._StreamMismatch as why:
        _LOG.warning("obvpm.h3: preview smart-cut not possible (%s); "
                     "re-encoding", why)
        try:
            os.remove(tmp)
        except OSError:
            pass
        frames = []
        for e in entries:
            imgs = _decode_images(e["path"])
            hi = imgs.shape[0] if e["exit"] is None else e["exit"]
            frames.append(imgs[e["enter"]:hi])
        H3SaveVideoWithMCtx._encode_mp4(
            out_path, torch.cat(frames, dim=0), audio, crf)
    return name, starts, total, True


def register():
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.post("/obvpm/h3/preview_cut")
    async def _preview_cut(request):
        import asyncio
        try:
            data = await request.json()
            name, starts, total, cached = await asyncio.to_thread(
                build_preview, str(data.get("sequence", "")),
                int(data.get("crf", 19)), bool(data.get("probe", False)))
            return web.json_response({
                "filename": name, "subfolder": SUBFOLDER, "type": "temp",
                "starts": starts, "total": total, "cached": cached,
            })
        except Exception as exc:
            _LOG.exception("obvpm.h3: preview_cut failed")
            return web.json_response({"error": str(exc)}, status=400)

    _LOG.info("obvpm.h3: preview route registered (/obvpm/h3/preview_cut)")
