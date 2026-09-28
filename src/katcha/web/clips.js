const state = {
    token: "",
    clips: [],
    sources: [],
    selectedId: null,
    features: new Map(),
    bucket: "all",
    previewUrl: null,
};
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const date = (value) => value ? new Date(value).toLocaleString() : "—";
const number = (value, digits = 0) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
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
        try { body = await response.json(); } catch {}
        const error = new Error(typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`);
        error.status = response.status;
        throw error;
    }
    return response.json();
}
async function apiBlob(path) {
    const response = await request(path);
    if (!response.ok) {
        let body;
        try { body = await response.json(); } catch {}
        throw new Error(typeof body?.detail === "string" ? body.detail : `Media request failed (${response.status})`);
    }
    return response.blob();
}
function clearPreview() {
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
}
function sourceRows(clipId) {
    return state.sources.filter((row) => row.clip_id === clipId);
}
function primarySource(clipId) {
    return sourceRows(clipId)[0] || null;
}
function bucketForStatus(status) {
    const value = String(status || "").toLowerCase();
    if (value === "failed") return "failed";
    if (["published", "measured"].includes(value)) return "history";
    if (["selected", "scripted", "voiced", "rendered", "review", "scheduled"].includes(value)) return "production";
    return "new";
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
    if (!Number.isFinite(bytes) || bytes <= 0) return "—";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / (1024 ** index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}
function clipTitle(clip) {
    const source = primarySource(clip.id);
    return source?.title || `${source?.platform || "Stored"} clip · ${clip.id.slice(0, 8)}`;
}
function matchesSearch(clip) {
    const q = $("search").value.trim().toLowerCase();
    if (!q) return true;
    const sources = sourceRows(clip.id);
    const haystack = [
        clip.id,
        clip.status,
        clip.extension,
        ...sources.flatMap((row) => [row.title, row.creator, row.platform, row.source_url]),
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(q);
}
function visibleClips() {
    const status = $("status-filter").value;
    return state.clips.filter((clip) =>
        (state.bucket === "all" || bucketForStatus(clip.status) === state.bucket)
        && (status === "all" || clip.status === status)
        && matchesSearch(clip)
    );
}
function renderCounts() {
    $("count-all").textContent = state.clips.length;
    for (const name of ["new", "production", "history", "failed"]) {
        $(`count-${name}`).textContent = state.clips.filter((clip) => bucketForStatus(clip.status) === name).length;
    }
}
function renderStatusOptions() {
    const current = $("status-filter").value;
    const statuses = [...new Set(state.clips.map((row) => row.status))].sort();
    $("status-filter").innerHTML = '<option value="all">All statuses</option>' + statuses.map((value) =>
        `<option value="${escapeHTML(value)}">${escapeHTML(value.replaceAll("_", " "))}</option>`
    ).join("");
    if (current === "all" || statuses.includes(current)) $("status-filter").value = current;
}
function renderList() {
    const rows = visibleClips();
    $("visible-count").textContent = `${rows.length} clip${rows.length === 1 ? "" : "s"}`;
    document.querySelectorAll("[data-bucket]").forEach((button) => {
        button.classList.toggle("is-active", button.dataset.bucket === state.bucket);
    });
    $("clip-list").innerHTML = rows.length ? rows.map((clip) => {
        const source = primarySource(clip.id);
        const cached = state.features.get(clip.id);
        const score = cached?.candidate_score;
        const bucket = bucketForStatus(clip.status);
        return `<button class="clip-row ${state.selectedId === clip.id ? "is-selected" : ""}" type="button" data-clip-id="${escapeHTML(clip.id)}">
            <div class="clip-row-top">
                <div>
                    <h3>${escapeHTML(clipTitle(clip))}</h3>
                    <p>${escapeHTML(source?.creator || "Unknown creator")} · ${escapeHTML(source?.platform || "stored media")} · ${escapeHTML(date(clip.created_at))}</p>
                </div>
                ${score != null ? `<span class="score-chip">${escapeHTML(number(score, 0))}</span>` : ""}
            </div>
            <div class="clip-row-meta">
                <span class="clip-chip status-${bucket}">${escapeHTML(clip.status.replaceAll("_", " ").toUpperCase())}</span>
                <span class="clip-chip">${escapeHTML(formatDuration(clip.duration_seconds))}</span>
                ${clip.width && clip.height ? `<span class="clip-chip">${escapeHTML(clip.width)}×${escapeHTML(clip.height)}</span>` : ""}
                <span class="clip-chip">${escapeHTML(formatBytes(clip.size_bytes))}</span>
            </div>
        </button>`;
    }).join("") : '<div class="empty">No clips match this view.</div>';
}
function aiResult(features) {
    const ai = features?.ai_features || {};
    return ai.deep || ai.bulk || null;
}
function renderDetail(clip, features) {
    clearPreview();
    const sources = sourceRows(clip.id);
    const source = sources[0] || null;
    const ai = aiResult(features);
    const bucket = bucketForStatus(clip.status);
    const canAnalyze = !features && clip.status !== "failed";
    const sourceLinks = sources.length ? sources.map((row) =>
        `<div><a class="source-link" href="${escapeHTML(row.source_url)}" target="_blank" rel="noopener">${escapeHTML(row.title || row.source_url)}</a><small>${escapeHTML(row.platform)}${row.creator ? ` · ${escapeHTML(row.creator)}` : ""}</small></div>`
    ).join("") : '<span class="empty">No source lineage is attached.</span>';
    const aiScores = ai ? [
        ["Hook", ai.hook_score],
        ["Surprise", ai.surprise_score],
        ["Humor", ai.humor_score],
        ["Comments", ai.comment_potential],
        ["Rewatch", ai.rewatch_potential],
    ] : [];
    $("clip-detail").innerHTML = `
        <div class="clip-detail-head">
            <div>
                <div class="eyebrow">CLIP DETAIL</div>
                <h2>${escapeHTML(clipTitle(clip))}</h2>
                <p>${escapeHTML(source?.creator || "Unknown creator")} · ${escapeHTML(source?.platform || "stored media")}</p>
            </div>
            <div class="detail-actions">
                <span class="clip-chip status-${bucket}">${escapeHTML(clip.status.replaceAll("_", " ").toUpperCase())}</span>
                <button class="mini" type="button" data-load-preview="${escapeHTML(clip.id)}">Load video preview</button>
                ${canAnalyze ? `<button class="mini" type="button" data-analyze="${escapeHTML(clip.id)}">Analyze clip</button>` : ""}
            </div>
        </div>
        <div id="clip-preview" class="clip-preview"><div class="empty">Preview is loaded only when requested so browsing the library stays fast.</div></div>
        <div class="detail-grid">
            <section class="detail-block">
                <h3>STORED MEDIA</h3>
                <dl class="detail-kv">
                    <dt>Clip ID</dt><dd>${escapeHTML(clip.id)}</dd>
                    <dt>Duration</dt><dd>${escapeHTML(formatDuration(clip.duration_seconds))}</dd>
                    <dt>Resolution</dt><dd>${clip.width && clip.height ? `${escapeHTML(clip.width)} × ${escapeHTML(clip.height)}` : "—"}</dd>
                    <dt>File size</dt><dd>${escapeHTML(formatBytes(clip.size_bytes))}</dd>
                    <dt>Type</dt><dd>${escapeHTML(clip.extension || "unknown")}</dd>
                    <dt>Stored</dt><dd>${escapeHTML(date(clip.created_at))}</dd>
                </dl>
            </section>
            <section class="detail-block">
                <h3>SOURCE LINEAGE</h3>
                <div class="source-stack">${sourceLinks}</div>
            </section>
            <section class="detail-block wide">
                <h3>AI & SCORE</h3>
                ${features ? `
                    <p class="ai-summary">${escapeHTML(ai?.event_summary || "Local analysis is available; no AI event summary is stored for this clip.")}</p>
                    <div class="ai-tags">${(ai?.categories || []).map((value) => `<span class="pill">${escapeHTML(value)}</span>`).join("")}${features.candidate_score != null ? `<span class="pill active">Candidate score ${escapeHTML(number(features.candidate_score, 1))}</span>` : ""}</div>
                    ${aiScores.length ? `<div class="ai-score-grid">${aiScores.map(([label, value]) => `<div><span>${escapeHTML(label)}</span><strong>${escapeHTML(number(value, 0))}</strong></div>`).join("")}</div>` : ""}
                ` : '<div class="empty">This clip has not produced stored analysis features yet.</div>'}
            </section>
            <section class="detail-block wide">
                <h3>TRANSCRIPT</h3>
                ${features?.transcript ? `<div class="transcript">${escapeHTML(features.transcript)}</div>` : '<div class="empty">No transcript stored for this clip.</div>'}
            </section>
        </div>
    `;
}
async function selectClip(id) {
    state.selectedId = id;
    renderList();
    const clip = state.clips.find((row) => row.id === id);
    if (!clip) return;
    $("clip-detail").innerHTML = '<div class="empty">Loading clip detail…</div>';
    let features = state.features.get(id);
    if (features === undefined) {
        try {
            features = await api(`/v1/clips/${encodeURIComponent(id)}/features`);
            state.features.set(id, features);
        } catch (error) {
            if (error.status === 404) {
                features = null;
                state.features.set(id, null);
            } else {
                message(error.message, true);
                features = null;
            }
        }
    }
    renderDetail(clip, features);
    renderList();
}
async function loadLibrary() {
    clearPreview();
    message("Loading stored media…");
    try {
        const [clips, sources] = await Promise.all([
            api("/v1/clips?limit=250"),
            api("/v1/sources?limit=250"),
        ]);
        state.clips = clips;
        state.sources = sources;
        const ids = new Set(clips.map((row) => row.id));
        for (const id of [...state.features.keys()]) if (!ids.has(id)) state.features.delete(id);
        renderCounts();
        renderStatusOptions();
        renderList();
        $("refresh").disabled = false;
        $("connection").textContent = "CONNECTED";
        $("connection").classList.add("online");
        const rows = visibleClips();
        if (state.selectedId && ids.has(state.selectedId)) {
            await selectClip(state.selectedId);
        } else if (rows.length) {
            await selectClip(rows[0].id);
        } else {
            state.selectedId = null;
            $("clip-detail").innerHTML = '<div class="empty">No stored clips are available yet. Ingestion Sources will populate this library as media is acquired.</div>';
        }
        message(`Library loaded · ${clips.length} stored clip${clips.length === 1 ? "" : "s"}.`);
    } catch (error) {
        $("connection").textContent = "OFFLINE";
        $("connection").classList.remove("online");
        message(error.message, true);
    }
}
async function connect(event) {
    event.preventDefault();
    state.token = $("token").value.trim();
    $("token").value = "";
    await loadLibrary();
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
        host.querySelector("video").play().catch(() => {});
    } catch (error) {
        host.innerHTML = `<div class="empty error">${escapeHTML(error.message)}</div>`;
        button.disabled = false;
    }
}
async function analyze(id, button) {
    button.disabled = true;
    try {
        const result = await api(`/v1/clips/${encodeURIComponent(id)}/analyze`, {
            method: "POST",
            body: JSON.stringify({ force_retry: false }),
        });
        message(`Analysis queued · ${result.stage.replaceAll("_", " ")}.`);
        button.textContent = "Analysis queued";
    } catch (error) {
        message(error.message, true);
        button.disabled = false;
    }
}
$("connect-form").addEventListener("submit", connect);
$("refresh").addEventListener("click", loadLibrary);
$("search").addEventListener("input", () => {
    renderList();
});
$("status-filter").addEventListener("change", renderList);
document.querySelector(".clip-stats").addEventListener("click", (event) => {
    const button = event.target.closest("[data-bucket]");
    if (!button) return;
    state.bucket = button.dataset.bucket;
    renderList();
});
$("clip-list").addEventListener("click", (event) => {
    const row = event.target.closest("[data-clip-id]");
    if (row) void selectClip(row.dataset.clipId);
});
$("clip-detail").addEventListener("click", (event) => {
    const preview = event.target.closest("[data-load-preview]");
    if (preview) void loadPreview(preview.dataset.loadPreview, preview);
    const analyzeButton = event.target.closest("[data-analyze]");
    if (analyzeButton) void analyze(analyzeButton.dataset.analyze, analyzeButton);
});
