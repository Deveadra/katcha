/* All state comes from the authenticated, same-origin Katcha control plane. */
const state = {
    token: "",
    channel: "",
    episodes: [],
    blueprints: [],
    attempts: new Map(),
    brands: [],
    candidates: [],
    productions: [],
    preview: null,
    previewUrl: null,
    epoch: 0,
};
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const date = (value) => value ? new Date(value).toLocaleString() : "—";
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function message(value, error = false) { $("message").textContent = value; $("message").classList.toggle("error", error); }
async function request(path, options = {}) {
    return fetch(path, {
        ...options,
        headers: {
            ...(options.body ? { "Content-Type": "application/json" } : {}),
            ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
            ...options.headers,
        },
    });
}
async function api(path, options = {}) {
    const response = await request(path, options);
    if (!response.ok) {
        let body; try { body = await response.json(); } catch {}
        throw new Error(typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`);
    }
    return response.json();
}
async function apiBlob(path) {
    const response = await request(path);
    if (!response.ok) {
        let body; try { body = await response.json(); } catch {}
        throw new Error(typeof body?.detail === "string" ? body.detail : `Media request failed (${response.status})`);
    }
    return response.blob();
}
function channelPath(suffix) { return `/v1/channels/${encodeURIComponent(state.channel)}${suffix}`; }
function clearPreviewUrl() {
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
}
function statusType(row) {
    const attempt = state.attempts.get(row.id);
    if (attempt?.status === "dead_letter" || row.error) return "attention";
    if (["published", "completed", "rejected", "cancelled"].includes(row.status)) return "done";
    return "active";
}
function renderBlueprints() {
    $("count-blueprints").textContent = state.blueprints.length;
    $("blueprints").innerHTML = state.blueprints.length ? state.blueprints.map((row) => `<article class="item"><div><h3>${escapeHTML(row.blueprint_key)} <small>v${escapeHTML(row.version)}</small></h3><p>Contract ${escapeHTML(row.contract_version)} · Created ${escapeHTML(date(row.created_at))}</p></div><div class="item-actions">${row.is_default ? '<span class="pill active">DEFAULT</span>' : row.is_active ? '<span class="pill active">ACTIVE</span>' : `<button class="mini" data-blueprint-activate="${escapeHTML(row.blueprint_key)}" data-version="${escapeHTML(row.version)}">Activate</button>`}</div></article>`).join("") : '<div class="empty">No blueprints configured for this channel.</div>';
}
function renderPerformance(row) {
    $("performance").classList.remove("empty");
    $("performance").innerHTML = row ? `<strong>${escapeHTML(row.publication_count)}</strong><p>Publications measured in the ${escapeHTML(row.age_bucket_hours)} hour window. ${escapeHTML(row.comparison_status?.replaceAll("_", " ") || "Comparison pending")}.</p><div class="metrics"><span class="pill">${escapeHTML(row.blueprint_group_count)} blueprint groups</span><span class="pill">${escapeHTML(row.retention_covered_publications)} retention samples</span><span class="pill">${escapeHTML(row.revenue_covered_publications)} revenue samples</span></div><p>Updated ${escapeHTML(date(row.created_at))}</p>` : '<div class="empty">No measured performance yet. Results appear after published episodes have analytics.</div>';
}
function renderEpisodes() {
    const filter = $("filter").value;
    const rows = state.episodes.filter((row) => filter === "all" || statusType(row) === filter);
    $("count-total").textContent = state.episodes.length;
    $("count-active").textContent = state.episodes.filter((row) => statusType(row) === "active").length;
    $("count-attention").textContent = state.episodes.filter((row) => statusType(row) === "attention").length;
    $("episodes").innerHTML = rows.length ? rows.map((row) => {
        const attempt = state.attempts.get(row.id);
        const recoverable = attempt?.status === "dead_letter";
        const type = statusType(row);
        return `<article class="item"><div><h3>${escapeHTML(row.premise)}</h3><p>Stage: ${escapeHTML(row.stage)} · ${escapeHTML(row.status)} · Updated ${escapeHTML(date(row.updated_at))}</p><p class="meta">${escapeHTML(row.edit_blueprint_key || "Channel default blueprint")}${row.edit_blueprint_version ? ` · v${escapeHTML(row.edit_blueprint_version)}` : ""} · Generation ${escapeHTML(row.generation)}</p>${row.error || attempt?.error ? `<p class="error-text">${escapeHTML(attempt?.error || row.error)}</p>` : ""}</div><div class="item-actions"><span class="pill ${type === "attention" ? "warning" : type === "done" ? "active" : ""}">${escapeHTML(recoverable ? "RENDER FAILED" : row.status.replaceAll("_", " ").toUpperCase())}</span>${recoverable ? `<button class="mini" data-recover="${escapeHTML(row.id)}">Recover render</button>` : ""}<button class="mini" data-attempts="${escapeHTML(row.id)}">Render details</button></div></article>`;
    }).join("") : `<div class="empty">${filter === "all" ? "No episodes in this channel yet." : "No episodes match this filter."}</div>`;
}
function previewableProductions() {
    return state.productions.filter((row) =>
        row.render_manifest?.version === "short-render-v1"
        && Array.isArray(row.render_manifest?.overlays)
        && row.render_manifest.overlays.length > 0
    );
}
function renderBrandLab() {
    const active = state.brands.find((row) => row.is_active);
    const staged = state.brands.filter((row) => !row.is_active);
    const stagedVersions = new Set(state.brands.map((row) => row.version));
    const availableCandidates = state.candidates.filter((row) => !stagedVersions.has(row.version));
    const productions = previewableProductions();
    const preview = state.preview;

    const activeCard = active
        ? `<div class="brand-current"><span class="pill active">LIVE</span><div><strong>${escapeHTML(active.brand_key)} v${escapeHTML(active.version)}</strong><small>Frozen productions keep the version they were created with.</small></div></div>`
        : '<div class="empty">No active brand version is configured.</div>';

    const candidateCards = availableCandidates.map((row) => `<article class="brand-candidate"><div><strong>${escapeHTML(row.brand_key)} v${escapeHTML(row.version)}</strong><small>Built-in staged candidate · does not change the live channel</small></div><button class="mini" data-stage-brand="${escapeHTML(row.version)}">Stage candidate</button></article>`).join("");

    const stagedOptions = staged.map((row) => `<option value="${escapeHTML(row.version)}">${escapeHTML(row.brand_key)} v${escapeHTML(row.version)}</option>`).join("");
    const productionOptions = productions.map((row) => {
        const label = row.render_manifest?.title_angle || `Production ${String(row.id).slice(0, 8)}`;
        return `<option value="${escapeHTML(row.id)}">${escapeHTML(label)} · ${escapeHTML(row.status)} · ${escapeHTML(date(row.updated_at))}</option>`;
    }).join("");

    let workflow = "";
    if (staged.length && productions.length) {
        workflow = `<div class="preview-controls"><label>STAGED BRAND<select id="preview-brand-version">${stagedOptions}</select></label><label>FROZEN PRODUCTION<select id="preview-production">${productionOptions}</select></label><button class="button primary" data-render-preview>Render preview ↗</button></div>`;
    } else if (!staged.length) {
        workflow = '<div class="empty">Stage a brand candidate before rendering a preview.</div>';
    } else {
        workflow = '<div class="empty">No channel production with a frozen short-render-v1 narration timeline is available yet.</div>';
    }

    let previewCard = "";
    if (preview) {
        const verified = preview.status === "verified";
        const failed = preview.status === "failed";
        const verify = preview.verification || {};
        previewCard = `<div class="preview-result"><div class="preview-meta"><div><span class="pill ${verified ? "active" : failed ? "warning" : ""}">${escapeHTML(preview.status.toUpperCase())}</span><strong>${escapeHTML(preview.brand_key)} v${escapeHTML(preview.brand_version)}</strong><small>Source production ${escapeHTML(preview.production_id)} · workflow attempt ${escapeHTML(preview.workflow_attempt)}</small></div><div class="preview-facts">${verify.duration_seconds ? `<span>${escapeHTML(Number(verify.duration_seconds).toFixed(2))}s</span>` : ""}${verify.width && verify.height ? `<span>${escapeHTML(verify.width)}×${escapeHTML(verify.height)}</span>` : ""}</div></div>${failed ? `<p class="error-text">${escapeHTML(preview.error || "Preview render failed")}</p>` : ""}${verified && state.previewUrl ? `<video id="brand-preview-video" controls playsinline preload="metadata" src="${escapeHTML(state.previewUrl)}"></video><div class="accept-row"><p>Inspect placement, caption clearance, animation rhythm and mobile-safe composition before activation.</p><button class="button secondary" data-activate-brand="${escapeHTML(preview.brand_version)}">Activate accepted brand v${escapeHTML(preview.brand_version)}</button></div>` : verified ? '<div class="empty">Verified. Loading private preview media…</div>' : failed ? "" : '<div class="empty">Renderer is working. This panel will update automatically.</div>'}</div>`;
    }

    $("brand-lab").innerHTML = `${activeCard}${candidateCards ? `<div class="candidate-list">${candidateCards}</div>` : ""}${workflow}${previewCard}`;
}
function previewCueForVersion(version) {
    const brand = state.brands.find((row) => Number(row.version) === Number(version));
    const assets = brand?.contract?.visual?.reaction_pack?.assets;
    if (!assets || typeof assets !== "object") return null;
    const assetKey = Object.keys(assets)[0];
    if (!assetKey) return null;
    return {
        id: `editing-preview-${assetKey}`,
        asset_key: assetKey,
        line_ref: 0,
        offset_seconds: 0.2,
        duration_seconds: 1.0,
        anchor: "bottom_right",
        animation: "pop_bounce",
        scale: 0.22,
    };
}
async function loadPreviewMedia(preview, epoch) {
    const blob = await apiBlob(channelPath(`/brand-previews/${encodeURIComponent(preview.id)}/media`));
    if (epoch !== state.epoch) return;
    clearPreviewUrl();
    state.previewUrl = URL.createObjectURL(blob);
    renderBrandLab();
}
async function watchPreview(previewId, epoch) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
        const preview = await api(channelPath(`/brand-previews/${encodeURIComponent(previewId)}`));
        if (epoch !== state.epoch) return;
        state.preview = preview;
        renderBrandLab();
        if (preview.status === "verified") {
            await loadPreviewMedia(preview, epoch);
            if (epoch === state.epoch) message("Brand preview verified. Inspect the video before activation.");
            return;
        }
        if (preview.status === "failed") {
            message(preview.error || "Brand preview failed.", true);
            return;
        }
        await delay(2000);
    }
    if (epoch === state.epoch) message("Preview is still processing. Refresh to check it again.", true);
}
async function loadChannel() {
    const epoch = ++state.epoch;
    clearPreviewUrl();
    state.channel = $("channel").value;
    state.episodes = []; state.blueprints = []; state.attempts = new Map();
    state.brands = []; state.candidates = []; state.productions = []; state.preview = null;
    if (!state.channel) { renderBlueprints(); renderEpisodes(); renderPerformance(null); renderBrandLab(); return; }
    message("Loading channel production…");
    try {
        const [blueprints, episodes, performance, brands, candidates, productions] = await Promise.all([
            api(channelPath("/edit-blueprints")),
            api(`/v1/short-episodes?channel_profile_id=${encodeURIComponent(state.channel)}&limit=100`),
            api(channelPath("/edit-blueprints/performance/latest")),
            api(channelPath("/brands")),
            api(channelPath("/brand-candidates")),
            api(`/v1/productions?channel_profile_id=${encodeURIComponent(state.channel)}&limit=100`),
        ]);
        if (epoch !== state.epoch) return;
        state.blueprints = blueprints;
        state.episodes = episodes;
        state.brands = brands;
        state.candidates = candidates;
        state.productions = productions;
        renderBlueprints(); renderEpisodes(); renderPerformance(performance); renderBrandLab();
        const attempts = await Promise.allSettled(episodes.map((row) => api(`/v1/short-episodes/${encodeURIComponent(row.id)}/render-attempts`)));
        if (epoch !== state.epoch) return;
        attempts.forEach((result, index) => { if (result.status === "fulfilled" && result.value.length) state.attempts.set(episodes[index].id, result.value.at(-1)); });
        renderEpisodes();
        const failed = attempts.filter((result) => result.status === "rejected").length;
        message(failed ? `Channel loaded. Render status unavailable for ${failed} episode(s).` : "Channel data is up to date.", Boolean(failed));
    } catch (error) { if (epoch === state.epoch) message(error.message, true); }
}
async function connect(event) {
    event.preventDefault(); state.token = $("token").value.trim(); $("token").value = "";
    message("Connecting…");
    try {
        const channels = await api("/v1/channels");
        $("channel").innerHTML = '<option value="">Select a channel</option>' + channels.map((row) => `<option value="${escapeHTML(row.id)}">${escapeHTML(row.profile_metadata?.channel_title || row.profile_metadata?.name || row.id)} · ${escapeHTML(row.status)}</option>`).join("");
        $("channel").disabled = false; $("refresh").disabled = false;
        $("connection").textContent = "CONNECTED"; $("connection").classList.add("online");
        if (channels.length) { $("channel").value = channels[0].id; await loadChannel(); }
        else message("Connected. Create a channel through the control API to begin.");
    } catch (error) { $("connection").textContent = "OFFLINE"; $("connection").classList.remove("online"); message(error.message, true); }
}
async function action(event) {
    const blueprintActivate = event.target.closest("[data-blueprint-activate]");
    const recover = event.target.closest("[data-recover]");
    const details = event.target.closest("[data-attempts]");
    const stageBrand = event.target.closest("[data-stage-brand]");
    const renderPreview = event.target.closest("[data-render-preview]");
    const activateBrand = event.target.closest("[data-activate-brand]");
    if (!blueprintActivate && !recover && !details && !stageBrand && !renderPreview && !activateBrand) return;
    if (details) {
        const attempt = state.attempts.get(details.dataset.attempts);
        message(attempt ? `Render attempt ${attempt.attempt_number}: ${attempt.status}${attempt.error ? ` · ${attempt.error}` : ""}` : "No render attempts recorded for this episode.");
        return;
    }
    const button = blueprintActivate || recover || stageBrand || renderPreview || activateBrand;
    button.disabled = true;
    try {
        if (blueprintActivate) {
            await api(channelPath(`/edit-blueprints/${encodeURIComponent(blueprintActivate.dataset.blueprintActivate)}/${encodeURIComponent(blueprintActivate.dataset.version)}/activate`), { method: "POST", body: JSON.stringify({ actor: "editing-control-center", set_default: true }) });
            await loadChannel(); message("Blueprint activated as this channel’s default.");
        } else if (recover) {
            const result = await api(`/v1/short-episodes/${encodeURIComponent(recover.dataset.recover)}/render/recover`, { method: "POST", body: JSON.stringify({ actor: "editing-control-center", note: "Operator recovery from Editing Control Center" }) });
            await loadChannel(); message(`Render recovery started as a new episode generation (${result.child_source_id}).`);
        } else if (stageBrand) {
            const candidate = state.candidates.find((row) => Number(row.version) === Number(stageBrand.dataset.stageBrand));
            if (!candidate) throw new Error("Brand candidate is no longer available.");
            await api(channelPath("/brands"), {
                method: "POST",
                body: JSON.stringify({
                    contract: candidate.contract,
                    actor: "editing-control-center",
                    hypothesis: "Visual acceptance against frozen channel production before activation",
                }),
            });
            await loadChannel(); message(`Brand v${candidate.version} staged. The live channel is unchanged.`);
        } else if (renderPreview) {
            const version = Number($("preview-brand-version").value);
            const productionId = $("preview-production").value;
            const reactionCue = previewCueForVersion(version);
            state.preview = await api(channelPath(`/brands/${encodeURIComponent(version)}/previews`), {
                method: "POST",
                body: JSON.stringify({ production_id: productionId, reaction_cue: reactionCue }),
            });
            renderBrandLab();
            message("Brand preview queued. Production and publication lineage remain untouched.");
            const epoch = state.epoch;
            if (state.preview.status === "verified") await loadPreviewMedia(state.preview, epoch);
            else void watchPreview(state.preview.id, epoch);
        } else if (activateBrand) {
            const version = Number(activateBrand.dataset.activateBrand);
            if (state.preview?.status !== "verified" || Number(state.preview.brand_version) !== version) {
                throw new Error("A verified preview is required in this control-center session before activation.");
            }
            await api(channelPath(`/brands/${encodeURIComponent(version)}/activate`), {
                method: "POST",
                body: JSON.stringify({ actor: "editing-control-center" }),
            });
            await loadChannel(); message(`Brand v${version} activated. New productions will freeze the new identity.`);
        }
    } catch (error) { message(error.message, true); button.disabled = false; }
}
$("connect-form").addEventListener("submit", connect);
$("channel").addEventListener("change", loadChannel);
$("refresh").addEventListener("click", loadChannel);
$("filter").addEventListener("change", renderEpisodes);
$("blueprints").addEventListener("click", action);
$("episodes").addEventListener("click", action);
$("brand-lab").addEventListener("click", action);
