"""obvpm nodes: optional gates, lazy switches, and small workflow helpers.

Optional gates pass an input through if it is connected/present. When the
input is missing, the on_empty toggle picks what downstream sees:
- mute: emit an ExecutionBlocker, silently skipping every downstream node
- bypass: forward the input as-is (None), so a downstream node with an
  optional input sees it as unconnected and handles the absence itself

Also registers the "beta57" scheduler (RES4LYF's beta schedule with
alpha=0.5, beta=0.7) into ComfyUI's global scheduler registry.
"""

from functools import partial

import comfy.samplers
from comfy_execution.graph_utils import ExecutionBlocker

from .load_image_crop import LoadImageCrop

if "beta57" not in comfy.samplers.SCHEDULER_HANDLERS:
    comfy.samplers.SCHEDULER_HANDLERS["beta57"] = comfy.samplers.SchedulerHandler(
        partial(comfy.samplers.beta_scheduler, alpha=0.5, beta=0.7)
    )
# SCHEDULER_NAMES and KSampler.SCHEDULERS are the same list object in
# comfy/samplers.py, so append to each only if genuinely missing.
for _names in (comfy.samplers.SCHEDULER_NAMES, comfy.samplers.KSampler.SCHEDULERS):
    if "beta57" not in _names:
        _names.append("beta57")


class OptionalGateBase:
    CATEGORY = "obvpm/gates"
    FUNCTION = "gate"
    DESCRIPTION = (
        "Passes the input through when connected. When the input is missing, "
        "on_empty decides what happens downstream: 'mute' blocks every node "
        "on this path (they are silently skipped), 'bypass' outputs None so a "
        "downstream node with an optional input treats it as unconnected. The "
        "'present' output is true when an input is connected — it stays live "
        "even in mute mode, so it can drive a Lazy Switch boolean."
    )

    OUTPUT_TOOLTIPS = (
        "The input passed through. When the input is empty: blocked (mute) or None (bypass).",
        "True when an input is connected. Stays live even in mute mode.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "on_empty": (["mute", "bypass"], {
                    "default": "mute",
                    "tooltip": "What downstream sees when no input is connected: mute skips every downstream node, bypass outputs None.",
                }),
            },
            "optional": {
                "input": (cls.TYPE, {
                    "tooltip": "The value to pass through. May be left unconnected.",
                }),
            },
        }

    def gate(self, on_empty, input=None):
        if input is None and on_empty == "mute":
            # The value path is blocked, but the present flag stays live so
            # it can drive switches even when the input is missing.
            return (ExecutionBlocker(None), False)
        return (input, input is not None)


class ImageOptionalGate(OptionalGateBase):
    TYPE = "IMAGE"
    RETURN_TYPES = ("IMAGE", "BOOLEAN")
    RETURN_NAMES = ("image", "present")


class VideoOptionalGate(OptionalGateBase):
    TYPE = "VIDEO"
    RETURN_TYPES = ("VIDEO", "BOOLEAN")
    RETURN_NAMES = ("video", "present")


class AudioOptionalGate(OptionalGateBase):
    TYPE = "AUDIO"
    RETURN_TYPES = ("AUDIO", "BOOLEAN")
    RETURN_NAMES = ("audio", "present")


class ModelOptionalGate(OptionalGateBase):
    TYPE = "MODEL"
    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    DESCRIPTION = (
        "Passes the model through when connected. With nothing connected, "
        "every node downstream of this gate is silently skipped. Use it to "
        "make a whole branch of the workflow conditional on a model being "
        "wired in."
    )

    OUTPUT_TOOLTIPS = (
        "The model passed through. Blocked when nothing is connected.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "optional": {
                "input": (cls.TYPE, {
                    "tooltip": "The model to pass through. Leave unconnected to skip everything downstream.",
                }),
            },
        }

    def gate(self, input=None):
        if input is None:
            return (ExecutionBlocker(None),)
        return (input,)


class LatentOptionalGate(OptionalGateBase):
    TYPE = "LATENT"
    RETURN_TYPES = ("LATENT", "BOOLEAN")
    RETURN_NAMES = ("latent", "present")


class AnyType(str):
    def __ne__(self, other):
        return False


ANY = AnyType("*")


class AnyOptionalGate(OptionalGateBase):
    TYPE = ANY
    RETURN_TYPES = (ANY, "BOOLEAN")
    RETURN_NAMES = ("value", "present")


class LazySwitch:
    CATEGORY = "obvpm/switches"
    FUNCTION = "switch"
    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("value",)
    DESCRIPTION = (
        "Outputs on_true when boolean is true, else on_false. Lazy: only the "
        "selected branch executes — every node feeding the unselected side is "
        "skipped entirely, saving its compute. An unconnected selected side "
        "outputs None instead of erroring, and blocked (muted) branches can "
        "be picked back up by selecting the other side. Drive the boolean "
        "from a gate's 'present' output to switch automatically."
    )

    OUTPUT_TOOLTIPS = (
        "The selected side's value. None when the selected side is unconnected.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "boolean": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Selects which side to execute and output: true = on_true, false = on_false.",
                }),
            },
            "optional": {
                "on_false": (ANY, {
                    "lazy": True,
                    "tooltip": "Output when boolean is false. Only executes while selected; may be left unconnected.",
                }),
                "on_true": (ANY, {
                    "lazy": True,
                    "tooltip": "Output when boolean is true. Only executes while selected; may be left unconnected.",
                }),
            },
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    def check_lazy_status(self, boolean, on_true=None, on_false=None, dynprompt=None, unique_id=None):
        wanted = "on_true" if boolean else "on_false"
        # Requesting a lazy input that has no link is a hard engine error,
        # so only ask for the selected side if it is actually connected.
        if dynprompt is not None and unique_id is not None:
            from comfy_execution.graph_utils import is_link

            value = dynprompt.get_node(unique_id)["inputs"].get(wanted)
            if not is_link(value):
                return []
        return [wanted]

    def switch(self, boolean, on_true=None, on_false=None, dynprompt=None, unique_id=None):
        return (on_true if boolean else on_false,)


class MuteGate:
    CATEGORY = "obvpm/gates"
    FUNCTION = "gate"
    RETURN_TYPES = (ANY,)
    RETURN_NAMES = ("value",)
    DESCRIPTION = (
        "Passes the input through unchanged; when mute is true, every node "
        "downstream of this gate is silently skipped. Accepts any type. Note "
        "that the nodes upstream of 'input' still run — to skip the upstream "
        "work as well, use a Lazy Switch instead."
    )

    OUTPUT_TOOLTIPS = (
        "The input passed through. Blocked while mute is true.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mute": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "When true, every node downstream of this gate is skipped. Connectable, so it can be driven by logic.",
                }),
            },
            "optional": {
                "input": (ANY, {
                    "tooltip": "Any value to pass through. Its upstream nodes still run even when muted.",
                }),
            },
        }

    def gate(self, mute, input=None):
        if mute:
            return (ExecutionBlocker(None),)
        return (input,)


class LazySwitchMultiBase:
    CATEGORY = "obvpm/switches"
    FUNCTION = "switch"
    DESCRIPTION = (
        "A Lazy Switch for several values at once: when boolean is true the "
        "on_true_value_* inputs are output as value_*, otherwise the "
        "on_false_value_* inputs. Only the selected side executes — nodes "
        "feeding the unselected side are skipped entirely. Unconnected slots "
        "on the selected side output None."
    )

    @classmethod
    def INPUT_TYPES(cls):
        optional = {}
        for side in ("false", "true"):
            for i in range(1, cls.COUNT + 1):
                optional[f"on_{side}_value_{i}"] = (ANY, {
                    "lazy": True,
                    "tooltip": f"Output as value_{i} when boolean is {side}. Only executes while selected; may be left unconnected.",
                })
        return {
            "required": {
                "boolean": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Selects which side to execute and output: true = the on_true_value inputs, false = the on_false_value inputs.",
                }),
            },
            "optional": optional,
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    def check_lazy_status(self, boolean, dynprompt=None, unique_id=None, **kwargs):
        side = "true" if boolean else "false"
        wanted = [f"on_{side}_value_{i}" for i in range(1, self.COUNT + 1)]
        # Requesting a lazy input that has no link is a hard engine error,
        # so only ask for selected-side inputs that are actually connected.
        if dynprompt is not None and unique_id is not None:
            from comfy_execution.graph_utils import is_link

            inputs = dynprompt.get_node(unique_id)["inputs"]
            wanted = [name for name in wanted if is_link(inputs.get(name))]
        return wanted

    def switch(self, boolean, dynprompt=None, unique_id=None, **kwargs):
        side = "true" if boolean else "false"
        return tuple(
            kwargs.get(f"on_{side}_value_{i}")
            for i in range(1, self.COUNT + 1)
        )


_LAZY_VALUE_TOOLTIP = "The selected side's value for this slot. None when that slot is unconnected."


class LazySwitch2(LazySwitchMultiBase):
    COUNT = 2
    RETURN_TYPES = (ANY, ANY)
    RETURN_NAMES = ("value_1", "value_2")
    OUTPUT_TOOLTIPS = (_LAZY_VALUE_TOOLTIP,) * 2


class LazySwitch3(LazySwitchMultiBase):
    COUNT = 3
    RETURN_TYPES = (ANY, ANY, ANY)
    RETURN_NAMES = ("value_1", "value_2", "value_3")
    OUTPUT_TOOLTIPS = (_LAZY_VALUE_TOOLTIP,) * 3


class DownscaleImageToMegapixels:
    CATEGORY = "obvpm/image"
    FUNCTION = "downscale"
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    DESCRIPTION = (
        "Scales an image down so its total pixel count fits within the given "
        "megapixels, keeping aspect ratio. Only changes the image if it is "
        "larger than megapixels; smaller images pass through untouched. "
        "With no image connected it bypasses (outputs None)."
    )

    OUTPUT_TOOLTIPS = (
        "The image, scaled down if it exceeded the megapixel target. None when no image is connected.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "megapixels": ("FLOAT", {
                    "default": 1.0, "min": 0.01, "max": 128.0, "step": 0.01,
                    "tooltip": "Maximum output size in megapixels (1.0 = 1024x1024 pixels). Larger images are scaled down to fit; smaller ones pass through untouched.",
                }),
                "method": (["lanczos", "area", "bicubic", "bilinear", "nearest-exact"], {
                    "default": "lanczos",
                    "tooltip": "Resampling filter used when downscaling.",
                }),
            },
            "optional": {
                "image": ("IMAGE", {
                    "tooltip": "The image to downscale. Leave unconnected to output None (bypass).",
                }),
            },
        }

    def downscale(self, megapixels, method, image=None):
        if image is None:
            return (None,)
        import comfy.utils

        height, width = image.shape[1], image.shape[2]
        target = megapixels * 1024 * 1024
        current = width * height
        if current <= target:
            return (image,)
        scale = (target / current) ** 0.5
        new_width = max(1, round(width * scale))
        new_height = max(1, round(height * scale))
        samples = image.movedim(-1, 1)
        samples = comfy.utils.common_upscale(samples, new_width, new_height, method, "disabled")
        return (samples.movedim(1, -1),)


class FirstValueBase:
    CATEGORY = "obvpm/values"
    FUNCTION = "select"
    SLOTS = 3
    DESCRIPTION = (
        "Outputs the first connected input that carries a value, or the "
        "fallback widget value when none do. Inputs fed by a gate in bypass "
        "mode (None) are skipped over, so this fans multiple optional paths "
        "back into one guaranteed value."
    )

    OUTPUT_TOOLTIPS = (
        "The first connected input that has a value, or the fallback.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        name = cls.TYPE.lower()
        return {
            "required": {
                "fallback": (cls.TYPE, {
                    **cls.FALLBACK_OPTS,
                    "tooltip": "Output when none of the inputs carry a value.",
                }),
            },
            "optional": {
                f"{name}{i}": (cls.TYPE, {
                    "forceInput": True,
                    "tooltip": f"Candidate {i}: used when it is the first connected input with a value. Bypassed (None) inputs are skipped.",
                })
                for i in range(1, cls.SLOTS + 1)
            },
        }

    def select(self, fallback, **values):
        name = self.TYPE.lower()
        for i in range(1, self.SLOTS + 1):
            value = values.get(f"{name}{i}")
            if value is not None:
                return (value,)
        return (fallback,)


class FirstFloat(FirstValueBase):
    TYPE = "FLOAT"
    RETURN_TYPES = ("FLOAT",)
    RETURN_NAMES = ("float",)
    FALLBACK_OPTS = {"default": 0.0, "min": -1.0e18, "max": 1.0e18, "step": 0.01}


class FirstInt(FirstValueBase):
    TYPE = "INT"
    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("int",)
    FALLBACK_OPTS = {"default": 0, "min": -2**53, "max": 2**53}


NODE_CLASS_MAPPINGS = {
    "ImageOptionalGate": ImageOptionalGate,
    "VideoOptionalGate": VideoOptionalGate,
    "AudioOptionalGate": AudioOptionalGate,
    "ModelOptionalGate": ModelOptionalGate,
    "LatentOptionalGate": LatentOptionalGate,
    "AnyOptionalGate": AnyOptionalGate,
    "MuteGate": MuteGate,
    "LazySwitch": LazySwitch,
    "LazySwitch2": LazySwitch2,
    "LazySwitch3": LazySwitch3,
    "DownscaleImageToMegapixels": DownscaleImageToMegapixels,
    "FirstFloat": FirstFloat,
    "FirstInt": FirstInt,
    "LoadImageCrop": LoadImageCrop,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ImageOptionalGate": "Optional Image",
    "VideoOptionalGate": "Optional Video",
    "AudioOptionalGate": "Optional Audio",
    "ModelOptionalGate": "Required Model",
    "LatentOptionalGate": "Optional Latent",
    "AnyOptionalGate": "Optional Any",
    "MuteGate": "Mute",
    "LazySwitch": "Lazy Switch",
    "LazySwitch2": "Lazy Switch 2 Values",
    "LazySwitch3": "Lazy Switch 3 Values",
    "DownscaleImageToMegapixels": "Downscale Image to Megapixels",
    "FirstFloat": "First Float (else fallback)",
    "FirstInt": "First Int (else fallback)",
    "LoadImageCrop": "Load Image & Crop",
}

WEB_DIRECTORY = "./web"

# h3 subpackage: guarded import -- H3 breakage (missing dependency, moved
# ComfyUI internals) must never take down the gates/switches above.
try:
    from .h3 import (
        NODE_CLASS_MAPPINGS as _H3_CLASSES,
        NODE_DISPLAY_NAME_MAPPINGS as _H3_NAMES,
    )
except Exception:
    import logging
    logging.getLogger("obvpm").exception(
        "obvpm: the h3 subpackage failed to import; h3 nodes are "
        "unavailable, the rest of obvpm is unaffected")
else:
    NODE_CLASS_MAPPINGS.update(_H3_CLASSES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_H3_NAMES)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
