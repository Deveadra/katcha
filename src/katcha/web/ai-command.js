const launchParams = new URLSearchParams(location.search);
const state = {
    token: sessionStorage.getItem("katcha.controlToken") || "",
    channels: [],
    channelId: "",
    threadId: "",
    threads: [],
    selectedClipIds: [],
    selectedProductionId: null,
    resourceRefs: [],
    controlSession: null,
    busy: false,
    durableGoals: false,
    deepLinkApplied: false,
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

function capability(name) {
    if (!state.controlSession) return false;
    if ((state.controlSession.scopes || []).includes("*")) return true;
    return Boolean(state.controlSession.capabilities?.[name]);
}

function actionPermission(action) {
    return state.controlSession?.action_permissions?.[action.type] || null;
}

function actionAllowed(action) {
    if (!state.controlSession) return false;
    if ((state.controlSession.scopes || []).includes("*")) return true;
    return Boolean(actionPermission(action)?.allowed);
}

function renderControlSession() {
    const session = state.controlSession;
    const host = $("principal-state");
    if (!session || !host) return;
    const identity =
        session.principal_name ||
        String(session.authentication_mode || "authenticated").replaceAll("_", " ");
    const channelLabel = session.channel_access?.all_channels
        ? "all channels"
        : String((session.channel_access?.channel_profile_ids || []).length) +
          " channel" +
          ((session.channel_access?.channel_profile_ids || []).length === 1 ? "" : "s");
    host.textContent = identity + " · " + channelLabel;
    host.hidden = false;

    const canCommand = capability("ai_command");
    $("prompt").disabled = !canCommand;
    $("send").disabled = !canCommand;
    document.querySelectorAll(".starter").forEach((button) => {
        button.disabled = !canCommand;
    });
    if (!canCommand) {
        $("prompt").placeholder = "This control principal has read-only Command Center access.";
    }
    $("thread-history").disabled = !capability("ai_read");
    $("archive-thread").disabled = !capability("ai_write") || !state.threadId;
}

function resetConversationView(message = "Start a new grounded conversation for this channel.") {
    $("thread").innerHTML =
        '<article class="message katcha-message welcome-message"><div class="message-avatar">K</div><div class="message-body"><span class="message-author">KATCHA AI</span><p>' +
        esc(message) +
        "</p></div></article>";
    $("context-panel").innerHTML =
        '<div class="context-empty"><span>◇</span><strong>Nothing hidden behind the answer.</strong><p>Evidence used by Katcha AI will appear here with IDs, stored metrics and auditable actions.</p></div>';
    $("narrator").textContent = "NO QUERY";
}

function newConversation(message = "New conversation ready. Existing history is preserved.") {
    state.threadId = "";
    state.selectedClipIds = [];
    state.selectedProductionId = null;
    state.resourceRefs = [];
    renderSelection();
    $("thread-history").value = "";
    $("archive-thread").disabled = true;
    resetConversationView(message);
}

async function loadThreads({ openLatest = true } = {}) {
    if (!state.channelId) return;
    if (!capability("ai_read")) {
        state.threads = [];
        $("thread-history").innerHTML =
            '<option value="">History unavailable for this principal</option>';
        $("thread-history").disabled = true;
        newConversation(
            "This principal can use Katcha AI commands, but cannot read durable conversation history.",
        );
        return;
    }
    const rows = await api(
        "/v1/ai/threads?channel_profile_id=" +
            encodeURIComponent(state.channelId) +
            "&limit=30",
    );
    state.threads = rows;
    $("thread-history").innerHTML =
        '<option value="">New conversation</option>' +
        rows
            .map(
                (thread) =>
                    '<option value="' +
                    esc(thread.thread_id) +
                    '">' +
                    esc(thread.title) +
                    "</option>",
            )
            .join("");

    const currentExists =
        state.threadId &&
        rows.some((thread) => thread.thread_id === state.threadId);
    if (currentExists) {
        $("thread-history").value = state.threadId;
        $("archive-thread").disabled = !capability("ai_write");
        return;
    }
    if (openLatest && rows.length) {
        await openThread(rows[0].thread_id);
        return;
    }
    newConversation();
}

async function openThread(threadId) {
    if (!threadId) {
        newConversation();
        return;
    }
    const detail = await api(
        "/v1/ai/threads/" + encodeURIComponent(threadId),
    );
    state.threadId = detail.thread.thread_id;
    state.selectedClipIds = [];
    state.selectedProductionId = null;
    state.resourceRefs = [];
    renderSelection();
    $("thread-history").value = state.threadId;
    $("archive-thread").disabled = !capability("ai_write");
    $("thread").innerHTML = "";

    for (const turn of detail.turns || []) {
        if (turn.role === "user") {
            appendUser(turn.content);
        } else if (turn.role === "assistant") {
            appendKatcha({
                answer: turn.content,
                intent: turn.intent || "grounded_answer",
                evidence: turn.evidence || [],
                ai_notice: (turn.narrator || "").startsWith("katcha/")
                    ? "This saved answer used Katcha's stored-data summary because live AI was unavailable."
                    : null,
            });
        }
    }
    if (!(detail.turns || []).length) {
        resetConversationView("This conversation has no recorded turns yet.");
        return;
    }

    const assistantTurns = (detail.turns || []).filter(
        (turn) => turn.role === "assistant",
    );
    const latest = assistantTurns.at(-1);
    if (latest) {
        const actionSourceTurnId =
            latest.context?.action_source_turn_id || latest.turn_id;
        const actions = (detail.actions || [])
            .filter((action) => action.source_turn_id === actionSourceTurnId)
            .map((action) => ({
                ...action,
                type: action.action_type,
            }));
        renderContext({
            narrator: latest.narrator || "GROUNDED",
            evidence: latest.evidence || [],
            key_points: latest.context?.key_points || [],
            caveats: latest.context?.caveats || [],
            planning: latest.context?.planning || null,
            actions,
        });
    }
    scrollThread();
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
        '<div class="message-avatar">K</div><div class="message-body"><span class="message-author">KATCHA AI</span><p>Checking channel evidence and discovery tools <span class="thinking-dot"></span><span class="thinking-dot"></span><span class="thinking-dot"></span></p></div>';
    $("thread").append(article);
    scrollThread();
}

function appendKatcha(result) {
    $("thinking-message")?.remove();
    const article = document.createElement("article");
    article.className = "message katcha-message";
    article.innerHTML =
        '<div class="message-avatar">K</div><div class="message-body"><span class="message-author">KATCHA AI</span><p>' +
        (result.ai_notice ? '<span class="ai-answer-notice">' + esc(result.ai_notice) + '</span>' : '') +
        esc(result.answer) +
        '</p><div class="message-meta">' +
        esc(String(result.intent || "grounded_answer").replaceAll("_", " ")) +
        " · " +
        esc((result.evidence || []).length) +
        " evidence record" +
        ((result.evidence || []).length === 1 ? "" : "s") +
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
    if (
        !state.selectedClipIds.length &&
        !state.selectedProductionId &&
        !state.resourceRefs.length
    ) {
        host.hidden = true;
        host.textContent = "";
        return;
    }
    const parts = [];
    if (state.selectedClipIds.length) {
        parts.push(
            state.selectedClipIds.length +
                " selected clip" +
                (state.selectedClipIds.length === 1 ? "" : "s"),
        );
    }
    if (state.selectedProductionId) {
        parts.push("production " + state.selectedProductionId.slice(0, 8));
    }
    for (const ref of state.resourceRefs) {
        parts.push(
            ref.kind.replaceAll("_", " ") +
                " " +
                String(ref.id).slice(0, 8) +
                (ref.selector
                    ? " · revision " + ref.revision + " · beat " + ref.selector
                    : ""),
        );
    }
    host.hidden = false;
    host.innerHTML =
        "Using context: " +
        parts.map(esc).join(" · ") +
        ' <button type="button" id="clear-selection">clear</button>';
    $("clear-selection").onclick = () => {
        state.selectedClipIds = [];
        state.selectedProductionId = null;
        state.resourceRefs = [];
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
    if (record.kind === "render_attempt" || record.kind === "production" || record.kind === "compilation" || record.kind === "publication" || record.kind === "short_episode") {
        return [record.status, record.stage, record.error, record.premise].filter(Boolean).join(" · ");
    }
    if (record.kind === "trend_opportunity") {
        const score = record.calibrated_score ?? record.opportunity_score;
        return [
            record.topic,
            score != null ? (Number(score) * 100).toFixed(0) + "/100" : "",
            record.lifecycle,
            ...(record.reasons || []).slice(0, 2),
        ].filter(Boolean).join(" · ");
    }
    if (record.kind === "editorial_project") {
        const run = record.latest_run || {};
        const beat = record.selected_beat || {};
        return [
            beat.id ? "selected beat " + beat.id + " · revision " + beat.revision : "",
            "revision " + String(record.revision ?? 0),
            String(record.source_count || 0) + " sources",
            String(record.managed_clip_count || 0) + " managed clips",
            run.stage ? "latest stage " + String(run.stage).replaceAll("_", " ") : "",
            run.error || "",
        ].filter(Boolean).join(" · ");
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
    if (record.kind === "source_discovery") {
        const platforms =
            (record.requested_platforms || []).join(", ") || "wide web";
        const readiness = record.web_scout_ready
            ? "autonomous scout ready"
            : String(record.web_scout_readiness || "scout unavailable");
        return (
            platforms +
            " · " +
            readiness +
            " · " +
            String(record.configured_source_count || 0) +
            " saved sources"
        );
    }
    return JSON.stringify(record).slice(0, 220);
}

async function refreshObservability() {
    if (!state.channelId || !$("observability") || !capability("ai_read")) {
        if ($("observability")) $("observability").hidden = true;
        return;
    }
    try {
        const row = await api(
            "/v1/ai/observability?channel_profile_id=" +
                encodeURIComponent(state.channelId) +
                "&hours=24",
        );
        $("obs-requests").textContent = String(row.request_count || 0);
        $("obs-latency").textContent =
            String(row.p95_latency_ms || 0) + " ms";
        $("obs-cost").textContent =
            "$" + Number(row.estimated_cost_usd || 0).toFixed(4);
        $("obs-context").textContent =
            String(row.typed_context_request_count || 0);
        $("observability").hidden = false;
    } catch {
        $("observability").hidden = true;
    }
}

function applyDeepLinkContext() {
    if (state.deepLinkApplied) return;
    const requestedChannel = launchParams.get("channel");
    const kind = launchParams.get("resource_kind");
    const id = launchParams.get("resource_id");
    const selector = launchParams.get("resource_selector");
    const revisionParam = launchParams.get("resource_revision");
    const revision =
        revisionParam != null && /^\d+$/.test(revisionParam)
            ? Number(revisionParam)
            : null;
    const beatContextReady =
        kind === "editorial_project" && selector && revision != null;
    const prompt = launchParams.get("prompt");
    const allowedKinds = new Set([
        "clip",
        "production",
        "short_episode",
        "publication",
        "trend_opportunity",
        "editorial_project",
    ]);
    if (requestedChannel && state.channels.some((row) => row.id === requestedChannel)) {
        state.channelId = requestedChannel;
        $("channel").value = requestedChannel;
    }
    if (kind && id && allowedKinds.has(kind)) {
        state.threadId = "";
        state.selectedClipIds = [];
        state.selectedProductionId = kind === "production" ? id : null;
        state.resourceRefs = [{
            kind,
            id,
            ...(
                beatContextReady ? {selector, revision} : {}
            ),
        }];
        $("thread-history").value = "";
        $("archive-thread").disabled = true;
        resetConversationView(
            "Typed " +
                kind.replaceAll("_", " ") +
                (beatContextReady ? " beat context" : " context") +
                " is attached from another Katcha workspace.",
        );
        renderSelection();
    }
    if (prompt) {
        $("prompt").value = prompt.slice(0, 4000);
        autoResize();
    }
    state.deepLinkApplied = true;
}

function actionPayloadSummary(action) {
    const payload = action.payload || {};
    if (action.type === "create_ranked_short_episode") {
        const clips = Array.isArray(payload.clip_ids) ? payload.clip_ids : [];
        const recipe = payload.edit_blueprint_key || "channel default";
        return clips.length + " locked clips · " + recipe + " · preserve order";
    }
    if (action.type === "create_short_production") {
        const clip = String(payload.clip_id || "").slice(0, 8);
        const recipe = payload.edit_blueprint_key || "channel default";
        return "clip " + clip + " · " + recipe;
    }
    if (action.type === "recover_production_render") {
        return "production " + String(payload.production_id || "").slice(0, 8);
    }
    if (action.type === "refresh_channel_intelligence") {
        return "recompute channel-scoped intelligence";
    }
    if (action.type === "start_source_scout") {
        const platforms =
            (payload.platforms || []).join(", ") || "wide web";
        const interval = Number(payload.interval_minutes || 60);
        return (
            platforms +
            " · every " +
            interval +
            " min · " +
            String((payload.terms || []).length) +
            " topic terms"
        );
    }
    if (action.type === "editorial_storyboard_edit") {
        const beat = payload.beat || {};
        const version = Number(payload.expected_workspace_version || 0);
        return (
            "beat " +
            String(beat.beat_id || "?") +
            " · workspace v" +
            version +
            " → v" +
            (version + 1) +
            " · " +
            String(beat.layout || "unassigned").replaceAll("_", " ")
        );
    }
    return "";
}


function actionActivitySummary(activity) {
    const resource = activity.resource;
    if (resource) {
        return [
            resource.kind.replaceAll("_", " "),
            resource.status.replaceAll("_", " "),
            resource.stage && resource.stage !== resource.status
                ? resource.stage.replaceAll("_", " ")
                : "",
            resource.error || "",
        ]
            .filter(Boolean)
            .join(" · ");
    }
    return [
        activity.state?.replaceAll("_", " "),
        activity.workflow_id ? "workflow " + activity.workflow_id : "",
    ]
        .filter(Boolean)
        .join(" · ");
}

async function refreshActionActivity(proposalId, card, attempt = 0, keepPolling = false) {
    if (!card || !document.body.contains(card)) return;
    const output = card.querySelector(".action-result");
    try {
        const activity = await api(
            "/v1/ai/actions/" + encodeURIComponent(proposalId) + "/activity",
        );
        output.hidden = false;
        output.textContent = actionActivitySummary(activity) || "No workflow activity yet.";
        card.dataset.activityState = activity.state || "";
        const activityButton = card.querySelector("[data-activity-id]");
        if (activityButton) {
            activityButton.textContent = activity.settled
                ? "Status is current"
                : "Refresh workflow status";
            activityButton.disabled = Boolean(activity.settled);
        }
        if (
            keepPolling &&
            !activity.settled &&
            attempt < 20 &&
            document.body.contains(card)
        ) {
            window.setTimeout(
                () => refreshActionActivity(proposalId, card, attempt + 1, true),
                3000,
            );
        }
    } catch (error) {
        output.hidden = false;
        output.textContent = "Status check failed: " + error.message;
    }
}

function renderContext(result) {
    $("narrator").textContent = result.narrator || "GROUNDED";
    const evidence = (result.evidence || [])
        .map((record) => {
            const selectable = record.kind === "clip" && record.id && record.id !== "current";
            return (
                '<article class="evidence-card"><div class="evidence-top"><span class="evidence-kind">' +
                esc(record.kind) +
                '</span><span class="evidence-kind">' +
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
        .map((action) => {
            const principalCanExecute = actionAllowed(action);
            const actionable = ["proposed", "failed"].includes(
                action.status || "proposed",
            );
            const canExecute = principalCanExecute && actionable;
            const canRevise = capability("ai_write") && actionable;
            const applyLabel =
                action.type === "editorial_storyboard_edit"
                    ? "Apply edit"
                    : "Review & confirm";
            const buttonLabel =
                action.status === "executed"
                    ? "✓ Executed"
                    : action.status === "executing"
                      ? "Executing…"
                      : action.status === "expired"
                        ? "Expired"
                        : !principalCanExecute
                          ? "Not permitted"
                          : applyLabel;
            return (
                '<article class="action-card" data-action-card="' +
                esc(action.proposal_id) +
                '"' +
                (!principalCanExecute && actionPermission(action)?.required_scope
                    ? ' title="Requires ' +
                      esc(actionPermission(action).required_scope) +
                      '"'
                    : "") +
                '><strong>' +
                esc(action.label) +
                "</strong><p>" +
                esc(action.description) +
                '</p><div class="action-payload">' +
                esc(actionPayloadSummary(action)) +
                '</div><button type="button" data-action-id="' +
                esc(action.proposal_id) +
                '"' +
                (canExecute ? "" : " disabled") +
                ">" +
                esc(buttonLabel) +
                "</button>" +
                (canRevise
                    ? '<button type="button" class="activity-button" data-modify-action="' +
                      esc(action.proposal_id) +
                      '">Modify</button><button type="button" class="activity-button" data-reject-action="' +
                      esc(action.proposal_id) +
                      '">Reject</button>'
                    : "") +
                (["executed", "executing"].includes(action.status)
                    ? '<button type="button" class="activity-button" data-activity-id="' +
                      esc(action.proposal_id) +
                      '">Check workflow status</button>'
                    : "") +
                '<div class="action-result"' +
                (action.result && Object.keys(action.result).length
                    ? ""
                    : " hidden") +
                ">" +
                esc(
                    action.result && Object.keys(action.result).length
                        ? Object.entries(action.result)
                              .slice(0, 2)
                              .map(
                                  ([key, value]) =>
                                      key.replaceAll("_", " ") +
                                      ": " +
                                      String(value),
                              )
                              .join(" · ")
                        : "",
                ) +
                "</div></article>"
            );
        })
        .join("");

    const planning = result.planning
        ? '<div class="context-section"><div class="context-section-head"><span>COMMAND ROUTING</span><b>' +
          esc(String(Math.round(Number(result.planning.confidence || 0) * 100))) +
          '%</b></div><article class="planner-card"><strong>' +
          esc(String(result.planning.intent || "unknown").replaceAll("_", " ")) +
          '</strong><span>' +
          esc(result.planning.source || "deterministic") +
          " · " +
          esc(result.planning.provider || "katcha") +
          "/" +
          esc(result.planning.model || "router") +
          '</span><p>' +
          esc(result.planning.reason || "") +
          "</p></article></div>"
        : "";

    const keyPoints = (result.key_points || []).length
        ? '<div class="context-section"><div class="context-section-head"><span>KATCHA NOTES</span><b>' +
          result.key_points.length +
          '</b></div><div class="evidence-list">' +
          result.key_points
              .map((point) => '<article class="evidence-card"><p>' + esc(point) + "</p></article>")
              .join("") +
          "</div></div>"
        : "";

    $("context-panel").innerHTML =
        planning +
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

    const byId = new Map((result.actions || []).map((action) => [action.proposal_id, action]));

    $("context-panel").querySelectorAll("[data-activity-id]").forEach((button) => {
        button.onclick = async () => {
            button.disabled = true;
            button.textContent = "Checking…";
            const card = button.closest("[data-action-card]");
            await refreshActionActivity(button.dataset.activityId, card);
            if (!button.disabled) {
                button.textContent = "Refresh workflow status";
            }
        };
    });

    async function rejectRenderedAction(action, card, reason) {
        const response = await api(
            "/v1/ai/actions/" + encodeURIComponent(action.proposal_id) + "/reject",
            {
                method: "POST",
                body: JSON.stringify({ reason }),
            },
        );
        const output = card.querySelector(".action-result");
        output.hidden = false;
        output.textContent = "rejected" + (reason ? " · " + reason : "");
        card.querySelectorAll(
            "[data-action-id], [data-modify-action], [data-reject-action]",
        ).forEach((control) => {
            control.disabled = true;
        });
        const apply = card.querySelector("[data-action-id]");
        if (apply) apply.textContent = "Rejected";
        return response;
    }

    $("context-panel").querySelectorAll("[data-reject-action]").forEach((button) => {
        button.onclick = async () => {
            const action = byId.get(button.dataset.rejectAction);
            if (!action) return;
            const card = button.closest("[data-action-card]");
            button.disabled = true;
            button.textContent = "Rejecting…";
            try {
                await rejectRenderedAction(
                    action,
                    card,
                    "Operator rejected this proposal.",
                );
            } catch (error) {
                const output = card.querySelector(".action-result");
                output.hidden = false;
                output.textContent = error.message;
                button.disabled = false;
                button.textContent = "Reject";
            }
        };
    });

    $("context-panel").querySelectorAll("[data-modify-action]").forEach((button) => {
        button.onclick = async () => {
            const action = byId.get(button.dataset.modifyAction);
            if (!action) return;
            const card = button.closest("[data-action-card]");
            button.disabled = true;
            button.textContent = "Preparing…";
            try {
                await rejectRenderedAction(
                    action,
                    card,
                    "Operator requested a modified proposal.",
                );
                $("prompt").value =
                    "Modify the rejected proposal \"" +
                    action.label +
                    "\" (" +
                    action.proposal_id +
                    "). Keep the same goal, but change it so that ";
                autoResize();
                $("prompt").focus();
                $("prompt").setSelectionRange(
                    $("prompt").value.length,
                    $("prompt").value.length,
                );
            } catch (error) {
                const output = card.querySelector(".action-result");
                output.hidden = false;
                output.textContent = error.message;
                button.disabled = false;
                button.textContent = "Modify";
            }
        };
    });

    $("context-panel").querySelectorAll("[data-action-id]").forEach((button) => {
        button.onclick = async () => {
            const action = byId.get(button.dataset.actionId);
            if (!action) return;
            if (!button.classList.contains("confirming")) {
                button.classList.add("confirming");
                button.textContent =
                    action.type === "editorial_storyboard_edit"
                        ? "Confirm: Apply edit"
                        : "Confirm: " + action.label;
                return;
            }
            button.disabled = true;
            button.textContent = "Starting…";
            const card = button.closest("[data-action-card]");
            const output = card.querySelector(".action-result");
            try {
                const execution = await api(
                    "/v1/ai/actions/" + encodeURIComponent(action.proposal_id) + "/execute",
                    {
                        method: "POST",
                        body: JSON.stringify({ confirmed: true }),
                    },
                );
                output.hidden = false;
                output.textContent =
                    execution.status.replaceAll("_", " ") +
                    " · " +
                    Object.entries(execution.result || {})
                    .slice(0, 2)
                    .map(([key, value]) => key.replaceAll("_", " ") + ": " + String(value))
                    .join(" · ");
                button.textContent =
                    execution.status === "executed" ? "✓ Executed" : "✓ " + execution.status;
                await refreshActionActivity(action.proposal_id, card, 0, true);
                appendKatcha({
                    answer:
                        action.type === "editorial_storyboard_edit"
                            ? "Storyboard edit applied as workspace version " +
                              String(execution.result?.workspace_version || "?") +
                              ". You can undo it from Editorial Studio."
                            : action.label +
                              " was accepted by the Katcha control plane. The evidence panel contains the returned workflow reference.",
                    intent: "action_execution",
                    evidence: [],
                });
            } catch (error) {
                output.hidden = false;
                output.textContent = error.message;
                button.disabled = false;
                button.classList.remove("confirming");
                button.textContent =
                    action.type === "editorial_storyboard_edit"
                        ? "Apply edit"
                        : "Review & confirm";
            }
        };
    });
}

function savedGoals() {
    try { return JSON.parse(sessionStorage.getItem("katcha.goalReceipts") || "[]"); }
    catch { return []; }
}
function saveGoal(receipt) {
    const rows = savedGoals().filter((row) => row.request.command_id !== receipt.request.command_id);
    sessionStorage.setItem("katcha.goalReceipts", JSON.stringify([...rows, receipt].slice(-20)));
}
function forgetGoal(commandId) {
    sessionStorage.setItem("katcha.goalReceipts", JSON.stringify(savedGoals().filter(
        (row) => row.request.command_id !== commandId)));
}
const finishedGoals = new Set(["completed", "blocked", "needs_input", "failed", "cancelled"]);
async function followGoal(receipt, { background = false } = {}) {
    let shownConfirmation = false;
    while (true) {
        const goal = await api("/v1/ai/goals/" + receipt.goalId);
        if (state.channelId !== goal.channel_profile_id ||
            (background && state.threadId && state.threadId !== goal.thread_id)) return null;
        state.threadId = goal.thread_id;
        let progress = $("goal-progress-" + goal.goal_id);
        if (!progress) {
            progress = document.createElement("article");
            progress.id = "goal-progress-" + goal.goal_id;
            progress.className = "message katcha-message goal-progress-message";
            progress.innerHTML =
                '<div class="message-avatar" aria-hidden="true">K</div>' +
                '<div class="message-body goal-progress-body">' +
                '<span class="message-author">Katcha AI · working</span>' +
                '<div class="goal-progress-line">' +
                '<span class="goal-progress-indicator" aria-hidden="true"></span>' +
                '<p role="status"></p>' +
                '</div>' +
                '<button class="goal-stop-button" type="button">Stop work</button>' +
                '</div>';
            $("thread").append(progress);
            progress.querySelector("button").onclick = async () => {
                try { await api("/v1/ai/goals/" + goal.goal_id + "/cancel", {method: "POST"}); }
                catch (error) { status(error.message, true); }
            };
        }
        progress.querySelector("p").textContent = goal.summary;
        progress.querySelector("button").disabled = !capability("ai_write") || finishedGoals.has(goal.status);
        if (finishedGoals.has(goal.status)) {
            forgetGoal(receipt.request.command_id);
            progress.querySelector("button").remove();
            const result = Object.keys(goal.result || {}).length ? goal.result : {
                answer: goal.summary, thread_id: goal.thread_id, evidence: [], actions: [],
            };
            if (background && state.threadId === goal.thread_id) {
                appendKatcha(result); renderContext(result);
                await loadThreads({openLatest: false});
            }
            return result;
        }
        if (goal.status === "waiting_confirmation" && !shownConfirmation) {
            shownConfirmation = true;
            if (!background) {
                setTimeout(() => followGoal(receipt, {background: true}).catch(
                    (error) => status("Saved work can be resumed after reconnecting. " + error.message, true)), 2000);
                return goal.result;
            }
            appendKatcha(goal.result); renderContext(goal.result);
        }
        if (goal.status === "queued") {
            await api("/v1/ai/goals", {method: "POST", body: JSON.stringify(receipt.request)});
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
    }
}
async function submitDurableGoal(request) {
    const actor = state.controlSession?.actor || state.controlSession?.principal_name || "current";
    const serialized = JSON.stringify({...request, thread_id: undefined});
    const prior = savedGoals().find((row) => row.actor === actor && row.serialized === serialized);
    const receipt = prior || {actor, serialized, request: {...request, command_id: crypto.randomUUID()}};
    saveGoal(receipt);
    const goal = await api("/v1/ai/goals", {method: "POST", body: JSON.stringify(receipt.request)});
    receipt.goalId = goal.goal_id;
    saveGoal(receipt);
    return followGoal(receipt);
}
async function resumeSavedGoals() {
    const actor = state.controlSession?.actor || state.controlSession?.principal_name || "current";
    try {
        const remote = await api("/v1/ai/goals?channel_profile_id=" + encodeURIComponent(state.channelId));
        for (const goal of remote.filter((row) => !finishedGoals.has(row.status))) {
            if (!savedGoals().some((row) => row.request.command_id === goal.command_id)) {
                saveGoal({actor, goalId: goal.goal_id,
                    request: {...goal.request, command_id: goal.command_id}});
            }
        }
        for (const receipt of savedGoals().filter((row) => row.actor === actor &&
            row.request.channel_profile_id === state.channelId)) {
            if (!receipt.goalId) {
                const goal = await api("/v1/ai/goals", {method: "POST", body: JSON.stringify(receipt.request)});
                receipt.goalId = goal.goal_id;
                saveGoal(receipt);
            }
            followGoal(receipt, {background: true}).catch((error) =>
                status("Saved work can be resumed after reconnecting. " + error.message, true));
        }
    } catch (error) {
        status("Saved work can be resumed after reconnecting. " + error.message, true);
    }
}

async function sendPrompt(text) {
    const prompt = text.trim();
    if (!prompt || !state.channelId || state.busy) return;
    if (!capability("ai_command")) {
        status(
            "This control principal does not have ai:command permission.",
            true,
        );
        return;
    }
    state.busy = true;
    $("send").disabled = true;
    appendUser(prompt);
    appendThinking();
    $("prompt").value = "";
    autoResize();
    try {
        const request = {
            channel_profile_id: state.channelId,
            thread_id: state.threadId || null,
            prompt,
            selected_clip_ids: [...state.selectedClipIds],
            selected_production_id: state.selectedProductionId,
            resource_refs: [...state.resourceRefs],
        };
        const result = state.durableGoals ? await submitDurableGoal(request) :
            await api("/v1/ai/command", {method: "POST", body: JSON.stringify(request)});
        if (!result) return;
        state.threadId = result.thread_id;
        const resolved = result.resolved_context || {};
        if (Array.isArray(resolved.resource_refs)) {
            state.resourceRefs = [...resolved.resource_refs];
        }
        if (
            resolved.inherited_from_thread &&
            Array.isArray(resolved.selected_clip_ids) &&
            resolved.selected_clip_ids.length
        ) {
            state.selectedClipIds = [...resolved.selected_clip_ids];
            state.selectedProductionId = null;
            renderSelection();
        }
        appendKatcha(result);
        renderContext(result);
        await loadThreads({ openLatest: false });
        $("thread-history").value = state.threadId;
        $("archive-thread").disabled = !capability("ai_write");
        await refreshObservability();
        status("");
    } catch (error) {
        appendError(error.message);
        if (!$('prompt').value.trim()) $('prompt').value = prompt;
        autoResize();
        status(error.message, true);
    } finally {
        state.busy = false;
        $("send").disabled = false;
        $("prompt").focus();
    }
}

async function connect(event) {
    event?.preventDefault();
    state.token = $("token").value.trim() || state.token;
    $("token").value = "";
    if (state.token) sessionStorage.setItem("katcha.controlToken", state.token);
    status("Connecting to Katcha control plane…");
    try {
        state.controlSession = await api("/v1/control/session");
        renderControlSession();
        if (!capability("channels_read")) {
            throw new Error(
                "Connected successfully, but this principal lacks channels:read required by the Command Center.",
            );
        }

        const allChannels = (await api("/v1/channels")).filter(
            (channel) => channel.status === "active",
        );
        const access = state.controlSession.channel_access || {};
        const allowedIds = new Set(access.channel_profile_ids || []);
        state.channels = access.all_channels
            ? allChannels
            : allChannels.filter((channel) => allowedIds.has(channel.id));
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
            status("No active channel is available yet. Set up a channel to chat with Katcha.", true);
            return;
        }
        const requestedChannel = launchParams.get("channel");
        state.channelId =
            requestedChannel &&
            state.channels.some((row) => row.id === requestedChannel)
                ? requestedChannel
                : state.channels[0].id;
        $("channel").value = state.channelId;
        $("command-center").hidden = false;
        $("connect-form").hidden = true;
        $("connection-state").textContent = "CONNECTED";
        $("connection-state").className = "simulation connected";
        try {
            const readiness = await api("/v1/ai/readiness");
            state.durableGoals = readiness.live && readiness.durable_goals === true;
            $("ai-readiness").textContent = readiness.message;
            $("ai-readiness").className = "ai-readiness" + (readiness.live ? " live" : " unavailable");
            $("ai-readiness").hidden = false;
        } catch {
            $("ai-readiness").textContent = "AI readiness could not be checked. You can retry your request or inspect the launch console.";
            $("ai-readiness").className = "ai-readiness unavailable";
            $("ai-readiness").hidden = false;
        }
        const hasTypedDeepLink = Boolean(
            launchParams.get("resource_kind") && launchParams.get("resource_id"),
        );
        await loadThreads({ openLatest: !hasTypedDeepLink });
        applyDeepLinkContext();
        renderControlSession();
        if (state.durableGoals) resumeSavedGoals();
        await refreshObservability();
        const active = state.channels.find((row) => row.id === state.channelId);
        const permissionLabel = capability("ai_command")
            ? "Command access is enabled."
            : "Read-only access: ai:command is not granted.";
        status(
            "Katcha AI is ready for " +
                channelName(active || state.channels[0]) +
                ". " +
                permissionLabel,
        );
        if (new URLSearchParams(location.search).get("focus") === "chat") $("prompt").focus();
    } catch (error) {
        $("connect-form").hidden = false;
        $("connection-state").textContent = "CONNECTION FAILED";
        status(error.message, true);
    }
}

function autoResize() {
    const input = $("prompt");
    input.style.height = "auto";
    input.style.height = Math.min(130, input.scrollHeight) + "px";
}

function setContextPanel(open) {
    document.body.classList.toggle("ai-context-open", open);
    const toggle = $("context-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
}
$("context-toggle")?.addEventListener("click", () => {
    setContextPanel(!document.body.classList.contains("ai-context-open"));
});
window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("ai-context-open")) {
        setContextPanel(false);
        $("context-toggle")?.focus();
    }
});

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
$("channel").onchange = async () => {
    state.channelId = $("channel").value;
    state.threadId = "";
    state.selectedClipIds = [];
    state.selectedProductionId = null;
    state.resourceRefs = [];
    renderSelection();
    await loadThreads({ openLatest: true });
    status("Channel context changed. Conversation history was reloaded for this channel.");
};
$("thread-history").onchange = async () => {
    try {
        await openThread($("thread-history").value);
        status(
            state.threadId
                ? "Reopened durable Katcha AI conversation."
                : "New conversation ready.",
        );
    } catch (error) {
        status(error.message, true);
    }
};
$("clear-thread").onclick = () => {
    newConversation();
    status("New conversation ready. Previous conversations remain in history.");
};
$("archive-thread").onclick = async () => {
    if (!state.threadId) return;
    if (!capability("ai_write")) {
        status(
            "This control principal does not have ai:write permission.",
            true,
        );
        return;
    }
    const threadId = state.threadId;
    $("archive-thread").disabled = true;
    try {
        await api(
            "/v1/ai/threads/" + encodeURIComponent(threadId) + "/archive",
            { method: "POST", body: "{}" },
        );
        state.threadId = "";
        await loadThreads({ openLatest: false });
        newConversation("Conversation archived. Start a new conversation or reopen another saved thread.");
        status("Conversation archived.");
    } catch (error) {
        $("archive-thread").disabled = false;
        status(error.message, true);
    }
};
document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.onclick = () => sendPrompt(button.dataset.prompt);
});
if (location.port !== "8765") connect();
