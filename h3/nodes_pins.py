"""The pins pipeline: decide -> slice+execute (DESIGN.md section 4).

  H3MCtxPinSpec     spec builder: pure data, no tensor work
  H3MCtxApplyPins   slices (sole frame<->latent math site, via
                    _prepare_pins) and feeds pinned runs to the vendored
                    patches; outputs the resolved pins wire
  H3TrimPinned      removes pinned scaffolding from the decoded output

Phase 1 scope: a single `before` pin sourced from a clip's tail -- the
extend case. The wire formats (PINSPECS, PINS) already carry the general
shape (placement, multiple pins, image sources) so later phases add
capability without changing these interfaces.
"""

import logging

import node_helpers

from . import frames as fr
from .avpack import unpack_av
from .vendor.patch_layout import (
    MC_KEY,
    MC_AUDIO_KEY,
    apply_patch as _apply_layout_patch,
    is_applied as _layout_patch_applied,
)
from .vendor.patch_payload import (
    apply_patch as _apply_payload_patch,
    is_applied as _payload_patch_applied,
)

_LOG = logging.getLogger("obvpm.h3")


def _ensure_layout_patch():
    """Lazy-install the layout patch on first use (never at import).

    apply_patch() self-tests bit-identical stock behavior and detects
    co-patchers (other copies stand down; a foreign wrapper -- e.g.
    MMH3Tools' patch_guide_origin -- is refused, never stacked).
    """
    if _layout_patch_applied():
        return
    # The vendored detection catches wrapped __init__ methods, but not a
    # pack that REPLACES the PackedLayout class outright (H3Studio's
    # middle_frame_patch subclasses GuidePackedLayout and rebinds the
    # module attribute; its own guard then makes the two packs mutually
    # exclusive). A replaced class carries a non-core __module__.
    import comfy.ldm.minimax.model as _mm
    cls_mod = getattr(getattr(_mm, "PackedLayout", None), "__module__", "")
    if cls_mod and not cls_mod.startswith("comfy."):
        raise RuntimeError(
            "obvpm.h3: another pack has REPLACED H3's PackedLayout class "
            "(it now comes from %r -- ComfyUI-H3Studio does this). The two "
            "continuation mechanisms cannot coexist in one session; disable "
            "one pack and restart." % cls_mod)
    if not _apply_layout_patch():
        raise RuntimeError(
            "obvpm.h3: the H3 layout patch could not be applied, so pinned "
            "runs would be rejected or mispositioned. The reason was logged "
            "just above this error.")


def pins_trim_totals(pins):
    """(head, tail) frames of pinned scaffolding, from PREPARED pins.

    Authoritative by construction: `covered` is what Prepare actually
    sliced (snap_down may have shrunk a requested window), so trim and
    save must read these, never the original spec numbers.
    """
    head = sum(int(p.get("covered", 0)) for p in (pins or [])
               if p.get("place") == "before")
    tail = sum(int(p.get("covered", 0)) for p in (pins or [])
               if p.get("place") == "after")
    return head, tail


def _ensure_payload_patch():
    if _payload_patch_applied():
        return
    if not _apply_payload_patch():
        raise RuntimeError(
            "obvpm.h3: the H3 payload patch could not be applied. Without it "
            "the pinned audio ref would overwrite the pinned video latents. "
            "The reason was logged just above this error.")


class H3MCtxPinSpec:
    CATEGORY = "obvpm/h3"
    FUNCTION = "build"
    RETURN_TYPES = ("PINSPECS",)
    RETURN_NAMES = ("pin_specs",)
    DESCRIPTION = (
        "Describes a pin as pure data: which part of a source clip to "
        "carry into the next generation, and where it sits in the target. "
        "No tensor work happens here -- Apply slices later. Chain "
        "several to pin from several sources."
    )
    OUTPUT_TOOLTIPS = (
        "The upstream stack plus this pin's spec. Feed H3MCtxApplyPins; "
        "Trim/Save ride the resolved pins wire Apply produces.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mctx": ("MCTX", {
                    "tooltip": "The source clip's latents+lineage bundle "
                               "from H3LoadVideoWithMCtx."}),
                "window": (list(fr.WINDOW_CHOICES), {
                    "default": "22",
                    "tooltip": "Frames of the source to pin. Only these "
                               "lengths are whole latent steps. 5 is barely "
                               "fluid, 22 nearly seamless; longer windows pin "
                               "more motion but cost more of the new clip. "
                               "1 is reserved for the future keyframe path."}),
                "take_from": (["head", "tail", "at_frame"], {
                    "default": "tail",
                    "tooltip": "Which part of the source supplies the "
                               "window: tail = the end (extend), head = the "
                               "start (for future prepends), at_frame = the "
                               "window ENDING at take_from_frame (a "
                               "timeline cut). Latent-grade cuts must land "
                               "on the 17-frame grid; misaligned cuts are "
                               "refused with the nearest valid frames."}),
                "take_from_frame": ("INT", {
                    "default": 0, "min": 0, "max": 4096,
                    "tooltip": "Only used when take_from is at_frame: the "
                               "delivered frame the window ends at (your "
                               "cut point)."}),
                "place": (["before", "after", "at_frame"], {
                    "default": "before",
                    "tooltip": "Where the pinned run sits in the target: "
                               "before = context leading in (extend; "
                               "re-rendered at the head, trimmed). after = "
                               "context leading out (prepend; generation "
                               "runs toward it, re-rendered at the tail, "
                               "trimmed -- typically with take_from=head). "
                               "at_frame (kept interior content) is NOT "
                               "YET IMPLEMENTED."}),
                "place_at_frame": ("INT", {
                    "default": 0, "min": 0, "max": 4096,
                    "tooltip": "Only used when place is at_frame (not yet "
                               "implemented)."}),
                "audio_window": ("INT", {
                    "default": 0, "min": 0, "max": 240,
                    "tooltip": "Frames of tail audio to pin, end-aligned with "
                               "the video window. 0 follows the video "
                               "window."}),
            },
            "optional": {
                "pin_specs": ("PINSPECS", {
                    "tooltip": "Upstream pin stack to append to."}),
            },
        }

    def build(self, mctx, window, take_from, take_from_frame, place,
              place_at_frame, audio_window, pin_specs=None):
        if mctx is None:
            raise ValueError(
                "H3MCtxPinSpec: mctx is None -- the loaded clip has no "
                "verified sidecar, so there are no latents to pin. Use the "
                "pixel route (H3MCtxFromFrames, Phase 2) or re-save the "
                "take with H3SaveVideoWithMCtx.")
        meta = mctx["meta"]
        delivered = int(meta.get("delivered_frames", 0))
        w = int(window)
        if take_from == "tail":
            start = delivered - w
        elif take_from == "head":
            start = 0
        else:
            start = int(take_from_frame) - w
        spec = {
            "source": mctx,
            "source_id": mctx.get("self_id", ""),
            "source_kind": "clip",
            "take_from": take_from,
            "take_from_frame": int(take_from_frame),
            "requested_window": w,
            # resolved best-effort range; Apply re-derives under its snap
            # policy and is authoritative
            "source_start": max(0, start),
            "source_frames": min(w, delivered) if delivered else w,
            "place": place,
            "place_at_frame": int(place_at_frame),
            "audio_window": int(audio_window),
        }
        return (list(pin_specs or []) + [spec],)


def _prepare_pins(pin_specs, snap_window_down_to_available):
    """Materialize specs into sliced pins. THE sole slicing site.

    Lives as a module function (not a node) since 2026-08-12: with Save
    and Trim consuming the resolved pins wire, a separate Prepare node
    added a mandatory hop with no unique contribution -- slicing is cheap
    and Apply outputs the pins itself. The single-implementation-site
    principle holds here regardless of node shape.
    """
    if not pin_specs:
        raise ValueError("H3MCtxApplyPins: the pin_specs stack is empty.")
    pins = []
    for i, spec in enumerate(pin_specs):
        if spec.get("source_kind") != "clip":
            raise ValueError(
                "H3MCtxApplyPins: spec %d has source_kind %r; only clip "
                "sources exist in Phase 1." % (i, spec.get("source_kind")))
        pins.append(_prepare_clip_pin(i, spec, snap_window_down_to_available))
    return pins


def _prepare_clip_pin(i, spec, snap_down):
        bundle = spec.get("source")
        if bundle is None:
            raise ValueError(
                "H3MCtxApplyPins: spec %d carries no source bundle. Specs must "
                "come from H3MCtxPinSpec in this same run." % i)
        meta = bundle["meta"]
        video, audio = bundle["video_latent"], bundle["audio_latent"]

        raw_steps = int(video.shape[2])
        raw_frames = fr.pixel_frames(raw_steps)
        meta_raw = int(meta.get("raw_frames", raw_frames))
        if meta_raw != raw_frames:
            raise ValueError(
                "H3MCtxApplyPins: sidecar header says %d raw frames but the "
                "stored latent covers %d. The sidecar is inconsistent; "
                "refusing." % (meta_raw, raw_frames))
        delivered = int(meta.get("delivered_frames", raw_frames))
        pinned_head = int(meta.get("pinned_head_frames", 0))
        take_from = spec.get("take_from", "tail")
        cut_frame = int(spec.get("take_from_frame", 0))

        # window availability depends on where it's taken from
        if take_from == "at_frame":
            if cut_frame < 1 or cut_frame > delivered:
                raise ValueError(
                    "H3MCtxApplyPins: spec %d's cut frame %d is outside the "
                    "source's delivered range (1..%d)."
                    % (i, cut_frame, delivered))
            available = cut_frame
        elif take_from in ("tail", "head"):
            available = delivered
        else:
            raise ValueError(
                "H3MCtxApplyPins: spec %d has unknown take_from %r."
                % (i, take_from))

        n = int(spec.get("requested_window", 22))
        if n > available:
            snapped = fr.snap_window(available)
            if not snap_down or snapped is None:
                raise ValueError(
                    "H3MCtxApplyPins: spec %d wants a %d frame window but "
                    "only %d frames are available (%s). Enable "
                    "snap_window_down_to_available to use %s instead."
                    % (i, n, available, take_from,
                       snapped if snapped else "nothing"))
            _LOG.warning("obvpm.h3: spec %d window %d -> %d (only %d frames "
                         "available)", i, n, snapped, available)
            n = snapped
        if n == 1:
            raise ValueError(
                "H3MCtxApplyPins: the 1-frame window is not sliceable from a "
                "latent (the last step of a clip spans 4 frames). Use 5 or "
                "more; 1-frame pins arrive with the keyframe path.")

        steps = fr.steps_for_frames(n)
        if steps is None:
            raise RuntimeError(
                "H3MCtxApplyPins: %d frames is not a whole number of latent "
                "steps; the window ladder no longer matches the VAE grid."
                % n)

        # Delivered-frame start of the slice, then mapped onto the RAW
        # timeline (the stored latent includes any pinned scaffolding).
        if take_from == "tail":
            d_start = delivered - n
        elif take_from == "head":
            d_start = 0
        else:
            d_start = cut_frame - n
        raw_start = pinned_head + d_start

        # Latent-grade cut rule: phase-0 slice starts occur only at
        # 17-frame group boundaries. This one check covers everything the
        # old special cases did -- pinned tails, misaligned continuation
        # heads (DESIGN 4a), arbitrary interior cuts.
        if raw_start % fr.FRAMES_PER_GROUP != 0:
            lo = (raw_start // fr.FRAMES_PER_GROUP) * fr.FRAMES_PER_GROUP
            hi = lo + fr.FRAMES_PER_GROUP
            suggest = sorted(set(
                b - pinned_head + n for b in (lo, hi)
                if 0 <= b - pinned_head and b - pinned_head + n <= delivered))
            raise ValueError(
                "H3MCtxApplyPins: spec %d's window would start at raw frame "
                "%d, which is not on the 17-frame latent grid -- the slice "
                "would be unsound. Nearest latent-grade window END frames: "
                "%s. (Or cross to pixels via H3MCtxFromFrames, Phase 2.)"
                % (i, raw_start, suggest))
        k = raw_start // fr.FRAMES_PER_GROUP * fr.LATENTS_PER_GROUP
        if fr.frame_at_latent(k) != raw_start:
            raise RuntimeError(
                "H3MCtxApplyPins: step mapping disagreement (frame %d -> "
                "step %d -> frame %d). Upstream grid change; refusing."
                % (raw_start, k, fr.frame_at_latent(k)))
        raw_end = raw_start + n
        if raw_end > raw_frames or k + steps > raw_steps:
            raise ValueError(
                "H3MCtxApplyPins: spec %d's window (raw frames %d..%d) "
                "exceeds the stored latent (%d frames)."
                % (i, raw_start, raw_end, raw_frames))

        video_slice = video[:1, :, k:k + steps].clone()
        covered = fr.pixel_frames(steps)
        if covered != n:
            raise RuntimeError(
                "H3MCtxApplyPins: %d steps cover %d frames, expected %d. "
                "Upstream VAE grid change; refusing." % (steps, covered, n))

        # Audio: end-aligned with the video window's END, boundaries on
        # the cumulative grid, never converted deltas.
        a_frames = int(spec.get("audio_window", 0)) or n
        total_t = int(audio.shape[-1])
        a_lo = max(0, raw_end - a_frames)
        start_idx = fr.audio_total(a_lo)
        end_idx = min(total_t, fr.audio_total(raw_end))
        rt = max(0, end_idx - start_idx)
        if rt < fr.audio_span(a_lo, raw_end):
            _LOG.warning("obvpm.h3: audio window wants %d steps, the latent "
                         "supplies %d; pinning what there is",
                         fr.audio_span(a_lo, raw_end), rt)
        # The grid overhang only exists at the CLIP's end; an interior
        # window ends exactly on its boundary coordinate.
        if raw_end == raw_frames:
            overhang = fr.audio_overhang(total_t, raw_frames)
            if overhang is None:
                _LOG.warning("obvpm.h3: unexpected audio grid (%d steps for "
                             "%d frames); assuming no overhang",
                             total_t, raw_frames)
                overhang = 0.0
        else:
            overhang = 0.0
        audio_slice = (audio[:1, ..., start_idx:end_idx].clone()
                       if rt > 0 else None)

        return {
            "kind": "clip",
            "place": spec.get("place", "before"),
            "video": video_slice,
            "steps": steps,
            "covered": covered,
            "audio": audio_slice,
            "audio_steps": rt,
            "overhang": overhang,
            "width": int(video.shape[4]) * 16,
            "height": int(video.shape[3]) * 16,
            "origin": bundle.get("origin", "sampled"),
            "source_id": bundle.get("self_id", ""),
            # the AUTHORITATIVE resolved spec: source range recomputed from
            # the actually-sliced window (snap_down may have shrunk it), so
            # downstream trim/save read what really happened
            "spec": dict(
                {k: v for k, v in spec.items() if k != "source"},
                source_start=d_start,
                source_frames=n,
            ),
        }


class H3MCtxApplyPins:
    CATEGORY = "obvpm/h3"
    FUNCTION = "apply"
    RETURN_TYPES = ("CONDITIONING", "PINS")
    RETURN_NAMES = ("conditioning", "pins")
    DESCRIPTION = (
        "Slices the pinned windows out of the sources (the sole "
        "frame/latent math site: window snapping, phase checks, "
        "cumulative-total audio cut) and attaches each pinned run to the "
        "conditioning via the vendored layout/payload patches. The pins "
        "output carries the resolved slices -- feed it to Trim/Save so "
        "they read what was actually pinned."
    )
    OUTPUT_TOOLTIPS = (
        "Conditioning with the pinned runs attached. Feed the guider/sampler.",
        "The resolved pins. Feed H3TrimPinned / the Save nodes.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING",),
                "latent": ("LATENT", {
                    "tooltip": "The TARGET clip's empty AV latent -- "
                               "authoritative for frame count and "
                               "resolution. Wire the same latent to the "
                               "sampler."}),
                "snap_window_down_to_available": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "When a source cannot supply the requested "
                               "window, snap down the ladder (56/39/22/5) "
                               "instead of refusing. Off = refuse loudly; "
                               "snapping silently weakens continuity."}),
            },
            "optional": {
                "pin_specs": ("PINSPECS", {
                    "tooltip": "The spec stack from H3MCtxPinSpec or a "
                               "loader's create_pins output. Unconnected or "
                               "empty = pass-through: the conditioning is "
                               "returned untouched and pins is empty (a "
                               "plain root generation), so the node can "
                               "stay in the graph when not pinning."}),
            },
        }

    def apply(self, conditioning, latent, snap_window_down_to_available,
              pin_specs=None):
        if not pin_specs:
            _LOG.info("obvpm.h3: no pin specs; conditioning passes through "
                      "untouched (plain root generation)")
            return (conditioning, [])
        for i, s in enumerate(pin_specs or []):
            if s.get("place") == "at_frame":
                raise ValueError(
                    "H3MCtxApplyPins: spec %d places 'at_frame' -- not yet "
                    "implemented; arrives with inside pins/repaint (a later "
                    "phase)." % i)
        pins = _prepare_pins(pin_specs, snap_window_down_to_available)
        if len(pins) != 1:
            raise ValueError(
                "H3MCtxApplyPins: exactly one pin per generation for now; "
                "got %d. (before+after together = bridging, a later "
                "experiment.)" % len(pins))
        pin = pins[0]
        place = pin.get("place")

        target_video, _ = unpack_av(latent, name="latent")
        latent_t = int(target_video.shape[2])
        frame_count = fr.pixel_frames(latent_t)
        width = int(target_video.shape[4]) * 16
        height = int(target_video.shape[3]) * 16

        if (pin["width"], pin["height"]) != (width, height):
            raise ValueError(
                "H3MCtxApplyPins: the pinned clip is %dx%d but this clip is "
                "%dx%d. A latent cannot be resized; regenerate the source at "
                "this resolution, or cross to pixels explicitly "
                "(H3MCtxFromFrames, Phase 2)."
                % (pin["width"], pin["height"], width, height))
        covered = int(pin["covered"])
        if covered >= frame_count:
            raise ValueError(
                "H3MCtxApplyPins: pinning %d frames into a %d frame clip; "
                "the pinned run must be a fraction of the timeline."
                % (covered, frame_count))
        if pin.get("origin") == "encoded":
            _LOG.info("obvpm.h3: pin from an encoded (pixel-grade) source; "
                      "continuity is soft, not exact")

        _ensure_layout_patch()

        # Pinned run coordinates on the target timeline. before: indices
        # 0..covered-1 (extend; trimmed off the head). after: the LAST
        # covered indices (prepend; generation runs TOWARD the pinned
        # content, which generalizes stock's trained-in last-frame anchor;
        # trimmed off the tail). Stock always gets a legal index-0 anchor;
        # the real position rides under MC_KEY (vendored mechanism).
        base = 0 if place == "before" else frame_count - covered
        offsets = [base + o for o in fr.step_offsets(int(pin["steps"]))]
        keyframes = []
        for k, p in enumerate(offsets):
            keyframes.append({
                "resolved_frame_index": 0,
                MC_KEY: p,
                "latent": pin["video"][:, :, k:k + 1],
            })
        out = node_helpers.conditioning_set_values(conditioning, {
            "minimax_keyframes": keyframes,
            "minimax_frame_count": frame_count,
        })

        rt = int(pin.get("audio_steps", 0))
        if rt > 0 and pin.get("audio") is not None:
            _ensure_payload_patch()
            # End-align the pinned audio with the pinned video's END on
            # the target timeline: frame `covered` for a before-pin,
            # `frame_count` for an after-pin. The sliced audio reaches
            # `overhang` of a step past the source slice's end (nonzero
            # only when the slice ends at the source clip's end), so the
            # end coordinate moves by that much, then snaps onto the
            # target's own audio grid (a third of a step is 8.3 ms; the
            # snap stops the offset cycling across chains). Vendored math.
            end_base = covered if place == "before" else frame_count
            end_frame = float(end_base) + float(pin["overhang"]) / fr.FRAME_RESCALE
            end_coord = round(fr.FRAME_RESCALE * end_frame)
            end_frame = end_coord / fr.FRAME_RESCALE
            ref = {
                "kind": "audio",
                "ref_audio_t": rt,
                "audio_latent": pin["audio"],
                MC_AUDIO_KEY: end_frame,
            }
            out = node_helpers.conditioning_set_values(
                out, {"minimax_refs": [ref]}, append=True)

        _LOG.info(
            "obvpm.h3: pinned %d frames (%d steps) at the %s of a %d frame "
            "clip at %dx%d (indices %d..%d); audio %s; trim %d off the %s",
            covered, int(pin["steps"]),
            "head" if place == "before" else "tail",
            frame_count, width, height, offsets[0], offsets[-1],
            ("%d steps ending at frame %.3f" % (rt, end_frame)) if rt > 0
            and pin.get("audio") is not None else "off", covered,
            "head" if place == "before" else "tail")
        return (out, pins)


class H3TrimPinned:
    CATEGORY = "obvpm/h3"
    FUNCTION = "trim"
    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    DESCRIPTION = (
        "Removes pinned scaffolding from a decoded clip, picture and "
        "sound together, and truncates the audio tail to exactly "
        "frames/fps (H3 rounds its audio grid up ~8 ms per clip; the "
        "error compounds at every join in a chain). Trim amounts come "
        "from the pins wire -- the same one that fed Apply."
    )
    OUTPUT_TOOLTIPS = (
        "The delivered frames. Feed H3SaveVideoWithMCtx.",
        "The delivered audio, duration-locked to the frames.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "pins": ("PINS", {
                    "tooltip": "The resolved pins from H3MCtxApplyPins. The "
                               "trim amounts are derived from what was "
                               "actually pinned: before-pins come off the "
                               "head, after-pins off the tail."}),
            },
            "optional": {
                "audio": ("AUDIO", {
                    "tooltip": "Decoded audio for the same clip; trimmed by "
                               "the same durations so sound stays locked to "
                               "picture, and tail-matched to exactly "
                               "frames/fps (H3 ships ~8 ms extra sound per "
                               "clip; the error compounds at joins). Leave "
                               "unwired for silent clips."}),
            },
        }

    def trim(self, images, pins, audio=None):
        head, tail = pins_trim_totals(pins)
        total = int(images.shape[0])
        if head + tail >= total:
            raise ValueError(
                "H3TrimPinned: trimming %d+%d frames from a %d frame clip "
                "leaves nothing." % (head, tail, total))
        out_images = images[head:total - tail] if (head or tail) else images
        remaining = total - head - tail

        out_audio = audio
        if audio is not None:
            waveform = audio["waveform"]
            sr = int(audio["sample_rate"])
            cut_head = int(round(head / float(fr.FPS) * sr))
            cut_tail = int(round(tail / float(fr.FPS) * sr))
            length = int(waveform.shape[-1])
            if cut_head + cut_tail >= length:
                raise ValueError(
                    "H3TrimPinned: trimming %.3fs+%.3fs from %.3fs of audio "
                    "leaves nothing. Wire the audio decoded from this same "
                    "clip." % (cut_head / sr, cut_tail / sr, length / sr))
            waveform = waveform[..., cut_head:length - cut_tail]
            # tail-match unconditionally: exactly one right answer (the
            # ~8 ms/clip audio surplus compounds at every join otherwise)
            want = int(round(remaining / float(fr.FPS) * sr))
            have = int(waveform.shape[-1])
            if have > want:
                _LOG.info("obvpm.h3: tail-matched audio, cut %d samples "
                          "(%.2f ms)", have - want,
                          (have - want) / sr * 1000.0)
                waveform = waveform[..., :want]
            out_audio = {"waveform": waveform, "sample_rate": sr}
        elif head or tail:
            _LOG.info("obvpm.h3: trimmed %d head / %d tail frames with no "
                      "audio wired; if this clip has sound, trim it through "
                      "this node too or it will drift %.3fs.",
                      head, tail, (head + tail) / float(fr.FPS))
        return (out_images, out_audio)
