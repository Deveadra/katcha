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
        "8765",
        "--bind",
        "127.0.0.1",
        "--directory",
        path.resolve(__dirname, "../src/katcha/web"),
    ],
    { stdio: "ignore" },
);
const now = "2026-09-23T12:00:00Z";
const rows = ["alpha", "beta"].map((id, i) => ({
    topic: `TEST FIXTURE ${id}`,
    tags: ["gaming"],
    opportunity: {
        id,
        opportunity_score: 0.8 - i * 0.1,
        confidence: 0.7 + i * 0.1,
        calibrated_score: null,
        lifecycle: "accelerating",
        created_at: now,
        expires_at: "2026-09-24T12:00:00Z",
        components: { acceleration: 0.5 + i * 0.1 },
        reasons: ["Fixture growth"],
        evidence_summary: {},
    },
}));
const requests = [];
let browser;
(async () => {
    for (let i = 0; i < 50; i++) {
        try {
            await fetch("http://127.0.0.1:8765");
            break;
        } catch {
            await new Promise((r) => setTimeout(r, 100));
        }
    }
    browser = await chromium.launch({
        headless: true,
        executablePath: process.env.CHROMIUM_PATH || undefined,
        args: process.env.CHROMIUM_PATH ? ["--no-sandbox"] : [],
    });
    const page = await browser.newPage({
        viewport: { width: 1440, height: 1000 },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/v1/**", async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        requests.push({
            path: url.pathname,
            method: req.method(),
            body: req.postDataJSON(),
            auth: req.headers().authorization,
        });
        let data;
        if (url.pathname === "/v1/channels")
            data = [
                {
                    id: "channel-1",
                    status: "active",
                    profile_metadata: { name: "Fixture gaming" },
                },
            ];
        else if (url.pathname.endsWith("/watch-profile"))
            data = {
                interests: ["gaming", "xbox"],
                excluded_terms: ["giveaway"],
                min_confidence: 0.45,
                opportunity_threshold: 0.55,
            };
        else if (url.pathname.endsWith("/source-health"))
            data = { status: "healthy", fixture: true };
        else if (url.pathname.endsWith("/explorer")) data = rows;
        else if (url.pathname.endsWith("/episodes"))
            data = [
                {
                    id: "episode-1",
                    premise: "Fixture planned episode",
                    status: "planned",
                    stage: "planned",
                    evidence_frozen: true,
                },
            ];
        else if (
            url.pathname.endsWith("/editorial") ||
            url.pathname.endsWith("/refresh")
        )
            data = { workflow_id: "fixture-workflow" };
        else {
            const id = url.pathname.split("/").at(-1);
            const row = rows.find((r) => r.opportunity.id === id);
            if (!row) throw new Error(`Unexpected API path ${url.pathname}`);
            data = {
                ...row,
                evidence: {
                    version: 1,
                    packet_sha256: "a".repeat(64),
                    thesis: "Fixture thesis",
                    sources: [
                        {
                            title: "TEST SOURCE",
                            canonical_url: "https://example.com/source",
                            source_kind: "video",
                            observed_at: now,
                        },
                    ],
                },
                signals: [
                    {
                        provider_key: "youtube",
                        external_id: "video",
                        title: "TEST SOURCE",
                        observed_at: "2026-09-23T08:00:00Z",
                        metrics: { views: 10 },
                    },
                    {
                        provider_key: "youtube",
                        external_id: "video",
                        title: "TEST SOURCE",
                        observed_at: "2026-09-23T10:00:00Z",
                        metrics: { views: 20 },
                    },
                ],
                truncated: false,
            };
        }
        await route.fulfill({ json: data });
    });
    await page.goto("http://127.0.0.1:8765");
    await page.locator("#token").fill("fixture-token");
    await page.locator("#connect-form button").click();
    await page.locator("#detail-panel h3").waitFor();
    assert.equal(await page.locator(".topic-row").count(), 2);
    assert.equal(await page.locator("#token").inputValue(), "");
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    await page.locator("#search").fill("beta");
    assert.equal(await page.locator(".topic-row").count(), 1);
    await page.locator("#search").fill("");
    await page.locator("#sort").selectOption("confidence");
    assert.match(await page.locator(".topic-row").first().innerText(), /beta/);
    await page.locator("#compare").click();
    await page.locator('[data-topic="beta"]').click();
    await page
        .getByRole("heading", { name: "TEST FIXTURE beta", exact: true })
        .waitFor();
    await page.locator("#compare").click();
    assert.equal(await page.locator(".comparison-table thead th").count(), 3);
    await page.locator("#hours").selectOption("24");
    await page.locator("#series").waitFor();
    await page.locator("[data-episode]").click();
    await page.waitForFunction(() =>
        document
            .getElementById("status")
            .textContent.includes("Editorial workflow accepted"),
    );
    assert(
        requests.some(
            (r) =>
                r.path.endsWith("/beta/editorial") &&
                r.body.episode_id === "episode-1",
        ),
    );
    await page.locator("#interests").fill("gaming, indie");
    await page.locator("#save-watch").click();
    await page.waitForFunction(() =>
        document
            .getElementById("status")
            .textContent.includes("Watch version saved"),
    );
    const save = requests.find(
        (r) => r.path.endsWith("/watch-profile") && r.method === "POST",
    );
    assert.deepEqual(save.body.excluded_terms, ["giveaway"]);
    assert.deepEqual(save.body.interests, ["gaming", "indie"]);
    assert(requests.every((r) => r.auth === "Bearer fixture-token"));
    fs.mkdirSync(path.join(__dirname, "test-results"), { recursive: true });
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({
        path: path.join(__dirname, "test-results/desktop.png"),
        fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
        await page.evaluate(
            () => document.documentElement.scrollWidth > innerWidth,
        ),
        false,
    );
    await page.screenshot({
        path: path.join(__dirname, "test-results/mobile.png"),
        fullPage: true,
    });
    assert.deepEqual(errors, []);
    console.log(
        "PASS: live API UI flows, auth handling, search/sort, comparison, time windows, watch preservation, editorial handoff, mobile width",
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
