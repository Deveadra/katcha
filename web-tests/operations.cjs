const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const port = 8778;
const server = spawn(
    "python3",
    [
        "-m",
        "http.server",
        String(port),
        "--bind",
        "127.0.0.1",
        "--directory",
        path.resolve(__dirname, "../src/katcha/web"),
    ],
    { stdio: "ignore" },
);

const channelOne = "11111111-1111-4111-8111-111111111111";
const channelTwo = "22222222-2222-4222-8222-222222222222";
const now = "2026-09-29T18:00:00Z";

const channels = [
    {
        id: channelOne,
        title: "RankSnaxx",
        status: "active",
        timezone: "America/Chicago",
        active_work: 1,
        needs_attention: 2,
        fresh_opportunities: 1,
        published_last_7d: 1,
        href: "/channels?channel=" + channelOne,
    },
    {
        id: channelTwo,
        title: "Second Channel",
        status: "active",
        timezone: "UTC",
        active_work: 1,
        needs_attention: 0,
        fresh_opportunities: 1,
        published_last_7d: 0,
        href: "/channels?channel=" + channelTwo,
    },
];

function payload(selected = "") {
    const visible = selected
        ? channels.filter((row) => row.id === selected)
        : channels;
    const rankOnly = !selected || selected === channelOne;
    return {
        generated_at: now,
        summary: {
            active_channels: visible.length,
            active_work: selected ? 1 : 2,
            needs_attention: rankOnly ? 2 : 0,
            fresh_opportunities: 1,
            published_last_7d: rankOnly ? 1 : 0,
        },
        channels: visible,
        attention: rankOnly
            ? [
                  {
                      kind: "short_episode",
                      id: "33333333-3333-4333-8333-333333333333",
                      channel_profile_id: channelOne,
                      title: "Xbox fails countdown",
                      status: "render_failed",
                      stage: "retry_exhausted",
                      state: "attention",
                      message: "Renderer exhausted retries",
                      updated_at: now,
                      href:
                          "/editing?channel=" +
                          channelOne +
                          "#editorial-pipeline",
                  },
                  {
                      kind: "publication",
                      id: "44444444-4444-4444-8444-444444444444",
                      channel_profile_id: channelOne,
                      title: "Upload fixture",
                      status: "failed",
                      stage: "upload",
                      state: "attention",
                      message: "YouTube upload failed",
                      updated_at: "2026-09-29T17:55:00Z",
                      href: "/channels?channel=" + channelOne + "#content",
                  },
              ]
            : [],
        active: [
            {
                kind: "production",
                id: "55555555-5555-4555-8555-555555555555",
                channel_profile_id: selected || channelOne,
                title: selected === channelTwo ? "Second channel edit" : "Ranking clips",
                status: "rendering",
                stage: "render",
                state: "active",
                message: "render · rendering",
                updated_at: "2026-09-29T17:50:00Z",
                href:
                    "/editing?channel=" +
                    (selected || channelOne) +
                    "#editorial-pipeline",
            },
        ],
        opportunities: [
            {
                id: "66666666-6666-4666-8666-666666666666",
                channel_profile_id: selected || channelOne,
                topic: selected === channelTwo ? "Movie trailer spike" : "Xbox showcase surprise",
                lifecycle: "accelerating",
                opportunity_score: "0.910000",
                confidence: "0.830000",
                expires_at: "2026-09-30T06:00:00Z",
                reasons: ["Fast growth"],
                href: "/explorer?channel=" + (selected || channelOne),
            },
        ],
        publications: rankOnly
            ? [
                  {
                      id: "77777777-7777-4777-8777-777777777777",
                      channel_profile_id: channelOne,
                      title: "Top 5 Xbox reveals",
                      status: "published",
                      youtube_video_id: "video-one",
                      published_at: "2026-09-28T18:00:00Z",
                      sampled_at: now,
                      views: 125000,
                      average_view_percentage: "72.500000",
                      estimated_revenue: "14.25000000",
                      href: "/channels?channel=" + channelOne + "#content",
                  },
              ]
            : [],
        activity: [
            {
                event_type: "production.render_dead_lettered",
                aggregate_type: "production",
                aggregate_id: "33333333-3333-4333-8333-333333333333",
                channel_profile_id: selected || channelOne,
                created_at: now,
                href:
                    "/editing?channel=" +
                    (selected || channelOne) +
                    "#editorial-pipeline",
            },
        ],
    };
}

(async () => {
    for (let i = 0; i < 50; i++) {
        try {
            await fetch("http://127.0.0.1:" + port + "/operations.html");
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
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const requests = [];
    const errors = [];
    let failRefresh = false;
    page.on("pageerror", (error) => errors.push(error.message));

    await page.route("**/v1/operations/overview**", async (route) => {
        const request = route.request();
        const url = new URL(request.url());
        if (request.headers().authorization !== "Bearer fixture-token") {
            return route.fulfill({status: 401, json: {detail: "Token required"}});
        }
        requests.push({
            channel: url.searchParams.get("channel_profile_id") || "",
            auth: request.headers().authorization,
        });
        if (failRefresh) {
            failRefresh = false;
            await route.fulfill({
                status: 503,
                contentType: "application/json",
                body: JSON.stringify({ detail: "Fixture operations outage" }),
            });
            return;
        }
        await route.fulfill({
            json: payload(url.searchParams.get("channel_profile_id") || ""),
        });
    });

    try {
        await page.goto("http://127.0.0.1:" + port + "/operations.html");
        assert.equal(await page.getByRole("heading", { name: "Your channel briefing." }).count(), 1);
        await page.locator("#token").fill("fixture-token");
        await page.locator("#connect-form button").click();

        await page.waitForFunction(
            () => document.getElementById("pulse-attention").textContent === "2",
        );
        assert.match(await page.locator("#briefing-list").innerText(), /measured revenue/);
        assert.equal(await page.locator("#briefing-list a").count(), 4);
        assert.equal(await page.locator("#connect-form").isVisible(), false);
        assert.equal(await page.locator("#pulse-active").innerText(), "2");
        assert.equal(await page.locator("#pulse-opportunities").innerText(), "1");
        assert.equal(await page.locator("#pulse-published").innerText(), "1");
        assert.equal(await page.locator("#attention-list .ops-row").count(), 2);
        assert.match(await page.locator("#attention-list").innerText(), /Renderer exhausted retries/);
        assert.match(await page.locator("#active-list").innerText(), /Ranking clips/);
        assert.match(await page.locator("#opportunity-list").innerText(), /Xbox showcase surprise/);
        assert.match(await page.locator("#publication-list").innerText(), /125K views/);
        assert.match(await page.locator("#publication-list").innerText(), /72\.5% avg viewed/);
        assert.match(await page.locator("#publication-list").innerText(), /\$14\.25/);
        assert.equal(await page.locator("#channel-filter option").count(), 3);
        assert.equal(await page.locator("#channel-list .channel-card").count(), 2);

        const productionHref = await page.locator("#attention-list .ops-actions a").first().getAttribute("href");
        assert.match(productionHref, new RegExp("/editing\\?channel=" + channelOne));
        assert.match(
            await page.locator("#opportunity-list .ops-actions a").getAttribute("href"),
            new RegExp("/explorer\\?channel=" + channelOne),
        );

        assert.equal(await page.locator(".activity-panel").evaluate(node => node.open), true);
        assert.match(await page.locator("#activity-list").innerText(), /Production\.Render Dead Lettered/i);

        await page.locator("#channel-filter").selectOption(channelOne);
        await page.waitForFunction(
            (channel) =>
                new URL(location.href).searchParams.get("channel") === channel &&
                document.getElementById("channel-list").textContent.includes("RankSnaxx"),
            channelOne,
        );
        assert(
            requests.some((request) => request.channel === channelOne),
            "channel filter should request a scoped operations overview",
        );
        assert.equal(await page.locator("#channel-list .channel-card").count(), 1);

        failRefresh = true;
        await page.locator("#refresh").click();
        await page.getByText("Fixture operations outage").waitFor();
        assert.match(await page.locator("#attention-list").innerText(), /Xbox fails countdown/);

        assert(
            requests.every((request) => request.auth === "Bearer fixture-token"),
            "all operations API calls should carry the in-memory control token",
        );
        assert.equal(await page.locator("#token").inputValue(), "");
        assert.equal(await page.evaluate(() => localStorage.length), 0);

        require("node:fs").mkdirSync(path.join(__dirname, "test-results"), {recursive: true});
        await page.screenshot({path: path.join(__dirname, "test-results/home-desktop.png"), fullPage: true});
        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(
            await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
            false,
        );
        assert.deepEqual(errors, []);
        console.log(
            "PASS: Operations prioritization, channel drill-down, performance evidence, stale-view recovery, secure auth and mobile width",
        );
    } finally {
        await browser.close();
    }
})()
    .catch((error) => {
        console.error(error);
        process.exitCode = 1;
    })
    .finally(() => server.kill());
