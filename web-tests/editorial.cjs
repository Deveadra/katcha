/* Synthetic API fixtures verify UI orchestration; this is not live editorial acceptance. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");
const server = spawn("python3", ["-m", "http.server", "8776", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")], { stdio: "ignore" });
const calls = [];
let project = null, run = null, revision = null;
let loseCreateResponse = true, loseSaveResponse = true;
const draft = {
    version: "editorial-draft-v1", observations: [],
    sources: [{ id: "source", title: "Interview", url: "https://example.com/interview", category: "interview", excerpt: "A synthetic quoted clue." }],
    claims: [{ id: "claim", text: "The symbol may connect to earlier material", classification: "theory", verification: "supported", verification_note: "Synthetic evidence check", source_ids: ["source"], contradictions: [] }],
    script: [{ id: "beat", role: "reveal", narration: "This might be a connection.", visual_intent: "Compare symbols", claim_ids: ["claim"], uncertainty_disclosure: "Unconfirmed theory", planned_duration_seconds: 8 }],
};
(async () => {
    const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
    try {
        const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
        const errors = [];
        page.on("pageerror", (error) => errors.push(error.message));
        await page.route("**/v1/**", (route) => {
            const request = route.request();
            const url = new URL(request.url());
            const body = request.method() === "POST" ? request.postDataJSON() : null;
            calls.push({ path: url.pathname, method: request.method(), body, auth: request.headers().authorization });
            const send = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
            if (url.pathname === "/v1/channels") return send([{ id: "one", status: "active", profile_metadata: { name: "FORESCENE" } }, { id: "two", status: "active", profile_metadata: { name: "Other channel" } }]);
            if (url.pathname.includes("/editorial-projects")) {
                if (url.pathname.includes("/two/")) return send([]);
                if (url.pathname.endsWith("/editorial-projects")) {
                    if (!body) return send(project ? [project] : []);
                    project = { id: "project", brief: body.brief, revision: 0 };
                    if (loseCreateResponse) { loseCreateResponse = false; return send({ detail: "Connection interrupted. Retry to recover the saved brief." }, 503); }
                    return send(project, 201);
                }
                if (url.pathname.endsWith("/revisions")) {
                    if (!body) return send(revision ? [revision] : []);
                    if (loseSaveResponse) { loseSaveResponse = false; return send({ detail: "Save response interrupted. Your text is retained." }, 503); }
                    revision = { revision: 2, draft: body.draft }; project.revision = 2;
                    return send(revision, 201);
                }
                if (url.pathname.endsWith("/runs")) {
                    if (!body) return send(run ? [run] : []);
                    run = { editorial_run_id: "run", attempt: 1, status: "blocked", stage: "researching", error: "Provider quota rejected the request. Resume after quota is available.", artifacts: {} };
                    return send(run, 202);
                }
                if (url.pathname.endsWith("/resume")) {
                    run = { ...run, attempt: 2, status: "completed", stage: "script_ready", error: null };
                    revision = { revision: 1, draft }; project.revision = 1;
                    return send(run, 202);
                }
                if (url.pathname.endsWith("/runs/run")) return send(run);
                return send(project);
            }
            if (url.pathname.endsWith("/performance/latest")) return send(null);
            return send([]);
        });
        await page.goto("http://127.0.0.1:8776/editing.html");
        await page.locator("#token").fill("fixture-token");
        await page.locator("#connect-form button").click();
        await page.getByText("No episodes in this channel yet.").waitFor();
        await page.locator('[data-production-tab="editorial"]').click();
        await page.getByText(/No editorial projects yet/).waitFor();
        await page.locator("#editorial-prompt").fill("Investigate the trailer clues");
        await page.locator("#editorial-urls").fill("https://www.youtube.com/watch?v=fixture");
        await page.getByRole("button", { name: "Save brief", exact: true }).click();
        await page.getByText(/Connection interrupted/).waitFor();
        assert.equal(await page.locator("#editorial-prompt").inputValue(), "Investigate the trailer clues");
        await page.getByRole("button", { name: "Save brief", exact: true }).click();
        await page.locator("#editorial-detail").waitFor({ state: "visible" });
        const creates = calls.filter((call) => call.method === "POST" && call.path.endsWith("/editorial-projects"));
        assert.equal(creates.length, 2);
        assert.equal(creates[0].body.idempotency_key, creates[1].body.idempotency_key);
        await page.getByRole("button", { name: "Research and draft script", exact: true }).click();
        await page.getByText(/Provider quota rejected/).waitFor();
        assert.equal(calls.find((call) => call.method === "POST" && call.path.endsWith("/runs")).body.target, "script");
        await page.getByRole("button", { name: "Resume saved work", exact: true }).click();
        await page.locator('[data-beat-narration="0"]').waitFor();
        assert.match(await page.locator("#editorial-evidence").innerText(), /theory/);
        assert.equal(await page.locator("#editorial-evidence a").getAttribute("href"), "https://example.com/interview");
        await page.locator('[data-beat-narration="0"]').fill("My edited narration.");
        await page.getByRole("button", { name: "Save script revision", exact: true }).click();
        await page.getByText(/Save response interrupted/).waitFor();
        assert.equal(await page.locator('[data-beat-narration="0"]').inputValue(), "My edited narration.");
        await page.locator("#editorial-refresh").click();
        await page.getByText(/Your unsaved changes have been restored/).waitFor();
        await page.getByRole("button", { name: "Save script revision", exact: true }).click();
        await page.getByText(/Script saved as a new revision/).waitFor();
        const saves = calls.filter((call) => call.method === "POST" && call.path.endsWith("/revisions"));
        assert.equal(saves[0].body.idempotency_key, saves[1].body.idempotency_key);
        assert.equal(saves[1].body.draft.script[0].narration, "My edited narration.");
        await page.locator('[data-beat-narration="0"]').fill("Keep my unsaved concurrent edit.");
        revision = { revision: 3, draft: { ...draft, script: [{ ...draft.script[0], narration: "Newer server script." }] } };
        project.revision = 3;
        await page.locator("#editorial-refresh").click();
        await page.getByText(/A newer script revision is available/).waitFor();
        assert.equal(await page.locator('[data-beat-narration="0"]').inputValue(), "Keep my unsaved concurrent edit.");
        assert.equal(await page.locator("#editorial-save-script").isDisabled(), true);
        await page.getByRole("button", { name: "Discard edits and load latest script", exact: true }).click();
        await page.getByText(/Editing revision 3/).waitFor();
        assert.equal(await page.locator('[data-beat-narration="0"]').inputValue(), "Newer server script.");
        await page.screenshot({ path: path.resolve(__dirname, "test-results/editorial-desktop.png"), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.equal(await page.locator(".editorial-form").evaluate((node) => getComputedStyle(node).display), "grid");
        await page.screenshot({ path: path.resolve(__dirname, "test-results/editorial-mobile.png"), fullPage: true });
        assert.equal(await page.locator('[data-production-tab="editorial"]').getAttribute("aria-selected"), "true");
        await page.locator('[data-production-tab="editorial"]').focus();
        await page.keyboard.press("ArrowRight");
        assert.equal(await page.locator('[data-production-tab="recipes"]').getAttribute("aria-selected"), "true");
        await page.keyboard.press("ArrowLeft");
        await page.locator("#channel").selectOption("two");
        await page.getByText(/No editorial projects yet/).waitFor();
        assert.equal(await page.locator("#editorial-detail").isHidden(), true);
        assert.equal(await page.locator("#editorial-prompt").inputValue(), "");
        await page.locator("#channel").selectOption("one");
        await page.getByRole("button", { name: "Open project", exact: true }).waitFor();
        assert.equal(await page.locator("#editorial-prompt").inputValue(), "Investigate the trailer clues");
        assert(calls.filter((call) => call.path.includes("editorial-projects")).every((call) => call.auth === "Bearer fixture-token"));
        assert.deepEqual(errors, []);
        console.log("Editorial project recovery, evidence, script editing, channel isolation and 390px checks passed.");
    } finally { await browser.close(); server.kill(); }
})().catch((error) => { server.kill(); console.error(error); process.exitCode = 1; });
