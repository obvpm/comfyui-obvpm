"""mctx_v1 sidecar files: read, write, hash, lineage (DESIGN.md sections 2-3).

A take is a clip pair with the same basename:

    clip_00001.mp4                   the video (lossy delivery copy)
    clip_00001.mctx.safetensors      full raw AV latents + header (the RAW)

The sidecar is a standard safetensors file. Tensors: the clip's FULL
unsliced sampled latents, `video` [1,24,T,h/16,w/16] and `audio`
[1,32,2,T40]. The header (safetensors __metadata__, all strings) carries
identity, lineage and the generation recipe, and is readable without
loading tensors: 8-byte length prefix + JSON. That property is what makes
registry-free scanning cheap, server- or browser-side.

The format string is deliberately un-namespaced: mctx sidecars are an open
convention any H3 pack can read or write.
"""

import hashlib
import json
import os
import struct

FORMAT = "mctx_v1"
SIDECAR_SUFFIX = ".mctx.safetensors"

# Header keys written by this implementation. Readers must ignore unknown
# keys; additions are non-breaking, semantic changes bump the format.
HEADER_KEYS = (
    "format", "self_id", "parent_id", "relation", "parent_join_frame",
    "width", "height", "fps", "raw_frames", "pinned_head_frames",
    "pinned_tail_frames", "delivered_frames", "pins",
    "user_meta",  # opaque JSON; well-known keys: prompt, seed, steps, refs_note
)


def sidecar_path(video_path):
    """clip_00001.mp4 -> clip_00001.mctx.safetensors, same folder."""
    base, _ext = os.path.splitext(video_path)
    return base + SIDECAR_SUFFIX


def hash_file(path, chunk_size=1024 * 1024):
    """Streamed SHA-256 of a file, lowercase hex. Identity == content."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_header(path):
    """The sidecar's string->string metadata, without loading tensors.

    safetensors layout: 8 bytes little-endian header length, then that
    many bytes of JSON whose "__metadata__" key holds the user metadata.
    Raises ValueError on anything that is not a plausible sidecar.
    """
    with open(path, "rb") as f:
        prefix = f.read(8)
        if len(prefix) != 8:
            raise ValueError("%s: too short to be a safetensors file" % path)
        (length,) = struct.unpack("<Q", prefix)
        if length <= 0 or length > 100 * 1024 * 1024:
            raise ValueError("%s: implausible safetensors header length %d"
                             % (path, length))
        raw = f.read(length)
        if len(raw) != length:
            raise ValueError("%s: truncated safetensors header" % path)
    header = json.loads(raw.decode("utf-8"))
    meta = header.get("__metadata__") or {}
    if meta.get("format") != FORMAT:
        raise ValueError("%s: not a %s sidecar (format=%r)"
                         % (path, FORMAT, meta.get("format")))
    return meta


def is_sidecar(path):
    """Cheap predicate: readable mctx_v1 header, no exception surface."""
    try:
        read_header(path)
        return True
    except Exception:
        return False


def load_sidecar(path):
    """Full load: (video latent, audio latent, meta dict), tensors on CPU."""
    from safetensors.torch import load_file
    tensors = load_file(path, device="cpu")
    if "video" not in tensors or "audio" not in tensors:
        raise ValueError("%s: sidecar is missing the video/audio tensors" % path)
    return tensors["video"], tensors["audio"], read_header(path)


def write_sidecar(path, video, audio, meta):
    """Atomic sidecar write: tmp file + os.replace.

    Callers order the larger transaction as MP4 first -> hash -> this;
    sidecar existence is the commit point, so a crash in between leaves a
    plain playable video, never a corrupt take.
    """
    from safetensors.torch import save_file
    clean = {}
    for k, v in meta.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("sidecar metadata must be str->str, got %r=%r" % (k, v))
        clean[k] = v
    if clean.get("format") != FORMAT:
        raise ValueError("sidecar metadata must carry format=%s" % FORMAT)
    tmp = path + ".tmp"
    save_file({"video": video.contiguous().cpu(),
               "audio": audio.contiguous().cpu()}, tmp, metadata=clean)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# pins: the generation recipe (serialized PINSPECS)
# ---------------------------------------------------------------------------

def serialize_pins(pin_specs):
    """PINSPECS -> the header's `pins` JSON: specs minus in-memory handles."""
    entries = []
    for spec in pin_specs or []:
        entries.append({
            "source_id": spec.get("source_id", ""),
            "source_kind": spec.get("source_kind", "clip"),
            "source_start": int(spec.get("source_start", 0)),
            "source_frames": int(spec.get("source_frames", 0)),
            "place": spec.get("place", ""),
            "audio_window": int(spec.get("audio_window", 0)),
        })
    return json.dumps(entries, separators=(",", ":"))


def parse_pins(meta):
    try:
        return json.loads(meta.get("pins") or "[]")
    except Exception:
        return []


def summarize_pins(pin_specs):
    """The fast-path lineage summary: (relation, parent_id, parent_join_frame).

    Single before-pin from a clip = "extends" (this clip sits AFTER the
    junction at source_start + source_frames); single after-pin from a
    clip = "prepends" (this clip sits BEFORE the junction at
    source_start). Anything the summary cannot express -- multiple pins,
    inside pins, image sources -- yields ("", "", 0) and consumers read
    the full `pins` recipe instead (DESIGN.md section 3).
    """
    specs = [s for s in (pin_specs or [])]
    if len(specs) != 1:
        return "", "", 0
    s = specs[0]
    if s.get("source_kind") != "clip" or not s.get("source_id"):
        return "", "", 0
    place = s.get("place")
    if place == "before":
        return ("extends", s["source_id"],
                int(s.get("source_start", 0)) + int(s.get("source_frames", 0)))
    if place == "after":
        return "prepends", s["source_id"], int(s.get("source_start", 0))
    return "", "", 0


def pinned_totals(pin_specs):
    """(before_total, after_total) delivered-frame counts across the specs."""
    head = sum(int(s.get("source_frames", 0)) for s in (pin_specs or [])
               if s.get("place") == "before")
    tail = sum(int(s.get("source_frames", 0)) for s in (pin_specs or [])
               if s.get("place") == "after")
    return head, tail


# ---------------------------------------------------------------------------
# MCTX bundles (the wire object; meta IS the parsed header)
# ---------------------------------------------------------------------------

def make_mctx(self_id, video, audio, meta, origin="sampled"):
    return {
        "self_id": self_id,
        "video_latent": video,
        "audio_latent": audio,
        "meta": dict(meta),
        "origin": origin,
    }


def scan_for_parent(folder, parent_id):
    """Find the sidecar whose self_id matches, header-scan only.

    Same-folder scan; returns the sidecar path or None. Missing parent is
    "lineage unknown", never an error (DESIGN.md section 3). The slow
    hash-the-MP4s path is deliberately not implemented here -- callers
    decide when that cost is worth paying.
    """
    if not parent_id:
        return None
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        if not name.endswith(SIDECAR_SUFFIX):
            continue
        p = os.path.join(folder, name)
        try:
            if read_header(p).get("self_id") == parent_id:
                return p
        except Exception:
            continue
    return None
