"""H3LoadVideoWithMCtx: clip picker with sidecar verification (DESIGN.md 4).

Mirrors the core Load Video idea but browses the OUTPUT folder (where
takes land) and adds mctx awareness: when a sidecar pairs with the file
(hash-verified), the clip's full latents + header ride out on the MCTX
wire. No sidecar, or a failed pairing check, emits None -- route the
IMAGE/AUDIO outputs through H3MCtxFromFrames instead (Phase 2).
"""

import logging
import os

import folder_paths

from . import mctx

_LOG = logging.getLogger("obvpm.h3")

_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")
_SCAN_DEPTH = 3

# hash cache: {abs_path: (size, mtime_ns, sha256)}. Disposable memoization,
# never authority (DESIGN.md section 3).
_HASH_CACHE = {}


def _list_clips():
    """Video files under the output folder, relative paths, depth-limited."""
    root = folder_paths.get_output_directory()
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= _SCAN_DEPTH:
            dirnames[:] = []
        for f in filenames:
            if f.lower().endswith(_VIDEO_EXTS):
                p = f if rel == "." else os.path.join(rel, f)
                out.append(p.replace(os.sep, "/"))
    out.sort()
    return out


def _cached_hash(path):
    st = os.stat(path)
    key = os.path.abspath(path)
    hit = _HASH_CACHE.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    digest = mctx.hash_file(path)
    _HASH_CACHE[key] = (st.st_size, st.st_mtime_ns, digest)
    return digest


class H3LoadVideoWithMCtx:
    CATEGORY = "obvpm/h3"
    FUNCTION = "load"
    RETURN_TYPES = ("VIDEO", "IMAGE", "AUDIO", "MCTX")
    RETURN_NAMES = ("video", "images", "audio", "mctx")
    DESCRIPTION = (
        "Loads a clip from the output folder with motion-context "
        "awareness: when a hash-verified .mctx.safetensors sidecar pairs "
        "with the file, its latents + lineage ride out on the mctx output "
        "for exact latent-grade continuation. Without one (or when the "
        "video was re-encoded/swapped), mctx is None and only the pixel "
        "route is available."
    )
    OUTPUT_TOOLTIPS = (
        "The clip as a VIDEO object (core-compatible).",
        "Decoded frames.",
        "Decoded audio track.",
        "The clip's latents + header for the pins pipeline; None when no "
        "verified sidecar pairs with this file.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        clips = _list_clips()
        return {
            "required": {
                "clip": (clips if clips else [""], {
                    "tooltip": "A video in the output folder. Clips saved by "
                               "H3SaveVideoWithMCtx carry a sidecar and load "
                               "with exact continuation latents."}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, clip):
        path = os.path.join(folder_paths.get_output_directory(), clip)
        parts = []
        for p in (path, mctx.sidecar_path(path)):
            try:
                st = os.stat(p)
                parts.append("%d:%d" % (st.st_size, st.st_mtime_ns))
            except OSError:
                parts.append("absent")
        return "|".join(parts)

    @classmethod
    def VALIDATE_INPUTS(cls, clip):
        if not clip:
            return "no clip selected (the output folder has no videos)"
        path = os.path.join(folder_paths.get_output_directory(), clip)
        if not os.path.isfile(path):
            return "clip not found: %s" % path
        return True

    def load(self, clip):
        path = os.path.join(folder_paths.get_output_directory(), clip)
        from comfy_api.input_impl import VideoFromFile
        video = VideoFromFile(path)
        components = video.get_components()
        images, audio = components.images, components.audio

        bundle = None
        side = mctx.sidecar_path(path)
        if os.path.isfile(side):
            try:
                header = mctx.read_header(side)
                actual = _cached_hash(path)
                if header.get("self_id") != actual:
                    _LOG.warning(
                        "obvpm.h3: %s does not pair with its sidecar (the "
                        "video was re-encoded, edited or swapped since the "
                        "take was saved). Latent continuation refused; the "
                        "pixel route still works.", clip)
                else:
                    video_lat, audio_lat, header = mctx.load_sidecar(side)
                    bundle = mctx.make_mctx(actual, video_lat, audio_lat,
                                            header, origin="sampled")
                    _LOG.info(
                        "obvpm.h3: %s + verified sidecar (%s, %s frames "
                        "delivered)", clip, header.get("relation") or "root",
                        header.get("delivered_frames"))
            except Exception:
                _LOG.exception("obvpm.h3: failed to read sidecar %s; loading "
                               "the clip without it", side)
        return (video, images, audio, bundle)
