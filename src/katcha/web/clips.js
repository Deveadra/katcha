const launchParams = new URLSearchParams(location.search);
const state = {
    token: sessionStorage.getItem("katcha.controlToken") || "",
    channels: [],
    selectedChannel: launchParams.get("channel") || sessionStorage.getItem("katcha.channel") || "",
    clips: [],
    total: 0,
    selectedId: null,
    linkedClip: launchParams.get("clip") || "",
    features: new Map(),
    sources: new Map(),
    bucket: "all",
    summary: null,
    previewUrl: null,
    offset: 0,
    limit: 80,
    searchTimer: null,
    purgeId: null,
    detailView: "overview",
};

const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
);
const date = (value) => value ? new Date(value).toLocaleString() : "—";
const number = (value, digits = 0) => Number.isFinite(Number(value))
    ? Number(value).toFixed(digits)
    : "—";

function message(value, error = false) {
    $("message").textContent = value;
    $("message").classList.toggle("error", error);
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
        let body;
        try {
            body = await response.json();
        } catch {}
        const error = new Error(
            typeof body?.detail === "string"
                ? body.detail
                : `Request failed (${response.status})`,
        );
        error.status = response.status;
        throw error;
    }
    return response.json();
}

async function apiBlob(path) {
    const response = await request(path);
    if (!response.ok) {
        let body;
        try {
            body = await response.json();
        } catch {}
        throw new Error(
            typeof body?.detail === "string"
                ? body.detail
                : `Media request failed (${response.status})`,
        );
    }
    return response.blob();
}

function clearPreview() {
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
}

function formatDuration(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return "—";
    if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    return `${mins}m ${String(secs).padStart(2, "0")}s`;
}

function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(
        Math.floor(Math.log(bytes) / Math.log(1024)),
        units.length - 1,
    );
    return `${(bytes / (1024 ** index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function currentChannel() {
    return state.channels.find((channel) => channel.id === state.selectedChannel) || null;
}

function channelName(channel) {
    const metadata = channel?.profile_metadata || {};
    return metadata.channel_title || metadata.name || metadata.channel_handle || "Channel";
}

function clipTitle(clip) {
    return clip.title || `${clip.platform || "Stored"} clip · ${clip.id.slice(0, 8)}`;
}

function askKatchaHref(clip, prompt) {
    const channelId =
        state.selectedChannel && state.selectedChannel !== "all"
            ? state.selectedChannel
            : (clip.channels || []).length === 1
              ? clip.channels[0].id
              : "";
    if (!channelId) return "";
    const params = new URLSearchParams({
        channel: channelId,
        resource_kind: "clip",
        resource_id: clip.id,
        prompt,
        focus: "chat",
    });
    return "/ai?" + params.toString();
}

function queryString(extra = {}) {
    const params = new URLSearchParams();
    const channelId = state.selectedChannel;
    if (channelId && channelId !== "all") {
        params.set("channel_profile_id", channelId);
    }
    if (state.linkedClip) params.set("clip_id", state.linkedClip);
    const search = $("search").value.trim();
    if (search) params.set("q", search);
    const status = $("status-filter").value;
    if (status !== "all") params.set("status", status);

    if (state.bucket === "hot") params.set("lifecycle_state", "hot");
    if (state.bucket === "archived") params.set("lifecycle_state", "archived");
    if (state.bucket === "purged") params.set("lifecycle_state", "purged");
    if (state.bucket === "failed") params.set("status", "failed");

    for (const [key, value] of Object.entries(extra)) {
        if (value !== undefined && value !== null) params.set(key, String(value));
    }
    return params.toString();
}

function renderCounts() {
    const summary = state.summary;
    if (!summary) return;
    $("count-all").textContent = summary.total;
    $("count-hot").textContent = summary.hot;
    $("count-archived").textContent = summary.archived;
    $("count-failed").textContent = summary.failed;
    $("count-purged").textContent = summary.purged;

    const saved = formatBytes(summary.purged_bytes);
    const hot = formatBytes(summary.hot_bytes);
    const archived = formatBytes(summary.archived_bytes);
    $("storage-health").innerHTML = `
        <span>STORAGE HEALTH</span>
        <strong>${escapeHTML(hot)} hot · ${escapeHTML(archived)} archived</strong>
        <small>${escapeHTML(saved)} of source media purged while metadata remains searchable</small>
    `;
}

function lifecycleChip(clip) {
    return `<span class="clip-chip ${escapeHTML(clip.lifecycle_state)}">${escapeHTML(clip.lifecycle_state.toUpperCase())}</span>`;
}

function renderList() {
    $("visible-count").textContent = `${state.total} clip${state.total === 1 ? "" : "s"}`;
    const shown = state.clips.length;
    $("library-range").textContent = state.total
        ? `Showing ${shown} of ${state.total} · newest first`
        : "No matches";

    document.querySelectorAll("[data-bucket]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.bucket === state.bucket);
    });

    const rows = state.clips.map((clip) => {
        const channelLabels = (clip.channels || []).map((channel) => channel.name).join(", ");
        const tags = (clip.tags || []).slice(0, 3).map(
            (tag) => `<span class="clip-chip">${escapeHTML(tag)}</span>`,
        ).join("");
        const selected = state.selectedId === clip.id ? "is-selected" : "";
        const lifeClass = clip.lifecycle_state === "archived"
            ? "is-archived"
            : clip.lifecycle_state === "purged"
                ? "is-purged"
                : "";
        return `<button class="clip-row ${selected} ${lifeClass}" type="button" data-clip-id="${escapeHTML(clip.id)}">
            <div class="clip-row-top">
                <div>
                    <h3>${escapeHTML(clipTitle(clip))}</h3>
                    <p>${escapeHTML(clip.creator || "Unknown creator")} · ${escapeHTML(clip.platform || "stored media")}</p>
                </div>
                ${clip.candidate_score != null ? `<span class="score-chip">${escapeHTML(number(clip.candidate_score, 0))}</span>` : ""}
            </div>
            <div class="clip-row-meta">
                ${lifecycleChip(clip)}
                <span class="clip-chip ${clip.status === "failed" ? "failed" : clip.status === "published" || clip.status === "measured" ? "published" : ""}">${escapeHTML(clip.status.replaceAll("_", " ").toUpperCase())}</span>
                <span class="clip-chip">${escapeHTML(formatDuration(clip.duration_seconds))}</span>
                <span class="clip-chip">${escapeHTML(formatBytes(clip.size_bytes))}</span>
                ${tags}
            </div>
            ${channelLabels ? `<p>${escapeHTML(channelLabels)}</p>` : ""}
        </button>`;
    });

    if (state.clips.length < state.total) {
        rows.push(`<button id="load-more" class="button secondary" type="button">Load more clips</button>`);
    }
    $("clip-list").innerHTML = rows.length
        ? rows.join("")
        : '<div class="empty">No clips match this channel and search.</div>';
}

function aiResult(features) {
    const ai = features?.ai_features || {};
    return ai.deep || ai.bulk || null;
}

function renderDetail(clip, features, sources) {
    clearPreview();
    const ai = aiResult(features);
    const analysisFailed = clip.analysis_status === "failed";
    const canAnalyze = !features &&
        clip.status !== "failed" &&
        clip.lifecycle_state === "hot" &&
        (!clip.analysis_status || analysisFailed);
    const canPreview = clip.lifecycle_state !== "purged";
    const channels = (clip.channels || []).map((channel) => channel.name).join(", ") || "Unassigned";
    const refs = clip.active_reference_count
        ? `${clip.active_reference_count} active production reference${clip.active_reference_count === 1 ? "" : "s"}`
        : clip.reference_count
            ? `${clip.reference_count} completed/historical reference${clip.reference_count === 1 ? "" : "s"}`
            : "No production references";

    const sourceLinks = sources.length ? sources.map((row) => `
        <div>
            ${/^https?:\/\//i.test(row.source_url) ? `<a class="source-link" href="${escapeHTML(row.source_url)}" target="_blank" rel="noopener">${escapeHTML(row.title || row.source_url)}</a>` : `<span>${escapeHTML(row.title || "Uploaded video file")}</span>`}
            <small>${escapeHTML(row.platform)}${row.creator ? ` · ${escapeHTML(row.creator)}` : ""} · ${escapeHTML(date(row.discovered_at))}</small>
        </div>
    `).join("") : '<span class="empty">No source lineage is attached.</span>';

    const aiScores = ai ? [
        ["Hook", ai.hook_score],
        ["Surprise", ai.surprise_score],
        ["Humor", ai.humor_score],
        ["Comments", ai.comment_potential],
        ["Rewatch", ai.rewatch_potential],
    ] : [];

    const metadata = clip.library_metadata || {};
    const tags = (clip.tags || []).join(", ");
    const embeddingStatus = clip.embedding_metadata?.status || "pending";
    const askHref = askKatchaHref(
        clip,
        "Explain this clip’s score, stored evidence, and whether it is strong enough to use.",
    );
    const askAction = askHref
        ? `<a class="mini" href="${escapeHTML(askHref)}">Ask Katcha ✦</a>`
        : "";

    const lifecycleActions = clip.lifecycle_state === "hot"
        ? `<button class="mini" type="button" data-archive="${escapeHTML(clip.id)}">Archive</button>
           <button class="mini danger" type="button" data-purge="${escapeHTML(clip.id)}">Delete media</button>`
        : clip.lifecycle_state === "archived"
            ? `<button class="mini" type="button" data-restore="${escapeHTML(clip.id)}">Restore</button>
               <button class="mini danger" type="button" data-purge="${escapeHTML(clip.id)}">Delete media</button>`
            : "";

    $("clip-detail").innerHTML = `
        <div class="clip-detail-head">
            <div>
                <div class="eyebrow">CLIP DETAIL</div>
                <h2>${escapeHTML(clipTitle(clip))}</h2>
                <p>${escapeHTML(clip.creator || "Unknown creator")} · ${escapeHTML(clip.platform || "stored media")} · ${escapeHTML(channels)}</p>
            </div>
            <div class="detail-actions">
                ${canPreview ? `<a class="mini" href="/content?channel=${encodeURIComponent(state.selectedChannel)}&clip=${encodeURIComponent(clip.id)}&title=${encodeURIComponent(clipTitle(clip))}">Create video</a>` : ""}

                ${lifecycleChip(clip)}
                ${canPreview ? `<button class="mini" type="button" data-load-preview="${escapeHTML(clip.id)}">Load preview</button>` : ""}
                ${canAnalyze ? `<button class="mini ${analysisFailed ? "danger" : ""}" type="button" data-analyze="${escapeHTML(clip.id)}" data-force-retry="${analysisFailed ? "true" : "false"}">${analysisFailed ? "Retry analysis" : "Analyze"}</button>` : ""}
                ${askAction}
                ${lifecycleActions}
            </div>
        </div>

        <div class="lifecycle-banner">
            <div>
                <strong>${escapeHTML(refs)}</strong>
                <small>${clip.lifecycle_state === "purged" ? "Media bytes removed; searchable record retained." : clip.lifecycle_state === "archived" ? "Source media is outside the hot tier and can be restored." : "Source media is immediately available to production."}</small>
            </div>
            <span class="clip-chip ${escapeHTML(clip.lifecycle_state)}">${escapeHTML(clip.lifecycle_state.toUpperCase())}</span>
        </div>

        ${analysisFailed ? `<div class="empty error analysis-failure"><strong>Latest analysis failed</strong><br>${escapeHTML(clip.analysis_error || "No failure detail was recorded.")}</div>` : ""}

        <div id="clip-preview" class="clip-preview">
            <div class="empty">${clip.lifecycle_state === "purged" ? "This clip’s source media was purged. Metadata and analysis remain below." : "Video is loaded only when requested so browsing stays fast."}</div>
        </div>

        <nav class="clip-inspector-tabs" aria-label="Clip detail views" role="tablist">
            <button type="button" role="tab" data-clip-detail-tab="overview" aria-selected="true">Overview</button>
            <button type="button" role="tab" data-clip-detail-tab="analysis" aria-selected="false">Analysis</button>
            <button type="button" role="tab" data-clip-detail-tab="lineage" aria-selected="false">Lineage</button>
            <button type="button" role="tab" data-clip-detail-tab="metadata" aria-selected="false">Metadata</button>
        </nav>

        <div class="detail-grid">
            <section class="detail-block" data-clip-detail-view="overview">
                <h3>STORED MEDIA</h3>
                <dl class="detail-kv">
                    <dt>Clip ID</dt><dd>${escapeHTML(clip.id)}</dd>
                    <dt>Duration</dt><dd>${escapeHTML(formatDuration(clip.duration_seconds))}</dd>
                    <dt>Resolution</dt><dd>${clip.width && clip.height ? `${escapeHTML(clip.width)} × ${escapeHTML(clip.height)}` : "—"}</dd>
                    <dt>Original size</dt><dd>${escapeHTML(formatBytes(clip.size_bytes))}</dd>
                    <dt>Editorial state</dt><dd>${escapeHTML(clip.status)}</dd>
                    <dt>Lifecycle</dt><dd>${escapeHTML(clip.lifecycle_state)}</dd>
                    <dt>Stored</dt><dd>${escapeHTML(date(clip.created_at))}</dd>
                </dl>
            </section>

            <section class="detail-block" data-clip-detail-view="lineage">
                <h3>SOURCE LINEAGE</h3>
                <div class="source-stack">${sourceLinks}</div>
            </section>

            <section class="detail-block wide" data-clip-detail-view="metadata">
                <h3>LIBRARY METADATA</h3>
                <form id="metadata-form" class="metadata-form">
                    <label class="wide">TAGS
                        <input id="meta-tags" value="${escapeHTML(tags)}" placeholder="xbox, funny, launch-week, boss-fight">
                    </label>
                    <label>TOPIC
                        <input id="meta-topic" value="${escapeHTML(metadata.topic || "")}" placeholder="Xbox showcase">
                    </label>
                    <label>CONTENT TYPE
                        <input id="meta-content-type" value="${escapeHTML(metadata.content_type || "")}" placeholder="reaction, fail, news clip">
                    </label>
                    <label class="wide">NOTES
                        <textarea id="meta-notes" rows="2" placeholder="Anything useful for future retrieval…">${escapeHTML(metadata.notes || "")}</textarea>
                    </label>
                    <div class="metadata-actions wide">
                        <small>Embedding document: ${escapeHTML(embeddingStatus)}. Metadata changes mark semantic indexing stale automatically.</small>
                        <button class="mini" type="submit">Save metadata</button>
                    </div>
                </form>
            </section>

            <section class="detail-block wide" data-clip-detail-view="analysis">
                <h3>AI & SCORE</h3>
                ${features ? `
                    <p class="ai-summary">${escapeHTML(ai?.event_summary || "Local analysis exists; no AI event summary is stored.")}</p>
                    <div class="ai-tags">
                        ${(ai?.categories || []).map((value) => `<span class="pill">${escapeHTML(value)}</span>`).join("")}
                        ${features.candidate_score != null ? `<span class="pill active">Candidate score ${escapeHTML(number(features.candidate_score, 1))}</span>` : ""}
                    </div>
                    ${aiScores.length ? `<div class="ai-score-grid">${aiScores.map(([label, value]) => `<div><span>${escapeHTML(label)}</span><strong>${escapeHTML(number(value, 0))}</strong></div>`).join("")}</div>` : ""}
                ` : '<div class="empty">No stored analysis features yet.</div>'}
            </section>

            <section class="detail-block wide" data-clip-detail-view="analysis">
                <h3>TRANSCRIPT</h3>
                ${features?.transcript ? `<div class="transcript">${escapeHTML(features.transcript)}</div>` : '<div class="empty">No transcript stored for this clip.</div>'}
            </section>
        </div>
    `;
    setClipDetailView(state.detailView, { updateFocus: false });
}

function setClipDetailView(view, { updateFocus = false } = {}) {
    const selected = ["overview", "analysis", "lineage", "metadata"].includes(view)
        ? view
        : "overview";
    state.detailView = selected;

    document.querySelectorAll("[data-clip-detail-tab]").forEach((button) => {
        const active = button.dataset.clipDetailTab === selected;
        button.setAttribute("aria-selected", active ? "true" : "false");
        button.tabIndex = active ? 0 : -1;
        if (active && updateFocus) button.focus();
    });
    document.querySelectorAll("[data-clip-detail-view]").forEach((section) => {
        section.hidden = section.dataset.clipDetailView !== selected;
    });
}

async function loadChannels() {
    state.channels = await api("/v1/channels");
    const select = $("channel");
    const options = state.channels.map((channel) => (
        `<option value="${escapeHTML(channel.id)}">${escapeHTML(channelName(channel))}</option>`
    ));
    options.push('<option value="all">All / shared clips</option>');
    select.innerHTML = options.join("");
    select.disabled = false;

    if (state.channels.length) {
        const preferred = state.selectedChannel && state.channels.some((row) => row.id === state.selectedChannel)
            ? state.selectedChannel
            : state.channels[0].id;
        state.selectedChannel = preferred;
        sessionStorage.setItem("katcha.channel", preferred);
    } else {
        state.selectedChannel = "all";
    }
    select.value = state.selectedChannel;
    updateChannelScope();
}

function updateChannelScope() {
    const channel = currentChannel();
    if (channel?.id) sessionStorage.setItem("katcha.channel", channel.id);
    const selectedName = channel ? channelName(channel) : "All / shared";
    $("channel-badge").textContent = selectedName.toUpperCase();
    $("retention-settings").disabled = !channel;
}

async function loadSummary() {
    const params = new URLSearchParams();
    if (state.selectedChannel && state.selectedChannel !== "all") {
        params.set("channel_profile_id", state.selectedChannel);
    }
    state.summary = await api(`/v1/clips/library/summary?${params}`);
    renderCounts();
}

async function loadLibrary(append = false) {
    clearPreview();
    if (!append) {
        state.offset = 0;
        state.clips = [];
        message("Loading channel media…");
    }

    const query = queryString({
        limit: state.limit,
        offset: state.offset,
    });
    try {
        const result = await api(`/v1/clips/library?${query}`);
        state.total = result.total;
        state.clips = append ? [...state.clips, ...result.items] : result.items;
        state.offset = state.clips.length;
        renderList();

        const ids = new Set(state.clips.map((row) => row.id));
        if (!append && state.selectedId && !ids.has(state.selectedId)) {
            state.selectedId = null;
        }
        if (!state.selectedId && state.clips.length) {
            await selectClip(state.clips[0].id);
        } else if (!state.clips.length) {
            $("clip-detail").innerHTML = '<div class="empty">No clips match this channel and search.</div>';
        }
        $("refresh").disabled = false;
        $("connection").textContent = "CONNECTED";
        $("connection").classList.add("online");
        message(`Loaded ${state.total} matching clip${state.total === 1 ? "" : "s"}.`);
    } catch (error) {
        $("connection").textContent = "OFFLINE";
        $("connection").classList.remove("online");
        message(error.message, true);
    }
}

async function refreshLibrary() {
    await Promise.all([loadSummary(), loadLibrary(false)]);
}

async function selectClip(id) {
    if (state.selectedId !== id) state.detailView = "overview";
    state.selectedId = id;
    renderList();
    const clip = state.clips.find((row) => row.id === id);
    if (!clip) return;
    $("clip-detail").innerHTML = '<div class="empty">Loading clip detail…</div>';

    let features = state.features.get(id);
    let sources = state.sources.get(id);
    const jobs = [];
    if (features === undefined) {
        jobs.push(
            api(`/v1/clips/${encodeURIComponent(id)}/features`)
                .then((value) => {
                    features = value;
                    state.features.set(id, value);
                })
                .catch((error) => {
                    if (error.status !== 404) throw error;
                    features = null;
                    state.features.set(id, null);
                }),
        );
    }
    if (sources === undefined) {
        jobs.push(
            api(`/v1/clips/${encodeURIComponent(id)}/sources`).then((value) => {
                sources = value;
                state.sources.set(id, value);
            }),
        );
    }
    try {
        await Promise.all(jobs);
        renderDetail(clip, features, sources || []);
        renderList();
    } catch (error) {
        message(error.message, true);
        $("clip-detail").innerHTML = '<div class="empty">Clip detail could not be loaded.</div>';
    }
}

async function connect(event) {
    event.preventDefault();
    state.token = $("token").value.trim() || state.token;
    $("token").value = "";
    if (state.token) sessionStorage.setItem("katcha.controlToken", state.token);
    try {
        await loadChannels();
        await refreshLibrary();
    } catch (error) {
        message(error.message, true);
    }
}

async function loadPreview(id, button) {
    button.disabled = true;
    const host = $("clip-preview");
    host.innerHTML = '<div class="empty">Loading stored video…</div>';
    try {
        const blob = await apiBlob(`/v1/clips/${encodeURIComponent(id)}/media`);
        clearPreview();
        state.previewUrl = URL.createObjectURL(blob);
        host.innerHTML = `<video controls playsinline preload="metadata" src="${escapeHTML(state.previewUrl)}"></video>`;
    } catch (error) {
        host.innerHTML = `<div class="empty error">${escapeHTML(error.message)}</div>`;
        button.disabled = false;
    }
}

async function analyze(id, button) {
    button.disabled = true;
    const forceRetry = button.dataset.forceRetry === "true";
    try {
        const result = await api(`/v1/clips/${encodeURIComponent(id)}/analyze`, {
            method: "POST",
            body: JSON.stringify({ force_retry: forceRetry }),
        });
        message(
            (forceRetry ? "Analysis restarted" : "Analysis queued") +
            ` · ${result.stage.replaceAll("_", " ")}.`
        );
        button.textContent = forceRetry ? "Analysis restarted" : "Analysis queued";
    } catch (error) {
        message(error.message, true);
        button.disabled = false;
    }
}

async function saveMetadata(event) {
    event.preventDefault();
    const clip = state.clips.find((row) => row.id === state.selectedId);
    if (!clip) return;
    const tags = $("meta-tags").value.split(",").map((value) => value.trim()).filter(Boolean);
    try {
        const lifecycle = await api(
            `/v1/clips/${encodeURIComponent(clip.id)}/library-metadata`,
            {
                method: "PATCH",
                body: JSON.stringify({
                    tags,
                    topic: $("meta-topic").value.trim() || null,
                    content_type: $("meta-content-type").value.trim() || null,
                    notes: $("meta-notes").value.trim() || null,
                    actor: "operator",
                }),
            },
        );
        Object.assign(clip, {
            tags: lifecycle.tags,
            library_metadata: lifecycle.library_metadata,
            embedding_metadata: lifecycle.embedding_metadata,
        });
        renderDetail(clip, state.features.get(clip.id), state.sources.get(clip.id) || []);
        renderList();
        message("Clip metadata saved. Semantic index marked for refresh.");
    } catch (error) {
        message(error.message, true);
    }
}

async function archiveClip(id) {
    try {
        await api(`/v1/clips/${encodeURIComponent(id)}/archive`, {
            method: "POST",
            body: JSON.stringify({ actor: "operator", reason: "Manual Clip Library archive" }),
        });
        state.features.delete(id);
        await refreshLibrary();
        message("Clip archived. Metadata remains hot and searchable.");
    } catch (error) {
        message(error.message, true);
    }
}

async function restoreClip(id) {
    try {
        await api(`/v1/clips/${encodeURIComponent(id)}/restore`, {
            method: "POST",
            body: JSON.stringify({ actor: "operator", reason: "Manual Clip Library restore" }),
        });
        await refreshLibrary();
        message("Clip restored to hot storage.");
    } catch (error) {
        message(error.message, true);
    }
}

function openPurgeDialog(id) {
    state.purgeId = id;
    $("purge-step1").hidden = false;
    $("purge-step2").hidden = true;
    $("purge-confirm-input").value = "";
    $("purge-phrase").textContent = `DELETE ${id}`;
    $("purge-dialog").showModal();
}

async function confirmPurge() {
    const id = state.purgeId;
    if (!id) return;
    const phrase = `DELETE ${id}`;
    if ($("purge-confirm-input").value.trim() !== phrase) {
        message("Deletion phrase does not match.", true);
        return;
    }
    try {
        await api(`/v1/clips/${encodeURIComponent(id)}/purge`, {
            method: "POST",
            body: JSON.stringify({
                actor: "operator",
                reason: "Manual Clip Library deletion",
                confirmation_text: phrase,
            }),
        });
        $("purge-dialog").close();
        clearPreview();
        await refreshLibrary();
        message("Source media deleted. Searchable metadata and lineage were retained.");
    } catch (error) {
        message(error.message, true);
    }
}

function setRetentionEnabled() {
    const managed = $("retention-mode").value === "managed";
    $("retention-mode-help").textContent = managed ? "Select the cleanup options you want, then save the policy." : "Media is kept indefinitely. Selecting an archive or cleanup option switches to Managed lifecycle.";
    for (const id of [
        "archive-days",
        "purge-days",
        "failed-days",
    ]) {
        $(id).disabled = !managed;
    }
}

async function openRetention() {
    const channel = currentChannel();
    if (!channel) return;
    try {
        const [policy, preview] = await Promise.all([
            api(`/v1/channels/${channel.id}/clip-retention`),
            api(`/v1/channels/${channel.id}/clip-retention/preview`),
        ]);
        $("retention-feedback").textContent = "Choose your settings, then Save policy. Close does not save changes.";
        $("retention-title").textContent = `${channelName(channel)} retention`;
        $("retention-mode").value = policy.retention_mode;
        $("auto-archive").checked = policy.auto_archive;
        $("auto-duplicates").checked = policy.auto_remove_duplicates;
        $("auto-purge").checked = policy.auto_purge;
        $("archive-days").value = policy.archive_after_days || 30;
        $("purge-days").value = policy.purge_after_days || 90;
        $("failed-days").value = policy.failed_purge_after_days || 14;
        $("retention-confirm").hidden = true;
        $("retention-confirm-step1").hidden = false;
        $("retention-confirm-step2").hidden = true;
        setRetentionEnabled();
        renderMaintenancePreview(preview);
        $("retention-dialog").showModal();
    } catch (error) {
        message(error.message, true);
    }
}

function renderMaintenancePreview(preview) {
    const archive = Number(preview.archive_count || 0);
    const purge = Number(preview.purge_count || 0);
    const duplicates = Number(preview.duplicate_count || 0);
    $("maintenance-summary").textContent = `${archive} archive · ${purge} purge · ${duplicates} near-duplicate${duplicates === 1 ? "" : "s"} detected`;
    $("maintenance-detail").textContent = `${preview.active_skipped || 0} active and ${preview.shared_skipped || 0} shared clips protected from automatic cleanup.`;
    $("run-maintenance").disabled = !(archive || purge);
}

async function previewMaintenance() {
    const channel = currentChannel();
    if (!channel) return;
    try {
        const preview = await api(`/v1/channels/${channel.id}/clip-retention/preview`);
        renderMaintenancePreview(preview);
    } catch (error) {
        message(error.message, true);
    }
}

function retentionPayload(confirmed = false, phrase = null) {
    const managed = $("retention-mode").value === "managed";
    const autoPurge = managed && $("auto-purge").checked;
    return {
        retention_mode: managed ? "managed" : "indefinite",
        auto_archive: managed && $("auto-archive").checked,
        auto_purge: autoPurge,
        auto_remove_duplicates: managed && $("auto-duplicates").checked,
        archive_after_days: managed && $("auto-archive").checked
            ? Number($("archive-days").value)
            : null,
        purge_after_days: autoPurge ? Number($("purge-days").value) : null,
        failed_purge_after_days: autoPurge ? Number($("failed-days").value) : null,
        actor: "operator",
        acknowledge_irreversible: confirmed,
        confirmation_text: phrase,
    };
}

async function persistRetention(confirmed = false, phrase = null) {
    const channel = currentChannel();
    if (!channel) return;
    $("save-retention").disabled = true;
    $("retention-feedback").textContent = "Saving policy…";
    try {
        await api(`/v1/channels/${channel.id}/clip-retention`, {
            method: "PUT",
            body: JSON.stringify(retentionPayload(confirmed, phrase)),
        });
        $("retention-confirm").hidden = true;
        await previewMaintenance();
        message("Channel retention policy saved.");
        $("retention-feedback").textContent = "Channel retention policy saved.";
    } catch (error) {
        $("retention-feedback").textContent = error.message;
        message(error.message, true);
    } finally {
        $("save-retention").disabled = false;
    }
}

function saveRetention() {
    if ($("retention-mode").value === "managed" && $("auto-purge").checked) {
        const channel = currentChannel();
        if (!channel) return;
        $("retention-confirm").hidden = false;
        $("retention-confirm-step1").hidden = false;
        $("retention-confirm-step2").hidden = true;
        $("retention-phrase").textContent = `ENABLE AUTO DELETE ${channelName(channel)}`;
        $("retention-confirm").scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
    }
    void persistRetention(false, null);
}

async function confirmAutoDelete() {
    const channel = currentChannel();
    if (!channel) return;
    const phrase = `ENABLE AUTO DELETE ${channelName(channel)}`;
    if (
        $("retention-confirm-input").value !== phrase
        || !$("retention-irreversible").checked
    ) {
        message("Complete the exact phrase and irreversible-action confirmation.", true);
        return;
    }
    await persistRetention(true, phrase);
}

async function runMaintenance() {
    const channel = currentChannel();
    if (!channel) return;
    try {
        const result = await api(`/v1/channels/${channel.id}/clip-retention/run`, {
            method: "POST",
            body: JSON.stringify({}),
        });
        await refreshLibrary();
        await previewMaintenance();
        message(`Cleanup completed · ${result.completed.length} action${result.completed.length === 1 ? "" : "s"}, ${result.blocked.length} blocked.`);
    } catch (error) {
        message(error.message, true);
    }
}

$("connect-form").addEventListener("submit", connect);
$("refresh").addEventListener("click", refreshLibrary);
$("channel").addEventListener("change", async () => {
    state.selectedChannel = $("channel").value;
    state.selectedId = null;
    state.features.clear();
    state.sources.clear();
    updateChannelScope();
    await refreshLibrary();
});
$("search").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => void loadLibrary(false), 250);
});
$("status-filter").addEventListener("change", () => void loadLibrary(false));
document.querySelector(".clip-stats").addEventListener("click", (event) => {
    const button = event.target.closest("[data-bucket]");
    if (!button) return;
    state.bucket = button.dataset.bucket;
    state.selectedId = null;
    void loadLibrary(false);
});
$("clip-list").addEventListener("click", (event) => {
    const more = event.target.closest("#load-more");
    if (more) {
        void loadLibrary(true);
        return;
    }
    const row = event.target.closest("[data-clip-id]");
    if (row) void selectClip(row.dataset.clipId);
});
$("clip-detail").addEventListener("click", (event) => {
    const detailTab = event.target.closest("[data-clip-detail-tab]");
    if (detailTab) {
        setClipDetailView(detailTab.dataset.clipDetailTab);
        return;
    }
    const preview = event.target.closest("[data-load-preview]");
    if (preview) void loadPreview(preview.dataset.loadPreview, preview);
    const analyzeButton = event.target.closest("[data-analyze]");
    if (analyzeButton) void analyze(analyzeButton.dataset.analyze, analyzeButton);
    const archiveButton = event.target.closest("[data-archive]");
    if (archiveButton) void archiveClip(archiveButton.dataset.archive);
    const restoreButton = event.target.closest("[data-restore]");
    if (restoreButton) void restoreClip(restoreButton.dataset.restore);
    const purgeButton = event.target.closest("[data-purge]");
    if (purgeButton) openPurgeDialog(purgeButton.dataset.purge);
});
$("clip-detail").addEventListener("submit", (event) => {
    if (event.target.id === "metadata-form") void saveMetadata(event);
});
$("clip-detail").addEventListener("keydown", (event) => {
    const current = event.target.closest("[data-clip-detail-tab]");
    if (!current || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...document.querySelectorAll("[data-clip-detail-tab]")];
    const index = tabs.indexOf(current);
    if (index < 0) return;
    event.preventDefault();
    let nextIndex = index;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    setClipDetailView(tabs[nextIndex].dataset.clipDetailTab, { updateFocus: true });
});

$("retention-settings").addEventListener("click", openRetention);
$("retention-mode").addEventListener("change", () => {
    if ($("retention-mode").value === "indefinite") {
        for (const id of ["auto-archive", "auto-duplicates", "auto-purge"]) $(id).checked = false;
    }
    setRetentionEnabled();
});
for (const id of ["auto-archive", "auto-duplicates", "auto-purge"]) {
    $(id).addEventListener("change", () => {
        if ($(id).checked) $("retention-mode").value = "managed";
        setRetentionEnabled();
    });
}
$("preview-maintenance").addEventListener("click", previewMaintenance);
$("run-maintenance").addEventListener("click", runMaintenance);
$("save-retention").addEventListener("click", saveRetention);
$("retention-confirm-continue").addEventListener("click", () => {
    $("retention-confirm-step1").hidden = true;
    $("retention-confirm-step2").hidden = false;
    $("retention-confirm-input").focus();
});
$("retention-confirm-save").addEventListener("click", confirmAutoDelete);

$("purge-continue").addEventListener("click", () => {
    $("purge-step1").hidden = true;
    $("purge-step2").hidden = false;
    $("purge-confirm-input").focus();
});
$("purge-confirm").addEventListener("click", confirmPurge);

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => {
        const dialog = $(button.dataset.closeDialog);
        if (dialog?.open) dialog.close();
    });
});

if(state.linkedClip){
    const button=document.createElement('button');button.type='button';button.className='button secondary';button.textContent='Showing linked clip · Show all clips';
    document.querySelector('.clip-toolbar').append(button);
    button.onclick=()=>{state.linkedClip='';state.selectedId=null;launchParams.delete('clip');history.replaceState(null,'','/clips?'+launchParams.toString());button.remove();refreshLibrary();};
}
