/* Same-origin API client. Scores and histories are always supplied by Katcha. */
const state = {
    token: "",
    channel: "",
    watch: null,
    editPerformance: null,
    rows: [],
    selected: null,
    dossier: null,
    compare: [],
    epoch: 0,
    detailEpoch: 0,
    hours: 168,
    pending: new Set(),
    initialInterests: [],
    dossierTab: "overview",
    dossierEpisodes: [],
};
const $ = (id) => document.getElementById(id);
const esc = (value) =>
    String(value ?? "").replace(
        /[&<>"']/g,
        (c) =>
            ({
                "&": "&amp;",
                "<": "&lt;",
                ">": "&gt;",
                '"': "&quot;",
                "'": "&#39;",
            })[c],
    );
const pct = (value) =>
    value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
const date = (value) => (value ? new Date(value).toLocaleString() : "Unknown");
const score = (row) =>
    Number(
        row.opportunity.calibrated_score ?? row.opportunity.opportunity_score,
    );
const safeURL = (value) => {
    try {
        const url = new URL(value);
        return ["https:", "http:"].includes(url.protocol) ? url.href : null;
    } catch {
        return null;
    }
};
function status(message) {
    $("status").textContent = message;
}
function feedback(message, kind = "success") {
    const host = $("interest-feedback");
    if (!host) return;
    host.hidden = !message;
    host.className = `interest-feedback ${kind}`;
    host.innerHTML = message
        ? `<span aria-hidden="true">${kind === "error" ? "!" : "✓"}</span><span>${esc(message)}</span>`
        : "";
}
function emptyDossier(message = "Select a ranked topic to inspect why it matters, supporting source records, interactive signal history, and editorial handoff.") {
    return `<div class="dossier-empty"><span class="dossier-empty-icon">◇</span><div><strong>Opportunity dossier</strong><p>${esc(message)}</p></div><div class="dossier-empty-grid"><span>Why it ranks</span><span>Source records</span><span>Signal graph</span><span>Editorial handoff</span></div></div>`;
}
function normalizeInterests(values) {
    const seen = new Set();
    return values
        .map((value) => String(value || "").trim())
        .filter(Boolean)
        .filter((value) => {
            const key = value.toLowerCase();
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
}
function renderInterests() {
    const host = $("interest-chips");
    if (!host) return;
    const interests = normalizeInterests(state.watch?.interests || []);
    host.innerHTML = interests.length
        ? interests
              .map(
                  (interest) =>
                      `<button class="chip" type="button" data-remove-interest="${esc(interest)}" title="Remove ${esc(interest)}">${esc(interest)} <span aria-hidden="true">×</span></button>`,
              )
              .join("")
        : '<span class="muted">No interests configured yet.</span>';
    $("reset-interests").disabled =
        !state.channel ||
        interests.map((item) => item.toLowerCase()).join("|") ===
            state.initialInterests.map((item) => item.toLowerCase()).join("|");
}
async function api(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
            ...options.headers,
        },
    });
    if (!response.ok) {
        let body;
        try {
            body = await response.json();
        } catch {}
        throw new Error(
            typeof body?.detail === "string"
                ? body.detail
                : `Request failed (${response.status})`,
        );
    }
    return response.json();
}
const channelProfileURL = () =>
    `/v1/channels/${encodeURIComponent(state.channel)}`;
const channelURL = () => `${channelProfileURL()}/trends`;
const explorerURL = () => `${channelURL()}/explorer`;
async function connect(event) {
    event?.preventDefault();
    state.token = $("token").value.trim();
    $("token").value = "";
    status("Connecting…");
    try {
        const channels = await api("/v1/channels");
        $("channel").innerHTML =
            '<option value="">Select a channel</option>' +
            channels
                .map(
                    (c) =>
                        `<option value="${esc(c.id)}">${esc(c.profile_metadata?.channel_title || c.profile_metadata?.name || c.id)} · ${esc(c.status)}</option>`,
                )
                .join("");
        $("channel").disabled = false;
        $("connection-state").textContent = "CONNECTED";
        if (channels.length) {
            $("channel").value = channels[0].id;
            await loadChannel();
        } else
            status(
                "No channels configured. Create a channel through the control API to begin.",
            );
    } catch (error) {
        $("connection-state").textContent = "CONNECTION FAILED";
        status(error.message);
    }
}
async function loadChannel() {
    const epoch = ++state.epoch;
    ++state.detailEpoch;
    state.channel = $("channel").value;
    state.selected = null;
    state.dossier = null;
    state.rows = [];
    state.compare = [];
    state.watch = null;
    state.editPerformance = null;
    state.initialInterests = [];
    state.dossierTab = "overview";
    $("interest-input").value = "";
    $("source-health").textContent = "";
    feedback("");
    renderInterests();
    renderEditPerformance();
    renderBoard();
    renderComparison();
    $("detail-panel").innerHTML = emptyDossier();
    for (const id of ["interest-input", "add-interest", "reset-interests", "refresh", "reload"])
        $(id).disabled = !state.channel;
    if (!state.channel) return;
    const base = channelURL();
    status("Loading stored opportunities…");
    try {
        const [watch, rows, health, editPerformance] = await Promise.all([
            api(`${base}/watch-profile`),
            api(`${base}/explorer`),
            api(`${base}/source-health`),
            api(`${channelProfileURL()}/editing-performance?age_bucket_hours=72`),
        ]);
        if (epoch !== state.epoch) return;
        state.watch = watch;
        state.initialInterests = normalizeInterests(watch?.interests || []);
        state.editPerformance = editPerformance;
        state.rows = rows;
        renderInterests();
        $("source-health").innerHTML =
            "<details><summary>Source health · latest stored state</summary><pre></pre></details>";
        $("source-health").querySelector("pre").textContent = JSON.stringify(
            health,
            null,
            2,
        );
        renderBoard();
        renderEditPerformance();
        status(
            rows.length
                ? `${rows.length} current topics loaded from stored opportunity snapshots.`
                : "No ranked opportunities yet. Add or refine an interest and Katcha will start a refresh automatically.",
        );
        if (rows.length) await selectTopic(rows[0].opportunity.id);
    } catch (error) {
        if (epoch === state.epoch) {
            status(error.message);
            feedback(error.message, "error");
        }
    }
}
function renderEditPerformance() {
    const host = $("edit-performance-body");
    if (!host) return;
    const snapshot = state.editPerformance;
    if (!snapshot) {
        host.className = "empty";
        host.innerHTML =
            "No maturity-matched edit evidence yet. Published videos need analytics near the selected outcome age.";
        return;
    }
    const groups = Array.isArray(snapshot.aggregate_metrics)
        ? snapshot.aggregate_metrics
        : [];
    host.className = "";
    const coverage = `Revenue ${pct(snapshot.monetary_coverage)} · retention ${pct(snapshot.retention_coverage)}`;
    const cards = groups
        .slice(0, 6)
        .map((group) => {
            const blueprint = `${group.edit_blueprint_key || "unknown"}@r${group.edit_blueprint_revision ?? "?"}`;
            const scope = `${group.source_kind || "source"} · ${group.source_scope || "unknown scope"}`;
            const avp =
                group.mean_average_view_percentage == null
                    ? "—"
                    : `${Number(group.mean_average_view_percentage).toFixed(1)}%`;
            const retention =
                group.mean_audience_watch_ratio_50pct == null
                    ? "—"
                    : pct(group.mean_audience_watch_ratio_50pct);
            const margin =
                group.covered_margin_per_publication_usd == null
                    ? "—"
                    : `${Number(group.covered_margin_per_publication_usd).toFixed(2)}`;
            return `<article class="edit-evidence-card"><div class="edit-evidence-title"><strong>${esc(blueprint)}</strong><span>${esc(scope)}</span></div><div class="edit-evidence-metrics"><div><span>Samples</span><strong>${Number(group.publication_count || 0)}</strong></div><div><span>Avg viewed</span><strong>${esc(avp)}</strong></div><div><span>50% retention</span><strong>${esc(retention)}</strong></div><div><span>Covered margin/video</span><strong>${esc(margin)}</strong></div></div><small>Style ${esc(group.selected_style || "default")} · money ${pct(group.monetary_coverage)} · retention data ${pct(group.retention_coverage)}</small></article>`;
        })
        .join("");
    host.innerHTML = `<div class="edit-performance-summary"><strong>${Number(snapshot.publication_count || 0)} maturity-matched publications</strong><span>${esc(coverage)} · snapshot v${Number(snapshot.version || 0)} · ${Number(snapshot.age_bucket_hours || 72)}h</span><span class="edit-performance-status">${esc(snapshot.comparison_status || "insufficient_data").replaceAll("_", " ")}</span></div><div class="edit-evidence-grid">${cards || '<p class="muted">No blueprint groups have enough lineage-backed analytics yet.</p>'}</div><p class="muted">Evidence is channel-scoped and advisory. Katcha does not automatically switch editing identity from this panel.</p>`;
}

function visibleRows() {
    const query = $("search").value.toLowerCase();
    return state.rows
        .filter((r) =>
            `${r.topic} ${r.tags.join(" ")}`.toLowerCase().includes(query),
        )
        .sort((a, b) => {
            const key = $("sort").value;
            return key === "confidence"
                ? Number(b.opportunity.confidence) -
                      Number(a.opportunity.confidence)
                : key === "acceleration"
                  ? Number(b.opportunity.components.acceleration || 0) -
                    Number(a.opportunity.components.acceleration || 0)
                  : score(b) - score(a);
        });
}
function renderBoard() {
    const rows = visibleRows();
    $("result-count").textContent = rows.length;
    $("topic-list").innerHTML = rows.length
        ? rows
              .map(
                  (r, i) =>
                      `<button class="topic-row ${state.selected === r.opportunity.id ? "active" : ""}" data-topic="${esc(r.opportunity.id)}" aria-pressed="${state.selected === r.opportunity.id}"><span class="rank">${i + 1}</span><span class="row-main"><span class="topic-title">${esc(r.topic)}</span><span class="row-sub">${esc(r.opportunity.lifecycle)} · confidence ${pct(r.opportunity.confidence)}</span><span class="row-sub">${date(r.opportunity.created_at)}</span></span><span class="row-score"><span class="score">${(score(r) * 100).toFixed(0)}<small>/100</small></span><span class="row-sub">${r.opportunity.calibrated_score == null ? "baseline" : "calibrated"}</span></span></button>`,
              )
              .join("")
        : '<div class="empty"><strong>No matching opportunities</strong>Only current snapshots for this channel’s active watch are shown.</div>';
}
async function selectTopic(id) {
    const epoch = ++state.detailEpoch;
    state.selected = id;
    state.dossier = null;
    state.dossierTab = "overview";
    renderBoard();
    $("detail-panel").innerHTML =
        '<div class="dossier-loading"><span></span><strong>Building dossier from stored evidence…</strong></div>';
    try {
        const base = explorerURL();
        const [dossier, episodes] = await Promise.all([
            api(`${base}/${id}?hours=${state.hours}`),
            api(`${base}/${id}/episodes`),
        ]);
        if (epoch !== state.detailEpoch) return;
        state.dossier = dossier;
        state.dossierEpisodes = episodes;
        renderDossier(dossier, episodes);
    } catch (error) {
        if (epoch === state.detailEpoch)
            $("detail-panel").innerHTML = emptyDossier(error.message);
    }
}
function sourceGlyph(record) {
    const key = `${record.provider_key || ""} ${record.source_kind || ""}`.toLowerCase();
    if (key.includes("youtube") || key.includes("video")) return "▶";
    if (key.includes("reddit") || key.includes("community")) return "◉";
    if (key.includes("news") || key.includes("article") || key.includes("web")) return "⌁";
    if (key.includes("developer") || key.includes("official")) return "◆";
    return "↗";
}
function metricSummary(metrics = {}) {
    const rows = Object.entries(metrics)
        .filter(([, value]) => Number.isFinite(Number(value)))
        .slice(0, 4);
    return rows.length
        ? rows
              .map(([key, value]) => `${key.replaceAll("_", " ")} ${Number(value).toLocaleString()}`)
              .join(" · ")
        : "No numeric metrics stored";
}
function latestSourceRecords(signals = []) {
    const records = new Map();
    for (const signal of signals) {
        const key = `${signal.provider_key}:${signal.external_id}`;
        const current = records.get(key);
        if (!current || new Date(signal.observed_at) > new Date(current.observed_at))
            records.set(key, signal);
    }
    return [...records.values()].sort(
        (a, b) => new Date(b.observed_at) - new Date(a.observed_at),
    );
}
function dossierOverview(d) {
    const o = d.opportunity;
    return `<div class="dossier-pane overview-pane">
        <div class="metrics dossier-metrics">
            <div class="metric"><span>OPPORTUNITY</span><strong>${pct(o.opportunity_score)}</strong></div>
            <div class="metric"><span>CONFIDENCE</span><strong>${pct(o.confidence)}</strong></div>
            <div class="metric"><span>CALIBRATED</span><strong>${pct(o.calibrated_score)}</strong></div>
        </div>
        <div class="snapshot-line">Snapshot ${date(o.created_at)} · expires ${date(o.expires_at)}</div>
        <div class="block-heading">WHY THIS RANKS</div>
        <ul class="why-list">${(o.reasons || []).map((reason) => `<li><span>↗</span>${esc(reason)}</li>`).join("") || "<li>No explanation was stored with this snapshot.</li>"}</ul>
        <details class="score-details">
            <summary>Inspect score components</summary>
            <div class="components">${Object.entries(o.components || {})
                .map(([key, value]) => `<div><span>${esc(key.replaceAll("_", " "))}</span><strong>${Number(value).toFixed(3)}</strong></div>`)
                .join("")}</div>
        </details>
    </div>`;
}
function dossierSources(d) {
    const evidence = d.evidence;
    const records = latestSourceRecords(d.signals);
    const evidenceHtml = evidence
        ? `<div class="evidence-thesis"><span>EVIDENCE THESIS</span><p>${esc(evidence.thesis)}</p></div>
           <div class="source-list evidence-sources">${(evidence.sources || [])
               .map((source) => {
                   const url = safeURL(source.canonical_url);
                   return `<article class="source-record evidence-record">
                       <span class="source-icon">${sourceGlyph(source)}</span>
                       <div class="source-record-main">
                           <strong>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(source.title || source.source_name || source.provider_key)} ↗</a>` : esc(source.title || source.source_name || source.provider_key)}</strong>
                           <small>${esc(source.source_kind || source.provider_key)} · observed ${date(source.observed_at)}</small>
                       </div>
                   </article>`;
               })
               .join("")}</div>
           <p class="detail-foot">Evidence packet v${evidence.version} · ${esc(evidence.packet_sha256.slice(0, 12))}…</p>`
        : '<p class="alert">No qualified evidence packet exists for this snapshot yet.</p>';
    const recordsHtml = records.length
        ? records
              .map((record) => {
                  const url = safeURL(record.canonical_url);
                  return `<article class="source-record">
                      <span class="source-icon">${sourceGlyph(record)}</span>
                      <div class="source-record-main">
                          <strong>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(record.title || record.source_name || record.external_id)} ↗</a>` : esc(record.title || record.source_name || record.external_id)}</strong>
                          <small>${esc(record.provider_key)} · ${esc(record.source_kind)} · observed ${date(record.observed_at)}</small>
                          <span class="source-metrics">${esc(metricSummary(record.metrics))}</span>
                      </div>
                  </article>`;
              })
              .join("")
        : '<div class="empty compact-empty">No supporting source observations are stored in this dossier window.</div>';
    return `<div class="dossier-pane sources-pane">
        ${evidenceHtml}
        <div class="source-section-heading"><div><span>SUPPORTING SOURCE RECORDS</span><strong>${records.length}</strong></div><small>Raw observations behind this opportunity</small></div>
        <div class="source-record-list">${recordsHtml}</div>
        <p class="detail-foot">Source claims remain untrusted until verified. Media references do not grant reuse rights.</p>
    </div>`;
}
function dossierSignals(d) {
    return `<div class="dossier-pane signals-pane">
        <div class="graph-copy">
            <div><span>INTERACTIVE SIGNAL HISTORY</span><h4>See how the source moved.</h4></div>
            <p>Hover or focus a point for its exact observation. Switch source, metric, or time window without leaving the dossier.</p>
        </div>
        <div class="graph-controls">
            <label>Window<select id="hours"><option value="24">24 hours</option><option value="168">7 days</option><option value="720">30 days</option></select></label>
            <label>Source<select id="series"></select></label>
            <label>Metric<select id="metric"></select></label>
        </div>
        <div id="signal-chart" class="interactive-chart"></div>
        <p class="muted">Counters are stored observations, not interpolated live data. Missing observations are not treated as zero.${d.truncated ? " The newest 500 observations are shown." : ""}</p>
    </div>`;
}
function dossierEditorial(d, episodes) {
    return `<div class="dossier-pane editorial-pane">
        <div class="editorial-actions">
            <div><span>CONTROLLER DOSSIER</span><p>Export the exact stored dossier Katcha can hand to an AI controller.</p></div>
            <button type="button" id="export" class="handoff">Download dossier ↗</button>
        </div>
        <div class="block-heading">EDITORIAL HANDOFF</div>
        <p class="muted">Episode plans freeze qualified evidence before scripting so a later source change cannot silently rewrite the brief.</p>
        <div id="episodes">${episodes.length
            ? episodes
                  .map(
                      (episode) =>
                          `<div class="episode"><strong>${esc(episode.premise)}</strong><p>${esc(episode.status)} · ${esc(episode.stage)} · ${episode.evidence_frozen ? "Evidence frozen" : "No frozen evidence"}</p><button type="button" class="icon-button" data-episode="${esc(episode.id)}" ${!episode.evidence_frozen || !["planned", "scripting"].includes(episode.status) ? "disabled" : ""}>Start AI editorial</button></div>`,
                  )
                  .join("")
            : '<div class="empty compact-empty">No linked episode plan yet. Planning an episode with this opportunity ID will freeze the dossier evidence.</div>'}</div>
    </div>`;
}
function renderDossier(d, episodes, tab = state.dossierTab) {
    const o = d.opportunity;
    state.dossierTab = tab;
    const panes = {
        overview: () => dossierOverview(d),
        sources: () => dossierSources(d),
        signals: () => dossierSignals(d),
        editorial: () => dossierEditorial(d, episodes),
    };
    const selectedPane = panes[tab] ? tab : "overview";
    $("detail-panel").innerHTML = `
        <div class="dossier-shell">
            <div class="detail-top">
                <span>OPPORTUNITY DOSSIER</span>
                <button id="compare" class="icon-button">${state.compare.includes(o.id) ? "✓ Comparing" : "+ Compare"}</button>
            </div>
            <div class="dossier-title-row">
                <div>
                    <h3>${esc(d.topic)}</h3>
                    <div class="tagline"><span class="tag status">${esc(o.lifecycle)}</span>${d.tags.map((tag) => `<span class="tag">${esc(tag)}</span>`).join("")}</div>
                </div>
                <strong class="dossier-score">${(Number(o.calibrated_score ?? o.opportunity_score) * 100).toFixed(0)}<small>/100</small></strong>
            </div>
            <nav class="dossier-tabs" aria-label="Opportunity dossier">
                ${[
                    ["overview", "Overview"],
                    ["sources", `Sources ${latestSourceRecords(d.signals).length}`],
                    ["signals", "Signal graph"],
                    ["editorial", "Editorial"],
                ]
                    .map(([key, label]) => `<button type="button" data-dossier-tab="${key}" class="${selectedPane === key ? "active" : ""}">${label}</button>`)
                    .join("")}
            </nav>
            ${panes[selectedPane]()}
        </div>`;
    $("detail-panel").querySelectorAll("[data-dossier-tab]").forEach((button) => {
        button.onclick = () => renderDossier(d, episodes, button.dataset.dossierTab);
    });
    $("compare").onclick = () => {
        if (state.compare.includes(o.id))
            state.compare = state.compare.filter((id) => id !== o.id);
        else if (state.compare.length < 2) state.compare.push(o.id);
        else {
            status("Compare supports two opportunities at a time. Clear one first.");
            return;
        }
        renderDossier(d, episodes, selectedPane);
        renderComparison();
    };
    if (selectedPane === "signals") {
        $("hours").value = String(state.hours);
        $("hours").onchange = (event) => {
            state.hours = Number(event.target.value);
            state.dossierTab = "signals";
            selectTopic(o.id);
        };
        setupSeries(d.signals);
    }
    if (selectedPane === "editorial") {
        $("export").onclick = () => {
            const blob = new Blob([JSON.stringify(d, null, 2)], {
                type: "application/json",
            });
            const url = URL.createObjectURL(blob);
            const anchor = document.createElement("a");
            anchor.href = url;
            anchor.download = `katcha-${o.id}-dossier.json`;
            anchor.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        };
        $("episodes").onclick = async (event) => {
            const button = event.target.closest("[data-episode]");
            if (!button) return;
            const id = button.dataset.episode;
            if (state.pending.has(id)) return;
            state.pending.add(id);
            button.disabled = true;
            try {
                const result = await api(`${explorerURL()}/${o.id}/editorial`, {
                    method: "POST",
                    body: JSON.stringify({ episode_id: id }),
                });
                status(`Editorial workflow started: ${result.workflow_id}.`);
            } catch (error) {
                status(error.message);
                button.disabled = false;
            } finally {
                state.pending.delete(id);
            }
        };
    }
}
function setupSeries(signals) {
    const groups = new Map();
    signals.forEach((signal) => {
        const key = `${signal.provider_key}:${signal.external_id}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(signal);
    });
    if (!groups.size) {
        $("signal-chart").innerHTML =
            '<div class="empty compact-empty">No signal observations are stored for this dossier window.</div>';
        $("series").innerHTML = "";
        $("metric").innerHTML = "";
        return;
    }
    $("series").innerHTML = [...groups]
        .map(([key, rows]) => `<option value="${esc(key)}">${esc(rows[0].title || rows[0].source_name || key)}</option>`)
        .join("");
    function metrics() {
        const rows = groups.get($("series").value) || [];
        const keys = [...new Set(rows.flatMap((row) => Object.keys(row.metrics || {})))];
        $("metric").innerHTML = keys.map((key) => `<option value="${esc(key)}">${esc(key.replaceAll("_", " "))}</option>`).join("");
        draw();
    }
    function draw() {
        const key = $("metric").value;
        const points = (groups.get($("series").value) || [])
            .filter((row) => Number.isFinite(Number(row.metrics?.[key])))
            .sort((a, b) => new Date(a.observed_at) - new Date(b.observed_at));
        if (points.length < 2) {
            $("signal-chart").innerHTML =
                '<div class="empty compact-empty">At least two observations of the selected metric are needed to draw change.</div>';
            return;
        }
        const width = 640;
        const height = 220;
        const padX = 28;
        const padY = 24;
        const values = points.map((point) => Number(point.metrics[key]));
        const min = Math.min(...values);
        const max = Math.max(...values);
        const start = +new Date(points[0].observed_at);
        const end = +new Date(points.at(-1).observed_at);
        const x = (point, index) =>
            end === start
                ? padX + (index / Math.max(1, points.length - 1)) * (width - padX * 2)
                : padX + ((+new Date(point.observed_at) - start) / (end - start)) * (width - padX * 2);
        const y = (value) =>
            max === min
                ? height / 2
                : height - padY - ((value - min) / (max - min)) * (height - padY * 2);
        const coords = points.map((point, index) => `${x(point, index)},${y(values[index])}`).join(" ");
        const circles = points
            .map(
                (point, index) =>
                    `<circle tabindex="0" role="button" aria-label="${esc(key)} ${values[index]} at ${date(point.observed_at)}" data-chart-point="${index}" cx="${x(point, index)}" cy="${y(values[index])}" r="4.5"></circle>`,
            )
            .join("");
        $("signal-chart").innerHTML = `
            <div class="graph-wrap">
                <svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(key)} history">
                    <line class="chart-grid" x1="${padX}" y1="${padY}" x2="${padX}" y2="${height - padY}"></line>
                    <line class="chart-grid" x1="${padX}" y1="${height - padY}" x2="${width - padX}" y2="${height - padY}"></line>
                    <polyline points="${coords}" fill="none"></polyline>
                    ${circles}
                </svg>
                <div class="chart-tooltip" hidden></div>
            </div>
            <div class="chart-axis"><span>${date(start)}<br><strong>${values[0].toLocaleString()}</strong></span><span>${date(end)}<br><strong>${values.at(-1).toLocaleString()}</strong></span></div>`;
        const host = $("signal-chart");
        const tooltip = host.querySelector(".chart-tooltip");
        const showPoint = (circle) => {
            const index = Number(circle.dataset.chartPoint);
            const point = points[index];
            tooltip.hidden = false;
            tooltip.innerHTML = `<strong>${values[index].toLocaleString()}</strong><span>${esc(key.replaceAll("_", " "))}</span><small>${esc(date(point.observed_at))}</small>`;
            tooltip.style.left = `${Math.min(82, Math.max(8, (Number(circle.getAttribute("cx")) / width) * 100))}%`;
            tooltip.style.top = `${Math.max(8, (Number(circle.getAttribute("cy")) / height) * 100 - 4)}%`;
        };
        host.querySelectorAll("[data-chart-point]").forEach((circle) => {
            circle.addEventListener("mouseenter", () => showPoint(circle));
            circle.addEventListener("focus", () => showPoint(circle));
            circle.addEventListener("mouseleave", () => (tooltip.hidden = true));
            circle.addEventListener("blur", () => (tooltip.hidden = true));
        });
    }
    $("series").onchange = metrics;
    $("metric").onchange = draw;
    metrics();
}
function renderComparison() {
    const rows = state.compare
        .map((id) => state.rows.find((r) => r.opportunity.id === id))
        .filter(Boolean);
    $("comparison").hidden = !rows.length;
    $("comparison").innerHTML =
        `<div class="comparison-head"><h3>Compare ${rows.length}/2</h3><button id="clear-compare">Clear</button></div><table class="comparison-table"><thead><tr><th>Signal</th>${rows.map((r) => `<th>${esc(r.topic)}</th>`).join("")}</tr></thead><tbody>${[
            ["Baseline", "opportunity_score"],
            ["Calibrated", "calibrated_score"],
            ["Confidence", "confidence"],
        ]
            .map(
                ([label, key]) =>
                    `<tr><th>${label}</th>${rows.map((r) => `<td>${pct(r.opportunity[key])}</td>`).join("")}</tr>`,
            )
            .join(
                "",
            )}<tr><th>Lifecycle</th>${rows.map((r) => `<td>${esc(r.opportunity.lifecycle)}</td>`).join("")}</tr></tbody></table><p class="muted">Scores are stored backend outputs. Snapshot times may differ.</p>`;
    $("clear-compare").onclick = () => {
        state.compare = [];
        renderComparison();
        if ($("compare")) $("compare").textContent = "+ Compare";
    };
}
async function refreshIntelligence({ automatic = false } = {}) {
    if (!state.channel) return null;
    const button = $("refresh");
    const previous = button.textContent;
    button.disabled = true;
    button.textContent = "Refreshing…";
    try {
        const result = await api(`${channelURL()}/refresh`, {
            method: "POST",
            body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
        });
        const interests = normalizeInterests(state.watch?.interests || []);
        const message = `Refresh started for ${interests.join(", ") || "this channel"}. Katcha is rescoring stored signals while discovery continues collecting new sources.`;
        status(message);
        feedback(message, "success");
        return result;
    } catch (error) {
        status(error.message);
        feedback(error.message, "error");
        if (!automatic) throw error;
        return null;
    } finally {
        button.textContent = previous;
        button.disabled = !state.channel;
    }
}
function watchBody(interests) {
    const old = state.watch || {};
    return {
        interests,
        excluded_terms: old.excluded_terms || [],
        entities: old.entities || [],
        platforms: old.platforms || [],
        languages: old.languages || [],
        regions: old.regions || [],
        source_weights: old.source_weights || {},
        freshness_horizon_hours: old.freshness_horizon_hours || 72,
        min_confidence: old.min_confidence ?? 0.45,
        opportunity_threshold: old.opportunity_threshold ?? 0.55,
        metadata: old.watch_metadata || {},
        actor: "explorer",
    };
}
async function saveInterests(interests, actionMessage) {
    const next = normalizeInterests(interests);
    if (!next.length) {
        feedback("Keep at least one channel interest so Katcha knows what to watch.", "error");
        return;
    }
    for (const id of ["interest-input", "add-interest", "reset-interests", "refresh"])
        $(id).disabled = true;
    feedback("Saving interests…", "working");
    try {
        state.watch = await api(`${channelURL()}/watch-profile`, {
            method: "POST",
            body: JSON.stringify(watchBody(next)),
        });
        renderInterests();
        $("interest-input").value = "";
        feedback(`${actionMessage}. Starting an intelligence refresh…`, "working");
        await refreshIntelligence({ automatic: true });
    } catch (error) {
        status(error.message);
        feedback(error.message, "error");
    } finally {
        $("interest-input").disabled = !state.channel;
        $("add-interest").disabled = !state.channel;
        $("refresh").disabled = !state.channel;
        renderInterests();
    }
}
$("connect-form").onsubmit = connect;
$("channel").onchange = loadChannel;
$("reload").onclick = loadChannel;
$("search").oninput = renderBoard;
$("sort").onchange = renderBoard;
$("topic-list").onclick = (event) => {
    const row = event.target.closest("[data-topic]");
    if (row) selectTopic(row.dataset.topic);
};
$("watch-form").onsubmit = async (event) => {
    event.preventDefault();
    const additions = normalizeInterests($("interest-input").value.split(","));
    if (!additions.length) {
        feedback("Type an interest to add.", "error");
        return;
    }
    const current = normalizeInterests(state.watch?.interests || []);
    const existing = new Set(current.map((item) => item.toLowerCase()));
    const fresh = additions.filter((item) => !existing.has(item.toLowerCase()));
    if (!fresh.length) {
        feedback("That interest is already being watched.", "error");
        return;
    }
    await saveInterests(
        [...current, ...fresh],
        `Added ${fresh.map((item) => `“${item}”`).join(", ")}`,
    );
};
$("interest-chips").onclick = async (event) => {
    const button = event.target.closest("[data-remove-interest]");
    if (!button) return;
    const current = normalizeInterests(state.watch?.interests || []);
    if (current.length <= 1) {
        feedback("Keep at least one interest. Add another before removing this one.", "error");
        return;
    }
    const target = button.dataset.removeInterest;
    await saveInterests(
        current.filter((item) => item.toLowerCase() !== target.toLowerCase()),
        `Removed “${target}”`,
    );
};
document.querySelectorAll("[data-suggested-interest]").forEach((button) => {
    button.onclick = async () => {
        const suggestion = button.dataset.suggestedInterest;
        const current = normalizeInterests(state.watch?.interests || []);
        if (current.some((item) => item.toLowerCase() === suggestion.toLowerCase())) {
            feedback(`“${suggestion}” is already being watched.`, "error");
            return;
        }
        await saveInterests([...current, suggestion], `Added “${suggestion}”`);
    };
});
$("reset-interests").onclick = async () => {
    if (!state.initialInterests.length) {
        feedback("There is no loaded interest set to restore.", "error");
        return;
    }
    await saveInterests(state.initialInterests, "Restored the interests loaded when you opened this channel");
};
$("refresh").onclick = () => refreshIntelligence();
