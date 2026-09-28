/* All state comes from the authenticated, same-origin Katcha control plane. */
const state = {
    token: "",
    channel: "",
    episodes: [],
    blueprints: [],
    templates: [],
    editingBlueprint: null,
    editorMode: null,
    editorBaseContract: null,
    editorSuggestedKey: "",
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
function rememberWorkspace() {
    if (state.token) sessionStorage.setItem("katcha.controlToken", state.token);
    if (state.channel) sessionStorage.setItem("katcha.channel", state.channel);
}
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
    const status = String(row.status || "").toLowerCase();
    if (
        attempt?.status === "dead_letter"
        || row.error
        || /(fail|error|dead_letter|blocked)/.test(status)
    ) return "attention";
    if (["published", "completed", "rejected", "cancelled"].includes(status)) return "done";
    return "active";
}
function blueprintName(row) {
    const saved = row?.blueprint_metadata?.display_name;
    if (saved) return saved;
    return String(row?.blueprint_key || "Recipe")
        .replaceAll("_", " ")
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
function blueprintDescription(row) {
    return row?.blueprint_metadata?.description || "";
}
function blueprintSummary(contract = {}) {
    const layout = contract.source_layout?.mode === "header_panel" ? "Header + video" : "Full-frame video";
    const narration = {
        persona_voice: "Persona voice",
        explanatory_voice: "Explanatory voice",
        text_only: "Text only",
        source_only: "Source audio only",
    }[contract.narration?.mode] || "Narration not set";
    const audio = {
        retain: "Source audio kept",
        duck: "Source audio ducks",
        mute: "Source audio muted",
    }[contract.narration?.source_audio_policy] || "Audio not set";
    const voiceMode = ["persona_voice", "explanatory_voice"].includes(contract.narration?.mode);
    const finalDetail = voiceMode
        ? (contract.narration?.captions_enabled ? "Captions on" : "Captions off")
        : (contract.quality?.max_duration_seconds ? `${contract.quality.max_duration_seconds}s max` : "Length not set");
    return [layout, narration, audio, finalDetail];
}
function blueprintFamilies() {
    const families = new Map();
    for (const row of state.blueprints) {
        const rows = families.get(row.blueprint_key) || [];
        rows.push(row);
        families.set(row.blueprint_key, rows);
    }
    for (const rows of families.values()) rows.sort((a, b) => Number(b.version) - Number(a.version));
    return families;
}
function findBlueprint(key, version) {
    return state.blueprints.find(
        (row) => row.blueprint_key === key && Number(row.version) === Number(version),
    );
}
function renderBlueprints() {
    const families = blueprintFamilies();
    $("count-blueprints").textContent = families.size;
    $("new-blueprint").disabled = !state.channel;
    if (!families.size) {
        $("blueprints").innerHTML = '<div class="empty">No editing recipes yet. Create one from a starter template.</div>';
        return;
    }
    $("blueprints").innerHTML = [...families.entries()].map(([key, versions]) => {
        const active = versions.find((row) => row.is_active) || versions[0];
        const summary = blueprintSummary(active.contract);
        const history = versions.map((row) => `
            <div class="recipe-history-row">
                <div><strong>v${escapeHTML(row.version)}</strong><small>${escapeHTML(date(row.created_at))}</small></div>
                <div class="item-actions">
                    ${row.is_active ? '<span class="pill active">ACTIVE</span>' : ""}
                    ${row.is_default ? '<span class="pill default-pill">DEFAULT</span>' : ""}
                    <button class="mini" type="button" data-blueprint-open="${escapeHTML(key)}" data-version="${escapeHTML(row.version)}">Open</button>
                    ${row.is_active ? "" : `<button class="mini" type="button" data-blueprint-restore="${escapeHTML(key)}" data-version="${escapeHTML(row.version)}">Restore</button>`}
                </div>
            </div>
        `).join("");
        return `<article class="recipe-card ${active.is_default ? "is-default" : ""}">
            <div class="recipe-card-head">
                <div>
                    <div class="recipe-status">${active.is_default ? "DEFAULT RECIPE" : "ACTIVE RECIPE"} · v${escapeHTML(active.version)}</div>
                    <h3>${escapeHTML(blueprintName(active))}</h3>
                    <p>${escapeHTML(blueprintDescription(active) || "Channel editing recipe")}</p>
                </div>
                <div class="item-actions">
                    <button class="mini recipe-open" type="button" data-blueprint-open="${escapeHTML(key)}" data-version="${escapeHTML(active.version)}">Edit recipe</button>
                    ${active.is_default ? "" : `<button class="mini" type="button" data-blueprint-default="${escapeHTML(key)}" data-version="${escapeHTML(active.version)}">Make default</button>`}
                </div>
            </div>
            <div class="recipe-summary">${summary.map((value) => `<span>${escapeHTML(value)}</span>`).join("")}</div>
            <details class="recipe-history"><summary>Version history · ${versions.length}</summary>${history}</details>
        </article>`;
    }).join("");
}
function setBlueprintEditorSubmitting(submitting) {
    const submit = $("blueprint-editor").querySelector('button[type="submit"]');
    if (submit) submit.disabled = submitting;
}
function closeBlueprintEditor() {
    state.editingBlueprint = null;
    state.editorMode = null;
    state.editorBaseContract = null;
    state.editorSuggestedKey = "";
    setBlueprintEditorSubmitting(false);
    $("blueprint-editor-panel").hidden = true;
}
function templateForKey(key) {
    return state.templates.find((row) => row.key === key);
}
function uniqueRecipeKey(base) {
    const used = new Set(state.blueprints.map((row) => row.blueprint_key));
    let candidate = `${base}_custom`;
    let index = 2;
    while (used.has(candidate)) {
        candidate = `${base}_custom_${index}`;
        index += 1;
    }
    return candidate;
}
function setEditorContract(contract) {
    const copy = structuredClone(contract);
    state.editorBaseContract = copy;
    $("bp-layout").value = copy.source_layout?.mode || "full_frame";
    $("bp-fit").value = copy.source_layout?.fit || "contain";
    $("bp-background").value = copy.source_layout?.background_mode || "solid";
    $("bp-header-height").value = copy.source_layout?.header_height_px ?? 0;
    $("bp-narration-mode").value = copy.narration?.mode || "source_only";
    $("bp-narration-required").checked = Boolean(copy.narration?.required);
    $("bp-captions").checked = Boolean(copy.narration?.captions_enabled);
    $("bp-audio-policy").value = copy.narration?.source_audio_policy || "retain";
    $("bp-source-volume").value = copy.narration?.source_audio_volume ?? 0.45;
    $("bp-duck-volume").value = copy.narration?.narration_duck_volume ?? 0.16;
    $("bp-header-required").checked = Boolean(copy.header?.required);
    $("bp-header-max").value = copy.header?.max_chars ?? 0;
    $("bp-header-bg").value = copy.header?.background || "#000000";
    $("bp-header-fg").value = copy.header?.foreground || "#FFFFFF";
    $("bp-header-font-size").value = copy.header?.font_size_px ?? 54;
    $("bp-header-font-weight").value = copy.header?.font_weight ?? 850;
    $("bp-header-padding").value = copy.header?.horizontal_padding_px ?? 56;
    $("bp-min-source").value = copy.quality?.min_source_seconds ?? 1;
    $("bp-max-duration").value = copy.quality?.max_duration_seconds ?? 60;
    $("bp-narration-ratio").value = Math.round((copy.quality?.max_narration_ratio ?? 0.55) * 100);
    syncBlueprintEditor();
}
function syncBlueprintEditor() {
    const voiceMode = ["persona_voice", "explanatory_voice"].includes($("bp-narration-mode").value);
    if (voiceMode) $("bp-layout").value = "full_frame";
    $("bp-layout").disabled = voiceMode;
    const headerMode = $("bp-layout").value === "header_panel";
    $("bp-voice-limit-note").hidden = !voiceMode;
    $("bp-header-group").hidden = !headerMode;
    $("bp-header-height-wrap").hidden = !headerMode;
    if (headerMode) {
        if (Number($("bp-header-height").value) <= 0) $("bp-header-height").value = 360;
        $("bp-header-required").checked = true;
        if (Number($("bp-header-max").value) <= 0) $("bp-header-max").value = 220;
    } else {
        $("bp-header-height").value = 0;
        $("bp-header-required").checked = false;
        $("bp-header-max").value = 0;
    }
    $("bp-narration-required").disabled = true;
    $("bp-narration-required").checked = voiceMode;
    $("bp-captions").disabled = !voiceMode;
    if (!voiceMode && $("bp-audio-policy").value === "duck") $("bp-audio-policy").value = "retain";
    $("bp-duck-volume").disabled = !voiceMode || $("bp-audio-policy").value !== "duck";
    $("bp-min-source").disabled = voiceMode;
    $("bp-max-duration").disabled = voiceMode;
    $("bp-narration-ratio").disabled = true;
    if (!voiceMode) {
        $("bp-captions").checked = false;
        $("bp-narration-ratio").value = 0;
    } else if (Number($("bp-narration-ratio").value) === 0) {
        $("bp-narration-ratio").value = 48;
    }
    $("bp-source-volume-value").value = `${Math.round(Number($("bp-source-volume").value) * 100)}%`;
    $("bp-duck-volume-value").value = `${Math.round(Number($("bp-duck-volume").value) * 100)}%`;
}
function openBlueprintEditor(row) {
    if (!row) return;
    setBlueprintEditorSubmitting(false);
    state.editingBlueprint = row;
    state.editorMode = "edit";
    $("blueprint-editor-title").textContent = `Edit ${blueprintName(row)}`;
    $("blueprint-editor-context").textContent = `You are editing from v${row.version}. Save creates a new version; videos already using v${row.version} keep it unchanged.`;
    $("bp-template-wrap").hidden = true;
    $("bp-key").disabled = true;
    $("bp-key").value = row.blueprint_key;
    $("bp-name").value = blueprintName(row);
    $("bp-description").value = blueprintDescription(row);
    $("bp-set-default").checked = Boolean(row.is_default);
    setEditorContract(row.contract);
    $("blueprint-editor-panel").hidden = false;
    $("blueprint-editor-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}
function applyNewRecipeTemplate(template) {
    if (!template) return;
    const previousSuggestion = state.editorSuggestedKey;
    const nextSuggestion = uniqueRecipeKey(template.key);
    if (!$("bp-key").value || $("bp-key").value === previousSuggestion) {
        $("bp-key").value = nextSuggestion;
    }
    state.editorSuggestedKey = nextSuggestion;
    $("bp-name").value = `Custom ${template.display_name}`;
    $("bp-description").value = template.description;
    $("bp-set-default").checked = false;
    setEditorContract(template.contract);
}
function openNewBlueprintEditor() {
    if (!state.channel || !state.templates.length) {
        message("Connect a channel before creating an editing recipe.", true);
        return;
    }
    setBlueprintEditorSubmitting(false);
    state.editingBlueprint = null;
    state.editorMode = "new";
    $("blueprint-editor-title").textContent = "Create editing recipe";
    $("blueprint-editor-context").textContent = "Start from a proven template, then adjust the editing behavior. The new recipe becomes an active option for future videos.";
    $("bp-template-wrap").hidden = false;
    $("bp-key").disabled = false;
    $("bp-template").innerHTML = state.templates.map((row) => `<option value="${escapeHTML(row.key)}">${escapeHTML(row.display_name)} — ${escapeHTML(row.description)}</option>`).join("");
    state.editorSuggestedKey = "";
    $("bp-key").value = "";
    applyNewRecipeTemplate(state.templates[0]);
    $("blueprint-editor-panel").hidden = false;
    $("blueprint-editor-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}
function editorContract() {
    const key = $("bp-key").value.trim();
    if (!/^[a-z0-9_]+$/.test(key)) {
        throw new Error("Recipe ID must use lowercase letters, numbers, and underscores only.");
    }
    const headerMode = $("bp-layout").value === "header_panel";
    const voiceMode = ["persona_voice", "explanatory_voice"].includes($("bp-narration-mode").value);
    return {
        key,
        version: state.editorBaseContract?.version || "1.0.0",
        composition: "blueprint_video",
        source_layout: {
            mode: $("bp-layout").value,
            fit: $("bp-fit").value,
            background_mode: $("bp-background").value,
            header_height_px: headerMode ? Number($("bp-header-height").value) : 0,
        },
        narration: {
            mode: $("bp-narration-mode").value,
            required: voiceMode && $("bp-narration-required").checked,
            captions_enabled: voiceMode && $("bp-captions").checked,
            source_audio_policy: $("bp-audio-policy").value,
            source_audio_volume: Number($("bp-source-volume").value),
            narration_duck_volume: Number($("bp-duck-volume").value),
        },
        header: {
            required: headerMode && $("bp-header-required").checked,
            max_chars: headerMode ? Number($("bp-header-max").value) : 0,
            background: $("bp-header-bg").value,
            foreground: $("bp-header-fg").value,
            font_size_px: Number($("bp-header-font-size").value),
            font_weight: Number($("bp-header-font-weight").value),
            horizontal_padding_px: Number($("bp-header-padding").value),
        },
        transition: state.editorBaseContract?.transition || (voiceMode ? "punch_cut" : "cut"),
        quality: {
            min_source_seconds: Number($("bp-min-source").value),
            max_duration_seconds: Number($("bp-max-duration").value),
            max_narration_ratio: Number($("bp-narration-ratio").value) / 100,
        },
        ai_guidance: state.editorBaseContract?.ai_guidance || {
            instruction_strength: "balanced",
            preserve_clip_order: true,
            prefer_native_moments: true,
            always_rules: [],
            never_rules: [],
            operator_notes: "",
        },
    };
}
async function saveBlueprintEditor(event) {
    event.preventDefault();
    const submit = event.submitter;
    setBlueprintEditorSubmitting(true);
    try {
        const contract = editorContract();
        const created = await api(channelPath("/edit-blueprints"), {
            method: "POST",
            body: JSON.stringify({
                contract,
                actor: "editing-control-center",
                set_default: $("bp-set-default").checked,
                display_name: $("bp-name").value.trim(),
                description: $("bp-description").value.trim(),
            }),
        });
        closeBlueprintEditor();
        await loadChannel();
        message(`${blueprintName(created)} v${created.version} saved. Existing videos keep their previous recipe version.`);
    } catch (error) {
        message(error.message, true);
        setBlueprintEditorSubmitting(false);
    }
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
        const stateLabel = type === "attention"
            ? (recoverable ? "RENDER FAILED" : "NEEDS ATTENTION")
            : type === "done" ? "COMPLETE" : "IN PRODUCTION";
        return `<article class="item episode-item status-${type}" data-episode-status="${type}"><div><div class="episode-state"><span class="state-dot" aria-hidden="true"></span>${escapeHTML(stateLabel)}</div><h3>${escapeHTML(row.premise)}</h3><p>Stage: ${escapeHTML(row.stage)} · ${escapeHTML(row.status)} · Updated ${escapeHTML(date(row.updated_at))}</p><p class="meta">${escapeHTML(row.edit_blueprint_key || "Channel default blueprint")}${row.edit_blueprint_version ? ` · v${escapeHTML(row.edit_blueprint_version)}` : ""} · Generation ${escapeHTML(row.generation)}</p>${row.error || attempt?.error ? `<p class="error-text">${escapeHTML(attempt?.error || row.error)}</p>` : ""}</div><div class="item-actions"><span class="pill state-pill ${type}">${escapeHTML(stateLabel)}</span><span class="pill">${escapeHTML(String(row.status || "unknown").replaceAll("_", " ").toUpperCase())}</span>${recoverable ? `<button class="mini" data-recover="${escapeHTML(row.id)}">Recover render</button>` : ""}<a class="mini studio-launch" data-studio="${escapeHTML(row.id)}" href="/studio?episode=${encodeURIComponent(row.id)}&channel=${encodeURIComponent(state.channel)}">Open Clip Studio</a></div></article>`;
    }).join("") : `<div class="empty">${filter === "all" ? "No episodes in this channel yet." : "No episodes match this filter."}</div>`;
}
function previewableSources() {
    const ranked = state.episodes
        .filter((row) =>
            row.render_manifest?.version === "ranked-episode-render-v1"
            && Array.isArray(row.render_manifest?.overlays)
            && row.render_manifest.overlays.length > 0
        )
        .map((row) => ({
            kind: "short_episode",
            id: row.id,
            label: row.premise || `Ranked episode ${String(row.id).slice(0, 8)}`,
            status: row.status,
            updated_at: row.updated_at,
            manifest: row.render_manifest,
        }));
    const standalone = state.productions
        .filter((row) =>
            row.render_manifest?.version === "short-render-v1"
            && Array.isArray(row.render_manifest?.overlays)
            && row.render_manifest.overlays.length > 0
        )
        .map((row) => ({
            kind: "production",
            id: row.id,
            label: row.render_manifest?.title_angle || `Standalone ${String(row.id).slice(0, 8)}`,
            status: row.status,
            updated_at: row.updated_at,
            manifest: row.render_manifest,
        }));
    return [...ranked, ...standalone];
}
function renderBrandLab() {
    const active = state.brands.find((row) => row.is_active);
    const staged = state.brands.filter((row) => !row.is_active);
    const stagedVersions = new Set(state.brands.map((row) => row.version));
    const availableCandidates = state.candidates.filter((row) => !stagedVersions.has(row.version));
    const sources = previewableSources();
    const preview = state.preview;

    const activeCard = active
        ? `<div class="brand-current"><span class="pill active">LIVE</span><div><strong>${escapeHTML(active.brand_key)} v${escapeHTML(active.version)}</strong><small>Frozen productions keep the version they were created with.</small></div></div>`
        : '<div class="empty">No active brand version is configured.</div>';

    const candidateCards = availableCandidates.map((row) => `<article class="brand-candidate"><div><strong>${escapeHTML(row.brand_key)} v${escapeHTML(row.version)}</strong><small>Built-in staged candidate · does not change the live channel</small></div><button class="mini" data-stage-brand="${escapeHTML(row.version)}">Stage candidate</button></article>`).join("");

    const stagedOptions = staged.map((row) => `<option value="${escapeHTML(row.version)}">${escapeHTML(row.brand_key)} v${escapeHTML(row.version)}</option>`).join("");
    const sourceOptions = sources.map((row) => {
        const type = row.kind === "short_episode" ? "Ranked episode" : "Standalone Short";
        return `<option value="${escapeHTML(`${row.kind}:${row.id}`)}">${escapeHTML(type)} · ${escapeHTML(row.label)} · ${escapeHTML(row.status)} · ${escapeHTML(date(row.updated_at))}</option>`;
    }).join("");

    let workflow = "";
    if (staged.length && sources.length) {
        workflow = `<div class="preview-controls"><label>STAGED BRAND<select id="preview-brand-version">${stagedOptions}</select></label><label>FROZEN MEDIA SOURCE<select id="preview-source">${sourceOptions}</select></label><button class="button primary" data-render-preview>Render preview ↗</button></div>`;
    } else if (!staged.length) {
        workflow = '<div class="empty">Stage a brand candidate before rendering a preview.</div>';
    } else {
        workflow = '<div class="empty">No ranked episode or standalone Short with a frozen narration timeline is available yet.</div>';
    }

    let previewCard = "";
    if (preview) {
        const verified = preview.status === "verified";
        const failed = preview.status === "failed";
        const verify = preview.verification || {};
        previewCard = `<div class="preview-result"><div class="preview-meta"><div><span class="pill ${verified ? "active" : failed ? "warning" : ""}">${escapeHTML(preview.status.toUpperCase())}</span><strong>${escapeHTML(preview.brand_key)} v${escapeHTML(preview.brand_version)}</strong><small>Source ${escapeHTML(preview.short_episode_id ? `ranked episode ${preview.short_episode_id}` : `production ${preview.production_id}`)} · workflow attempt ${escapeHTML(preview.workflow_attempt)}</small></div><div class="preview-facts">${verify.duration_seconds ? `<span>${escapeHTML(Number(verify.duration_seconds).toFixed(2))}s</span>` : ""}${verify.width && verify.height ? `<span>${escapeHTML(verify.width)}×${escapeHTML(verify.height)}</span>` : ""}</div></div>${failed ? `<p class="error-text">${escapeHTML(preview.error || "Preview render failed")}</p>` : ""}${verified && state.previewUrl ? `<video id="brand-preview-video" controls playsinline preload="metadata" src="${escapeHTML(state.previewUrl)}"></video><div class="accept-row"><p>Inspect placement, caption clearance, animation rhythm and mobile-safe composition before activation.</p><button class="button secondary" data-activate-brand="${escapeHTML(preview.brand_version)}">Activate accepted brand v${escapeHTML(preview.brand_version)}</button></div>` : verified ? '<div class="empty">Verified. Loading private preview media…</div>' : failed ? "" : '<div class="empty">Renderer is working. This panel will update automatically.</div>'}</div>`;
    }

    $("brand-lab").innerHTML = `${activeCard}${candidateCards ? `<div class="candidate-list">${candidateCards}</div>` : ""}${workflow}${previewCard}`;
}
function previewCueForVersion(version, source) {
    const brand = state.brands.find((row) => Number(row.version) === Number(version));
    const assets = brand?.contract?.visual?.reaction_pack?.assets;
    if (!assets || typeof assets !== "object") return null;
    const assetKey = Object.keys(assets)[0];
    if (!assetKey) return null;
    const firstOverlay = source?.manifest?.overlays?.[0];
    const lineRef = source?.kind === "short_episode"
        ? Number(firstOverlay?.sequence)
        : 0;
    if (!Number.isInteger(lineRef) || lineRef < 0) {
        throw new Error("Selected preview source has no valid narration cue reference.");
    }
    return {
        id: `editing-preview-${assetKey}`,
        asset_key: assetKey,
        line_ref: lineRef,
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
    rememberWorkspace();
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
    rememberWorkspace();
    message("Connecting…");
    try {
        const [channels, templates] = await Promise.all([
            api("/v1/channels"),
            api("/v1/channels/edit-blueprint-templates"),
        ]);
        state.templates = templates;
        $("channel").innerHTML = '<option value="">Select a channel</option>' + channels.map((row) => `<option value="${escapeHTML(row.id)}">${escapeHTML(row.profile_metadata?.channel_title || row.profile_metadata?.name || row.id)} · ${escapeHTML(row.status)}</option>`).join("");
        $("channel").disabled = false; $("refresh").disabled = false;
        $("connection").textContent = "CONNECTED"; $("connection").classList.add("online");
        if (channels.length) { $("channel").value = channels[0].id; await loadChannel(); }
        else message("Connected. Create a channel through the control API to begin.");
    } catch (error) { $("connection").textContent = "OFFLINE"; $("connection").classList.remove("online"); message(error.message, true); }
}
async function action(event) {
    const blueprintOpen = event.target.closest("[data-blueprint-open]");
    const blueprintDefault = event.target.closest("[data-blueprint-default]");
    const blueprintRestore = event.target.closest("[data-blueprint-restore]");
    const recover = event.target.closest("[data-recover]");
    const studio = event.target.closest("[data-studio]");
    const stageBrand = event.target.closest("[data-stage-brand]");
    const renderPreview = event.target.closest("[data-render-preview]");
    const activateBrand = event.target.closest("[data-activate-brand]");
    if (!blueprintOpen && !blueprintDefault && !blueprintRestore && !recover && !studio && !stageBrand && !renderPreview && !activateBrand) return;
    if (blueprintOpen) {
        const row = findBlueprint(blueprintOpen.dataset.blueprintOpen, blueprintOpen.dataset.version);
        if (!row) {
            message("That recipe version is no longer available. Refresh and try again.", true);
            return;
        }
        openBlueprintEditor(row);
        return;
    }
    if (studio) {
        rememberWorkspace();
        return;
    }
    const button = blueprintDefault || blueprintRestore || recover || stageBrand || renderPreview || activateBrand;
    button.disabled = true;
    try {
        if (blueprintDefault || blueprintRestore) {
            const target = blueprintDefault || blueprintRestore;
            const key = blueprintDefault
                ? blueprintDefault.dataset.blueprintDefault
                : blueprintRestore.dataset.blueprintRestore;
            const version = target.dataset.version;
            await api(channelPath(`/edit-blueprints/${encodeURIComponent(key)}/${encodeURIComponent(version)}/activate`), {
                method: "POST",
                body: JSON.stringify({
                    actor: "editing-control-center",
                    set_default: Boolean(blueprintDefault),
                }),
            });
            await loadChannel();
            message(blueprintDefault
                ? "Recipe is now the channel default for new videos."
                : `Recipe v${version} restored. Existing videos remain unchanged.`);
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
            const [sourceKind, sourceId] = $("preview-source").value.split(":", 2);
            const source = previewableSources().find(
                (row) => row.kind === sourceKind && String(row.id) === sourceId
            );
            if (!source) throw new Error("Selected preview source is no longer available.");
            const reactionCue = previewCueForVersion(version, source);
            const sourcePayload = sourceKind === "short_episode"
                ? { short_episode_id: sourceId }
                : { production_id: sourceId };
            state.preview = await api(channelPath(`/brands/${encodeURIComponent(version)}/previews`), {
                method: "POST",
                body: JSON.stringify({ ...sourcePayload, reaction_cue: reactionCue }),
            });
            renderBrandLab();
            const epoch = state.epoch;
            if (state.preview.status === "verified") {
                await loadPreviewMedia(state.preview, epoch);
                if (epoch === state.epoch) message("Brand preview verified. Inspect the video before activation.");
            } else {
                message("Brand preview queued. Production and publication lineage remain untouched.");
                void watchPreview(state.preview.id, epoch);
            }
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
$("channel").addEventListener("change", () => {
    closeBlueprintEditor();
    void loadChannel();
});
$("refresh").addEventListener("click", loadChannel);
$("new-blueprint").addEventListener("click", openNewBlueprintEditor);
$("close-blueprint-editor").addEventListener("click", closeBlueprintEditor);
$("cancel-blueprint-editor").addEventListener("click", closeBlueprintEditor);
$("blueprint-editor").addEventListener("submit", saveBlueprintEditor);
$("bp-template").addEventListener("change", () => applyNewRecipeTemplate(templateForKey($("bp-template").value)));
for (const id of ["bp-layout", "bp-narration-mode", "bp-audio-policy", "bp-source-volume", "bp-duck-volume"]) {
    $(id).addEventListener("input", syncBlueprintEditor);
    $(id).addEventListener("change", syncBlueprintEditor);
}
$("filter").addEventListener("change", renderEpisodes);
document.querySelector(".stats").addEventListener("click", (event) => {
    const target = event.target.closest("[data-summary-filter]");
    if (!target) return;
    $("filter").value = target.dataset.summaryFilter;
    renderEpisodes();
});
$("blueprints").addEventListener("click", action);
$("episodes").addEventListener("click", action);
$("brand-lab").addEventListener("click", action);
