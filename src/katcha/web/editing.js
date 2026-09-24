/* All state comes from the authenticated, same-origin Katcha control plane. */
const state = { token: "", channel: "", episodes: [], blueprints: [], attempts: new Map(), epoch: 0 };
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const date = (value) => value ? new Date(value).toLocaleString() : "—";
function message(value, error = false) { $("message").textContent = value; $("message").classList.toggle("error", error); }
async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), ...options.headers } });
    if (!response.ok) {
        let body; try { body = await response.json(); } catch {}
        throw new Error(typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`);
    }
    return response.json();
}
function channelPath(suffix) { return `/v1/channels/${encodeURIComponent(state.channel)}${suffix}`; }
function statusType(row) {
    const attempt = state.attempts.get(row.id);
    if (attempt?.status === "dead_letter" || row.error) return "attention";
    if (["published", "completed", "rejected", "cancelled"].includes(row.status)) return "done";
    return "active";
}
function renderBlueprints() {
    $("count-blueprints").textContent = state.blueprints.length;
    $("blueprints").innerHTML = state.blueprints.length ? state.blueprints.map((row) => `<article class="item"><div><h3>${escapeHTML(row.blueprint_key)} <small>v${escapeHTML(row.version)}</small></h3><p>Contract ${escapeHTML(row.contract_version)} · Created ${escapeHTML(date(row.created_at))}</p></div><div class="item-actions">${row.is_default ? '<span class="pill active">DEFAULT</span>' : row.is_active ? '<span class="pill active">ACTIVE</span>' : `<button class="mini" data-activate="${escapeHTML(row.blueprint_key)}" data-version="${escapeHTML(row.version)}">Activate</button>`}</div></article>`).join("") : '<div class="empty">No blueprints configured for this channel.</div>';
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
async function loadChannel() {
    const epoch = ++state.epoch;
    state.channel = $("channel").value;
    state.episodes = []; state.blueprints = []; state.attempts = new Map();
    if (!state.channel) { renderBlueprints(); renderEpisodes(); renderPerformance(null); return; }
    message("Loading channel production…");
    try {
        const [blueprints, episodes, performance] = await Promise.all([
            api(channelPath("/edit-blueprints")),
            api(`/v1/short-episodes?channel_profile_id=${encodeURIComponent(state.channel)}&limit=100`),
            api(channelPath("/edit-blueprints/performance/latest")),
        ]);
        if (epoch !== state.epoch) return;
        state.blueprints = blueprints; state.episodes = episodes;
        renderBlueprints(); renderEpisodes(); renderPerformance(performance);
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
    const activate = event.target.closest("[data-activate]");
    const recover = event.target.closest("[data-recover]");
    const details = event.target.closest("[data-attempts]");
    if (!activate && !recover && !details) return;
    if (details) { const attempt = state.attempts.get(details.dataset.attempts); message(attempt ? `Render attempt ${attempt.attempt_number}: ${attempt.status}${attempt.error ? ` · ${attempt.error}` : ""}` : "No render attempts recorded for this episode."); return; }
    const button = activate || recover; button.disabled = true;
    try {
        if (activate) {
            await api(channelPath(`/edit-blueprints/${encodeURIComponent(activate.dataset.activate)}/${encodeURIComponent(activate.dataset.version)}/activate`), { method: "POST", body: JSON.stringify({ actor: "editing-control-center", set_default: true }) });
            await loadChannel(); message("Blueprint activated as this channel’s default.");
        } else {
            const result = await api(`/v1/short-episodes/${encodeURIComponent(recover.dataset.recover)}/render/recover`, { method: "POST", body: JSON.stringify({ actor: "editing-control-center", note: "Operator recovery from Editing Control Center" }) });
            await loadChannel(); message(`Render recovery started as a new episode generation (${result.child_source_id}).`);
        }
    } catch (error) { message(error.message, true); button.disabled = false; }
}
$("connect-form").addEventListener("submit", connect);
$("channel").addEventListener("change", loadChannel);
$("refresh").addEventListener("click", loadChannel);
$("filter").addEventListener("change", renderEpisodes);
$("blueprints").addEventListener("click", action);
$("episodes").addEventListener("click", action);
