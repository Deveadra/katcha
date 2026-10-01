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

function usageWindowLabel(minutes, fallback) {
    const value = Number(minutes || 0);
    if (!value) return fallback;
    if (value % 10080 === 0) return (value / 10080) + "w limit";
    if (value % 1440 === 0) return (value / 1440) + "d limit";
    if (value % 60 === 0) return (value / 60) + "h limit";
    return value + "m limit";
}

function resetLabel(epochSeconds) {
    const epoch = Number(epochSeconds || 0);
    if (!epoch) return "Reset unavailable";
    const date = new Date(epoch * 1000);
    if (Number.isNaN(date.getTime())) return "Reset unavailable";
    return "Resets " + date.toLocaleString();
}

function renderCodexWindow(prefix, row, fallbackLabel) {
    const window = row || {};
    const known = typeof window.used_percent === "number" && Number.isFinite(window.used_percent);
    const percent = known ? Math.max(0, Math.min(100, window.used_percent)) : 0;
    $(prefix + "-label").textContent =
        usageWindowLabel(window.window_minutes, fallbackLabel);
    $(prefix + "-value").textContent = known ? percent.toFixed(percent % 1 ? 1 : 0) + "% used" : "Unavailable";
    $(prefix + "-bar").style.width = percent + "%";
    $(prefix + "-reset").textContent = resetLabel(window.resets_at);
}

async function loadCodexUsage() {
    const usage = await api("/v1/integrations/codex/usage");
    $("codex-usage").hidden = false;
    $("codex-plan").textContent = String(usage.plan_type || "ChatGPT").toUpperCase();
    renderCodexWindow("codex-primary", usage.primary, "5h limit");
    renderCodexWindow("codex-secondary", usage.secondary, "Weekly limit");
    const credits = usage.credits;
    if (credits && typeof credits === "object") {
        const balance = credits.balance ?? credits.remaining ?? credits.amount;
        $("codex-credits").textContent =
            credits.unlimited
                ? "Credits: unlimited"
                : balance !== undefined
                  ? "Credits: " + String(balance)
                  : "Credits: available";
    } else {
        $("codex-credits").textContent = "Credits: not reported";
    }
}

async function loadCodex() {
    const status = await api("/v1/integrations/codex/status");
    const connected = Boolean(status.connected);
    $("codex-badge").textContent = connected ? "CONNECTED" : "NOT CONNECTED";
    $("codex-badge").className = "status-pill" + (connected ? "" : " muted");
    $("codex-connect").hidden = connected;
    $("codex-reconnect").hidden = !connected;
    $("codex-disconnect").hidden = !connected;
    $("codex-account").hidden = !connected;
    $("codex-model-field").hidden = !connected;
    $("codex-usage").hidden = !connected;
    if (!connected) return;

    $("codex-identity").textContent = status.email || "Connected ChatGPT account";
    $("codex-expiry").textContent =
        (status.selected_model ? "Using " + status.selected_model : "Model pending") +
        (status.plan_type ? " · " + status.plan_type : "");

    const results = await Promise.allSettled([
        api("/v1/integrations/codex/models"),
        loadCodexUsage(),
    ]);
    if (results[1].status === "rejected") {
        renderCodexWindow("codex-primary", null, "5h limit");
        renderCodexWindow("codex-secondary", null, "Weekly limit");
        $("codex-credits").textContent = "Usage unavailable. Test response can still verify AI.";
    }
    if (results[0].status === "fulfilled") {
        const models = results[0].value;
        $("codex-model").replaceChildren(
            ...models.map((model) => {
                const option = document.createElement("option");
                option.value = model.slug;
                option.textContent = model.display_name + " · " + model.slug;
                option.selected = model.slug === status.selected_model;
                return option;
            }),
        );
    }
}

async function startCodex() {
    await ensureLiveMode();
    message("Opening ChatGPT sign-in for the Codex subscription path…");
    const result = await api("/v1/integrations/codex/oauth/start", {
        method: "POST",
        body: "{}",
    });
    location.href = result.authorization_url;
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

$("codex-connect").onclick = async () => {
    try {
        await startCodex();
    } catch (error) {
        message(error.message, "error");
    }
};
$("codex-reconnect").onclick = async () => {
    try {
        await api("/v1/integrations/codex/disconnect", { method: "POST", body: "{}" });
        await startCodex();
    } catch (error) {
        message(error.message, "error");
    }
};
$("codex-test").onclick = async () => {
    const button = $("codex-test");
    button.disabled = true;
    try {
        const result = await api("/v1/integrations/codex/test", {
            method: "POST",
            body: "{}",
        });
        message(
            "Codex plan is live · " + result.selected_model +
            " · real inference verified.",
            "good",
        );
        await loadCodex();
    } catch (error) {
        message(error.message, "error");
    } finally {
        button.disabled = false;
    }
};
$("codex-refresh-usage").onclick = async () => {
    const button = $("codex-refresh-usage");
    button.disabled = true;
    try {
        await loadCodexUsage();
        message("ChatGPT subscription usage refreshed.", "good");
    } catch (error) {
        message(error.message, "error");
    } finally {
        button.disabled = false;
    }
};
$("codex-model").onchange = async () => {
    try {
        const result = await api("/v1/integrations/codex/model", {
            method: "POST",
            body: JSON.stringify({ model: $("codex-model").value }),
        });
        message("Codex model changed to " + result.selected_model + ".", "good");
        await loadCodex();
    } catch (error) {
        message(error.message, "error");
    }
};
$("codex-disconnect").onclick = async () => {
    const button = $("codex-disconnect");
    button.disabled = true;
    try {
        await api("/v1/integrations/codex/disconnect", {
            method: "POST",
            body: "{}",
        });
        message("Codex ChatGPT connection disconnected from Katcha.", "good");
        await loadCodex();
    } catch (error) {
        message(error.message, "error");
    } finally {
        button.disabled = false;
    }
};

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
    if (query.get("codex") === "connected") {
        message("ChatGPT Codex subscription connected successfully.", "good");
    }
    if (query.get("codex") === "error") {
        message("Codex sign-in did not complete. Try again from this page.", "error");
    }
    if (query.get("codex") === "state_error") {
        message("Codex sign-in returned an invalid or expired session. Start a fresh sign-in.", "error");
    }
    if (query.get("chatgpt") === "connected") message("Direct ChatGPT sharing connected successfully.", "good");
    if (query.get("chatgpt") === "error") message("Direct ChatGPT sign-in did not complete. Try again from this page.", "error");
    if (query.get("chatgpt") === "state_error") message("Direct ChatGPT sign-in returned an invalid or expired session. Start a fresh sign-in from this page.", "error");
    await Promise.allSettled([loadRuntime(), loadCodex(), loadChatGPT()]);
})();

$("check-systems").onclick = async () => {
    const button = $("check-systems"), output = $("system-check-results");
    button.disabled = true;
    output.textContent = "Checking Katcha's services…";
    try {
        const result = await api("/v1/operations/system-check");
        output.replaceChildren();
        for (const check of result.checks) {
            const row = document.createElement("p");
            row.textContent = check.label + " · " + check.status + " — " + (typeof check.detail === "string" ? check.detail : "Saved data is available");
            output.append(row);
        }
        const copy = document.createElement("button");
        copy.className = "button secondary";
        copy.textContent = "Copy diagnostics";
        copy.onclick = async () => {
            try { await navigator.clipboard.writeText(JSON.stringify(result, null, 2)); copy.textContent = "Copied"; }
            catch { message("Clipboard is unavailable. Select and copy the check results.", "error"); }
        };
        output.append(copy);
    } catch (error) { output.textContent = error.message; }
    finally { button.disabled = false; }
};
