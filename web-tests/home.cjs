const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const port = 8778;
const server = spawn(
    "python3",
    ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")],
    { stdio: "ignore" },
);

const channel = {
    id: "channel-two",
    status: "active",
    youtube_connection_id: "youtube-two",
    profile_metadata: { channel_title: "RankSnaxx" },
};
const summary = {
    economics: {
        revenue_usd: 220.4,
        contribution_margin_usd: 143.2,
        effective_budget_usd: 100,
        budget_headroom_usd: 10,
    },
    growth: {
        ai: {
            stage: "building_watch_hours",
            priority_metrics: ["qualified_watch_hours_365d"],
            selected_path: "fastest",
            pace: "aggressive",
        },
    },
    automation: { level: "review_first", eligible_for_next_level: false },
};
const productions = [
    { id: "prod-active", status: "rendering", updated_at: "2026-09-29T18:00:00Z", render_manifest: { title_angle: "Xbox handheld launch" } },
    { id: "prod-failed", status: "failed", updated_at: "2026-09-29T19:00:00Z", title: "Broken render" },
];
const episodes = [
    { id: "episode-active", premise: "Top five Xbox stories", stage: "scripted", status: "voiced", updated_at: "2026-09-29T19:30:00Z" },
];
const publications = [
    { id: "pub-failed", title: "Failed upload", status: "failed" },
];
const clipSummary = { total: 50, hot: 42, archived: 6, failed: 2, purged: 0 };
const trends = [
    {
        topic: "Xbox handheld",
        opportunity: {
            calibrated_score: 0.91,
            confidence: 0.82,
            lifecycle: "accelerating",
            reasons: ["Fast acceleration with strong supporting coverage"],
        },
    },
];
const observability = { request_count: 16, p95_latency_ms: 720, estimated_cost_usd: 1.24, typed_context_request_count: 6 };

(async () => {
    for (let i = 0; i < 50; i++) {
        try {
            await fetch("http://127.0.0.1:" + port + "/home.html");
            break;
        } catch {
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
    }

    const browser = await chromium.launch({
        headless: true,
        executablePath: process.env.CHROMIUM_PATH || undefined,
        args: process.env.CHROMIUM_PATH ? ["--no-sandbox"] : [],
    });

    try {
        const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
        const requests = [];
        await page.route("**/v1/**", async (route) => {
            const req = route.request();
            const url = new URL(req.url());
            requests.push(url.pathname + url.search);
            let body;
            if (url.pathname === "/v1/channels") body = [{ id: "channel-one", status: "active", profile_metadata: { channel_title: "Other" } }, channel];
            else if (url.pathname === "/v1/channels/channel-two") body = summary;
            else if (url.pathname === "/v1/productions") body = productions;
            else if (url.pathname === "/v1/short-episodes") body = episodes;
            else if (url.pathname === "/v1/clips/library/summary") body = clipSummary;
            else if (url.pathname === "/v1/channels/channel-two/trends/explorer") body = trends;
            else if (url.pathname === "/v1/ai/observability") body = observability;
            else if (url.pathname === "/v1/publications") body = publications;
            else throw new Error("Unexpected API path " + url.pathname);
            await route.fulfill({ json: body });
        });

        await page.goto("http://127.0.0.1:" + port + "/home.html?channel=channel-two");
        await page.locator("#token").fill("fixture-token");
        await page.locator("#connect-form button").click();

        await page.getByText("Channel operations are current.").waitFor();
        assert.equal(await page.locator("#channel").inputValue(), "channel-two");
        const currentWorkspace = page.locator(".workspace-menu [aria-current=page]");
        assert.equal(await currentWorkspace.count(), 1);
        assert.equal((await currentWorkspace.locator("b").textContent()).trim(), "Home");
        assert.equal(await page.locator(".brand").getAttribute("href"), "/home");
        assert.equal(await page.locator("#metric-production").innerText(), "2");
        assert.equal(await page.locator("#metric-failures").innerText(), "3");
        assert.equal(await page.locator("#metric-clips").innerText(), "42");
        assert.match(await page.locator("#metric-margin").innerText(), /143\.20/);
        assert.match(await page.locator("#attention-list").innerText(), /Broken render/);
        assert.match(await page.locator("#attention-list").innerText(), /Failed upload/);
        assert.match(await page.locator("#attention-list").innerText(), /2 clips failed/);
        assert.match(await page.locator("#next-move").innerText(), /Broken render/);
        assert.match(await page.locator("#work-list").innerText(), /Top five Xbox stories/);
        assert.match(await page.locator("#growth").innerText(), /Qualified Watch Hours 365d/);
        assert.match(await page.locator("#opportunity").innerText(), /Xbox handheld/);
        assert.match(await page.locator("#ai-observability").innerText(), /16/);

        for (const id of ["open-production", "open-growth", "open-trends", "open-ai", "pulse-clips"]) {
            const href = await page.locator("#" + id).getAttribute("href");
            assert.match(href, /channel=channel-two/);
        }
        assert(requests.some((value) => value.includes("channel_profile_id=channel-two")));
        assert.equal(await page.evaluate(() => sessionStorage.getItem("katcha.channel")), "channel-two");

        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);

        const partial = await browser.newPage({ viewport: { width: 1440, height: 900 } });
        await partial.route("**/v1/**", async (route) => {
            const url = new URL(route.request().url());
            if (url.pathname === "/v1/ai/observability") {
                await route.fulfill({ status: 403, json: { detail: "Not permitted" } });
                return;
            }
            let body;
            if (url.pathname === "/v1/channels") body = [channel];
            else if (url.pathname === "/v1/channels/channel-two") body = summary;
            else if (url.pathname === "/v1/productions") body = productions;
            else if (url.pathname === "/v1/short-episodes") body = episodes;
            else if (url.pathname === "/v1/clips/library/summary") body = clipSummary;
            else if (url.pathname === "/v1/channels/channel-two/trends/explorer") body = trends;
            else if (url.pathname === "/v1/publications") body = publications;
            else throw new Error("Unexpected API path " + url.pathname);
            await route.fulfill({ json: body });
        });
        await partial.goto("http://127.0.0.1:" + port + "/home.html?channel=channel-two");
        await partial.locator("#token").fill("fixture-token");
        await partial.locator("#connect-form button").click();
        await partial.getByText(/Loaded available channel state/).waitFor();
        assert.equal(await partial.locator("#connection-state").innerText(), "PARTIAL");
        assert.match(await partial.locator("#opportunity").innerText(), /Xbox handheld/);
        assert.match(await partial.locator("#ai-observability").innerText(), /unavailable/);
        await partial.close();

        console.log("PASS: Home operator workspace, attention prioritization, live channel aggregation, cross-workspace context, partial degradation and mobile width");
    } finally {
        await browser.close();
        server.kill();
    }
})().catch((error) => {
    console.error(error);
    server.kill();
    process.exitCode = 1;
});
