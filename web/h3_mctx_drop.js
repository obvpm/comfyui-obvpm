import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Drag & drop videos onto the H3 MCtx loader nodes. Uses the classic
// node.onDragOver/onDragDrop prototype API and core's /upload/image
// endpoint (a general file-upload route; type: "output" targets the
// output folder, where the loaders browse). Dropping an MP4 together
// with its .mctx.safetensors sidecar uploads the pair, so a take moved
// from elsewhere keeps its lineage and hash-verifies on load.

const NODES = ["H3LoadVideoWithMCtx", "H3LoadMCtx"];
const SUBFOLDER = "dropped";
const VIDEO_RE = /\.(mp4|mkv|webm|mov)$/i;
const SIDECAR_SUFFIX = ".mctx.safetensors";

function dbg(...args) {
    console.log("[obvpm-h3-drop]", ...args);
}

async function uploadToOutput(file, overwrite, asName) {
    const body = new FormData();
    body.append("image", file, asName ?? file.name);
    body.append("type", "output");
    body.append("subfolder", SUBFOLDER);
    body.append("overwrite", overwrite ? "true" : "false");
    const resp = await api.fetchApi("/upload/image", { method: "POST", body });
    if (resp.status !== 200) {
        throw new Error(`upload of ${file.name} failed: ${resp.status} ${resp.statusText}`);
    }
    // On name collision (without overwrite) the server RENAMES the file
    // ("name (1).mp4") and returns the final name -- always use it.
    return resp.json();
}

function baseName(name) {
    return name.replace(/\.[^.]+$/, "");
}

app.registerExtension({
    name: "obvpm.h3_mctx_drop",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODES.includes(nodeData.name)) return;

        // wrap-and-chain: another extension may also handle drags
        const prevDragOver = nodeType.prototype.onDragOver;
        nodeType.prototype.onDragOver = function (e) {
            if (prevDragOver?.apply(this, arguments)) return true;
            return !!Array.from(e?.dataTransfer?.items ?? [])
                .some((item) => item.kind === "file");
        };

        const prevDragDrop = nodeType.prototype.onDragDrop;
        nodeType.prototype.onDragDrop = async function (e) {
            if (await prevDragDrop?.apply(this, arguments)) return true;
            const node = this;
            const files = Array.from(e?.dataTransfer?.files ?? []);
            const video = files.find(
                (f) => VIDEO_RE.test(f.name) || f.type?.startsWith("video/"));
            if (!video) return false;

            const sidecar = files.find((f) =>
                f.name.toLowerCase().endsWith(SIDECAR_SUFFIX));
            if (sidecar &&
                baseName(baseName(sidecar.name)) !== baseName(video.name)) {
                dbg("sidecar", sidecar.name, "does not match video",
                    video.name, "- uploading the video only");
            }

            let finalName;
            try {
                const uploaded = await uploadToOutput(video, false);
                finalName = uploaded.name; // may differ: collision rename
                if (sidecar &&
                    baseName(baseName(sidecar.name)) === baseName(video.name)) {
                    // keep the pair's basenames in lock step even when the
                    // video was collision-renamed, or pairing breaks
                    const sidecarName =
                        baseName(finalName) + SIDECAR_SUFFIX;
                    await uploadToOutput(sidecar, true, sidecarName);
                    dbg("uploaded clip pair:", finalName, "+", sidecarName);
                } else {
                    dbg("uploaded video:", finalName);
                }
            } catch (err) {
                dbg("upload failed:", err);
                return false;
            }

            // Select the uploaded clip: add to the combo's values if new,
            // set it, and fire the callback chain (preview refresh rides
            // the wrapped combo callback).
            const clipWidget = node.widgets?.find((w) => w.name === "clip");
            if (clipWidget) {
                const value = `${SUBFOLDER}/${finalName}`;
                const values = clipWidget.options?.values;
                if (Array.isArray(values) && !values.includes(value)) {
                    values.push(value);
                    values.sort();
                }
                clipWidget.value = value;
                clipWidget.callback?.(value);
                node.setDirtyCanvas?.(true, true);
            }
            return true;
        };
    },
});
