"""H3Assemble: sequence clips into one MP4, seams derived from sidecars.

The assembler's job is the last mile of the lineage system: the sidecar
headers already record who joins whom and at which raw frame, so given an
ORDERED list of clips this node derives every seam cut itself:

  R extends L   ->  L exits at (R.join_raw - L.pinned_head); R enters at 0.
                    A plain tail extend puts that exit exactly at L's
                    delivered end (no cut); a trim-point extension or the
                    auto-shifted extend of a prepend-made clip lands the
                    exit earlier, cutting the frames that belong to the
                    other seam.
  L prepends R  ->  L plays whole; R enters at (L.join_raw - R.pinned_head).
                    Prepend to a root enters at 0; prepend to a
                    continuation enters at the conserved 12-frame offset.
  no relation   ->  butt join (L whole, R from 0), with a warning.

Writing is smart-cut: every clip's played range [enter, exit) is split
into COPY runs -- packet spans that start at an IDR keyframe and end at
one (or at the clip end), stream-copied bit for bit -- and BRIDGE runs,
the sub-GOP remainders around a mid-clip seam, re-encoded from the
already-decoded frames with the same PyAV H.264 path the save nodes use.
A whole-clip sequence is therefore 100% copied (zero video generation
loss); a mid-clip seam re-encodes only the frames between the cut and the
nearest keyframe on its side. Since our saves use x264's default GOP
(~250 frames), short takes carry a single IDR at frame 0 and a mid-clip
entry re-encodes that one clip; longer takes with interior keyframes get
true sub-clip copying automatically.

Audio is decoded, concatenated sample-accurately and encoded ONCE as a
continuous AAC track regardless (copying AAC packets across a splice
always clicks: each frame overlap-adds with its neighbor, so the first
~21ms after a splice decodes with a fade-in artifact).

The whole path is defensive, because splicing packets from different
encodes is exactly where players glitch: every piece (bridges included)
must match the first clip's codec/extradata/dimensions/time base byte for
byte, frame counts are verified per piece, and DTS monotonicity is
enforced while muxing. Any surprise raises _StreamMismatch and the node
falls back to one full decode -> re-encode rather than writing a broken
file. The source takes remain the masters either way.

No sidecar is written: an assembly is a delivery, not a take -- it has no
latents of its own to store.
"""

import logging
import os
import re

import folder_paths

from . import frames as fr
from . import mctx

_LOG = logging.getLogger("obvpm.h3")

_LINE_RE = re.compile(r"^(?P<path>.+?)(?:\s*@\s*(?P<enter>\d+))?$")


def _parse_sequence(sequence):
    """Lines of "clip/path.mp4" or "clip/path.mp4 @ enter_frame"."""
    out = []
    for raw in (sequence or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        enter = m.group("enter")
        out.append({"clip": m.group("path").strip(),
                    "enter_override": None if enter is None else int(enter)})
    return out


def _read_verified_header(path):
    """The clip's sidecar header, hash-verified; None when unusable."""
    side = mctx.sidecar_path(path)
    if not os.path.isfile(side):
        return None
    try:
        header = mctx.read_header(side)
        from .nodes_load import _cached_hash
        if header.get("self_id") != _cached_hash(path):
            _LOG.warning("obvpm.h3: %s does not pair with its sidecar; "
                         "assembling it as a plain video (butt joins)",
                         os.path.basename(path))
            return None
        return header
    except Exception:
        _LOG.exception("obvpm.h3: unreadable sidecar for %s; assembling it "
                       "as a plain video", os.path.basename(path))
        return None


def _derive_seam(left, right):
    """(left_exit_delivered_frame_or_None, right_enter_frame, note).

    None exit = play the left clip to its delivered end.
    """
    lh, rh = left.get("header"), right.get("header")
    if not lh or not rh:
        return None, 0, "no sidecar data: butt join"
    l_id, r_id = lh.get("self_id"), rh.get("self_id")

    if rh.get("relation") == "extends" and rh.get("parent_id") == l_id:
        join = int(rh.get("parent_join_frame", 0) or 0)
        exit_f = join - int(lh.get("pinned_head_frames", 0) or 0)
        delivered = int(lh.get("delivered_frames", 0) or 0)
        if not 0 < exit_f <= delivered:
            raise ValueError(
                "H3Assemble: %s extends %s at raw frame %d, which maps "
                "outside the parent's %d delivered frames. The sidecars "
                "disagree with themselves; re-check the takes."
                % (right["clip"], left["clip"], join, delivered))
        note = ("extends: seamless" if exit_f == delivered else
                "extends at a cut: %s exits at frame %d (%d frames held "
                "back)" % (left["clip"], exit_f, delivered - exit_f))
        return (None if exit_f == delivered else exit_f), 0, note

    if lh.get("relation") == "prepends" and lh.get("parent_id") == r_id:
        join = int(lh.get("parent_join_frame", 0) or 0)
        enter = join - int(rh.get("pinned_head_frames", 0) or 0)
        delivered = int(rh.get("delivered_frames", 0) or 0)
        if not 0 <= enter < delivered:
            raise ValueError(
                "H3Assemble: %s prepends %s at raw frame %d, which maps "
                "outside the target's %d delivered frames. The sidecars "
                "disagree with themselves; re-check the takes."
                % (left["clip"], right["clip"], join, delivered))
        note = ("prepends: seamless" if enter == 0 else
                "prepends at the conserved offset: %s enters at frame %d"
                % (right["clip"], enter))
        return None, enter, note

    return None, 0, "no recorded relation: butt join"


def resolve_sequence(sequence):
    """sequence text -> entries with path/header/enter/exit, seams derived.

    Shared by the Assemble node and the preview route; the traversal
    guard matters for the latter (the route accepts arbitrary text).
    """
    root = os.path.abspath(folder_paths.get_output_directory())
    entries = _parse_sequence(sequence)
    if not entries:
        raise ValueError(
            "H3Assemble: the sequence is empty. List one clip per "
            "line, in playback order.")
    for e in entries:
        e["path"] = os.path.abspath(os.path.join(root, e["clip"]))
        if not e["path"].startswith(root + os.sep):
            raise ValueError(
                "H3Assemble: %s escapes the output folder" % e["clip"])
        if not os.path.isfile(e["path"]):
            raise ValueError("H3Assemble: clip not found: %s" % e["clip"])
        e["header"] = _read_verified_header(e["path"])

    # seams: enter[i] from the join with the left neighbor, exit[i]
    # from the join with the right neighbor (None = delivered end)
    for e in entries:
        e["enter"], e["exit"] = 0, None
    for left, right in zip(entries, entries[1:]):
        exit_f, enter_f, note = _derive_seam(left, right)
        if left["exit"] is None:
            left["exit"] = exit_f
        right["enter"] = enter_f
        _LOG.debug("obvpm.h3: seam %s | %s -- %s",
                  left["clip"], right["clip"], note)
    for e in entries:
        if e["enter_override"] is not None:
            e["enter"] = e["enter_override"]
            _LOG.info("obvpm.h3: %s enters at frame %d (manual override)",
                      e["clip"], e["enter"])
    return entries


class _StreamMismatch(Exception):
    """Pieces cannot be spliced losslessly; fall back to full re-encode."""


def _video_stream_info(container, path):
    """(stream, pts_step, base_pts) with CFR sanity checks."""
    v = container.streams.video[0]
    rate = v.average_rate or fr.FPS
    step = int(round((1 / rate) / v.time_base))
    if step <= 0:
        raise _StreamMismatch("cannot derive a frame step from %s"
                              % os.path.basename(path))
    return v, step


def _scan_keyframes(path):
    """(keyframe frame indices, total frames) of the clip's video stream.

    Frame indices are display positions derived from pts on the CFR grid;
    anything that does not divide evenly is not a clip our encoder wrote.
    """
    import av
    with av.open(path) as c:
        v, step = _video_stream_info(c, path)
        pts = []
        kf_pts = []
        for packet in c.demux(v):
            if packet.dts is None:
                continue
            pts.append(packet.pts)
            if packet.is_keyframe:
                kf_pts.append(packet.pts)
    if not pts:
        raise _StreamMismatch("no video packets in %s"
                              % os.path.basename(path))
    base = min(pts)
    kfs = []
    for p in sorted(kf_pts):
        if (p - base) % step:
            raise _StreamMismatch("%s is not on a uniform frame grid"
                                  % os.path.basename(path))
        kfs.append((p - base) // step)
    return kfs, len(pts)


def _plan_pieces(enter, exit_f, keyframes, n_total):
    """Split [enter, exit) into ("copy"|"bridge", from, to) runs.

    Copy runs must START at an IDR (decoding joins the stream there) and
    STOP at one (dropping a closed GOP's tail packets is only safe at the
    next IDR boundary) -- or at the clip end. Everything else bridges.
    """
    end = n_total if exit_f is None else exit_f
    kin = next((k for k in keyframes if k >= enter), None)
    if end == n_total:
        kout = n_total
    else:
        kout = max((k for k in keyframes if k <= end), default=None)
    if kin is None or kout is None or kin >= kout:
        return [("bridge", enter, end)]
    pieces = []
    if kin > enter:
        pieces.append(("bridge", enter, kin))
    pieces.append(("copy", kin, kout))
    if kout < end:
        pieces.append(("bridge", kout, end))
    return pieces


def _mux_pieces(pieces, out_path, audio):
    """Splice piece runs into one MP4: copy packets, one audio encode.

    pieces: [{"path", "from", "to"}] -- "from"/"to" are frame indices
    into that file; bridges arrive as whole little files (from 0 to
    their length). Every piece must share the first piece's stream
    signature; per-piece frame counts and DTS monotonicity are verified,
    and any violation raises _StreamMismatch (caller falls back).
    """
    import av

    def sig(c):
        v = c.streams.video[0]
        cc = v.codec_context
        return (cc.name, bytes(cc.extradata or b""), cc.width,
                cc.height, v.time_base)

    with av.open(out_path, mode="w") as out:
        vout = aout = layout = None
        ref = None
        offset = 0
        last_dts = None
        for piece in pieces:
            with av.open(piece["path"]) as c:
                if ref is None:
                    ref = sig(c)
                    vout = out.add_stream_from_template(
                        template=c.streams.video[0])
                    if audio is not None:
                        wave = audio["waveform"]
                        wave = wave[0] if wave.dim() == 3 else wave
                        layout = {1: "mono", 2: "stereo", 6: "5.1"}.get(
                            int(wave.shape[0]), "stereo")
                        aout = out.add_stream(
                            "aac", rate=int(audio["sample_rate"]),
                            layout=layout)
                elif sig(c) != ref:
                    raise _StreamMismatch(
                        "%s was encoded with different stream parameters"
                        % os.path.basename(piece["path"]))

                v, step = _video_stream_info(c, piece["path"])
                base = None
                shift = None
                copying = piece["from"] == 0
                want = piece["to"] - piece["from"]
                n = 0
                for packet in c.demux(v):
                    if packet.dts is None:
                        continue
                    if base is None:
                        base = packet.pts  # first packet is the first IDR
                    frame = (packet.pts - base) // step
                    if packet.is_keyframe:
                        if not copying and frame == piece["from"]:
                            copying = True
                        elif copying and frame >= piece["to"]:
                            break
                    if not copying:
                        continue
                    if shift is None:
                        # rebase the run so it looks like a fresh file:
                        # first display frame at pts 0, dts keeping its
                        # native B-frame lead
                        shift = base + piece["from"] * step
                    packet.stream = vout
                    packet.pts += offset - shift
                    packet.dts += offset - shift
                    if last_dts is not None and packet.dts <= last_dts:
                        raise _StreamMismatch(
                            "non-monotonic DTS at the splice into %s"
                            % os.path.basename(piece["path"]))
                    last_dts = packet.dts
                    out.mux(packet)
                    n += 1
                if n != want:
                    raise _StreamMismatch(
                        "expected %d frames from %s [%d..%d), muxed %d"
                        % (want, os.path.basename(piece["path"]),
                           piece["from"], piece["to"], n))
                offset += want * step

        if aout is not None:
            frame = av.AudioFrame.from_ndarray(
                wave.float().cpu().contiguous().numpy(),
                format="fltp", layout=layout)
            frame.sample_rate = int(audio["sample_rate"])
            frame.pts = 0
            out.mux(aout.encode(frame))
            out.mux(aout.encode())


class H3Timeline:
    """The timeline hub: composes, previews and exports -- never executes.

    All the work happens through the timeline widget (web/h3_mctx_ui.js)
    and the stateless routes (preview_route.py): the full preview is a
    server-built smart cut, and the export button promotes that exact
    file into output/. The node has no outputs and is not an output
    node, so a workflow run never touches it -- queueing a generation
    while composing costs nothing. For an in-graph assembled result
    (feed frames onward in one run), use H3 MCtx Assemble instead.
    """

    CATEGORY = "obvpm/h3"
    FUNCTION = "noop"
    RETURN_TYPES = ()
    DESCRIPTION = (
        "Timeline editor for composed clips: seam-aware blocks, "
        "drag-reorder, seamless server-built preview, and an export "
        "button that writes the current cut to the output folder "
        "(byte-identical to the full preview). Never runs as part of a "
        "workflow -- previewing and exporting are button-driven."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "sequence": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "One clip per line, in playback order, as "
                               "output-relative paths (the same values the "
                               "H3 loaders list), e.g. h3/clip_00001.mp4. "
                               "Append ' @ N' to force a clip to enter at "
                               "delivered frame N instead of the derived "
                               "seam. Lines starting with # are ignored."}),
                "base_folder": ("STRING", {
                    "default": "",
                    "tooltip": "Output-relative folder this node works "
                               "in (e.g. h3): scopes the clip picker and "
                               "seam suggestions, and holds the preview "
                               "file and exports. Empty = the output "
                               "root."}),
                "preview_filename": ("STRING", {
                    "default": "obvpm_h3_preview",
                    "tooltip": "Name (no extension) of the single "
                               "preview file, written into base_folder "
                               "and overwritten on every full build. "
                               "Give each Timeline node its own name if "
                               "you use several."}),
                "export_filename_prefix": ("STRING", {
                    "default": "full",
                    "tooltip": "Filename prefix for the export button; "
                               "written into base_folder with the usual "
                               "counter, like core save nodes."}),
                "crf": ("INT", {
                    "default": 23, "min": 0, "max": 51,
                    "tooltip": "H.264 quality for re-encoded seam bridges "
                               "and the export (lower = better, bigger)."}),
                "auto_add": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "When a run produces a clip that extends "
                               "the LAST clip in the timeline, append it "
                               "automatically. Anything else a run "
                               "produces (prepends, branches off earlier "
                               "clips, roots) is only offered as a chip "
                               "below the timeline."}),
            },
        }

    def noop(self):
        return ()


class H3Assemble:
    CATEGORY = "obvpm/h3"
    FUNCTION = "assemble"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING", "IMAGE", "AUDIO")
    RETURN_NAMES = ("path", "images", "audio")
    DESCRIPTION = (
        "Plays an ordered list of clips as one MP4, deriving every seam "
        "cut from the mctx sidecars: extend seams butt seamlessly, "
        "trim-point and prepend seams enter/exit at the recorded join "
        "(including the 12-frame offset of continuation prepends). "
        "Writes by SMART-CUT: video packets are stream-copied bit for "
        "bit wherever possible, and only the sub-GOP frames around a "
        "mid-clip seam are re-encoded (with crf); audio is encoded once "
        "as a continuous track. The source takes remain the masters."
    )
    OUTPUT_TOOLTIPS = (
        "Path of the written MP4.",
        "The assembled frames.",
        "The assembled audio (present when every clip carries audio).",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "sequence": ("STRING", {
                    "default": "", "multiline": True,
                    "tooltip": "One clip per line, in playback order, as "
                               "output-relative paths (the same values the "
                               "H3 loaders list), e.g. h3/clip_00001.mp4. "
                               "Append ' @ N' to force a clip to enter at "
                               "delivered frame N instead of the derived "
                               "seam. Lines starting with # are ignored."}),
                "filename_prefix": ("STRING", {
                    "default": "h3/cut",
                    "tooltip": "Output path prefix, like core save nodes."}),
                "crf": ("INT", {
                    "default": 19, "min": 0, "max": 51,
                    "tooltip": "H.264 quality (lower = better, bigger)."}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, sequence, **_):
        root = folder_paths.get_output_directory()
        parts = []
        for entry in _parse_sequence(sequence):
            p = os.path.join(root, entry["clip"])
            for f in (p, mctx.sidecar_path(p)):
                try:
                    st = os.stat(f)
                    parts.append("%d:%d" % (st.st_size, st.st_mtime_ns))
                except OSError:
                    parts.append("absent")
        return "|".join(parts)

    def assemble(self, sequence, filename_prefix, crf):
        import torch

        root = folder_paths.get_output_directory()
        entries = resolve_sequence(sequence)

        from comfy_api.input_impl import VideoFromFile
        frames_parts, audio_parts, sample_rate = [], [], None
        have_audio = True
        size = None
        for e in entries:
            comps = VideoFromFile(e["path"]).get_components()
            images = comps.images
            n = int(images.shape[0])
            lo = min(e["enter"], n)
            hi = n if e["exit"] is None else min(e["exit"], n)
            if hi <= lo:
                raise ValueError(
                    "H3Assemble: the seam cuts leave nothing of %s "
                    "(frames %d..%d of %d)." % (e["clip"], lo, hi, n))
            this = (int(images.shape[2]), int(images.shape[1]))
            if size is None:
                size = this
            elif this != size:
                raise ValueError(
                    "H3Assemble: %s is %dx%d but the sequence started at "
                    "%dx%d. Clips must share one resolution."
                    % (e["clip"], this[0], this[1], size[0], size[1]))
            frames_parts.append(images[lo:hi])

            wav = (comps.audio or {}).get("waveform") \
                if isinstance(comps.audio, dict) else None
            if wav is None:
                have_audio = False
            elif have_audio:
                sr = int(comps.audio["sample_rate"])
                if sample_rate is None:
                    sample_rate = sr
                elif sr != sample_rate:
                    _LOG.warning("obvpm.h3: %s has sample rate %d, sequence "
                                 "started at %d; dropping audio from the "
                                 "assembly", e["clip"], sr, sample_rate)
                    have_audio = False
                if have_audio:
                    a_lo = round(lo / fr.FPS * sample_rate)
                    a_hi = round(hi / fr.FPS * sample_rate)
                    audio_parts.append(wav[..., a_lo:min(a_hi, wav.shape[-1])])

            _LOG.info("obvpm.h3: + %s frames %d..%d (%d of %d)",
                      e["clip"], lo, hi, hi - lo, n)

        images = torch.cat(frames_parts, dim=0)
        audio = None
        if have_audio and audio_parts:
            audio = {"waveform": torch.cat(audio_parts, dim=-1),
                     "sample_rate": sample_rate}

        full_folder, filename, counter, _subfolder, _ = \
            folder_paths.get_save_image_path(
                filename_prefix, root, size[0], size[1])
        out_path = os.path.join(full_folder, "%s_%05d.mp4"
                                % (filename, counter))

        # smart-cut: plan copy/bridge runs per clip, splice packets. Any
        # _StreamMismatch (foreign encode, odd timestamps, bridge params
        # not byte-matching) falls back to one full re-encode.
        import tempfile
        from .nodes_save import H3SaveVideoWithMCtx
        wrote = None
        tmp = out_path + ".tmp.mp4"
        try:
            with tempfile.TemporaryDirectory() as btd:
                pieces, n_copy, n_bridge = [], 0, 0
                for i, (e, part) in enumerate(zip(entries, frames_parts)):
                    enter, exit_f = e["enter"], e["exit"]
                    if enter == 0 and exit_f is None:
                        pieces.append({"path": e["path"], "from": 0,
                                       "to": int(part.shape[0])})
                        n_copy += int(part.shape[0])
                        continue
                    kfs, n_total = _scan_keyframes(e["path"])
                    for kind, a, b in _plan_pieces(enter, exit_f, kfs,
                                                   n_total):
                        if kind == "copy":
                            pieces.append({"path": e["path"],
                                           "from": a, "to": b})
                            n_copy += b - a
                        else:
                            bp = os.path.join(btd,
                                              "bridge_%d_%d.mp4" % (i, a))
                            H3SaveVideoWithMCtx._encode_mp4(
                                bp, part[a - enter:b - enter], None, crf)
                            pieces.append({"path": bp, "from": 0,
                                           "to": b - a})
                            n_bridge += b - a
                            _LOG.info("obvpm.h3: bridge for %s frames "
                                      "%d..%d (no keyframe at the cut)",
                                      e["clip"], a, b)
                _mux_pieces(pieces, tmp, audio)
                os.replace(tmp, out_path)
            wrote = ("video lossless" if n_bridge == 0 else
                     "smart-cut: %d frames copied, %d re-encoded"
                     % (n_copy, n_bridge))
        except _StreamMismatch as why:
            _LOG.warning("obvpm.h3: smart-cut not possible (%s); "
                         "re-encoding everything", why)
            try:
                os.remove(tmp)
            except OSError:
                pass
        if wrote is None:
            H3SaveVideoWithMCtx._encode_mp4(out_path, images, audio, crf)
            wrote = "re-encoded"

        _LOG.info("obvpm.h3: assembled %d clips -> %s (%d frames, %.1fs%s, "
                  "%s)", len(entries), out_path, int(images.shape[0]),
                  images.shape[0] / fr.FPS,
                  ", audio" if audio is not None else ", silent", wrote)
        return (out_path, images, audio)
