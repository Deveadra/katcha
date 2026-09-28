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

        let data;
        if (url.pathname === "/v1/channels") {
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
            const userTurnId =
                commandCount === 1
                    ? "88888888-8888-4888-8888-888888888881"
                    : "88888888-8888-4888-8888-888888888883";
            const assistantTurnId =
                commandCount === 1
                    ? "88888888-8888-4888-8888-888888888882"
                    : "88888888-8888-4888-8888-888888888884";
            const answer = body.selected_clip_ids.length
                ? "This clip scored highly because its stored hook and rewatch signals were strong."
                : "I found one strong Xbox clip from today. Its channel score is 91/100.";
            const intent = body.selected_clip_ids.length
                ? "clip_explanation"
                : "best_clips";
            const evidence = [
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
        } else {
            throw new Error("Unexpected API request " + req.method() + " " + url.pathname);
        }

        await route.fulfill({ json: data });
    });

    await page.goto("http://127.0.0.1:8770/ai.html");
    await page.locator(".workspace-menu").waitFor();
    await page.locator(".workspace-menu > summary").click();
    assert.match(await page.locator(".workspace-menu-popover").innerText(), /Katcha AI/);
    await page.locator(".workspace-menu > summary").click();

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
    assert.equal(await page.locator("#channel").inputValue(), channelId);
    assert.equal(await page.locator("#thread-history").inputValue(), "");
    assert.equal(await page.locator("#token").inputValue(), "");
    assert.equal(await page.evaluate(() => localStorage.length), 0);

    await page.locator("#prompt").fill("Show me the best Xbox clips found today and explain why they scored highly.");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByText(/channel score is 91\/100/i).waitFor();

    assert.match(await page.locator("#context-panel").innerText(), /Xbox fixture clip/);
    assert.match(await page.locator("#context-panel").innerText(), /hook score/);
    assert.match(await page.locator("#narrator").innerText(), /fixture\/grounded-command-v1/);

    await page.locator("[data-select-clip]").click();
    assert.match(await page.locator("#selection-bar").innerText(), /1 selected clip/);

    await page.locator("#prompt").fill("Why did Katcha score this clip highly?");
    await page.locator("#command-form").evaluate((form) => form.requestSubmit());
    await page.getByText(/stored hook and rewatch signals/i).waitFor();

    const commandRequests = requests.filter((request) => request.path === "/v1/ai/command");
    assert.equal(commandRequests.length, 2);
    assert.equal(commandRequests[0].body.thread_id, null);
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
    await page.getByText(/executed · production id/i).waitFor();
    assert.equal(
        requests.filter((request) => request.path.endsWith("/execute")).length,
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

    assert(requests.every((request) => request.auth === "Bearer fixture-token"));

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
    await page.screenshot({
        path: path.join(__dirname, "test-results/ai-command-mobile.png"),
        fullPage: true,
    });

    assert.deepEqual(errors, []);
    console.log(
        "PASS: Katcha AI grounded conversation, durable history reopen, evidence, context selection, two-step confirmed action, auth, and mobile width",
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
