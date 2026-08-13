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

const TL_NODE = "H3Assemble";
const TL_FPS = 24;
const TL_PICKER_CLASS = "H3LoadVideoWithMCtx"; // combo lists the output tree
const TL_COLORS = { seamless: "#4d9960", cut: "#c9973b", butt: "#8a8f98" };

function tldbg(...args) {
    console.log("[obvpm-h3-timeline]", ...args);
}
tldbg("timeline code loaded (inside h3_mctx_preview.js)");

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
                             note: "no sidecar data: butt join" };
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
             note: "no recorded relation: butt join" };
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
            const playBtn = mkBtn("▶ preview",
                "Play the sequence with the derived seam cuts");
            header.append(picker, addBtn, playBtn);

            // ---- timeline strip ---------------------------------------
            const strip = document.createElement("div");
            Object.assign(strip.style, {
                display: "flex", gap: "3px", minHeight: "48px",
                alignItems: "stretch", overflowX: "auto",
            });

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
            container.append(header, strip, videoWrap);
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
            function watchBoundary() {
                cancelAnimationFrame(rafId);
                const tick = () => {
                    const item = playlist[playIdx];
                    const v = vids[act];
                    if (!item) return;
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
            function playAt(i) {
                if (i < 0 || i >= playlist.length) {
                    playIdx = -1;
                    cancelAnimationFrame(rafId);
                    return;
                }
                playIdx = i;
                const item = playlist[i];
                const standby = vids[1 - act];
                if (standby._item === item && standby.readyState >= 1) {
                    vids[act].pause();
                    act = 1 - act;    // the hop: already loaded + seeked
                } else {
                    preloadInto(vids[act], item);
                }
                const v = vids[act];
                if (v.readyState >= 1 &&
                    Math.abs(v.currentTime - item.enter / TL_FPS) > 0.05) {
                    v.currentTime = item.enter / TL_FPS;
                }
                showActive();
                highlight(i);
                v.muted = vids[1 - act].muted; // keep the user's choice
                v.play().catch(() => {
                    v.muted = true;
                    v.play().catch(() => {});
                });
                preloadInto(vids[1 - act], playlist[i + 1]);
                watchBoundary();
            }
            for (const v of vids) {
                v.addEventListener("ended", () => {
                    if (vids[act] === v && playIdx >= 0) {
                        playAt(playIdx + 1);
                    }
                });
            }
            playBtn.addEventListener("click", () => playAt(0));

            function highlight(active) {
                Array.from(strip.children).forEach((el, i) => {
                    el.style.outline = i === active
                        ? "2px solid #e8a33d" : "none";
                });
            }

            // ---- timeline rendering -----------------------------------
            let refreshSeq = 0;
            async function refresh() {
                const seq = ++refreshSeq;
                const entries = tlParseSequence(seqWidget.value);
                const metas = await Promise.all(
                    entries.map((e) => tlClipMeta(e.clip)));
                if (seq !== refreshSeq) return;

                // enters/exits exactly like assemble()
                entries.forEach((e, i) => {
                    e.meta = metas[i];
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

                strip.replaceChildren();
                entries.forEach((e, i) => {
                    const delivered =
                        Number(e.meta?.delivered_frames ?? 0) || 0;
                    const played = (e.exit ?? delivered) - e.enter;
                    const block = document.createElement("div");
                    Object.assign(block.style, {
                        flexGrow: String(Math.max(played, 20)),
                        flexBasis: "0", minWidth: "88px",
                        background: "#242832",
                        border: "1px solid #3a3f48",
                        borderLeft: "4px solid " +
                            (i === 0 ? "#3a3f48"
                                     : TL_COLORS[e.seam?.kind ?? "butt"]),
                        borderRadius: "4px", padding: "3px 6px",
                        display: "flex", flexDirection: "column",
                        gap: "1px", cursor: "pointer", color: "#e8eaee",
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
                        overflow: "hidden", fontWeight: "600",
                    });
                    const sub = document.createElement("div");
                    sub.style.opacity = "0.65";
                    sub.textContent = played > 0
                        ? `${played}f · ${(played / TL_FPS).toFixed(1)}s`
                        : (e.meta ? "" : "no mctx");
                    const ctl = document.createElement("div");
                    Object.assign(ctl.style,
                        { display: "flex", gap: "4px", marginTop: "auto" });
                    const mini = (label, title, fn) => {
                        const b = document.createElement("button");
                        b.textContent = label;
                        b.title = title;
                        Object.assign(b.style, {
                            background: "none", border: "none",
                            color: "#9aa1ac", cursor: "pointer",
                            padding: "0 2px", font: "11px sans-serif",
                        });
                        b.addEventListener("click", (ev) => {
                            ev.stopPropagation();
                            fn();
                        });
                        return b;
                    };
                    ctl.append(
                        mini("◀", "Move earlier", () => moveEntry(i, -1)),
                        mini("▶", "Move later", () => moveEntry(i, +1)),
                        mini("✕", "Remove from the sequence",
                             () => removeEntry(i)));
                    block.append(name, sub, ctl);
                    block.addEventListener("click", () => playAt(i));
                    strip.appendChild(block);
                });
                highlight(playIdx);
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
                for (const v of vids) {
                    v.pause();
                    v.removeAttribute("src");
                    v.load();
                }
                return onRemoved?.apply(this, arguments);
            };

            void (async () => {
                const clips = await tlFetchClipList();
                picker.replaceChildren(...clips.map((c) => {
                    const o = document.createElement("option");
                    o.value = o.textContent = c;
                    return o;
                }));
            })();
            void refresh();
            return result;
        }
    },
});
