const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const server = spawn(
    "python3",
    ["-m", "http.server", "8768", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")],
    { stdio: "ignore" },
);

(async () => {
    let browser;
    try {
        for (let i = 0; i < 50; i++) {
            try { await fetch("http://127.0.0.1:8768"); break; }
            catch { await new Promise((resolve) => setTimeout(resolve, 100)); }
        }
        browser = await chromium.launch({
            headless: true,
            executablePath: process.env.CHROMIUM_PATH || undefined,
            args: process.env.CHROMIUM_PATH ? ["--no-sandbox"] : [],
        });
        const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
        const errors = [];
        const requests = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("**/v1/**", (route) => {
            const request = route.request();
            const url = new URL(request.url());
            let body = null;
            try { body = request.postDataJSON(); } catch {}
            requests.push({ path: url.pathname, method: request.method(), body, auth: request.headers().authorization });
            const json = (data, status = 200) => route.fulfill({
                status,
                contentType: "application/json",
                body: JSON.stringify(data),
            });
            if (request.headers().authorization !== "Bearer clip-token") return json({ detail: "Unauthorized" }, 401);
            if (url.pathname === "/v1/clips") return json([
                {
                    id: "clip-scored",
                    sha256: "a".repeat(64),
                    storage_key: "raw/aa/scored.mp4",
                    extension: "mp4",
                    size_bytes: 12582912,
                    duration_seconds: 14.2,
                    width: 1080,
                    height: 1920,
                    status: "scored",
                    created_at: "2026-09-28T08:00:00Z",
                    updated_at: "2026-09-28T08:05:00Z",
                },
                {
                    id: "clip-new",
                    sha256: "b".repeat(64),
                    storage_key: "raw/bb/new.mp4",
                    extension: "mp4",
                    size_bytes: 5242880,
                    duration_seconds: 7.8,
                    width: 1920,
                    height: 1080,
                    status: "ingested",
                    created_at: "2026-09-28T09:00:00Z",
                    updated_at: "2026-09-28T09:00:00Z",
                },
                {
                    id: "clip-published",
                    sha256: "c".repeat(64),
                    storage_key: "raw/cc/history.mp4",
                    extension: "mp4",
                    size_bytes: 20971520,
                    duration_seconds: 31,
                    width: 1080,
                    height: 1920,
                    status: "published",
                    created_at: "2026-09-20T09:00:00Z",
                    updated_at: "2026-09-22T09:00:00Z",
                },
            ]);
            if (url.pathname === "/v1/sources") return json([
                { id: "source-new", source_url: "https://example.com/new", canonical_url: "https://example.com/new", platform: "youtube", status: "ready", title: "Fresh gaming clip", creator: "Creator A", workflow_id: null, error: null, clip_id: "clip-new", discovered_at: "2026-09-28T09:00:00Z", updated_at: "2026-09-28T09:00:00Z" },
                { id: "source-scored", source_url: "https://example.com/scored", canonical_url: "https://example.com/scored", platform: "reddit", status: "ready", title: "Analyzed gaming clip", creator: "Creator B", workflow_id: null, error: null, clip_id: "clip-scored", discovered_at: "2026-09-28T08:00:00Z", updated_at: "2026-09-28T08:00:00Z" },
                { id: "source-published", source_url: "https://example.com/history", canonical_url: "https://example.com/history", platform: "youtube", status: "ready", title: "Past published clip", creator: "Creator C", workflow_id: null, error: null, clip_id: "clip-published", discovered_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z" },
            ]);
            if (url.pathname === "/v1/clips/clip-scored/features") return json({
                clip_id: "clip-scored",
                contact_sheet_key: "analysis/contact.jpg",
                keyframe_keys: [],
                perceptual_hashes: [],
                transcript: "This is the stored transcript.",
                transcript_language: "en",
                transcript_confidence: 0.98,
                local_features: {},
                ai_features: {
                    bulk: {
                        event_summary: "A surprising gameplay moment with a clean payoff.",
                        categories: ["gaming", "xbox"],
                        tone: ["surprised"],
                        hook_score: 82,
                        surprise_score: 88,
                        humor_score: 61,
                        comment_potential: 79,
                        rewatch_potential: 84,
                    },
                },
                candidate_score: 86.4,
                score_breakdown: {},
                updated_at: "2026-09-28T08:05:00Z",
            });
            if (url.pathname === "/v1/clips/clip-new/features") return json({ detail: "clip features not found" }, 404);
            if (url.pathname === "/v1/clips/clip-published/features") return json({ detail: "clip features not found" }, 404);
            if (url.pathname === "/v1/clips/clip-scored/media") {
                return route.fulfill({ status: 200, contentType: "video/mp4", body: Buffer.from("fixture-video") });
            }
            if (url.pathname === "/v1/clips/clip-new/analyze") {
                return json({
                    analysis_run_id: "run-1",
                    clip_id: "clip-new",
                    workflow_id: "clip-analysis",
                    status: "queued",
                    stage: "queued",
                }, 202);
            }
            throw new Error(`Unexpected request: ${request.method()} ${url.pathname}`);
        });

        await page.goto("http://127.0.0.1:8768/clips.html");
        await page.locator(".workspace-menu").waitFor();
        await page.locator(".workspace-menu > summary").click();
        const nav = await page.locator(".workspace-menu-popover").innerText();
        assert.match(nav, /Trend explorer/);
        assert.match(nav, /Ingestion sources/);
        assert.match(nav, /Clip library/);
        assert.match(nav, /Editing control center/);
        await page.locator(".workspace-menu > summary").click();

        await page.locator("#token").fill("clip-token");
        await page.locator("#connect-form button").click();
        await page.getByText("Fresh gaming clip").waitFor();
        assert.equal(await page.locator("#count-all").innerText(), "3");
        assert.equal(await page.locator("#count-new").innerText(), "2");
        assert.equal(await page.locator("#count-history").innerText(), "1");

        await page.locator('[data-bucket="history"]').click();
        assert.equal(await page.locator(".clip-row").count(), 1);
        assert.match(await page.locator(".clip-row").innerText(), /Past published clip/);

        await page.locator('[data-bucket="all"]').click();
        await page.locator("#search").fill("Analyzed");
        assert.equal(await page.locator(".clip-row").count(), 1);
        await page.locator(".clip-row").click();
        await page.getByText(/surprising gameplay moment/).waitFor();
        assert.match(await page.locator("#clip-detail").innerText(), /Candidate score 86.4/);
        assert.match(await page.locator("#clip-detail").innerText(), /stored transcript/);

        await page.getByRole("button", { name: "Load video preview" }).click();
        await page.locator("#clip-preview video").waitFor();
        assert((await page.locator("#clip-preview video").getAttribute("src")).startsWith("blob:"));

        await page.locator("#search").fill("Fresh");
        await page.locator(".clip-row").click();
        await page.getByRole("button", { name: "Analyze clip" }).click();
        await page.waitForFunction(() => document.querySelector("#message").textContent.includes("Analysis queued"));
        assert(requests.some((row) => row.path === "/v1/clips/clip-new/analyze" && row.body.force_retry === false));

        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.deepEqual(errors, []);
        console.log("Clip library navigation, filters, detail, preview and analysis actions passed");
    } finally {
        if (browser) await browser.close();
        server.kill();
    }
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
