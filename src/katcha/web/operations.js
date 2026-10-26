const $ = (id) => document.getElementById(id);
const state = {
    token: sessionStorage.getItem("katcha.controlToken") || "",
    allChannels: [],
    overview: null,
    loading: false,
    metricsSequence: 0,
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

function incidentHref(id) {
    const numeric = Number(id);
    if (!Number.isSafeInteger(numeric) || numeric <= 0) return "";
    return "/operations?incident=" + numeric + "#operations-health";
}

function setStatus(message, kind = "") {
    const host = $("status");
    host.textContent = message;
    host.className = "ops-status" + (kind ? " " + kind : "");
}

async function showAccessPrompt(error) {
    const form = $("connect-form");
    form.hidden = false;
    const tokenDetails = $("manual-token-details");
    const localDevelopment =
        location.port === "8765" ||
        ["localhost", "127.0.0.1"].includes(location.hostname);
    tokenDetails.hidden = !localDevelopment;
    tokenDetails.open = localDevelopment;

    $("access-message").textContent = error.status === 403
        ? "This email has no Katcha operator profile. Use an authorized email or contact the workspace owner."
        : "Sign in with the email authorized for this Katcha workspace.";
    const login = $("access-login");
    login.hidden = true;
    try {
        const returnTo = location.pathname + location.search + location.hash;
        const response = await fetch(
            "/v1/control/access-login?return_to=" + encodeURIComponent(returnTo),
            { headers: { Accept: "application/json" }, cache: "no-store" },
        );
        if (!response.ok) throw new Error("sign-in settings unavailable");
        const info = await response.json();
        if (info.enabled && info.login_url) {
            login.href = info.login_url;
            login.hidden = false;
            return;
        }
        $("access-message").textContent = localDevelopment
            ? "Use your local launcher to connect. A standalone development server can use the token option under Advanced."
            : "Cloudflare sign-in is not set up for this site yet. Ask the Katcha owner to finish Access setup.";
    } catch {
        $("access-message").textContent =
            "Katcha could not load sign-in settings. Refresh the page or contact the workspace owner.";
    }
}

async function api(path, { method = "GET", body = null } = {}) {
    const headers = { Accept: "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;
    if (body !== null) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
        method,
        headers,
        cache: "no-store",
        ...(body === null ? {} : { body: JSON.stringify(body) }),
    });
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
        return '<article class="ops-row" role="link" tabindex="0" data-work-id="' +
            escapeHTML(row.id) + '" data-card-href="' + escapeHTML(row.href) + '">' +
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
            '<div class="ops-actions">' +
                (row.recovery_action
                    ? '<button type="button" class="ops-recover ' +
                        (row.recovery_action === "restart" ? "restart" : "") +
                        '" data-recover-kind="' + escapeHTML(row.kind) +
                        '" data-recover-id="' + escapeHTML(row.id) +
                        '" data-recover-action="' + escapeHTML(row.recovery_action) + '">' +
                        escapeHTML(row.recovery_label || friendly(row.recovery_action)) +
                        '</button>'
                    : '') +
                '<a href="' + escapeHTML(row.href) + '">Open</a>' +
                (["production", "short_episode", "publication"].includes(row.kind)
                    ? '<button type="button" class="ops-secondary" data-outcome-kind="' +
                        escapeHTML(row.kind) + '" data-outcome-id="' +
                        escapeHTML(row.id) + '">Check evidence</button>'
                    : '') +
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
        return '<article class="ops-row" role="link" tabindex="0" data-card-href="' + escapeHTML(row.href) + '">' +
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
        return '<article class="ops-row" role="link" tabindex="0" data-card-href="' + escapeHTML(row.href) + '">' +
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

function renderBriefing(payload) {
    const notes = [];
    if (payload.summary.needs_attention) notes.push({
        title: `${payload.summary.needs_attention} items need attention`,
        detail: "Resolve blocked work before starting more production.", href: "#attention",
    });
    const best = [...payload.publications].filter(row => row.views != null)
        .sort((a, b) => Number(b.views) - Number(a.views))[0];
    if (best) notes.push({title: best.title,
        detail: `${compactNumber(best.views)} views · ${percent(best.average_view_percentage)} average viewed · ${currency(best.estimated_revenue)} measured revenue. Review its packaging and pacing for repeatable wins.`,
        href: best.href});
    const opportunity = [...payload.opportunities].sort((a, b) => Number(b.opportunity_score) - Number(a.opportunity_score))[0];
    if (opportunity) notes.push({title: opportunity.topic,
        detail: `${Math.round(Number(opportunity.opportunity_score) * 100)} opportunity score · expires ${when(opportunity.expires_at)}. ${opportunity.reasons?.[0] || "Open the evidence before deciding."}`,
        href: opportunity.href});
    for (const channel of payload.channels.filter(row => !row.published_last_7d).slice(0, 3)) notes.push({
        title: `${channel.title}: no published output in the last 7 days`,
        detail: `${channel.active_work} work items in motion. Review its pipeline and publishing plan.`, href: channel.href,
    });
    $("briefing-list").innerHTML = notes.length ? notes.map(row =>
        `<article class="ops-row"><div class="ops-row-main"><strong>${escapeHTML(row.title)}</strong><p>${escapeHTML(row.detail)}</p></div><a class="panel-link" href="${escapeHTML(row.href)}">Open →</a></article>`
    ).join("") : empty("Your briefing is waiting for evidence", "Add sources and publish content to build a measured picture of your channels.");
}

async function loadAiReadiness() {
    const host = $("home-ai-readiness");
    try {
        const readiness = await api("/v1/ai/readiness");
        host.textContent = readiness.message;
        host.className = readiness.live ? "success" : "error";
    } catch {
        host.textContent = "AI readiness is unavailable. Open Katcha AI to inspect the connection.";
        host.className = "error";
    }
}

function focusLinkedWork() {
    const workId = new URLSearchParams(location.search).get("work");
    if (!workId) return;
    const row = [...document.querySelectorAll(
        "#attention-list .ops-row[data-work-id], #active-list .ops-row[data-work-id]",
    )].find((item) => item.dataset.workId === workId);
    if (row) {
        row.classList.add("linked-work");
        row.scrollIntoView({ block: "center" });
        row.focus({ preventScroll: true });
    }
}

function renderOverview(payload) {
    state.overview = payload;
    renderBriefing(payload);

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
    focusLinkedWork();
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

async function loadOutputMetrics(channelId) {
    const host = $("output-metrics");
    const requestId = ++state.metricsSequence;
    if (!channelId) {
        host.textContent = "Choose one channel to inspect its stored output records.";
        return;
    }
    host.textContent = "Loading channel output evidence…";
    try {
        const metrics = await api(
            "/v1/operations/output-metrics?channel_profile_id=" +
            encodeURIComponent(channelId) + "&hours=48",
        );
        if (requestId !== state.metricsSequence) return;
        host.textContent =
            "Last 48 hours: " +
            compactNumber(metrics.render_attempts_marked_verified) + " verified render-attempt records / " +
            compactNumber(metrics.render_attempt_records) + " attempts, " +
            compactNumber(metrics.render_attempts_dead_lettered) + " dead-lettered; " +
            compactNumber(metrics.recorded_published_videos) + " recorded publications, " +
            compactNumber(metrics.failed_publication_records) + " failed publications, " +
            compactNumber(metrics.analytics_snapshots_collected) + " analytics snapshots. " +
            "Stale processing candidates: " +
            compactNumber(
                Number(metrics.stale_production_candidates || 0) +
                Number(metrics.stale_short_episode_candidates || 0)
            ) + ". " +
            (metrics.youtube_connection_configured
                ? "" : "No YouTube connection configured. ") +
            "Database records only; remote object existence and live YouTube status are unverified.";
    } catch {
        if (requestId !== state.metricsSequence) return;
        host.textContent =
            "Output evidence unavailable. No conclusion about pipeline health can be drawn.";
    }
}

async function loadOperationsHealth() {
    const button = $("health-refresh");
    button.disabled = true;
    try {
        const health = await api("/v1/operations/health");
        const notifications = health.notifications;
        $("health-status").textContent = health.message
            + (health.checked_at ? " Last checked " + when(health.checked_at * 1000) + "." : "")
            + (notifications.status === "failed" ? " Telegram delivery failed; alerts remain queued." : "")
            + (notifications.pending ? " " + notifications.pending + " alerts await delivery." : "");
        $("health-signals").innerHTML = health.signals.map((row) => {
            const sha = row.key === "release" && row.detail.match(/\b[0-9a-f]{40}\b/);
            const detail = sha ? row.detail.replace(sha[0], "ahead of production") : row.detail;
            return (
            '<article class="ops-item"><strong>' + escapeHTML(row.label) + " · "
            + escapeHTML(friendly(row.status)) + '</strong><p>' + escapeHTML(detail)
            + '</p>' + (row.action ? '<p>' + escapeHTML(row.action) + '</p>' : '')
            + (sha ? '<details class="ae-help"><summary>Release details</summary><p>'
                + escapeHTML(row.detail) + '</p><a href="https://github.com/Deveadra/katcha/commit/'
                + sha[0] + '">Review this release on GitHub</a></details>' : '') + '</article>'
            );
        }).join("");
        const active = health.incidents.filter((row) => row.resolved === null);
        const botLabels = { queued: "Bot investigation queued", verifying: "Bot retry awaiting verification",
            operator_decision: "Operator review needed", verified: "Recovery verified",
            quieted: "No new failures observed; earlier work not proven recovered" };
        const selected = new URLSearchParams(location.search).get("incident");
        const selectedId = selected && /^\d{1,12}$/.test(selected) ? Number(selected) : null;
        const focused = selectedId !== null
            ? health.incidents.find((row) => row.id === selectedId)
            : null;
        const focusedHistory = focused
            ? health.history.filter((row) => row.incident === focused.id).slice(0, 10)
            : [];
        const focusedMarkup = selectedId === null ? "" : focused
            ? '<article class="ops-item" id="focused-health-incident"><strong>Incident #'
                + escapeHTML(focused.id) + ' · ' + escapeHTML(friendly(focused.status))
                + '</strong><p>' + escapeHTML(focused.label) + '</p><p>'
                + escapeHTML(focused.detail || "No detailed diagnostic available.")
                + '</p><p>' + escapeHTML(focused.action || "")
                + '</p><p>' + (focused.resolved === null ? "Unresolved" : "Closed")
                + ' · Last updated ' + escapeHTML(when(focused.updated * 1000))
                + '</p>' + focusedHistory.map((event) =>
                    '<p>' + escapeHTML(when(event.at * 1000)) + ' · '
                    + escapeHTML(event.message) + '</p>'
                ).join("")
                + '<p>Sanitized recent history only; full evidence requires authorized host inspection.</p></article>'
            : '<article class="ops-item" id="focused-health-incident">'
                + '<strong>Incident not in recent snapshot</strong><p>'
                + 'The health feed only includes the latest bounded incidents. '
                + 'Inspect the private host ledger for older evidence.</p></article>';
        $("health-incidents").innerHTML = focusedMarkup + (active.length ? active.map((row) => {
            const href = incidentHref(row.id);
            return '<article class="ops-item"><strong>' + escapeHTML(row.label) + '</strong><p>'
                + escapeHTML(botLabels[row.bot] || friendly(row.bot)) + '</p><p>'
                + escapeHTML(row.action) + '</p>'
                + (href ? '<a href="' + escapeHTML(href) + '">Inspect incident #'
                    + escapeHTML(row.id) + '</a>' : "") + '</article>';
        }).join("") : '<p>' + (health.status === "healthy"
            ? "No outstanding operational decisions."
            : "No current incident list is available. Check monitoring before relying on this view.")
            + '</p>');
        $("health-history").innerHTML = health.history.map((row) => {
            const href = incidentHref(row.incident);
            return '<p><time>' + escapeHTML(when(row.at * 1000)) + '</time> · '
                + escapeHTML(row.message)
                + (href ? ' · <a href="' + escapeHTML(href)
                    + '">Incident #' + escapeHTML(row.incident) + '</a>' : '')
                + '</p>';
        }).join("") || '<p>No health history has been recorded yet.';
        const review = health.release_review;
        $("health-release-review").textContent = review
            ? "Candidate " + review.target_sha + ". Checked " + when(review.checked_at * 1000) + ". "
                + review.checks.map((row) => friendly(row.name) + ": " + friendly(row.status)).join("; ")
                + ". Deployment approval still required. This review belongs only to the candidate shown."
            : "The bot has not prepared a release review yet. Review the selected release through the existing deployment runbook.";
    } catch (error) {
        if (error.status === 401 || error.status === 403) {
            for (const id of ["health-signals", "health-incidents", "health-history", "health-release-review"]) $(id).replaceChildren();
        }
        $("health-status").textContent = error.status === 403
            ? "Host health is available to a global operator. Your channel briefing remains available."
            : "Health could not be refreshed. Previously shown results may be outdated. Retry or inspect host monitoring.";
    } finally {
        button.disabled = false;
    }
}

$('health-refresh').addEventListener('click', loadOperationsHealth);
setInterval(() => { if (!document.hidden) void loadOperationsHealth(); }, 60000);

async function loadOverview(channelId = "", { preserveStatus = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    $("refresh").disabled = true;
    if (!preserveStatus) setStatus("Loading live operations…");
    try {
        const query = new URLSearchParams({
            limit: new URLSearchParams(location.search).has("work") ? "50" : "12",
        });
        if (channelId) query.set("channel_profile_id", channelId);
        const payload = await api("/v1/operations/overview?" + query.toString());
        if (!channelId || !state.allChannels.length) syncChannelFilter(payload.channels, channelId);
        else syncChannelFilter([], channelId);
        renderOverview(payload);
        void loadOutputMetrics(channelId);
        void loadAiReadiness();
        void loadOperationsHealth();
        setStatus(
            payload.summary.needs_attention
                ? payload.summary.needs_attention + " item(s) need operator attention."
                : "Your channel queue is clear. Check Operations Health for service readiness.",
            payload.summary.needs_attention ? "" : "success",
        );
        $("refresh").disabled = false;
        return payload;
    } catch (error) {
        if (error.status === 401 || error.status === 403) {
            if (error.status === 401 && state.token) {
                state.token = "";
                sessionStorage.removeItem("katcha.controlToken");
            }
            setStatus("Sign in to choose a channel and load its briefing.", "error");
            void showAccessPrompt(error);
        } else {
            setStatus(error.message, "error");
        }
        return null;
    } finally {
        state.loading = false;
        $("refresh").disabled = false;
    }
}

async function connect(event) {
    if (event) event.preventDefault();
    state.token = $("token").value.trim() || state.token;
    if (state.token) sessionStorage.setItem("katcha.controlToken", state.token);
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

async function inspectOutcome(event) {
    const button = event.target.closest("[data-outcome-kind][data-outcome-id]");
    if (!button) return;
    const card = button.closest(".ops-row");
    if (!card) return;
    let note = card.querySelector(".ops-outcome-note");
    if (!note) {
        note = document.createElement("p");
        note.className = "ops-outcome-note";
        note.setAttribute("role", "status");
        card.append(note);
    }
    button.disabled = true;
    note.textContent = "Checking durable outcome records…";
    try {
        const record = await api(
            "/v1/operations/work/" + encodeURIComponent(button.dataset.outcomeKind) +
            "/" + encodeURIComponent(button.dataset.outcomeId) + "/outcome-evidence",
        );
        const evidence = Array.isArray(record.evidence_codes) ? record.evidence_codes : [];
        const missing = Array.isArray(record.missing_codes) ? record.missing_codes : [];
        note.textContent =
            "Recorded outcome: " + friendly(record.assessment) + ". " +
            "Evidence: " + (evidence.map(friendly).join(", ") || "none") + ". " +
            "Missing or conflicting: " + (missing.map(friendly).join(", ") || "none") + ". " +
            "Live provider and business recovery not verified.";
    } catch {
        note.textContent = "Outcome evidence unavailable. Do not interpret this as success.";
    } finally {
        button.disabled = false;
    }
}

async function recoverWork(event) {
    const button = event.target.closest("[data-recover-kind][data-recover-id]");
    if (!button) return;
    button.disabled = true;
    const action = button.dataset.recoverAction || "resume";
    setStatus(
        (action === "restart" ? "Restarting" : "Resuming") + " work safely…",
    );
    try {
        const result = await api(
            "/v1/operations/work/" +
                encodeURIComponent(button.dataset.recoverKind) +
                "/" +
                encodeURIComponent(button.dataset.recoverId) +
                "/recover",
            {
                method: "POST",
                body: { actor: "operations-ui" },
            },
        );
        const successMessage =
            result.action === "restarted"
                ? "Restart created a new attempt; previous failure history was preserved."
                : "Resume request accepted. Existing workflow history will continue from its durable state.";
        await loadOverview($("channel-filter").value, { preserveStatus: true });
        setStatus(successMessage, "success");
    } catch (error) {
        setStatus(error.message, "error");
        button.disabled = false;
    }
}

$("connect-form").addEventListener("submit", connect);
$("refresh").addEventListener("click", () => loadOverview($("channel-filter").value));
$("attention-list").addEventListener("click", recoverWork);
$("active-list").addEventListener("click", recoverWork);
$("attention-list").addEventListener("click", inspectOutcome);
$("active-list").addEventListener("click", inspectOutcome);
function navigateCard(event) {
    const card = event.target.closest(".ops-row[data-card-href]");
    if (!card || event.target.closest("a, button, input, select, textarea")) return;
    if (event.type === "keydown" && (event.target !== card || !["Enter", " "].includes(event.key))) return;
    if (event.type === "keydown") event.preventDefault();
    const href = card.dataset.cardHref;
    if (href && href.startsWith("/") && !href.startsWith("//")) location.assign(href);
}
for (const id of ["attention-list", "active-list", "opportunity-list", "publication-list"]) {
    $(id).addEventListener("click", navigateCard);
    $(id).addEventListener("keydown", navigateCard);
}
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
    void connect();
}
