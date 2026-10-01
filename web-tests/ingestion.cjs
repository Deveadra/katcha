const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const port = 8767;
const server = spawn(
    "python3",
    ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")],
    { stdio: "ignore" },
);

const catalog = [
    { key: "operator_feed", label: "Operator Feed", supports_imports: true, supported_platforms: ["custom"] },
    { key: "web_scout", label: "Autonomous Web Scout", supports_imports: false, supported_platforms: ["web"] },
    { key: "youtube", label: "YouTube Search", supports_imports: false, supported_platforms: ["youtube"] },
    { key: "reddit", label: "Reddit Search", supports_imports: false, supported_platforms: ["reddit"] },
    { key: "rss_atom", label: "RSS / Atom", supports_imports: false, supported_platforms: ["web"] },
].map((row) => ({
    version: "v1",
    description: "Installed connection.",
    source_types: [],
    query_fields: [],
    required_credentials: [],
    sample_query: {},
    ...row,
}));

const channels = [
    { id: "channel-one", status: "active", profile_metadata: { channel_title: "RankSnaxx" } },
    { id: "channel-two", status: "active", profile_metadata: { channel_title: "Forescene" } },
];

const now = "2026-10-01T12:00:00Z";
const sources = Array.from({ length: 52 }, (_, index) => ({
    id: "seed-" + String(index + 1),
    source_key: "seed-source-" + String(index + 1).padStart(3, "0"),
    name: "Seed Source " + String(index + 1).padStart(3, "0"),
    enabled: true,
    adapter_key: index % 2 ? "youtube" : "reddit",
    adapter_version: "v1",
    platform: index % 2 ? "youtube" : "reddit",
    usage_mode: index % 3 ? "candidate_review" : "discovery_only",
    channel_profile_id: index % 4 ? "channel-one" : null,
    query_template: { q: "fixture topic " + index },
    default_candidate_metadata: {},
    poll_interval_minutes: 60,
    source_metadata: { execution_mode: "manual" },
    created_at: now,
    updated_at: new Date(Date.parse(now) - index * 60000).toISOString(),
}));

const runs = [];
const finds = new Map();
const requests = [];
let handoffItems = [];
let loseSaveResponse = false;
let executeFails = false;

function clone(value) {
    return JSON.parse(JSON.stringify(value));
}

function sourceById(id) {
    return sources.find((row) => row.id === id);
}

function libraryPage(url) {
    let rows = [...sources];
    const q = (url.searchParams.get("q") || "").toLowerCase();
    const channel = url.searchParams.get("channel_profile_id");
    const shared = url.searchParams.get("shared_only") === "true";
    const adapter = url.searchParams.get("adapter_key");
    const usage = url.searchParams.get("usage_mode");
    const enabled = url.searchParams.get("enabled");
    const sort = url.searchParams.get("sort") || "recent";
    const limit = Number(url.searchParams.get("limit") || 50);
    const offset = Number(url.searchParams.get("offset") || 0);

    if (q) rows = rows.filter((row) =>
        row.name.toLowerCase().includes(q) || row.source_key.toLowerCase().includes(q)
    );
    if (channel) rows = rows.filter((row) => row.channel_profile_id === channel);
    if (shared) rows = rows.filter((row) => !row.channel_profile_id);
    if (adapter) rows = rows.filter((row) => row.adapter_key === adapter);
    if (usage) rows = rows.filter((row) => row.usage_mode === usage);
    if (enabled === "true") rows = rows.filter((row) => row.enabled);
    if (enabled === "false") rows = rows.filter((row) => !row.enabled);

    if (sort === "name") rows.sort((a, b) => a.name.localeCompare(b.name));
    else if (sort === "created") rows.sort((a, b) => b.created_at.localeCompare(a.created_at));
    else rows.sort((a, b) => b.updated_at.localeCompare(a.updated_at));

    return {
        total: rows.length,
        limit,
        offset,
        items: clone(rows.slice(offset, offset + limit)),
    };
}

function overview(source) {
    const sourceRuns = runs
        .filter((run) => run.sourceId === source.id)
        .sort((a, b) => b.created_at.localeCompare(a.created_at));
    const statusCount = (status) => sourceRuns.filter((run) => run.status === status).length;
    const completed = statusCount("completed");
    const failed = statusCount("failed");
    const terminal = completed + failed;
    const sourceFinds = finds.get(source.id) || [];
    return {
        source: clone(source),
        channel_name: source.channel_profile_id
            ? channels.find((row) => row.id === source.channel_profile_id)?.profile_metadata.channel_title || null
            : null,
        channel_status: source.channel_profile_id ? "active" : null,
        run_count: sourceRuns.length,
        completed_runs: completed,
        failed_runs: failed,
        running_runs: statusCount("running"),
        queued_runs: statusCount("queued"),
        success_rate: terminal ? completed / terminal : null,
        discovery_count: sourceFinds.length,
        unique_candidate_count: sourceFinds.length,
        recent_runs: clone(sourceRuns.slice(0, 8)),
        recent_finds: clone(sourceFinds.slice(0, 8)),
    };
}

(async () => {
    for (let i = 0; i < 50; i++) {
        try {
            await fetch("http://127.0.0.1:" + port + "/ingestion.html");
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

    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));

    await page.route("**/v1/**", async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const contentType = req.headers()["content-type"] || "";
        const body = req.method() === "POST" && contentType.includes("application/json")
            ? req.postDataJSON()
            : null;
        requests.push({ method: req.method(), path: url.pathname, search: url.search, body });

        const reply = (data, status = 200) =>
            route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });

        if (req.headers().authorization !== "Bearer test-token") {
            return reply({ detail: "Unauthorized" }, 401);
        }
        if (url.pathname === "/v1/discovery/adapters") return reply(catalog);
        if (url.pathname === "/v1/channels") return reply(channels);

        if (url.pathname === "/v1/intelligence-ingest/inbox" && req.method() === "GET") {
            const counts = { incoming: 0, processed: 0, failed: 0 };
            for (const item of handoffItems) counts[item.status] += 1;
            return reply({
                incoming_path: "handoff/incoming",
                max_file_bytes: 10485760,
                counts,
                items: clone(handoffItems),
            });
        }

        if (url.pathname === "/v1/intelligence-ingest/inbox/files" && req.method() === "POST") {
            const item = {
                filename: "rank-snaxx-first-run.json",
                status: "processed",
                size_bytes: 512,
                modified_at: now,
                channel_profile_id: "channel-one",
                batch_key: "orion-ranksnaxx-first-run",
                record_count: 4,
                error: null,
                receipt: {
                    created_count: 4,
                    updated_count: 0,
                    replayed: false,
                },
            };
            handoffItems = [item, ...handoffItems];
            return reply(clone(item), 201);
        }

        if (url.pathname === "/v1/intelligence-ingest/inbox/process" && req.method() === "POST") {
            return reply([]);
        }

        if (url.pathname === "/v1/discovery/source-library" && req.method() === "GET") {
            return reply(libraryPage(url));
        }

        if (url.pathname === "/v1/discovery/sources" && req.method() === "GET") {
            return reply(clone(sources));
        }

        if (url.pathname === "/v1/discovery/sources" && req.method() === "POST") {
            const existing = sources.find((row) => row.source_key === body.source_key);
            if (existing) {
                if (body.create_only) return reply({ detail: "source key already exists" }, 409);
                Object.assign(existing, body, { updated_at: new Date().toISOString() });
                return reply(clone(existing));
            }
            const row = {
                ...body,
                id: "source-" + (sources.length + 1),
                enabled: body.enabled ?? true,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
            };
            sources.push(row);
            if (loseSaveResponse) {
                loseSaveResponse = false;
                return route.abort();
            }
            return reply(clone(row), 201);
        }

        const sourceMatch = url.pathname.match(/^\/v1\/discovery\/sources\/([^/]+)/);
        const sourceId = sourceMatch?.[1];
        const source = sourceId ? sourceById(sourceId) : null;

        if (source && url.pathname.endsWith("/overview") && req.method() === "GET") {
            return reply(overview(source));
        }

        if (source && url.pathname.endsWith("/runs") && req.method() === "POST") {
            const key = body.idempotency_key;
            let run = runs.find((row) => row.sourceId === sourceId && row.run_key === key);
            if (!run) {
                run = {
                    id: "run-" + (runs.length + 1),
                    sourceId,
                    adapter_key: source.adapter_key,
                    adapter_version: source.adapter_version,
                    run_key: key,
                    status: "queued",
                    query: clone(source.query_template || {}),
                    cursor: {},
                    run_metadata: { ingestion_source_id: sourceId },
                    error: null,
                    started_at: null,
                    completed_at: null,
                    created_at: new Date().toISOString(),
                    updated_at: new Date().toISOString(),
                };
                runs.push(run);
            }
            return reply(clone(run), 201);
        }

        if (source && url.pathname.endsWith("/imports") && req.method() === "POST") {
            const key = body.batch_key;
            let run = runs.find((row) => row.sourceId === sourceId && row.run_key === key);
            if (!run) {
                run = {
                    id: "run-" + (runs.length + 1),
                    sourceId,
                    adapter_key: source.adapter_key,
                    adapter_version: source.adapter_version,
                    run_key: key,
                    status: "queued",
                    query: { urls: body.urls },
                    cursor: {},
                    run_metadata: { ingestion_source_id: sourceId },
                    error: null,
                    started_at: null,
                    completed_at: null,
                    created_at: new Date().toISOString(),
                    updated_at: new Date().toISOString(),
                };
                runs.push(run);
            }
            return reply({ discovery_run: clone(run), batch_key: key, item_count: body.urls.length }, 201);
        }

        const executeMatch = url.pathname.match(/^\/v1\/discovery\/runs\/([^/]+)\/execute$/);
        if (executeMatch && req.method() === "POST") {
            const run = runs.find((row) => row.id === executeMatch[1]);
            if (!run) return reply({ detail: "not found" }, 404);
            if (executeFails) return reply({ detail: "worker unavailable" }, 503);
            run.status = "running";
            run.started_at = new Date().toISOString();
            run.updated_at = run.started_at;
            return reply({ discovery_run_id: run.id, workflow_id: "discovery-run-" + run.id, status: "queued" }, 202);
        }

        const resultsMatch = url.pathname.match(
            /^\/v1\/discovery\/sources\/([^/]+)\/runs\/([^/]+)\/results$/
        );
        if (resultsMatch && req.method() === "GET") {
            const sourceFinds = finds.get(resultsMatch[1]) || [];
            return reply({
                total: sourceFinds.length,
                candidates: sourceFinds.map((item) => item.candidate),
            });
        }

        if (url.pathname.endsWith("/promote") && req.method() === "POST") {
            return reply({ status: "queued", clip_id: "clip-one" });
        }

        return reply({ detail: "unexpected request: " + url.pathname }, 404);
    });

    try {
        await page.goto("http://127.0.0.1:" + port + "/ingestion.html");
        await page.locator("#connection-panel").waitFor({ state: "visible" });
        await page.locator("#token").fill("test-token");
        await page.locator("#connect button").click();
        await page.waitForFunction(() => !document.querySelector("#workspace").disabled);

        assert.equal(await page.locator('[data-source-tab="library"]').getAttribute("aria-selected"), "true");

        // Handoff inbox imports a portable intelligence batch through the normal Sources UI.
        await page.locator('[data-source-tab="handoff"]').click();
        await page.locator('[data-source-view="handoff"]').waitFor({ state: "visible" });
        assert.match(await page.locator("#handoff-path").innerText(), /handoff\/incoming/);
        assert.equal(await page.locator("#handoff-count-incoming").innerText(), "0");
        await page.locator("#handoff-file").setInputFiles({
            name: "rank-snaxx-first-run.json",
            mimeType: "application/json",
            buffer: Buffer.from(JSON.stringify({
                channel_profile_id: "channel-one",
                batch_key: "orion-ranksnaxx-first-run",
                producer: "orion",
                source_type: "assistant",
                records: [{
                    record_kind: "video",
                    record_key: "youtube:video:test",
                    title: "Test",
                }],
            })),
        });
        await page.locator("#handoff-upload button[type=submit]").click();
        await page.waitForFunction(() =>
            document.querySelector("#handoff-count-processed")?.textContent === "1"
        );
        assert.match(await page.locator("#handoff-list").innerText(), /rank-snaxx-first-run\.json/);
        assert.match(await page.locator("#handoff-list").innerText(), /RankSnaxx/);
        assert.match(await page.locator("#message").innerText(), /4 intelligence records/);
        assert(requests.some((row) =>
            row.path === "/v1/intelligence-ingest/inbox/files" &&
            row.method === "POST"
        ));
        await page.locator('[data-source-tab="library"]').click();
        await page.locator('[data-source-view="library"]').waitFor({ state: "visible" });

        assert.equal(await page.locator(".source-row").count(), 50);
        assert.match(await page.locator("#source-count").innerText(), /52 sources/);
        assert.equal(await page.locator("#source-next").isDisabled(), false);

        await page.locator("#source-next").click();
        await page.waitForFunction(() => document.querySelectorAll(".source-row").length === 2);
        assert.equal(await page.locator(".source-row").count(), 2);
        assert.match(await page.locator("#source-page-label").innerText(), /51–52/);

        await page.locator("#source-search").fill("Seed Source 003");
        await page.waitForFunction(() => document.querySelectorAll(".source-row").length === 1);
        assert.equal(await page.locator(".source-row").count(), 1);
        assert.match(await page.locator(".source-row").innerText(), /Seed Source 003/);

        await page.locator("#source-search").fill("");
        await page.waitForFunction(() => document.querySelectorAll(".source-row").length === 50);

        // Save + check now is explicit and starts exactly one run.
        await page.locator("#header-add-source").click();
        await page.locator('[data-source-view="add"]').waitFor({ state: "visible" });
        await page.locator('[data-method="youtube"]').click();
        await page.locator("#name").fill("Xbox Launch Watch");
        await page.locator("#channel").selectOption("channel-one");
        await page.locator("#search").fill("Xbox hardware announcements");
        assert.equal(
            await page.locator('input[name="source-purpose"]:checked').inputValue(),
            "candidate_review",
        );
        assert.equal(
            await page.locator('input[name="after-save"]:checked').inputValue(),
            "run",
        );
        await page.locator("#next").click();
        assert.match(await page.locator("#review").innerText(), /Find content for review/);
        assert.match(await page.locator("#review").innerText(), /Save the source and run one check/);
        assert.equal(await page.locator("#save").innerText(), "Save & check now");
        const savedSourceCheck = page.waitForResponse((response) =>
            response.url().includes("/v1/discovery/runs/") &&
            response.url().endsWith("/execute") && response.status() === 202
        );
        await page.locator("#save").click();
        await page.waitForFunction(() => document.querySelector('[data-source-view="library"]').hidden === false);
        const xbox = sources.find((row) => row.name === "Xbox Launch Watch");
        assert(xbox);
        assert.equal(xbox.usage_mode, "candidate_review");
        assert.equal(runs.filter((row) => row.sourceId === xbox.id).length, 1);
        await savedSourceCheck;
        assert.equal(runs.find((row) => row.sourceId === xbox.id).status, "running");
        await page.waitForFunction(() => document.querySelector("#source-name")?.textContent === "Xbox Launch Watch");
        assert.match(await page.locator("#source-info").innerText(), /RankSnaxx/);
        assert.match(await page.locator("#source-info").innerText(), /Find content for review/);

        // Lost save responses recover the same source; Save only starts no run.
        await page.locator("#header-add-source").click();
        await page.locator('[data-method="youtube_channel"]').click();
        await page.locator("#name").fill("Marvel Entertainment");
        await page.locator("#channel").selectOption("channel-one");
        await page.locator("#youtube-channel").fill("https://www.youtube.com/@marvel");
        assert.equal(
            await page.locator('input[name="source-purpose"]:checked').inputValue(),
            "discovery_only",
        );
        await page.locator('input[name="after-save"][value="save"]').check();
        await page.locator("#next").click();
        assert.match(await page.locator("#review").innerText(), /Research only/);
        assert.match(await page.locator("#review").innerText(), /No search starts/);
        assert.equal(await page.locator("#save").innerText(), "Save source");

        loseSaveResponse = true;
        await page.locator("#save").click();
        await page.waitForFunction(() =>
            document.querySelector("#setup-error").textContent.includes("could not be reached")
        );
        assert.equal(sources.filter((row) => row.name === "Marvel Entertainment").length, 1);

        await page.locator("#save").click();
        await page.waitForFunction(() => document.querySelector("#source-name")?.textContent === "Marvel Entertainment");
        const marvel = sources.find((row) => row.name === "Marvel Entertainment");
        assert(marvel);
        assert.equal(marvel.usage_mode, "discovery_only");
        assert.equal(marvel.query_template.channel_reference, "https://www.youtube.com/@marvel");
        assert.equal(runs.filter((row) => row.sourceId === marvel.id).length, 0);
        assert.match(await page.locator("#message").innerText(), /No check was started/);
        assert.match(await page.locator("#source-info").innerText(), /Research only/);
        assert.match(await page.locator("#source-info").innerText(), /Saving a source does not create a recurring schedule/);

        // Exact provider failure is visible in the inspector instead of only a generic wrapper.
        runs.push({
            id: "run-marvel-failed",
            sourceId: marvel.id,
            adapter_key: "youtube",
            adapter_version: "v1",
            run_key: "marvel-failed",
            status: "failed",
            query: clone(marvel.query_template),
            cursor: {},
            run_metadata: { ingestion_source_id: marvel.id },
            error: "YouTube search request failed (status 403): quota or credential denied",
            started_at: now,
            completed_at: now,
            created_at: "2026-10-01T12:29:23.104054Z",
            updated_at: "2026-10-01T12:29:23.104054Z",
        });
        finds.set(marvel.id, [{
            observed_at: now,
            candidate: {
                id: "candidate-marvel",
                source_url: "https://www.youtube.com/watch?v=abc",
                title: "Marvel trailer",
                creator: "Marvel Entertainment",
                platform: "youtube",
            },
        }]);
        await page.locator("#history-refresh").click();
        await page.waitForFunction(() => !document.querySelector("#source-alert").hidden);
        assert.match(await page.locator("#source-alert").innerText(), /status 403/);
        assert.match(await page.locator("#history").innerText(), /quota or credential denied/);
        assert.match(await page.locator("#recent-finds").innerText(), /Marvel trailer/);
        assert.equal(await page.locator("#recent-finds [data-add-clip]").count(), 0);

        // Candidate-review sources can send finds to Clips.
        finds.set(xbox.id, [{
            observed_at: now,
            candidate: {
                id: "candidate-xbox",
                source_url: "https://www.youtube.com/watch?v=xbox",
                title: "Xbox handheld reveal",
                creator: "Xbox",
                platform: "youtube",
            },
        }]);
        const resetSourceSearch = page.locator("#source-search");
        await resetSourceSearch.fill("Xbox Launch Watch");
        await page.waitForFunction(() => document.querySelectorAll(".source-row").length === 1);
        await page.locator(".source-row").click();
        await page.waitForFunction(() => document.querySelector("#source-name")?.textContent === "Xbox Launch Watch");
        assert.equal(await page.locator("#recent-finds [data-add-clip]").count(), 1);
        await page.locator("#recent-finds [data-add-clip]").click();
        assert(requests.some((row) => row.path.endsWith("/promote") && row.body.for_review === true));

        // Filtering is server-driven and the list stays bounded.
        await page.locator("#source-search").fill("");
        await page.waitForFunction(() => document.querySelectorAll(".source-row").length === 50);
        await page.locator("#source-purpose-filter").selectOption("discovery_only");
        await page.waitForFunction(() =>
            [...document.querySelectorAll(".source-row em")].some((node) => node.textContent.includes("Research only"))
        );
        assert(requests.some((row) =>
            row.path === "/v1/discovery/source-library" &&
            row.search.includes("usage_mode=discovery_only")
        ));

        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(
            await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
            false,
        );

        assert.deepEqual(pageErrors, []);
        console.log(
            "PASS: scalable Source Library, intelligence handoff import, explicit save/run semantics, source health inspector, research-only isolation, provider failure visibility and mobile width"
        );
    } finally {
        await browser.close();
        server.kill();
    }
})().catch((error) => {
    console.error(error);
    server.kill();
    process.exitCode = 1;
});
