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
