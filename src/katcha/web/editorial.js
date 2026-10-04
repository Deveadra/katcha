/* Editorial projects share Production's channel, authentication and API transport. */
window.KatchaEditorial = (() => {
    const el = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
    const state = { channel: "", epoch: 0, project: null, revision: null, run: null, busy: false, timer: null, editorKey: "" };
    let api, apiBlob;
    function clearPreview() {
        if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
        state.previewUrl = null; el("editorial-preview").removeAttribute("src"); el("editorial-preview").hidden = true;
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
    function saveBrief() {
        if (state.channel) remember(storageKey("brief"), {
            prompt: el("editorial-prompt").value, urls: el("editorial-urls").value,
            seconds: el("editorial-duration").value,
        });
    }
    function restoreBrief() {
        const value = read(storageKey("brief"), {});
        el("editorial-prompt").value = value.prompt || "";
        el("editorial-urls").value = value.urls || "";
        el("editorial-duration").value = value.seconds || "420";
    }
    async function guarded(action) {
        if (state.busy || !state.channel) return;
        state.busy = true;
        el("editorial-form-fields").disabled = true;
        el("editorial-detail").querySelectorAll("button").forEach((button) => { button.disabled = true; });
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
            window.KatchaEditorialHistory.reset();
            clearPreview(); state.boardKey = ""; state.assetRun = null;
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
                <article class="item"><div><h3>${esc(row.brief.prompt)}</h3><p>${row.brief.source_urls.length} source(s) · Revision ${row.revision}</p></div>
                <button type="button" class="mini" data-open-editorial="${esc(row.id)}">Open project</button></article>`).join("")
                : '<div class="empty">No editorial projects yet. Add a brief and a source link to create one.</div>';
            if (state.project) await open(state.project.id, { focus: false });
        } catch (error) {
            if (epoch !== state.epoch) return;
            el("editorial-projects").innerHTML = '<div class="empty">Projects are unavailable. Use Refresh projects to try again.</div>';
            feedback(error.message, true);
        }
    }
    async function open(id, { focus = true } = {}) {
        clearTimeout(state.timer);
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
        const narration = project.revision > 0 ? await api(`${base}/narration?revision=${project.revision}`).catch(error => ({error: error.message, recordings: []})) : {voice_enabled: true, recordings: []};
        const acquisition = runs.find(item => item.target === "acquire_assets" && item.status === "completed" && item.input_revision === project.revision);
        const assetRun = acquisition ? (acquisition.editorial_run_id === run?.editorial_run_id ? run : await api(`${base}/runs/${encodeURIComponent(acquisition.editorial_run_id)}`)) : null;
        if (epoch !== state.epoch) return;
        if (state.project?.id !== id || state.run?.editorial_run_id !== run?.editorial_run_id) clearPreview();
        window.KatchaEditorialHistory.context(channel, id);
        state.assetRun = assetRun; state.review = review; state.narration = narration;
        const pending = read(storageKey(`pending.${id}`), null);
        state.project = project; state.revision = revisions[0] || null; state.run = run;
        state.stale = Boolean(pending && pending.revision < project.revision);
        if (state.stale) state.revision = pending;
        el("editorial-detail").hidden = false;
        el("editorial-title").textContent = project.brief.prompt;
        el("editorial-status").textContent = run
            ? `${String(run.status).replaceAll("_", " ")} · ${String(run.stage).replaceAll("_", " ")}`
            : "Brief saved. Choose source analysis or research and scripting.";
        el("editorial-discard").hidden = !state.stale;
        feedback(run?.error || "Project loaded. Generated claims and scripts need editorial review.", Boolean(run?.error));
        renderActions(); renderEvidence(); renderScript(); renderStoryboard(); renderReview(); renderNarration(); renderActions();
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
        el("editorial-detail").querySelectorAll("button").forEach((button) => { button.disabled = state.busy; });
        el("editorial-save-script").disabled = state.busy || !state.revision || Boolean(active) || state.stale;
        assetControls();
        el("editorial-render").disabled = state.busy || Boolean(active) || state.stale || !state.assetRun || Boolean(read(storageKey(`pending.${state.project.id}`), null));
        el("editorial-render").disabled ||= el("editorial-presentation").value === "narrated" && !state.narration?.voice_enabled;
        el("editorial-render").textContent = el("editorial-presentation").value === "narrated" ? "Create narrated preview" : "Create silent captioned preview";
        el("editorial-play").hidden = state.run?.stage !== "render_ready_for_review" || state.run?.status !== "completed";
        el("editorial-approve").disabled = state.busy || !state.review?.can_approve || !state.previewUrl || state.stale || Boolean(read(storageKey(`pending.${state.project.id}`), null));
        el("editorial-request-changes").disabled = state.busy || !state.review || Boolean(state.review.error);
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
        const key = narrationKey() + JSON.stringify(data.recordings);
        if (key === state.narrationKey) return;
        state.narrationKey = key;
        el("editorial-narration").innerHTML = (state.revision?.draft.script || []).map((beat, index) => {
            const recordings = (data.recordings || []).filter(item => item.beat_id === beat.id && item.status === "active");
            return `<fieldset class="editorial-beat" data-narration-beat="${esc(beat.id)}"><legend>Beat ${index + 1}</legend><p>${esc(beat.narration)}</p><label>Recording for beat ${index + 1}<select data-narration-select="${esc(beat.id)}"><option value="">Choose a recording</option>${recordings.map(item => `<option value="${esc(item.id)}" ${saved.choices[beat.id] === item.id ? "selected" : ""}>${Number(item.duration_seconds).toFixed(2)}s · ${esc(item.created_at)}</option>`).join("")}</select></label><button type="button" class="mini" data-narration-revoke>Remove selected recording</button><label>WAV recording for beat ${index + 1}<input type="file" accept=".wav,audio/wav" data-narration-file></label><label class="check-row"><input type="checkbox" data-narration-permitted> I have permission to use this recording</label><button type="button" class="mini" data-narration-upload>Upload recording</button></fieldset>`;
        }).join("");
    }
    function boardStorage() { return storageKey(`board.${state.project.id}.${state.project.revision}.${state.assetRun?.editorial_run_id || "none"}`); }
    function saveStoryboard() {
        const rows = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")].map(row => ({
            choice: row.querySelector("select").value, start: row.querySelector("input[type=number]").value,
            freeze: row.querySelector("input[type=checkbox]").checked,
        }));
        remember(boardStorage(), rows);
    }
    function renderStoryboard() {
        const key = boardStorage();
        if (key === state.boardKey) return;
        state.boardKey = key;
        const choices = state.assetRun?.artifacts?.asset_selection || [];
        const receipts = state.assetRun?.artifacts?.acquired_assets || {};
        const draft = state.revision?.draft || {};
        const saved = read(key, []);
        el("editorial-storyboard").innerHTML = (draft.script || []).map((beat, index) => {
            const claims = (draft.claims || []).filter(claim => beat.claim_ids.includes(claim.id));
            const sourceIds = new Set(claims.flatMap(claim => claim.source_ids));
            const sources = (draft.sources || []).filter(source => sourceIds.has(source.id));
            const options = [...choices.filter(item => item.beat_id === beat.id && receipts[item.id]).map(item => ({value: `media:${item.id}`, label: item.title})), ...sources.map(item => ({value: `quote:${item.id}`, label: `Evidence quote: ${item.title}`}))];
            return `<fieldset class="editorial-beat" data-board-beat="${esc(beat.id)}"><legend>Beat ${index + 1} · ${beat.planned_duration_seconds}s</legend><label>Visual<select>${'<option value="">Choose a visual</option>'}${options.map(item => `<option value="${esc(item.value)}" ${saved[index]?.choice === item.value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></label><label>Footage start (seconds)<input type="number" min="0" step="0.1" value="${esc(saved[index]?.start || "0")}"></label><label class="check-row"><input type="checkbox" ${saved[index]?.freeze ? "checked" : ""}> Hold this frame</label></fieldset>`;
        }).join("") || '<p class="empty">Save a script and acquire supporting media to prepare a preview.</p>';
    }
    function storyboardPlan() {
        const beats = [...el("editorial-storyboard").querySelectorAll("[data-board-beat]")].map(row => {
            const value = row.querySelector("select").value;
            if (!value) throw new Error("Choose a visual for each script beat.");
            const [kind, ...parts] = value.split(":"); const id = parts.join(":");
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
    function init(transport, blobTransport) {
        api = transport; apiBlob = blobTransport;
        window.KatchaEditorialHistory.init(transport, blobTransport);
        el("editorial-presentation").addEventListener("change", () => { saveNarrationChoices(); renderNarration(); renderActions(); });
        el("editorial-narration").addEventListener("change", saveNarrationChoices);
        el("editorial-narration").addEventListener("click", event => {
            const button = event.target.closest("[data-narration-upload], [data-narration-revoke]");
            if (!button) return;
            const row = button.closest("[data-narration-beat]");
            void guarded(async () => {
                const channel = state.channel; const project = state.project.id; const revision = state.project.revision;
                if (state.stale || read(storageKey(`pending.${project}`), null)) throw new Error("Save or discard script edits before changing recordings.");
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
                        saved.choices[payload.beat_id] = uploaded.id; remember(narrationKey(), saved);
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
                if (channel === state.channel) await open(project, {focus: false});
            }));
        }
        el("editorial-storyboard").addEventListener("input", saveStoryboard);
        el("editorial-render").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            const payload = {target: "render", expected_revision: state.project.revision, asset_run_id: state.assetRun.editorial_run_id, storyboard: storyboardPlan()};
            await api(path(channel, `/${project}/storyboard/preflight`), {method: "POST", body: JSON.stringify({expected_revision: payload.expected_revision, asset_run_id: payload.asset_run_id, plan: payload.storyboard})});
            payload.idempotency_key = identity(`render.${project}`, payload);
            await api(path(channel, `/${project}/runs`), {method: "POST", body: JSON.stringify(payload)});
            if (channel === state.channel) await open(project, {focus: false});
        }));
        el("editorial-play").addEventListener("click", () => void guarded(async () => {
            const epoch = state.epoch;
            const blob = await apiBlob(path(state.channel, `/${state.project.id}/runs/${state.run.editorial_run_id}/preview`));
            if (epoch !== state.epoch) return;
            clearPreview(); state.previewUrl = URL.createObjectURL(blob);
            el("editorial-preview").src = state.previewUrl; el("editorial-preview").hidden = false;
            feedback("Preview loaded. Review the evidence, timing and media before publication.");
        }));
        el("editorial-form").addEventListener("input", saveBrief);
        el("editorial-form").addEventListener("submit", (event) => {
            event.preventDefault();
            void guarded(async () => {
                const channel = state.channel;
                const payload = { brief: { prompt: el("editorial-prompt").value.trim(), source_urls: el("editorial-urls").value.split(/\s+/).filter(Boolean), target_duration_seconds: Number(el("editorial-duration").value) } };
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
                if (channel === state.channel) await open(project, { focus: false });
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
            if (channel === state.channel) await open(project, { focus: false });
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
            void open(state.project.id, { focus: false }).catch((error) => feedback(error.message, true));
        });
        el("editorial-save-script").addEventListener("click", () => void guarded(async () => {
            const channel = state.channel; const project = state.project.id;
            const key = storageKey(`script.${state.editorKey}`);
            const payload = { expected_revision: state.revision.revision, draft: { ...state.revision.draft, script: editedBeats() } };
            payload.idempotency_key = identity(`save.${project}`, payload);
            await api(path(channel, `/${project}/revisions`), { method: "POST", body: JSON.stringify(payload) });
            sessionStorage.removeItem(key);
            sessionStorage.removeItem(`katcha.editorial.${channel}.pending.${project}`);
            if (channel === state.channel) { await open(project, { focus: false }); feedback("Script saved as a new revision. Review evidence before production."); }
        }));
    }
    return { init, load };
})();
