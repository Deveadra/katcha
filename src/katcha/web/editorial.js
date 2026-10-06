/* Editorial projects share Production's channel, authentication and API transport. */
window.KatchaEditorial = (() => {
    const el = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
    const state = { channel: "", epoch: 0, project: null, revision: null, run: null, busy: false, timer: null, editorKey: "", sourceClipBindings: {}, clipResults: [], clipPickerEpoch: 0, clipSearchTimer: null, scriptSeedMeta: null, sourceMonitorKey: "", sourceMonitorUrl: null, sourceMonitorEpoch: 0, storyboardWorkspace: null, storyboardSaveTimer: null, storyboardSaving: false, storyboardDirty: false, storyboardRetryCount: 0 };
    let api, apiBlob;
    function previewReady() {
        return state.run?.stage === "render_ready_for_review" && state.run?.status === "completed";
    }
    function syncProgramMonitor() {
        const ready = previewReady();
        const video = el("editorial-program-monitor-video");
        const empty = el("editorial-program-monitor-empty");
        const loadButton = el("editorial-program-load");
        const meta = el("editorial-program-monitor-meta");
        const status = el("editorial-program-monitor-status");
        loadButton.hidden = !ready || Boolean(state.previewUrl);
        if (state.previewUrl) {
            video.src = state.previewUrl;
            video.hidden = false;
            empty.hidden = true;
            meta.textContent = `Loaded · revision ${state.run?.input_revision ?? state.project?.revision ?? "?"}`;
            status.textContent = "Current private preview is loaded. Use this monitor to compare the assembled cut with the selected source.";
            return;
        }
        video.pause();
        video.removeAttribute("src");
        video.load();
        video.hidden = true;
        empty.hidden = false;
        if (ready) {
            empty.textContent = "A completed private preview is ready. Load it here without leaving the storyboard.";
            meta.textContent = `Ready · revision ${state.run?.input_revision ?? state.project?.revision ?? "?"}`;
            status.textContent = "The program monitor follows the latest completed render for this project revision.";
        } else {
            empty.textContent = "Create a preview to inspect the assembled cut against the selected source.";
            meta.textContent = "No preview loaded";
            status.textContent = "The program monitor follows the latest completed render.";
        }
    }
    function clearPreview() {
        if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
        state.previewUrl = null;
        el("editorial-preview").pause();
        el("editorial-preview").removeAttribute("src");
        el("editorial-preview").load();
        el("editorial-preview").hidden = true;
        syncProgramMonitor();
    }
    async function loadCurrentPreview({advance = false} = {}) {
        if (!previewReady()) throw new Error("Create a completed private preview before loading the program monitor.");
        const epoch = state.epoch;
        const blob = await apiBlob(path(state.channel, `/${state.project.id}/runs/${state.run.editorial_run_id}/preview`));
        if (epoch !== state.epoch) return;
        clearPreview();
        state.previewUrl = URL.createObjectURL(blob);
        el("editorial-preview").src = state.previewUrl;
        el("editorial-preview").hidden = false;
        syncProgramMonitor();
        if (advance) advanceStage("preview");
        feedback("Preview loaded. Review the evidence, timing and media before publication.");
        renderActions();
    }
    function clearSourceMonitor(message = "Choose acquired footage to inspect sampled source frames.") {
        state.sourceMonitorEpoch += 1;
        state.sourceMonitorKey = "";
        if (state.sourceMonitorUrl) URL.revokeObjectURL(state.sourceMonitorUrl);
        state.sourceMonitorUrl = null;
        el("editorial-source-monitor").hidden = true;
        el("editorial-source-monitor-image").hidden = true;
        el("editorial-source-monitor-image").removeAttribute("src");
        el("editorial-source-monitor-title").textContent = "Selected footage";
        el("editorial-source-monitor-meta").textContent = "";
        el("editorial-source-monitor-times").innerHTML = "";
        el("editorial-source-monitor-empty").textContent = message;
        el("editorial-source-monitor-empty").hidden = false;
        el("editorial-source-monitor-status").textContent = "";
    }
    async function loadSourceMonitor() {
        const row = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")]
            .find(item => !item.hidden);
        const value = row?.querySelector("[data-primary-visual]")?.value || "";
        if (!value.startsWith("media:") || !state.project || !state.assetRun) {
            clearSourceMonitor();
            return;
        }
        const candidateId = value.slice("media:".length);
        const runId = state.assetRun.editorial_run_id;
        const key = [state.channel, state.project.id, state.project.revision, runId, candidateId].join(":");
        if (state.sourceMonitorKey === key && state.sourceMonitorUrl) {
            el("editorial-source-monitor").hidden = false;
            return;
        }
        const epoch = ++state.sourceMonitorEpoch;
        state.sourceMonitorKey = key;
        if (state.sourceMonitorUrl) URL.revokeObjectURL(state.sourceMonitorUrl);
        state.sourceMonitorUrl = null;
        el("editorial-source-monitor").hidden = false;
        el("editorial-source-monitor-image").hidden = true;
        el("editorial-source-monitor-empty").hidden = false;
        el("editorial-source-monitor-empty").textContent = "Loading sampled source frames…";
        el("editorial-source-monitor-times").innerHTML = "";
        el("editorial-source-monitor-status").textContent = "";
        const base = path(
            state.channel,
            `/${encodeURIComponent(state.project.id)}/runs/${encodeURIComponent(runId)}/assets/${encodeURIComponent(candidateId)}`,
        );
        try {
            const [metadata, imageBlob] = await Promise.all([
                api(`${base}/source-monitor`),
                apiBlob(`${base}/contact-sheet`),
            ]);
            if (epoch !== state.sourceMonitorEpoch || state.sourceMonitorKey !== key) return;
            const objectUrl = URL.createObjectURL(imageBlob);
            if (state.sourceMonitorUrl) URL.revokeObjectURL(state.sourceMonitorUrl);
            state.sourceMonitorUrl = objectUrl;
            el("editorial-source-monitor-title").textContent = metadata.title;
            el("editorial-source-monitor-meta").textContent =
                `${duration(metadata.duration_seconds)} · ${metadata.frame_count} sampled frame${metadata.frame_count === 1 ? "" : "s"}`;
            el("editorial-source-monitor-image").src = objectUrl;
            el("editorial-source-monitor-image").alt =
                `Sampled source frames for ${metadata.title}`;
            el("editorial-source-monitor-image").hidden = false;
            el("editorial-source-monitor-empty").hidden = true;
            el("editorial-source-monitor-times").innerHTML = metadata.sample_times
                .map((seconds, index) => `<span>F${index + 1} · ${timelineClock(seconds)}</span>`)
                .join("");
            el("editorial-source-monitor-status").textContent = metadata.limitation;
        } catch (error) {
            if (epoch !== state.sourceMonitorEpoch || state.sourceMonitorKey !== key) return;
            el("editorial-source-monitor-image").hidden = true;
            el("editorial-source-monitor-empty").hidden = false;
            el("editorial-source-monitor-empty").textContent = "Sampled frames unavailable.";
            el("editorial-source-monitor-status").textContent = error.message;
        }
    }
    const path = (channel, suffix = "") => `/v1/channels/${encodeURIComponent(channel)}/editorial-projects${suffix}`;
    const storageKey = (name) => `katcha.editorial.${state.channel}.${name}`;
    const read = (key, fallback) => { try { return JSON.parse(sessionStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
    function remember(key, value) { sessionStorage.setItem(key, JSON.stringify(value)); }
    function identity(name, payload) {
        const key = storageKey(`request.${name}`);
        const prior = read(key, null);
        if (prior?.payload === JSON.stringify(payload)) return prior.id;
        const id = crypto.randomUUID();
        remember(key, { id, payload: JSON.stringify(payload) });
        return id;
    }
    function feedback(text, error = false) {
        el("editorial-feedback").textContent = text;
        el("editorial-feedback").classList.toggle("error", error);
    }
    function safeLink(url) {
        try { const parsed = new URL(url); return ["https:", "http:"].includes(parsed.protocol) ? esc(parsed.href) : "#"; }
        catch { return "#"; }
    }
    function sourceUrls() {
        return el("editorial-urls").value.split(/\s+/).filter(Boolean);
    }
    function clipLabel(item) {
        return item.title || item.creator || `${item.platform || "Stored"} clip · ${String(item.clip_id || item.id).slice(0, 8)}`;
    }
    function duration(value) {
        const seconds = Number(value);
        if (!Number.isFinite(seconds)) return "duration unavailable";
        const minutes = Math.floor(seconds / 60);
        const remainder = Math.round(seconds % 60);
        return minutes ? `${minutes}m ${String(remainder).padStart(2, "0")}s` : `${Math.round(seconds)}s`;
    }
    function reconcileSourceClipBindings() {
        const urls = new Set(sourceUrls());
        state.sourceClipBindings = Object.fromEntries(
            Object.entries(state.sourceClipBindings || {}).filter(([url]) => urls.has(url)),
        );
    }
    function renderSelectedClips() {
        const entries = Object.entries(state.sourceClipBindings || {});
        el("editorial-selected-clips").innerHTML = entries.length ? entries.map(([url, item]) => {
            const lineage = item.platform === "upload"
                ? `<span class="editorial-upload-lineage">${esc(item.filename || "Local source upload")}</span>`
                : `<a href="${safeLink(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`;
            const action = item.platform === "upload"
                ? `<button type="button" class="mini" data-editorial-remove-source="${esc(url)}">Remove source</button>`
                : `<button type="button" class="mini" data-editorial-unpin-clip="${esc(url)}">Use URL instead</button>`;
            return `
            <article class="editorial-selected-clip">
                <div><strong>${esc(clipLabel(item))}</strong><small>${esc(item.creator || item.platform || "Managed Katcha media")} · ${esc(duration(item.duration_seconds))}</small>${lineage}</div>
                ${action}
            </article>`;
        }).join("") : '<p class="empty">No managed clips pinned. Source links will be acquired only when needed.</p>';
    }
    async function uploadSourceMedia() {
        const file = el("editorial-source-upload-file").files[0];
        if (!file) throw new Error("Choose a source video to upload.");
        if (file.size <= 0 || file.size > 512 * 1024 * 1024) {
            throw new Error("Choose a source video no larger than 512 MiB.");
        }
        if (!el("editorial-source-upload-confirm").checked) {
            throw new Error("Confirm you have permission to use this source video.");
        }
        const title = el("editorial-source-upload-title").value.trim();
        const requestId = identity("source-upload", {
            filename: file.name,
            size: file.size,
            lastModified: file.lastModified,
            title,
        });
        const params = new URLSearchParams({
            filename: file.name,
            idempotency_key: requestId,
            permitted_use: "true",
        });
        if (title) params.set("title", title);
        const button = el("editorial-source-upload-button");
        button.disabled = true;
        el("editorial-source-upload-status").textContent = `Uploading ${file.name}…`;
        try {
            const result = await api(path(state.channel, `/source-uploads?${params}`), {
                method: "POST",
                body: file,
                headers: {"Content-Type": file.type || "application/octet-stream"},
            });
            state.sourceClipBindings[result.source_url] = {
                clip_id: result.clip_id,
                title: result.title,
                creator: "Operator upload",
                platform: "upload",
                duration_seconds: result.duration_seconds,
                filename: result.filename,
            };
            const urls = sourceUrls();
            if (!urls.includes(result.source_url)) {
                el("editorial-urls").value = [...urls, result.source_url].join("\n");
            }
            saveBrief();
            renderClipResults();
            el("editorial-source-upload-status").textContent =
                `${result.filename} ready · ${duration(result.duration_seconds)} · ${result.width}×${result.height}${result.deduplicated ? " · reused existing media" : ""}`;
            feedback("Source upload is managed by Katcha and pinned to this project.");
        } finally {
            button.disabled = false;
        }
    }
    async function sha256Hex(bytes) {
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
    }
    function renderScriptSeedStatus() {
        const text = el("editorial-script-seed").value;
        const meta = state.scriptSeedMeta;
        if (!text.trim()) {
            el("editorial-script-seed-status").textContent = "No script seed. Katcha will write from the brief and verified research.";
            return;
        }
        const words = text.trim().split(/\s+/).length;
        el("editorial-script-seed-status").textContent = meta?.origin === "operator_file" && meta.loadedText === text
            ? `${meta.filename} imported · ${words.toLocaleString()} words · unverified writing intent`
            : `Pasted/edited draft · ${words.toLocaleString()} words · unverified writing intent`;
    }
    async function scriptSeedPayload() {
        const text = el("editorial-script-seed").value;
        if (!text.trim()) return null;
        const content_sha256 = await sha256Hex(new TextEncoder().encode(text));
        const meta = state.scriptSeedMeta;
        if (meta?.origin === "operator_file" && meta.loadedText === text) {
            return {
                text, origin: "operator_file", content_sha256,
                filename: meta.filename, media_type: meta.media_type,
                source_file_sha256: meta.source_file_sha256,
            };
        }
        return {text, origin: "operator_paste", content_sha256, media_type: "text/plain"};
    }
    function saveBrief() {
        if (!state.channel) return;
        reconcileSourceClipBindings();
        renderSelectedClips();
        remember(storageKey("brief"), {
            prompt: el("editorial-prompt").value, urls: el("editorial-urls").value,
            seconds: el("editorial-duration").value, sourceClipBindings: state.sourceClipBindings,
            scriptSeedText: el("editorial-script-seed").value, scriptSeedMeta: state.scriptSeedMeta,
        });
    }
    function restoreBrief() {
        const value = read(storageKey("brief"), {});
        el("editorial-prompt").value = value.prompt || "";
        el("editorial-urls").value = value.urls || "";
        el("editorial-duration").value = value.seconds || "420";
        state.sourceClipBindings = value.sourceClipBindings || {};
        state.scriptSeedMeta = value.scriptSeedMeta || null;
        el("editorial-script-seed").value = value.scriptSeedText || "";
        reconcileSourceClipBindings();
        renderSelectedClips();
        renderScriptSeedStatus();
    }
    function renderClipResults() {
        const selectedIds = new Set(Object.values(state.sourceClipBindings || {}).map(item => item.clip_id));
        const rows = (state.clipResults || []).filter(item => item.status !== "failed" && item.lifecycle_state !== "purged");
        el("editorial-clip-results").innerHTML = rows.length ? rows.map(item => `
            <article class="editorial-clip-result">
                <div><strong>${esc(clipLabel(item))}</strong><small>${esc(item.creator || item.platform || "Stored media")} · ${esc(duration(item.duration_seconds))} · ${esc(String(item.status || "ready").replaceAll("_", " "))}</small></div>
                <button type="button" class="mini" data-editorial-use-clip="${esc(item.id)}" ${selectedIds.has(item.id) ? "disabled" : ""}>${selectedIds.has(item.id) ? "Pinned" : "Use clip"}</button>
            </article>`).join("") : '<p class="empty">No reusable channel clips match this search.</p>';
    }
    async function loadClipPicker(query = "") {
        if (!state.channel) return;
        const requestEpoch = ++state.clipPickerEpoch;
        el("editorial-clip-results").innerHTML = '<p class="empty">Searching channel clips…</p>';
        const params = new URLSearchParams({channel_profile_id: state.channel, lifecycle_state: "hot", limit: "20"});
        if (query.trim()) params.set("q", query.trim());
        try {
            const result = await api(`/v1/clips/library?${params}`);
            if (requestEpoch !== state.clipPickerEpoch) return;
            state.clipResults = result.items || [];
            renderClipResults();
        } catch (error) {
            if (requestEpoch !== state.clipPickerEpoch) return;
            el("editorial-clip-results").innerHTML = `<p class="empty error">${esc(error.message)}</p>`;
        }
    }
    async function pinManagedClip(clipId) {
        const clip = state.clipResults.find(item => item.id === clipId);
        if (!clip) throw new Error("Refresh the clip picker and choose the clip again.");
        const sources = await api(`/v1/clips/${encodeURIComponent(clipId)}/sources`);
        const source = sources.find(item => {
            try {
                const parsed = new URL(item.canonical_url || item.source_url);
                return ["https:", "http:"].includes(parsed.protocol);
            } catch { return false; }
        });
        if (!source) throw new Error("This clip has no reusable source lineage. Open Clip Library to inspect it.");
        const sourceUrl = source.canonical_url || source.source_url;
        const existingForClip = Object.entries(state.sourceClipBindings).find(([, item]) => item.clip_id === clipId);
        if (existingForClip && existingForClip[0] !== sourceUrl) delete state.sourceClipBindings[existingForClip[0]];
        state.sourceClipBindings[sourceUrl] = {
            clip_id: clip.id, title: clip.title, creator: clip.creator, platform: clip.platform,
            duration_seconds: clip.duration_seconds,
        };
        const urls = sourceUrls();
        if (!urls.includes(sourceUrl)) {
            el("editorial-urls").value = [...urls, sourceUrl].join("\n");
        }
        saveBrief();
        renderClipResults();
        feedback(`${clipLabel(clip)} pinned. Katcha will reuse this managed media for the source.`);
    }
    function renderProjectSources() {
        if (!state.project) return;
        const bindings = state.project.brief?.source_clip_bindings || {};
        const sources = (state.project.brief?.source_urls || []).map(url => {
            const value = String(url);
            const managed = bindings[value];
            let host = value;
            try { host = new URL(value).hostname.replace(/^www\./, ""); } catch {}
            const uploaded = host === "upload.katcha.invalid";
            const label = uploaded ? "Uploaded source" : managed ? "Managed clip" : "Source URL";
            return `<span class="editorial-source-chip ${managed ? "managed" : ""}">${label} · ${esc(uploaded ? "local media" : host)}</span>`;
        });
        const seed = state.project.brief?.script_seed;
        if (seed) sources.push(`<span class="editorial-source-chip managed">Script seed · ${esc(seed.filename || "pasted draft")}</span>`);
        el("editorial-source-bindings").innerHTML = sources.join("");
    }
    async function guarded(action) {
        if (state.busy || !state.channel) return;
        state.busy = true;
        el("editorial-form-fields").disabled = true;
        el("editorial-detail").querySelectorAll("button:not([data-editorial-stage])").forEach((button) => { button.disabled = true; });
        const channel = state.channel;
        try { await action(); }
        catch (error) { if (channel === state.channel) feedback(error.message, true); }
        finally {
            state.busy = false;
            el("editorial-form-fields").disabled = !state.channel;
            if (state.project) renderActions();
        }
    }
    async function load(channel) {
        saveBrief();
        clearTimeout(state.timer);
        const epoch = ++state.epoch;
        const changed = state.channel !== channel;
        state.channel = channel;
        if (changed) {
            window.KatchaFrameInspector.reset();
            el("editorial-auto-regions").checked = false;
            el("editorial-narration-confirm").checked = false;
            window.KatchaEditorialHistory.reset();
            clearPreview(); clearSourceMonitor(); state.boardKey = ""; state.assetRun = null; state.imageFormKey = ""; el("editorial-image-file").value = ""; el("editorial-image-confirm").checked = false;
            clearTimeout(state.clipSearchTimer); state.clipPickerEpoch += 1; state.clipResults = []; state.sourceClipBindings = {}; state.scriptSeedMeta = null;
            state.project = null; state.revision = null; state.run = null; state.editorKey = ""; state.renderKey = "";
            el("editorial-detail").hidden = true;
        }
        restoreBrief();
        el("editorial-form-fields").disabled = !channel || state.busy;
        el("editorial-projects").innerHTML = `<div class="empty">${channel ? "Loading projects…" : "Select a channel to begin."}</div>`;
        if (!channel || document.querySelector('[data-production-group="editorial"]').hidden) return;
        try {
            const rows = await api(path(channel, "?limit=100"));
            if (epoch !== state.epoch) return;
            el("editorial-projects").innerHTML = rows.length ? rows.map((row) => `
                <article class="item"><div><h3>${esc(row.brief.prompt)}</h3><p>${row.brief.source_urls.length} source(s) · ${Object.keys(row.brief.source_clip_bindings || {}).length} managed${row.brief.script_seed ? " · script seed" : ""} · Revision ${row.revision}</p></div>
                <button type="button" class="mini" data-open-editorial="${esc(row.id)}">Open project</button></article>`).join("")
                : '<div class="empty">No editorial projects yet. Add a brief and choose a source link or managed clip.</div>';
            if (state.project) await open(state.project.id, { focus: false });
        } catch (error) {
            if (epoch !== state.epoch) return;
            el("editorial-projects").innerHTML = '<div class="empty">Projects are unavailable. Use Refresh projects to try again.</div>';
            feedback(error.message, true);
        }
    }
    async function open(id, { focus = true } = {}) {
        if (state.project?.id !== id) window.KatchaFrameInspector.reset();
        if (state.project?.id !== id) el("editorial-narration-confirm").checked = false;
        clearTimeout(state.timer);
        clearTimeout(state.storyboardSaveTimer);
        if (state.project?.id !== id) window.KatchaEditorialHistory.reset();
        const epoch = ++state.epoch;
        const channel = state.channel;
        feedback("Loading project evidence and progress…");
        const base = path(channel, `/${encodeURIComponent(id)}`);
        const [project, revisions, runs] = await Promise.all([
            api(base), api(`${base}/revisions?limit=1`), api(`${base}/runs?limit=100`),
        ]);
        const run = runs[0] ? await api(`${base}/runs/${encodeURIComponent(runs[0].editorial_run_id)}`) : null;
        const review = run?.target === "render" && run.status === "completed"
            ? await api(`${base}/runs/${encodeURIComponent(run.editorial_run_id)}/review`).catch(error => ({error: error.message})) : null;
        const [narration, images, storyboardWorkspace] = project.revision > 0
            ? await Promise.all([
                api(`${base}/narration?revision=${project.revision}`).catch(error => ({error: error.message, recordings: []})),
                api(`${base}/images?revision=${project.revision}`).catch(error => ({error: error.message, images: []})),
                api(`${base}/storyboard?script_revision=${project.revision}`).catch(error => ({error: error.message})),
            ])
            : [{voice_enabled: true, recordings: []}, {images: []}, null];
        const acquisition = runs.find(item => item.target === "acquire_assets" && item.status === "completed" && item.input_revision === project.revision);
        const assetRun = acquisition ? (acquisition.editorial_run_id === run?.editorial_run_id ? run : await api(`${base}/runs/${encodeURIComponent(acquisition.editorial_run_id)}`)) : null;
        const directed = runs.find(item => item.target === "direction" && item.status === "completed" && item.input_revision === project.revision);
        const directionRun = directed ? (directed.editorial_run_id === run?.editorial_run_id ? run : await api(`${base}/runs/${encodeURIComponent(directed.editorial_run_id)}`)) : null;
        if (epoch !== state.epoch) return;
        state.directionRun = run?.target === "direction" && run.status !== "completed" ? null : directionRun;
        if (state.project?.id !== id || state.run?.editorial_run_id !== run?.editorial_run_id) clearPreview();
        window.KatchaEditorialHistory.context(channel, id);
        state.assetRun = assetRun; state.review = review; state.narration = narration; state.images = images;
        const pending = read(storageKey(`pending.${id}`), null);
        state.project = project; state.revision = revisions[0] || null; state.run = run;
        state.stale = Boolean(pending && pending.revision < project.revision);
        if (state.stale) state.revision = pending;
        state.storyboardWorkspace = !state.stale && storyboardWorkspace?.workspace
            ? storyboardWorkspace
            : null;
        state.storyboardDirty = false;
        state.storyboardSaving = false;
        if (state.storyboardWorkspace) hydrateStoryboardLocalState(state.storyboardWorkspace);
        el("editorial-detail").hidden = false;
        el("editorial-title").textContent = project.brief.prompt;
        el("editorial-status").textContent = run
            ? `${String(run.status).replaceAll("_", " ")} · ${String(run.stage).replaceAll("_", " ")}`
            : "Brief saved. Choose source analysis or research and scripting.";
        el("editorial-discard").hidden = !state.stale;
        feedback(run?.error || "Project loaded. Generated claims and scripts need editorial review.", Boolean(run?.error));
        renderProjectSources();
        renderActions(); renderEvidence(); renderScript(); renderImages(); renderNarration(); renderStoryboard(); renderDirection(); renderReview(); renderActions();
        if (storyboardWorkspace?.error && !state.stale) {
            storyboardSyncStatus(
                `Workspace unavailable · ${storyboardWorkspace.error}`,
                {error: true},
            );
        } else {
            updateStoryboardUndo();
        }
        setAiLinks(); restoreStage();
        if (state.stale) feedback("A newer script revision is available. Your unsaved text is retained below. Copy it before discarding edits to load the latest version.", true);
        if (focus) el("editorial-title").focus();
        if (run && ["queued", "running"].includes(run.status)) {
            state.timer = setTimeout(() => {
                if (epoch === state.epoch && !state.busy) void open(id, { focus: false }).catch((error) => feedback(`Progress unavailable: ${error.message}. Refresh projects to reconnect.`, true));
            }, 10000);
        }
    }
    function scoutId() { return state.run?.artifacts?.scout_run_id || (state.run?.target === "assets" ? state.run.editorial_run_id : null); }
    function canDownload(candidate) {
        try { const url = new URL(candidate.url); return candidate.medium === "video" && url.protocol === "https:" && ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"].includes(url.hostname); } catch { return false; }
    }
    function selectedAssets() { return [...el("editorial-assets").querySelectorAll("input:checked")].map((input) => input.value).sort(); }
    function assetControls() {
        const active = state.run && ["queued", "running"].includes(state.run.status);
        el("editorial-acquire-assets").disabled = state.busy || Boolean(active) || state.stale || !scoutId() || !selectedAssets().length;
    }
    function renderActions() {
        const active = state.run && ["queued", "running"].includes(state.run.status);
        const canResume = state.run && ["failed", "blocked"].includes(state.run.status);
        el("editorial-actions").innerHTML = active
            ? '<button class="mini" type="button" data-editorial-action="cancel">Stop work</button>'
            : '<button class="mini" type="button" data-editorial-action="analysis">Analyze sources</button><button class="button primary" type="button" data-editorial-action="script">Research and draft script</button>'
                + (canResume ? '<button class="mini" type="button" data-editorial-action="resume">Resume saved work</button>' : "");
        if (!active && state.revision?.draft.script?.length) el("editorial-actions").insertAdjacentHTML("beforeend", '<button class="mini" type="button" data-editorial-action="assets">Find supporting assets</button>');
        el("editorial-detail").querySelectorAll("button:not([data-editorial-stage])").forEach((button) => { button.disabled = state.busy; });
        el("editorial-save-script").disabled = state.busy || !state.revision || Boolean(active) || state.stale;
        assetControls();
        el("editorial-render").disabled = state.busy || Boolean(active) || state.stale || !state.revision?.draft.script?.length || Boolean(read(storageKey(`pending.${state.project.id}`), null));
        el("editorial-render").disabled ||= el("editorial-presentation").value === "narrated" && !state.narration?.voice_enabled;
        el("editorial-generate-narration").disabled = state.busy || Boolean(active) || state.stale || !state.narration?.voice_enabled || !state.revision?.draft.script?.length || Boolean(read(storageKey(`pending.${state.project.id}`), null));
        el("editorial-direct").disabled = el("editorial-render").disabled || !state.assetRun;
        el("editorial-auto-regions").disabled = el("editorial-direct").disabled;
        el("editorial-image-upload").disabled = state.busy || Boolean(active) || state.stale || !state.revision?.draft.script?.length || Boolean(read(storageKey(`pending.${state.project.id}`), null));
        el("editorial-render-directed").disabled = state.busy || Boolean(active) || state.stale || !state.directionRun || Boolean(read(storageKey(`pending.${state.project.id}`), null));
        el("editorial-render").textContent = el("editorial-presentation").value === "narrated" ? "Create narrated preview" : "Create silent captioned preview";
        el("editorial-play").hidden = !previewReady();
        syncProgramMonitor();
        el("editorial-approve").disabled = state.busy || !state.review?.can_approve || !state.previewUrl || state.stale || Boolean(read(storageKey(`pending.${state.project.id}`), null));
        el("editorial-request-changes").disabled = state.busy || !state.review || Boolean(state.review.error);
        updateStoryboardUndo();
    }
    function reviewNoteKey() { return storageKey(`review-note.${state.run?.editorial_run_id}`); }
    function renderReview() {
        el("editorial-review").hidden = !state.review;
        if (!state.review) return;
        const labels = {unreviewed: "Awaiting your review. Load the private preview before approving.", approve: "Review approved for this rendered revision.", request_changes: "Changes requested. Update the script or storyboard and create a new preview.", invalidated: "Previous approval is no longer valid."};
        el("editorial-review-status").textContent = state.review.error
            ? `Review status unavailable: ${state.review.error}. Refresh projects to retry.`
            : `${labels[state.review.status] || "Review required."} ${state.review.blocker || ""}`;
        el("editorial-review-note").value = read(reviewNoteKey(), "");
        el("editorial-review-history").innerHTML = (state.review.reviews || []).map(review => `<article class="item"><div><p>${esc(review.decision === "approve" ? "Approved" : "Changes requested")} · ${esc(review.actor)} · ${esc(review.created_at)}</p><p>${esc(review.note)}</p></div></article>`).join("") || "No saved reviews.";
    }
    function narrationKey() { return storageKey(`narration.${state.project.id}.${state.project.revision}`); }
    function saveNarrationChoices() {
        const choices = Object.fromEntries([...el("editorial-narration").querySelectorAll("[data-narration-select]")].map(input => [input.dataset.narrationSelect, input.value]));
        remember(narrationKey(), {mode: el("editorial-presentation").value, choices});
    }
    function renderNarration() {
        const data = state.narration || {};
        const saved = read(narrationKey(), {mode: "captioned_silent", choices: {}});
        el("editorial-presentation").value = saved.mode;
        el("editorial-presentation").querySelector('[value="narrated"]').disabled = !data.voice_enabled;
        el("editorial-narration-panel").hidden = saved.mode !== "narrated" || !data.voice_enabled;
        el("editorial-narration-status").textContent = data.error ? `Recordings unavailable: ${data.error}. Refresh projects to retry.` : !data.voice_enabled ? "Voice is off. Enable it in Channel Studio to use narration, or choose Silent with captions." : "";
        const holds = ["blocked", "failed", "cancelled"].includes(state.run?.status) ? state.run?.artifacts?.narration_billing || [] : [];
        const billingKey = state.run?.editorial_run_id + JSON.stringify(holds);
        el("editorial-narration-billing").hidden = !holds.length;
        if (billingKey !== state.billingKey) {
            state.billingKey = billingKey;
            el("editorial-narration-billing").innerHTML = holds.map(hold => `<fieldset class="editorial-beat" data-billing-beat="${esc(hold.beat_id)}" data-billing-attempt="${esc(hold.dispatch_count)}"><legend>Resolve speech charge · ${esc(hold.beat_id)}</legend><p>Estimated $${esc(hold.estimated_cost_usd)} remains held. Check the provider's final request history before recording an outcome. Confirming no charge allows another attempt; recording a charge does not generate replacement audio.</p><label>Verified outcome<select data-billing-outcome><option value="charged">Provider charged for the request</option><option value="not_charged">Provider confirms no charge</option></select></label><label>Final cost (USD)<input data-billing-cost type="number" min="0" max="1000" step="0.000001" value="0"></label><label>Provider receipt or final-status reference<textarea data-billing-receipt maxlength="2000" rows="2"></textarea></label><label class="check-row"><input data-billing-confirm type="checkbox"> I verified the final provider outcome</label><button data-billing-save type="button" class="mini">Save verified billing outcome</button></fieldset>`).join("");
        }
        const key = narrationKey() + JSON.stringify(data.recordings);
        if (key === state.narrationKey) return;
        state.narrationKey = key;
        el("editorial-narration").innerHTML = (state.revision?.draft.script || []).map((beat, index) => {
            const recordings = (data.recordings || []).filter(item => item.beat_id === beat.id && item.status === "active");
            const savedRecording = saved.choices[beat.id] || "";
            const unavailableRecording = savedRecording
                && !recordings.some(item => item.id === savedRecording);
            return `<fieldset class="editorial-beat" data-narration-beat="${esc(beat.id)}"><legend>Beat ${index + 1}</legend><p>${esc(beat.narration)}</p><label>Recording for beat ${index + 1}<select data-narration-select="${esc(beat.id)}"><option value="">Choose a recording</option>${unavailableRecording ? `<option value="${esc(savedRecording)}" selected>Unavailable recording · choose a replacement</option>` : ""}${recordings.map(item => `<option value="${esc(item.id)}" ${savedRecording === item.id ? "selected" : ""}>${Number(item.duration_seconds).toFixed(2)}s · ${esc(item.created_at)}</option>`).join("")}</select></label><button type="button" class="mini" data-narration-revoke>Remove selected recording</button><label>WAV recording for beat ${index + 1}<input type="file" accept=".wav,audio/wav" data-narration-file></label><label class="check-row"><input type="checkbox" data-narration-permitted> I have permission to use this recording</label><button type="button" class="mini" data-narration-upload>Upload recording</button></fieldset>`;
        }).join("");
    }
    function boardStorage() { return storageKey(`board.${state.project.id}.${state.project.revision}.${state.assetRun?.editorial_run_id || "none"}`); }
    function saveStoryboard() {
        const rows = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")].map(row => ({
            choice: row.querySelector("select").value, start: row.querySelector("input[type=number]").value,
            freeze: row.querySelector("input[type=checkbox]").checked,
            compare: row.querySelector("[data-image-compare]").value,
            annotation: Object.fromEntries([...row.querySelectorAll("[data-region]")].map(input => [input.dataset.region, input.value])),
        }));
        remember(boardStorage(), rows);
    }
    function workspaceSavedRows(workspace) {
        return (workspace?.beats || []).map(beat => {
            let choice = "";
            let compare = "";
            if (beat.layout === "single" && beat.media?.[0]) {
                choice = `media:${beat.media[0].candidate_id}`;
            } else if (beat.layout === "quote" && beat.quote_source_id) {
                choice = `quote:${beat.quote_source_id}`;
            } else if (beat.layout === "image" && beat.image_id) {
                choice = `image:${beat.image_id}`;
            } else if (beat.layout === "image_comparison" && beat.image_ids?.length === 2) {
                choice = `image:${beat.image_ids[0]}`;
                compare = beat.image_ids[1];
            }
            const overlay = beat.overlays?.[0];
            return {
                choice,
                start: String(beat.media?.[0]?.start_seconds ?? 0),
                freeze: Boolean(beat.media?.[0]?.freeze),
                compare,
                annotation: overlay ? {
                    kind: overlay.kind,
                    target: String(overlay.media_index ?? 0),
                    x: String(Number(overlay.region.x) * 100),
                    y: String(Number(overlay.region.y) * 100),
                    width: String(Number(overlay.region.width) * 100),
                    height: String(Number(overlay.region.height) * 100),
                    label: overlay.label || "",
                } : {},
            };
        });
    }
    function hydrateStoryboardLocalState(row) {
        if (!row?.workspace || !state.project) return;
        remember(boardStorage(), workspaceSavedRows(row.workspace));
        remember(narrationKey(), {
            mode: row.workspace.presentation_mode,
            choices: Object.fromEntries(
                (state.revision?.draft.script || []).map(beat => [
                    beat.id,
                    row.workspace.narration_ids?.[beat.id] || "",
                ]),
            ),
        });
    }
    function storyboardWorkspaceDraft() {
        const rows = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")];
        if (!rows.length) return null;
        const beats = rows.map(row => {
            const value = row.querySelector("[data-primary-visual]").value;
            if (!value) return {beat_id: row.dataset.boardBeat, layout: "unassigned"};
            const [kind, ...parts] = value.split(":");
            const id = parts.join(":");
            if (kind === "quote") {
                return {
                    beat_id: row.dataset.boardBeat,
                    layout: "quote",
                    quote_source_id: id,
                };
            }
            if (kind === "media") {
                const start = Number(row.querySelector("input[type=number]").value);
                if (!Number.isFinite(start) || start < 0) {
                    throw new Error("Enter a valid footage start time.");
                }
                return {
                    beat_id: row.dataset.boardBeat,
                    layout: "single",
                    media: [{
                        candidate_id: id,
                        start_seconds: start,
                        freeze: row.querySelector("input[type=checkbox]").checked,
                    }],
                };
            }
            if (kind !== "image") throw new Error("Choose a supported visual.");
            const compare = row.querySelector("[data-image-compare]").value;
            const annotation = Object.fromEntries(
                [...row.querySelectorAll("[data-region]")].map(input => [
                    input.dataset.region,
                    input.value,
                ]),
            );
            const overlays = [];
            if (annotation.kind) {
                const region = Object.fromEntries(
                    ["x", "y", "width", "height"].map(key => [
                        key,
                        Number(annotation[key]) / 100,
                    ]),
                );
                if (
                    Object.values(region).some(value => !Number.isFinite(value))
                    || region.x < 0
                    || region.y < 0
                    || region.width <= 0
                    || region.height <= 0
                    || region.x + region.width > 1
                    || region.y + region.height > 1
                ) {
                    throw new Error("Keep the annotation within the original image.");
                }
                if (annotation.target === "1" && !compare) {
                    throw new Error("Choose a second image before marking it.");
                }
                overlays.push({
                    kind: annotation.kind,
                    media_index: Number(annotation.target),
                    region,
                    label: annotation.label.trim() || null,
                });
            }
            return {
                beat_id: row.dataset.boardBeat,
                layout: compare ? "image_comparison" : "image",
                ...(compare ? {image_ids: [id, compare]} : {image_id: id}),
                overlays,
            };
        });
        const mode = el("editorial-presentation").value;
        const narration_ids = mode === "narrated"
            ? Object.fromEntries(
                [...el("editorial-narration").querySelectorAll("[data-narration-select]")]
                    .filter(input => input.value)
                    .map(input => [input.dataset.narrationSelect, input.value]),
            )
            : {};
        return {
            presentation_mode: mode,
            asset_run_id: beats.some(beat => beat.media?.length)
                ? state.assetRun?.editorial_run_id || null
                : null,
            beats,
            narration_ids,
        };
    }
    function storyboardSyncStatus(message, {error = false, saving = false} = {}) {
        const status = el("editorial-storyboard-sync-status");
        const host = status.closest(".editorial-storyboard-sync");
        status.textContent = message;
        host.classList.toggle("is-error", error);
        host.classList.toggle("is-saving", saving);
    }
    function updateStoryboardUndo() {
        const row = state.storyboardWorkspace;
        el("editorial-storyboard-undo").disabled = (
            state.busy
            || state.stale
            || state.storyboardSaving
            || state.storyboardDirty
            || !row
            || (
                !row.parent_version
                && !(row.version === 1 && row.origin !== "undo")
            )
        );
        if (state.storyboardSaving || state.storyboardDirty) return;
        if (row?.version) {
            storyboardSyncStatus(
                `Saved workspace · v${row.version} · ${String(row.origin || "operator").replaceAll("_", " ")}`,
            );
        } else if (!state.stale) {
            storyboardSyncStatus("Storyboard workspace is local until the first edit.");
        }
    }
    async function flushStoryboardWorkspace() {
        clearTimeout(state.storyboardSaveTimer);
        state.storyboardSaveTimer = null;
        if (
            state.storyboardSaving
            || state.stale
            || !state.project
            || !state.revision
            || read(storageKey(`pending.${state.project.id}`), null)
        ) {
            if (state.storyboardSaving) state.storyboardDirty = true;
            return;
        }
        let workspace;
        try {
            workspace = storyboardWorkspaceDraft();
        } catch (error) {
            storyboardSyncStatus(`Complete this edit to save · ${error.message}`);
            return;
        }
        if (!workspace) return;
        const epoch = state.epoch;
        const channel = state.channel;
        const project = state.project.id;
        const scriptRevision = state.project.revision;
        const expectedVersion = state.storyboardWorkspace?.version || 0;
        const body = {
            script_revision: scriptRevision,
            expected_version: expectedVersion,
            workspace,
        };
        body.idempotency_key = identity(`storyboard.${project}`, body);
        state.storyboardSaving = true;
        state.storyboardDirty = false;
        let failed = false;
        storyboardSyncStatus("Saving Storyboard workspace…", {saving: true});
        updateStoryboardUndo();
        try {
            const saved = await api(path(channel, `/${project}/storyboard`), {
                method: "POST",
                body: JSON.stringify(body),
            });
            if (
                epoch === state.epoch
                && channel === state.channel
                && project === state.project?.id
                && scriptRevision === state.project?.revision
            ) {
                state.storyboardWorkspace = saved;
                state.storyboardRetryCount = 0;
                storyboardSyncStatus(`Saved workspace · v${saved.version}`);
            }
        } catch (error) {
            failed = true;
            if (epoch === state.epoch && project === state.project?.id) {
                state.storyboardDirty = true;
                storyboardSyncStatus(
                    `Workspace not saved · ${error.message}`,
                    {error: true},
                );
            }
        } finally {
            if (epoch === state.epoch && project === state.project?.id) {
                state.storyboardSaving = false;
                updateStoryboardUndo();
                if (state.storyboardDirty) {
                    if (!failed) {
                        state.storyboardSaveTimer = setTimeout(
                            () => void flushStoryboardWorkspace(),
                            500,
                        );
                    } else if (state.storyboardRetryCount < 1) {
                        state.storyboardRetryCount += 1;
                        state.storyboardSaveTimer = setTimeout(
                            () => void flushStoryboardWorkspace(),
                            1500,
                        );
                    }
                }
            }
        }
    }
    function scheduleStoryboardWorkspaceSave() {
        if (!state.project || !state.revision) return;
        if (state.stale || read(storageKey(`pending.${state.project.id}`), null)) {
            storyboardSyncStatus(
                "Save or discard script edits before saving Storyboard changes.",
            );
            return;
        }
        state.storyboardDirty = true;
        state.storyboardRetryCount = 0;
        clearTimeout(state.storyboardSaveTimer);
        storyboardSyncStatus("Unsaved Storyboard changes");
        updateStoryboardUndo();
        state.storyboardSaveTimer = setTimeout(
            () => void flushStoryboardWorkspace(),
            700,
        );
    }
    async function undoStoryboardWorkspace() {
        const current = state.storyboardWorkspace;
        const canUndo = current && (
            current.parent_version
            || (current.version === 1 && current.origin !== "undo")
        );
        if (!canUndo || state.storyboardDirty) {
            throw new Error("Save the current Storyboard before undoing.");
        }
        const epoch = state.epoch;
        const channel = state.channel;
        const project = state.project.id;
        const body = {
            script_revision: state.project.revision,
            expected_version: current.version,
        };
        body.idempotency_key = identity(`storyboard.undo.${project}`, body);
        const restored = await api(path(channel, `/${project}/storyboard/undo`), {
            method: "POST",
            body: JSON.stringify(body),
        });
        if (
            epoch !== state.epoch
            || channel !== state.channel
            || project !== state.project?.id
        ) return;
        state.storyboardWorkspace = restored;
        state.storyboardDirty = false;
        state.boardKey = "";
        state.narrationKey = "";
        hydrateStoryboardLocalState(restored);
        renderNarration();
        renderStoryboard();
        updateStoryboardUndo();
        feedback(
            current.parent_version
                ? `Storyboard restored from v${current.parent_version}.`
                : "Storyboard restored to the blank script baseline.",
        );
    }
    function imageFormKey() { return storageKey(`image-form.${state.project.id}.${state.project.revision}`); }
    const imageFields = ["title", "source", "permission", "beat"];
    function saveImageForm() {
        const value = Object.fromEntries(imageFields.map(name => [name, el(`editorial-image-${name}`).value]));
        value.illustration = el("editorial-image-illustration").checked;
        remember(imageFormKey(), value);
    }
    function renderImages() {
        const key = imageFormKey();
        if (key !== state.imageFormKey) {
            state.imageFormKey = key;
            const saved = read(key, {});
            el("editorial-image-beat").innerHTML = (state.revision?.draft.script || []).map((beat, index) => `<option value="${esc(beat.id)}">Beat ${index + 1} · ${esc(beat.role)}</option>`).join("");
            imageFields.forEach(name => { if (saved[name] != null || name !== "beat") el(`editorial-image-${name}`).value = saved[name] || ""; });
            el("editorial-image-file").value = "";
            el("editorial-image-illustration").checked = Boolean(saved.illustration);
            el("editorial-image-confirm").checked = false;
        }
        el("editorial-images").innerHTML = state.images?.error
            ? `<p class="error">Images unavailable: ${esc(state.images.error)}. Refresh projects to retry.</p>`
            : (state.images?.images || []).filter(item => item.status === "active").map(item => `<article class="item"><div><h4>${esc(item.title)}</h4><p>${item.width} × ${item.height} · ${item.illustration ? "Illustration" : "Still image"}</p><p>Source: ${esc(item.source_reference)}</p><p>Use: ${esc(item.use_note)}</p><button class="mini" type="button" data-image-revoke="${esc(item.id)}">Remove image</button></div></article>`).join("") || '<p class="empty">No images for this revision. Upload an image, then select it as a beat’s visual below.</p>';
    }
    function timelineStorage() {
        return storageKey(`timeline.${state.project.id}.${state.project.revision}`);
    }
    function timelineClock(seconds) {
        const value = Math.max(0, Number(seconds) || 0);
        const minutes = Math.floor(value / 60);
        const remainder = Math.round(value % 60);
        return `${minutes}:${String(remainder).padStart(2, "0")}`;
    }
    function timelineVisualStatus(choice, unavailable = false) {
        if (unavailable) return "Needs replacement";
        if (!choice) return "Needs visual";
        if (choice.startsWith("media:")) return "Footage";
        if (choice.startsWith("image:")) return "Image";
        if (choice.startsWith("quote:")) return "Evidence";
        return "Visual set";
    }
    function refreshTimelineStatus() {
        const rows = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")];
        const buttons = [...el("editorial-beat-timeline").querySelectorAll("[data-timeline-beat]")];
        let assigned = 0;
        rows.forEach((row, index) => {
            const select = row.querySelector("[data-primary-visual]");
            const choice = select?.value || "";
            const unavailable = select?.selectedOptions?.[0]?.dataset.unavailable === "true";
            if (choice && !unavailable) assigned += 1;
            const status = buttons[index]?.querySelector("[data-timeline-status]");
            if (status) status.textContent = timelineVisualStatus(choice, unavailable);
            buttons[index]?.classList.toggle(
                "is-ready",
                Boolean(choice) && !unavailable,
            );
        });
        const total = (state.revision?.draft.script || []).reduce(
            (sum, beat) => sum + Number(beat.planned_duration_seconds || 0),
            0,
        );
        el("editorial-timeline-summary").textContent = rows.length
            ? `${rows.length} beat${rows.length === 1 ? "" : "s"} · ${timelineClock(total)} planned · ${assigned}/${rows.length} visuals assigned`
            : "";
    }
    function selectedTimelineBeat() {
        const row = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")]
            .find(item => !item.hidden);
        if (!row) return null;
        return (state.revision?.draft.script || []).find(beat => beat.id === row.dataset.boardBeat) || null;
    }
    function refreshContextInspector(beatId) {
        const script = state.revision?.draft.script || [];
        const beat = script.find(item => item.id === beatId);
        if (!beat) {
            el("editorial-context-title").textContent = "Selected beat";
            el("editorial-context-summary").textContent = "Choose a saved script beat to inspect its editorial context.";
            return;
        }
        const index = script.findIndex(item => item.id === beatId);
        const claims = (state.revision?.draft.claims || []).filter(claim => beat.claim_ids.includes(claim.id));
        const supported = claims.filter(claim => claim.verification === "supported").length;
        el("editorial-context-title").textContent = `Beat ${index + 1} · ${beat.role}`;
        el("editorial-context-summary").textContent =
            `${timelineClock(beat.planned_duration_seconds)} planned · ${claims.length} claim${claims.length === 1 ? "" : "s"} · ${supported}/${claims.length} supported`
            + (beat.uncertainty_disclosure ? ` · disclosure: ${beat.uncertainty_disclosure}` : "");
    }
    function selectTimelineBeat(beatId, {focus = false, persist = true} = {}) {
        const rows = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")];
        if (!rows.length) return;
        const available = new Set(rows.map(row => row.dataset.boardBeat));
        const selected = available.has(beatId) ? beatId : rows[0].dataset.boardBeat;
        rows.forEach(row => {
            row.hidden = row.dataset.boardBeat !== selected;
        });
        el("editorial-beat-timeline").querySelectorAll("[data-timeline-beat]").forEach(button => {
            const active = button.dataset.timelineBeat === selected;
            button.setAttribute("aria-selected", active ? "true" : "false");
            button.tabIndex = active ? 0 : -1;
            if (active && focus) {
                button.focus();
                button.scrollIntoView({block: "nearest", inline: "nearest"});
            }
        });
        if (persist) remember(timelineStorage(), selected);
        refreshContextInspector(selected);
        setAiLinks();
        if (!document.querySelector('[data-editorial-stage-panel="storyboard"]').hidden) {
            void loadSourceMonitor();
        }
    }
    function showFootageControls() {
        el("editorial-storyboard").querySelectorAll("[data-board-beat]").forEach(row => {
            const footage = row.querySelector("select").value.startsWith("media:");
            row.querySelectorAll("input:not([data-region])").forEach(input => { input.closest("label").hidden = !footage; });
            const image = row.querySelector("select").value.startsWith("image:");
            row.querySelector("[data-image-tools]").hidden = !image;
            row.querySelector("[data-region-fields]").hidden = !row.querySelector('[data-region="kind"]').value;
        });
    }
    function renderStoryboard() {
        const key = boardStorage()
            + ":" + String(state.storyboardWorkspace?.version || 0)
            + JSON.stringify(state.images);
        if (key === state.boardKey) {
            refreshTimelineStatus();
            return;
        }
        state.boardKey = key;
        const choices = state.assetRun?.artifacts?.asset_selection || [];
        const receipts = state.assetRun?.artifacts?.acquired_assets || {};
        const draft = state.revision?.draft || {};
        const script = draft.script || [];
        const saved = read(boardStorage(), []);
        let elapsed = 0;
        el("editorial-beat-timeline").innerHTML = script.map((beat, index) => {
            const start = elapsed;
            const duration = Number(beat.planned_duration_seconds || 0);
            elapsed += duration;
            const choice = saved[index]?.choice || "";
            return `<button type="button" role="tab" class="editorial-timeline-beat ${choice ? "is-ready" : ""}" data-timeline-beat="${esc(beat.id)}" aria-selected="false" aria-controls="board-panel-${esc(beat.id)}" style="--beat-grow:${Math.max(1, Math.min(duration || 1, 60))}">
                <span class="editorial-timeline-index">${String(index + 1).padStart(2, "0")}</span>
                <strong>${esc(beat.role)}</strong>
                <small>${timelineClock(start)}–${timelineClock(elapsed)} · <span data-timeline-status>${timelineVisualStatus(choice)}</span></small>
            </button>`;
        }).join("");
        el("editorial-storyboard").innerHTML = script.map((beat, index) => {
            const claims = (draft.claims || []).filter(claim => beat.claim_ids.includes(claim.id));
            const sourceIds = new Set(claims.flatMap(claim => claim.source_ids));
            const sources = (draft.sources || []).filter(source => sourceIds.has(source.id));
            const savedChoice = saved[index]?.choice || "";
            const options = [...(state.images?.images || []).filter(item => item.status === "active" && item.beat_id === beat.id).map(item => ({value: `image:${item.id}`, label: `${item.illustration ? "Illustration" : "Image"}: ${item.title}`})), ...choices.filter(item => item.beat_id === beat.id && receipts[item.id]).map(item => ({value: `media:${item.id}`, label: item.title})), ...sources.map(item => ({value: `quote:${item.id}`, label: `Evidence quote: ${item.title}`}))];
            if (savedChoice && !options.some(item => item.value === savedChoice)) {
                options.unshift({
                    value: savedChoice,
                    label: "Unavailable visual · choose a replacement",
                    unavailable: true,
                });
            }
            return `<fieldset id="board-panel-${esc(beat.id)}" class="editorial-beat editorial-beat-editor" role="tabpanel" data-board-beat="${esc(beat.id)}"><legend><span>Beat ${index + 1} · ${esc(beat.role)}</span><small>${timelineClock(beat.planned_duration_seconds)} planned</small></legend><p class="editorial-beat-intent">${esc(beat.visual_intent)}</p><label>Visual<select data-primary-visual>${'<option value="">Choose a visual</option>'}${options.map(item => `<option value="${esc(item.value)}" ${item.unavailable ? 'data-unavailable="true"' : ""} ${savedChoice === item.value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label><label>Footage start (seconds)<input type="number" min="0" step="0.1" value="${esc(saved[index]?.start || "0")}"></label><label class="check-row"><input type="checkbox" ${saved[index]?.freeze ? "checked" : ""}> Hold this frame</label>
                <div data-image-tools hidden><label>Compare with another image<select data-image-compare><option value="">Single image</option>${saved[index]?.compare && !(state.images?.images || []).some(item => item.id === saved[index].compare && item.status === "active" && item.beat_id === beat.id) ? `<option value="${esc(saved[index].compare)}" selected>Unavailable image · choose a replacement</option>` : ""}${(state.images?.images || []).filter(item => item.status === "active" && item.beat_id === beat.id).map(item => `<option value="${esc(item.id)}" ${saved[index]?.compare === item.id ? "selected" : ""}>${esc(item.title)}</option>`).join("")}</select></label>
                <details class="ae-help"><summary>Mark a source region</summary><p>Manual placement on the original image. Percentages follow the image through resizing and push-in. Check the preview before approval.</p>
                <label>Annotation<select data-region="kind">${[["", "None"], ["circle", "Circle"], ["arrow", "Arrow"], ["highlight", "Highlight"]].map(([value, label]) => `<option value="${value}" ${saved[index]?.annotation?.kind === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>
                <div data-region-fields><label>Image to mark<select data-region="target"><option value="0">First image</option><option value="1" ${saved[index]?.annotation?.target === "1" ? "selected" : ""}>Second image</option></select></label>
                ${[["x", "Left", 25], ["y", "Top", 25], ["width", "Width", 50], ["height", "Height", 50]].map(([key, label, fallback]) => `<label>${label} (%)<input data-region="${key}" type="number" min="${key === "width" || key === "height" ? 0.1 : 0}" max="100" step="0.1" value="${esc(saved[index]?.annotation?.[key] ?? fallback)}"></label>`).join("")}
                <label>Optional label<input data-region="label" maxlength="100" value="${esc(saved[index]?.annotation?.label || "")}"></label></div></details></div></fieldset>`;
        }).join("") || '<p class="empty">Save a script and choose supporting media to prepare a preview.</p>';
        showFootageControls();
        refreshTimelineStatus();
        selectTimelineBeat(read(timelineStorage(), script[0]?.id || ""), {persist: false});
    }
    function frameCitations(artifacts, beatId, candidateId, context = {run: state.directionRun?.editorial_run_id, channel: state.channel, project: state.project?.id}) {
        const evidence = artifacts.direction_shot_evidence;
        if (!evidence) return '<p class="muted">No saved frame citation for this older plan. Check the source footage before using it.</p>';
        const shots = (evidence.shots || []).filter(shot => shot.beat_id === beatId && shot.candidate_id === candidateId);
        if (!shots.length) return '<p class="error-text">Frame citation unavailable. Plan visuals again before relying on this shot.</p>';
        return shots.map(shot => {
            const observation = shot.observation;
            return `<details class="ae-help"><summary>Observed frame · ${esc(Number(observation.start_seconds).toFixed(3))}s</summary><p>${esc(observation.observation)}</p><a href="${safeLink(observation.source_url)}" target="_blank" rel="noopener noreferrer">Open source video</a><p>Sampled frame only. This does not establish continuous visibility or verify an identity.</p>${(shot.limitations || []).map(value => `<p>${esc(value)}</p>`).join("")}${context.run ? `<button type="button" class="mini" data-frame-run="${esc(context.run)}" data-frame-channel="${esc(context.channel)}" data-frame-project="${esc(context.project)}" data-frame-index="${evidence.shots.indexOf(shot)}">Inspect cited frame</button>` : ""}${shot.regions ? `<p>AI region suggestions · verify placement and meaning in the preview.</p>${shot.regions.regions.length ? shot.regions.regions.map(region => `<p>${esc(region.kind)} · ${esc(region.description)}${region.label ? ` · Label: ${esc(region.label)}` : ""}</p>`).join("") : "<p>No confident callout suggested for this frame.</p>"}${(shot.regions.limitations || []).map(value => `<p>${esc(value)}</p>`).join("")}` : ""}</details>`;
        }).join("");
    }
    function renderDirection() {
        el("editorial-auto-regions").checked = read(boardStorage() + ".auto-regions", false);
        const artifacts = state.directionRun?.artifacts || {};
        const proposal = artifacts.direction_proposal || (state.run?.target === "direction" ? state.run.artifacts?.direction_proposal : null);
        el("editorial-render-directed").hidden = !artifacts.storyboard;
        const titles = new Map((state.assetRun?.artifacts?.asset_selection || []).map(item => [item.id, item.title]));
        el("editorial-direction").innerHTML = proposal
            ? `<p>${artifacts.storyboard ? `Plan ready · ${esc(artifacts.storyboard.presentation_mode === "narrated" ? "selected narration" : "silent captions")} · ${Number(artifacts.direction_duration_seconds).toFixed(1)}s. Review every choice below.` : "This proposal did not pass validation. Adjust the manual storyboard below."}</p>`
                + proposal.beats.map((beat, index) => `<article class="item"><div><h4>Beat ${index + 1} · ${esc(beat.layout)}</h4><p>${esc(beat.rationale)}</p>${beat.media.map(use => `<p>${esc(titles.get(use.candidate_id) || use.candidate_id)} · from ${esc(use.start_seconds)}s · ${use.freeze ? "held frame" : `${esc(use.playback_rate)}× playback`} · ${esc(use.push_in)}× push-in</p>${frameCitations(artifacts, beat.beat_id, use.candidate_id)}`).join("")}${beat.quote_source_id ? `<p>Evidence quote: ${esc((state.revision?.draft.sources || []).find(source => source.id === beat.quote_source_id)?.title || beat.quote_source_id)}</p>` : ""}</div></article>`).join("")
                + (artifacts.direction_warnings || []).map(warning => `<p>${esc(warning)}</p>`).join("")
            : '<p class="empty">No validated plan for this revision yet. Save the script, acquire its supporting media, and choose the presentation above.</p>';
    }
    function directionOptions() {
        const presentation_mode = el("editorial-presentation").value;
        const narration_ids = presentation_mode === "narrated" ? Object.fromEntries([...el("editorial-narration").querySelectorAll("[data-narration-select]")].map(input => [input.dataset.narrationSelect, input.value])) : {};
        if (presentation_mode === "narrated" && (state.revision.draft.script.some(beat => !narration_ids[beat.id]) || !state.narration?.voice_enabled)) throw new Error("Enable voice and choose a recording for every beat.");
        return {presentation_mode, narration_ids, ...(el("editorial-auto-regions").checked ? {annotate_regions: true} : {})};
    }
    function storyboardPlan() {
        const beats = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")].map(row => {
            const value = row.querySelector("select").value;
            if (!value) throw new Error("Choose a visual for each script beat.");
            const [kind, ...parts] = value.split(":"); const id = parts.join(":");
            if (kind === "image") {
                const compare = row.querySelector("[data-image-compare]").value;
                const available = new Set((state.images?.images || []).filter(item => item.status === "active" && item.beat_id === row.dataset.boardBeat).map(item => item.id));
                if (!available.has(id) || (compare && !available.has(compare))) throw new Error("An image is no longer available. Choose a replacement before rendering.");
                if (compare === id) throw new Error("Choose two different images for comparison.");
                const annotation = Object.fromEntries([...row.querySelectorAll("[data-region]")].map(input => [input.dataset.region, input.value]));
                const overlays = [];
                if (annotation.kind) {
                    const region = Object.fromEntries(["x", "y", "width", "height"].map(key => [key, Number(annotation[key]) / 100]));
                    if (Object.values(region).some(value => !Number.isFinite(value)) || region.x < 0 || region.y < 0 || region.width <= 0 || region.height <= 0 || region.x + region.width > 1 || region.y + region.height > 1) throw new Error("Keep the annotation within the original image (0–100%).");
                    if (annotation.target === "1" && !compare) throw new Error("Choose a second image before marking it.");
                    overlays.push({kind: annotation.kind, media_index: Number(annotation.target), region, label: annotation.label.trim() || null});
                }
                return {beat_id: row.dataset.boardBeat, layout: compare ? "image_comparison" : "image", ...(compare ? {image_ids: [id, compare]} : {image_id: id}), media: [], image_push_in: 1, overlays};
            }
            return kind === "quote" ? {beat_id: row.dataset.boardBeat, layout: "quote", quote_source_id: id, media: []} : {beat_id: row.dataset.boardBeat, layout: "single", media: [{candidate_id: id, start_seconds: Number(row.querySelector("input[type=number]").value), freeze: row.querySelector("input[type=checkbox]").checked}]};
        });
        const mode = el("editorial-presentation").value;
        const narration_ids = Object.fromEntries([...el("editorial-narration").querySelectorAll("[data-narration-select]")].map(input => [input.dataset.narrationSelect, input.value]));
        if (mode === "narrated" && (beats.some(beat => !narration_ids[beat.beat_id]) || !state.narration?.voice_enabled)) throw new Error("Enable voice and choose a recording for every beat.");
        return {presentation_mode: mode, beats, ...(mode === "narrated" ? {narration_ids} : {})};
    }
    function renderEvidence() {
        const artifacts = state.run?.artifacts || {};
        const dossier = state.revision?.draft || artifacts.dossier || {};
        const observations = dossier.observations || artifacts.observations || [];
        const sources = dossier.sources || Object.values(artifacts.research?.sources || {});
        const claims = dossier.claims || artifacts.research?.claims || [];
        el("editorial-observations").innerHTML = observations.length ? observations.map((row) => `<article class="item"><div><p>${esc(row.observation)}</p><small>${esc(row.coverage.replaceAll("_", " "))} · ${Number(row.start_seconds).toFixed(1)}–${Number(row.end_seconds).toFixed(1)}s</small> <a href="${safeLink(row.source_url)}" target="_blank" rel="noopener noreferrer">Source video</a></div></article>`).join("") : '<p class="empty">No interpreted observations yet. Source analysis alone prepares frames and transcripts.</p>';
        const sourceMap = new Map(sources.map((source) => [source.id, source]));
        el("editorial-evidence").innerHTML = claims.length ? claims.map((claim) => `<article class="item editorial-claim"><div><span class="tag">${esc(claim.classification)} · ${esc(claim.verification)}</span><h3>${esc(claim.text)}</h3><p>${esc(claim.verification_note || "Awaiting evidence review")}</p>${(claim.contradictions || []).map((text) => `<p class="error-text">${esc(text)}</p>`).join("")}${(claim.source_ids || []).map((id) => sourceMap.get(id)).filter(Boolean).map((source) => `<blockquote>${esc(source.excerpt)}<footer><a href="${safeLink(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title)}</a> · ${esc(source.category)}</footer></blockquote>`).join("")}</div></article>`).join("") : '<p class="empty">No research claims yet. Research requires an eligible live provider.</p>';
        el("editorial-gaps").textContent = (artifacts.research?.gaps?.length || artifacts.research?.limit_reached)
            ? `${artifacts.research?.gaps?.length || 0} research gap(s). ${artifacts.research?.limit_reached ? "The research limit was reached; some questions remain open." : "Some sources could not be retrieved."}` : "";
        const scout = artifacts.asset_scout || state.assetRun?.artifacts?.asset_scout || {};
        const selected = read(storageKey(`assets.${scoutId()}`), []);
        el("editorial-assets").innerHTML = (scout.candidates || []).length ? scout.candidates.map((candidate) => `<article class="item"><div><h3><a href="${safeLink(candidate.url)}" target="_blank" rel="noopener noreferrer">${esc(candidate.title)}</a></h3><p>${esc(candidate.relevance)}</p><small>${esc(candidate.medium)} · Rights at scout time: ${esc(candidate.rights_status.replaceAll("_", " "))} · ${candidate.acquired ? "Managed media available" : "Not acquired"}</small>${canDownload(candidate) && !candidate.acquired ? `<label class="check-row"><input type="checkbox" value="${esc(candidate.id)}" ${selected.includes(candidate.id) ? "checked" : ""}> Select video for review download</label>` : ""}</div></article>`).join("") : '<p class="empty">No supporting assets discovered yet. A discovery link does not grant reuse permission.</p>';
        assetControls();
        el("editorial-asset-gaps").textContent = (scout.gaps || []).length ? `${scout.gaps.length} visual request(s) need manual material or visual composition. Review the requests below.` : "";
        el("editorial-asset-requests").textContent = JSON.stringify({ requests: scout.requests || [], gaps: scout.gaps || [] }, null, 2);
        const calls = Object.values(artifacts.provider_calls || {});
        el("editorial-receipts").textContent = JSON.stringify({
            calls: calls.map(({ status, provider, model, input_tokens, output_tokens, coverage, billing_basis }) => ({ status, provider, model, input_tokens, output_tokens, coverage, billing_basis })),
            gaps: artifacts.research?.gaps || [], critique: artifacts.critique || null,
        }, null, 2);
    }
    function renderScript() {
        const key = `${state.project.id}.${state.revision?.revision || 0}`;
        const renderKey = state.revision ? key : key + JSON.stringify(state.run?.artifacts?.script || null);
        if (state.renderKey === renderKey) {
            if (read(storageKey(`script.${key}`), null)) el("editorial-script-note").textContent = "Your unsaved changes have been restored.";
            return;
        }
        state.editorKey = key; state.renderKey = renderKey;
        const beats = state.revision?.draft.script || state.run?.artifacts?.script?.beats || [];
        const unsaved = read(storageKey(`script.${key}`), null);
        el("editorial-script").innerHTML = beats.length ? beats.map((beat, index) => `<fieldset class="editorial-beat"><legend>${index + 1}. ${esc(beat.role)}</legend><label>Narration<textarea rows="4" maxlength="4000" data-beat-narration="${index}" ${state.revision ? "" : "readonly"}>${esc(unsaved?.[index]?.narration ?? beat.narration)}</textarea></label><label>Visual direction<textarea rows="2" maxlength="4000" data-beat-visual="${index}" ${state.revision ? "" : "readonly"}>${esc(unsaved?.[index]?.visual_intent ?? beat.visual_intent)}</textarea></label><p>${esc(beat.uncertainty_disclosure || "")} · Planned ${beat.planned_duration_seconds}s</p></fieldset>`).join("") : '<p class="empty">No script yet. Research and draft a script to begin.</p>';
        el("editorial-script-note").textContent = unsaved ? "Your unsaved changes have been restored." : state.revision ? `Editing revision ${state.revision.revision}. Saving creates a new revision.` : "A blocked draft is shown read-only until it passes the script checks.";
    }
    function editedBeats() {
        return (state.revision?.draft.script || []).map((beat, index) => ({ ...beat,
            narration: el("editorial-script").querySelector(`[data-beat-narration="${index}"]`).value,
            visual_intent: el("editorial-script").querySelector(`[data-beat-visual="${index}"]`).value,
        }));
    }

    const EDITORIAL_STAGES = ["research", "script", "assets", "storyboard", "preview"];
    function stageKey() {
        return state.project ? storageKey(`stage.${state.project.id}`) : "";
    }
    function selectStage(stage, {focus = false, persist = true} = {}) {
        if (!EDITORIAL_STAGES.includes(stage)) stage = "research";
        el("editorial-stage-tabs").querySelectorAll("[data-editorial-stage]").forEach((button) => {
            const active = button.dataset.editorialStage === stage;
            button.setAttribute("aria-selected", active ? "true" : "false");
            button.tabIndex = active ? 0 : -1;
            if (active && focus) button.focus();
        });
        document.querySelectorAll("[data-editorial-stage-panel]").forEach((panel) => {
            panel.hidden = panel.dataset.editorialStagePanel !== stage;
        });
        if (persist && state.project) remember(stageKey(), stage);
        if (stage === "storyboard") void loadSourceMonitor();
    }
    function restoreStage() {
        selectStage(read(stageKey(), "research"), {persist: false});
    }
    function setAiLinks() {
        if (!state.project) return;
        const title = state.project.brief?.prompt || "the current editorial project";
        const selectedBeat = selectedTimelineBeat();
        const prompts = {
            research: `Inspect editorial project ${state.project.id} for "${title}". Review source observations, evidence quality, contradictions and research gaps. Do not invent evidence. Recommend the next research action.`,
            script: `Inspect editorial project ${state.project.id} for "${title}". Help improve the current script while preserving evidence links, uncertainty wording, pacing and viewer payoff. Flag unsupported lines instead of rewriting them as facts.`,
            assets: `Inspect editorial project ${state.project.id} for "${title}". Help fill the current asset requests using relevant, rights-aware supporting media. Prefer clear provenance and explain any gaps that need operator-supplied material.`,
            storyboard: `Inspect editorial project ${state.project.id} for "${title}". Review the visual direction and storyboard for pacing, evidence alignment, repetition and clarity. Suggest changes using cleared project assets; keep every suggestion operator-reviewable.`,
            beat: selectedBeat
                ? `Work on the selected Editorial beat. Preserve its evidence and narration unless I explicitly ask to change them. Use the attached typed beat context to explain or propose visual, timing, crop, caption, overlay, or source changes. Keep proposals operator-reviewable and identify any evidence or rights blocker.`
                : `Inspect the current Editorial storyboard and help me choose a beat to improve.`,
            preview: `Inspect editorial project ${state.project.id} for "${title}". Review render/review status, blockers and downstream publication readiness. Identify what still needs operator verification before approval.`,
        };
        document.querySelectorAll("[data-editorial-ai]").forEach((link) => {
            const kind = link.dataset.editorialAi;
            const prompt = prompts[kind] || prompts.research;
            const params = {
                focus: "chat",
                channel: state.channel,
                resource_kind: "editorial_project",
                resource_id: state.project.id,
                prompt,
            };
            if (kind === "beat" && selectedBeat?.id && state.revision) {
                params.resource_selector = selectedBeat.id;
                params.resource_revision = String(state.revision.revision);
            }
            link.href = "/ai?" + new URLSearchParams(params).toString();
        });
    }
    function advanceStage(stage) {
        selectStage(stage);
        el("editorial-stage-tabs").scrollIntoView({block: "nearest", behavior: "smooth"});
    }
    function init(transport, blobTransport) {
        window.KatchaFrameInspector.init(transport, blobTransport);
        api = transport; apiBlob = blobTransport;
        el("editorial-source-upload-file").addEventListener("change", () => {
            const file = el("editorial-source-upload-file").files[0];
            if (!file) {
                el("editorial-source-upload-status").textContent = "No local source uploaded.";
                return;
            }
            if (!el("editorial-source-upload-title").value.trim()) {
                el("editorial-source-upload-title").value = file.name.replace(/\.[^.]+$/, "");
            }
            el("editorial-source-upload-status").textContent =
                `${file.name} selected · ${(file.size / (1024 * 1024)).toFixed(1)} MiB`;
        });
        el("editorial-source-upload-button").addEventListener("click", () => {
            void uploadSourceMedia().catch(error => {
                el("editorial-source-upload-status").textContent = error.message;
                feedback(error.message, true);
            });
        });
        el("editorial-script-seed-file").addEventListener("change", () => void (async () => {
            const file = el("editorial-script-seed-file").files[0];
            if (!file) return;
            if (file.size > 256 * 1024) throw new Error("Script seed files must be 256 KiB or smaller.");
            const name = file.name.toLowerCase();
            if (!name.endsWith(".txt") && !name.endsWith(".md")) throw new Error("Import a UTF-8 .txt or .md file.");
            const bytes = await file.arrayBuffer();
            let text;
            try { text = new TextDecoder("utf-8", {fatal: true}).decode(bytes); }
            catch { throw new Error("Script seed files must be valid UTF-8 text."); }
            if (!text.trim() || text.length > 120000) throw new Error("Script seed text must be between 1 and 120,000 characters.");
            el("editorial-script-seed").value = text;
            state.scriptSeedMeta = {
                origin: "operator_file", filename: file.name,
                media_type: name.endsWith(".md") ? "text/markdown" : "text/plain",
                source_file_sha256: await sha256Hex(bytes), loadedText: text,
            };
            renderScriptSeedStatus(); saveBrief();
        })().catch(error => feedback(error.message, true)));
        el("editorial-script-seed").addEventListener("input", () => {
            if (state.scriptSeedMeta?.loadedText !== el("editorial-script-seed").value) state.scriptSeedMeta = null;
            renderScriptSeedStatus();
        });
        el("editorial-script-seed-clear").addEventListener("click", () => {
            el("editorial-script-seed").value = "";
            el("editorial-script-seed-file").value = "";
            state.scriptSeedMeta = null;
            renderScriptSeedStatus(); saveBrief();
        });
        el("editorial-source-picker").addEventListener("toggle", () => {
            if (el("editorial-source-picker").open && state.channel) void loadClipPicker(el("editorial-clip-search").value);
        });
        el("editorial-clip-search").addEventListener("input", () => {
            clearTimeout(state.clipSearchTimer);
            state.clipSearchTimer = setTimeout(() => void loadClipPicker(el("editorial-clip-search").value), 250);
        });
        el("editorial-clip-results").addEventListener("click", (event) => {
            const button = event.target.closest("[data-editorial-use-clip]");
            if (!button) return;
            button.disabled = true;
            void pinManagedClip(button.dataset.editorialUseClip)
                .catch((error) => { feedback(error.message, true); renderClipResults(); });
        });
        el("editorial-selected-clips").addEventListener("click", (event) => {
            const remove = event.target.closest("[data-editorial-remove-source]");
            if (remove) {
                const sourceUrl = remove.dataset.editorialRemoveSource;
                delete state.sourceClipBindings[sourceUrl];
                el("editorial-urls").value = sourceUrls().filter(url => url !== sourceUrl).join("\n");
                saveBrief();
                renderClipResults();
                feedback("Uploaded source removed from this new project.");
                return;
            }
            const button = event.target.closest("[data-editorial-unpin-clip]");
            if (!button) return;
            delete state.sourceClipBindings[button.dataset.editorialUnpinClip];
            saveBrief();
            renderClipResults();
            feedback("Managed clip unpinned. The source URL remains available for normal intake.");
        });
        el("editorial-beat-timeline").addEventListener("click", event => {
            const button = event.target.closest("[data-timeline-beat]");
            if (button) selectTimelineBeat(button.dataset.timelineBeat);
        });
        el("editorial-beat-timeline").addEventListener("keydown", event => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            const buttons = [...el("editorial-beat-timeline").querySelectorAll("[data-timeline-beat]")];
            const current = buttons.indexOf(event.target.closest("[data-timeline-beat]"));
            if (current < 0 || !buttons.length) return;
            event.preventDefault();
            let next = current;
            if (event.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
            if (event.key === "ArrowRight") next = (current + 1) % buttons.length;
            if (event.key === "Home") next = 0;
            if (event.key === "End") next = buttons.length - 1;
            selectTimelineBeat(buttons[next].dataset.timelineBeat, {focus: true});
        });
        el("editorial-stage-tabs").addEventListener("click", (event) => {
            const button = event.target.closest("[data-editorial-stage]");
            if (button) selectStage(button.dataset.editorialStage);
        });
        el("editorial-stage-tabs").addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            const current = EDITORIAL_STAGES.indexOf(event.target.dataset.editorialStage);
            if (current < 0) return;
            event.preventDefault();
            let next = current;
            if (event.key === "ArrowLeft") next = (current - 1 + EDITORIAL_STAGES.length) % EDITORIAL_STAGES.length;
            if (event.key === "ArrowRight") next = (current + 1) % EDITORIAL_STAGES.length;
            if (event.key === "Home") next = 0;
            if (event.key === "End") next = EDITORIAL_STAGES.length - 1;
            selectStage(EDITORIAL_STAGES[next], {focus: true});
        });
        el("editorial-narration-billing").addEventListener("click", event => {
            const button = event.target.closest("[data-billing-save]");
            if (!button) return;
            const row = button.closest("[data-billing-beat]");
            void guarded(async () => {
                if (!row.querySelector("[data-billing-confirm]").checked) throw new Error("Verify the final provider outcome before saving.");
                const payload = {beat_id: row.dataset.billingBeat, expected_dispatch_count: Number(row.dataset.billingAttempt), outcome: row.querySelector("[data-billing-outcome]").value, actual_cost_usd: Number(row.querySelector("[data-billing-cost]").value), provider_receipt: row.querySelector("[data-billing-receipt]").value.trim(), confirmed: true};
                if (!payload.provider_receipt || !Number.isFinite(payload.actual_cost_usd) || payload.actual_cost_usd < 0 || payload.actual_cost_usd > 1000) throw new Error("Enter the provider receipt and final cost between $0 and $1000.");
                const channel = state.channel; const project = state.project.id; const run = state.run.editorial_run_id;
                payload.idempotency_key = identity(`speech-billing.${run}.${payload.beat_id}`, payload);
                await api(path(channel, `/${project}/runs/${run}/narration-billing`), {method: "POST", body: JSON.stringify(payload)});
                if (channel === state.channel) { await open(project, {focus: false}); advanceStage("storyboard"); }
            });
        });
        el("editorial-generate-narration").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            if (!el("editorial-narration-confirm").checked) throw new Error("Confirm use of the channel voice and budget.");
            const limit = Number(el("editorial-narration-limit").value);
            if (!Number.isFinite(limit) || limit <= 0 || limit > 25) throw new Error("Enter an estimated cost limit between $0.01 and $25.");
            const payload = {target: "narration", expected_revision: state.project.revision, confirm_narration: true, max_narration_estimate_usd: limit};
            payload.idempotency_key = identity(`generate-narration.${project}`, payload);
            await api(path(channel, `/${project}/runs`), {method: "POST", body: JSON.stringify(payload)});
            if (channel === state.channel) { await open(project, {focus: false}); advanceStage("storyboard"); }
        }));
        el("editorial-image-form").addEventListener("input", saveImageForm);
        el("editorial-image-upload").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id; const revision = state.project.revision;
            const file = el("editorial-image-file").files[0];
            if (!file || file.size > 16 * 1024 * 1024) throw new Error("Choose a PNG/JPEG image no larger than 16 MiB.");
            if (!el("editorial-image-confirm").checked) throw new Error("Confirm permission to use this image.");
            const metadata = {revision, beat_id: el("editorial-image-beat").value, title: el("editorial-image-title").value.trim(), source_reference: el("editorial-image-source").value.trim(), use_note: el("editorial-image-permission").value.trim(), illustration: el("editorial-image-illustration").checked, permitted_use: true};
            if (!metadata.title || !metadata.source_reference || !metadata.use_note) throw new Error("Enter the on-screen credit, source and permitted-use basis.");
            const bytes = await file.arrayBuffer();
            const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(value => value.toString(16).padStart(2, "0")).join("");
            if (channel !== state.channel || project !== state.project?.id) return;
            metadata.idempotency_key = identity(`image.${project}`, {...metadata, sha256: hash});
            const requestKey = storageKey(`request.image.${project}`);
            const uploaded = await api(path(channel, `/${project}/images?${new URLSearchParams(metadata)}`), {method: "POST", body: bytes, headers: {"Content-Type": "application/octet-stream"}});
            if (uploaded.status !== "active") {
                sessionStorage.removeItem(requestKey);
                throw new Error("This earlier upload was removed. Upload again to attach it as a new image.");
            }
            if (channel === state.channel && project === state.project?.id) {
                el("editorial-image-file").value = ""; el("editorial-image-confirm").checked = false;
                await open(project, {focus: false}); feedback("Image saved. Select it as a beat’s visual below.");
            }
        }));
        el("editorial-images").addEventListener("click", event => {
            const button = event.target.closest("[data-image-revoke]");
            if (!button) return;
            void guarded(async () => {
                const channel = state.channel; const project = state.project.id;
                const requestKey = storageKey(`request.image.${project}`);
                await api(path(channel, `/${project}/images/${encodeURIComponent(button.dataset.imageRevoke)}/revoke`), {method: "POST"});
                sessionStorage.removeItem(requestKey);
                if (channel === state.channel && project === state.project?.id) await open(project, {focus: false});
            });
        });
        window.KatchaEditorialHistory.init(transport, blobTransport);
        el("editorial-presentation").addEventListener("change", () => {
            saveNarrationChoices();
            renderNarration();
            renderActions();
            scheduleStoryboardWorkspaceSave();
        });
        el("editorial-narration").addEventListener("change", event => {
            saveNarrationChoices();
            if (event.target.matches("[data-narration-select]")) {
                scheduleStoryboardWorkspaceSave();
            }
        });
        el("editorial-narration").addEventListener("click", event => {
            const button = event.target.closest("[data-narration-upload], [data-narration-revoke]");
            if (!button) return;
            const row = button.closest("[data-narration-beat]");
            void guarded(async () => {
                const channel = state.channel; const project = state.project.id; const revision = state.project.revision;
                if (state.stale || read(storageKey(`pending.${project}`), null)) throw new Error("Save or discard script edits before changing recordings.");
                if (state.storyboardDirty) await flushStoryboardWorkspace();
                const base = path(channel, `/${project}/narration`);
                if (button.hasAttribute("data-narration-revoke")) {
                    const selected = row.querySelector("select").value;
                    if (!selected) throw new Error("Choose the recording to remove.");
                    await api(`${base}/${encodeURIComponent(selected)}/revoke`, {method: "POST"});
                } else {
                    const file = row.querySelector("input[type=file]").files[0];
                    if (!file || file.size > 32 * 1024 * 1024) throw new Error("Choose a PCM WAV recording no larger than 32 MiB.");
                    if (!row.querySelector("[data-narration-permitted]").checked) throw new Error("Confirm permission to use this recording.");
                    const audio = await file.arrayBuffer();
                    const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", audio))].map(value => value.toString(16).padStart(2, "0")).join("");
                    if (channel !== state.channel || project !== state.project?.id) return;
                    const payload = {revision, beat_id: row.dataset.narrationBeat, sha256: hash};
                    const key = identity(`upload.${project}.${payload.beat_id}`, payload);
                    const query = new URLSearchParams({revision, beat_id: payload.beat_id, idempotency_key: key, permitted_use: "true"});
                    const uploaded = await api(`${base}?${query}`, {method: "POST", body: audio, headers: {"Content-Type": "audio/wav"}});
                    if (channel === state.channel && project === state.project?.id) {
                        const saved = read(narrationKey(), {mode: "narrated", choices: {}});
                        saved.mode = "narrated";
                        saved.choices[payload.beat_id] = uploaded.id;
                        remember(narrationKey(), saved);
                        state.narration = {
                            ...(state.narration || {}),
                            recordings: [
                                ...((state.narration?.recordings || []).filter(
                                    item => item.id !== uploaded.id,
                                )),
                                uploaded,
                            ],
                        };
                        state.narrationKey = "";
                        renderNarration();
                        scheduleStoryboardWorkspaceSave();
                        await flushStoryboardWorkspace();
                    }
                }
                if (channel === state.channel) { state.narrationKey = ""; await open(project, {focus: false}); }
            });
        });
        el("editorial-review-note").addEventListener("input", () => remember(reviewNoteKey(), el("editorial-review-note").value));
        for (const [id, decision] of [["editorial-approve", "approve"], ["editorial-request-changes", "request_changes"]]) {
            el(id).addEventListener("click", () => void guarded(async () => {
                const channel = state.channel; const project = state.project.id; const run = state.run.editorial_run_id;
                const key = reviewNoteKey();
                const payload = {expected_revision: state.run.input_revision, expected_review_sequence: state.review.sequence, decision, note: el("editorial-review-note").value};
                payload.idempotency_key = identity(`review.${run}`, payload);
                await api(path(channel, `/${project}/runs/${run}/review`), {method: "POST", body: JSON.stringify(payload)});
                sessionStorage.removeItem(key);
                if (channel === state.channel) { await open(project, {focus: false}); advanceStage("preview"); }
            }));
        }
        el("editorial-auto-regions").addEventListener("change", () => remember(boardStorage() + ".auto-regions", el("editorial-auto-regions").checked));
        el("editorial-storyboard-undo").addEventListener("click", () => {
            void guarded(undoStoryboardWorkspace);
        });
        el("editorial-direct").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            const payload = {target: "direction", expected_revision: state.project.revision, asset_run_id: state.assetRun.editorial_run_id, direction: directionOptions()};
            payload.idempotency_key = identity(`direction.${project}`, payload);
            await api(path(channel, `/${project}/runs`), {method: "POST", body: JSON.stringify(payload)});
            if (channel === state.channel) { await open(project, {focus: false}); advanceStage("storyboard"); }
        }));
        el("editorial-render-directed").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            const artifacts = state.directionRun.artifacts;
            const payload = {target: "render", expected_revision: state.project.revision, asset_run_id: artifacts.direction_asset_run_id, direction_run_id: state.directionRun.editorial_run_id, storyboard: artifacts.storyboard};
            await api(path(channel, `/${project}/storyboard/preflight`), {method: "POST", body: JSON.stringify({expected_revision: payload.expected_revision, asset_run_id: payload.asset_run_id, plan: payload.storyboard})});
            payload.idempotency_key = identity(`render.${project}`, payload);
            await api(path(channel, `/${project}/runs`), {method: "POST", body: JSON.stringify(payload)});
            if (channel === state.channel) { await open(project, {focus: false}); advanceStage("preview"); }
        }));
        const persistStoryboardChoice = (event) => {
            saveStoryboard();
            showFootageControls();
            refreshTimelineStatus();
            scheduleStoryboardWorkspaceSave();
            if (event?.type === "change" && event.target.matches("[data-primary-visual]")) {
                void loadSourceMonitor();
            }
        };
        el("editorial-storyboard").addEventListener("input", persistStoryboardChoice);
        el("editorial-storyboard").addEventListener("change", persistStoryboardChoice);
        el("editorial-render").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            const payload = {target: "render", expected_revision: state.project.revision, asset_run_id: state.assetRun?.editorial_run_id || null, storyboard: storyboardPlan()};
            await api(path(channel, `/${project}/storyboard/preflight`), {method: "POST", body: JSON.stringify({expected_revision: payload.expected_revision, asset_run_id: payload.asset_run_id, plan: payload.storyboard})});
            payload.idempotency_key = identity(`render.${project}`, payload);
            await api(path(channel, `/${project}/runs`), {method: "POST", body: JSON.stringify(payload)});
            if (channel === state.channel) { await open(project, {focus: false}); advanceStage("preview"); }
        }));
        el("editorial-play").addEventListener("click", () => void guarded(async () => {
            await loadCurrentPreview({advance: true});
        }));
        el("editorial-program-load").addEventListener("click", () => void guarded(async () => {
            await loadCurrentPreview();
        }));
        el("editorial-form").addEventListener("input", saveBrief);
        el("editorial-form").addEventListener("submit", (event) => {
            event.preventDefault();
            void guarded(async () => {
                const channel = state.channel;
                reconcileSourceClipBindings();
                const seed = await scriptSeedPayload();
                const payload = { brief: {
                    prompt: el("editorial-prompt").value.trim(),
                    source_urls: sourceUrls(),
                    source_clip_bindings: Object.fromEntries(
                        Object.entries(state.sourceClipBindings).map(([url, item]) => [url, item.clip_id]),
                    ),
                    ...(seed ? {script_seed: seed} : {}),
                    target_duration_seconds: Number(el("editorial-duration").value),
                } };
                payload.idempotency_key = identity("create", payload);
                const row = await api(path(channel), { method: "POST", body: JSON.stringify(payload) });
                if (channel === state.channel) { await load(channel); await open(row.id); }
            });
        });
        el("editorial-refresh").addEventListener("click", () => { if (!state.busy) void load(state.channel); });
        el("editorial-projects").addEventListener("click", (event) => {
            const target = event.target.closest("[data-open-editorial]");
            if (target && !state.busy) void open(target.dataset.openEditorial).catch((error) => feedback(error.message, true));
        });
        el("editorial-actions").addEventListener("click", (event) => {
            const target = event.target.closest("[data-editorial-action]");
            if (!target) return;
            void guarded(async () => {
                const channel = state.channel; const project = state.project.id;
                const action = target.dataset.editorialAction;
                let suffix = `/${project}/runs`; let payload;
                if (["resume", "cancel"].includes(action)) {
                    suffix += `/${state.run.editorial_run_id}/${action}`;
                    payload = { expected_attempt: state.run.attempt };
                } else {
                    payload = { expected_revision: state.project.revision, target: action };
                    payload.idempotency_key = identity(`start.${project}`, payload);
                }
                await api(path(channel, suffix), { method: "POST", body: JSON.stringify(payload) });
                if (channel === state.channel) {
                    await open(project, { focus: false });
                    advanceStage(action === "assets" ? "assets" : ["script", "resume"].includes(action) ? "script" : "research");
                }
            });
        });
        el("editorial-assets").addEventListener("change", () => {
            remember(storageKey(`assets.${scoutId()}`), selectedAssets()); assetControls();
        });
        el("editorial-acquire-assets").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            const payload = { target: "acquire_assets", expected_revision: state.project.revision,
                scout_run_id: scoutId(), asset_candidate_ids: selectedAssets() };
            payload.idempotency_key = identity(`acquire.${project}`, payload);
            await api(path(channel, `/${project}/runs`), { method: "POST", body: JSON.stringify(payload) });
            if (channel === state.channel) { await open(project, { focus: false }); advanceStage("storyboard"); }
        }));
        el("editorial-script").addEventListener("input", () => {
            if (state.revision) {
                const beats = editedBeats();
                remember(storageKey(`script.${state.editorKey}`), beats);
                remember(storageKey(`pending.${state.project.id}`), { revision: state.revision.revision, draft: { ...state.revision.draft, script: beats } });
                renderActions();
            }
        });
        el("editorial-discard").addEventListener("click", () => {
            if (state.busy) return;
            sessionStorage.removeItem(storageKey(`pending.${state.project.id}`));
            sessionStorage.removeItem(storageKey(`script.${state.editorKey}`));
            state.renderKey = "";
            void open(state.project.id, { focus: false })
                .then(() => advanceStage("script"))
                .catch((error) => feedback(error.message, true));
        });
        el("editorial-save-script").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            const key = storageKey(`script.${state.editorKey}`);
            const payload = { expected_revision: state.revision.revision, draft: { ...state.revision.draft, script: editedBeats() } };
            payload.idempotency_key = identity(`save.${project}`, payload);
            await api(path(channel, `/${project}/revisions`), { method: "POST", body: JSON.stringify(payload) });
            sessionStorage.removeItem(key);
            sessionStorage.removeItem(`katcha.editorial.${channel}.pending.${project}`);
            if (channel === state.channel) { await open(project, { focus: false }); advanceStage("script"); feedback("Script saved as a new revision. Review evidence before production."); }
        }));
    }
    return { init, load, frameCitations };
})();
