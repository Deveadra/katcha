/* Synthetic API fixtures verify UI orchestration; this is not live editorial acceptance. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");
const server = spawn("python3", ["-m", "http.server", "8776", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")], { stdio: "ignore" });
const calls = [];
let project = null, run = null, revision = null, acquisitionRun = null;
let loseCreateResponse = true, loseSaveResponse = true;
let review = {status: "unreviewed", sequence: 0, can_approve: true, reviews: []};
let loseReviewResponse = true;
let recordings = [];
let loseUploadResponse = true;
let failHistoryPage = true, failHistoryPreview = true, delayedHistory = null;
const historyRows = Array.from({length: 23}, (_, index) => ({editorial_run_id: `past-${index}`, target: index ? 'script' : 'render', input_revision: 1, attempt: 1, status: index ? 'blocked' : 'completed', stage: index ? 'researching' : 'render_ready_for_review', created_at: '2026-10-01T12:00:00Z', error: index ? 'Saved provider failure' : null, artifacts: {saved_revision: 1}}));
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
        page.on("pageerror", (error) => { errors.push(error.message); console.error("Browser error:", error.message); });
        await page.route("**/v1/**", (route) => {
            const request = route.request();
            const url = new URL(request.url());
            const body = request.method() === "POST" && !url.pathname.endsWith("/narration") ? request.postDataJSON() : null;
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
                if (url.pathname.endsWith('/revisions/1')) return send({revision: 1, draft, created_at: '2026-10-01T12:00:00Z'});
                if (url.pathname.includes('/runs/past-')) {
                    if (url.pathname.endsWith('/review')) return send({status: 'invalidated', blocker: 'Script revision changed; rebuild the render', reviews: [{decision: 'approve', actor: 'editor', note: 'Historical approval', created_at: '2026-10-01'}]});
                    if (url.pathname.endsWith('/preview')) {
                        if (failHistoryPreview) { failHistoryPreview = false; return send({detail: 'Preview clearance changed'}, 409); }
                        return route.fulfill({status: 200, contentType: 'video/mp4', body: 'synthetic history transport'});
                    }
                    if (url.pathname.endsWith('/past-22')) { delayedHistory = () => send(historyRows[22]); return; }
                    return send(historyRows.find(row => url.pathname.endsWith(`/${row.editorial_run_id}`)));
                }
                if (url.pathname.endsWith("/revisions")) {
                    if (!body) return send(revision ? [revision] : []);
                    if (loseSaveResponse) { loseSaveResponse = false; return send({ detail: "Save response interrupted. Your text is retained." }, 503); }
                    revision = { revision: 2, draft: body.draft }; project.revision = 2;
                    return send(revision, 201);
                }
                if (url.pathname.endsWith("/runs")) {
                    if (url.searchParams.get('limit') === '21') {
                        if (url.searchParams.has('before') && failHistoryPage) { failHistoryPage = false; return send({detail: 'History connection interrupted'}, 503); }
                        const anchor = historyRows.findIndex(row => row.editorial_run_id === url.searchParams.get('before'));
                        return send(historyRows.slice(anchor + 1, anchor + 22));
                    }
                    if (!body) return send(run ? [run, ...(acquisitionRun && run !== acquisitionRun ? [acquisitionRun] : [])] : []);
                    if (body.target === "render") {
                        run = {editorial_run_id: "render", target: "render", input_revision: 3, attempt: 1, status: "completed", stage: "render_ready_for_review", artifacts: {requires_editorial_review: true}};
                        return send(run, 202);
                    }
                    if (body.target === "assets") {
                        run = { editorial_run_id: "scout", target: "assets", attempt: 1, status: "completed", stage: "asset_candidates_ready", artifacts: { asset_scout: { candidates: [{ id: "candidate", beat_id: "beat", medium: "video", url: "https://www.youtube.com/watch?v=support", title: "Supporting interview", relevance: "Explains the symbol", rights_status: "unreviewed", acquired: false }] } } };
                        return send(run, 202);
                    }
                    if (body.target === "acquire_assets") {
                        run = { ...run, editorial_run_id: "acquire", target: "acquire_assets", stage: "assets_acquired_for_review", artifacts: { ...run.artifacts, scout_run_id: "scout", asset_scout: { candidates: run.artifacts.asset_scout.candidates.map((candidate) => ({ ...candidate, acquired: true, rights_status: "review_required" })) } } };
                        run.input_revision = 3;
                        run.artifacts.asset_selection = run.artifacts.asset_scout.candidates;
                        run.artifacts.acquired_assets = {candidate: {clip_id: "clip"}};
                        acquisitionRun = run;
                        return send(run, 202);
                    }
                    run = { editorial_run_id: "run", attempt: 1, status: "blocked", stage: "researching", error: "Provider quota rejected the request. Resume after quota is available.", artifacts: {} };
                    return send(run, 202);
                }
                if (url.pathname.endsWith("/resume")) {
                    run = { ...run, attempt: 2, status: "completed", stage: "script_ready", error: null };
                    revision = { revision: 1, draft }; project.revision = 1;
                    return send(run, 202);
                }
                if (url.pathname.endsWith("/narration")) {
                    if (request.method() === 'GET') return send({voice_enabled: true, recordings});
                    calls.at(-1).uploadKey = url.searchParams.get('idempotency_key');
                    recordings = [{id: 'audio-id', beat_id: 'beat', status: 'active', duration_seconds: 2, created_at: '2026-10-04', revision: 3}];
                    if (loseUploadResponse) { loseUploadResponse = false; return send({detail: 'Upload response lost. Retry the same recording.'}, 503); }
                    return send(recordings[0], 201);
                }
                if (url.pathname.endsWith("/preview")) return route.fulfill({status: 200, contentType: "video/mp4", body: "synthetic-media-transport-only"});
                if (url.pathname.endsWith("/review")) {
                    if (!body) return send(review);
                    review = {status: body.decision, sequence: 1, can_approve: true, reviews: [{decision: body.decision, actor: "editor", note: body.note, created_at: "2026-10-03"}]};
                    if (loseReviewResponse) { loseReviewResponse = false; return send({detail: "Review response lost. Retry to recover your decision."}, 503); }
                    return send(review.reviews[0], 201);
                }
                if (url.pathname.endsWith("/storyboard/preflight")) return send({version: "editorial-render-v1"});
                if (url.pathname.endsWith("/runs/acquire")) return send(acquisitionRun);
                if (url.pathname.includes("/runs/")) return send(run);
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
        await page.getByRole("button", { name: "Find supporting assets", exact: true }).click();
        await page.getByText("Supporting interview", { exact: true }).waitFor();
        await page.getByLabel("Select video for review download").check();
        await page.locator("#editorial-refresh").click();
        await page.getByText("Supporting interview", { exact: true }).waitFor();
        assert.equal(await page.getByLabel("Select video for review download").isChecked(), true);
        await page.getByRole("button", { name: "Download selected videos for review", exact: true }).click();
        await page.getByText(/Managed media available/).waitFor();
        const acquired = calls.find((call) => call.body?.target === "acquire_assets");
        assert.equal(acquired.body.scout_run_id, "scout");
        assert.deepEqual(acquired.body.asset_candidate_ids, ["candidate"]);
        assert.match(await page.locator("#editorial-assets").innerText(), /review required/);
        await page.locator('#editorial-storyboard select').selectOption('media:candidate');
        await page.locator('#editorial-storyboard input[type=number]').fill('1.5');
        await page.locator('#editorial-storyboard input[type=checkbox]').check();
        await page.locator('#editorial-refresh').click();
        await page.getByText(/Managed media available/).waitFor();
        assert.equal(await page.locator('#editorial-storyboard input[type=number]').inputValue(), '1.5');
        await page.getByRole('button', {name: 'Create silent captioned preview', exact: true}).click();
        await page.getByRole('button', {name: 'Load private preview', exact: true}).waitFor();
        const rendering = calls.find(call => call.body?.target === 'render');
        assert.equal(rendering.body.asset_run_id, 'acquire');
        assert.equal(rendering.body.storyboard.presentation_mode, 'captioned_silent');
        assert.equal(rendering.body.storyboard.beats[0].media[0].start_seconds, 1.5);
        assert.equal(rendering.body.storyboard.beats[0].media[0].freeze, true);
        assert(calls.find(call => call.path.endsWith('/storyboard/preflight')));
        assert.equal(await page.locator('#editorial-approve').isDisabled(), true);
        await page.getByRole('button', {name: 'Load private preview', exact: true}).click();
        await page.getByText(/Preview loaded/).waitFor();
        assert.equal(await page.locator('#editorial-preview').isVisible(), true);
        await page.locator('#editorial-review-note').fill('Evidence and timing checked.');
        await page.locator('#editorial-approve').click();
        await page.getByText(/Review response lost/).waitFor();
        assert.equal(await page.locator('#editorial-review-note').inputValue(), 'Evidence and timing checked.');
        await page.locator('#editorial-approve').click();
        await page.getByText(/Review approved for this rendered revision/).waitFor();
        const reviewCalls = calls.filter(call => call.path.endsWith('/review') && call.body);
        assert.equal(reviewCalls.length, 2);
        assert.deepEqual(reviewCalls[0].body, reviewCalls[1].body);
        await page.locator('#editorial-presentation').selectOption('narrated');
        await page.locator('[data-narration-file]').setInputFiles({name: 'recording.wav', mimeType: 'audio/wav', buffer: Buffer.from('synthetic transport only')});
        await page.locator('[data-narration-permitted]').check();
        await page.getByRole('button', {name: 'Upload recording', exact: true}).click();
        await page.getByText(/Upload response lost/).waitFor();
        assert.equal(await page.locator('[data-narration-file]').evaluate(input => input.files.length), 1);
        await page.getByRole('button', {name: 'Upload recording', exact: true}).click();
        await page.locator('[data-narration-select] option[value="audio-id"]').waitFor({state: 'attached'});
        const uploads = calls.filter(call => call.uploadKey);
        assert.equal(uploads.length, 2); assert.equal(uploads[0].uploadKey, uploads[1].uploadKey);
        assert.equal(await page.locator('[data-narration-select]').inputValue(), 'audio-id');
        await Promise.all([
            page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/runs') && response.request().postDataJSON()?.storyboard?.presentation_mode === 'narrated'),
            page.getByRole('button', {name: 'Create narrated preview', exact: true}).click(),
        ]);
        await page.getByText(/Review approved for this rendered revision/).waitFor();
        const voiced = calls.find(call => call.body?.storyboard?.presentation_mode === 'narrated');
        assert.equal(voiced.body.storyboard.narration_ids.beat, 'audio-id');
        // Independent history must preserve edits and current review/preview identity.
        await page.locator('[data-beat-narration="0"]').fill('Unsaved current script stays here.');
        const writesBeforeHistory = calls.filter(call => call.method === 'POST').length;
        await page.locator('#editorial-history > summary').click();
        await page.locator('#editorial-history-status').filter({hasText: 'History page 1'}).waitFor();
        assert.equal(await page.locator('[data-history-run]').count(), 20);
        await page.locator('[data-history-run="past-0"]').click();
        await page.locator('#editorial-history-review').filter({hasText: 'Previous approval is no longer valid'}).waitFor();
        assert.match(await page.locator('#editorial-history-script').innerText(), /This might be a connection/);
        assert.equal(await page.locator('[data-beat-narration="0"]').inputValue(), 'Unsaved current script stays here.');
        await page.locator('#editorial-history-play').click();
        await page.locator('#editorial-history-status').filter({hasText: 'Preview clearance changed'}).waitFor();
        assert.equal(await page.locator('#editorial-history-preview').isHidden(), true);
        await page.locator('#editorial-history-play').click();
        await page.locator('#editorial-history-preview').waitFor({state: 'visible'});
        await page.locator('#editorial-history-next').click();
        await page.locator('#editorial-history-status').filter({hasText: 'History connection interrupted'}).waitFor();
        assert.equal(await page.locator('[data-history-run]').count(), 20);
        await page.locator('#editorial-history-next').click();
        await page.locator('#editorial-history-status').filter({hasText: 'History page 2'}).waitFor();
        assert.equal(await page.locator('[data-history-run]').count(), 3);
        await page.locator('#editorial-history-back').click();
        await page.locator('#editorial-history-status').filter({hasText: 'History page 1'}).waitFor();
        assert.equal(calls.filter(call => call.method === 'POST').length, writesBeforeHistory);
        await page.screenshot({ path: path.resolve(__dirname, "test-results/editorial-desktop.png"), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.equal(await page.locator(".editorial-form").evaluate((node) => getComputedStyle(node).display), "grid");
        await page.screenshot({ path: path.resolve(__dirname, "test-results/editorial-mobile.png"), fullPage: true });
        await page.locator('#editorial-history-detail').scrollIntoViewIfNeeded();
        await page.screenshot({path: path.resolve(__dirname, 'test-results/editorial-history-mobile.png')});
        assert.equal(await page.locator('[data-production-tab="editorial"]').getAttribute("aria-selected"), "true");
        await page.locator('[data-production-tab="editorial"]').focus();
        await page.keyboard.press("ArrowRight");
        assert.equal(await page.locator('[data-production-tab="recipes"]').getAttribute("aria-selected"), "true");
        await page.keyboard.press("ArrowLeft");
        await page.locator('#editorial-history-next').click();
        await page.locator('[data-history-run="past-22"]').click();
        await page.waitForFunction(() => document.querySelector('#editorial-history-status').textContent === 'Loading selected work…');
        await page.locator("#channel").selectOption("two");
        assert.equal(await page.locator('#editorial-history').isHidden(), true);
        if (delayedHistory) await delayedHistory();
        assert.equal(await page.locator('#editorial-history-detail').isHidden(), true);

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
