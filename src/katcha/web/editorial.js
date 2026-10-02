/* Editorial projects share Production's channel, authentication and API transport. */
window.KatchaEditorial = (() => {
    const el = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
    const state = { channel: "", epoch: 0, project: null, revision: null, run: null, busy: false, timer: null, editorKey: "" };
    let api;
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
        const epoch = ++state.epoch;
        const channel = state.channel;
        feedback("Loading project evidence and progress…");
        const base = path(channel, `/${encodeURIComponent(id)}`);
        const [project, revisions, runs] = await Promise.all([
            api(base), api(`${base}/revisions?limit=1`), api(`${base}/runs?limit=1`),
        ]);
        const run = runs[0] ? await api(`${base}/runs/${encodeURIComponent(runs[0].editorial_run_id)}`) : null;
        if (epoch !== state.epoch) return;
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
        renderActions(); renderEvidence(); renderScript();
        if (state.stale) feedback("A newer script revision is available. Your unsaved text is retained below. Copy it before discarding edits to load the latest version.", true);
        if (focus) el("editorial-title").focus();
        if (run && ["queued", "running"].includes(run.status)) {
            state.timer = setTimeout(() => {
                if (epoch === state.epoch && !state.busy) void open(id, { focus: false }).catch((error) => feedback(`Progress unavailable: ${error.message}. Refresh projects to reconnect.`, true));
            }, 10000);
        }
    }
    function renderActions() {
        const active = state.run && ["queued", "running"].includes(state.run.status);
        const canResume = state.run && ["failed", "blocked"].includes(state.run.status);
        el("editorial-actions").innerHTML = active
            ? '<button class="mini" type="button" data-editorial-action="cancel">Stop work</button>'
            : '<button class="mini" type="button" data-editorial-action="analysis">Analyze sources</button><button class="button primary" type="button" data-editorial-action="script">Research and draft script</button>'
                + (canResume ? '<button class="mini" type="button" data-editorial-action="resume">Resume saved work</button>' : "");
        el("editorial-detail").querySelectorAll("button").forEach((button) => { button.disabled = state.busy; });
        el("editorial-save-script").disabled = state.busy || !state.revision || Boolean(active) || state.stale;
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
    function init(transport) {
        api = transport;
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
        el("editorial-script").addEventListener("input", () => {
            if (state.revision) {
                const beats = editedBeats();
                remember(storageKey(`script.${state.editorKey}`), beats);
                remember(storageKey(`pending.${state.project.id}`), { revision: state.revision.revision, draft: { ...state.revision.draft, script: beats } });
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
