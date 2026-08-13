"""H3SaveVideoWithMCtx: MP4 + mctx sidecar, one transaction (DESIGN.md 2-4).

Owns its encode (core's PyAV H.264/AAC path, called by us) so the MP4 and
the sidecar are written by one node with one trustworthy pairing hash --
deliberately NOT downstream of VHS or another saver. Transaction order:
MP4 first -> hash it -> sidecar tmp + os.replace. Sidecar existence is
the commit point; a crash in between leaves a plain playable video.
"""

import logging
import os

import folder_paths

from . import frames as fr
from . import mctx
from .avpack import unpack_av

_LOG = logging.getLogger("obvpm.h3")

_VIDEO_EXTS = (".mp4",)


class H3SaveVideoWithMCtx:
    CATEGORY = "obvpm/h3"
    FUNCTION = "save"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    DESCRIPTION = (
        "Saves a finished H3 take as a clip pair: the MP4 plus a "
        ".mctx.safetensors sidecar holding the clip's full raw latents "
        "and lineage. Any clip saved here can later seed extensions via "
        "its sidecar. Wire the TRIMMED images/audio (the delivered clip) "
        "and the sampler's raw latent; wire the same pins wire that fed "
        "Apply so lineage is recorded. For the one-node version, use "
        "H3 MCtx Trim and Save Video."
    )
    OUTPUT_TOOLTIPS = ("Path of the written MP4; the sidecar sits next to it.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "samples": ("LATENT", {
                    "tooltip": "The sampler's RAW output latent for this take "
                               "(the same one you decode). Stored whole in "
                               "the sidecar; the pinned head/tail stay in it "
                               "and are mapped out via the header."}),
                "images": ("IMAGE", {
                    "tooltip": "Delivered frames (AFTER the trim node removed "
                               "pinned scaffolding)."}),
                "filename_prefix": ("STRING", {
                    "default": "h3/clip",
                    "tooltip": "Output path prefix, like core save nodes. "
                               "Numbering is appended automatically."}),
                "crf": ("INT", {
                    "default": 19, "min": 0, "max": 51,
                    "tooltip": "H.264 quality (lower = better, bigger). The "
                               "MP4 is the delivery copy; the sidecar keeps "
                               "the lossless latents regardless."}),
            },
            "optional": {
                "audio": ("AUDIO", {
                    "tooltip": "Delivered audio (after the same trim). Leave "
                               "unwired for a silent MP4; the sidecar still "
                               "stores the audio latent."}),
                "pins": ("PINS", {
                    "tooltip": "The resolved pins from H3MCtxApplyPins. "
                               "Carries the authoritative generation recipe "
                               "for the sidecar and derives the lineage "
                               "edge. Unconnected = root clip."}),
                "metadata": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "Optional provenance as JSON, stored verbatim "
                               "in the sidecar header (user_meta). Well-known "
                               "keys browser UI looks for: prompt, seed, "
                               "steps, refs_note (nothing can fingerprint "
                               "reference images automatically -- a refs_note "
                               "is the mitigation). Any other keys are yours. "
                               "Empty is fine; invalid JSON is refused."}),
            },
        }

    def save(self, images, samples, filename_prefix, crf,
             audio=None, pins=None, metadata=""):
        video_lat, audio_lat = unpack_av(samples, name="samples")
        raw_steps = int(video_lat.shape[2])
        raw_frames = fr.pixel_frames(raw_steps)
        width = int(video_lat.shape[4]) * 16
        height = int(video_lat.shape[3]) * 16

        delivered = int(images.shape[0])
        if (int(images.shape[2]), int(images.shape[1])) != (width, height):
            raise ValueError(
                "H3SaveVideoWithMCtx: images are %dx%d but the latent decodes "
                "to %dx%d. Wire the images decoded from this same latent."
                % (int(images.shape[2]), int(images.shape[1]), width, height))

        from .nodes_pins import pins_trim_totals
        head, tail = pins_trim_totals(pins)
        if delivered != raw_frames - head - tail:
            raise ValueError(
                "H3SaveVideoWithMCtx: %d delivered frames but the raw latent "
                "covers %d and the pin specs account for %d pinned head + %d "
                "pinned tail frames (expected %d delivered). Wire the images "
                "through the trim node with the trim counts from Apply, and "
                "wire the SAME pin_specs here."
                % (delivered, raw_frames, head, tail, raw_frames - head - tail))

        user_meta = (metadata or "").strip()
        if user_meta:
            import json
            try:
                json.loads(user_meta)
            except ValueError as exc:
                raise ValueError(
                    "H3SaveVideoWithMCtx: metadata is not valid JSON (%s). "
                    "Leave it empty or pass a JSON object, e.g. "
                    "{\"prompt\": \"...\", \"seed\": 7}." % exc)

        specs_public = [dict(p.get("spec") or {}) for p in (pins or [])]
        relation, parent_id, join = mctx.summarize_pins(specs_public)

        full_folder, filename, counter, subfolder, _ = \
            folder_paths.get_save_image_path(
                filename_prefix, folder_paths.get_output_directory(),
                width, height)
        file = "%s_%05d.mp4" % (filename, counter)
        video_path = os.path.join(full_folder, file)

        self._encode_mp4(video_path, images, audio, crf)
        self_id = mctx.hash_file(video_path)

        meta = {
            "format": mctx.FORMAT,
            "self_id": self_id,
            "parent_id": parent_id,
            "relation": relation,
            "parent_join_frame": str(join),
            "width": str(width),
            "height": str(height),
            "fps": str(fr.FPS),
            "raw_frames": str(raw_frames),
            "pinned_head_frames": str(head),
            "pinned_tail_frames": str(tail),
            "delivered_frames": str(delivered),
            "pins": mctx.serialize_pins(specs_public),
            "user_meta": user_meta,
        }
        sidecar = mctx.write_sidecar(
            mctx.sidecar_path(video_path), video_lat, audio_lat, meta)
        _LOG.info("obvpm.h3: saved take %s (+ sidecar %s): %d delivered of "
                  "%d raw frames, %s%s", video_path, os.path.basename(sidecar),
                  delivered, raw_frames,
                  relation or "root",
                  (" <- %s..." % parent_id[:12]) if parent_id else "")
        return (video_path,)

    @staticmethod
    def _encode_mp4(path, images, audio, crf):
        from comfy_api.input_impl import VideoFromComponents
        from comfy_api.util import VideoComponents
        from fractions import Fraction
        components = VideoComponents(
            images=images, audio=audio, frame_rate=Fraction(fr.FPS))
        VideoFromComponents(components).save_to(path, crf=float(crf))


class H3SaveMCtxForVideo:
    """Sidecar-only writer: pair an mctx with a video saved by ANY node.

    The escape hatch for users who prefer their own video saver (VHS
    Video Combine for speed/format options, hardware encoders, etc.):
    wire that saver's output path here and this node hashes the written
    file and commits the sidecar next to it. The pairing hash is exactly
    as trustworthy as the all-in-one nodes; what this path cannot fully
    guarantee is that the video CONTENT came from `samples`, so it
    validates what the container cheaply reveals (resolution and frame
    count against the latent + pins) and refuses on mismatch.
    """

    CATEGORY = "obvpm/h3"
    FUNCTION = "save_sidecar"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("sidecar_path",)
    DESCRIPTION = (
        "Writes ONLY the .mctx.safetensors sidecar, paired to a video "
        "some other node already saved (e.g. VHS Video Combine -- wire "
        "its filenames output here). Use this when you want a specific "
        "video saver; the all-in-one Save/TrimAndSave nodes remain the "
        "simplest guaranteed-consistent route. The video must be the "
        "DELIVERED (trimmed) clip."
    )
    OUTPUT_TOOLTIPS = ("Path of the written sidecar.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_path": ("STRING", {
                    "default": "",
                    "tooltip": "Path of the saved video: absolute, or "
                               "relative to the output folder. Accepts a "
                               "VHS filenames output wired via any "
                               "to-string conversion; the LAST path is "
                               "used."}),
                "samples": ("LATENT", {
                    "tooltip": "The sampler's RAW output latent for this "
                               "take (same as the all-in-one Save)."}),
            },
            "optional": {
                "pins": ("PINS", {
                    "tooltip": "The resolved pins from H3MCtxApplyPins. "
                               "Unconnected = root clip."}),
                "metadata": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "Optional provenance JSON, stored verbatim "
                               "as user_meta (see the all-in-one Save)."}),
            },
        }

    def save_sidecar(self, video_path, samples, pins=None, metadata=""):
        path = self._resolve_path(video_path)
        video_lat, audio_lat = unpack_av(samples, name="samples")
        raw_steps = int(video_lat.shape[2])
        raw_frames = fr.pixel_frames(raw_steps)
        width = int(video_lat.shape[4]) * 16
        height = int(video_lat.shape[3]) * 16

        from .nodes_pins import pins_trim_totals
        head, tail = pins_trim_totals(pins)
        expect_delivered = raw_frames - head - tail

        vid_w, vid_h, vid_frames = self._probe(path)
        if (vid_w, vid_h) != (width, height):
            raise ValueError(
                "H3SaveMCtxForVideo: %s is %dx%d but the latent decodes to "
                "%dx%d. This video was not rendered from these samples."
                % (path, vid_w, vid_h, width, height))
        if vid_frames and vid_frames != expect_delivered:
            raise ValueError(
                "H3SaveMCtxForVideo: %s has %d frames but %d delivered "
                "frames were expected (%d raw - %d pinned head - %d pinned "
                "tail). Save the TRIMMED clip, and wire the same pins."
                % (path, vid_frames, expect_delivered, raw_frames, head, tail))
        if not vid_frames:
            _LOG.warning("obvpm.h3: could not determine %s's frame count; "
                         "skipping the delivered-frames check", path)

        user_meta = (metadata or "").strip()
        if user_meta:
            import json
            try:
                json.loads(user_meta)
            except ValueError as exc:
                raise ValueError(
                    "H3SaveMCtxForVideo: metadata is not valid JSON (%s)."
                    % exc)

        specs_public = [dict(p.get("spec") or {}) for p in (pins or [])]
        relation, parent_id, join = mctx.summarize_pins(specs_public)
        self_id = mctx.hash_file(path)
        meta = {
            "format": mctx.FORMAT,
            "self_id": self_id,
            "parent_id": parent_id,
            "relation": relation,
            "parent_join_frame": str(join),
            "width": str(width),
            "height": str(height),
            "fps": str(fr.FPS),
            "raw_frames": str(raw_frames),
            "pinned_head_frames": str(head),
            "pinned_tail_frames": str(tail),
            "delivered_frames": str(expect_delivered),
            "pins": mctx.serialize_pins(specs_public),
            "user_meta": user_meta,
        }
        sidecar = mctx.write_sidecar(
            mctx.sidecar_path(path), video_lat, audio_lat, meta)
        _LOG.info("obvpm.h3: paired sidecar %s to externally saved %s (%s)",
                  os.path.basename(sidecar), path, relation or "root")
        return (sidecar,)

    @staticmethod
    def _resolve_path(video_path):
        raw = video_path
        # tolerate VHS-style filenames structures stringified or wired
        if isinstance(raw, (tuple, list)):
            if len(raw) == 2 and isinstance(raw[1], (tuple, list)):
                raw = raw[1]
            raw = raw[-1] if raw else ""
        p = str(raw).strip().strip('"').strip("'")
        if not p:
            raise ValueError("H3SaveMCtxForVideo: video_path is empty.")
        candidates = [p, os.path.join(folder_paths.get_output_directory(), p)]
        for c in candidates:
            if os.path.isfile(c):
                return c
        raise ValueError(
            "H3SaveMCtxForVideo: no file at %r (tried absolute and "
            "output-relative). Wire the path your video saver actually "
            "wrote." % p)

    @staticmethod
    def _probe(path):
        """(width, height, frame_count) from the container, cheaply."""
        import av
        with av.open(path) as container:
            stream = container.streams.video[0]
            w = int(stream.codec_context.width)
            h = int(stream.codec_context.height)
            frames = int(stream.frames or 0)
            if not frames and stream.duration and stream.average_rate:
                frames = int(round(
                    float(stream.duration * stream.time_base)
                    * float(stream.average_rate)))
        return w, h, frames


class H3TrimAndSaveVideoWithMCtx:
    """H3TrimPinned + H3SaveVideoWithMCtx in one node: the common case.

    Takes the UNTRIMMED decode output plus the pins wire, removes the
    pinned scaffolding itself, then runs the exact save transaction. The
    separate Trim/Save nodes remain for graphs that want the delivered
    frames mid-graph (preview, post-processing before save).
    """

    CATEGORY = "obvpm/h3"
    FUNCTION = "trim_and_save"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "IMAGE", "AUDIO")
    RETURN_NAMES = ("path", "images", "audio")
    DESCRIPTION = (
        "Trims the pinned scaffolding off a decoded H3 take and saves the "
        "clip pair (MP4 + mctx sidecar) in one step. Wire the RAW decode "
        "output (untrimmed images/audio), the sampler's raw latent, and "
        "the pins wire that fed Apply. For a root clip (no pins), it "
        "saves as-is."
    )
    OUTPUT_TOOLTIPS = (
        "Path of the written MP4; the sidecar sits next to it.",
        "The delivered (trimmed) frames, for preview.",
        "The delivered (trimmed) audio, for preview.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        base = H3SaveVideoWithMCtx.INPUT_TYPES()
        req = dict(base["required"])
        req["images"] = ("IMAGE", {
            "tooltip": "The decode output, UNTRIMMED -- this node removes "
                       "the pinned scaffolding itself using the pins wire."})
        opt = dict(base["optional"])
        opt["audio"] = ("AUDIO", {
            "tooltip": "The decoded audio, untrimmed. Trimmed here in lock "
                       "step with the frames and tail-matched to exactly "
                       "frames/fps."})
        return {"required": req, "optional": opt}

    def trim_and_save(self, images, samples, filename_prefix, crf,
                      audio=None, pins=None, metadata=""):
        from .nodes_pins import H3TrimPinned
        if pins:
            images, audio = H3TrimPinned().trim(images, pins, audio=audio)
        (path,) = H3SaveVideoWithMCtx().save(
            images, samples, filename_prefix, crf,
            audio=audio, pins=pins, metadata=metadata)
        return (path, images, audio)
