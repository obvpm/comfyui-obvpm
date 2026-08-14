"""Stateless preview + export endpoints for the Timeline node.

POST /obvpm/h3/preview_cut  {"sequence": "<the Timeline node's text>",
                             "crf": 23, "base_folder": "h3",
                             "preview_filename": "obvpm_h3_preview",
                             "probe": false}
->  {"filename", "subfolder", "type": "output", "starts": [f...],
     "total", "cached", "v"}

POST /obvpm/h3/export  {"sequence", "crf", "base_folder",
                        "preview_filename",
                        "export_filename_prefix": "full"}
->  {"path": "<output-relative path of the written MP4>", "cached"}

The timeline widget plays the preview as ONE continuous stream -- the
only way a preview can be truly gapless (two <video> elements can never
be sample-locked). For all-copyable sequences this is a packet copy,
sub-second; seams needing bridges decode only the bridged clips.

The preview is a SINGLE file per Timeline node, at
output/<base_folder>/<preview_filename>.mp4, overwritten on every
build; a meta JSON beside it records the content key so staleness
survives page reloads AND ComfyUI restarts (it deliberately does NOT
live in temp, which is wiped at startup). Because the URL never changes
per build, players must cache-bust with the returned "v". Export =
promote: ensure the preview is current (rebuild if stale), then copy it
beside itself under export_filename_prefix with the normal counter --
the export is byte-identical to the preview by construction.

These are stateless utility routes, NOT review gates: they drive no
generation, hold no state, and keep the loop-less design intact
(DESIGN.md section 1).
"""

import hashlib
import json
import logging
import os
import shutil
import tempfile

import folder_paths

from . import frames as fr
from . import nodes_assemble as na
from .nodes_save import H3SaveVideoWithMCtx

_LOG = logging.getLogger("obvpm.h3")

DEFAULT_PREVIEW_NAME = "obvpm_h3_preview"


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


def _preview_paths(base_folder, preview_filename):
    """(mp4_path, meta_path, filename, subfolder) under the OUTPUT root.

    The preview lives beside the clips it cuts (base_folder), not in
    temp -- findable, and it survives restarts together with its meta.
    Refused if the combination escapes the output root (same traversal
    rule as the sequence paths).
    """
    name = str(preview_filename or DEFAULT_PREVIEW_NAME).strip().strip("/")
    sub = str(base_folder or "").strip().strip("/")
    rel = "%s/%s" % (sub, name) if sub else name
    root = os.path.abspath(folder_paths.get_output_directory())
    path = os.path.abspath(os.path.join(root, rel + ".mp4"))
    if os.path.commonpath([root, path]) != root:
        raise ValueError(
            "preview location escapes the output folder: %r" % rel)
    sub, name = os.path.split(os.path.relpath(path, root))
    return path, path + ".json", name, sub.replace(os.sep, "/")


def _read_meta(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_preview(sequence, crf, probe=False, base_folder="",
                  preview_filename=None):
    """Returns (filename, subfolder, starts, total, cached, key).

    One file per node at output/<base_folder>/<preview_filename>.mp4,
    overwritten on rebuild; the meta JSON beside it holds the content
    key, so `cached` means "the file on disk was built from exactly this
    sequence/crf/source state". probe=True never builds.
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
    out_path, meta_path, name, sub = _preview_paths(base_folder,
                                                    preview_filename)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    current = (os.path.isfile(out_path)
               and _read_meta(meta_path).get("key") == key)
    if current or probe:
        return name, sub, starts, total, current, key

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
        _LOG.info("obvpm.h3: preview built at %s (%d clips, %d frames)",
                  out_path, len(entries), total)
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
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"key": key, "starts": starts, "total": total}, f)
    return name, sub, starts, total, True, key


def export_cut(sequence, crf, base_folder, preview_filename,
               export_filename_prefix):
    """Promote the preview beside itself: (relative_path, was_cached).

    Rebuilds first when missing or stale, so the exported file is always
    the current sequence -- and byte-identical to the preview. Exports
    land in base_folder under export_filename_prefix with the usual
    counter.
    """
    name, sub, _, _, cached, _ = build_preview(
        sequence, crf, probe=False, base_folder=base_folder,
        preview_filename=preview_filename)
    src = os.path.join(folder_paths.get_output_directory(), sub, name)
    prefix = str(export_filename_prefix or "full").strip().strip("/")
    folder = str(base_folder or "").strip().strip("/")
    if folder:
        prefix = "%s/%s" % (folder, prefix)
    full_folder, base, counter, subfolder, _ = folder_paths.get_save_image_path(
        prefix, folder_paths.get_output_directory())
    out_name = "%s_%05d.mp4" % (base, counter)
    out_path = os.path.join(full_folder, out_name)
    shutil.copy2(src, out_path)
    rel = os.path.join(subfolder, out_name).replace(os.sep, "/").lstrip("/")
    _LOG.info("obvpm.h3: exported %s (%s)", rel,
              "promoted cached preview" if cached else "built fresh")
    return rel, cached


def register():
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.post("/obvpm/h3/preview_cut")
    async def _preview_cut(request):
        import asyncio
        try:
            data = await request.json()
            name, sub, starts, total, cached, key = await asyncio.to_thread(
                build_preview, str(data.get("sequence", "")),
                int(data.get("crf", 23)), bool(data.get("probe", False)),
                str(data.get("base_folder", "")),
                str(data.get("preview_filename", "")
                    or DEFAULT_PREVIEW_NAME))
            return web.json_response({
                "filename": name, "subfolder": sub, "type": "output",
                "starts": starts, "total": total, "cached": cached,
                "v": key,
            })
        except Exception as exc:
            _LOG.exception("obvpm.h3: preview_cut failed")
            return web.json_response({"error": str(exc)}, status=400)

    @PromptServer.instance.routes.post("/obvpm/h3/export")
    async def _export(request):
        import asyncio
        try:
            data = await request.json()
            rel, cached = await asyncio.to_thread(
                export_cut, str(data.get("sequence", "")),
                int(data.get("crf", 23)),
                str(data.get("base_folder", "")),
                str(data.get("preview_filename", "")
                    or DEFAULT_PREVIEW_NAME),
                str(data.get("export_filename_prefix", "") or "full"))
            return web.json_response({"path": rel, "cached": cached})
        except Exception as exc:
            _LOG.exception("obvpm.h3: export failed")
            return web.json_response({"error": str(exc)}, status=400)

    _LOG.info("obvpm.h3: preview + export routes registered "
              "(/obvpm/h3/preview_cut, /obvpm/h3/export)")
