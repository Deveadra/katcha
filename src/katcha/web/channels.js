const state = {
    token: "",
    channels: [],
    connections: [],
    channelId: "",
    summary: null,
    publications: [],
    productions: [],
    brands: [],
    analytics: new Map(),
    selectedPublicationId: "",
};

const $ = (id) => document.getElementById(id);
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function headers(extra = {}) {
    return {
        "content-type": "application/json",
        ...(state.token ? { Authorization: "Bearer " + state.token } : {}),
        ...extra,
    };
}

async function api(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: headers(options.headers || {}),
    });
    if (!response.ok) {
        let message = response.status + " " + response.statusText;
        try {
            const payload = await response.json();
            message = payload.detail || message;
        } catch {}
        throw new Error(message);
    }
    if (response.status === 204) return null;
    return response.json();
}

function setStatus(message, kind = "") {
    $("status").textContent = message || "";
    $("status").className = kind;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function number(value) {
    const n = Number(value);
    return Number.isFinite(n) ? new Intl.NumberFormat("en-US").format(n) : "—";
}

function compact(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return new Intl.NumberFormat("en-US", {
        notation: "compact",
        maximumFractionDigits: 1,
    }).format(n);
}

function currency(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
    }).format(n);
}

function percent(value, digits = 0) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return (n * 100).toFixed(digits) + "%";
}

function dateText(value) {
    if (!value) return "Not yet";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: date.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined,
    });
}

function friendly(value) {
    return String(value || "unknown")
        .replaceAll("_", " ")
        .replace(/\b\w/g, (char) => char.toUpperCase());
}

function activeChannel() {
    return state.channels.find((item) => item.id === state.channelId) || null;
}

function activeConnection() {
    const channel = activeChannel();
    if (!channel) return null;
    return state.connections.find((item) => item.id === channel.youtube_connection_id) || null;
}

function latestSnapshot(publicationId) {
    const detail = state.analytics.get(publicationId);
    return detail ? detail.snapshot : null;
}

async function connect() {
    state.token = $("token").value.trim();
    $("token").value = "";
    $("connection-state").textContent = "CONNECTING";
    setStatus("Loading YouTube connections and channel workspaces…");
    try {
        const [channels, connections] = await Promise.all([
            api("/v1/channels"),
            api("/v1/integrations/youtube"),
        ]);
        state.channels = channels;
        state.connections = connections;
        $("connection-state").textContent = "CONNECTED";
        $("connection-state").className = "simulation connected";
        if (!channels.length) {
            renderSetup();
            setStatus("Connected. Create a channel workspace to begin.");
            return;
        }
        $("channel-setup").hidden = true;
        $("studio").hidden = false;
        renderChannelSelect();
        const existing = state.channelId && channels.some((item) => item.id === state.channelId);
        state.channelId = existing ? state.channelId : channels[0].id;
        $("channel").value = state.channelId;
        await loadChannel();
    } catch (error) {
        $("connection-state").textContent = "DISCONNECTED";
        $("connection-state").className = "simulation";
        setStatus(error.message, "error");
    }
}

function renderSetup() {
    $("studio").hidden = true;
    $("channel-setup").hidden = false;
    const container = $("setup-actions");
    if (!state.connections.length) {
        $("setup-copy").textContent =
            "Connect YouTube once. Katcha will keep the resulting workspace channel-scoped.";
        container.innerHTML =
            '<button class="studio-button primary" type="button" data-action="connect-youtube">Connect YouTube ↗</button>';
        return;
    }
    $("setup-copy").textContent =
        "YouTube is connected. Choose which connection should become a Katcha channel workspace.";
    container.innerHTML = state.connections
        .map((connection) =>
            '<button class="studio-button primary" type="button" data-action="create-channel" data-connection="' +
            escapeHtml(connection.id) +
            '">Create ' +
            escapeHtml(connection.channel_title) +
            " workspace</button>",
        )
        .join("");
}

async function beginYouTubeOAuth() {
    try {
        setStatus("Starting YouTube authorization…");
        const result = await api("/v1/integrations/youtube/oauth/start");
        window.location.assign(result.authorization_url);
    } catch (error) {
        setStatus(error.message, "error");
    }
}

async function createChannel(connectionId) {
    try {
        setStatus("Creating channel workspace…");
        const timezone =
            Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
        await api("/v1/channels", {
            method: "POST",
            body: JSON.stringify({
                youtube_connection_id: connectionId,
                timezone,
                fallback_schedule: [],
            }),
        });
        state.channels = await api("/v1/channels");
        $("channel-setup").hidden = true;
        $("studio").hidden = false;
        renderChannelSelect();
        state.channelId = state.channels[0]?.id || "";
        $("channel").value = state.channelId;
        await loadChannel();
    } catch (error) {
        setStatus(error.message, "error");
    }
}

function channelName(channel) {
    return (
        channel?.profile_metadata?.channel_title ||
        channel?.profile_metadata?.name ||
        state.connections.find((item) => item.id === channel?.youtube_connection_id)
            ?.channel_title ||
        "YouTube channel"
    );
}

function renderChannelSelect() {
    $("channel").innerHTML = state.channels
        .map(
            (channel) =>
                '<option value="' +
                escapeHtml(channel.id) +
                '">' +
                escapeHtml(channelName(channel)) +
                "</option>",
        )
        .join("");
}

async function loadChannel() {
    if (!state.channelId) return;
    setStatus("Loading channel operations…");
    state.analytics.clear();
    state.selectedPublicationId = "";
    const channel = activeChannel();
    try {
        const [summary, publications, productions, brands] = await Promise.all([
            api("/v1/channels/" + state.channelId),
            api("/v1/publications?limit=250"),
            api("/v1/productions?limit=100&channel_profile_id=" + encodeURIComponent(state.channelId)),
            api("/v1/channels/" + state.channelId + "/brands"),
        ]);
        state.summary = summary;
        state.publications = publications.filter(
            (item) => item.youtube_connection_id === channel.youtube_connection_id,
        );
        state.productions = productions;
        state.brands = brands;

        const measurable = state.publications
            .filter((item) => item.youtube_video_id)
            .slice(0, 12);
        const results = await Promise.allSettled(
            measurable.map((item) =>
                api("/v1/publications/" + item.id + "/analytics?limit=1"),
            ),
        );
        results.forEach((result, index) => {
            if (result.status === "fulfilled" && result.value[0]) {
                state.analytics.set(measurable[index].id, result.value[0]);
            }
        });

        renderAll();
        setStatus(
            "Loaded " +
                channelName(channel) +
                ". " +
                state.analytics.size +
                " recent videos have stored analytics snapshots.",
            "success",
        );
    } catch (error) {
        setStatus(error.message, "error");
    }
}

function renderAll() {
    renderChannelHeader();
    renderMetrics();
    renderFocus();
    renderEconomics();
    renderPublications();
    renderGrowth();
    renderProductions();
    renderControls();
}

function renderChannelHeader() {
    const channel = activeChannel();
    const connection = activeConnection();
    const title = channelName(channel);
    $("channel-avatar").textContent = title
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((word) => word[0])
        .join("")
        .toUpperCase() || "YT";
    $("channel-meta").textContent =
        (channel?.status ? friendly(channel.status) : "Unknown") +
        " · " +
        (channel?.timezone || "UTC") +
        (connection?.status ? " · YouTube " + friendly(connection.status) : "");
}

function sampledTotals() {
    let views = 0;
    let subs = 0;
    let revenue = 0;
    let measured = 0;
    for (const detail of state.analytics.values()) {
        const snapshot = detail.snapshot || {};
        if (snapshot.views != null) views += Number(snapshot.views) || 0;
        if (snapshot.subscribers_gained != null) subs += Number(snapshot.subscribers_gained) || 0;
        if (snapshot.subscribers_lost != null) subs -= Number(snapshot.subscribers_lost) || 0;
        if (snapshot.estimated_revenue != null) revenue += Number(snapshot.estimated_revenue) || 0;
        measured += 1;
    }
    return { views, subs, revenue, measured };
}

function renderMetrics() {
    const totals = sampledTotals();
    const economics = state.summary?.economics;
    const automation = state.summary?.automation;
    $("metric-videos").textContent = number(state.publications.length);
    $("metric-videos-sub").textContent =
        state.publications.filter((item) => item.status === "published").length +
        " published in recent Katcha history";
    $("metric-views").textContent = compact(totals.views);
    $("metric-views-sub").textContent =
        totals.measured + " recent video" + (totals.measured === 1 ? "" : "s") + " sampled";
    $("metric-margin").textContent = economics
        ? currency(economics.contribution_margin_usd)
        : "—";
    $("metric-margin-sub").textContent = economics?.monetary_scope_available
        ? "Tracked channel economics"
        : "Waiting for monetary coverage";
    $("metric-automation").textContent = automation
        ? friendly(automation.level).replace("Auto ", "")
        : "—";
    $("metric-automation-sub").textContent = automation?.eligible_for_next_level
        ? "Evidence clears next promotion gate"
        : "Current review safeguards active";
}

function focusItems() {
    const items = [];
    const failedPubs = state.publications.filter((item) => item.status === "failed");
    const failedProductions = state.productions.filter((item) =>
        ["failed", "dead_letter", "error"].includes(item.status),
    );
    const packaging = state.summary?.packaging_intelligence;
    const ranking = state.summary?.ranking;
    const economics = state.summary?.economics;
    const automation = state.summary?.automation;
    const activeBrand = state.brands.find((item) => item.is_active);

    if (failedPubs.length) {
        items.push({
            title: failedPubs.length + " publication" + (failedPubs.length === 1 ? "" : "s") + " need attention",
            note: "Resolve failed publishing before adding more unattended output.",
            href: "#content",
        });
    }
    if (failedProductions.length) {
        items.push({
            title: failedProductions.length + " production" + (failedProductions.length === 1 ? "" : "s") + " need recovery",
            note: "Use the Editing Studio for render diagnostics and safe regeneration.",
            href: "/editing#editorial-pipeline",
        });
    }
    if (packaging?.recommendation_count) {
        items.push({
            title: packaging.recommendation_count + " packaging recommendation" + (packaging.recommendation_count === 1 ? "" : "s"),
            note: "Review measured title and thumbnail evidence before the next packaging experiment.",
            href: "#growth",
        });
    }
    if (!activeBrand) {
        items.push({
            title: "No active brand version",
            note: "Stage and visually accept a channel identity before scaling production.",
            href: "/editing#brand-acceptance",
        });
    }
    if (!ranking || Number(ranking.sample_count || 0) < 20) {
        items.push({
            title: "Channel learning is still early",
            note: (ranking?.sample_count || 0) + " outcome samples currently inform learned ranking.",
            href: "#growth",
        });
    }
    if (economics) {
        const effective = Number(economics.effective_budget_usd || 0);
        const headroom = Number(economics.budget_headroom_usd || 0);
        if (effective > 0 && headroom / effective < 0.2) {
            items.push({
                title: "AI budget headroom is below 20%",
                note: currency(headroom) + " remains in the current tracked budget window.",
                href: "#identity",
            });
        }
    }
    if (automation?.eligible_for_next_level) {
        items.push({
            title: "Automation has enough evidence for the next gate",
            note: "Katcha will not promote itself here; review the evidence before changing policy.",
            href: "#identity",
        });
    }
    if (!state.publications.length) {
        items.push({
            title: "No publication history yet",
            note: "Create the first measurable private or published output so the learning loop has evidence.",
            href: "/explorer",
        });
    }
    if (!items.length) {
        items.push({
            title: "No immediate exception is blocking the channel",
            note: "Continue collecting measured outcomes; Katcha will surface failures and evidence-backed opportunities here.",
            href: "#growth",
        });
    }
    return items.slice(0, 5);
}

function renderFocus() {
    const items = focusItems();
    $("focus-count").textContent = String(items.length);
    $("focus-list").className = "focus-list";
    $("focus-list").innerHTML = items
        .map(
            (item, index) =>
                '<div class="focus-item"><span class="focus-rank">' +
                String(index + 1).padStart(2, "0") +
                '</span><div><strong>' +
                escapeHtml(item.title) +
                "</strong><small>" +
                escapeHtml(item.note) +
                '</small></div><a href="' +
                escapeHtml(item.href) +
                '">Open ↗</a></div>',
        )
        .join("");
}

function renderEconomics() {
    const e = state.summary?.economics;
    if (!e) {
        $("economics").className = "money-grid empty-state";
        $("economics").textContent = "No channel economics snapshot yet.";
        return;
    }
    const cells = [
        ["Revenue", currency(e.revenue_usd)],
        ["AI cost", currency(e.attributed_ai_cost_usd)],
        ["Margin", currency(e.contribution_margin_usd)],
        ["Budget headroom", currency(e.budget_headroom_usd)],
        ["MTD spend", currency(e.month_to_date_spend_usd)],
        ["Projected month end", currency(e.projected_month_end_spend_usd)],
    ];
    $("economics").className = "money-grid";
    $("economics").innerHTML = cells
        .map(
            ([label, value]) =>
                '<div class="money-cell"><span>' +
                escapeHtml(label) +
                "</span><strong>" +
                escapeHtml(value) +
                "</strong></div>",
        )
        .join("");
}

function filteredPublications() {
    const query = $("content-search").value.trim().toLowerCase();
    const filter = $("content-filter").value;
    return state.publications.filter((item) => {
        if (filter !== "all" && item.status !== filter) return false;
        if (!query) return true;
        return [item.title, item.description, ...(item.tags || [])]
            .join(" ")
            .toLowerCase()
            .includes(query);
    });
}

function renderPublications() {
    const rows = filteredPublications();
    const container = $("publication-list");
    if (!rows.length) {
        container.innerHTML = '<div class="empty-state" style="padding:18px">No videos match this view.</div>';
        $("video-detail").innerHTML =
            '<div class="empty-state">Select a video to inspect its details.</div>';
        return;
    }
    container.innerHTML = rows
        .map((item) => {
            const active = item.id === state.selectedPublicationId ? " active" : "";
            return (
                '<button class="publication-row' +
                active +
                '" type="button" data-publication="' +
                escapeHtml(item.id) +
                '"><span class="publication-title"><strong>' +
                escapeHtml(item.title || "Untitled video") +
                "</strong><small>" +
                escapeHtml(item.privacy_status || "unknown") +
                (item.processing_status ? " · " + escapeHtml(item.processing_status) : "") +
                '</small></span><span class="status-pill ' +
                escapeHtml(item.status) +
                '">' +
                escapeHtml(friendly(item.status)) +
                '</span><span class="publication-date">' +
                escapeHtml(dateText(item.published_at || item.publish_at || item.created_at)) +
                "</span></button>"
            );
        })
        .join("");
    if (!state.selectedPublicationId || !rows.some((item) => item.id === state.selectedPublicationId)) {
        state.selectedPublicationId = rows[0].id;
    }
    container.querySelectorAll("[data-publication]").forEach((button) => {
        button.addEventListener("click", () => selectPublication(button.dataset.publication));
    });
    renderVideoDetail();
}

async function selectPublication(id) {
    state.selectedPublicationId = id;
    renderPublications();
    if (!state.analytics.has(id)) {
        try {
            const rows = await api("/v1/publications/" + id + "/analytics?limit=1");
            if (rows[0]) state.analytics.set(id, rows[0]);
        } catch (error) {
            setStatus("Video loaded, but analytics could not be read: " + error.message, "error");
        }
    }
    renderVideoDetail();
    renderMetrics();
}

function renderVideoDetail() {
    const item = state.publications.find((row) => row.id === state.selectedPublicationId);
    if (!item) return;
    const detail = state.analytics.get(item.id);
    const snapshot = detail?.snapshot || null;
    const retention = detail?.retention || [];
    const watch50 = retention.find((point) => Number(point.elapsed_video_time_ratio) >= 0.5);
    const watch50Value = watch50?.audience_watch_ratio;
    const youtube =
        item.youtube_video_id
            ? '<a class="studio-button secondary small" target="_blank" rel="noopener noreferrer" href="https://www.youtube.com/watch?v=' +
              encodeURIComponent(item.youtube_video_id) +
              '">Open on YouTube ↗</a>'
            : "";
    const refresh =
        item.youtube_video_id
            ? '<button class="studio-button secondary small" id="refresh-video-analytics" type="button">Refresh analytics</button>'
            : "";
    const errorNote = item.error || item.failure_reason || item.rejection_reason;
    $("video-detail").innerHTML =
        '<span class="eyebrow">VIDEO DETAILS</span><h3>' +
        escapeHtml(item.title || "Untitled video") +
        '</h3><div class="detail-meta">' +
        escapeHtml(friendly(item.status)) +
        " · " +
        escapeHtml(friendly(item.privacy_status)) +
        " · " +
        escapeHtml(dateText(item.published_at || item.publish_at || item.created_at)) +
        '</div><div class="detail-actions">' +
        youtube +
        refresh +
        '</div><div class="video-kpis">' +
        videoKpi("Views", snapshot ? compact(snapshot.views) : "—") +
        videoKpi("Avg viewed", snapshot ? percent(Number(snapshot.average_view_percentage || 0) / 100, 1) : "—") +
        videoKpi("Likes", snapshot ? number(snapshot.likes) : "—") +
        videoKpi("Net subs", snapshot ? number(Number(snapshot.subscribers_gained || 0) - Number(snapshot.subscribers_lost || 0)) : "—") +
        videoKpi("Revenue", snapshot ? currency(snapshot.estimated_revenue) : "—") +
        videoKpi("50% retention", watch50Value != null ? percent(watch50Value, 1) : "—") +
        "</div>" +
        (watch50Value != null
            ? '<span class="micro-label">AUDIENCE STILL WATCHING AT ~50%</span><div class="retention-track"><i style="width:' +
              Math.min(100, Math.max(0, Number(watch50Value) * 100)) +
              '%"></i></div>'
            : "") +
        '<p class="detail-note">' +
        (snapshot
            ? "Latest stored analytics sample: " + escapeHtml(dateText(snapshot.sampled_at)) + "."
            : "No stored analytics snapshot for this video yet.") +
        (errorNote ? " Attention: " + escapeHtml(errorNote) : "") +
        "</p>";
    const refreshButton = $("refresh-video-analytics");
    if (refreshButton) refreshButton.addEventListener("click", refreshVideoAnalytics);
}

function videoKpi(label, value) {
    return (
        '<div class="video-kpi"><span>' +
        escapeHtml(label) +
        "</span><strong>" +
        escapeHtml(value) +
        "</strong></div>"
    );
}

async function refreshVideoAnalytics() {
    const item = state.publications.find((row) => row.id === state.selectedPublicationId);
    if (!item) return;
    try {
        setStatus("Starting analytics refresh for " + (item.title || "video") + "…");
        const result = await api("/v1/publications/" + item.id + "/analytics/refresh", {
            method: "POST",
            body: "{}",
        });
        setStatus(
            "Analytics refresh queued (" + result.sample_key + "). Reload this view after the workflow completes.",
            "success",
        );
    } catch (error) {
        setStatus(error.message, "error");
    }
}

function recommendationText(item) {
    if (typeof item === "string") return item;
    if (!item || typeof item !== "object") return "Recommendation available";
    return (
        item.recommendation ||
        item.summary ||
        item.reason ||
        item.action ||
        item.title ||
        Object.entries(item)
            .slice(0, 2)
            .map(([key, value]) => friendly(key) + ": " + String(value))
            .join(" · ")
    );
}

function renderGrowth() {
    const packaging = state.summary?.packaging_intelligence;
    $("packaging-status").textContent = packaging
        ? friendly(packaging.recommendation_status)
        : "NO DATA";
    if (packaging && (packaging.recommendations || []).length) {
        $("packaging").className = "insight-list";
        $("packaging").innerHTML = packaging.recommendations
            .slice(0, 5)
            .map(
                (item) =>
                    '<div class="insight"><strong>' +
                    escapeHtml(recommendationText(item)) +
                    '</strong><p>Based on ' +
                    escapeHtml(number(packaging.variant_window_count)) +
                    " measured packaging windows across " +
                    escapeHtml(number(packaging.publication_count)) +
                    " publications.</p></div>",
            )
            .join("");
    } else {
        $("packaging").className = "insight-list empty-state";
        $("packaging").textContent =
            packaging
                ? "Katcha has packaging evidence, but no recommendation has cleared its evidence policy yet."
                : "No packaging intelligence snapshot yet.";
    }

    const edit = state.summary?.edit_performance;
    if (edit && (edit.aggregate_metrics || []).length) {
        $("editing-performance").className = "insight-list";
        $("editing-performance").innerHTML = edit.aggregate_metrics
            .slice(0, 5)
            .map((item) => {
                const avp = item.mean_average_view_percentage;
                const retention50 = item.mean_audience_watch_ratio_50pct;
                return (
                    '<div class="insight"><strong>' +
                    escapeHtml(item.group_key || item.edit_blueprint_key || "Editing recipe") +
                    '</strong><div class="insight-metrics"><span>' +
                    escapeHtml(number(item.publication_count)) +
                    " videos</span><span>Avg viewed " +
                    escapeHtml(avp != null ? Number(avp).toFixed(1) + "%" : "—") +
                    "</span><span>50% retention " +
                    escapeHtml(retention50 != null ? percent(retention50, 1) : "—") +
                    "</span></div></div>"
                );
            })
            .join("");
    } else {
        $("editing-performance").className = "insight-list empty-state";
        $("editing-performance").textContent = "No maturity-matched editing evidence yet.";
    }

    const schedule = state.summary?.schedule || [];
    if (schedule.length) {
        $("schedule").className = "schedule-list";
        $("schedule").innerHTML = schedule
            .slice(0, 5)
            .map(
                (slot) =>
                    '<div class="schedule-slot"><b>#' +
                    escapeHtml(slot.rank) +
                    "</b><div><strong>" +
                    escapeHtml((WEEKDAYS[slot.weekday] || "Day") + " · " + String(slot.hour_local).padStart(2, "0") + ":00") +
                    "</strong><small>" +
                    escapeHtml(number(slot.sample_count)) +
                    " samples · " +
                    escapeHtml(friendly(slot.source)) +
                    '</small></div><span class="badge">' +
                    escapeHtml(percent(slot.confidence, 0)) +
                    "</span></div>",
            )
            .join("");
    } else {
        $("schedule").className = "schedule-list empty-state";
        $("schedule").textContent = "No evidence-backed publish window yet.";
    }
}

function renderProductions() {
    const container = $("production-list");
    if (!state.productions.length) {
        container.innerHTML =
            '<div class="studio-card empty-state" style="padding:16px">No channel-scoped productions yet.</div>';
        return;
    }
    container.innerHTML = state.productions.slice(0, 9)
        .map((item) => {
            const issue = item.error ? '<p>' + escapeHtml(item.error) + "</p>" : '<p>' + escapeHtml(friendly(item.kind || "short")) + " · generation " + escapeHtml(item.generation) + "</p>";
            return (
                '<article class="studio-card production-card"><div class="prod-top"><strong>' +
                escapeHtml(friendly(item.stage || item.status)) +
                '</strong><span class="status-pill ' +
                escapeHtml(item.status) +
                '">' +
                escapeHtml(friendly(item.status)) +
                "</span></div>" +
                issue +
                '<div class="prod-foot"><span>' +
                escapeHtml(item.edit_blueprint_key || "default recipe") +
                "</span><span>" +
                escapeHtml(dateText(item.created_at)) +
                "</span></div></article>"
            );
        })
        .join("");
}

function kv(label, value) {
    return (
        '<div class="kv"><span>' +
        escapeHtml(label) +
        "</span><strong>" +
        escapeHtml(value) +
        "</strong></div>"
    );
}

function renderControls() {
    const activeBrand = state.brands.find((item) => item.is_active);
    if (activeBrand) {
        const contract = activeBrand.contract || {};
        $("brand-panel").className = "key-value";
        $("brand-panel").innerHTML =
            kv("Brand", activeBrand.brand_key) +
            kv("Version", "v" + activeBrand.version) +
            kv("Format", contract.format || contract.channel_format || "Stored contract") +
            kv("Updated", dateText(activeBrand.created_at));
    } else {
        $("brand-panel").className = "key-value empty-state";
        $("brand-panel").textContent =
            state.brands.length
                ? "Brand versions exist, but none is active."
                : "No channel brand version has been staged yet.";
    }

    const automation = state.summary?.automation;
    $("automation-badge").textContent = automation ? friendly(automation.level) : "—";
    if (automation) {
        const evidence = automation.evidence || {};
        $("automation-panel").className = "key-value";
        $("automation-panel").innerHTML =
            kv("Current level", friendly(automation.level)) +
            kv("Reviewed items", number(evidence.reviewed_items)) +
            kv("Approval rate", percent(evidence.approval_rate, 1)) +
            kv("Regeneration rate", percent(evidence.regeneration_rate, 1)) +
            kv("Publish failure rate", percent(evidence.publication_failure_rate, 1)) +
            kv("Ranking confidence", percent(evidence.ranking_confidence, 1)) +
            kv(
                "Next gate",
                automation.next_level
                    ? automation.eligible_for_next_level
                        ? "Eligible: " + friendly(automation.next_level)
                        : "Not yet: " + friendly(automation.next_level)
                    : "Highest level",
            );
    }

    const strategy = state.summary?.strategy;
    $("routing-badge").textContent = strategy?.routing_policy?.mode
        ? friendly(strategy.routing_policy.mode)
        : "—";
    if (strategy) {
        $("strategy-panel").className = "key-value";
        $("strategy-panel").innerHTML =
            kv("Base monthly budget", currency(strategy.monthly_base_budget_usd)) +
            kv("Hard ceiling", currency(strategy.monthly_hard_budget_usd)) +
            kv("Reinvestment rate", percent(strategy.reinvestment_rate, 0)) +
            kv("Reinvestment cap", currency(strategy.reinvestment_cap_usd)) +
            kv("AI routing", friendly(strategy.routing_policy?.mode || "default")) +
            kv("Quality floor", friendly(strategy.routing_policy?.quality_floor || "task default"));
    }
}

async function refreshIntelligence() {
    try {
        setStatus("Starting channel intelligence refresh…");
        const result = await api("/v1/channels/" + state.channelId + "/intelligence/refresh", {
            method: "POST",
            body: JSON.stringify({
                idempotency_key: "channel-studio-" + new Date().toISOString().slice(0, 13),
            }),
        });
        setStatus(
            "Intelligence refresh queued (" + result.run_key + "). Current stored evidence remains visible while it runs.",
            "success",
        );
    } catch (error) {
        setStatus(error.message, "error");
    }
}

$("connect-form").addEventListener("submit", (event) => {
    event.preventDefault();
    connect();
});
$("channel").addEventListener("change", async (event) => {
    state.channelId = event.target.value;
    await loadChannel();
});
$("reload").addEventListener("click", loadChannel);
$("refresh-intelligence").addEventListener("click", refreshIntelligence);
$("content-search").addEventListener("input", renderPublications);
$("content-filter").addEventListener("change", renderPublications);
$("setup-actions").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.action === "connect-youtube") beginYouTubeOAuth();
    if (button.dataset.action === "create-channel") createChannel(button.dataset.connection);
});
