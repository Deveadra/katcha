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
    $("interests").value = "";
    $("source-health").textContent = "";
    renderEditPerformance();
    renderBoard();
    renderComparison();
    $("detail-panel").innerHTML =
        '<div class="empty">Select an opportunity.</div>';
    for (const id of ["interests", "save-watch", "refresh", "reload"])
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
        state.editPerformance = editPerformance;
        state.rows = rows;
        $("interests").value = (watch?.interests || []).join(", ");
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
                ? `${rows.length} current topics loaded. Ranked from stored opportunity snapshots.`
                : "No current opportunities. Configure interests and discovery watches, then refresh intelligence.",
        );
        if (rows.length) await selectTopic(rows[0].opportunity.id);
    } catch (error) {
        if (epoch === state.epoch) status(error.message);
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
    renderBoard();
    $("detail-panel").innerHTML = '<div class="empty">Loading evidence…</div>';
    try {
        const base = explorerURL();
        const [dossier, episodes] = await Promise.all([
            api(`${base}/${id}?hours=${state.hours}`),
            api(`${base}/${id}/episodes`),
        ]);
        if (epoch !== state.detailEpoch) return;
        state.dossier = dossier;
        renderDossier(dossier, episodes);
    } catch (error) {
        if (epoch === state.detailEpoch)
            $("detail-panel").innerHTML =
                `<div class="empty">${esc(error.message)}</div>`;
    }
}
function renderDossier(d, episodes) {
    const o = d.opportunity;
    $("detail-panel").innerHTML =
        `<div class="detail-top"><span>OPPORTUNITY DOSSIER</span><button id="compare" class="icon-button">${state.compare.includes(o.id) ? "✓ Comparing" : "+ Compare"}</button></div><h3>${esc(d.topic)}</h3><div class="tagline"><span class="tag status">${esc(o.lifecycle)}</span>${d.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div><div class="metrics"><div class="metric"><span>BASELINE SCORE</span><strong>${pct(o.opportunity_score)}</strong></div><div class="metric"><span>CONFIDENCE</span><strong>${pct(o.confidence)}</strong></div><div class="metric"><span>CALIBRATED</span><strong>${pct(o.calibrated_score)}</strong></div></div><p class="muted">Snapshot ${date(o.created_at)} · expires ${date(o.expires_at)}</p><div class="block-heading">WHY THIS RANKS</div><ul class="why-list">${o.reasons.map((r) => `<li><span>↗</span>${esc(r)}</li>`).join("")}</ul><details><summary>Inspect score components</summary><div class="components">${Object.entries(
            o.components,
        )
            .map(
                ([k, v]) =>
                    `<div><span>${esc(k.replaceAll("_", " "))}</span><strong>${Number(v).toFixed(3)}</strong></div>`,
            )
            .join(
                "",
            )}</div></details><div class="chart-card"><div class="chart-head">Observed source history <span>as of this snapshot</span></div><div class="field-grid"><label>Window<select id="hours"><option value="24">24 hours</option><option value="168">7 days</option><option value="720">30 days</option></select></label><label>Metric<select id="metric"></select></label><label>Source<select id="series"></select></label></div><div id="signal-chart"></div><p class="muted">Cumulative counters per source. Missing observations are not zero.${d.truncated ? " Showing the newest 500 observations; history is truncated." : ""}</p></div><div class="source-head"><span class="block-heading">EVIDENCE PACKET</span></div>${
            d.evidence
                ? `<p class="detail-summary">${esc(d.evidence.thesis)}</p><div class="source-list">${d.evidence.sources
                      .map((s) => {
                          const url = safeURL(s.canonical_url);
                          return `<div class="source"><span class="source-icon">↗</span><div><strong>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.source_name || s.provider_key)} ↗</a>` : esc(s.title || s.provider_key)}</strong><small>${esc(s.source_kind)} · observed ${date(s.observed_at)}</small></div></div>`;
                      })
                      .join(
                          "",
                      )}</div><p class="detail-foot">Packet v${d.evidence.version} · ${esc(d.evidence.packet_sha256.slice(0, 12))}…<br>Source claims require verification. Media references do not grant reuse rights.</p>`
                : '<p class="alert">No qualified evidence packet exists for this snapshot. New episode planning requires a qualified packet.</p>'
        }<button type="button" id="export" class="handoff">Download controller dossier ↗</button><details open><summary>AI editorial handoff</summary><p class="muted">Planned episodes linked to this opportunity. Creating a plan through the episode API freezes qualified evidence and checks analyzed clips and acquisition eligibility.</p><div id="episodes">${episodes.length ? episodes.map((e) => `<div class="episode"><strong>${esc(e.premise)}</strong><p>${esc(e.status)} · ${esc(e.stage)} · ${e.evidence_frozen ? "Evidence frozen" : "No frozen evidence"}</p><button type="button" class="icon-button" data-episode="${esc(e.id)}" ${!e.evidence_frozen || !["planned", "scripting"].includes(e.status) ? "disabled" : ""}>Start AI editorial</button></div>`).join("") : '<p class="muted">No linked episode plans yet. The controller can create one with this opportunity ID via POST /v1/short-episodes.</p>'}</div></details>`;
    $("hours").value = state.hours;
    $("hours").onchange = (e) => {
        state.hours = Number(e.target.value);
        selectTopic(o.id);
    };
    $("compare").onclick = () => {
        if (state.compare.includes(o.id))
            state.compare = state.compare.filter((x) => x !== o.id);
        else if (state.compare.length < 2) state.compare.push(o.id);
        else {
            status("Clear one comparison before adding another.");
            return;
        }
        $("compare").textContent = state.compare.includes(o.id)
            ? "✓ Comparing"
            : "+ Compare";
        renderComparison();
    };
    $("export").onclick = () => {
        const blob = new Blob([JSON.stringify(d, null, 2)], {
            type: "application/json",
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `katcha-${o.id}.json`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    $("episodes").onclick = async (e) => {
        const button = e.target.closest("[data-episode]");
        if (!button) return;
        const id = button.dataset.episode;
        if (state.pending.has(id)) return;
        state.pending.add(id);
        button.disabled = true;
        const base = explorerURL();
        try {
            const result = await api(`${base}/${o.id}/editorial`, {
                method: "POST",
                body: JSON.stringify({ episode_id: id }),
            });
            status(
                `Editorial workflow accepted: ${result.workflow_id}. Reload to inspect progress.`,
            );
        } catch (error) {
            status(error.message);
            button.disabled = false;
        } finally {
            state.pending.delete(id);
        }
    };
    setupSeries(d.signals);
}
function setupSeries(signals) {
    const groups = new Map();
    signals.forEach((s) => {
        const key = `${s.provider_key}:${s.external_id}`;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(s);
    });
    $("series").innerHTML = [...groups]
        .map(
            ([key, rows]) =>
                `<option value="${esc(key)}">${esc(rows[0].title || key)}</option>`,
        )
        .join("");
    function metrics() {
        const rows = groups.get($("series").value) || [];
        const keys = [...new Set(rows.flatMap((r) => Object.keys(r.metrics)))];
        $("metric").innerHTML = keys
            .map((k) => `<option>${esc(k)}</option>`)
            .join("");
        draw();
    }
    function draw() {
        const key = $("metric").value;
        const points = (groups.get($("series").value) || [])
            .filter((r) => Number.isFinite(r.metrics[key]))
            .sort((a, b) => new Date(a.observed_at) - new Date(b.observed_at));
        if (points.length < 2) {
            $("signal-chart").innerHTML =
                '<p class="muted">At least two observations of the same metric are needed to chart change.</p>';
            return;
        }
        const values = points.map((p) => Number(p.metrics[key]));
        const min = Math.min(...values),
            max = Math.max(...values);
        const start = +new Date(points[0].observed_at),
            end = +new Date(points.at(-1).observed_at);
        const coords = points
            .map(
                (p, i) =>
                    `${end === start ? (i / (points.length - 1)) * 300 : ((+new Date(p.observed_at) - start) / (end - start)) * 300},${max === min ? 45 : 82 - ((values[i] - min) / (max - min)) * 72}`,
            )
            .join(" ");
        $("signal-chart").innerHTML =
            `<svg class="chart" viewBox="0 0 300 90" role="img" aria-label="${esc(key)} from ${min} to ${max}"><polyline points="${coords}" fill="none" stroke="#c8f36a" stroke-width="2"/></svg><div class="chart-axis"><span>${date(start)}<br>${values[0].toLocaleString()}</span><span>${date(end)}<br>${values.at(-1).toLocaleString()}</span></div>`;
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
$("connect-form").onsubmit = connect;
$("channel").onchange = loadChannel;
$("reload").onclick = loadChannel;
$("search").oninput = renderBoard;
$("sort").onchange = renderBoard;
$("topic-list").onclick = (e) => {
    const row = e.target.closest("[data-topic]");
    if (row) selectTopic(row.dataset.topic);
};
$("watch-form").onsubmit = async (e) => {
    e.preventDefault();
    const interests = [
        ...new Set(
            $("interests")
                .value.split(",")
                .map((x) => x.trim())
                .filter(Boolean),
        ),
    ];
    if (!interests.length) {
        status("Enter at least one interest.");
        return;
    }
    const old = state.watch || {};
    const body = {
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
    $("save-watch").disabled = true;
    try {
        await api(`${channelURL()}/watch-profile`, {
            method: "POST",
            body: JSON.stringify(body),
        });
        await loadChannel();
        status(
            "Watch version saved. Refresh intelligence to score using these interests.",
        );
    } catch (error) {
        status(error.message);
    } finally {
        $("save-watch").disabled = !state.channel;
    }
};
$("refresh").onclick = async () => {
    const button = $("refresh");
    button.disabled = true;
    try {
        const result = await api(`${channelURL()}/refresh`, {
            method: "POST",
            body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
        });
        status(
            `Refresh accepted: ${result.workflow_id}. This scores stored signals; discovery watches collect new sources. Reload after the worker completes.`,
        );
    } catch (error) {
        status(error.message);
    } finally {
        button.disabled = !state.channel;
    }
};
