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
    goalDraft: [],
    goalChannelId: "",
    providerStatus: [],
    elevenlabsStatus: null,
    elevenlabsConfig: null,
    elevenlabsVoices: [],
    elevenlabsModels: [],
    elevenlabsPreviewUrl: null,
    providerError: "",
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

async function audioBlob(path, options = {}) {
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
    return response.blob();
}

function revokeProviderPreview() {
    if (state.elevenlabsPreviewUrl) {
        URL.revokeObjectURL(state.elevenlabsPreviewUrl);
        state.elevenlabsPreviewUrl = null;
    }
    const audio = $("elevenlabs-preview");
    if (audio) {
        audio.removeAttribute("src");
        audio.hidden = true;
        audio.load();
    }
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

async function loadProviderData() {
    revokeProviderPreview();
    state.providerStatus = [];
    state.elevenlabsStatus = null;
    state.elevenlabsConfig = null;
    state.elevenlabsVoices = [];
    state.elevenlabsModels = [];
    state.providerError = "";
    if (!state.channelId) return;

    try {
        state.providerStatus = await api("/v1/integrations/providers");
        const eleven = state.providerStatus.find((row) => row.provider === "elevenlabs");
        const configResult = await Promise.allSettled([
            api("/v1/integrations/elevenlabs/channels/" + encodeURIComponent(state.channelId)),
            eleven?.configured
                ? api("/v1/integrations/elevenlabs/status?channel_profile_id=" + encodeURIComponent(state.channelId))
                : Promise.resolve(null),
            eleven?.configured
                ? api("/v1/integrations/elevenlabs/models")
                : Promise.resolve([]),
            eleven?.configured
                ? api("/v1/integrations/elevenlabs/voices?page_size=100")
                : Promise.resolve({ voices: [] }),
        ]);
        if (configResult[0].status === "fulfilled") {
            state.elevenlabsConfig = configResult[0].value;
        }
        if (configResult[1].status === "fulfilled") {
            state.elevenlabsStatus = configResult[1].value;
        }
        if (configResult[2].status === "fulfilled") {
            state.elevenlabsModels = configResult[2].value || [];
        }
        if (configResult[3].status === "fulfilled") {
            state.elevenlabsVoices = configResult[3].value?.voices || [];
        }
        const rejected = configResult.find((result) => result.status === "rejected");
        if (rejected) state.providerError = rejected.reason?.message || "Provider discovery is partially unavailable.";
    } catch (error) {
        state.providerError = error.message;
    }
}

function renderProviders() {
    const elevenProvider = state.providerStatus.find((row) => row.provider === "elevenlabs");
    const invideoProvider = state.providerStatus.find((row) => row.provider === "invideo");
    const connected = Boolean(state.elevenlabsStatus?.connected);
    const configured = Boolean(elevenProvider?.configured);
    $("elevenlabs-state").textContent = connected
        ? "CONNECTED"
        : configured
            ? "CONFIGURED"
            : "NOT CONFIGURED";
    $("elevenlabs-detail").textContent =
        state.providerError
        || state.elevenlabsStatus?.detail
        || elevenProvider?.detail
        || "ElevenLabs status unavailable.";
    $("elevenlabs-plan").textContent = state.elevenlabsStatus?.subscription?.tier || "—";
    const subscription = state.elevenlabsStatus?.subscription;
    $("elevenlabs-usage").textContent = subscription
        ? number(subscription.character_count || 0) + " / " + number(subscription.character_limit || 0) + " characters"
        : "—";

    const voiceInput = $("elevenlabs-voice-id");
    const voiceList = $("elevenlabs-voices");
    voiceList.innerHTML = state.elevenlabsVoices.map((voice) =>
        '<option value="' + escapeHtml(voice.voice_id || "") + '">' +
        escapeHtml(voice.name || voice.voice_id || "Voice") +
        (voice.category ? " · " + escapeHtml(voice.category) : "") +
        "</option>"
    ).join("");
    voiceInput.value = state.elevenlabsConfig?.voice_id || state.elevenlabsStatus?.voice_id || "";
    voiceInput.disabled = !configured;

    const selectedVoice = state.elevenlabsVoices.find(
        (voice) => voice.voice_id === voiceInput.value,
    );
    const voiceName = selectedVoice?.name
        || state.elevenlabsConfig?.voice_name
        || state.elevenlabsStatus?.voice_name;
    $("elevenlabs-voice-help").textContent = voiceName
        ? voiceName + (selectedVoice?.category ? " · " + selectedVoice.category : "")
        : configured
            ? "Paste a known voice ID if voice discovery is unavailable on your plan."
            : "Configure the ElevenLabs API key in Katcha first.";

    const modelSelect = $("elevenlabs-model");
    const savedModel = state.elevenlabsConfig?.model_id || state.elevenlabsStatus?.model_id || "";
    const models = [...state.elevenlabsModels];
    if (savedModel && !models.some((row) => row.model_id === savedModel)) {
        models.unshift({ model_id: savedModel, name: savedModel });
    }
    modelSelect.innerHTML = models.length
        ? models.map((model) =>
            '<option value="' + escapeHtml(model.model_id || "") + '">' +
            escapeHtml(model.name || model.model_id || "TTS model") +
            "</option>"
        ).join("")
        : '<option value="">No TTS models discovered</option>';
    if (savedModel) modelSelect.value = savedModel;
    modelSelect.disabled = !configured || !models.length;

    const actionable = configured && Boolean(voiceInput.value.trim()) && Boolean(modelSelect.value);
    $("save-elevenlabs").disabled = !actionable;
    $("preview-elevenlabs").disabled = !actionable;
    $("refresh-elevenlabs").disabled = !configured;

    $("invideo-state").textContent = invideoProvider?.configured ? "BRIDGE READY" : "UNAVAILABLE";
    $("invideo-detail").textContent = invideoProvider?.detail
        || "Direct InVideo automation is waiting for a documented account API contract.";
    $("invideo-studio-link").href = "/studio?channel=" + encodeURIComponent(state.channelId);
}

async function saveElevenLabsConfig() {
    const voiceId = $("elevenlabs-voice-id").value.trim();
    const modelId = $("elevenlabs-model").value;
    if (!voiceId || !modelId) throw new Error("Choose an ElevenLabs voice and TTS model.");
    state.elevenlabsConfig = await api(
        "/v1/integrations/elevenlabs/channels/" + encodeURIComponent(state.channelId),
        {
            method: "PUT",
            body: JSON.stringify({
                enabled: true,
                voice_id: voiceId,
                model_id: modelId,
                actor: "channel-studio",
            }),
        },
    );
    state.elevenlabsStatus = await api(
        "/v1/integrations/elevenlabs/status?channel_profile_id=" + encodeURIComponent(state.channelId),
    );
    renderProviders();
    setStatus(
        "ElevenLabs voice saved for " + channelName(activeChannel()) + ". Future narration will use this channel selection.",
        "success",
    );
}

async function previewElevenLabsVoice() {
    const voiceId = $("elevenlabs-voice-id").value.trim();
    const modelId = $("elevenlabs-model").value;
    const text = $("elevenlabs-preview-text").value.trim();
    if (!voiceId || !modelId || !text) throw new Error("Voice, model, and preview text are required.");
    const blob = await audioBlob(
        "/v1/integrations/elevenlabs/channels/" + encodeURIComponent(state.channelId) + "/preview",
        {
            method: "POST",
            body: JSON.stringify({
                text,
                voice_id: voiceId,
                model_id: modelId,
            }),
        },
    );
    revokeProviderPreview();
    state.elevenlabsPreviewUrl = URL.createObjectURL(blob);
    const audio = $("elevenlabs-preview");
    audio.src = state.elevenlabsPreviewUrl;
    audio.hidden = false;
    await audio.play().catch(() => {});
    setStatus("ElevenLabs preview generated. Listen before saving this voice to the channel.", "success");
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
            api(
                "/v1/publications?limit=250&youtube_connection_id=" +
                    encodeURIComponent(channel.youtube_connection_id),
            ),
            api("/v1/productions?limit=100&channel_profile_id=" + encodeURIComponent(state.channelId)),
            api("/v1/channels/" + state.channelId + "/brands"),
        ]);
        state.summary = summary;
        state.publications = publications;
        state.productions = productions;
        state.brands = brands;
        await loadProviderData();

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
    renderMonetization();
    renderPublications();
    renderGrowth();
    renderProductions();
    renderControls();
    renderProviders();
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
    const growth = state.summary?.growth;
    const activeBrand = state.brands.find((item) => item.is_active);

    if (growth?.ai?.stage && growth.ai.stage !== "ads_thresholds_met") {
        const priority = growth.ai.priority_metrics?.[0];
        items.push({
            title: priority
                ? "Monetization focus: " + friendly(priority)
                : "Monetization progress needs more data",
            note:
                "Katcha is using the " +
                friendly(growth.ai.selected_path || "balanced") +
                " growth path at " +
                friendly(growth.ai.pace || "aggressive") +
                " pace.",
            href: "#monetization",
        });
    }
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

function growthMetricLabel(metric) {
    const labels = {
        subscribers: "Subscribers",
        public_uploads_90d: "Public uploads · 90d",
        qualified_watch_hours_365d: "Qualified watch hours · 365d",
        qualified_shorts_views_90d: "Qualified Shorts views · 90d",
    };
    return labels[metric] || friendly(metric);
}

function growthMetricValue(metric, value) {
    if (value == null) return "Needs refresh";
    if (metric === "qualified_watch_hours_365d") {
        return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value));
    }
    return compact(value);
}

function growthProgressRow(item) {
    const progress = item.progress == null ? null : Number(item.progress);
    const width = progress == null ? 0 : Math.max(0, Math.min(100, progress * 100));
    const prefix = item.estimated ? "~" : "";
    const current = growthMetricValue(item.metric, item.current);
    const target = growthMetricValue(item.metric, item.target);
    return (
        '<div class="progress-item"><div class="progress-meta"><span>' +
        escapeHtml(growthMetricLabel(item.metric)) +
        '</span><span>' +
        escapeHtml(prefix + current + " / " + target) +
        '</span></div><div class="progress-track"><i style="width:' +
        width.toFixed(1) +
        '%"></i></div></div>'
    );
}

function milestoneMarkup(milestone, title, subtitle) {
    if (!milestone) {
        return '<div class="milestone"><div class="empty-state">No milestone data yet.</div></div>';
    }
    const status = milestone.thresholds_met_estimate
        ? "THRESHOLDS MET*"
        : milestone.progress == null
            ? "NEEDS DATA"
            : Math.round(Number(milestone.progress) * 100) + "%";
    return (
        '<div class="milestone"><div class="milestone-top"><div><strong>' +
        escapeHtml(title) +
        "</strong><small>" +
        escapeHtml(subtitle) +
        '</small></div><span class="milestone-status">' +
        escapeHtml(status) +
        "</span></div>" +
        (milestone.requirements || []).map(growthProgressRow).join("") +
        '<div class="audience-or">AND EITHER</div>' +
        (milestone.audience_paths || []).map(growthProgressRow).join("") +
        "</div>"
    );
}

function renderCustomGoals() {
    const container = $("custom-goals");
    if (!state.goalDraft.length) {
        container.innerHTML = '<div class="empty-state">No extra operator benchmarks.</div>';
        return;
    }
    const priorityNames = {
        5: "Critical",
        4: "High",
        3: "Normal",
        2: "Low",
        1: "Background",
    };
    container.innerHTML = state.goalDraft
        .map(
            (goal, index) =>
                '<div class="custom-goal"><div><strong>' +
                escapeHtml(growthMetricLabel(goal.metric)) +
                "</strong><small>Target " +
                escapeHtml(growthMetricValue(goal.metric, goal.target)) +
                (goal.target_date ? " by " + escapeHtml(dateText(goal.target_date)) : "") +
                "</small></div><span>" +
                escapeHtml(priorityNames[goal.priority] || "Normal") +
                '</span><button type="button" data-remove-goal="' +
                index +
                '" aria-label="Remove goal">×</button></div>',
        )
        .join("");
    container.querySelectorAll("[data-remove-goal]").forEach((button) => {
        button.addEventListener("click", () => {
            state.goalDraft.splice(Number(button.dataset.removeGoal), 1);
            renderCustomGoals();
        });
    });
}

function renderMonetization() {
    const growth = state.summary?.growth;
    if (!growth) {
        $("growth-stage").textContent = "NEEDS DATA";
        $("milestone-grid").innerHTML =
            '<div class="empty-state">No monetization progress has been measured yet.</div>';
        return;
    }

    const goals = growth.goals || {};
    const ai = growth.ai || {};
    $("growth-stage").textContent = friendly(ai.stage || "needs data");
    $("growth-pace-badge").textContent = String(ai.pace || "aggressive").toUpperCase();
    $("growth-sampled-at").textContent = growth.sampled_at
        ? "Measured " + dateText(growth.sampled_at)
        : "Threshold model ready · metrics need refresh";

    $("milestone-grid").innerHTML =
        milestoneMarkup(
            growth.milestones?.early_ypp,
            "Early YPP access",
            "Fan funding & Shopping where expanded YPP is available",
        ) +
        milestoneMarkup(
            growth.milestones?.ads_premium,
            "Ads & Premium",
            "Revenue-sharing entry benchmark",
        );

    const change = growth.benchmarks?.next_change;
    $("threshold-change").hidden = !change;
    if (change) {
        $("threshold-change").textContent =
            "Scheduled YouTube change · " +
            dateText(change.effective_date) +
            ": new ad-revenue applicants need " +
            number(change.ads_premium.watch_hours_365d) +
            " watch hours or " +
            compact(change.ads_premium.shorts_views_90d) +
            " Shorts views, alongside 1,000 subscribers.";
    }
    $("monetization-disclaimer").textContent =
        growth.disclaimer ||
        "Watch-hour and Shorts-view progress are estimates. YouTube Studio remains the authority for exact YPP eligibility.";

    if (state.goalChannelId !== state.channelId) {
        state.goalChannelId = state.channelId;
        state.goalDraft = (goals.custom_targets || []).map((item) => ({ ...item }));
    }
    $("growth-objective").value = goals.objective || "ads_revenue";
    $("growth-path").value = goals.path || "fastest";
    $("growth-pace").value = goals.pace || "aggressive";
    $("growth-target-date").value = goals.target_date || "";
    renderCustomGoals();

    const priorities = (ai.priority_metrics || []).map(growthMetricLabel);
    $("ai-growth-focus").className = "ai-growth-focus";
    $("ai-growth-focus").innerHTML =
        "<strong>AI focus:</strong> " +
        escapeHtml(
            priorities.length
                ? priorities.join(" → ")
                : "Current monetization benchmark is satisfied",
        ) +
        "<br>Route: " +
        escapeHtml(friendly(ai.selected_path || goals.path || "balanced")) +
        " · Pace: " +
        escapeHtml(friendly(ai.pace || goals.pace || "aggressive")) +
        ". Candidate ranking keeps measured quality as the majority signal while giving these growth gaps extra weight.";
}

function addCustomGoal() {
    const target = Number($("custom-goal-target").value);
    if (!Number.isFinite(target) || target <= 0) {
        setStatus("Enter a custom goal target greater than zero.", "error");
        return;
    }
    state.goalDraft.push({
        metric: $("custom-goal-metric").value,
        target,
        target_date: $("custom-goal-date").value || null,
        priority: Number($("custom-goal-priority").value || 3),
        enabled: true,
    });
    $("custom-goal-target").value = "";
    $("custom-goal-date").value = "";
    renderCustomGoals();
    setStatus("Custom benchmark staged. Save growth goals to make it active.");
}

async function saveGrowthGoals(event) {
    event.preventDefault();
    try {
        setStatus("Saving channel growth goals…");
        const strategy = await api(
            "/v1/channels/" + state.channelId + "/growth-goals",
            {
                method: "POST",
                body: JSON.stringify({
                    objective: $("growth-objective").value,
                    path: $("growth-path").value,
                    pace: $("growth-pace").value,
                    target_date: $("growth-target-date").value || null,
                    custom_targets: state.goalDraft,
                    actor: "channel_studio",
                }),
            },
        );
        const growth = await api("/v1/channels/" + state.channelId + "/growth");
        state.summary.strategy = strategy;
        state.summary.growth = growth;
        state.goalChannelId = "";
        renderMonetization();
        renderFocus();
        renderControls();
        setStatus(
            "Growth goals saved as strategy v" +
                strategy.version +
                ". Katcha is using them in channel-scoped candidate scoring.",
            "success",
        );
    } catch (error) {
        setStatus(error.message, "error");
    }
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
    revokeProviderPreview();
    state.channelId = event.target.value;
    await loadChannel();
});
$("reload").addEventListener("click", loadChannel);
$("refresh-intelligence").addEventListener("click", refreshIntelligence);
$("save-elevenlabs").addEventListener("click", async () => {
    const button = $("save-elevenlabs");
    button.disabled = true;
    try {
        await saveElevenLabsConfig();
    } catch (error) {
        setStatus(error.message, "error");
    } finally {
        renderProviders();
    }
});
$("preview-elevenlabs").addEventListener("click", async () => {
    const button = $("preview-elevenlabs");
    button.disabled = true;
    try {
        await previewElevenLabsVoice();
    } catch (error) {
        setStatus(error.message, "error");
    } finally {
        renderProviders();
    }
});
$("refresh-elevenlabs").addEventListener("click", async () => {
    const button = $("refresh-elevenlabs");
    button.disabled = true;
    try {
        await loadProviderData();
        renderProviders();
        setStatus("Provider data refreshed.", "success");
    } catch (error) {
        setStatus(error.message, "error");
    } finally {
        renderProviders();
    }
});
$("elevenlabs-voice-id").addEventListener("input", renderProviders);
$("elevenlabs-model").addEventListener("change", renderProviders);
window.addEventListener("beforeunload", revokeProviderPreview);
$("growth-goals-form").addEventListener("submit", saveGrowthGoals);
$("add-custom-goal").addEventListener("click", addCustomGoal);
$("growth-pace").addEventListener("change", (event) => {
    $("growth-pace-badge").textContent = event.target.value.toUpperCase();
});
$("content-search").addEventListener("input", renderPublications);
$("content-filter").addEventListener("change", renderPublications);
$("setup-actions").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.action === "connect-youtube") beginYouTubeOAuth();
    if (button.dataset.action === "create-channel") createChannel(button.dataset.connection);
});
