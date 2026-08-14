# comfyui-obvpm

ComfyUI nodes: an interactive **Load Image & Crop**, megapixel-based
downscaling, and workflow-control nodes (optional gates, lazy switches,
fallback values) — plus the `beta57` scheduler.

## Installation

Clone (or copy) this folder into `ComfyUI/custom_nodes`:

```
cd ComfyUI/custom_nodes
git clone <repo-url> comfyui-obvpm
```

Restart ComfyUI. No extra Python dependencies are required.

## Image nodes (obvpm/image)

### Load Image & Crop

![Load Image & Crop node with an active crop selection](assets/loadandcrop.jpg)

A Load Image with an interactive crop editor drawn directly on the node:

- **Drag** on the image to draw a crop area.
- **Drag inside** the selection to move it; **drag a corner** to resize.
- **Click** (without dragging) outside the selection to clear it.
- With no crop drawn, the full image is output.

The label above the selection shows its size in source pixels; the row under
the preview shows the full image size. If `max_megapixels` is greater than 0,
the output (crop or full image) is scaled down to fit within it, aspect
preserved — the preview labels show the resulting size as `Downscaled To:`.
A value of `0` disables the cap.

Outputs are `image` and `mask` (from the alpha channel, like the stock Load
Image). The crop is stored in the workflow in normalized coordinates, so it
survives reloads; changing the crop re-executes the node on the next run.
Drag & drop and paste upload work like the stock node. Works in both the
classic canvas renderer and Nodes 2.0.

### Downscale Image to Megapixels

Scales an image down so its total pixel count fits within `megapixels`,
keeping aspect ratio. Images already at or under the target pass through
completely untouched (no resample). With no image connected it outputs `None`
(bypass). 1.0 megapixels = 1024×1024 pixels, matching ComfyUI's
`ImageScaleToTotalPixels` convention.

## Workflow-control nodes

The rest of the pack is built around making *optional paths* work well:
workflows where an input may or may not be connected, where a branch should
only run under some condition, and where downstream nodes need something
sensible either way.

### Core concepts

ComfyUI offers three different ways to "not run" part of a workflow, and the
nodes in this pack are organized around them:

| Mechanism | What happens | When to use |
|---|---|---|
| **Mute** (ExecutionBlocker) | Every node downstream of the blocked output is silently skipped. Cannot be caught or handled downstream. | Kill an entire path when its input is missing. |
| **Bypass** (forward `None`) | Downstream nodes with an *optional* input see it as unconnected and handle the absence themselves. Feeding `None` into a *required* input errors. | Let a tolerant downstream node decide what to do. |
| **Lazy** (lazy inputs) | The unselected branch is never executed at all — its upstream nodes don't run and cost nothing. | Conditionally skip expensive work (sampling, upscaling, whole groups). |

Mute and bypass act *downstream* of the gate; only lazy evaluation saves the
*upstream* work feeding the unselected side.

### Gates (obvpm/gates)

**Optional Image / Optional Video / Optional Audio / Optional Latent /
Optional Any**

Pass the input through when connected. When the input is missing, the
`on_empty` toggle decides what downstream sees:

- `mute` — block the path; every downstream node is skipped.
- `bypass` — output `None`; a downstream node with an optional input treats it
  as unconnected.

Each gate also has a **`present`** boolean output that is true when an input
is connected. It stays live even in mute mode, so it can drive a Lazy
Switch's boolean while the value path is dead. `Optional Any` is the wildcard
version and accepts any type.

**Required Model**

The minimal gate: passes a MODEL through, and if nothing is connected, mutes
the path. Use it to make a whole branch conditional on a model being wired in.

**Mute**

Passes any input through unchanged; when the `mute` boolean is true, blocks
everything downstream. The boolean is connectable, so it can be driven by
logic (e.g. a gate's `present` through a Boolean invert). Note: nodes
*upstream* of the input still run — use a Lazy Switch when you want the
upstream work skipped too.

### Switches (obvpm/switches)

**Lazy Switch**

Outputs `on_true` when the boolean is true, else `on_false` — and only the
selected branch executes. The entire upstream chain of the unselected side is
skipped, making this the way to bypass whole groups of nodes conditionally.

Details that matter in practice:

- An unconnected selected side outputs `None` instead of erroring.
- A branch that was muted by a gate can be "picked back up": select the other
  side and the workflow continues.
- Drive the boolean from a gate's `present` output to switch automatically
  based on whether an input exists.

**Lazy Switch 2 Values / 3 Values**

The same switch for several values at once: `boolean` selects between the
`on_false_value_*` block and the `on_true_value_*` block, output as
`value_1..N`. All slots switch together; unconnected slots on the selected
side output `None`.

### Values (obvpm/values)

**First Float (else fallback) / First Int (else fallback)**

Output the first connected input that carries a value; if none do, output the
`fallback` widget value. Inputs fed by a gate in bypass mode (`None`) are
skipped over, so several optional paths fan back into one guaranteed value.

## beta57 scheduler

This nodepack automatically adds **beta57** for you if you don't have or
don't want to install [RES4LYF](https://github.com/ClownsharkBatwing/RES4LYF).

beta57 is the beta sigma schedule with `alpha=0.5, beta=0.7`, popularized by
RES4LYF. It appears in every scheduler dropdown (KSampler, BasicScheduler, …)
and behaves like a built-in.

## License

[Apache-2.0](LICENSE). See
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md) for acknowledgments.
