const state = {
    token: sessionStorage.getItem("katcha.controlToken") || "",
    channels: [],
    channelId: new URLSearchParams(location.search).get("channel") || sessionStorage.getItem("katcha.channel") || "",
    data: {},
    epoch: 0,
};
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" })[c]);
const friendly = (value) => String(value || "—").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
const number = (value) => new Intl.NumberFormat("en-US").format(Number(value || 0));
const compact = (value) => new Intl.NumberFormat("en-US", { notation:"compact", maximumFractionDigits:1 }).format(Number(value || 0));
const currency = (value) => Number.isFinite(Number(value)) ? new Intl.NumberFormat("en-US", { style:"currency", currency:"USD", maximumFractionDigits:2 }).format(Number(value)) : "—";
const date = (value) => value ? new Date(value).toLocaleString() : "—";
function setStatus(message, error=false) { $("status").textContent = message || ""; $("status").className = error ? "home-status error" : "home-status"; }
async function api(path, options={}) {
    const response = await fetch(path, {
        ...options,
        headers: {
            ...(options.body ? {"Content-Type":"application/json"} : {}),
            ...(state.token ? {Authorization:"Bearer " + state.token} : {}),
            ...options.headers,
        },
    });
    if (!response.ok) {
        let body; try { body = await response.json(); } catch {}
        throw new Error(typeof body?.detail === "string" ? body.detail : "Request failed (" + response.status + ")");
    }
    return response.status === 204 ? null : response.json();
}
async function safe(path) {
    try { return {ok:true, value:await api(path)}; }
    catch (error) { return {ok:false, error}; }
}
function channelName(row) { return row?.profile_metadata?.channel_title || row?.profile_metadata?.name || row?.profile_metadata?.channel_handle || row?.id || "Channel"; }
function channelHref(path, extra="") {
    const join = path.includes("?") ? "&" : "?";
    return path + join + "channel=" + encodeURIComponent(state.channelId) + extra;
}
function applyContextLinks() {
    const map = {
        "open-production": "/editing",
        "open-growth": "/channels#growth",
        "open-trends": "/explorer",
        "open-ai": "/ai?focus=chat",
        "pulse-production": "/editing",
        "pulse-failures": "/editing#editorial-pipeline",
        "pulse-clips": "/clips",
        "pulse-margin": "/channels#monetization",
    };
    for (const [id, path] of Object.entries(map)) {
        const node = $(id);
        if (!node || !state.channelId) continue;
        if (path.includes("#")) {
            const [base, hash] = path.split("#");
            node.href = channelHref(base) + "#" + hash;
        } else {
            node.href = channelHref(path);
        }
    }
}
function isAttention(row) {
    const status = String(row?.status || "").toLowerCase();
    return Boolean(row?.error) || /(fail|error|dead_letter|blocked)/.test(status);
}
function isDone(row) {
    return ["published","completed","rejected","cancelled"].includes(String(row?.status || "").toLowerCase());
}
function attentionItems() {
    const out = [];
    for (const row of state.data.episodes || []) {
        if (isAttention(row)) out.push({
            severity:"high",
            title: row.premise || "Episode needs attention",
            note: "Production · " + friendly(row.stage || row.status) + (row.error ? " · " + row.error : ""),
            href: channelHref("/editing") + "#editorial-pipeline",
        });
    }
    for (const row of state.data.productions || []) {
        if (isAttention(row)) out.push({
            severity:"high",
            title: row.render_manifest?.title_angle || row.title || "Production needs attention",
            note: "Production · " + friendly(row.status),
            href: channelHref("/editing") + "#editorial-pipeline",
        });
    }
    for (const row of state.data.publications || []) {
        if (String(row.status).toLowerCase() === "failed") out.push({
            severity:"high",
            title: row.title || "Publication failed",
            note: "Publishing failed. Resolve before scaling unattended output.",
            href: channelHref("/channels") + "#content",
        });
    }
    const clipFailures = Number(state.data.clipSummary?.failed || 0);
    if (clipFailures) out.push({
        severity:"high",
        title: clipFailures + " clip" + (clipFailures === 1 ? "" : "s") + " failed",
        note:"Inspect source or analysis failures in the Clip Library.",
        href:channelHref("/clips"),
    });
    const growth = state.data.summary?.growth;
    if (growth?.ai?.stage && growth.ai.stage !== "ads_thresholds_met") {
        const priority = growth.ai.priority_metrics?.[0];
        out.push({
            severity:"growth",
            title: priority ? "Monetization focus: " + friendly(priority) : "Monetization progress needs data",
            note:"Katcha is optimizing the " + friendly(growth.ai.selected_path || "balanced") + " path at " + friendly(growth.ai.pace || "aggressive") + " pace.",
            href:channelHref("/channels") + "#growth",
        });
    }
    const economics = state.data.summary?.economics;
    const effective = Number(economics?.effective_budget_usd || 0);
    const headroom = Number(economics?.budget_headroom_usd || 0);
    if (effective > 0 && headroom / effective < 0.2) out.push({
        severity:"growth",
        title:"AI budget headroom is below 20%",
        note:currency(headroom) + " remains in the tracked budget window.",
        href:channelHref("/channels") + "#settings",
    });
    return out.slice(0,8);
}
function renderAttention() {
    const items = attentionItems();
    $("attention-count").textContent = String(items.length);
    $("attention-list").innerHTML = items.length ? items.map((item) =>
        '<article class="attention-item severity-' + esc(item.severity) + '"><div><strong>' + esc(item.title) + '</strong><small>' + esc(item.note) + '</small></div><a href="' + esc(item.href) + '">Open ↗</a></article>'
    ).join("") : '<div class="home-empty">Nothing currently requires operator intervention.</div>';
}
function rankedOpportunities() {
    return [...(state.data.trends || [])].sort((a,b) => {
        const score = (row) => Number(row?.opportunity?.calibrated_score ?? row?.opportunity?.opportunity_score ?? 0);
        return score(b) - score(a);
    });
}
function renderNextMove() {
    const attention = attentionItems();
    if (attention.some((item) => item.severity === "high")) {
        const item = attention.find((row) => row.severity === "high");
        $("next-move").className = "next-move";
        $("next-move").innerHTML = '<strong>' + esc(item.title) + '</strong><p>' + esc(item.note) + '</p><div class="next-meta"><span>Operator action</span><span>Highest priority</span></div><a href="' + esc(item.href) + '">Resolve this first ↗</a>';
        return;
    }
    const top = rankedOpportunities()[0];
    if (top) {
        const score = Number(top.opportunity?.calibrated_score ?? top.opportunity?.opportunity_score ?? 0);
        $("next-move").className = "next-move";
        $("next-move").innerHTML = '<strong>' + esc(top.topic || "Top trend opportunity") + '</strong><p>' + esc((top.opportunity?.reasons || [])[0] || "This is the strongest stored opportunity for the active channel.") + '</p><div class="next-meta"><span>Opportunity ' + esc((score*100).toFixed(0)) + '</span><span>' + esc(friendly(top.opportunity?.lifecycle || "active")) + '</span></div><a href="' + esc(channelHref("/explorer")) + '">Inspect opportunity ↗</a>';
        return;
    }
    const growth = state.data.summary?.growth;
    if (growth?.ai?.priority_metrics?.length) {
        $("next-move").className = "next-move";
        $("next-move").innerHTML = '<strong>Close the ' + esc(friendly(growth.ai.priority_metrics[0])) + ' gap</strong><p>Katcha has identified this as the channel\'s strongest current monetization pressure.</p><a href="' + esc(channelHref("/channels") + "#growth") + '">Open growth plan ↗</a>';
        return;
    }
    $("next-move").className = "next-move home-empty";
    $("next-move").textContent = "No urgent operator action is currently evident.";
}
function renderPulse() {
    const activeEpisodes = (state.data.episodes || []).filter((row) => !isDone(row) && !isAttention(row)).length;
    const activeProductions = (state.data.productions || []).filter((row) => !isDone(row) && !isAttention(row)).length;
    const failures = attentionItems().filter((row) => row.severity === "high").length;
    $("metric-production").textContent = number(activeEpisodes + activeProductions);
    $("metric-failures").textContent = number(failures);
    $("metric-clips").textContent = number(state.data.clipSummary?.hot || 0);
    $("metric-margin").textContent = currency(state.data.summary?.economics?.contribution_margin_usd);
    $("metric-production-note").textContent = activeEpisodes + " episode" + (activeEpisodes===1?"":"s") + " · " + activeProductions + " standalone";
    $("metric-failures-note").textContent = failures ? "Operator attention is required" : "No blocking failures found";
    $("metric-clips-note").textContent = number(state.data.clipSummary?.total || 0) + " total searchable clips";
}
function renderWork() {
    const rows = [
        ...(state.data.episodes || []).map((row) => ({kind:"Episode", title:row.premise || "Ranked episode", status:row.status, stage:row.stage, updated_at:row.updated_at, attention:isAttention(row)})),
        ...(state.data.productions || []).map((row) => ({kind:"Production", title:row.render_manifest?.title_angle || row.title || "Standalone production", status:row.status, stage:row.stage, updated_at:row.updated_at, attention:isAttention(row)})),
    ].filter((row) => !isDone(row)).sort((a,b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0)).slice(0,6);
    $("work-list").innerHTML = rows.length ? rows.map((row) =>
        '<article class="work-item"><div><strong>' + esc(row.title) + '</strong><small>' + esc(row.kind + " · " + friendly(row.stage || row.status) + " · " + date(row.updated_at)) + '</small></div><span class="work-state">' + esc(row.attention ? "ATTENTION" : friendly(row.status)) + '</span></article>'
    ).join("") : '<div class="home-empty">No active productions for this channel.</div>';
}
function renderGrowth() {
    const summary = state.data.summary || {};
    const growth = summary.growth;
    const economics = summary.economics;
    const automation = summary.automation;
    const stage = growth?.ai?.stage ? friendly(growth.ai.stage) : "Needs data";
    const priority = growth?.ai?.priority_metrics?.[0] ? friendly(growth.ai.priority_metrics[0]) : "No current gap";
    $("growth").innerHTML =
        '<div class="growth-grid">' +
            '<div class="growth-cell"><span>Monetization stage</span><strong>' + esc(stage) + '</strong></div>' +
            '<div class="growth-cell"><span>Primary growth gap</span><strong>' + esc(priority) + '</strong></div>' +
            '<div class="growth-cell"><span>Revenue</span><strong>' + esc(currency(economics?.revenue_usd)) + '</strong></div>' +
            '<div class="growth-cell"><span>Budget headroom</span><strong>' + esc(currency(economics?.budget_headroom_usd)) + '</strong></div>' +
        '</div>' +
        '<div class="growth-priority">Automation: <strong>' + esc(friendly(automation?.level || "not measured")) + '</strong>' +
        (automation?.eligible_for_next_level ? " · Evidence clears the next promotion gate." : " · Current safeguards remain active.") + '</div>';
}
function renderOpportunity() {
    const row = rankedOpportunities()[0];
    if (!row) { $("opportunity").innerHTML = '<div class="home-empty">No ranked trend evidence is currently available.</div>'; return; }
    const opportunity = row.opportunity || {};
    const score = Number(opportunity.calibrated_score ?? opportunity.opportunity_score ?? 0);
    $("opportunity").innerHTML =
        '<div class="opportunity-card"><h3>' + esc(row.topic || "Opportunity") + '</h3>' +
        '<div class="opportunity-score"><span>Score ' + esc((score*100).toFixed(0)) + '</span><span>Confidence ' + esc(((Number(opportunity.confidence || 0))*100).toFixed(0)) + '</span><span>' + esc(friendly(opportunity.lifecycle || "active")) + '</span></div>' +
        '<p>' + esc((opportunity.reasons || [])[0] || "Stored trend evidence supports this opportunity.") + '</p></div>';
}
function renderAI() {
    const row = state.data.observability;
    if (!row) { $("ai-observability").innerHTML = '<div class="home-empty">AI observability is unavailable for this principal or channel.</div>'; return; }
    $("ai-observability").innerHTML =
        '<div class="ai-grid">' +
        '<div class="ai-cell"><span>Requests · 24h</span><strong>' + esc(number(row.request_count)) + '</strong></div>' +
        '<div class="ai-cell"><span>P95 latency</span><strong>' + esc(number(row.p95_latency_ms)) + ' ms</strong></div>' +
        '<div class="ai-cell"><span>Estimated cost</span><strong>' + esc(currency(row.estimated_cost_usd)) + '</strong></div>' +
        '<div class="ai-cell"><span>Typed context</span><strong>' + esc(number(row.typed_context_request_count)) + '</strong></div>' +
        '</div>';
}
function renderAll() {
    applyContextLinks();
    renderAttention();
    renderNextMove();
    renderPulse();
    renderWork();
    renderGrowth();
    renderOpportunity();
    renderAI();
}
async function loadChannel() {
    const epoch = ++state.epoch;
    state.channelId = $("channel").value;
    if (!state.channelId) return;
    sessionStorage.setItem("katcha.channel", state.channelId);
    applyContextLinks();
    setStatus("Loading channel operations…");
    const channel = state.channels.find((row) => row.id === state.channelId);
    const id = encodeURIComponent(state.channelId);
    const requests = {
        summary: safe("/v1/channels/" + id),
        productions: safe("/v1/productions?limit=100&channel_profile_id=" + id),
        episodes: safe("/v1/short-episodes?channel_profile_id=" + id + "&limit=100"),
        clipSummary: safe("/v1/clips/library/summary?channel_profile_id=" + id),
        trends: safe("/v1/channels/" + id + "/trends/explorer"),
        observability: safe("/v1/ai/observability?channel_profile_id=" + id + "&hours=24"),
        publications: channel?.youtube_connection_id ? safe("/v1/publications?limit=100&youtube_connection_id=" + encodeURIComponent(channel.youtube_connection_id)) : Promise.resolve({ok:true,value:[]}),
    };
    const entries = await Promise.all(Object.entries(requests).map(async ([key,promise]) => [key, await promise]));
    if (epoch !== state.epoch) return;
    state.data = Object.fromEntries(entries.map(([key,result]) => [key, result.ok ? result.value : null]));
    state.data.publications ||= [];
    state.data.productions ||= [];
    state.data.episodes ||= [];
    state.data.trends ||= [];
    renderAll();
    const failed = entries.filter(([,result]) => !result.ok);
    $("connection-state").textContent = failed.length ? "PARTIAL" : "CONNECTED";
    $("connection-state").className = failed.length ? "simulation warning" : "simulation connected";
    setStatus(failed.length ? "Loaded available channel state. " + failed.length + " optional section" + (failed.length===1?" is":"s are") + " unavailable." : "Channel operations are current.");
}
async function connect(event) {
    event?.preventDefault();
    state.token = $("token").value.trim() || state.token;
    $("token").value = "";
    if (state.token) sessionStorage.setItem("katcha.controlToken", state.token);
    $("connection-state").textContent = "CONNECTING";
    setStatus("Loading channel workspaces…");
    try {
        state.channels = await api("/v1/channels");
        $("channel").innerHTML = '<option value="">Select a channel</option>' + state.channels.map((row) => '<option value="' + esc(row.id) + '">' + esc(channelName(row)) + ' · ' + esc(row.status || "unknown") + '</option>').join("");
        $("channel").disabled = false;
        $("refresh").disabled = false;
        const preferred = state.channelId && state.channels.some((row) => row.id === state.channelId) ? state.channelId : state.channels.find((row) => row.status === "active")?.id || state.channels[0]?.id || "";
        state.channelId = preferred;
        $("channel").value = preferred;
        if (preferred) await loadChannel();
        else { $("connection-state").textContent = "CONNECTED"; setStatus("Connected. No channels are configured yet."); }
    } catch (error) {
        $("connection-state").textContent = "DISCONNECTED";
        $("connection-state").className = "simulation";
        setStatus(error.message, true);
    }
}
$("connect-form").addEventListener("submit", connect);
$("channel").addEventListener("change", loadChannel);
$("refresh").addEventListener("click", loadChannel);
if (state.token) connect();
