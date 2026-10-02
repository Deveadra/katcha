const launchParams = new URLSearchParams(location.search);
const state = {
    token: sessionStorage.getItem("katcha.controlToken") || "",
    channels: [],
    connections: [],
    channelId: launchParams.get("channel") || sessionStorage.getItem("katcha.channel") || "",
    summary: null,
    operations: null,
    publications: [],
    productions: [],
    brands: [],
    analytics: new Map(),
    packagingVariants: new Map(),
    selectedPublicationId: "",
    goalDraft: [],
    goalChannelId: "",
    providerStatus: [],
    elevenlabsStatus: null,
    elevenlabsConfig: null,
    elevenlabsVoices: [],
    elevenlabsVoiceLibrary: [],
    elevenlabsModels: [],
    elevenlabsPreviewUrl: null,
    providerError: "",
    voiceToggleBusy: false,
    oauthResult: launchParams.get("youtube") || "",
    oauthConnectionId: launchParams.get("connection") || "",
    oauthMessage: launchParams.get("message") || "",
    managerRequested:
        launchParams.get("setup") === "1" || Boolean(launchParams.get("youtube")),
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

function channelForConnection(connectionId) {
    return state.channels.find((item) => item.youtube_connection_id === connectionId) || null;
}

function channelConnectionLabel(connection) {
    const channel = channelForConnection(connection.id);
    return (
        channel?.profile_metadata?.channel_handle ||
        connection.channel_id ||
        friendly(connection.status || "connected")
    );
}

function renderChannelManager() {
    const container = $("channel-manager-connections");
    if (!container) return;

    if (!state.connections.length) {
        container.innerHTML =
            '<div class="channel-manager-empty">No YouTube channels are connected yet. Use “Connect another YouTube channel” to authorize one.</div>';
        return;
    }

    container.innerHTML = state.connections
        .map((connection) => {
            const channel = channelForConnection(connection.id);
            const action = channel
                ? '<span class="channel-connection-state">IN KATCHA</span>'
                : '<button class="studio-button primary small" type="button" data-action="create-channel" data-connection="' +
                  escapeHtml(connection.id) +
                  '">Add to Katcha</button>';
            return (
                '<article class="channel-connection-row' +
                (channel ? " is-added" : "") +
                '"><div class="channel-connection-copy"><strong>' +
                escapeHtml(connection.channel_title || "YouTube channel") +
                '</strong><small>' +
                escapeHtml(channelConnectionLabel(connection)) +
                '</small></div>' +
                action +
                "</article>"
            );
        })
        .join("");
}

function openChannelManager() {
    renderChannelManager();
    $("channel-manager").hidden = false;
    $("channel-manager").scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearOAuthResultParams() {
    const url = new URL(location.href);
    for (const key of ["youtube", "connection", "message"]) {
        url.searchParams.delete(key);
    }
    history.replaceState({}, "", url.pathname + url.search + url.hash);
    state.oauthResult = "";
    state.oauthConnectionId = "";
    state.oauthMessage = "";
}

function closeChannelManager() {
    $("channel-manager").hidden = true;
    state.managerRequested = false;
    const url = new URL(location.href);
    if (url.searchParams.has("setup")) {
        url.searchParams.delete("setup");
        history.replaceState({}, "", url.pathname + url.search + url.hash);
    }
}

function askKatchaHref(kind, id, prompt) {
    const params = new URLSearchParams({
        channel: state.channelId,
        resource_kind: kind,
        resource_id: id,
        prompt,
        focus: "chat",
    });
    return "/ai?" + params.toString();
}

function latestSnapshot(publicationId) {
    const detail = state.analytics.get(publicationId);
    return detail ? detail.snapshot : null;
}

async function connect() {
    state.token = $("token").value.trim() || state.token;
    $("token").value = "";
    if (state.token) sessionStorage.setItem("katcha.controlToken", state.token);
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
        renderChannelManager();

        if (state.oauthResult === "connected" && state.oauthConnectionId) {
            const connection = state.connections.find(
                (item) => item.id === state.oauthConnectionId,
            );
            if (!connection) {
                const missingConnection = state.oauthConnectionId;
                clearOAuthResultParams();
                throw new Error(
                    "YouTube authorization completed, but Katcha could not find connection " +
                        missingConnection +
                        ". Refresh and try connecting the channel again.",
                );
            }
            const existingChannel = channelForConnection(connection.id);
            clearOAuthResultParams();
            if (existingChannel) {
                $("channel-setup").hidden = true;
                $("studio").hidden = false;
                renderChannelSelect();
                state.channelId = existingChannel.id;
                $("channel").value = state.channelId;
                sessionStorage.setItem("katcha.channel", state.channelId);
                closeChannelManager();
                await loadChannel();
                setStatus(
                    "YouTube authorization refreshed for " +
                        channelName(existingChannel) +
                        ".",
                    "success",
                );
                return;
            }
            await createChannel(connection.id);
            return;
        }

        const oauthError =
            state.oauthResult === "error"
                ? state.oauthMessage || "YouTube authorization did not complete."
                : "";

        if (!channels.length) {
            renderSetup();
            setStatus("Connected. Add your first channel to begin.");
            if (state.managerRequested) openChannelManager();
            if (oauthError) {
                clearOAuthResultParams();
                setStatus(oauthError, "error");
            }
            return;
        }
        $("channel-setup").hidden = true;
        $("studio").hidden = false;
        renderChannelSelect();
        const existing = state.channelId && channels.some((item) => item.id === state.channelId);
        state.channelId = existing ? state.channelId : channels[0].id;
        $("channel").value = state.channelId;
        await loadChannel();
        if (state.managerRequested) openChannelManager();
        if (oauthError) {
            clearOAuthResultParams();
            setStatus(oauthError, "error");
        }
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
            "Connect a YouTube channel once. Katcha will create an isolated workspace for its brand, learning, sources, production, and analytics.";
        container.innerHTML =
            '<button class="studio-button primary" type="button" data-action="connect-youtube">Connect YouTube channel ↗</button>';
        return;
    }
    $("setup-copy").textContent =
        "Choose a connected YouTube channel to add to Katcha.";
    const available = state.connections.filter((connection) => !channelForConnection(connection.id));
    container.innerHTML =
        available
            .map((connection) =>
                '<button class="studio-button primary" type="button" data-action="create-channel" data-connection="' +
                escapeHtml(connection.id) +
                '">Add ' +
                escapeHtml(connection.channel_title) +
                "</button>",
            )
            .join("") +
        '<button class="studio-button secondary" type="button" data-action="connect-youtube">Connect another YouTube channel ↗</button>';
}

async function beginYouTubeOAuth() {
    try {
        setStatus("Starting YouTube authorization…");
        const returnTo = new URL("/channels?setup=1", location.origin).toString();
        const result = await api(
            "/v1/integrations/youtube/oauth/start?return_to=" +
                encodeURIComponent(returnTo),
        );
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
        const created = await api("/v1/channels", {
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
        const createdChannel =
            (created?.id && state.channels.find((item) => item.id === created.id)) ||
            state.channels.find((item) => item.youtube_connection_id === connectionId);
        state.channelId = createdChannel?.id || state.channelId || state.channels[0]?.id || "";
        $("channel").value = state.channelId;
        sessionStorage.setItem("katcha.channel", state.channelId);
        renderChannelManager();
        closeChannelManager();
        await loadChannel();
        setStatus("Channel added to Katcha and selected.", "success");
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
    state.elevenlabsVoiceLibrary = [];
    state.elevenlabsModels = [];
    state.providerError = "";
    if (!state.channelId) return;

    try {
        state.providerStatus = await api("/v1/integrations/providers");
        const eleven = state.providerStatus.find((row) => row.provider === "elevenlabs");
        state.elevenlabsConfig = await api(
            "/v1/integrations/elevenlabs/channels/" +
                encodeURIComponent(state.channelId),
        );
        state.elevenlabsVoiceLibrary = (state.elevenlabsConfig?.saved_voices || [])
            .filter((voice) => voice?.voice_id)
            .map((voice) => ({ ...voice }));
        if (
            state.elevenlabsConfig?.voice_id &&
            !state.elevenlabsVoiceLibrary.some(
                (voice) => voice.voice_id === state.elevenlabsConfig.voice_id,
            )
        ) {
            state.elevenlabsVoiceLibrary.unshift({
                voice_id: state.elevenlabsConfig.voice_id,
                name:
                    state.elevenlabsConfig.voice_name ||
                    state.elevenlabsConfig.voice_id,
                category: null,
                labels: {},
            });
        }

        if (!state.elevenlabsConfig?.enabled || !eleven?.configured) return;

        const providerResults = await Promise.allSettled([
            api(
                "/v1/integrations/elevenlabs/status?channel_profile_id=" +
                    encodeURIComponent(state.channelId),
            ),
            api("/v1/integrations/elevenlabs/models"),
            api("/v1/integrations/elevenlabs/voices?page_size=100"),
        ]);
        if (providerResults[0].status === "fulfilled") {
            state.elevenlabsStatus = providerResults[0].value;
        }
        if (providerResults[1].status === "fulfilled") {
            state.elevenlabsModels = providerResults[1].value || [];
        }
        if (providerResults[2].status === "fulfilled") {
            state.elevenlabsVoices = providerResults[2].value?.voices || [];
        }
        const rejected = providerResults.find(
            (result) => result.status === "rejected",
        );
        if (rejected) {
            state.providerError =
                rejected.reason?.message ||
                "Provider discovery is partially unavailable.";
        }
    } catch (error) {
        state.providerError = error.message;
    }
}

function discoveredVoice(voiceId) {
    return state.elevenlabsVoices.find((voice) => voice.voice_id === voiceId) || null;
}

function voiceLibraryOption(voice) {
    const discovered = discoveredVoice(voice.voice_id);
    const name = discovered?.name || voice.name || voice.voice_id;
    return '<option value="' + escapeHtml(voice.voice_id) + '">' + escapeHtml(name) + "</option>";
}

function normalizedVoiceLibrary() {
    const seen = new Set();
    return state.elevenlabsVoiceLibrary
        .filter((voice) => {
            const id = String(voice?.voice_id || "").trim();
            if (!id || seen.has(id)) return false;
            seen.add(id);
            return true;
        })
        .slice(0, 12)
        .map((voice) => {
            const discovered = discoveredVoice(voice.voice_id);
            return {
                voice_id: voice.voice_id,
                name: discovered?.name || voice.name || voice.voice_id,
                category: discovered?.category || voice.category || null,
                labels: discovered?.labels || voice.labels || {},
            };
        });
}

function renderVoiceLibrary(configured) {
    state.elevenlabsVoiceLibrary = normalizedVoiceLibrary();
    const library = state.elevenlabsVoiceLibrary;
    const host = $("elevenlabs-saved-voices");
    host.innerHTML = library.length
        ? library.map((voice) => {
            const discovered = discoveredVoice(voice.voice_id);
            const name = discovered?.name || voice.name || voice.voice_id;
            const category = discovered?.category || voice.category || "";
            return (
                '<div class="voice-library-row">' +
                    '<div><strong>' + escapeHtml(name) + '</strong>' +
                    '<small>' + escapeHtml(voice.voice_id) +
                    (category ? " · " + escapeHtml(category) : "") +
                    '</small></div>' +
                    '<button type="button" class="voice-remove" data-remove-elevenlabs-voice="' +
                    escapeHtml(voice.voice_id) + '" aria-label="Remove ' +
                    escapeHtml(name) + '">Remove</button>' +
                '</div>'
            );
        }).join("")
        : '<div class="provider-help">No voices saved for this channel yet.</div>';

    const optionMarkup = library.map(voiceLibraryOption).join("");
    const defaultSelect = $("elevenlabs-default-voice");
    const primarySelect = $("elevenlabs-longform-primary");
    const secondarySelect = $("elevenlabs-longform-secondary");
    const previewSelect = $("elevenlabs-preview-voice");
    const previous = {
        defaultVoice: defaultSelect.value || state.elevenlabsConfig?.voice_id || "",
        primary: primarySelect.value || state.elevenlabsConfig?.longform_primary_voice_id || "",
        secondary: secondarySelect.value || state.elevenlabsConfig?.longform_secondary_voice_id || "",
        preview: previewSelect.value || state.elevenlabsConfig?.voice_id || "",
    };

    defaultSelect.innerHTML = library.length
        ? optionMarkup
        : '<option value="">Add a voice first</option>';
    primarySelect.innerHTML = library.length
        ? optionMarkup
        : '<option value="">Add a voice first</option>';
    secondarySelect.innerHTML =
        '<option value="">Not assigned</option>' + optionMarkup;
    previewSelect.innerHTML = library.length
        ? optionMarkup
        : '<option value="">Add a voice first</option>';

    const ids = new Set(library.map((voice) => voice.voice_id));
    const fallback = library[0]?.voice_id || "";
    defaultSelect.value = ids.has(previous.defaultVoice) ? previous.defaultVoice : fallback;
    primarySelect.value = ids.has(previous.primary) ? previous.primary : defaultSelect.value;
    secondarySelect.value = ids.has(previous.secondary) ? previous.secondary : "";
    previewSelect.value = ids.has(previous.preview) ? previous.preview : defaultSelect.value;

    for (const node of [defaultSelect, primarySelect, secondarySelect, previewSelect]) {
        node.disabled = !configured || !library.length;
    }
    host.querySelectorAll("[data-remove-elevenlabs-voice]").forEach((button) => {
        button.disabled = !configured;
    });
}

function renderProviders() {
    const elevenProvider = state.providerStatus.find((row) => row.provider === "elevenlabs");
    const invideoProvider = state.providerStatus.find((row) => row.provider === "invideo");
    const enabled = Boolean(state.elevenlabsConfig?.enabled);
    const connected = enabled && Boolean(state.elevenlabsStatus?.connected);
    const configured = Boolean(elevenProvider?.configured);
    const voiceConfigured = enabled && configured;
    const toggle = $("elevenlabs-enabled");
    const panel = $("elevenlabs-panel");
    const card = $("voice-provider-card");

    toggle.checked = enabled;
    toggle.disabled = !state.channelId || state.voiceToggleBusy;
    toggle.setAttribute("aria-expanded", enabled ? "true" : "false");
    $("elevenlabs-enabled-label").textContent = enabled ? "Enabled" : "Disabled";
    panel.setAttribute("aria-hidden", enabled ? "false" : "true");
    panel.inert = !enabled;
    card.classList.toggle("is-enabled", enabled);

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

    const discoveredList = $("elevenlabs-voices");
    discoveredList.innerHTML = state.elevenlabsVoices.map((voice) =>
        '<option value="' + escapeHtml(voice.voice_id || "") + '">' +
        escapeHtml(voice.name || voice.voice_id || "Voice") +
        (voice.category ? " · " + escapeHtml(voice.category) : "") +
        "</option>"
    ).join("");
    $("elevenlabs-new-voice-id").disabled = !voiceConfigured;
    $("add-elevenlabs-voice").disabled = !voiceConfigured;

    renderVoiceLibrary(voiceConfigured);

    const defaultVoice = $("elevenlabs-default-voice").value;
    const selectedVoice = discoveredVoice(defaultVoice)
        || state.elevenlabsVoiceLibrary.find((voice) => voice.voice_id === defaultVoice);
    $("elevenlabs-voice-help").textContent = state.elevenlabsVoiceLibrary.length
        ? "Default: " +
            (selectedVoice?.name || defaultVoice) +
            ". Long-form Voice A and Voice B are stored independently for multi-host narration."
        : configured
            ? "Add a voice ID from your ElevenLabs account. You can keep up to 12 per channel."
            : "Configure the ElevenLabs API key in Katcha first.";

    const modelSelect = $("elevenlabs-model");
    const savedModel = state.elevenlabsConfig?.model_id || state.elevenlabsStatus?.model_id || "";
    const models = [...state.elevenlabsModels];
    if (savedModel && !models.some((row) => row.model_id === savedModel)) {
        models.unshift({ model_id: savedModel, name: state.elevenlabsConfig?.model_name || savedModel });
    }
    modelSelect.innerHTML = models.length
        ? models.map((model) =>
            '<option value="' + escapeHtml(model.model_id || "") + '">' +
            escapeHtml(model.name || model.model_id || "TTS model") +
            "</option>"
        ).join("")
        : '<option value="">No TTS models discovered</option>';
    if (savedModel) modelSelect.value = savedModel;
    modelSelect.disabled = !voiceConfigured || !models.length;

    updateProviderActionState();

    $("invideo-state").textContent = invideoProvider?.configured ? "BRIDGE READY" : "UNAVAILABLE";
    $("invideo-detail").textContent = invideoProvider?.detail
        || "Direct InVideo automation is waiting for a documented account API contract.";
    $("invideo-studio-link").href = "/studio?channel=" + encodeURIComponent(state.channelId);
}

function updateProviderActionState() {
    const configured = Boolean(
        state.elevenlabsConfig?.enabled &&
        state.providerStatus.find((row) => row.provider === "elevenlabs")?.configured,
    );
    const defaultVoice = $("elevenlabs-default-voice").value;
    const previewVoice = $("elevenlabs-preview-voice").value;
    const modelId = $("elevenlabs-model").value;
    const actionable = configured && state.elevenlabsVoiceLibrary.length > 0 && Boolean(defaultVoice) && Boolean(modelId);
    $("save-elevenlabs").disabled = !actionable;
    $("preview-elevenlabs").disabled = !(configured && Boolean(previewVoice) && Boolean(modelId));
    $("refresh-elevenlabs").disabled = !configured;
}

async function setElevenLabsEnabled(enabled) {
    const previousConfig = state.elevenlabsConfig
        ? { ...state.elevenlabsConfig }
        : null;
    state.voiceToggleBusy = true;
    state.elevenlabsConfig = {
        ...(state.elevenlabsConfig || {}),
        enabled,
    };
    renderProviders();
    setStatus((enabled ? "Enabling" : "Disabling") + " voice for this channel…");
    try {
        state.elevenlabsConfig = await api(
            "/v1/integrations/elevenlabs/channels/" +
                encodeURIComponent(state.channelId) +
                "/enabled",
            {
                method: "PATCH",
                body: JSON.stringify({
                    enabled,
                    actor: "channel-studio",
                }),
            },
        );
        await loadProviderData();
        setStatus(
            "Voice " +
                (enabled ? "enabled" : "disabled") +
                " for " +
                channelName(activeChannel()) +
                ".",
            "success",
        );
    } catch (error) {
        state.elevenlabsConfig = previousConfig;
        setStatus(error.message, "error");
    } finally {
        state.voiceToggleBusy = false;
        renderProviders();
    }
}

function addElevenLabsVoice() {
    const input = $("elevenlabs-new-voice-id");
    const voiceId = input.value.trim();
    if (!voiceId) throw new Error("Paste or choose an ElevenLabs voice ID first.");
    if (state.elevenlabsVoiceLibrary.some((voice) => voice.voice_id === voiceId)) {
        input.value = "";
        setStatus("That voice is already in this channel's library.", "success");
        return;
    }
    if (state.elevenlabsVoiceLibrary.length >= 12) {
        throw new Error("A channel can save at most 12 ElevenLabs voices.");
    }
    const discovered = discoveredVoice(voiceId);
    state.elevenlabsVoiceLibrary.push({
        voice_id: voiceId,
        name: discovered?.name || voiceId,
        category: discovered?.category || null,
        labels: discovered?.labels || {},
    });
    input.value = "";
    renderProviders();
    setStatus("Voice added. Save the voice configuration to make it persistent.", "success");
}

function removeElevenLabsVoice(voiceId) {
    state.elevenlabsVoiceLibrary = state.elevenlabsVoiceLibrary.filter(
        (voice) => voice.voice_id !== voiceId,
    );
    renderProviders();
    setStatus("Voice removed from the draft configuration. Save to persist the change.", "success");
}

async function saveElevenLabsConfig() {
    const voiceId = $("elevenlabs-default-voice").value;
    const modelId = $("elevenlabs-model").value;
    if (!voiceId || !modelId) throw new Error("Add a voice, then choose the default voice and TTS model.");
    state.elevenlabsConfig = await api(
        "/v1/integrations/elevenlabs/channels/" + encodeURIComponent(state.channelId),
        {
            method: "PUT",
            body: JSON.stringify({
                enabled: true,
                voice_id: voiceId,
                saved_voice_ids: state.elevenlabsVoiceLibrary.map((voice) => voice.voice_id),
                longform_primary_voice_id: $("elevenlabs-longform-primary").value || voiceId,
                longform_secondary_voice_id: $("elevenlabs-longform-secondary").value || null,
                model_id: modelId,
                actor: "channel-studio",
            }),
        },
    );
    state.elevenlabsVoiceLibrary = (state.elevenlabsConfig.saved_voices || []).map(
        (voice) => ({ ...voice }),
    );
    state.elevenlabsStatus = await api(
        "/v1/integrations/elevenlabs/status?channel_profile_id=" + encodeURIComponent(state.channelId),
    );
    renderProviders();
    setStatus(
        "ElevenLabs voice library saved for " + channelName(activeChannel()) + ".",
        "success",
    );
}

async function previewElevenLabsVoice() {
    const voiceId = $("elevenlabs-preview-voice").value;
    const modelId = $("elevenlabs-model").value;
    const text = $("elevenlabs-preview-text").value.trim();
    if (!voiceId || !modelId || !text) throw new Error("Preview voice, model, and preview text are required.");
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
    setStatus("ElevenLabs preview generated for " + (discoveredVoice(voiceId)?.name || voiceId) + ".", "success");
}

async function loadChannel() {
    if (!state.channelId) return;
    sessionStorage.setItem("katcha.channel", state.channelId);
    setStatus("Loading channel operations…");
    state.analytics.clear();
    state.packagingVariants.clear();
    state.selectedPublicationId = "";
    const channel = activeChannel();
    try {
        const [summary, operations, publications, productions, brands] = await Promise.all([
            api("/v1/channels/" + state.channelId),
            api(
                "/v1/operations/overview?channel_profile_id=" +
                    encodeURIComponent(state.channelId) +
                    "&limit=25",
            ),
            api(
                "/v1/publications?limit=250&youtube_connection_id=" +
                    encodeURIComponent(channel.youtube_connection_id),
            ),
            api("/v1/productions?limit=100&channel_profile_id=" + encodeURIComponent(state.channelId)),
            api("/v1/channels/" + state.channelId + "/brands"),
        ]);
        state.summary = summary;
        state.operations = operations;
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
    renderChannelActivity();
    renderPublications();
    renderGrowth();
    renderProductions();
    renderControls();
    renderProviders();
}

function renderChannelHeader() {
    const channel = state.summary?.profile || activeChannel();
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

    $("overview-channel-name").textContent = title;
    $("overview-channel-handle").textContent =
        channel?.profile_metadata?.channel_handle || "—";
    $("overview-channel-profile-id").textContent = channel?.id || "—";
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

function renderChannelActivity() {
    const container = $("channel-activity-list");
    const operations = state.operations || {};
    const intake = (operations.intake || []).map((item) => ({
        ...item,
        lane: "Intake",
        state: item.stage === "download_failed" || item.stage === "needs_attention"
            ? "attention"
            : "active",
    }));
    const attention = (operations.attention || []).map((item) => ({
        ...item,
        lane: "Needs attention",
    }));
    const active = (operations.active || []).map((item) => ({
        ...item,
        lane: item.kind === "publication" ? "Publishing" : "Production",
    }));
    const recent = (operations.activity || []).map((item) => ({
        ...item,
        id: item.aggregate_id,
        title: friendly(item.event_type || "Recent activity"),
        status: "completed",
        stage: "recent",
        state: "recent",
        lane: "Recent",
        message: friendly(item.aggregate_type || "event"),
        updated_at: item.created_at,
    }));
    const rows = [...intake, ...attention, ...active, ...recent]
        .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))
        .slice(0, 24);

    if (!rows.length) {
        container.innerHTML =
            '<div class="studio-card empty-state">Nothing is queued or running for this channel right now.</div>';
        return;
    }
    container.innerHTML = rows.map((item) => {
        const link = item.source_url
            ? '<a target="_blank" rel="noopener noreferrer" href="' +
                escapeHtml(item.source_url) +
                '">Source ↗</a>'
            : item.href
                ? '<a href="' + escapeHtml(item.href) + '">Open ↗</a>'
                : "";
        const message = item.message
            ? '<small>' + escapeHtml(item.message) + '</small>'
            : "";
        return (
            '<article class="studio-card channel-activity-item ' +
            (item.state === "attention" ? "is-attention" : "") +
            '"><div class="channel-activity-head"><span class="eyebrow">' +
            escapeHtml(item.lane || "Activity") +
            '</span><span class="status-pill ' +
            escapeHtml(item.status || "active") +
            '">' +
            escapeHtml(friendly(item.stage || item.status || "active")) +
            '</span></div><strong>' +
            escapeHtml(item.title || "Untitled work") +
            '</strong>' +
            message +
            '<div class="channel-activity-foot"><span>' +
            escapeHtml(dateText(item.updated_at)) +
            '</span>' +
            link +
            '</div></article>'
        );
    }).join("");
}


function datetimeLocalValue(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
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
    const selected = state.publications.find((item) => item.id === id);
    const needsPreuploadPackaging =
        selected?.stage === "metadata_hold" && !selected.youtube_video_id;
    if (needsPreuploadPackaging && !state.packagingVariants.has(id)) {
        try {
            const variants = await api("/v1/publications/" + id + "/packaging/variants");
            state.packagingVariants.set(id, variants || []);
        } catch (error) {
            state.packagingVariants.set(id, []);
            setStatus("Video loaded, but packaging options could not be read: " + error.message, "error");
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
        '<a class="studio-button secondary small" href="' +
        escapeHtml(
            askKatchaHref(
                "publication",
                item.id,
                "Explain this video’s performance, current state, and the strongest evidence-backed change to test next.",
            ),
        ) +
        '">Ask Katcha ✦</a>' +
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

    if (item.stage === "metadata_hold" && !item.youtube_video_id) {
        const variants = state.packagingVariants.get(item.id) || [];
        const applied = item.treatment_metadata?.preupload_packaging?.variant_id || "";
        const variantMarkup = variants.length
            ? '<div class="prepublish-variants">' +
                variants.map((variant) =>
                    '<button type="button" class="seo-variant' +
                    (String(variant.id) === String(applied) ? " is-selected" : "") +
                    '" data-apply-packaging="' + escapeHtml(variant.id) + '">' +
                    '<strong>' + escapeHtml(variant.title) + '</strong>' +
                    '<small>' + escapeHtml((variant.tags || []).slice(0, 8).join(" · ")) + '</small>' +
                    '</button>'
                ).join("") +
                '</div>'
            : '<div class="empty-state compact">No SEO options generated yet.</div>';
        $("video-detail").insertAdjacentHTML(
            "beforeend",
            '<div class="prepublish-panel"><span class="eyebrow">PRE-PUBLISH CONTROL</span>' +
            '<h4>SEO package & publish plan</h4>' +
            '<p class="detail-note">Nothing has been uploaded yet. Choose the metadata package and timing here.</p>' +
            variantMarkup +
            '<div class="prepublish-actions">' +
            '<button id="generate-packaging-options" class="studio-button secondary small" type="button">Generate SEO options</button>' +
            '</div><label class="provider-field">SCHEDULED TIME · DEVICE LOCAL TIME' +
            '<input id="publication-publish-at" type="datetime-local" value="' +
            escapeHtml(datetimeLocalValue(item.publish_at)) +
            '"></label>' +
            '<label class="prepublish-check"><input id="publication-notify" type="checkbox"' +
            (item.notify_subscribers ? " checked" : "") +
            '> Notify subscribers</label>' +
            '<div class="prepublish-actions">' +
            '<button id="publication-asap" class="studio-button secondary small" type="button">Set ASAP</button>' +
            '<button id="publication-schedule" class="studio-button secondary small" type="button">Save schedule</button>' +
            '<button id="publication-start" class="studio-button primary small" type="button"' +
            (applied ? "" : " disabled") +
            '>Start upload</button></div>' +
            '<small class="provider-help">' +
            (applied
                ? 'SEO package selected. Start upload when the publish plan is correct.'
                : 'Select an SEO package before starting the upload.') +
            '</small></div>'
        );

        $("video-detail").querySelectorAll("[data-apply-packaging]").forEach((button) => {
            button.addEventListener("click", () => applyPreuploadPackaging(button.dataset.applyPackaging));
        });
        $("generate-packaging-options")?.addEventListener("click", generatePackagingOptions);
        $("publication-asap")?.addEventListener("click", () => savePublicationPlan("asap"));
        $("publication-schedule")?.addEventListener("click", () => savePublicationPlan("scheduled"));
        $("publication-start")?.addEventListener("click", startHeldPublication);
    }
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

async function generatePackagingOptions() {
    const item = state.publications.find((row) => row.id === state.selectedPublicationId);
    if (!item) return;
    try {
        setStatus("Generating grounded SEO options…");
        const result = await api(
            "/v1/publications/" + item.id + "/packaging/generations",
            {
                method: "POST",
                body: JSON.stringify({
                    generation_key: "channel-studio-" + Date.now(),
                    candidate_count: 3,
                }),
            },
        );
        state.packagingVariants.set(item.id, result.variants || []);
        renderVideoDetail();
        setStatus("SEO options generated. Choose one before upload.", "success");
    } catch (error) {
        setStatus(error.message, "error");
    }
}


async function applyPreuploadPackaging(variantId) {
    const item = state.publications.find((row) => row.id === state.selectedPublicationId);
    if (!item) return;
    try {
        setStatus("Applying SEO package…");
        const updated = await api(
            "/v1/publications/" + item.id + "/packaging/preupload",
            {
                method: "POST",
                body: JSON.stringify({ variant_id: variantId, actor: "channel-studio" }),
            },
        );
        const index = state.publications.findIndex((row) => row.id === item.id);
        if (index >= 0) state.publications[index] = updated;
        renderPublications();
        setStatus("SEO package applied before upload.", "success");
    } catch (error) {
        setStatus(error.message, "error");
    }
}


async function savePublicationPlan(mode) {
    const item = state.publications.find((row) => row.id === state.selectedPublicationId);
    if (!item) return;
    const notify = Boolean($("publication-notify")?.checked);
    let publishAt = null;
    if (mode === "scheduled") {
        const value = $("publication-publish-at")?.value || "";
        if (!value) {
            setStatus("Choose a scheduled time first.", "error");
            return;
        }
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) {
            setStatus("The scheduled time is invalid.", "error");
            return;
        }
        publishAt = parsed.toISOString();
    }
    try {
        setStatus(mode === "scheduled" ? "Saving publication schedule…" : "Setting publication to ASAP…");
        const updated = await api(
            "/v1/publications/" + item.id + "/plan",
            {
                method: "POST",
                body: JSON.stringify({
                    publish_mode: mode,
                    publish_at: publishAt,
                    notify_subscribers: notify,
                    actor: "channel-studio",
                }),
            },
        );
        const index = state.publications.findIndex((row) => row.id === item.id);
        if (index >= 0) state.publications[index] = updated;
        renderPublications();
        setStatus(
            mode === "scheduled"
                ? "Publication schedule saved."
                : "Publication will go public as soon as upload and YouTube processing complete.",
            "success",
        );
    } catch (error) {
        setStatus(error.message, "error");
    }
}


async function startHeldPublication() {
    const item = state.publications.find((row) => row.id === state.selectedPublicationId);
    if (!item) return;
    if (!item.treatment_metadata?.preupload_packaging?.variant_id) {
        setStatus("Choose an SEO package before starting upload.", "error");
        return;
    }
    try {
        setStatus("Starting resumable private-first YouTube upload…");
        const updated = await api(
            "/v1/publications/" + item.id + "/start",
            {
                method: "POST",
                body: JSON.stringify({ actor: "channel-studio" }),
            },
        );
        const index = state.publications.findIndex((row) => row.id === item.id);
        if (index >= 0) state.publications[index] = updated;
        await loadChannel();
        setStatus("Upload started. Channel activity now tracks the publishing workflow.", "success");
    } catch (error) {
        setStatus(error.message, "error");
    }
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
                '</span><a class="studio-button secondary small" href="' +
                escapeHtml(
                    askKatchaHref(
                        "production",
                        item.id,
                        "Explain this production’s state, any failure evidence, and what should happen next.",
                    ),
                ) +
                '">Ask Katcha ✦</a></div></article>'
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

const CHANNEL_STUDIO_HASH_GROUPS = {
    overview: "overview",
    content: "content",
    production: "content",
    monetization: "growth",
    growth: "growth",
    identity: "settings",
    integrations: "settings",
    settings: "settings",
};

function channelStudioTabFromHash() {
    const hash = window.location.hash.replace(/^#/, "");
    return CHANNEL_STUDIO_HASH_GROUPS[hash] || "overview";
}

function setChannelStudioTab(tab, { updateHash = true, focus = false } = {}) {
    const selected = ["overview", "content", "growth", "settings"].includes(tab)
        ? tab
        : "overview";
    document.querySelectorAll("[data-channel-tab]").forEach((button) => {
        const active = button.dataset.channelTab === selected;
        button.setAttribute("aria-selected", active ? "true" : "false");
        button.tabIndex = active ? 0 : -1;
        if (active && focus) button.focus();
    });
    document.querySelectorAll("[data-studio-group]").forEach((section) => {
        section.hidden = section.dataset.studioGroup !== selected;
    });

    if (updateHash) {
        const next = window.location.pathname + window.location.search + "#" + selected;
        window.history.replaceState(null, "", next);
    }
}

function installChannelStudioTabs() {
    const tabs = [...document.querySelectorAll("[data-channel-tab]")];
    if (!tabs.length) return;

    tabs.forEach((button, index) => {
        button.addEventListener("click", () => {
            setChannelStudioTab(button.dataset.channelTab);
        });
        button.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            let nextIndex = index;
            if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
            if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
            if (event.key === "Home") nextIndex = 0;
            if (event.key === "End") nextIndex = tabs.length - 1;
            setChannelStudioTab(tabs[nextIndex].dataset.channelTab, {
                focus: true,
            });
        });
    });

    window.addEventListener("hashchange", () => {
        const rawHash = window.location.hash.replace(/^#/, "");
        const tab = channelStudioTabFromHash();
        setChannelStudioTab(tab, { updateHash: false });
        if (rawHash && rawHash !== tab) {
            window.requestAnimationFrame(() => {
                document.getElementById(rawHash)?.scrollIntoView({ block: "start" });
            });
        }
    });

    setChannelStudioTab(channelStudioTabFromHash(), { updateHash: false });
}

installChannelStudioTabs();

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
$("elevenlabs-enabled").addEventListener("change", async (event) => {
    await setElevenLabsEnabled(event.target.checked);
});
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
$("add-elevenlabs-voice").addEventListener("click", () => {
    try {
        addElevenLabsVoice();
    } catch (error) {
        setStatus(error.message, "error");
    }
});
$("elevenlabs-saved-voices").addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-elevenlabs-voice]");
    if (!button) return;
    removeElevenLabsVoice(button.dataset.removeElevenlabsVoice);
});
$("elevenlabs-new-voice-id").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    try {
        addElevenLabsVoice();
    } catch (error) {
        setStatus(error.message, "error");
    }
});
for (const id of [
    "elevenlabs-default-voice",
    "elevenlabs-longform-primary",
    "elevenlabs-longform-secondary",
    "elevenlabs-preview-voice",
    "elevenlabs-model",
]) {
    $(id).addEventListener("change", updateProviderActionState);
}
window.addEventListener("beforeunload", revokeProviderPreview);
$("growth-goals-form").addEventListener("submit", saveGrowthGoals);
$("add-custom-goal").addEventListener("click", addCustomGoal);
$("growth-pace").addEventListener("change", (event) => {
    $("growth-pace-badge").textContent = event.target.value.toUpperCase();
});
$("content-search").addEventListener("input", renderPublications);
$("content-filter").addEventListener("change", renderPublications);
function handleChannelAction(event) {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.action === "connect-youtube") beginYouTubeOAuth();
    if (button.dataset.action === "create-channel") createChannel(button.dataset.connection);
}

$("setup-actions").addEventListener("click", handleChannelAction);
$("channel-manager-connections").addEventListener("click", handleChannelAction);
$("open-channel-manager").addEventListener("click", openChannelManager);
$("open-channel-manager-inline").addEventListener("click", openChannelManager);
$("close-channel-manager").addEventListener("click", closeChannelManager);
$("connect-another-youtube").addEventListener("click", beginYouTubeOAuth);

if (state.oauthResult) {
    connect();
}
