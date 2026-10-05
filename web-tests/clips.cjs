const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const server = spawn(
    "python3",
    [
        "-m",
        "http.server",
        "8768",
        "--bind",
        "127.0.0.1",
        "--directory",
        path.resolve(__dirname, "../src/katcha/web"),
    ],
    { stdio: "ignore" },
);

const channelA = "11111111-1111-4111-8111-111111111111";
const channelB = "22222222-2222-4222-8222-222222222222";
const clipA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const clipB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

const clips = [
    {
        id: clipA,
        sha256: "a".repeat(64),
        extension: "mp4",
        size_bytes: 12582912,
        duration_seconds: 14.2,
        width: 1080,
        height: 1920,
        status: "scored",
        lifecycle_state: "hot",
        tags: ["xbox", "boss-fight"],
        library_metadata: { topic: "Xbox showcase", content_type: "gameplay", notes: "" },
        embedding_metadata: { status: "pending", stale: true },
        archived_at: null,
        purged_at: null,
        created_at: "2026-09-28T08:00:00Z",
        updated_at: "2026-09-28T08:05:00Z",
        title: "Analyzed gaming clip",
        creator: "Creator B",
        platform: "reddit",
        candidate_score: 86.4,
        channels: [{ id: channelA, name: "RankSnaxx" }],
        active_reference_count: 0,
        reference_count: 0,
    },
    {
        id: clipB,
        sha256: "b".repeat(64),
        extension: "mp4",
        size_bytes: 5242880,
        duration_seconds: 7.8,
        width: 1920,
        height: 1080,
        status: "ingested",
        analysis_status: "failed",
        analysis_stage: "failed",
        analysis_error: "Fixture model analysis failed",
        lifecycle_state: "hot",
        tags: ["funny"],
        library_metadata: { topic: "Weekly fails", content_type: "fail", notes: "" },
        embedding_metadata: { status: "pending", stale: true },
        archived_at: null,
        purged_at: null,
        created_at: "2026-09-28T09:00:00Z",
        updated_at: "2026-09-28T09:00:00Z",
        title: "Fresh funny clip",
        creator: "Creator A",
        platform: "youtube",
        candidate_score: null,
        channels: [{ id: channelA, name: "RankSnaxx" }],
        active_reference_count: 0,
        reference_count: 0,
    },
];

(async () => {
    let browser;
    let savedRetention = null;
    try {
        for (let i = 0; i < 50; i++) {
            try {
                await fetch("http://127.0.0.1:8768");
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
        const requests = [];
        page.on("pageerror", (error) => errors.push(error.message));

        await page.route("**/v1/**", (route) => {
            const request = route.request();
            const url = new URL(request.url());
            let body = null;
            try {
                body = request.postDataJSON();
            } catch {}
            requests.push({
                path: url.pathname,
                query: Object.fromEntries(url.searchParams),
                method: request.method(),
                body,
                auth: request.headers().authorization,
            });
            const json = (data, status = 200) => route.fulfill({
                status,
                contentType: "application/json",
                body: JSON.stringify(data),
            });
            if (request.headers().authorization !== "Bearer clip-token") {
                return json({ detail: "Unauthorized" }, 401);
            }

            if (url.pathname === "/v1/channels") {
                return json([
                    {
                        id: channelA,
                        status: "active",
                        profile_metadata: { channel_title: "RankSnaxx" },
                    },
                    {
                        id: channelB,
                        status: "active",
                        profile_metadata: { channel_title: "NewsWire" },
                    },
                ]);
            }

            if (url.pathname === "/v1/clips/library/summary") {
                const rows = clips.filter((clip) => clip.lifecycle_state !== "removed");
                return json({
                    total: rows.length,
                    hot: rows.filter((clip) => clip.lifecycle_state === "hot").length,
                    archived: rows.filter((clip) => clip.lifecycle_state === "archived").length,
                    purged: rows.filter((clip) => clip.lifecycle_state === "purged").length,
                    failed: rows.filter((clip) => clip.status === "failed").length,
                    hot_bytes: rows.filter((clip) => clip.lifecycle_state === "hot").reduce((sum, clip) => sum + clip.size_bytes, 0),
                    archived_bytes: rows.filter((clip) => clip.lifecycle_state === "archived").reduce((sum, clip) => sum + clip.size_bytes, 0),
                    purged_bytes: rows.filter((clip) => clip.lifecycle_state === "purged").reduce((sum, clip) => sum + clip.size_bytes, 0),
                });
            }

            if (url.pathname === "/v1/clips/library") {
                let rows = [...clips];
                const q = (url.searchParams.get("q") || "").toLowerCase();
                if (q) {
                    rows = rows.filter((clip) => JSON.stringify(clip).toLowerCase().includes(q));
                }
                const lifecycle = url.searchParams.get("lifecycle_state");
                if (lifecycle) rows = rows.filter((clip) => clip.lifecycle_state === lifecycle);
                const status = url.searchParams.get("status");
                if (status) rows = rows.filter((clip) => clip.status === status);
                return json({
                    items: rows,
                    total: rows.length,
                    offset: 0,
                    limit: 80,
                });
            }

            if (url.pathname === `/v1/clips/${clipA}/features`) {
                return json({
                    clip_id: clipA,
                    contact_sheet_key: "analysis/contact.jpg",
                    keyframe_keys: [],
                    perceptual_hashes: ["0000000000000000"],
                    transcript: "This is the stored transcript about an Xbox boss fight.",
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
            }
            if (url.pathname === `/v1/clips/${clipB}/features`) {
                return json({ detail: "clip features not found" }, 404);
            }
            if (url.pathname === `/v1/clips/${clipA}/sources`) {
                return json([{
                    id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    source_url: "https://example.com/scored",
                    canonical_url: "https://example.com/scored",
                    platform: "reddit",
                    status: "ready",
                    title: "Analyzed gaming clip",
                    creator: "Creator B",
                    discovered_at: "2026-09-28T08:00:00Z",
                }]);
            }
            if (url.pathname === `/v1/clips/${clipB}/sources`) {
                return json([{
                    id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    source_url: "https://example.com/fail",
                    canonical_url: "https://example.com/fail",
                    platform: "youtube",
                    status: "ready",
                    title: "Fresh funny clip",
                    creator: "Creator A",
                    discovered_at: "2026-09-28T09:00:00Z",
                }]);
            }
            if (url.pathname === `/v1/clips/${clipA}/media`) {
                return route.fulfill({
                    status: 200,
                    contentType: "video/mp4",
                    body: Buffer.from("fixture-video"),
                });
            }
            if (
                url.pathname === `/v1/clips/${clipA}/library-metadata`
                && request.method() === "PATCH"
            ) {
                clips[0].tags = body.tags;
                clips[0].library_metadata = {
                    topic: body.topic,
                    content_type: body.content_type,
                    notes: body.notes,
                };
                clips[0].embedding_metadata = { status: "pending", stale: true };
                return json({
                    clip_id: clipA,
                    lifecycle_state: clips[0].lifecycle_state,
                    archive_key: null,
                    tags: clips[0].tags,
                    library_metadata: clips[0].library_metadata,
                    embedding_metadata: clips[0].embedding_metadata,
                    archived_at: null,
                    purged_at: null,
                });
            }
            if (
                url.pathname === `/v1/clips/${clipA}/archive`
                && request.method() === "POST"
            ) {
                clips[0].lifecycle_state = "archived";
                clips[0].archived_at = "2026-09-28T10:00:00Z";
                return json({
                    clip_id: clipA,
                    lifecycle_state: "archived",
                    archive_key: "archive/raw/aa/a.mp4",
                    tags: clips[0].tags,
                    library_metadata: clips[0].library_metadata,
                    embedding_metadata: clips[0].embedding_metadata,
                    archived_at: clips[0].archived_at,
                    purged_at: null,
                });
            }
            if (
                url.pathname === `/v1/clips/${clipA}/purge`
                && request.method() === "POST"
            ) {
                if (body.confirmation_text !== `DELETE ${clipA}`) {
                    return json({ detail: "confirmation mismatch" }, 400);
                }
                clips[0].lifecycle_state = "purged";
                clips[0].purged_at = "2026-09-28T10:05:00Z";
                return json({
                    clip_id: clipA,
                    lifecycle_state: "purged",
                    archive_key: null,
                    tags: clips[0].tags,
                    library_metadata: clips[0].library_metadata,
                    embedding_metadata: clips[0].embedding_metadata,
                    archived_at: clips[0].archived_at,
                    purged_at: clips[0].purged_at,
                });
            }

            if (url.pathname === `/v1/channels/${channelA}/clip-retention`) {
                if (request.method() === "GET") {
                    return json(savedRetention || {
                        channel_profile_id: channelA,
                        retention_mode: "indefinite",
                        auto_archive: false,
                        auto_purge: false,
                        auto_remove_duplicates: false,
                        archive_after_days: null,
                        purge_after_days: null,
                        failed_purge_after_days: null,
                        confirmed_at: null,
                        confirmed_by: null,
                    });
                }
                if (request.method() === "PUT") {
                    savedRetention = {...body, channel_profile_id: channelA};
                    return json({
                        channel_profile_id: channelA,
                        retention_mode: body.retention_mode,
                        auto_archive: body.auto_archive,
                        auto_purge: body.auto_purge,
                        auto_remove_duplicates: body.auto_remove_duplicates,
                        archive_after_days: body.archive_after_days,
                        purge_after_days: body.purge_after_days,
                        failed_purge_after_days: body.failed_purge_after_days,
                        confirmed_at: body.auto_purge ? "2026-09-28T10:00:00Z" : null,
                        confirmed_by: body.auto_purge ? "operator" : null,
                    });
                }
            }
            if (url.pathname === `/v1/channels/${channelA}/clip-retention/preview`) {
                return json({
                    channel_profile_id: channelA,
                    channel_name: "RankSnaxx",
                    policy_enabled: true,
                    clip_count: 2,
                    actions: [{ clip_id: clipB, action: "purge", reason: "failed_retention_expired" }],
                    archive_count: 0,
                    purge_count: 1,
                    duplicate_count: 1,
                    shared_skipped: 1,
                    active_skipped: 1,
                });
            }
            if (
                url.pathname === `/v1/channels/${channelA}/clip-retention/run`
                && request.method() === "POST"
            ) {
                return json({
                    completed: [{ clip_id: clipB, action: "purge" }],
                    blocked: [],
                });
            }
            if (url.pathname === `/v1/clips/${clipB}/analyze`) {
                return json({
                    analysis_run_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                    clip_id: clipB,
                    workflow_id: "clip-analysis",
                    status: "queued",
                    stage: "queued",
                }, 202);
            }

            throw new Error(`Unexpected request: ${request.method()} ${url.pathname}`);
        });

        await page.goto("http://127.0.0.1:8768/clips.html");
        await page.locator(".workspace-tree").waitFor();
        assert.equal(await page.locator(".workspace-menu").isVisible(), false);
        assert.equal(
            (await page.locator(".workspace-tree-link.is-current b").textContent()).trim(),
            "Clips",
        );
        await page.locator("#token").fill("clip-token");
        await page.locator("#connect-form button").click();

        await page.locator(`[data-clip-id="${clipA}"]`).waitFor();
        assert.equal(await page.locator("#channel").inputValue(), channelA);
        assert.match(await page.locator("#channel-badge").innerText(), /RANKSNAXX/);
        assert.equal(await page.locator("#count-all").innerText(), "2");
        assert.equal(await page.locator("#count-hot").innerText(), "2");
        assert.match(await page.locator(".clip-library-head").innerText(), /Source media, in context/);
        assert.equal(await page.locator("[data-clip-detail-tab]").count(), 4);
        assert.equal(
            await page.locator('[data-clip-detail-tab="overview"]').getAttribute("aria-selected"),
            "true",
        );
        assert.equal(await page.locator('[data-clip-detail-view="analysis"]').first().isHidden(), true);
        assert.equal(
            await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 2),
            true,
        );

        await page.locator("#search").fill("boss-fight");
        await page.waitForTimeout(350);
        assert(
            requests.some(
                (row) => row.path === "/v1/clips/library" && row.query.q === "boss-fight",
            ),
        );
        await page.locator("#search").fill("");
        await page.waitForTimeout(350);

        await page.locator(`[data-clip-id="${clipB}"]`).click();
        const retryAnalysis = page.getByRole("button", { name: "Retry analysis", exact: true });
        await retryAnalysis.waitFor();
        assert.match(await page.locator("#clip-detail").innerText(), /Fixture model analysis failed/);
        await retryAnalysis.click();
        await page.waitForFunction(
            () => document.querySelector("#message").textContent.includes("Analysis restarted"),
        );
        assert(
            requests.some(
                (row) =>
                    row.path === `/v1/clips/${clipB}/analyze` &&
                    row.method === "POST" &&
                    row.body?.force_retry === true,
            ),
            "failed analysis retry must create a new analysis attempt",
        );

        await page.locator(`[data-clip-id="${clipA}"]`).click();
        await page.locator('[data-clip-detail-tab="analysis"]').click();
        await page.getByText(/surprising gameplay moment/).waitFor();
        assert.match(await page.locator("#clip-detail").innerText(), /stored transcript/);
        assert.equal(await page.locator('[data-clip-detail-view="overview"]').isHidden(), true);

        await page.locator('[data-clip-detail-tab="lineage"]').click();
        await page.locator('.source-link[href="https://example.com/scored"]').waitFor();

        await page.locator('[data-clip-detail-tab="metadata"]').click();
        await page.locator("#metadata-form").waitFor({ state: "visible" });
        await page.locator("#meta-tags").fill("xbox, launch-week, boss");
        await page.locator("#meta-topic").fill("Xbox launch");
        await page.locator("#metadata-form button").click();
        await page.waitForFunction(
            () => document.querySelector("#message").textContent.includes("metadata saved"),
        );
        assert(
            requests.some(
                (row) =>
                    row.path.endsWith("/library-metadata")
                    && row.body.tags.includes("launch-week"),
            ),
        );

        await page.getByRole("button", { name: "Archive", exact: true }).click();
        await page.waitForFunction(
            () => document.querySelector("#message").textContent.includes("archived"),
        );
        assert.equal(clips[0].lifecycle_state, "archived");

        await page.getByRole("button", { name: "Delete media", exact: true }).click();
        await page.locator("#purge-dialog").waitFor({ state: "visible" });
        await page.locator("#purge-continue").click();
        await page.locator("#purge-confirm-input").fill(`DELETE ${clipA}`);
        await page.locator("#purge-confirm").click();
        await page.waitForFunction(
            () => document.querySelector("#message").textContent.includes("Source media deleted"),
        );
        assert.equal(clips[0].lifecycle_state, "purged");

        await page.locator("#retention-settings").click();
        await page.locator("#retention-dialog").waitFor({ state: "visible" });
        assert.equal(await page.locator("#auto-archive").isEnabled(), true);
        await page.locator("#auto-archive").check();
        assert.equal(await page.locator("#retention-mode").inputValue(), "managed");
        await page.locator("#auto-duplicates").check();
        await page.locator("#auto-purge").check();
        await page.locator("#save-retention").click();

        assert.equal(await page.locator("#retention-confirm").isVisible(), true);
        assert.equal(await page.locator("#retention-confirm-step1").isVisible(), true);
        await page.locator("#retention-confirm-continue").click();
        const phrase = "ENABLE AUTO DELETE RankSnaxx";
        assert.equal(await page.locator("#retention-phrase").innerText(), phrase);
        await page.locator("#retention-confirm-input").fill(phrase);
        await page.locator("#retention-irreversible").check();
        await page.locator("#retention-confirm-save").click();
        await page.waitForFunction(
            () => document.querySelector("#message").textContent.includes("retention policy saved"),
        );
        const retentionUpdate = requests.find(
            (row) => row.path.endsWith("/clip-retention") && row.method === "PUT",
        );
        assert.equal(retentionUpdate.body.acknowledge_irreversible, true);
        assert.equal(retentionUpdate.body.confirmation_text, phrase);

        assert.match(await page.locator("#retention-feedback").innerText(), /saved/);
        await page.locator('[data-close-dialog="retention-dialog"]').click();
        await page.locator("#retention-settings").click();
        assert.equal(await page.locator("#retention-mode").inputValue(), "managed");
        assert.equal(await page.locator("#auto-archive").isChecked(), true);
        assert.equal(await page.locator("#auto-duplicates").isChecked(), true);
        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(
            await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
            false,
        );
        assert.deepEqual(errors, []);
        console.log(
            "Aerith Clip Library bounded workspace, failed-analysis retry, inspector tabs, channel scope, semantic search, metadata, archive, purge, retention confirmations and responsive layout passed",
        );
    } finally {
        if (browser) await browser.close();
        server.kill();
    }
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
