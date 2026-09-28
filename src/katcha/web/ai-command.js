const state = {
    token: "",
    channels: [],
    channelId: "",
    selectedClipIds: [],
    selectedProductionId: null,
    busy: false,
};

const $ = (id) => document.getElementById(id);

function headers() {
    return {
        "Content-Type": "application/json",
        ...(state.token ? { Authorization: "Bearer " + state.token } : {}),
    };
}

async function api(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: { ...headers(), ...(options.headers || {}) },
    });
    if (!response.ok) {
        let message = response.status + " " + response.statusText;
        try {
            const body = await response.json();
            message = body.detail || message;
        } catch {}
        throw new Error(message);
    }
    return response.status === 204 ? null : response.json();
}

function esc(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

function status(message, error = false) {
    $("status").textContent = message || "";
    $("status").className = "ai-status" + (error ? " error" : "");
}

function channelName(channel) {
    return (
        channel.profile_metadata?.channel_title ||
        channel.profile_metadata?.name ||
        channel.id
    );
}

function appendUser(text) {
    const article = document.createElement("article");
    article.className = "message user-message";
    article.innerHTML =
        '<div class="message-body"><span class="message-author">YOU</span><p>' +
        esc(text) +
        "</p></div>";
    $("thread").append(article);
    scrollThread();
}

function appendThinking() {
    const article = document.createElement("article");
    article.className = "message katcha-message thinking";
    article.id = "thinking-message";
    article.innerHTML =
        '<div class="message-avatar">K</div><div class="message-body"><span class="message-author">KATCHA AI</span><p>Reading channel evidence <span class="thinking-dot"></span><span class="thinking-dot"></span><span class="thinking-dot"></span></p></div>';
    $("thread").append(article);
    scrollThread();
}

function appendKatcha(result) {
    $("thinking-message")?.remove();
    const article = document.createElement("article");
    article.className = "message katcha-message";
    article.innerHTML =
        '<div class="message-avatar">K</div><div class="message-body"><span class="message-author">KATCHA AI</span><p>' +
        esc(result.answer) +
        '</p><div class="message-meta">' +
        esc(result.intent.replaceAll("_", " ")) +
        " · " +
        esc(result.evidence.length) +
        " evidence record" +
        (result.evidence.length === 1 ? "" : "s") +
        "</div></div>";
    $("thread").append(article);
    scrollThread();
}

function appendError(message) {
    $("thinking-message")?.remove();
    const article = document.createElement("article");
    article.className = "message katcha-message";
    article.innerHTML =
        '<div class="message-avatar">!</div><div class="message-body"><span class="message-author">KATCHA</span><p>' +
        esc(message) +
        "</p></div>";
    $("thread").append(article);
    scrollThread();
}

function scrollThread() {
    $("thread").scrollTop = $("thread").scrollHeight;
}

function renderSelection() {
    const host = $("selection-bar");
    if (!state.selectedClipIds.length && !state.selectedProductionId) {
        host.hidden = true;
        host.textContent = "";
        return;
    }
    host.hidden = false;
    host.innerHTML =
        "Using context: " +
        (state.selectedClipIds.length
            ? state.selectedClipIds.length + " selected clip" + (state.selectedClipIds.length === 1 ? "" : "s")
            : "") +
        (state.selectedProductionId ? " · production " + esc(state.selectedProductionId.slice(0, 8)) : "") +
        ' <button type="button" id="clear-selection">clear</button>';
    $("clear-selection").onclick = () => {
        state.selectedClipIds = [];
        state.selectedProductionId = null;
        renderSelection();
    };
}

function evidenceSummary(record) {
    if (record.kind === "clip") {
        const score =
            record.channel_score != null
                ? (Number(record.channel_score) * 100).toFixed(1) + "/100 channel score"
                : record.candidate_score != null
                  ? Number(record.candidate_score).toFixed(1) + "/100 candidate score"
                  : "Stored clip evidence";
        return [score, ...(record.why || []).slice(0, 2)].join(" · ");
    }
    if (record.kind === "render_attempt" || record.kind === "production" || record.kind === "compilation" || record.kind === "publication") {
        return [record.status, record.stage, record.error].filter(Boolean).join(" · ");
    }
    if (record.kind === "edit_performance") {
        return (
            String(record.publication_count || 0) +
            " maturity-matched publications · " +
            String(record.comparison_status || "unknown").replaceAll("_", " ")
        );
    }
    if (record.kind === "packaging_intelligence") {
        return (
            String(record.publication_count || 0) +
            " publications · " +
            String(record.recommendation_status || "unknown").replaceAll("_", " ")
        );
    }
    return JSON.stringify(record).slice(0, 220);
}

function renderContext(result) {
    $("narrator").textContent = result.narrator || "GROUNDED";
    const evidence = (result.evidence || [])
        .map((record) => {
            const selectable = record.kind === "clip" && record.id && record.id !== "current";
            return (
                '<article class="evidence-card"><div class="evidence-top"><span class="evidence-kind">' +
                esc(record.kind) +
                "</span><span class="evidence-kind">" +
                esc(String(record.id || "").slice(0, 8)) +
                "</span></div><strong>" +
                esc(record.title || record.error || record.kind.replaceAll("_", " ")) +
                "</strong><p>" +
                esc(evidenceSummary(record)) +
                "</p>" +
                (selectable
                    ? '<button type="button" data-select-clip="' +
                      esc(record.id) +
                      '">+ Use this clip as command context</button>'
                    : "") +
                "</article>"
            );
        })
        .join("");

    const actions = (result.actions || [])
        .map(
            (action) =>
                '<article class="action-card" data-action-card="' +
                esc(action.id) +
                '"><strong>' +
                esc(action.label) +
                "</strong><p>" +
                esc(action.description) +
                '</p><button type="button" data-action-id="' +
                esc(action.id) +
                '">Review & confirm</button><div class="action-result" hidden></div></article>',
        )
        .join("");

    const keyPoints = (result.key_points || []).length
        ? '<div class="context-section"><div class="context-section-head"><span>KATCHA NOTES</span><b>' +
          result.key_points.length +
          "</b></div><div class="evidence-list">" +
          result.key_points
              .map((point) => '<article class="evidence-card"><p>' + esc(point) + "</p></article>")
              .join("") +
          "</div></div>"
        : "";

    $("context-panel").innerHTML =
        keyPoints +
        '<section class="context-section"><div class="context-section-head"><span>EVIDENCE USED</span><b>' +
        (result.evidence || []).length +
        '</b></div><div class="evidence-list">' +
        (evidence || '<div class="context-empty" style="min-height:120px"><p>No supporting records were available for this answer.</p></div>') +
        "</div></section>" +
        '<section class="context-section"><div class="context-section-head"><span>PROPOSED ACTIONS</span><b>' +
        (result.actions || []).length +
        '</b></div><div class="action-list">' +
        (actions || '<div class="context-empty" style="min-height:100px"><p>This answer is informational; no action is proposed.</p></div>') +
        "</div></section>";

    $("context-panel").querySelectorAll("[data-select-clip]").forEach((button) => {
        button.onclick = () => {
            const id = button.dataset.selectClip;
            if (!state.selectedClipIds.includes(id)) state.selectedClipIds.push(id);
            renderSelection();
            button.textContent = "✓ Added to command context";
            button.disabled = true;
        };
    });

    const byId = new Map((result.actions || []).map((action) => [action.id, action]));
    $("context-panel").querySelectorAll("[data-action-id]").forEach((button) => {
        button.onclick = async () => {
            const action = byId.get(button.dataset.actionId);
            if (!action) return;
            if (!button.classList.contains("confirming")) {
                button.classList.add("confirming");
                button.textContent = "Confirm: " + action.label;
                return;
            }
            button.disabled = true;
            button.textContent = "Starting…";
            const card = button.closest("[data-action-card]");
            const output = card.querySelector(".action-result");
            try {
                const execution = await api("/v1/ai/actions/execute", {
                    method: "POST",
                    body: JSON.stringify({
                        channel_profile_id: state.channelId,
                        action_type: action.type,
                        payload: action.payload,
                        confirmed: true,
                        actor: "operator:katcha-ai",
                    }),
                });
                output.hidden = false;
                output.textContent = "Accepted · " + Object.entries(execution.result)
                    .slice(0, 2)
                    .map(([key, value]) => key.replaceAll("_", " ") + ": " + String(value))
                    .join(" · ");
                button.textContent = "✓ Accepted";
                appendKatcha({
                    answer: action.label + " was accepted by the Katcha control plane. The evidence panel contains the returned workflow reference.",
                    intent: "action_execution",
                    evidence: [],
                });
            } catch (error) {
                output.hidden = false;
                output.textContent = error.message;
                button.disabled = false;
                button.classList.remove("confirming");
                button.textContent = "Review & confirm";
            }
        };
    });
}

async function sendPrompt(text) {
    const prompt = text.trim();
    if (!prompt || !state.channelId || state.busy) return;
    state.busy = true;
    $("send").disabled = true;
    appendUser(prompt);
    appendThinking();
    $("prompt").value = "";
    autoResize();
    try {
        const result = await api("/v1/ai/command", {
            method: "POST",
            body: JSON.stringify({
                channel_profile_id: state.channelId,
                prompt,
                selected_clip_ids: state.selectedClipIds,
                selected_production_id: state.selectedProductionId,
            }),
        });
        appendKatcha(result);
        renderContext(result);
        status("");
    } catch (error) {
        appendError(error.message);
        status(error.message, true);
    } finally {
        state.busy = false;
        $("send").disabled = false;
        $("prompt").focus();
    }
}

async function connect(event) {
    event?.preventDefault();
    state.token = $("token").value.trim();
    $("token").value = "";
    status("Connecting to Katcha control plane…");
    try {
        state.channels = await api("/v1/channels");
        $("channel").innerHTML = state.channels
            .map(
                (channel) =>
                    '<option value="' +
                    esc(channel.id) +
                    '">' +
                    esc(channelName(channel)) +
                    " · " +
                    esc(channel.status) +
                    "</option>",
            )
            .join("");
        if (!state.channels.length) {
            status("No channel workspace is configured yet.", true);
            return;
        }
        state.channelId = state.channels[0].id;
        $("channel").value = state.channelId;
        $("command-center").hidden = false;
        $("connection-state").textContent = "CONNECTED";
        $("connection-state").className = "simulation connected";
        status("Katcha AI is ready. Answers will be scoped to " + channelName(state.channels[0]) + ".");
    } catch (error) {
        $("connection-state").textContent = "CONNECTION FAILED";
        status(error.message, true);
    }
}

function autoResize() {
    const input = $("prompt");
    input.style.height = "auto";
    input.style.height = Math.min(130, input.scrollHeight) + "px";
}

$("connect-form").onsubmit = connect;
$("command-form").onsubmit = (event) => {
    event.preventDefault();
    sendPrompt($("prompt").value);
};
$("prompt").addEventListener("input", autoResize);
$("prompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        $("command-form").requestSubmit();
    }
});
$("channel").onchange = () => {
    state.channelId = $("channel").value;
    state.selectedClipIds = [];
    state.selectedProductionId = null;
    renderSelection();
    status("Channel context changed. New answers will use this channel's stored data.");
};
$("clear-thread").onclick = () => {
    $("thread").innerHTML =
        '<article class="message katcha-message welcome-message"><div class="message-avatar">K</div><div class="message-body"><span class="message-author">KATCHA AI</span><p>Conversation cleared. Channel context and Katcha state were not changed.</p></div></article>';
    $("context-panel").innerHTML =
        '<div class="context-empty"><span>◇</span><strong>Nothing hidden behind the answer.</strong><p>Evidence used by Katcha AI will appear here.</p></div>';
    $("narrator").textContent = "NO QUERY";
};
document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.onclick = () => sendPrompt(button.dataset.prompt);
});
