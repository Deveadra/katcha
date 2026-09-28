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
                    id: "11111111-1111-4111-8111-111111111111",
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
        } else if (url.pathname === "/v1/ai/command") {
            data = {
                request_id: "33333333-3333-4333-8333-333333333333",
                channel_profile_id: body.channel_profile_id,
                intent: body.selected_clip_ids.length ? "clip_explanation" : "best_clips",
                answer: body.selected_clip_ids.length
                    ? "This clip scored highly because its stored hook and rewatch signals were strong."
                    : "I found one strong Xbox clip from today. Its channel score is 91/100.",
                key_points: ["The answer uses stored clip evidence."],
                caveats: [],
                evidence: [
                    {
                        kind: "clip",
                        id: "44444444-4444-4444-8444-444444444444",
                        title: "Xbox fixture clip",
                        candidate_score: 88.0,
                        channel_score: 0.91,
                        why: ["hook score", "rewatch potential"],
                    },
                ],
                actions: [
                    {
                        proposal_id: "66666666-6666-4666-8666-666666666666",
                        type: "create_short_production",
                        label: "Make a short from top clip",
                        description: "Start a channel-scoped production using the current channel defaults.",
                        status: "proposed",
                        expires_at: "2026-09-28T13:30:00Z",
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
    assert.equal(await page.locator("#channel").inputValue(), "11111111-1111-4111-8111-111111111111");
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
    assert.deepEqual(commandRequests[1].body.selected_clip_ids, [
        "44444444-4444-4444-8444-444444444444",
    ]);

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
        "PASS: Katcha AI grounded conversation, evidence, context selection, two-step confirmed action, auth, and mobile width",
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
