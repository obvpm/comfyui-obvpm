"""Vendored runtime patches for MiniMax H3 (see THIRD_PARTY_NOTICES.md).

patch_layout.py and patch_payload.py are verbatim copies from
ComfyUI-H3-Motion-Context (GPL-3.0, NikoDemon80). They are designed for
vendoring: a shared PATCH_MARKER ABI lets multiple copies across packs
detect each other and stand down instead of stacking, and a foreign
wrapper (a different pack patching the same internals, e.g. MMH3Tools'
patch_guide_origin) is detected and refused. Do not modify them here --
the marker strings and detection logic are a cross-pack contract.
"""
