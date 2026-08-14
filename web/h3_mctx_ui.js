import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Video preview for the H3 MCtx loader nodes, mirroring core's own
// useNodeVideo pattern (src/composables/node/useNodeImage.ts) exactly:
// a comfy-img-preview container div as the DOM widget, a growable
// computeLayoutSize override with min dimensions refreshed on load, and
// canvas-gesture forwarding so wheel/middle-drag over the video still
// pan/zoom the graph. The node auto-grows via the layout; no manual
// sizing anywhere.

const NODES = ["H3LoadVideoWithMCtx", "H3LoadMCtx"];
const DEFAULT_SIZE = 256;
const MIN_HEIGHT = 64;

// ---- diagnostics (set DEBUG true when hunting sizing bugs) ---------------
const DEBUG = false;
function dbg(...args) {
    if (DEBUG) console.log("[obvpm-h3-preview]", ...args);
}
dbg("extension loaded; vueNodesMode =",
    typeof LiteGraph !== "undefined" && !!LiteGraph.vueNodesMode);

// Samples the full sizing chain twice a second and logs ONLY when values
// change, tagging which link moved. Reproduce the freeze, then paste the
// console lines: the last link that stops changing is the broken one.
function attachSampler(node, widget, el) {
    if (!DEBUG) return;
    let prev = "";
    const sample = () => {
        if (!node.graph) return; // node removed; stop
        const r = el.getBoundingClientRect();
        const p = el.parentElement;
        const pr = p ? p.getBoundingClientRect() : null;
        const snap = {
            nodeSize: [Math.round(node.size[0]), Math.round(node.size[1])],
            widgetY: Math.round(widget.y ?? -1),
            computedHeight: Math.round(widget.computedHeight ?? -1),
            widgetWidth: widget.width ?? null,
            layout: widget.computeLayoutSize
                ? widget.computeLayoutSize(node) : null,
            videoRect: [Math.round(r.width), Math.round(r.height)],
            containerRect: pr ? [Math.round(pr.width), Math.round(pr.height)] : null,
            containerStyle: p ? { w: p.style.width, h: p.style.height } : null,
        };
        const s = JSON.stringify(snap);
        if (s !== prev) {
            prev = s;
            dbg("node", node.id, s);
        }
        setTimeout(sample, 500);
    };
    setTimeout(sample, 500);
}
// --------------------------------------------------------------------------

function viewRoute(value) {
    if (!value) return null;
    let filename = String(value);
    let subfolder = "";
    const slash = filename.lastIndexOf("/");
    if (slash >= 0) {
        subfolder = filename.slice(0, slash);
        filename = filename.slice(slash + 1);
    }
    return `/view?filename=${encodeURIComponent(filename)}` +
        `&subfolder=${encodeURIComponent(subfolder)}&type=output`;
}

function viewURL(clipValue) {
    const route = viewRoute(clipValue);
    return route ? api.apiURL(route) : null;
}

// ---- mctx sidecar badge --------------------------------------------------

const SIDECAR_SUFFIX = ".mctx.safetensors";
const MCTX_FORMAT = "mctx_v1";

function sidecarValue(clipValue) {
    // mctx.sidecar_path convention: video extension REPLACED by the suffix
    return String(clipValue).replace(/\.[^.]+$/, "") + SIDECAR_SUFFIX;
}

// Range-read the sidecar's safetensors header, mirroring mctx.read_header:
// 8 bytes little-endian header length, then that much JSON whose
// "__metadata__" holds the string metadata. Never touches the tensors.
// Returns the metadata dict, null when the file is absent, and throws
// when a file is there but is not a plausible mctx_v1 sidecar.
async function readSidecarMeta(value) {
    const route = viewRoute(value);
    const r1 = await api.fetchApi(route,
        { headers: { Range: "bytes=0-7" } });
    if (r1.status === 404) return null;
    // FileResponse honors Range; anything else risks pulling whole tensors
    if (r1.status !== 206) throw new Error(`no ranged read (${r1.status})`);
    const prefix = await r1.arrayBuffer();
    if (prefix.byteLength < 8) throw new Error("too short for safetensors");
    const length = Number(new DataView(prefix).getBigUint64(0, true));
    if (length <= 0 || length > 10 * 1024 * 1024) {
        throw new Error(`implausible header length ${length}`);
    }
    const r2 = await api.fetchApi(route,
        { headers: { Range: `bytes=8-${8 + length - 1}` } });
    if (r2.status !== 206) throw new Error(`no ranged read (${r2.status})`);
    const meta = JSON.parse(await r2.text())?.__metadata__ ?? {};
    if (meta.format !== MCTX_FORMAT) {
        throw new Error(`format=${meta.format ?? "?"}`);
    }
    return meta;
}

const short = (id) => (id ? String(id).slice(0, 12) : "");

function mctxTooltip(meta) {
    const lines = [`mctx sidecar found (${meta.format})`,
        `id ${short(meta.self_id)}`];
    if (meta.parent_id) {
        lines.push(`${meta.relation || "related to"} ${short(meta.parent_id)}`
            + (meta.parent_join_frame !== undefined && meta.parent_join_frame !== ""
                ? ` @ frame ${meta.parent_join_frame}` : ""));
    } else {
        lines.push("root clip (no parent)");
    }
    if (meta.width) {
        lines.push(`${meta.width}x${meta.height} @ ${meta.fps}fps`);
    }
    if (meta.delivered_frames) {
        lines.push(`${meta.delivered_frames} frames delivered`
            + ` (${meta.raw_frames} raw, pinned ${meta.pinned_head_frames}`
            + `+${meta.pinned_tail_frames})`);
    }
    lines.push("pairing hash is verified at load time");
    return lines.join("\n");
}

app.registerExtension({
    name: "obvpm.h3_mctx_preview",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODES.includes(nodeData.name)) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            const node = this;
            const clipWidget = node.widgets?.find((w) => w.name === "clip");
            if (!clipWidget) return result;

            // Container div is the widget element; core's
            // .comfy-img-preview CSS sizes media children to fit.
            const container = document.createElement("div");
            container.classList.add("comfy-img-preview");

            const el = document.createElement("video");
            el.playsInline = true;
            el.controls = true;
            el.loop = true;
            el.muted = true; // autoplay policy; unmute via controls
            el.autoplay = true;

            // Sidecar badge: does the selected clip have its mctx? The
            // wrapper Vue component positions the widget element but does
            // not set its inline position, so anchoring the container is
            // safe -- the badge rides the video's top-left corner.
            const badge = document.createElement("div");
            Object.assign(badge.style, {
                position: "absolute", top: "4px", left: "4px",
                padding: "1px 7px", borderRadius: "9px",
                font: "10px sans-serif", lineHeight: "16px",
                background: "rgba(0,0,0,0.65)", color: "#fff",
                zIndex: "1", whiteSpace: "pre",
            });
            badge.textContent = "";
            if (!container.style.position) {
                container.style.position = "relative";
            }
            container.replaceChildren(el, badge);

            let badgeSeq = 0; // async probe guard (gotchas section 9)
            async function updateBadge(value) {
                const seq = ++badgeSeq;
                // bright white text throughout; state lives in the bg color
                const set = (text, bg, tip) => {
                    if (seq !== badgeSeq) return;
                    badge.textContent = text;
                    badge.style.background = bg;
                    badge.title = tip;
                };
                if (!value) return set("", "rgba(0,0,0,0.65)", "");
                try {
                    const meta = await readSidecarMeta(sidecarValue(value));
                    if (meta) {
                        set("mctx ✓", "rgba(30,110,50,0.85)",
                            mctxTooltip(meta));
                    } else {
                        set("no mctx", "rgba(170,40,40,0.85)",
                            "No .mctx.safetensors next to this clip.\n" +
                            "Loads without latents (MCTX = None); extends " +
                            "must go through the pixel route.");
                    }
                } catch (err) {
                    dbg("sidecar probe failed for", value, err);
                    set("mctx ?", "rgba(190,120,30,0.85)",
                        "A sidecar file exists but is not a readable " +
                        `mctx_v1 sidecar (${err?.message ?? err}).`);
                }
            }

            // Growable widget with aspect-derived minimums, exactly like
            // core's video preview: refreshed when a video loads, consumed
            // by the layout on every arrange pass.
            let minWidth = DEFAULT_SIZE;
            let minHeight = DEFAULT_SIZE;
            el.addEventListener("loadeddata", () => {
                if (el.videoWidth > 0) {
                    minWidth = node.size?.[0] || DEFAULT_SIZE;
                    minHeight = Math.max(
                        minWidth * (el.videoHeight / el.videoWidth),
                        MIN_HEIGHT);
                }
                dbg("loadeddata: video", el.videoWidth, "x", el.videoHeight,
                    "-> min", Math.round(minWidth), "x", Math.round(minHeight),
                    "nodeSize", [...node.size]);
                node.graph?.setDirtyCanvas(true);
            });
            el.addEventListener("error", () =>
                dbg("video error for", clipWidget.value, el.error));

            const widget = node.addDOMWidget("mctx_preview", "video",
                container, { hideOnZoom: false });
            widget.serialize = false;
            widget.options.serialize = false;
            widget.computeLayoutSize = () => ({ minHeight, minWidth });
            // THE frozen-width culprit (diagnosed via the sampler): once
            // something stores widget.width (the Vue legacy-widget mirror
            // does: WidgetLegacy.vue `widgetInstance.width = width`), the
            // element-size formula in DomWidgets.vue --
            //   width = (widget.width ?? node.width) - margin*2
            // -- prefers the stored value FOREVER, freezing the element's
            // width while height keeps tracking. Shield it: reads yield
            // undefined so the live node.width fallback always wins.
            Object.defineProperty(widget, "width", {
                configurable: true,
                get: () => undefined,
                set: () => {},
            });

            // Forward canvas gestures so the video doesn't swallow graph
            // navigation (core does the same via useCanvasInteractions).
            container.addEventListener("wheel", (e) => {
                const canvasEl = app.canvas?.canvas;
                if (!canvasEl) return;
                e.preventDefault();
                e.stopPropagation();
                const { clientX, clientY, deltaX, deltaY,
                        ctrlKey, metaKey, shiftKey } = e;
                canvasEl.dispatchEvent(new WheelEvent("wheel", {
                    clientX, clientY, deltaX, deltaY,
                    ctrlKey, metaKey, shiftKey,
                }));
            });
            const forwardMiddle = (e) => {
                if (e.button === 1 || (e.buttons & 4)) {
                    const canvasEl = app.canvas?.canvas;
                    if (!canvasEl) return;
                    e.preventDefault();
                    e.stopPropagation();
                    canvasEl.dispatchEvent(new PointerEvent(e.type, e));
                }
            };
            container.addEventListener("pointerdown", forwardMiddle);
            container.addEventListener("pointermove", forwardMiddle);

            // The container is a fixed-position DOM overlay ABOVE the
            // canvas: file drags over it never reach the canvas element,
            // so app.dragOverNode is never set and the node's classic
            // onDragDrop path is dead wherever the preview covers the
            // node (gotchas 12). Accept the drop here and route it to
            // the same node API the canvas path uses.
            // Accept both OS file drags and Artius-browser card drags
            // (custom MIME, no File objects). Routing Artius drops here is
            // a bonus: the overlay sits above the canvas, so Artius's own
            // capture-phase canvas bridge (which spawns a LoadVideo node)
            // never sees them either.
            const ARTIUS_MIME = "application/x-timesaver-artius-asset";
            const dropTargetsUs = (dt) =>
                Array.from(dt?.types ?? []).includes(ARTIUS_MIME) ||
                Array.from(dt?.items ?? []).some((i) => i.kind === "file");
            container.addEventListener("dragover", (e) => {
                if (dropTargetsUs(e.dataTransfer)) {
                    e.preventDefault(); // permits dropping here
                    e.stopPropagation();
                }
            });
            container.addEventListener("drop", (e) => {
                if (!dropTargetsUs(e.dataTransfer)) return;
                e.preventDefault();  // document drop handler checks this
                e.stopPropagation();
                node.onDragDrop?.(e);
            });
            if (DEBUG) {
                el.addEventListener("click", () =>
                    dbg("video clicked; node", node.id, "size", [...node.size]));
                attachSampler(node, widget, el);
            }
            dbg("widget attached to node", node.id, nodeData.name)

            let current = null;
            function updateSrc() {
                const value = clipWidget.value;
                if (value === current) return;
                current = value;
                const url = viewURL(value);
                if (url) {
                    el.src = url;
                } else {
                    el.removeAttribute("src");
                    el.load();
                }
                void updateBadge(value);
            }

            // user changes: wrap-and-chain the combo callback...
            const prevCallback = clipWidget.callback;
            clipWidget.callback = function (...args) {
                const r = prevCallback?.apply(this, args);
                updateSrc();
                return r;
            };
            // ...workflow load: widgets_values arrive with NO callbacks.
            const onConfigure = node.onConfigure;
            node.onConfigure = function () {
                const r = onConfigure?.apply(this, arguments);
                updateSrc();
                return r;
            };

            const onRemoved = node.onRemoved;
            node.onRemoved = function () {
                el.pause();
                el.removeAttribute("src");
                el.load();
                return onRemoved?.apply(this, arguments);
            };

            updateSrc();
            return result;
        };
    },
});

// ======================= H3 MCtx Assemble: timeline =======================
// Lives in this file (which demonstrably loads -- the badge proves it)
// after a standalone h3_mctx_timeline.js was served by /extensions yet
// never executed in the user's browser; cause unresolved. It also reuses
// viewRoute/sidecarValue/readSidecarMeta directly instead of duplicating
// them. The multiline `sequence` widget stays the source of truth; this
// widget renders it as a timeline (blocks sized by played frames, seam
// chips derived like the Python _derive_seam) and previews the cut by
// chaining clips in one <video> honoring the derived enter/exit points.

const TL_NODE = "H3Timeline";
const TL_FPS = 24;
const TL_DRAG_MIME = "application/x-obvpm-timeline-index";
const TL_PICKER_CLASS = "H3LoadVideoWithMCtx"; // combo lists the output tree
const TL_COLORS = { seamless: "#4d9960", cut: "#c9973b", butt: "#a4aab4" };

function tldbg(...args) {
    console.log("[obvpm-h3-timeline]", ...args);
}
tldbg("timeline code loaded, build v9-timeline-node");

function tlParseSequence(text) {
    const out = [];
    for (const raw of String(text || "").split("\n")) {
        const line = raw.trim();
        if (!line || line.startsWith("#")) continue;
        const m = line.match(/^(.+?)(?:\s*@\s*(\d+))?$/);
        out.push({ clip: m[1].trim(),
                   enterOverride: m[2] === undefined ? null : Number(m[2]) });
    }
    return out;
}

// mirrors nodes_assemble._derive_seam; returns cuts for the RIGHT clip's
// enter and the LEFT clip's exit, plus a display verdict
function tlDeriveSeam(lh, rh) {
    if (!lh || !rh) return { exitL: null, enterR: 0, kind: "butt",
                             note: "no sidecar data: plays back-to-back" };
    const num = (m, k) => Number(m[k] ?? 0) || 0;
    if (rh.relation === "extends" && rh.parent_id === lh.self_id) {
        const exitF = num(rh, "parent_join_frame") -
            num(lh, "pinned_head_frames");
        const delivered = num(lh, "delivered_frames");
        const clean = exitF === delivered;
        return { exitL: clean ? null : exitF, enterR: 0,
                 kind: clean ? "seamless" : "cut",
                 note: clean ? "extends: seamless"
                     : `extends at a cut: exits at frame ${exitF}` };
    }
    if (lh.relation === "prepends" && lh.parent_id === rh.self_id) {
        const enter = num(lh, "parent_join_frame") -
            num(rh, "pinned_head_frames");
        return { exitL: null, enterR: enter,
                 kind: enter === 0 ? "seamless" : "cut",
                 note: enter === 0 ? "prepends: seamless"
                     : `prepends: enters at frame ${enter}` };
    }
    return { exitL: null, enterR: 0, kind: "butt",
             note: "no recorded relation: plays back-to-back" };
}

// duration of clips WITHOUT a sidecar: ask the browser for just the
// container metadata (no frame data is fetched with preload="metadata")
const tlFramesCache = new Map();
function tlProbeClipFrames(clip) {
    if (!tlFramesCache.has(clip)) {
        tlFramesCache.set(clip, new Promise((resolve) => {
            const v = document.createElement("video");
            v.preload = "metadata";
            v.onloadedmetadata = () =>
                resolve(Math.round(v.duration * TL_FPS) || 0);
            v.onerror = () => resolve(0);
            v.src = api.apiURL(viewRoute(clip));
        }));
    }
    return tlFramesCache.get(clip);
}

const TL_BUILD = "v7-panel-probe";

// The color of an actually-VISIBLE UI panel beats the legacy CSS var:
// modern Comfy paints its menus with different tokens, and a user
// palette can leave --comfy-menu-bg near-black while the real panels
// are lighter. Sample rendered panels first, fall back to the var.
function tlPanelColor() {
    const sels = [".comfyui-menu", ".side-tool-bar-container",
                  ".comfyui-body-top", ".p-menubar", ".actionbar"];
    for (const sel of sels) {
        const el = document.querySelector(sel);
        if (!el) continue;
        const bg = getComputedStyle(el).backgroundColor;
        if (bg && bg !== "transparent" &&
            !/rgba\(\s*\d+,\s*\d+,\s*\d+,\s*0\)/.test(bg)) {
            return { color: bg, source: sel };
        }
    }
    return { color: tlCssVar("--comfy-menu-bg", "#353535"),
             source: "--comfy-menu-bg" };
}

function tlCssVar(name, fallback) {
    try {
        const v = getComputedStyle(document.documentElement)
            .getPropertyValue(name).trim();
        return v || fallback;
    } catch {
        return fallback;
    }
}

// darken a CSS color by scaling its channels; plain rgb()/rgba() out so
// every engine applies it (fancy color functions get silently rejected
// by older CSSOMs, which leaves the property unset entirely)
function tlDarken(color, f, off) {
    let r, g, b, a = null;
    let m = color.match(/^#([0-9a-f]{3})$/i);
    if (m) {
        [r, g, b] = [...m[1]].map((c) => parseInt(c + c, 16));
    } else if ((m = color.match(/^#([0-9a-f]{6})([0-9a-f]{2})?$/i))) {
        r = parseInt(m[1].slice(0, 2), 16);
        g = parseInt(m[1].slice(2, 4), 16);
        b = parseInt(m[1].slice(4, 6), 16);
        if (m[2]) a = parseInt(m[2], 16) / 255;
    } else if ((m = color.match(/^rgba?\(([^)]+)\)$/i))) {
        const parts = m[1].split(/[,/\s]+/).filter(Boolean).map(Number);
        [r, g, b] = parts;
        if (parts.length > 3) a = parts[3];
        if ([r, g, b].some((x) => !Number.isFinite(x))) return color;
    } else {
        return color; // unknown notation: leave untouched
    }
    const sc = (x) =>
        Math.round(Math.max(0, Math.min(255, x * f + (off || 0))));
    return a === null
        ? `rgb(${sc(r)}, ${sc(g)}, ${sc(b)})`
        : `rgba(${sc(r)}, ${sc(g)}, ${sc(b)}, ${a})`;
}

function tlAlpha(color, a) {
    const c = tlDarken(color, 1, 0); // normalizes to rgb()/rgba()
    const m = c.match(/^rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    return m ? `rgba(${m[1]}, ${m[2]}, ${m[3]}, ${a})` : c;
}

const tlMetaCache = new Map();
async function tlClipMeta(clip) {
    if (!tlMetaCache.has(clip)) {
        let m = null;
        try {
            m = await readSidecarMeta(sidecarValue(clip));
        } catch { /* unreadable sidecar = no seam data */ }
        tlMetaCache.set(clip, m);
    }
    return tlMetaCache.get(clip);
}

async function tlFetchClipList() {
    const resp = await api.fetchApi(
        `/object_info/${encodeURIComponent(TL_PICKER_CLASS)}`);
    if (!resp.ok) return [];
    const info = await resp.json();
    return info?.[TL_PICKER_CLASS]?.input?.required?.clip?.[0] ?? [];
}

app.registerExtension({
    name: "obvpm.h3_mctx_timeline",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== TL_NODE) return;
        tldbg("registering for", nodeData.name);

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            try {
                return buildTimeline(this, result);
            } catch (err) {
                console.error("[obvpm-h3-timeline] widget build FAILED:",
                              err);
                return result;
            }
        };

        function buildTimeline(node, result) {
            const seqWidget = node.widgets?.find(
                (w) => w.name === "sequence");
            if (!seqWidget) {
                tldbg("node", node.id, "has no 'sequence' widget; widgets:",
                      node.widgets?.map((w) => w.name));
                return result;
            }
            tldbg("building timeline widget on node", node.id);

            const container = document.createElement("div");
            Object.assign(container.style, {
                display: "flex", flexDirection: "column", gap: "6px",
                font: "12px sans-serif", overflow: "hidden",
                position: "relative",
            });

            // ---- header: clip picker + preview control ----------------
            const header = document.createElement("div");
            Object.assign(header.style,
                { display: "flex", gap: "6px", alignItems: "center" });
            const picker = document.createElement("select");
            Object.assign(picker.style, {
                flex: "1", minWidth: "0", background: "#2a2e36",
                color: "#e8eaee", border: "1px solid #3a3f48",
                borderRadius: "4px", padding: "2px 4px",
            });
            const mkBtn = (label, title) => {
                const b = document.createElement("button");
                b.textContent = label;
                b.title = title;
                Object.assign(b.style, {
                    background: "#2a2e36", color: "#e8eaee",
                    border: "1px solid #3a3f48", borderRadius: "4px",
                    padding: "2px 8px", cursor: "pointer",
                });
                return b;
            };
            const addBtn = mkBtn("+ add", "Append the picked clip");
            const quickBtn = mkBtn("▶ quick",
                "Instant per-clip preview (near-gapless hops)");
            const fullBtn = mkBtn("▶ full",
                "Build the real cut into a temp file and play it "
                + "seamlessly -- identical content to the export");
            quickBtn.style.minWidth = "90px";
            fullBtn.style.minWidth = "90px";
            const exportBtn = mkBtn("⇪ export",
                "Write the current cut to the output folder under "
                + "filename_prefix (byte-identical to the full preview; "
                + "rebuilds first if the preview is stale)");
            const stateChip = document.createElement("span");
            Object.assign(stateChip.style, {
                font: "12px sans-serif", color: "#9aa1ac",
                whiteSpace: "nowrap",
            });
            // export sits alone at the far right; the built-state text
            // rides with the preview buttons and hides when there is
            // nothing built
            exportBtn.style.marginLeft = "auto";
            exportBtn.style.minWidth = "90px";
            // two rows: playback controls ride with the preview/timeline,
            // the clip picker sits at the very bottom
            header.append(quickBtn, fullBtn, stateChip, exportBtn);
            const pickerRow = document.createElement("div");
            Object.assign(pickerRow.style,
                { display: "flex", gap: "6px", alignItems: "center" });
            pickerRow.append(picker, addBtn);

            // ---- ruler + timeline strip + playhead --------------------
            const timelineWrap = document.createElement("div");
            Object.assign(timelineWrap.style, {
                position: "relative", display: "flex",
                flexDirection: "column", gap: "2px",
            });
            const ruler = document.createElement("canvas");
            ruler.height = 16;
            Object.assign(ruler.style, {
                width: "100%", height: "16px", display: "block",
                cursor: "ew-resize",
            });
            ruler.title = "Drag to scrub";
            if (!document.getElementById("obvpm-tl-style")) {
                const st = document.createElement("style");
                st.id = "obvpm-tl-style";
                st.textContent =
                    ".obvpm-tl-strip { scrollbar-width: thin; " +
                    "scrollbar-color: #555b66 transparent; } " +
                    ".obvpm-tl-strip::-webkit-scrollbar { height: 6px; } " +
                    ".obvpm-tl-strip::-webkit-scrollbar-thumb " +
                    "{ background: #555b66; border-radius: 3px; } " +
                    ".obvpm-tl-strip::-webkit-scrollbar-track " +
                    "{ background: transparent; }";
                document.head.appendChild(st);
            }
            const strip = document.createElement("div");
            strip.classList.add("obvpm-tl-strip");
            Object.assign(strip.style, {
                display: "flex", gap: "3px", minHeight: "48px",
                alignItems: "stretch", overflowX: "scroll",
                position: "relative", paddingTop: "6px",
                paddingBottom: "6px",
            });
            // insertion indicator for drag-reorder: a green I-beam in
            // the GAP the dragged clip would land in; hidden when the
            // drop would not move it. Green = action, distinct from the
            // amber time playhead.
            const DROP_GREEN = "#2fce68";
            const dropLine = document.createElement("div");
            Object.assign(dropLine.style, {
                position: "absolute", top: "0", bottom: "0",
                width: "9px", pointerEvents: "none",
                display: "none", zIndex: "3",
            });
            const beam = document.createElement("div");
            Object.assign(beam.style, {
                position: "absolute", top: "0", bottom: "0",
                left: "3px", width: "3px",
                background: DROP_GREEN, borderRadius: "1px",
            });
            const capT = document.createElement("div");
            const capB = document.createElement("div");
            for (const cap of [capT, capB]) {
                Object.assign(cap.style, {
                    position: "absolute", left: "0", width: "9px",
                    height: "3px", background: DROP_GREEN,
                    borderRadius: "1px",
                });
            }
            capT.style.top = "0";
            capB.style.bottom = "0";
            dropLine.append(beam, capT, capB);
            let dragFrom = null;      // index being dragged
            let dropInsertAt = null;  // insertion index in the gap
            const playhead = document.createElement("div");
            Object.assign(playhead.style, {
                position: "absolute", top: "0", bottom: "0",
                width: "2px", background: "#e8a33d",
                pointerEvents: "none", display: "none", zIndex: "2",
            });
            timelineWrap.append(ruler, strip, playhead);

            // Theme-aware strip palette: one hardcoded set cannot serve
            // both Comfy themes (dark blocks read as black holes on a
            // light page). Luminance of the page background decides.
            function themePalette() {
                let light = false;
                try {
                    const bg = getComputedStyle(document.body)
                        .backgroundColor.match(/\d+/g);
                    if (bg) {
                        const [r, g, b] = bg.map(Number);
                        light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 128;
                    }
                } catch { /* default dark */ }
                // dark chrome with light text in BOTH themes, but
                // tuned per ground: lighter on a light page, darker
                // on a dark one; buttons/picker share it
                if (light) {
                    // same theme-variable derivation as dark mode:
                    // surfaces follow the palette's input color, with
                    // dark ink and darker mixes for edges/panel
                    const lb = "var(--comfy-input-bg, #dfe2e8)";
                    return {
                        rest: lb, text: "#23262b", sub: "#5c6270",
                        active: "#3557b0", activeText: "#f2f4f8",
                        edge: tlDarken(tlCssVar(
                            "--comfy-input-bg", "#dfe2e8"), 0.76),
                        tick: "#6a707a", tickLine: "#b6bac3",
                        // the theme's own panel color, not a
                        // black-mix (mixing black into a light color
                        // makes mud, not shade)
                        stripBg: tlAlpha(tlCssVar(
                            "--comfy-input-bg", "#dfe2e8"), 0.4),
                        drop: "#454b55",
                    };
                }
                // dark: surfaces follow the theme's input color; the
                // derived shades (edge/panel) are computed in JS at
                // palette time, not with CSS color functions
                const base = "var(--comfy-input-bg, #17191f)";
                return {
                    rest: base, text: "#e8eaee", sub: "#9aa1ac",
                    active: "#1f4390", activeText: "#e8eaee",
                    edge: tlDarken(tlCssVar(
                        "--comfy-input-bg", "#17191f"), 1.6),
                    tick: "#9aa1ac", tickLine: "#3a3f48",
                    // the clip bars' surface at half opacity; the
                    // node body blends through
                    stripBg: tlAlpha(
                        tlCssVar("--comfy-input-bg", "#17191f"), 0.4),
                    drop: "#d7dbe2",
                };
            }
            let PAL = themePalette();
            function applyChrome() {
                for (const el of [picker, addBtn, quickBtn, fullBtn]) {
                    el.style.background = PAL.rest;
                    el.style.borderColor = PAL.edge;
                    el.style.color = PAL.text;
                }
                timelineWrap.style.background = PAL.stripBg;
                timelineWrap.style.borderRadius = "4px";
                timelineWrap.style.padding = "3px";
                {   // one-line diagnosis: paste this if colors look off
                    const panel = tlPanelColor();
                    requestAnimationFrame(() => tldbg("chrome:",
                        "panel", panel.color, "from", panel.source,
                        "| menu-var",
                        tlCssVar("--comfy-menu-bg", "(unset)"),
                        "| input-var",
                        tlCssVar("--comfy-input-bg", "(unset)"),
                        "| content-bg",
                        tlCssVar("--content-bg", "(unset)"),
                        "| stripBg set to", PAL.stripBg,
                        "| computed",
                        getComputedStyle(timelineWrap).backgroundColor));
                }
                for (const el of [beam, capT, capB]) {
                    el.style.background = PAL.drop;
                }
            }
            applyChrome();

            let cumStarts = [];   // global start frame of each clip
            let playedArr = [];   // played frames of each clip
            let totalFrames = 0;  // of the whole cut
            let blockEls = [];    // the strip's clip blocks, in order
            let linkEls = [];     // lineage connectors between blocks

            // Blocks are NOT linearly proportional to time (min widths,
            // gaps), so every time<->pixel mapping goes through the
            // actual block geometry: a clip's end in the preview then
            // lands exactly on its box edge.
            function frameToX(f) {
                if (!blockEls.length) return 0;
                let k = 0;
                cumStarts.forEach((s, i2) => { if (f >= s) k = i2; });
                const el = blockEls[k];
                const frac = playedArr[k]
                    ? Math.min(1, (f - cumStarts[k]) / playedArr[k]) : 0;
                return el.offsetLeft + frac * el.offsetWidth;
            }
            function xToFrame(x) {
                if (!blockEls.length) return 0;
                for (let k = 0; k < blockEls.length; k++) {
                    const el = blockEls[k];
                    const lo = el.offsetLeft;
                    const hi = lo + el.offsetWidth;
                    if (x < lo) return cumStarts[k]; // in the gap before
                    if (x <= hi || k === blockEls.length - 1) {
                        const frac = Math.max(0, Math.min(1,
                            (x - lo) / (el.offsetWidth || 1)));
                        return cumStarts[k] + frac * playedArr[k];
                    }
                }
                return totalFrames;
            }

            function drawRuler() {
                const w = timelineWrap.clientWidth || 300;
                if (ruler.width !== w) ruler.width = w;
                const ctx = ruler.getContext("2d");
                ctx.clearRect(0, 0, w, 16);
                if (!totalFrames || !blockEls.length) return;
                ctx.fillStyle = PAL.tick;
                ctx.strokeStyle = PAL.tickLine;
                ctx.font = "9px sans-serif";
                const secs = totalFrames / TL_FPS;
                // piecewise mapping compresses seconds inside
                // width-clamped blocks, so density is enforced in
                // PIXELS: skip ticks closer than 8px, labels than 26px
                let lastX = -1e9;
                let lastLabelX = -1e9;
                for (let s = 0; s <= Math.floor(secs); s++) {
                    const x = Math.round(frameToX(s * TL_FPS) -
                        strip.scrollLeft) + 0.5;
                    if (x < 0 || x > w) continue;
                    if (x - lastX < 8) continue;
                    lastX = x;
                    const label = x - lastLabelX >= 26;
                    ctx.beginPath();
                    ctx.moveTo(x, label ? 6 : 11);
                    ctx.lineTo(x, 16);
                    ctx.stroke();
                    if (label) {
                        ctx.fillText(`${s}s`, x + 2, 8);
                        lastLabelX = x;
                    }
                }
            }
            let lastEntries = [];
            const rulerRO = new ResizeObserver(
                () => requestAnimationFrame(() => {
                    drawRuler();
                    drawLinks(lastEntries); // zones sit at computed x
                }));
            rulerRO.observe(timelineWrap);
            let themeTimer = null;
            const themeMO = new MutationObserver(() => {
                clearTimeout(themeTimer);
                themeTimer = setTimeout(() => {
                    // compare COMPUTED values: the var() strings are
                    // constants, but tlAlpha/tlDarken outputs change
                    // whenever the underlying theme vars do
                    const next = themePalette();
                    if (JSON.stringify(next) === JSON.stringify(PAL)) {
                        return;
                    }
                    PAL = next;
                    applyChrome();
                    void refresh(); // reskin blocks/pills/popup chrome
                }, 150);
            });
            for (const t of [document.documentElement, document.body]) {
                themeMO.observe(t, { attributes: true,
                    attributeFilter: ["class", "style", "data-theme"] });
            }
            strip.addEventListener("scroll",
                () => requestAnimationFrame(drawRuler));

            function setPlayhead(f) {
                if (!totalFrames || f == null || !blockEls.length) {
                    playhead.style.display = "none";
                    return;
                }
                const c = Math.max(0, Math.min(f, totalFrames));
                playhead.style.display = "block";
                playhead.style.left =
                    (frameToX(c) - strip.scrollLeft) + "px";
            }

            // ---- preview player ---------------------------------------
            // Double-buffered for near-gapless hops: while one <video>
            // plays, the next clip is already loaded and seeked in the
            // hidden twin, so a seam is a visibility swap + play(), not a
            // src teardown. Boundary cuts run on a per-frame rAF watcher
            // (timeupdate only fires ~4x/s -- up to 250ms late).
            const videoWrap = document.createElement("div");
            videoWrap.classList.add("comfy-img-preview");
            Object.assign(videoWrap.style,
                { flex: "1", position: "relative" });
            const vids = [0, 1].map(() => {
                const v = document.createElement("video");
                v.playsInline = true;
                Object.assign(v.style, {
                    position: "absolute", inset: "0",
                    width: "100%", height: "100%",
                    objectFit: "contain", opacity: "0",
                    pointerEvents: "none",
                });
                return v;
            });
            container.append(videoWrap, header, timelineWrap, pickerRow);
            videoWrap.append(...vids);

            const widget = node.addDOMWidget("mctx_timeline", "div",
                container, { hideOnZoom: false });
            widget.serialize = false;
            widget.options.serialize = false;
            const minHeight = 320;
            widget.computeLayoutSize = () => ({ minHeight, minWidth: 340 });
            Object.defineProperty(widget, "width", {
                configurable: true, get: () => undefined, set: () => {},
            });

            // forward canvas gestures (wheel over the video area only;
            // the strip keeps its own horizontal scrolling)
            videoWrap.addEventListener("wheel", (e) => {
                const canvasEl = app.canvas?.canvas;
                if (!canvasEl) return;
                e.preventDefault();
                e.stopPropagation();
                const { clientX, clientY, deltaX, deltaY,
                        ctrlKey, metaKey, shiftKey } = e;
                canvasEl.dispatchEvent(new WheelEvent("wheel", {
                    clientX, clientY, deltaX, deltaY,
                    ctrlKey, metaKey, shiftKey,
                }));
            });

            // ---- sequence editing (text widget = source of truth) -----
            function currentLines() {
                return String(seqWidget.value || "").split("\n");
            }
            function setSequence(text) {
                seqWidget.value = text;
                if (seqWidget.inputEl) seqWidget.inputEl.value = text;
                seqWidget.callback?.(text);
                void refresh();
            }
            function entryLines() {
                // indices of non-comment lines within the raw line list
                const map = [];
                currentLines().forEach((raw, i) => {
                    const t = raw.trim();
                    if (t && !t.startsWith("#")) map.push(i);
                });
                return map;
            }
            function moveEntry(i, dir) {
                const lines = currentLines();
                const map = entryLines();
                const j = i + dir;
                if (j < 0 || j >= map.length) return;
                [lines[map[i]], lines[map[j]]] =
                    [lines[map[j]], lines[map[i]]];
                setSequence(lines.join("\n"));
            }
            function removeEntry(i) {
                const lines = currentLines();
                lines.splice(entryLines()[i], 1);
                setSequence(lines.join("\n"));
            }
            function reorderEntry(from, to) {
                if (from === to) return;
                const lines = currentLines();
                const [moved] = lines.splice(entryLines()[from], 1);
                // recompute entry positions after the removal
                const map = [];
                lines.forEach((raw, k) => {
                    const t = raw.trim();
                    if (t && !t.startsWith("#")) map.push(k);
                });
                const at = to >= map.length ? lines.length : map[to];
                lines.splice(at, 0, moved);
                setSequence(lines.join("\n"));
            }
            addBtn.addEventListener("click", () => {
                if (!picker.value) return;
                const text = String(seqWidget.value || "").trimEnd();
                setSequence(text ? text + "\n" + picker.value
                                 : picker.value);
            });

            // ---- preview playback -------------------------------------
            let playlist = [];
            let playIdx = -1;
            let act = 0;      // which of the two videos is on screen
            let rafId = null;

            // The user's mute choice, tracked separately from autoplay
            // fallback muting -- copying .muted between the twin videos
            // let one fallback mute stick forever (the self-muting bug).
            let wantMuted = false;
            for (const v of vids) {
                v.addEventListener("volumechange", () => {
                    if (v === vids[act] && !v._progMute) {
                        wantMuted = v.muted;
                    }
                });
            }
            function tryPlay(v) {
                v._progMute = true;
                v.muted = wantMuted;
                setTimeout(() => { v._progMute = false; }, 60);
                v.play().catch(() => {
                    // autoplay policy: retry muted, but never let this
                    // fallback become the remembered preference
                    v._progMute = true;
                    v.muted = true;
                    setTimeout(() => { v._progMute = false; }, 60);
                    v.play().catch(() => {});
                });
            }

            function showActive() {
                vids.forEach((v, k) => {
                    const on = k === act;
                    v.controls = on;
                    v.style.opacity = on ? "1" : "0";
                    v.style.pointerEvents = on ? "auto" : "none";
                });
            }
            function preloadInto(v, item) {
                v._item = item ?? null;
                if (!item) {
                    v.removeAttribute("src");
                    v.load();
                    return;
                }
                v.src = api.apiURL(viewRoute(item.clip));
                v.addEventListener("loadedmetadata", () => {
                    // guard: a later preload may have replaced the src
                    if (v._item === item) {
                        v.currentTime = item.enter / TL_FPS;
                    }
                }, { once: true });
            }
            function globalFrame() {
                const v = vids[act];
                if (usingSingle && single) return v.currentTime * TL_FPS;
                if (playIdx >= 0 && playlist[playIdx]) {
                    return cumStarts[playIdx] + v.currentTime * TL_FPS -
                        playlist[playIdx].enter;
                }
                return null;
            }
            // Until a seek actually lands, the video's currentTime is
            // still the OLD position -- drawing the playhead from it
            // makes the line flash backwards on every cross-clip jump.
            // seekTargetF overrides the display until the seek settles.
            let seekTargetF = null;
            for (const v of vids) {
                v.addEventListener("seeked", () => {
                    if (v === vids[act]) seekTargetF = null;
                });
            }
            function displayFrame() {
                if (seekTargetF !== null) {
                    const gf = globalFrame();
                    if (gf !== null && Math.abs(gf - seekTargetF) < 1) {
                        seekTargetF = null;
                        return gf;
                    }
                    return seekTargetF;
                }
                return globalFrame();
            }
            function watchBoundary() {
                cancelAnimationFrame(rafId);
                const tick = () => {
                    const item = playlist[playIdx];
                    const v = vids[act];
                    if (!item) return;
                    setPlayhead(displayFrame());
                    // half a frame early beats a quarter second late
                    if (item.exit !== null &&
                        v.currentTime >= item.exit / TL_FPS -
                            0.5 / TL_FPS) {
                        playAt(playIdx + 1);
                        return;
                    }
                    rafId = requestAnimationFrame(tick);
                };
                rafId = requestAnimationFrame(tick);
            }
            function playAt(i, offset = 0, autoplay = true) {
                if (i < 0 || i >= playlist.length) {
                    playIdx = -1;
                    cancelAnimationFrame(rafId);
                    return;
                }
                usingSingle = false;
                playIdx = i;
                seekTargetF = cumStarts[i] + offset;
                const item = playlist[i];
                const standby = vids[1 - act];
                if (standby._item === item && standby.readyState >= 1) {
                    vids[act].pause();
                    act = 1 - act;    // the hop: already loaded + seeked
                } else {
                    preloadInto(vids[act], item);
                }
                const v = vids[act];
                const want = (item.enter + offset) / TL_FPS;
                if (v.readyState >= 1) {
                    if (Math.abs(v.currentTime - want) > 0.02) {
                        v.currentTime = want;
                    }
                } else if (offset) {
                    // preloadInto seeks to enter; land on the offset after
                    v.addEventListener("loadedmetadata", () => {
                        if (v._item === item) v.currentTime = want;
                    }, { once: true });
                }
                showActive();
                highlight(i);
                if (autoplay) tryPlay(v);
                preloadInto(vids[1 - act], playlist[i + 1]);
                watchBoundary();
            }
            for (const v of vids) {
                v.addEventListener("ended", () => {
                    if (vids[act] === v && playIdx >= 0 && !single) {
                        playAt(playIdx + 1);
                    }
                });
            }

            // ---- truly seamless preview: server-built temp smart-cut --
            // One real file has no seams to hide (identical content to
            // the export -- same code path). The per-clip double buffer
            // above is the instant "quick" mode and the fallback when
            // the route is unavailable.
            let single = null;       // {url, starts:[frame...]}
            let usingSingle = false; // which mode is on screen
            function setChip(text, color) {
                stateChip.textContent = text;
                stateChip.style.color = color;
            }
            function widgetValue(name, fallback) {
                return node.widgets?.find((w) => w.name === name)
                    ?.value ?? fallback;
            }
            function crfValue() { return widgetValue("crf", 19); }
            async function previewApi(probe) {
                const resp = await api.fetchApi("/obvpm/h3/preview_cut", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        sequence: seqWidget.value, crf: crfValue(),
                        base_folder: widgetValue("base_folder", ""),
                        preview_filename:
                            widgetValue("preview_filename", ""),
                        probe,
                    }),
                });
                if (!resp.ok) throw new Error(await resp.text());
                return resp.json();
            }
            function adoptSingle(d) {
                // one overwritten file per node: the URL only changes via
                // the content key, so ?v= is what defeats the browser cache
                single = {
                    url: api.apiURL(
                        `/view?filename=${encodeURIComponent(d.filename)}` +
                        `&subfolder=${encodeURIComponent(d.subfolder)}` +
                        `&type=${encodeURIComponent(d.type ?? "output")}` +
                        `&v=${encodeURIComponent(d.v ?? "")}`),
                    starts: d.starts,
                };
                setChip("full built ✓", "#4d9960");
            }
            let exportSeq = 0;
            async function doExport() {
                const seq = ++exportSeq;
                exportBtn.disabled = true;
                exportBtn.textContent = "⏳ export";
                try {
                    const resp = await api.fetchApi("/obvpm/h3/export", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            sequence: seqWidget.value, crf: crfValue(),
                            base_folder: widgetValue("base_folder", ""),
                            preview_filename:
                                widgetValue("preview_filename", ""),
                            export_filename_prefix: widgetValue(
                                "export_filename_prefix", "full"),
                        }),
                    });
                    if (!resp.ok) throw new Error(await resp.text());
                    const d = await resp.json();
                    tldbg("exported:", d.path);
                    exportBtn.textContent = "✓ exported";
                    exportBtn.title = "Last export: " + d.path;
                    app.extensionManager?.toast?.add?.({
                        severity: "success", summary: "Cut exported",
                        detail: d.path, life: 4000,
                    });
                    // the export ensured a current build server-side;
                    // adopt it so "▶ full" is instant now
                    void probeFull();
                } catch (err) {
                    tldbg("export failed:", err);
                    exportBtn.textContent = "✗ export";
                    app.extensionManager?.toast?.add?.({
                        severity: "error", summary: "Export failed",
                        detail: String(err?.message ?? err), life: 6000,
                    });
                }
                exportBtn.disabled = false;
                setTimeout(() => {
                    if (seq === exportSeq) exportBtn.textContent = "⇪ export";
                }, 2500);
            }
            exportBtn.addEventListener("click", () => void doExport());
            let probeSeq = 0;
            async function probeFull() {
                const seq = ++probeSeq;
                try {
                    const d = await previewApi(true);
                    if (seq !== probeSeq) return;
                    if (d.cached) adoptSingle(d);
                    else setChip("", "#9aa1ac");
                } catch {
                    if (seq === probeSeq) setChip("", "#9aa1ac");
                }
            }
            function watchSingle() {
                cancelAnimationFrame(rafId);
                const tick = () => {
                    if (!single || !usingSingle) return;
                    const f = displayFrame() ?? 0;
                    setPlayhead(f);
                    let idx = 0;
                    single.starts.forEach((s, k) => {
                        if (f >= s - 0.5) idx = k;
                    });
                    highlight(idx);
                    rafId = requestAnimationFrame(tick);
                };
                rafId = requestAnimationFrame(tick);
            }
            function ensureSingleSrc(cb) {
                const v = vids[act];
                if (v._single !== single.url) {
                    v._single = single.url;
                    v._item = null;
                    v.src = single.url;
                    v.addEventListener("loadedmetadata", cb, { once: true });
                } else {
                    cb();
                }
                showActive();
            }
            function playSingle(i) {
                usingSingle = true;
                seekTargetF = single.starts[i] ?? 0;
                ensureSingleSrc(() => {
                    const v = vids[act];
                    v.currentTime = (single.starts[i] ?? 0) / TL_FPS;
                    tryPlay(v);
                    watchSingle();
                });
            }
            async function playFull(i) {
                if (!single) {
                    fullBtn.disabled = true;
                    fullBtn.textContent = "⏳ building";
                    setChip("building…", "#c9973b");
                    try {
                        adoptSingle(await previewApi(false));
                    } catch (err) {
                        tldbg("full preview failed; quick fallback:", err);
                        setChip("✗ build failed", "#de5b5b");
                    }
                    fullBtn.disabled = false;
                    fullBtn.textContent = "▶ full";
                }
                if (single) playSingle(i);
                else playAt(i);
            }
            // quick/full buttons double as play/pause toggles for their
            // own mode; pressing the other mode's button switches mode
            function activeStarted() {
                return usingSingle
                    ? (single && vids[act]._single === single.url)
                    : playIdx >= 0;
            }
            function updateButtons() {
                const playing = activeStarted() && !vids[act].paused;
                quickBtn.textContent =
                    !usingSingle && playing ? "⏸ quick" : "▶ quick";
                if (!fullBtn.disabled) {
                    fullBtn.textContent =
                        usingSingle && playing ? "⏸ full" : "▶ full";
                }
            }
            for (const v of vids) {
                v.addEventListener("play", updateButtons);
                v.addEventListener("pause", updateButtons);
            }
            quickBtn.addEventListener("click", () => {
                const v = vids[act];
                if (!usingSingle && playIdx >= 0) {
                    if (v.paused) tryPlay(v);
                    else v.pause();
                    return;
                }
                playAt(0);
            });
            fullBtn.addEventListener("click", () => {
                const v = vids[act];
                if (usingSingle && single && v._single === single.url) {
                    if (v.paused) {
                        tryPlay(v);
                        watchSingle();
                    } else {
                        v.pause();
                    }
                    return;
                }
                void playFull(0);
            });

            // ---- ruler scrubbing (both modes, keeps paused state) -----
            function seekGlobal(f) {
                f = Math.max(0, Math.min(f, Math.max(totalFrames - 1, 0)));
                const wasPlaying = activeStarted() && !vids[act].paused;
                seekTargetF = f;
                if (usingSingle && single) {
                    ensureSingleSrc(() => {
                        vids[act].currentTime = f / TL_FPS;
                        watchSingle();
                    });
                    setPlayhead(f);
                    return;
                }
                let k = 0;
                cumStarts.forEach((s, i2) => { if (f >= s) k = i2; });
                const off = f - cumStarts[k];
                if (k === playIdx && vids[act].readyState >= 1) {
                    vids[act].currentTime =
                        (playlist[k].enter + off) / TL_FPS;
                } else {
                    // scrubbing must not start playback by itself
                    playAt(k, off, wasPlaying);
                }
                setPlayhead(f);
            }
            let scrubbing = false;
            function scrubTo(ev) {
                // rect is in SCREEN px but offsetLeft math is LAYOUT px;
                // the canvas zoom CSS-scales the widget, so divide it out
                const r = ruler.getBoundingClientRect();
                const scale = r.width / (ruler.offsetWidth || 1) || 1;
                const x = (ev.clientX - r.left) / scale + strip.scrollLeft;
                seekGlobal(xToFrame(x));
            }
            ruler.addEventListener("pointerdown", (ev) => {
                if (!totalFrames) return;
                ev.preventDefault();
                ev.stopPropagation();
                scrubbing = true;
                ruler.setPointerCapture(ev.pointerId);
                if (single && playIdx < 0 && !usingSingle) {
                    usingSingle = true; // prefer the seamless file
                }
                scrubTo(ev);
            });
            ruler.addEventListener("pointermove", (ev) => {
                if (scrubbing) scrubTo(ev);
            });
            ruler.addEventListener("pointerup", () => {
                scrubbing = false;
            });

            let lastHighlight = -1;
            let linkTinted = false;
            function clearLinkTint() {
                if (!linkTinted) return;
                linkTinted = false;
                highlight(lastHighlight);
            }
            function tintLinkBlock(el) {
                el.style.background = "#1e5231";
                el.style.color = "#e9f5ec";
                linkTinted = true;
            }
            function highlight(active) {
                lastHighlight = active;
                blockEls.forEach((el, i) => {
                    el.style.background =
                        i === active ? PAL.active : PAL.rest;
                    el.style.color =
                        i === active ? PAL.activeText : PAL.text;
                });
            }

            // seam zones: the lineage connector is also a CONTROL. Hover
            // a gap between mctx clips to see the (potential) link;
            // click to pick a compatible clip -- insert one that links
            // here, or (when a link exists) replace either side while
            // keeping the link intact.
            let seamPopup = null;
            function closeSeamPopup() {
                seamPopup?.remove();
                seamPopup = null;
            }
            function linkedMeta(a, b) {
                if (!a || !b) return false;
                return (b.relation === "extends" &&
                        b.parent_id === a.self_id) ||
                       (a.relation === "prepends" &&
                        a.parent_id === b.self_id);
            }
            function drawLinks(entries) {
                linkEls.forEach((el) => el.remove());
                linkEls = [];
                closeSeamPopup();
                entries.forEach((e, i) => {
                    if (!i) return;
                    const L = blockEls[i - 1];
                    const R = blockEls[i];
                    if (!L || !R) return;
                    const hasLink = e.seam && e.seam.kind !== "butt";
                    const offer = entries[i - 1].meta && e.meta;
                    if (!hasLink && !offer) return;
                    const x = (L.offsetLeft + L.offsetWidth +
                               R.offsetLeft) / 2;
                    const zone = document.createElement("div");
                    Object.assign(zone.style, {
                        position: "absolute", top: "0", bottom: "0",
                        left: (x - 9) + "px", width: "18px",
                        zIndex: "4",
                        cursor: offer ? "pointer" : "default",
                    });
                    const pill = document.createElement("div");
                    Object.assign(pill.style, {
                        position: "absolute", top: "50%", left: "2px",
                        transform: "translateY(-50%)", width: "14px",
                        height: "6px", borderRadius: "3px",
                        background: hasLink
                            ? TL_COLORS[e.seam.kind] : PAL.sub,
                        boxShadow: "0 0 0 2px rgba(0,0,0,0.35)",
                        opacity: hasLink ? "1" : "0.35",
                        transition: "opacity 0.1s, height 0.1s",
                        pointerEvents: "none",
                    });
                    zone.title = (hasLink ? e.seam.note
                        : "no link") + (offer
                        ? "\nclick: insert or swap a linking clip" : "");
                    zone.appendChild(pill);
                    if (offer) {
                        zone.addEventListener("mouseenter", () => {
                            pill.style.opacity = "1";
                            pill.style.height = "11px";
                        });
                        zone.addEventListener("mouseleave", () => {
                            pill.style.height = "6px";
                            if (!hasLink) pill.style.opacity = "0.35";
                        });
                        zone.addEventListener("click", (ev) => {
                            ev.stopPropagation();
                            void openSeamPopup(i, entries, hasLink, x);
                        });
                    }
                    strip.appendChild(zone);
                    linkEls.push(zone);
                });
            }
            async function openSeamPopup(i, entries, hasLink, x) {
                closeSeamPopup();
                const L = entries[i - 1];
                const R = entries[i];
                const pop = document.createElement("div");
                seamPopup = pop;
                const stripTop = timelineWrap.offsetTop +
                    strip.offsetTop;
                Object.assign(pop.style, {
                    position: "absolute", zIndex: "10",
                    left: Math.max(0, Math.min(
                        timelineWrap.offsetLeft + 3 + x -
                            strip.scrollLeft - 110,
                        container.clientWidth - 240)) + "px",
                    bottom: (container.clientHeight - stripTop + 2)
                        + "px",
                    minWidth: "220px", maxWidth: "320px",
                    maxHeight: "190px", overflowY: "auto",
                    background: PAL.rest, color: PAL.text,
                    border: "1px solid " + PAL.edge,
                    borderRadius: "5px", padding: "6px",
                    font: "11px sans-serif",
                    boxShadow: "0 4px 14px rgba(0,0,0,0.45)",
                });
                pop.addEventListener("click",
                    (ev) => ev.stopPropagation());
                pop.textContent = "scanning clips…";
                container.appendChild(pop);
                setTimeout(() => document.addEventListener("click",
                    closeSeamPopup, { once: true }), 0);

                const values = Array.from(picker.options)
                    .map((o) => o.value);
                const metas = await Promise.all(
                    values.map((v) => tlClipMeta(v)));
                if (seamPopup !== pop) return;
                const cand = values
                    .map((v, k) => ({ clip: v, meta: metas[k] }))
                    .filter((c) => c.meta);

                const section = (title, items, act) => {
                    const h = document.createElement("div");
                    h.textContent = title;
                    Object.assign(h.style, {
                        color: PAL.sub, margin: "4px 0 2px",
                        textTransform: "uppercase",
                        fontSize: "9px", letterSpacing: "0.05em",
                    });
                    pop.appendChild(h);
                    if (!items.length) {
                        const n = document.createElement("div");
                        n.textContent = "none found";
                        n.style.color = PAL.sub;
                        pop.appendChild(n);
                        return;
                    }
                    for (const c of items) {
                        const it = document.createElement("div");
                        it.textContent = c.clip;
                        Object.assign(it.style, {
                            padding: "3px 5px", borderRadius: "3px",
                            cursor: "pointer", whiteSpace: "nowrap",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                        });
                        it.addEventListener("mouseenter", () =>
                            it.style.background = PAL.active);
                        it.addEventListener("mouseleave", () =>
                            it.style.background = "none");
                        it.addEventListener("click", () => {
                            act(c.clip);
                            closeSeamPopup();
                        });
                        pop.appendChild(it);
                    }
                };

                pop.textContent = "";
                const lName = L.clip.split("/").pop();
                const rName = R.clip.split("/").pop();
                const insertAtGap = (clip) => {
                    const ls = currentLines();
                    ls.splice(entryLines()[i], 0, clip);
                    setSequence(ls.join("\n"));
                };
                const replaceAt = (idx, clip) => {
                    const ls = currentLines();
                    ls[entryLines()[idx]] = clip;
                    setSequence(ls.join("\n"));
                };
                if (hasLink) {
                    section(`replace ${lName} (keep link to ${rName})`,
                        cand.filter((c) => c.clip !== L.clip &&
                            linkedMeta(c.meta, R.meta)),
                        (clip) => replaceAt(i - 1, clip));
                    section(`replace ${rName} (keep link to ${lName})`,
                        cand.filter((c) => c.clip !== R.clip &&
                            linkedMeta(L.meta, c.meta)),
                        (clip) => replaceAt(i, clip));
                } else {
                    section(`insert: links after ${lName}`,
                        cand.filter((c) => linkedMeta(L.meta, c.meta)),
                        insertAtGap);
                    section(`insert: links before ${rName}`,
                        cand.filter((c) => linkedMeta(c.meta, R.meta)),
                        insertAtGap);
                }
            }

            // ---- timeline rendering -----------------------------------
            let refreshSeq = 0;
            async function refresh() {
                const seq = ++refreshSeq;
                const entries = tlParseSequence(seqWidget.value);
                const metas = await Promise.all(
                    entries.map((e) => tlClipMeta(e.clip)));
                // clip lengths: sidecar header, else container metadata
                const lens = await Promise.all(entries.map((e, i) =>
                    metas[i]
                        ? Number(metas[i].delivered_frames ?? 0) || 0
                        : tlProbeClipFrames(e.clip)));
                if (seq !== refreshSeq) return;

                // enters/exits exactly like assemble()
                entries.forEach((e, i) => {
                    e.meta = metas[i];
                    e.frames = lens[i];
                    e.enter = 0;
                    e.exit = null;
                });
                for (let i = 1; i < entries.length; i++) {
                    const s = tlDeriveSeam(entries[i - 1].meta,
                                           entries[i].meta);
                    if (entries[i - 1].exit === null) {
                        entries[i - 1].exit = s.exitL;
                    }
                    entries[i].enter = s.enterR;
                    entries[i].seam = s;
                }
                for (const e of entries) {
                    if (e.enterOverride !== null) e.enter = e.enterOverride;
                }

                playlist = entries.map((e) => ({
                    clip: e.clip, enter: e.enter, exit: e.exit,
                }));
                // the sequence changed: previous single file no longer
                // matches; re-probe whether a cached build exists for it
                single = null;
                usingSingle = false;
                setPlayhead(null);
                PAL = themePalette();
                applyChrome();
                cumStarts = [];
                playedArr = [];
                totalFrames = 0;
                entries.forEach((e) => {
                    const played =
                        Math.max(0, (e.exit ?? e.frames) - e.enter);
                    cumStarts.push(totalFrames);
                    playedArr.push(played);
                    totalFrames += played;
                });
                updateButtons();
                void probeFull();

                strip.replaceChildren();
                strip.appendChild(dropLine);
                blockEls = [];
                entries.forEach((e, i) => {
                    const delivered = e.frames;
                    const played = (e.exit ?? delivered) - e.enter;
                    const block = document.createElement("div");
                    Object.assign(block.style, {
                        flexGrow: String(Math.max(played, 20)),
                        flexBasis: "0", minWidth: "88px",
                        background: PAL.rest,
                        border: "1px solid " + PAL.edge,
                        borderLeft: "4px solid " +
                            (i === 0 ? "#a4aab4"
                                     : TL_COLORS[e.seam?.kind ?? "butt"]),
                        borderRadius: "4px", padding: "3px 6px",
                        display: "flex", flexDirection: "column",
                        gap: "1px", cursor: "pointer", color: PAL.text,
                        overflow: "hidden",
                    });
                    block.title = (i ? e.seam?.note + "\n" : "") +
                        (e.meta ? "mctx ✓" : "no mctx") +
                        (e.enter || e.exit !== null
                            ? `\nplays ${e.enter}..${e.exit ?? (delivered || "end")}`
                            : "");
                    const name = document.createElement("div");
                    name.textContent = e.clip.split("/").pop();
                    Object.assign(name.style, {
                        whiteSpace: "nowrap", textOverflow: "ellipsis",
                        overflow: "hidden", font: "600 11px sans-serif",
                    });
                    const sub = document.createElement("div");
                    sub.style.opacity = "0.65";
                    sub.style.font = "10px sans-serif";
                    sub.textContent = played > 0
                        ? `${(played / TL_FPS).toFixed(1)}s` : "?";
                    const ctl = document.createElement("div");
                    Object.assign(ctl.style,
                        { display: "flex", gap: "4px", marginTop: "auto" });
                    const mini = (label, title, fn) => {
                        const b = document.createElement("button");
                        b.textContent = label;
                        b.title = title;
                        Object.assign(b.style, {
                            background: "none", border: "none",
                            color: PAL.sub, cursor: "pointer",
                            padding: "0 2px", font: "11px sans-serif",
                        });
                        b.addEventListener("click", (ev) => {
                            ev.stopPropagation();
                            fn();
                        });
                        return b;
                    };
                    const mbadge = document.createElement("span");
                    Object.assign(mbadge.style, {
                        display: "inline-block", padding: "0 6px",
                        whiteSpace: "nowrap",
                        borderRadius: "8px", textAlign: "center",
                        background: e.meta ? "rgba(30,110,50,0.85)"
                                           : "rgba(170,40,40,0.85)",
                        color: "#fff", font: "9px/13px sans-serif",
                    });
                    mbadge.textContent = e.meta ? "mctx ✓" : "no mctx";
                    mbadge.title = e.meta
                        ? "verified mctx sidecar"
                        : "no mctx sidecar (pixel route only)";
                    const subRow = document.createElement("div");
                    Object.assign(subRow.style, {
                        display: "flex", gap: "5px",
                        alignItems: "center",
                    });
                    subRow.append(sub, mbadge);
                    ctl.append(
                        mini("◀", "Move earlier", () => moveEntry(i, -1)),
                        mini("▶", "Move later", () => moveEntry(i, +1)),
                        mini("✕", "Remove from the sequence",
                             () => removeEntry(i)));
                    block.append(name, subRow, ctl);
                    block.addEventListener("click",
                        () => {
                            const playing = activeStarted() &&
                                !vids[act].paused;
                            if (playing) {
                                (single ? playSingle : playAt)(i);
                            } else {
                                // paused: move there, don't start
                                seekGlobal(cumStarts[i]);
                                highlight(i);
                            }
                        });

                    // drag & drop reorder (custom MIME so nothing here
                    // ever looks like a file drop to the loader nodes)
                    block.draggable = true;
                    block.addEventListener("dragstart", (ev) => {
                        dragFrom = i;
                        ev.dataTransfer.setData(TL_DRAG_MIME, String(i));
                        ev.dataTransfer.effectAllowed = "move";
                    });
                    block.addEventListener("dragover", (ev) => {
                        if (!Array.from(ev.dataTransfer?.types ?? [])
                            .includes(TL_DRAG_MIME)) return;
                        ev.preventDefault();
                        ev.stopPropagation();
                        ev.dataTransfer.dropEffect = "move";
                        // which GAP: left or right half of this block
                        const r = block.getBoundingClientRect();
                        const before = ev.clientX - r.left < r.width / 2;
                        const j = before ? i : i + 1;
                        if (dragFrom === null ||
                            j === dragFrom || j === dragFrom + 1) {
                            // dropping here would not move the clip
                            dropInsertAt = null;
                            dropLine.style.display = "none";
                            clearLinkTint();
                            return;
                        }
                        dropInsertAt = j;
                        // link preview: tint the WHOLE clip(s)
                        // this drop would link with dark green
                        const dragged = entries[dragFrom];
                        const linkL = j > 0 && linkedMeta(
                            entries[j - 1]?.meta, dragged?.meta);
                        const linkR = j < entries.length && linkedMeta(
                            dragged?.meta, entries[j]?.meta);
                        clearLinkTint();
                        if (linkL) tintLinkBlock(blockEls[j - 1]);
                        if (linkR) tintLinkBlock(blockEls[j]);
                        const cx = before
                            ? block.offsetLeft - 1.5
                            : block.offsetLeft + block.offsetWidth + 1.5;
                        dropLine.style.left =
                            Math.max(0, cx - 4.5) + "px";
                        dropLine.style.display = "block";
                    });
                    block.addEventListener("drop", (ev) => {
                        dropLine.style.display = "none";
                        clearLinkTint();
                        if (dragFrom === null || dropInsertAt === null) {
                            return;
                        }
                        ev.preventDefault();
                        ev.stopPropagation();
                        // insertion index -> position after removal
                        const to = dropInsertAt > dragFrom
                            ? dropInsertAt - 1 : dropInsertAt;
                        reorderEntry(dragFrom, to);
                        dragFrom = dropInsertAt = null;
                    });
                    block.addEventListener("dragend", () => {
                        dropLine.style.display = "none";
                        clearLinkTint();
                        dragFrom = dropInsertAt = null;
                    });
                    blockEls.push(block);
                    strip.appendChild(block);
                });
                highlight(playIdx);
                // ruler + link geometry depend on freshly laid-out blocks
                lastEntries = entries;
                requestAnimationFrame(() => {
                    drawRuler();
                    drawLinks(entries);
                });
                node.graph?.setDirtyCanvas(true);
            }

            // ---- wiring ----------------------------------------------
            const prevCallback = seqWidget.callback;
            seqWidget.callback = function (...args) {
                const r = prevCallback?.apply(this, args);
                void refresh();
                return r;
            };
            // live re-render while typing in the textarea
            let typeTimer = null;
            seqWidget.inputEl?.addEventListener("input", () => {
                clearTimeout(typeTimer);
                typeTimer = setTimeout(() => void refresh(), 400);
            });
            const onConfigure = node.onConfigure;
            node.onConfigure = function () {
                const r = onConfigure?.apply(this, arguments);
                void refresh();
                return r;
            };
            const onRemoved = node.onRemoved;
            node.onRemoved = function () {
                cancelAnimationFrame(rafId);
                rulerRO.disconnect();
                themeMO.disconnect();
                for (const v of vids) {
                    v.pause();
                    v.removeAttribute("src");
                    v.load();
                }
                return onRemoved?.apply(this, arguments);
            };

            // picker (and thereby seam-popup candidates) scoped by the
            // base_folder widget; re-filtered live when it changes. The
            // node's own preview file lives in that folder too -- hide
            // it, it is a cut, not a source clip.
            let allPickerClips = [];
            function populatePicker() {
                const folder = String(widgetValue("base_folder", ""))
                    .trim().replace(/^\/+|\/+$/g, "");
                const pv = (folder ? folder + "/" : "") +
                    String(widgetValue("preview_filename",
                                       "obvpm_h3_preview")).trim() +
                    ".mp4";
                const shown = allPickerClips.filter((c) =>
                    c !== pv && (!folder || c.startsWith(folder + "/")));
                picker.replaceChildren(...shown.map((c) => {
                    const o = document.createElement("option");
                    o.value = o.textContent = c;
                    return o;
                }));
            }
            void (async () => {
                allPickerClips = await tlFetchClipList();
                populatePicker();
            })();
            const folderWidget = node.widgets?.find(
                (w) => w.name === "base_folder");
            if (folderWidget) {
                const folderCb = folderWidget.callback;
                folderWidget.callback = function (...args) {
                    const r = folderCb?.apply(this, args);
                    populatePicker();
                    return r;
                };
            }
            void refresh();
            return result;
        }
    },
});
