const $ = (id) => document.getElementById(id);
const message = (text, kind = "") => {
    const node = $("settings-message");
    node.textContent = text || "";
    node.className = "settings-message" + (kind ? " " + kind : "");
};

async function api(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
    });
    let payload = {};
    try { payload = await response.json(); } catch {}
    if (!response.ok) throw new Error(payload.detail || payload.error || response.statusText);
    return payload;
}

async function runtimeStatus() {
    const response = await fetch("/runtime/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Open Settings through the Katcha launcher to change local runtime credentials.");
    return response.json();
}

async function loadRuntime() {
    try {
        const runtime = await runtimeStatus();
        const settings = runtime.settings || {};
        $("ai-mode").value = settings.KATCHA_AI_EXECUTION_MODE || "auto";
        $("openai-key").placeholder = settings.KATCHA_OPENAI_API_KEY
            ? "Saved credential — leave blank to keep"
            : "Not configured";
        $("gemini-key").placeholder = settings.KATCHA_GEMINI_API_KEY
            ? "Saved credential — leave blank to keep"
            : "Not configured";
        $("openai-state").textContent = settings.KATCHA_OPENAI_API_KEY
            ? "Configured as an API fallback."
            : "Optional fallback.";
        $("gemini-state").textContent = settings.KATCHA_GEMINI_API_KEY
            ? "Configured as a Gemini fallback."
            : "Used for Gemini-native workloads and fallback.";
        $("settings-state").textContent = "LOCAL";
        $("settings-state").className = "simulation connected";
    } catch (error) {
        $("settings-state").textContent = "READ ONLY";
        message(error.message, "error");
        $("save-ai").disabled = true;
    }
}

async function loadChatGPT() {
    const status = await api("/v1/integrations/chatgpt/status");
    const connected = Boolean(status.connected);
    $("chatgpt-badge").textContent = connected ? "CONNECTED" : "NOT CONNECTED";
    $("chatgpt-badge").className = "status-pill" + (connected ? "" : " muted");
    $("chatgpt-connect").hidden = connected;
    $("chatgpt-connect").dataset.connectionId = status.saved_connection_id || "";
    $("chatgpt-reconnect").hidden = !connected;
    $("chatgpt-disconnect").hidden = !connected;
    $("chatgpt-account").hidden = !connected;
    $("chatgpt-model-field").hidden = !connected;
    if (!connected) return;

    $("chatgpt-identity").textContent =
        status.display_name || status.email || "Connected ChatGPT account";
    $("chatgpt-expiry").textContent = status.selected_model
        ? "Using " + status.selected_model
        : "Model selection pending";

    const models = await api("/v1/integrations/chatgpt/models");
    $("chatgpt-model").replaceChildren(
        ...models.map((model) => {
            const option = document.createElement("option");
            option.value = model.slug;
            option.textContent = model.display_name + " · " + model.slug;
            option.selected = model.slug === status.selected_model;
            return option;
        }),
    );
}

async function ensureLiveMode() {
    if (location.port !== "8765") return;
    const runtime = await runtimeStatus();
    if ((runtime.settings || {}).KATCHA_AI_EXECUTION_MODE === "live") return;
    message("Enabling Live AI before ChatGPT sign-in…");
    await api("/runtime/settings", {
        method: "POST",
        body: JSON.stringify({ KATCHA_AI_EXECUTION_MODE: "live" }),
    });
    await api("/runtime/ai/apply", { method: "POST", body: "{}" });
    $("ai-mode").value = "live";
}

async function startChatGPT(connectionId = null) {
    await ensureLiveMode();
    message("Opening ChatGPT sign-in…");
    const suffix = connectionId ? "?connection_id=" + encodeURIComponent(connectionId) : "";
    const result = await api("/v1/integrations/chatgpt/oauth/start" + suffix, {
        method: "POST",
        body: "{}",
    });
    location.href = result.authorization_url;
}

$("chatgpt-connect").onclick = async () => {
    try {
        await startChatGPT($("chatgpt-connect").dataset.connectionId || null);
    } catch (error) {
        message(error.message, "error");
    }
};
$("chatgpt-reconnect").onclick = async () => {
    try {
        const status = await api("/v1/integrations/chatgpt/status");
        await startChatGPT(status.saved_connection_id || status.connection_id || null);
    } catch (error) { message(error.message, "error"); }
};
$("chatgpt-test").onclick = async () => {
    const button = $("chatgpt-test");
    button.disabled = true;
    try {
        const result = await api("/v1/integrations/chatgpt/test", { method: "POST", body: "{}" });
        message("ChatGPT plan is live · " + result.selected_model + " · " + result.model_count + " models available.", "good");
        await loadChatGPT();
    } catch (error) {
        message(error.message, "error");
    } finally { button.disabled = false; }
};
$("chatgpt-model").onchange = async () => {
    try {
        const result = await api("/v1/integrations/chatgpt/model", {
            method: "POST",
            body: JSON.stringify({ model: $("chatgpt-model").value }),
        });
        message("ChatGPT model changed to " + result.selected_model + ".", "good");
        await loadChatGPT();
    } catch (error) { message(error.message, "error"); }
};
$("chatgpt-disconnect").onclick = async () => {
    const button = $("chatgpt-disconnect");
    button.disabled = true;
    try {
        await api("/v1/integrations/chatgpt/disconnect", { method: "POST", body: "{}" });
        message("ChatGPT disconnected from Katcha.", "good");
        await loadChatGPT();
    } catch (error) {
        message(error.message, "error");
    } finally { button.disabled = false; }
};

async function testApiProvider(provider) {
    const button = provider === "openai" ? $("test-openai") : $("test-gemini");
    button.disabled = true;
    try {
        const result = await api("/v1/integrations/ai/providers/" + provider + "/test", {
            method: "POST",
            body: "{}",
        });
        message(provider.toUpperCase() + " · " + result.detail, "good");
    } catch (error) {
        message(error.message, "error");
    } finally {
        button.disabled = false;
    }
}
$("test-openai").onclick = () => testApiProvider("openai");
$("test-gemini").onclick = () => testApiProvider("gemini");

$("ai-settings-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = $("save-ai");
    button.disabled = true;
    try {
        const changes = { KATCHA_AI_EXECUTION_MODE: $("ai-mode").value };
        if ($("openai-key").value.trim()) changes.KATCHA_OPENAI_API_KEY = $("openai-key").value.trim();
        if ($("gemini-key").value.trim()) changes.KATCHA_GEMINI_API_KEY = $("gemini-key").value.trim();
        await api("/runtime/settings", { method: "POST", body: JSON.stringify(changes) });
        const applied = await api("/runtime/ai/apply", { method: "POST", body: "{}" });
        $("openai-key").value = "";
        $("gemini-key").value = "";
        message(
            applied.applied
                ? "AI settings saved and live AI services restarted."
                : "AI settings saved. Start Katcha to apply them.",
            "good",
        );
        await loadRuntime();
    } catch (error) {
        message(error.message, "error");
    } finally { button.disabled = false; }
};

(async () => {
    const query = new URLSearchParams(location.search);
    if (query.get("chatgpt") === "connected") message("ChatGPT account connected successfully.", "good");
    if (query.get("chatgpt") === "error") message("ChatGPT sign-in did not complete. Try again from this page.", "error");
    if (query.get("chatgpt") === "state_error") message("ChatGPT sign-in returned an invalid or expired session. Start a fresh sign-in from this page.", "error");
    await Promise.allSettled([loadRuntime(), loadChatGPT()]);
})();
