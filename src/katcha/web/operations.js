const $ = (id) => document.getElementById(id);
const state = {
    token: "",
    allChannels: [],
    overview: null,
    loading: false,
};

function escapeHTML(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function friendly(value) {
    return String(value || "unknown")
        .replaceAll("_", " ")
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function compactNumber(value) {
    if (value == null) return "—";
    const number = Number(value);
    return new Intl.NumberFormat("en-US", {
        notation: Math.abs(number) >= 10000 ? "compact" : "standard",
        maximumFractionDigits: 1,
    }).format(number);
}

function currency(value) {
    if (value == null) return "—";
    return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
    }).format(Number(value));
}

function percent(value) {
    if (value == null) return "—";
    return Number(value).toFixed(1) + "%";
}

function when(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "—";
    return new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
    }).format(date);
}

function setStatus(message, kind = "") {
    const host = $("status");
    host.textContent = message;
    host.className = "ops-status" + (kind ? " " + kind : "");
}

async function api(path) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;
    const response = await fetch(path, { headers, cache: "no-store" });
    let payload = null;
    try {
        payload = await response.json();
    } catch {
        payload = null;
    }
    if (!response.ok) {
        const detail = typeof payload?.detail === "string"
            ? payload.detail
            : payload?.error || "Request failed (" + response.status + ")";
        const error = new Error(detail);
        error.status = response.status;
        throw error;
    }
    return payload;
}

function channelName(id) {
    return state.allChannels.find((row) => row.id === id)?.title || "Channel";
}

function askHref(row) {
    const prompt = row.state === "attention"
        ? "Explain why this item needs attention and what the safest next action is."
        : "Explain this workflow's current state and what Katcha is doing next.";
    const params = new URLSearchParams({
        focus: "chat",
        channel: row.channel_profile_id,
        prompt,
    });
    return "/ai?" + params.toString();
}

function empty(title, copy) {
    return '<div class="ops-empty"><strong>' + escapeHTML(title) + '</strong>' +
        escapeHTML(copy) + "</div>";
}

function renderWork(items, hostId, stateClass) {
    const host = $(hostId);
    if (!items.length) {
        host.innerHTML = stateClass === "attention"
            ? empty("Nothing needs intervention", "Failures, blocked work, and recovery decisions will appear here.")
            : empty("No work in motion", "Queued and active production or publication work will appear here.");
        return;
    }
    host.innerHTML = items.map((row) => {
        const channel = channelName(row.channel_profile_id);
        return '<article class="ops-row">' +
            '<div class="ops-row-main">' +
                '<div class="ops-row-title"><span class="ops-pill ' + stateClass + '">' +
                    escapeHTML(stateClass === "attention" ? "Needs attention" : "Active") +
                '</span><strong>' + escapeHTML(row.title) + '</strong></div>' +
                '<p>' + escapeHTML(row.message || friendly(row.stage)) + '</p>' +
                '<div class="ops-row-meta"><span>' + escapeHTML(channel) + '</span>' +
                    '<span>' + escapeHTML(friendly(row.kind)) + '</span>' +
                    '<span>' + escapeHTML(friendly(row.stage)) + '</span>' +
                    '<span>Updated ' + escapeHTML(when(row.updated_at)) + '</span></div>' +
            '</div>' +
            '<div class="ops-actions"><a href="' + escapeHTML(row.href) + '">Open</a>' +
                '<a href="' + escapeHTML(askHref(row)) + '">Ask Katcha ✦</a></div>' +
        '</article>';
    }).join("");
}

function renderOpportunities(rows) {
    const host = $("opportunity-list");
    if (!rows.length) {
        host.innerHTML = empty("No fresh opportunities", "New channel-scoped opportunities will appear as trend evidence clears its thresholds.");
        return;
    }
    host.innerHTML = rows.map((row) => {
        const reason = row.reasons?.[0] || friendly(row.lifecycle);
        return '<article class="ops-row">' +
            '<div class="ops-row-main">' +
                '<div class="ops-row-title"><span class="ops-pill opportunity">' +
                    Math.round(Number(row.opportunity_score) * 100) + '</span>' +
                    '<strong>' + escapeHTML(row.topic) + '</strong></div>' +
                '<p>' + escapeHTML(reason) + '</p>' +
                '<div class="ops-row-meta"><span>' + escapeHTML(channelName(row.channel_profile_id)) + '</span>' +
                    '<span>' + escapeHTML(friendly(row.lifecycle)) + '</span>' +
                    '<span>' + Math.round(Number(row.confidence) * 100) + '% confidence</span>' +
                    '<span>Expires ' + escapeHTML(when(row.expires_at)) + '</span></div>' +
            '</div>' +
            '<div class="ops-actions"><a href="' + escapeHTML(row.href) + '">Inspect</a></div>' +
        '</article>';
    }).join("");
}

function renderPublications(rows) {
    const host = $("publication-list");
    if (!rows.length) {
        host.innerHTML = empty("No measured publications yet", "Published videos and their latest stored analytics will appear here.");
        return;
    }
    host.innerHTML = rows.map((row) => {
        const metrics = [
            row.views == null ? "Views pending" : compactNumber(row.views) + " views",
            row.average_view_percentage == null ? "Retention pending" : percent(row.average_view_percentage) + " avg viewed",
            row.estimated_revenue == null ? "Revenue pending" : currency(row.estimated_revenue),
        ];
        return '<article class="ops-row">' +
            '<div class="ops-row-main">' +
                '<div class="ops-row-title"><span class="ops-pill">' + escapeHTML(friendly(row.status)) +
                    '</span><strong>' + escapeHTML(row.title) + '</strong></div>' +
                '<div class="ops-row-meta"><span>' + escapeHTML(channelName(row.channel_profile_id)) + '</span>' +
                    metrics.map((value) => '<span>' + escapeHTML(value) + '</span>').join("") +
                    '<span>Sample ' + escapeHTML(when(row.sampled_at || row.published_at)) + '</span></div>' +
            '</div>' +
            '<div class="ops-actions"><a href="' + escapeHTML(row.href) + '">Open</a></div>' +
        '</article>';
    }).join("");
}

function renderChannels(rows) {
    const host = $("channel-list");
    if (!rows.length) {
        host.innerHTML = empty("No active channels", "Connect or create a channel before Katcha can operate it.");
        return;
    }
    host.innerHTML = rows.map((row) =>
        '<a class="channel-card" href="' + escapeHTML(row.href || ("/channels?channel=" + row.id)) + '">' +
            '<div><span class="ops-pill ' + (row.needs_attention ? "attention" : "active") + '">' +
                escapeHTML(row.needs_attention ? "Check" : "Healthy") + '</span></div>' +
            '<strong>' + escapeHTML(row.title) + '</strong>' +
            '<div class="channel-metrics">' +
                '<span><b>' + row.needs_attention + '</b> attention</span>' +
                '<span><b>' + row.active_work + '</b> active</span>' +
                '<span><b>' + row.fresh_opportunities + '</b> opportunities</span>' +
                '<span><b>' + row.published_last_7d + '</b> published 7d</span>' +
            '</div>' +
        '</a>'
    ).join("");
}

function renderActivity(rows) {
    $("activity-count").textContent = String(rows.length);
    const host = $("activity-list");
    if (!rows.length) {
        host.innerHTML = empty("No recent activity", "Channel-scoped workflow events will appear here.");
        return;
    }
    host.innerHTML = rows.map((row) =>
        '<article class="ops-row activity-row">' +
            '<div class="ops-row-main"><div class="event-name">' +
                escapeHTML(friendly(row.event_type)) +
                ' <span>· ' + escapeHTML(channelName(row.channel_profile_id)) + '</span></div>' +
                '<div class="ops-row-meta"><span>' + escapeHTML(friendly(row.aggregate_type)) + '</span>' +
                    '<span>' + escapeHTML(when(row.created_at)) + '</span>' +
                    (row.href ? '<a href="' + escapeHTML(row.href) + '">Open related workspace</a>' : '') +
                '</div></div>' +
        '</article>'
    ).join("");
}

function renderOverview(payload) {
    state.overview = payload;
    const summary = payload.summary;
    $("pulse-attention").textContent = compactNumber(summary.needs_attention);
    $("pulse-active").textContent = compactNumber(summary.active_work);
    $("pulse-opportunities").textContent = compactNumber(summary.fresh_opportunities);
    $("pulse-published").textContent = compactNumber(summary.published_last_7d);
    $("attention-count").textContent = String(payload.attention.length);
    $("active-count").textContent = String(payload.active.length);
    $("generated-at").textContent = "Updated " + when(payload.generated_at);

    renderWork(payload.attention, "attention-list", "attention");
    renderWork(payload.active, "active-list", "active");
    renderOpportunities(payload.opportunities);
    renderPublications(payload.publications);
    renderChannels(payload.channels);
    renderActivity(payload.activity);
}

function syncChannelFilter(channels, selected = "") {
    if (channels.length) state.allChannels = channels;
    const options = ['<option value="">All channels</option>']
        .concat(state.allChannels.map((row) =>
            '<option value="' + escapeHTML(row.id) + '">' + escapeHTML(row.title) + '</option>'
        ));
    $("channel-filter").innerHTML = options.join("");
    $("channel-filter").value = selected;
    $("channel-filter").disabled = false;
}

async function loadOverview(channelId = "", { preserveStatus = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    $("refresh").disabled = true;
    if (!preserveStatus) setStatus("Loading live operations…");
    try {
        const query = new URLSearchParams({ limit: "12" });
        if (channelId) query.set("channel_profile_id", channelId);
        const payload = await api("/v1/operations/overview?" + query.toString());
        if (!channelId || !state.allChannels.length) syncChannelFilter(payload.channels, channelId);
        else syncChannelFilter([], channelId);
        renderOverview(payload);
        setStatus(
            payload.summary.needs_attention
                ? payload.summary.needs_attention + " item(s) need operator attention."
                : "Operations are clear. No intervention is required.",
            payload.summary.needs_attention ? "" : "success",
        );
        $("refresh").disabled = false;
        return payload;
    } catch (error) {
        setStatus(error.message, "error");
        if (error.status === 401 || error.status === 403) $("connect-form").hidden = false;
        return null;
    } finally {
        state.loading = false;
        $("refresh").disabled = false;
    }
}

async function connect(event) {
    if (event) event.preventDefault();
    state.token = $("token").value.trim();
    $("token").value = "";
    try {
        const all = await loadOverview("");
        if (!all) return;
        const requested = new URLSearchParams(location.search).get("channel") || "";
        const valid = requested && all.channels.some((row) => row.id === requested);
        if (valid) {
            $("channel-filter").value = requested;
            await loadOverview(requested, { preserveStatus: true });
        }
        $("connect-form").hidden = true;
    } catch {
        // Status is already visible and the form remains available.
    }
}

async function launcherConnect() {
    $("connect-form").hidden = true;
    for (;;) {
        try {
            const response = await fetch("/runtime/status", { cache: "no-store" });
            if (!response.ok) throw new Error("launcher unavailable");
            const runtime = await response.json();
            if (runtime.workspace_ready) {
                state.token = "";
                const loaded = await loadOverview("");
                if (loaded) return;
            }
            setStatus(
                runtime.desired_running
                    ? "Katcha is warming. Operations will load when the workspace is ready."
                    : "Katcha services are stopped. Start them from the launch console.",
            );
            if (!runtime.desired_running) return;
        } catch (error) {
            setStatus("Reconnecting to the local Katcha supervisor…", "error");
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
    }
}

$("connect-form").addEventListener("submit", connect);
$("refresh").addEventListener("click", () => loadOverview($("channel-filter").value));
$("channel-filter").addEventListener("change", async (event) => {
    const channel = event.target.value;
    const url = new URL(location.href);
    if (channel) url.searchParams.set("channel", channel);
    else url.searchParams.delete("channel");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
    await loadOverview(channel);
});

if (location.port === "8765") {
    void launcherConnect();
} else {
    setStatus("Connect to load live operations.");
}
