import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Drag & drop videos onto the H3 MCtx loader nodes. Three drop sources,
// in priority order:
//
//   1. Artius browser cards: no File objects -- a JSON payload under a
//      custom MIME (assets carry root_id/folder_path/filename). Output-
//      root assets are selected IN PLACE (no copy), so the clip stays
//      next to its .mctx.safetensors sidecar and keeps its lineage.
//      Other roots are fetched from Artius's /file route and uploaded.
//   2. OS files already present in the output folder: selected in place
//      instead of re-uploading. The browser hides an OS drag's source
//      path (File exposes only name/size/type/mtime), so the check is a
//      LIVE server-side scan (/object_info re-runs the loader's combo
//      listing) + basename match + byte-size confirmation via a ranged
//      /view request. A wrong match can't corrupt lineage -- sidecar
//      pairing is hash-verified.
//   3. Fresh OS files: uploaded via core's /upload/image (a general
//      file-upload route; type "output" targets the output folder, where
//      the loaders browse). An accompanying .mctx.safetensors in the same
//      drop rides along with its basename kept in lock step.
//
// Return-value contract with core (app.ts addDropHandler): the document
// drop handler falls through to its OWN default -- upload to INPUT plus a
// new LoadVideo node -- whenever the node's onDragDrop returns false. So
// once a drop clearly targets us (it carries a video), we ALWAYS return
// true, even on failure, and report the failure ourselves; returning
// false would produce a mystery duplicate in the input folder.

const NODES = ["H3LoadVideoWithMCtx", "H3LoadMCtx"];
const SUBFOLDER = "dropped";
const VIDEO_RE = /\.(mp4|mkv|webm|mov)$/i;
const SIDECAR_SUFFIX = ".mctx.safetensors";
const ARTIUS_MIME = "application/x-timesaver-artius-asset";
const ARTIUS_ROUTE_BASE = "/asset_browser";

function dbg(...args) {
    console.log("[obvpm-h3-drop]", ...args);
}

function toast(severity, summary, detail) {
    // Best-effort surfacing; the console line above is the reliable record.
    try {
        app.extensionManager?.toast?.add?.({
            severity, summary, detail, life: 6000,
        });
    } catch { /* toast API absent on old frontends */ }
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

function selectClip(node, value) {
    const clipWidget = node.widgets?.find((w) => w.name === "clip");
    if (!clipWidget) return false;
    const values = clipWidget.options?.values;
    if (Array.isArray(values) && !values.includes(value)) {
        values.push(value);
        values.sort();
    }
    clipWidget.value = value;
    // preview refresh rides the wrapped combo callback
    clipWidget.callback?.(value);
    node.setDirtyCanvas?.(true, true);
    return true;
}

// The server re-runs the node's INPUT_TYPES on every /object_info
// request, and the loader's clip combo lists the output tree fresh each
// time -- so this is a live output-folder scan with no custom endpoint.
// (The widget's own options.values are a snapshot from page load and go
// stale the moment a new clip is generated.)
async function fetchFreshClipList(node) {
    const cls = node.comfyClass ?? node.type;
    const resp = await api.fetchApi(
        `/object_info/${encodeURIComponent(cls)}`);
    if (!resp.ok) throw new Error(`object_info ${resp.status}`);
    const info = await resp.json();
    const values = info?.[cls]?.input?.required?.clip?.[0];
    if (!Array.isArray(values)) throw new Error("no clip list in object_info");
    return values;
}

// /view serves output files via FileResponse, which honors Range -- a
// one-byte request yields "Content-Range: bytes 0-0/<total>", i.e. the
// file size without downloading it. Null = file absent / no size.
async function serverFileSize(value) {
    let filename = String(value);
    let subfolder = "";
    const slash = filename.lastIndexOf("/");
    if (slash >= 0) {
        subfolder = filename.slice(0, slash);
        filename = filename.slice(slash + 1);
    }
    const resp = await api.fetchApi(
        `/view?filename=${encodeURIComponent(filename)}` +
        `&subfolder=${encodeURIComponent(subfolder)}&type=output`,
        { headers: { Range: "bytes=0-0" } });
    if (resp.status === 206) {
        const total = resp.headers.get("Content-Range")?.split("/")[1];
        const n = Number(total);
        return Number.isFinite(n) ? n : null;
    }
    if (resp.ok) {
        const n = Number(resp.headers.get("Content-Length"));
        return Number.isFinite(n) ? n : null;
    }
    return null;
}

// An output-folder clip with the same basename AND byte size as the
// dropped file is the same content -- reference it instead of copying.
// Identical copies can coexist (an original beside its sidecar plus a
// bare duplicate in dropped/): prefer the one with an adjacent
// .mctx.safetensors, so selection lands on the lineage-bearing copy.
// Falls back to the (stale) widget list if the live scan fails.
async function findExistingClip(node, file) {
    let values;
    try {
        values = await fetchFreshClipList(node);
        // refresh the combo while we have the live list
        const clipWidget = node.widgets?.find((w) => w.name === "clip");
        if (clipWidget?.options) clipWidget.options.values = values;
    } catch (err) {
        dbg("live clip-list fetch failed, using stale combo:", err);
        values = node.widgets?.find((w) => w.name === "clip")
            ?.options?.values;
    }
    if (!Array.isArray(values)) return null;
    const matches = values.filter(
        (v) => String(v).split("/").pop() === file.name);
    const confirmed = [];
    for (const value of matches) {
        const size = await serverFileSize(value);
        if (size === file.size) confirmed.push(value);
        else dbg("name match", value, "rejected: size", size, "!=", file.size);
    }
    for (const value of confirmed) {
        // sidecar_path convention: video extension REPLACED by the suffix
        if ((await serverFileSize(baseName(value) + SIDECAR_SUFFIX)) !== null) {
            dbg("preferring sidecar-bearing match:", value);
            return value;
        }
    }
    if (confirmed.length > 1) {
        dbg("no match has a sidecar; using first of", confirmed);
    }
    return confirmed[0] ?? null;
}

// ---- Artius browser payloads --------------------------------------------

function readArtiusAssets(e) {
    let raw = "";
    try {
        raw = e?.dataTransfer?.getData(ARTIUS_MIME) || "";
    } catch { /* getData throws outside drop dispatch */ }
    if (!raw) raw = window.__tsArtiusDraggedAsset || "";
    if (!raw) return null;
    try {
        const parsed = JSON.parse(raw);
        return (Array.isArray(parsed) ? parsed : [parsed]).filter(Boolean);
    } catch (err) {
        dbg("unparseable Artius payload:", err);
        return null;
    }
}

function artiusRelativePath(asset) {
    if (!asset?.filename) return "";
    const folder = String(asset.folder_path || "")
        .replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
    return folder ? `${folder}/${asset.filename}` : String(asset.filename);
}

function isArtiusVideo(asset) {
    return asset?.type === "video" ||
        VIDEO_RE.test(String(asset?.filename || ""));
}

async function handleArtiusDrop(node, assets) {
    const asset = assets.find(isArtiusVideo);
    if (!asset) {
        dbg("Artius payload has no video asset:", assets);
        return true; // it targeted us; don't let anyone else re-handle it
    }
    // Consumed: Artius's own dragend/bridge fallback must not re-insert it.
    window.__tsArtiusDraggedAsset = "";

    if (asset.root_id === "output" || asset.scope === "output") {
        // Already in the output folder: reference in place. No copy, and
        // the sidecar (if any) is still right next to it.
        const rel = artiusRelativePath(asset);
        if (rel && selectClip(node, rel)) {
            dbg("selected existing output clip in place:", rel);
            return true;
        }
        dbg("could not resolve Artius output asset path:", asset);
        toast("error", "H3 MCtx drop",
            `Could not resolve path for ${asset?.filename ?? "asset"}`);
        return true;
    }

    // Input/custom roots: not browsable by the loaders (they list the
    // output folder), so a copy is unavoidable. Fetch from Artius's file
    // route and upload. Its index has no sidecars, so lineage does not
    // travel this path -- the loader will report MCTX unverified.
    const src = asset.file_url ||
        (asset.id != null
            ? `${ARTIUS_ROUTE_BASE}/file?id=${encodeURIComponent(String(asset.id))}`
            : "");
    if (!src) {
        dbg("Artius asset has no fetchable source:", asset);
        toast("error", "H3 MCtx drop",
            `No source route for ${asset?.filename ?? "asset"}`);
        return true;
    }
    try {
        const resp = await api.fetchApi(src);
        if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
        const blob = await resp.blob();
        const file = new File([blob], String(asset.filename || "clip.mp4"),
            { type: blob.type || "video/mp4" });
        const uploaded = await uploadToOutput(file, false);
        selectClip(node, `${SUBFOLDER}/${uploaded.name}`);
        dbg("copied Artius asset from root", asset.root_id,
            "to output:", uploaded.name, "(no sidecar travels this path)");
    } catch (err) {
        dbg("Artius asset copy failed:", err);
        toast("error", "H3 MCtx drop", `Copy failed: ${err}`);
    }
    return true;
}

// ---- OS file drops -------------------------------------------------------

async function handleFileDrop(node, files) {
    const video = files.find(
        (f) => VIDEO_RE.test(f.name) || f.type?.startsWith("video/"));
    if (!video) return false; // not ours; let core handle the drop

    // Same name + same size in a live output scan: it IS that file --
    // select it in place, keeping it next to its sidecar.
    const existing = await findExistingClip(node, video);
    if (existing) {
        selectClip(node, existing);
        dbg("matched existing output clip by name, no copy:", existing);
        return true;
    }

    const sidecar = files.find((f) =>
        f.name.toLowerCase().endsWith(SIDECAR_SUFFIX));
    const sidecarMatches = sidecar &&
        baseName(baseName(sidecar.name)) === baseName(video.name);
    if (sidecar && !sidecarMatches) {
        dbg("sidecar", sidecar.name, "does not match video",
            video.name, "- uploading the video only");
    }

    try {
        const uploaded = await uploadToOutput(video, false);
        const finalName = uploaded.name; // may differ: collision rename
        if (sidecarMatches) {
            // keep the pair's basenames in lock step even when the video
            // was collision-renamed, or pairing breaks
            const sidecarName = baseName(finalName) + SIDECAR_SUFFIX;
            await uploadToOutput(sidecar, true, sidecarName);
            dbg("uploaded clip pair:", finalName, "+", sidecarName);
        } else {
            dbg("uploaded video:", finalName,
                "(no sidecar in the drop; MCTX will be unverified)");
        }
        selectClip(node, `${SUBFOLDER}/${finalName}`);
    } catch (err) {
        dbg("upload failed:", err);
        toast("error", "H3 MCtx drop", `Upload failed: ${err}`);
        // The drop DID target us with a video; returning false here would
        // make core upload a duplicate to input + spawn a LoadVideo node.
    }
    return true;
}

// --------------------------------------------------------------------------

app.registerExtension({
    name: "obvpm.h3_mctx_drop",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODES.includes(nodeData.name)) return;

        // wrap-and-chain: another extension may also handle drags
        const prevDragOver = nodeType.prototype.onDragOver;
        nodeType.prototype.onDragOver = function (e) {
            if (prevDragOver?.apply(this, arguments)) return true;
            const types = Array.from(e?.dataTransfer?.types ?? []);
            if (types.includes(ARTIUS_MIME)) return true;
            return Array.from(e?.dataTransfer?.items ?? [])
                .some((item) => item.kind === "file");
        };

        const prevDragDrop = nodeType.prototype.onDragDrop;
        nodeType.prototype.onDragDrop = async function (e) {
            if (await prevDragDrop?.apply(this, arguments)) return true;
            const artius = readArtiusAssets(e);
            if (artius) return handleArtiusDrop(this, artius);
            const files = Array.from(e?.dataTransfer?.files ?? []);
            if (files.length === 0) return false;
            return handleFileDrop(this, files);
        };
    },
});
