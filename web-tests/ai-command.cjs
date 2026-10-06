const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const server = spawn(
    "python3",
    [
        "-m",
        "http.server",
        "8770",
        "--bind",
        "127.0.0.1",
        "--directory",
        path.resolve(__dirname, "../src/katcha/web"),
    ],
    { stdio: "ignore" },
);

const requests = [];
const threadId = "77777777-7777-4777-8777-777777777777";
const channelId = "11111111-1111-4111-8111-111111111111";
const proposalId = "66666666-6666-4666-8666-666666666666";
const storedTurns = [];
let threadExists = false;
let commandCount = 0;
let restrictedSession = false;
let durableMode = false;
let failGoalOnce = true;
let goalPhase = "completed";
const goalBodies = [];
let goalId = "88888888-8888-4888-8888-888888888888";
let browser;

(async () => {
    for (let i = 0; i < 50; i++) {
        try {
            await fetch("http://127.0.0.1:8770");
            break;
        } catch {
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
    }

    browser = await chromium.launch({
        headless: true,
        executablePath: process.env.CHROMIUM_PATH || undefined,
        args: process.env.CHROMIUM_PATH ? ["--no-sandbox"] : [],
    });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));

    await page.route("**/v1/**", async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const body = req.postData() ? req.postDataJSON() : null;
        requests.push({
            path: url.pathname,
            method: req.method(),
            body,
            auth: req.headers().authorization,
        });

        if (
            ["/v1/control/session", "/v1/channels"].includes(url.pathname) &&
            req.headers().authorization !== "Bearer fixture-token"
        ) {
            await route.fulfill({
                status: 401,
                json: { detail: "Workspace token required" },
            });
            return;
        }
        if (url.pathname === "/v1/ai/command" && body?.prompt === "Please do something ambiguous") {
            await route.fulfill({status: 503, json: {detail: "Katcha AI could not interpret this request right now."}});
            return;
        }

        let data;
        if (url.pathname === "/v1/control/session") {
            data = restrictedSession
                ? {
                      control_contract_version: "1",
                      katcha_version: "0.0.0-fixture",
                      actor: "control-principal:auditor",
                      principal_name: "auditor",
                      authentication_mode: "named_principal",
                      scopes: ["channels:read", "ai:read"],
                      channel_access: {
                          all_channels: false,
                          channel_profile_ids: [channelId],
                      },
                      capabilities: {
                          ai_read: true,
                          ai_command: false,
                          ai_write: false,
                          channels_read: true,
                          channels_write: false,
                          intelligence_write: false,
                          production_create: false,
                          render_recover: false,
                          discovery_write: false,
                          events_read: false,
                          events_ack: false,
                          trends_read: false,
                          trends_write: false,
                      },
                      action_permissions: {
                          refresh_channel_intelligence: {
                              required_scope: "intelligence:write",
                              allowed: false,
                          },
                          create_short_production: {
                              required_scope: "production:create",
                              allowed: false,
                          },
                          create_ranked_short_episode: {
                              required_scope: "production:create",
                              allowed: false,
                          },
                          recover_production_render: {
                              required_scope: "render:recover",
                              allowed: false,
                          },
                          start_source_scout: {
                              required_scope: "discovery:write",
                              allowed: false,
                          },
                      },
                      event_stream: {
                          read_endpoint: "/v1/control/events",
                          acknowledge_endpoint_template:
                              "/v1/control/events/{event_id}/ack",
                          channel_scope_required: true,
                          consumer_cursor_is_principal_bound: true,
                      },
                  }
                : {
                      control_contract_version: "1",
                      katcha_version: "0.0.0-fixture",
                      actor: "control-principal:operator-ui",
                      principal_name: "operator-ui",
                      authentication_mode: "named_principal",
                      scopes: [
                          "ai:read",
                          "ai:command",
                          "ai:write",
                          "channels:read",
                          "channels:write",
                          "intelligence:write",
                          "production:create",
                          "render:recover",
                          "discovery:write",
                          "events:read",
                          "events:ack",
                          "trends:read",
                          "trends:write",
                      ],
                      channel_access: {
                          all_channels: true,
                          channel_profile_ids: [],
                      },
                      capabilities: {
                          ai_read: true,
                          ai_command: true,
                          ai_write: true,
                          channels_read: true,
                          channels_write: true,
                          intelligence_write: true,
                          production_create: true,
                          render_recover: true,
                          discovery_write: true,
                          events_read: true,
                          events_ack: true,
                          trends_read: true,
                          trends_write: true,
                      },
                      action_permissions: {
                          refresh_channel_intelligence: {
                              required_scope: "intelligence:write",
                              allowed: true,
                          },
                          create_short_production: {
                              required_scope: "production:create",
                              allowed: true,
                          },
                          create_ranked_short_episode: {
                              required_scope: "production:create",
                              allowed: true,
                          },
                          recover_production_render: {
                              required_scope: "render:recover",
                              allowed: true,
                          },
                          start_source_scout: {
                              required_scope: "discovery:write",
                              allowed: true,
                          },
                      },
                      event_stream: {
                          read_endpoint: "/v1/control/events",
                          acknowledge_endpoint_template:
                              "/v1/control/events/{event_id}/ack",
                          channel_scope_required: false,
                          consumer_cursor_is_principal_bound: true,
                      },
                  };
        } else if (url.pathname === "/v1/ai/goals" && req.method() === "GET") {
            data = [];
        } else if (url.pathname === "/v1/ai/goals" && req.method() === "POST") {
            goalBodies.push(body);
            if (failGoalOnce) {
                failGoalOnce = false;
                await route.fulfill({status: 503, contentType: "application/json",
                    body: JSON.stringify({detail: "Saved request response was interrupted"})});
                return;
            }
            data = {goal_id: goalId, thread_id: threadId, channel_profile_id: channelId,
                status: goalPhase, summary: "Saved goal status", result: {}};
        } else if (url.pathname === "/v1/ai/goals/" + goalId + "/cancel") {
            goalPhase = "cancelled";
            data = {status: goalPhase};
        } else if (url.pathname === "/v1/ai/goals/" + goalId) {
            data = {goal_id: goalId, thread_id: threadId, channel_profile_id: channelId,
                status: goalPhase, summary: goalPhase === "cancelled" ? "Planning stopped" :
                    "Saved workflow result received", result: goalPhase === "completed" ? {
                    thread_id: threadId, answer: "Durable goal completed from its saved result",
                    intent: "goal_completed", evidence: [], actions: [],
                } : {}};
        } else if (url.pathname === "/v1/ai/readiness") {
            data = durableMode ? {live: true, durable_goals: true, message: "Live goals ready"} :
                {live: false, message: "Katcha is in fixture mode. Select Live AI in the launch console."};
        } else if (url.pathname === "/v1/ai/observability") {
            data = {
                channel_profile_id: channelId,
                window_hours: 24,
                request_count: commandCount,
                average_latency_ms: commandCount ? 120 : 0,
                p95_latency_ms: commandCount ? 180 : 0,
                degraded_answer_count: 0,
                ai_planned_count: 0,
                typed_context_request_count: commandCount ? 1 : 0,
                input_units: 0,
                output_units: 0,
                estimated_cost_usd: 0,
                actions: {proposed: commandCount ? 1 : 0, executed: 0, failed: 0},
                recent: [],
            };
        } else if (url.pathname === "/v1/channels") {
            data = [
                {
                    id: channelId,
                    youtube_connection_id: "22222222-2222-4222-8222-222222222222",
                    status: "active",
                    timezone: "America/Chicago",
                    active_strategy_version: 1,
                    active_automation_version: 1,
                    profile_metadata: { channel_title: "RankSnaxx" },
                    created_at: "2026-09-28T12:00:00Z",
                    updated_at: "2026-09-28T12:00:00Z",
                },
            ];
        } else if (url.pathname === "/v1/ai/threads" && req.method() === "GET") {
            data = threadExists
                ? [
                      {
                          thread_id: threadId,
                          channel_profile_id: channelId,
                          title: "Show me the best Xbox clips found today",
                          status: "active",
                          created_by: "control-token:fixture",
                          last_activity_at: "2026-09-28T12:05:00Z",
                          archived_at: null,
                          created_at: "2026-09-28T12:00:00Z",
                      },
                  ]
                : [];
        } else if (
            url.pathname === "/v1/ai/threads/" + threadId &&
            req.method() === "GET"
        ) {
            data = {
                thread: {
                    thread_id: threadId,
                    channel_profile_id: channelId,
                    title: "Show me the best Xbox clips found today",
                    status: "active",
                    created_by: "control-token:fixture",
                    last_activity_at: "2026-09-28T12:05:00Z",
                    archived_at: null,
                    created_at: "2026-09-28T12:00:00Z",
                },
                turns: storedTurns,
                actions: [
                    {
                        proposal_id: proposalId,
                        request_id: "33333333-3333-4333-8333-333333333333",
                        thread_id: threadId,
                        source_turn_id:
                            storedTurns.filter((turn) => turn.role === "assistant").at(-1)?.turn_id ||
                            "99999999-9999-4999-8999-999999999999",
                        channel_profile_id: channelId,
                        action_type: "create_short_production",
                        label: "Make a short from top clip",
                        description: "Start a channel-scoped production using the current channel defaults.",
                        status: "proposed",
                        execution_attempts: 0,
                        expires_at: "2026-09-28T13:30:00Z",
                        confirmed_by: null,
                        confirmed_at: null,
                        execution_started_at: null,
                        executed_at: null,
                        result: {},
                        error: null,
                        payload: {
                            clip_id: "44444444-4444-4444-8444-444444444444",
                            edit_blueprint_key: "persona_commentary",
                        },
                    },
                ],
            };
        } else if (
            url.pathname === "/v1/ai/threads/" + threadId + "/archive" &&
            req.method() === "POST"
        ) {
            threadExists = false;
            data = {
                thread_id: threadId,
                channel_profile_id: channelId,
                title: "Show me the best Xbox clips found today",
                status: "archived",
                created_by: "control-token:fixture",
                last_activity_at: "2026-09-28T12:10:00Z",
                archived_at: "2026-09-28T12:10:00Z",
                created_at: "2026-09-28T12:00:00Z",
            };
        } else if (url.pathname === "/v1/ai/command") {
            commandCount += 1;
            const contextSourceTurnId =
                storedTurns.filter((turn) => turn.role === "assistant").at(-1)?.turn_id || null;
            const turnSuffix = String(commandCount).padStart(2, "0");
            const userTurnId =
                "88888888-8888-4888-8888-8888888888" + turnSuffix + "1";
            const assistantTurnId =
                "88888888-8888-4888-8888-8888888888" + turnSuffix + "2";
            const inheritedFollowUp = commandCount === 3;
            const answer = inheritedFollowUp
                ? "I prepared a production proposal for the clip resolved from the prior grounded answer."
                : body.selected_clip_ids.length
                  ? "This clip scored highly because its stored hook and rewatch signals were strong."
                  : "I found one strong Xbox clip from today. Its channel score is 91/100.";
            const intent = inheritedFollowUp
                ? "create_content"
                : body.selected_clip_ids.length
                  ? "clip_explanation"
                  : "best_clips";
            const evidence = inheritedFollowUp
                ? [
                      {
                          kind: "selection",
                          id: "current",
                          selected_clip_ids: [
                              "44444444-4444-4444-8444-444444444444",
                          ],
                          conversation_source_turn_id: contextSourceTurnId,
                      },
                  ]
                : [
                      {
                          kind: "clip",
                          id: "44444444-4444-4444-8444-444444444444",
                          title: "Xbox fixture clip",
                          candidate_score: 88.0,
                          channel_score: 0.91,
                          why: ["hook score", "rewatch potential"],
                      },
                  ];
            threadExists = true;
            storedTurns.push(
                {
                    turn_id: userTurnId,
                    thread_id: threadId,
                    channel_profile_id: channelId,
                    sequence_number: storedTurns.length + 1,
                    role: "user",
                    request_id: "33333333-3333-4333-8333-333333333333",
                    intent: null,
                    narrator: null,
                    content: body.prompt,
                    evidence: [],
                    context: {
                        selected_clip_ids: body.selected_clip_ids,
                        resource_refs: body.resource_refs || [],
                    },
                    created_at: "2026-09-28T12:00:00Z",
                },
                {
                    turn_id: assistantTurnId,
                    thread_id: threadId,
                    channel_profile_id: channelId,
                    sequence_number: storedTurns.length + 2,
                    role: "assistant",
                    request_id: "33333333-3333-4333-8333-333333333333",
                    intent,
                    narrator: "fixture/grounded-command-v1",
                    content: answer,
                    evidence,
                    context: {
                        key_points: ["The answer uses stored clip evidence."],
                        caveats: [],
                        planning: {
                            intent,
                            source: "deterministic",
                            provider: "katcha",
                            model: "deterministic-command-router-v1",
                            confidence: 1,
                            reason: "A registered deterministic intent matched the request.",
                        },
                    },
                    created_at: "2026-09-28T12:00:01Z",
                },
            );
            data = {
                request_id: "33333333-3333-4333-8333-333333333333",
                thread_id: threadId,
                user_turn_id: userTurnId,
                assistant_turn_id: assistantTurnId,
                channel_profile_id: body.channel_profile_id,
                intent,
                answer,
                key_points: ["The answer uses stored clip evidence."],
                caveats: [],
                evidence,
                actions: [
                    {
                        proposal_id: proposalId,
                        thread_id: threadId,
                        source_turn_id: assistantTurnId,
                        type: "create_short_production",
                        label: "Make a short from top clip",
                        description: "Start a channel-scoped production using the current channel defaults.",
                        status: "proposed",
                        expires_at: "2026-09-28T13:30:00Z",
                        payload: {
                            clip_id: "44444444-4444-4444-8444-444444444444",
                            edit_blueprint_key: "persona_commentary",
                        },
                        requires_confirmation: true,
                    },
                ],
                planning: {
                    intent,
                    source: "deterministic",
                    provider: "katcha",
                    model: "deterministic-command-router-v1",
                    confidence: 1,
                    reason: "A registered deterministic intent matched the request.",
                },
                resolved_context: {
                    selected_clip_ids: inheritedFollowUp
                        ? ["44444444-4444-4444-8444-444444444444"]
                        : body.selected_clip_ids,
                    resource_refs: body.resource_refs || [],
                    inherited_from_thread: inheritedFollowUp,
                    source_turn_id: inheritedFollowUp ? contextSourceTurnId : null,
                    resolution: inheritedFollowUp
                        ? "Resolved the singular reference to the first applicable clip from the previous grounded answer."
                        : null,
                    action_source_turn_id: null,
                },
                grounded: true,
                narrator: "fixture/grounded-command-v1",
            };
        } else if (
            url.pathname ===
            "/v1/ai/actions/66666666-6666-4666-8666-666666666666/execute"
        ) {
            assert.deepEqual(body, { confirmed: true });
            data = {
                proposal_id: "66666666-6666-4666-8666-666666666666",
                action_type: "create_short_production",
                status: "executed",
                execution_attempts: 1,
                result: {
                    production_id: "55555555-5555-4555-8555-555555555555",
                    workflow_id: "fixture-production-workflow",
                },
            };
        } else if (
            url.pathname ===
            "/v1/ai/actions/66666666-6666-4666-8666-666666666666/activity"
        ) {
            data = {
                proposal_id: "66666666-6666-4666-8666-666666666666",
                action_type: "create_short_production",
                proposal_status: "executed",
                workflow_id: "fixture-production-workflow",
                state: "awaiting_review",
                settled: true,
                resource: {
                    kind: "production",
                    id: "55555555-5555-4555-8555-555555555555",
                    workflow_id: "fixture-production-workflow",
                    status: "review",
                    stage: "review",
                    generation: 1,
                    error: null,
                    updated_at: "2026-09-28T12:10:00Z",
                },
                events: [],
            };
        } else {
            throw new Error("Unexpected API request " + req.method() + " " + url.pathname);
        }

        await route.fulfill({ json: data });
    });

    await page.goto(
        "http://127.0.0.1:8770/ai.html?channel=" +
            channelId +
            "&resource_kind=clip&resource_id=44444444-4444-4444-8444-444444444444" +
            "&prompt=" +
            encodeURIComponent("Explain why this clip is strong.") +
            "&focus=chat",
    );
    await page.waitForFunction(() => document.querySelector("#status").textContent.includes("Workspace token required"));
    assert.equal(await page.locator("#katcha-chat-shortcut").getAttribute("href"), "#prompt");
    await page.locator(".workspace-tree").waitFor();
    assert.equal(await page.locator(".workspace-menu").isVisible(), false);
    assert.equal(
        (await page.locator(".workspace-tree-link.is-current b").textContent()).trim(),
        "Katcha AI",
    );

    await page.locator("#token").fill("fixture-token");
    await page.locator("#connect-form button").click();
    try {
        await page.locator("#command-center:not([hidden])").waitFor({ timeout: 5000 });
    } catch (error) {
        throw new Error(
            "Katcha AI did not connect. status=" +
                (await page.locator("#status").innerText()) +
                " pageErrors=" +
                JSON.stringify(errors) +
                " requests=" +
                JSON.stringify(requests),
            { cause: error },
        );
    }
    assert.match(await page.locator(".ai-workspace-head").innerText(), /Ask\. Inspect\. Act\./i);
    assert.notEqual(
        await page.evaluate(() => getComputedStyle(document.body).overflowY),
        "hidden",
    );
    await page.locator("#command-form").scrollIntoViewIfNeeded();
    assert.equal(
        await page.locator("#command-form").evaluate(
            (form) => form.getBoundingClientRect().bottom <= innerHeight + 2,
        ),
        true,
    );
    assert.equal(await page.locator("#channel").inputValue(), channelId);
    assert.match(await page.locator("#principal-state").innerText(), /operator-ui/i);
    assert.match(await page.locator("#principal-state").innerText(), /all channels/i);
    assert.match(await page.locator("#ai-readiness").innerText(), /fixture mode/);
    assert.equal(await page.locator("#thread-history").inputValue(), "");
    assert.equal(await page.locator("#token").inputValue(), "");
    assert.match(await page.locator("#selection-bar").innerText(), /clip 44444444/i);
    assert.equal(await page.locator("#prompt").inputValue(), "Explain why this clip is strong.");
    assert.equal(await page.locator("#obs-requests").innerText(), "0");
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    await page.locator("#katcha-chat-shortcut").click();
    assert.equal(await page.evaluate(() => document.activeElement.id), "prompt");

    await page.locator("#prompt").fill("Show me the best Xbox clips found today and explain why they scored highly.");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByText(/channel score is 91\/100/i).waitFor();

    assert.match(await page.locator("#context-panel").innerText(), /Xbox fixture clip/);
    assert.match(await page.locator("#context-panel").innerText(), /hook score/);
    assert.match(await page.locator("#context-panel").innerText(), /COMMAND ROUTING/);
    assert.match(await page.locator("#context-panel").innerText(), /best clips/i);
    assert.match(
        await page.locator("#context-panel").innerText(),
        /deterministic-command-router-v1/i,
    );
    assert.match(await page.locator("#narrator").innerText(), /fixture\/grounded-command-v1/);

    await page.locator("[data-select-clip]").click();
    assert.match(await page.locator("#selection-bar").innerText(), /1 selected clip/);

    await page.locator("#prompt").fill("Why did Katcha score this clip highly?");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByText(/stored hook and rewatch signals/i).waitFor();

    const commandRequests = requests.filter((request) => request.path === "/v1/ai/command");
    assert.equal(commandRequests.length, 2);
    assert.equal(commandRequests[0].body.thread_id, null);
    assert.deepEqual(commandRequests[0].body.resource_refs, [
        {
            kind: "clip",
            id: "44444444-4444-4444-8444-444444444444",
        },
    ]);
    assert.equal(commandRequests[1].body.thread_id, threadId);
    assert.deepEqual(commandRequests[1].body.selected_clip_ids, [
        "44444444-4444-4444-8444-444444444444",
    ]);

    assert.match(await page.locator(".action-payload").first().innerText(), /clip 44444444/i);
    assert.match(await page.locator(".action-payload").first().innerText(), /persona_commentary/i);

    const actionButton = page.locator("[data-action-id]").first();
    await actionButton.click();
    assert.match(await actionButton.innerText(), /Confirm:/);
    assert.equal(
        requests.filter((request) => request.path.endsWith("/execute")).length,
        0,
    );
    await actionButton.click();
    await page.getByText(/production · review/i).waitFor();
    assert.equal(
        requests.filter((request) => request.path.endsWith("/execute")).length,
        1,
    );
    assert.equal(
        requests.filter((request) => request.path.endsWith("/activity")).length,
        1,
    );

    await page.locator("#clear-thread").click();
    assert.equal(await page.locator("#thread-history").inputValue(), "");
    await page.locator("#thread-history").selectOption(threadId);
    await page.getByText(/stored hook and rewatch signals/i).waitFor();
    assert.equal(await page.locator("#thread-history").inputValue(), threadId);
    assert.match(await page.locator("#context-panel").innerText(), /Xbox fixture clip/);
    assert(
        requests.some(
            (request) =>
                request.path === "/v1/ai/threads/" + threadId &&
                request.method === "GET",
        ),
    );

    await page.locator("#prompt").fill("Turn that into a short.");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByText(/resolved from the prior grounded answer/i).waitFor();
    const followUpRequests = requests.filter(
        (request) => request.path === "/v1/ai/command",
    );
    assert.equal(followUpRequests.length, 3);
    assert.deepEqual(followUpRequests[2].body.selected_clip_ids, []);
    assert.equal(followUpRequests[2].body.thread_id, threadId);
    assert.match(await page.locator("#selection-bar").innerText(), /1 selected clip/);

    await page.locator("#prompt").fill("Please do something ambiguous");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.waitForFunction(() => document.querySelector("#status").textContent.includes("could not interpret"));
    assert.equal(await page.locator("#prompt").inputValue(), "Please do something ambiguous");
    const unauthenticatedRequests = requests.filter((request) => !request.auth);
    assert.deepEqual(
        unauthenticatedRequests.map((request) => request.path),
        ["/v1/control/session"],
    );
    assert(
        requests
            .filter((request) => request.auth)
            .every((request) => request.auth === "Bearer fixture-token"),
    );

    durableMode = true;
    await page.reload();
    await page.getByText("Live goals ready").waitFor();
    await page.locator("#prompt").fill("Please keep looking until the saved result arrives");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByText("Saved request response was interrupted").first().waitFor();
    assert.equal(await page.locator("#prompt").inputValue(),
        "Please keep looking until the saved result arrives");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByText("Durable goal completed from its saved result").waitFor();
    assert.equal(goalBodies[0].command_id, goalBodies[1].command_id);
    assert.deepEqual(goalBodies[0], goalBodies[1]);
    await page.waitForFunction(() => !document.getElementById("send").disabled);
    goalId = "99999999-9999-4999-8999-999999999999";
    goalPhase = "waiting_workflow";
    await page.locator("#prompt").fill("Keep checking this pending work");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByRole("button", {name: "Stop work"}).last().waitFor();
    const goalProgress = page.locator(".goal-progress-message").last();
    assert.equal(await goalProgress.locator(".message-avatar").innerText(), "K");
    assert.equal(
        await goalProgress.locator(".goal-progress-body").evaluate(
            (element) => element.getBoundingClientRect().width > 240,
        ),
        true,
    );
    await page.reload();
    await page.getByRole("button", {name: "Stop work"}).last().waitFor();
    await page.getByRole("button", {name: "Stop work"}).last().click();
    await page.getByText("Planning stopped").first().waitFor();
    assert.equal(await page.evaluate(() =>
        JSON.parse(sessionStorage.getItem("katcha.goalReceipts")).length), 0);
    durableMode = false;

    fs.mkdirSync(path.join(__dirname, "test-results"), { recursive: true });
    await page.screenshot({
        path: path.join(__dirname, "test-results/ai-command-desktop.png"),
        fullPage: true,
    });

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
    );
    assert.equal(await page.locator("#ai-context").isHidden(), true);
    await page.locator("#context-toggle").click();
    await page.locator("#ai-context").waitFor({ state: "visible" });
    assert.equal(await page.locator("#context-toggle").getAttribute("aria-expanded"), "true");
    await page.keyboard.press("Escape");
    await page.locator("#ai-context").waitFor({ state: "hidden" });
    await page.screenshot({
        path: path.join(__dirname, "test-results/ai-command-mobile.png"),
        fullPage: true,
    });

    const editorialProjectId = "77777777-7777-4777-8777-777777777777";
    await page.goto(
        "http://127.0.0.1:8770/ai.html?channel=" +
            channelId +
            "&resource_kind=editorial_project&resource_id=" +
            editorialProjectId +
            "&prompt=" +
            encodeURIComponent("Review the current script stage.") +
            "&focus=chat",
    );
    await page.locator("#command-center:not([hidden])").waitFor();
    await page.waitForFunction(() =>
        /editorial project 77777777/i.test(
            document.querySelector("#selection-bar")?.textContent || "",
        ),
    );
    assert.match(
        await page.locator("#selection-bar").innerText(),
        /editorial project 77777777/i,
    );
    assert.equal(
        await page.locator("#prompt").inputValue(),
        "Review the current script stage.",
    );

    await page.goto("http://127.0.0.1:8770/index.html");
    assert.equal(
        await page.locator("#katcha-chat-shortcut").getAttribute("href"),
        "/ai?focus=chat",
    );

    restrictedSession = true;
    await page.goto("http://127.0.0.1:8770/ai.html");
    await page.locator("#command-center:not([hidden])").waitFor();
    await page.getByText(/stored hook and rewatch signals/i).waitFor();
    assert.match(await page.locator("#principal-state").innerText(), /auditor/i);
    assert.match(await page.locator("#principal-state").innerText(), /1 channel/i);
    assert.equal(await page.locator("#prompt").isDisabled(), true);
    assert.equal(await page.locator("#send").isDisabled(), true);
    assert.equal(await page.locator(".starter").first().isDisabled(), true);
    assert.equal(await page.locator("#archive-thread").isDisabled(), true);
    assert.equal(
        await page.locator("[data-action-id]").first().isDisabled(),
        true,
    );
    assert.match(
        await page.locator("[data-action-id]").first().innerText(),
        /Not permitted/i,
    );
    assert.match(
        await page.locator("[data-action-card]").first().getAttribute("title"),
        /production:create/i,
    );
    assert.match(await page.locator("#status").innerText(), /Read-only access/i);

    assert.deepEqual(errors, []);
    console.log(
        "PASS: Aerith Katcha AI bounded workspace, mobile Evidence panel, control-session identity, capability-aware read-only mode, typed context, observability, grounded conversation, durable history, live action status, confirmation safety, auth, and mobile width",
    );
})()
    .catch((error) => {
        console.error(error);
        process.exitCode = 1;
    })
    .finally(async () => {
        if (browser) await browser.close();
        server.kill();
    });
