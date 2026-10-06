/* Synthetic API fixtures verify UI orchestration; this is not live editorial acceptance. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");
const server = spawn("python3", ["-m", "http.server", "8776", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")], { stdio: "ignore" });
const calls = [];
let project = null, run = null, revision = null, acquisitionRun = null;
const managedClipId = "11111111-1111-4111-8111-111111111111";
const managedSourceUrl = "https://www.youtube.com/watch?v=fixture";
const uploadedClipId = "22222222-2222-4222-8222-222222222222";
const uploadedSourceUrl = "https://upload.katcha.invalid/33333333-3333-4333-8333-333333333333";
let loseSourceMediaResponse = true;
let loseCreateResponse = true, loseSaveResponse = true;
let review = {status: "unreviewed", sequence: 0, can_approve: true, reviews: []};
let loseReviewResponse = true;
let recordings = [];
let stillImages = [], loseImageResponse = true;
let loseUploadResponse = true;
let loseGenerationResponse = true;
let loseBillingResponse = true, loseDirectionResponse = true;
const frameReceipt = {storyboard_digest: 'fixture', shots: [{beat_id: 'beat', candidate_id: 'candidate', observation: {start_seconds: 2.5, source_url: 'https://example.com/footage', observation: 'A red door <script>unsafe</script> is visible.'}, limitations: ['Sparse sampling; movement is unverified.']}]};
let directionRun = null;
let failHistoryPage = true, failHistoryPreview = true, delayedHistory = null;
let historyRequestStarted;
const historyStarted = new Promise(resolve => { historyRequestStarted = resolve; });
const historyRows = Array.from({length: 23}, (_, index) => ({editorial_run_id: `past-${index}`, target: index ? 'script' : 'render', input_revision: 1, attempt: 1, status: index ? 'blocked' : 'completed', stage: index ? 'researching' : 'render_ready_for_review', created_at: '2026-10-01T12:00:00Z', error: index ? 'Saved provider failure' : null, artifacts: {saved_revision: 1, ...(index === 0 ? {direction_shot_evidence: frameReceipt} : {})}}));
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
            const body = request.method() === "POST" && !url.pathname.endsWith("/narration") && !url.pathname.endsWith("/images") && !url.pathname.endsWith("/source-uploads") ? request.postDataJSON() : null;
            calls.push({ query: url.search, path: url.pathname, method: request.method(), body, auth: request.headers().authorization });
            const send = (value, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
            if (url.pathname === "/v1/channels") return send([{ id: "one", status: "active", profile_metadata: { name: "FORESCENE" } }, { id: "two", status: "active", profile_metadata: { name: "Other channel" } }]);
            if (url.pathname === "/v1/clips/library") return send({
                items: [{id: managedClipId, title: "Official trailer", creator: "Marvel Entertainment", platform: "youtube", duration_seconds: 90, status: "scored", lifecycle_state: "hot"}],
                total: 1, offset: 0, limit: 20,
            });
            if (url.pathname === `/v1/clips/${managedClipId}/sources`) return send([{
                id: "source-item", source_url: managedSourceUrl, canonical_url: managedSourceUrl,
                platform: "youtube", status: "ready", title: "Official trailer",
                creator: "Marvel Entertainment", discovered_at: "2026-10-01T12:00:00Z",
            }]);
            if (url.pathname.endsWith("/editorial-projects/source-uploads")) {
                calls.at(-1).sourceUploadKey = url.searchParams.get("idempotency_key");
                if (loseSourceMediaResponse) {
                    loseSourceMediaResponse = false;
                    return send({detail: "Upload response interrupted. Retry to recover the managed source."}, 503);
                }
                return send({
                    source_id: "33333333-3333-4333-8333-333333333333",
                    clip_id: uploadedClipId,
                    source_url: uploadedSourceUrl,
                    title: "Owned local source",
                    filename: "owned-source.mp4",
                    sha256: "a".repeat(64),
                    size_bytes: 23,
                    duration_seconds: 42.5,
                    width: 1920,
                    height: 1080,
                    extension: "mp4",
                    deduplicated: true,
                }, 201);
            }
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
                    if (url.pathname.endsWith('/past-22')) { delayedHistory = () => send(historyRows[22]); historyRequestStarted(); return; }
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
                    if (!body) return send(run ? [run, ...(acquisitionRun && run !== acquisitionRun ? [acquisitionRun] : []), ...(directionRun && run !== directionRun ? [directionRun] : [])] : []);
                    if (body.target === 'narration') {
                        run = {editorial_run_id: 'generated', target: 'narration', input_revision: 3, attempt: 1, status: 'completed', stage: 'narration_ready_for_review', artifacts: {generated_narration: {beat: 'generated-audio'}}};
                        recordings = [...recordings.filter(item => item.id !== 'generated-audio'), {id: 'generated-audio', beat_id: 'beat', status: 'active', duration_seconds: 2, created_at: '2026-10-04', revision: 3}];
                        if (loseGenerationResponse) { loseGenerationResponse = false; return send({detail: 'Generation response lost. Retry to recover saved work.'}, 503); }
                        return send(run, 202);
                    }
                    if (body.target === 'direction') {
                        const beat = {beat_id: 'beat', layout: 'single', media: [{candidate_id: 'candidate', start_seconds: 2, playback_rate: .5, freeze: false, push_in: 1.1}], overlays: []};
                        directionRun = {editorial_run_id: 'direction', target: 'direction', input_revision: 3, attempt: 1, status: 'completed', stage: 'storyboard_ready_for_review', artifacts: {storyboard: {...body.direction, beats: [beat]}, direction_proposal: {beats: [{...beat, rationale: 'Hold attention on the linked interview'}]}, direction_asset_run_id: 'acquire', direction_duration_seconds: 2, direction_warnings: ['Review the selected footage against the script.'], direction_shot_evidence: frameReceipt}};
                        run = directionRun;
                        if (loseDirectionResponse) { loseDirectionResponse = false; return send({detail: 'Visual plan response lost. Retry to recover saved work.'}, 503); }
                        return send(run, 202);
                    }
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
                if (url.pathname.endsWith('/narration-billing')) {
                    run.artifacts.narration_billing = [];
                    if (loseBillingResponse) { loseBillingResponse = false; return send({detail: 'Billing response lost. Retry the same receipt.'}, 503); }
                    return send({outcome: body.outcome}, 201);
                }
                if (url.pathname.endsWith('/images/still-image/revoke')) { stillImages[0].status = 'revoked'; review = {...review, status: 'invalidated', can_approve: false}; return send(stillImages[0]); }
                if (url.pathname.endsWith('/images')) {
                    if (request.method() === 'GET') return send({images: stillImages});
                    stillImages = [{id: 'still-image', beat_id: url.searchParams.get('beat_id'), revision: 3, status: 'active', width: 320, height: 180, title: url.searchParams.get('title'), source_reference: url.searchParams.get('source_reference'), use_note: url.searchParams.get('use_note'), illustration: url.searchParams.get('illustration') === 'true'}];
                    stillImages.push({...stillImages[0], id: 'second-image', title: 'Second portrait image', width: 180, height: 320});
                    if (loseImageResponse) { loseImageResponse = false; return send({detail: 'Image response lost. Retry to recover upload.'}, 503); }
                    return send(stillImages[0], 201);
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
                if (url.pathname.endsWith("/runs/direction")) return send(directionRun);
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
        await page.getByText("Use existing Katcha clip", {exact: true}).click();
        await page.getByText("Official trailer", {exact: true}).waitFor();
        await page.getByRole("button", {name: "Use clip", exact: true}).click();
        await page.getByRole("button", {name: "Use URL instead", exact: true}).waitFor();
        assert.equal(await page.locator("#editorial-urls").inputValue(), managedSourceUrl);
        assert.match(await page.locator("#editorial-selected-clips").innerText(), /Marvel Entertainment/);
        await page.getByText("Upload source video", {exact: true}).click();
        await page.locator("#editorial-source-upload-file").setInputFiles({
            name: "owned-source.mp4",
            mimeType: "video/mp4",
            buffer: Buffer.from("synthetic-video-transport"),
        });
        await page.locator("#editorial-source-upload-confirm").check();
        await page.locator("#editorial-source-upload-button").click();
        await page.locator("#editorial-source-upload-status").getByText(/Upload response interrupted/).waitFor();
        await page.locator("#editorial-source-upload-button").click();
        await page.getByText(/owned-source\.mp4 ready/).waitFor();
        assert.match(await page.locator("#editorial-selected-clips").innerText(), /Owned local source/);
        const sourceUploads = calls.filter(call => call.path.endsWith("/source-uploads"));
        assert.equal(sourceUploads.length, 2);
        assert.equal(sourceUploads[0].sourceUploadKey, sourceUploads[1].sourceUploadKey);
        await page.getByText("Start from a script", {exact: true}).click();
        await page.locator("#editorial-script-seed-file").setInputFiles({
            name: "operator-draft.md",
            mimeType: "text/markdown",
            buffer: Buffer.from("# Draft\n\nKeep this voice, but verify the factual claims."),
        });
        await page.getByText(/operator-draft\.md imported/).waitFor();
        await page.getByRole("button", { name: "Create project", exact: true }).click();
        await page.getByText(/Connection interrupted/).waitFor();
        assert.equal(await page.locator("#editorial-prompt").inputValue(), "Investigate the trailer clues");
        assert.match(await page.locator("#editorial-selected-clips").innerText(), /Official trailer/);
        assert.match(await page.locator("#editorial-script-seed-status").innerText(), /operator-draft\.md imported/);
        await page.getByRole("button", { name: "Create project", exact: true }).click();
        await page.locator("#editorial-detail").waitFor({ state: "visible" });
        assert.deepEqual(
            await page.locator("[data-editorial-stage]").allTextContents(),
            ["Research", "Script", "Assets", "Storyboard", "Preview"],
        );
        assert.equal(await page.locator('[data-editorial-stage="research"]').getAttribute("aria-selected"), "true");
        assert.equal(await page.locator('[data-editorial-stage-panel="research"]').isVisible(), true);
        assert.equal(await page.locator('[data-editorial-stage-panel="script"]').isHidden(), true);
        const researchAiHref = await page.locator('[data-editorial-ai="research"]').getAttribute("href");
        const researchAiUrl = new URL(researchAiHref, "http://127.0.0.1");
        assert.equal(researchAiUrl.pathname, "/ai");
        assert.equal(researchAiUrl.searchParams.get("channel"), "one");
        assert.equal(researchAiUrl.searchParams.get("resource_kind"), "editorial_project");
        assert.equal(researchAiUrl.searchParams.get("resource_id"), project.id);
        assert.match(researchAiUrl.searchParams.get("prompt"), /Inspect editorial project/);
        await page.locator('[data-editorial-stage="research"]').focus();
        await page.keyboard.press("ArrowRight");
        assert.equal(await page.locator('[data-editorial-stage="script"]').getAttribute("aria-selected"), "true");
        await page.keyboard.press("ArrowLeft");
        assert.equal(await page.locator('[data-editorial-stage="research"]').getAttribute("aria-selected"), "true");
        const creates = calls.filter((call) => call.method === "POST" && call.path.endsWith("/editorial-projects"));
        assert.equal(creates.length, 2);
        assert.equal(creates[0].body.idempotency_key, creates[1].body.idempotency_key);
        assert.deepEqual(creates[1].body.brief.source_clip_bindings, {
            [managedSourceUrl]: managedClipId,
            [uploadedSourceUrl]: uploadedClipId,
        });
        assert.equal(creates[1].body.brief.script_seed.origin, "operator_file");
        assert.equal(creates[1].body.brief.script_seed.filename, "operator-draft.md");
        assert.match(creates[1].body.brief.script_seed.content_sha256, /^[0-9a-f]{64}$/);
        assert.match(creates[1].body.brief.script_seed.source_file_sha256, /^[0-9a-f]{64}$/);
        assert.match(await page.locator("#editorial-source-bindings").innerText(), /Managed clip · youtube.com/);
        assert.match(await page.locator("#editorial-source-bindings").innerText(), /Uploaded source · local media/);
        assert.match(await page.locator("#editorial-source-bindings").innerText(), /Script seed · operator-draft\.md/);
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
        await page.waitForFunction(() =>
            document.querySelector('[data-editorial-stage="storyboard"]')?.getAttribute("aria-selected") === "true"
        );
        const acquired = calls.find((call) => call.body?.target === "acquire_assets");
        assert.equal(acquired.body.scout_run_id, "scout");
        assert.deepEqual(acquired.body.asset_candidate_ids, ["candidate"]);
        await page.locator('[data-editorial-stage="assets"]').click();
        await page.getByText(/Managed media available/).waitFor();
        assert.match(await page.locator("#editorial-assets").innerText(), /review required/);
        await page.locator('[data-editorial-stage="storyboard"]').click();
        await page.locator('#editorial-storyboard select[data-primary-visual]').selectOption('media:candidate');
        await page.locator('#editorial-storyboard input[type=number]:not([data-region])').fill('1.5');
        await page.locator('#editorial-storyboard input[type=checkbox]').check();
        await page.locator('#editorial-refresh').click();
        assert.equal(await page.locator('[data-editorial-stage="storyboard"]').getAttribute("aria-selected"), "true");
        assert.equal(await page.locator('#editorial-storyboard input[type=number]:not([data-region])').inputValue(), '1.5');
        await page.locator('[data-editorial-stage="assets"]').click();
        await page.getByText(/Managed media available/).waitFor();
        await page.locator('[data-editorial-stage="storyboard"]').click();
        await page.locator('[data-editorial-stage="preview"]').click();
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
        await page.locator('[data-editorial-stage="storyboard"]').click();
        await page.locator('#editorial-presentation').selectOption('narrated');
        await page.locator('[data-narration-file]').setInputFiles({name: 'recording.wav', mimeType: 'audio/wav', buffer: Buffer.from('synthetic transport only')});
        await page.locator('[data-narration-permitted]').check();
        await page.getByRole('button', {name: 'Upload recording', exact: true}).click();
        await page.getByText(/Upload response lost/).waitFor();
        assert.equal(await page.locator('[data-narration-file]').evaluate(input => input.files.length), 1);
        await page.getByRole('button', {name: 'Upload recording', exact: true}).click();
        await page.locator('[data-narration-select] option[value="audio-id"]').waitFor({state: 'attached'});
        const uploads = calls.filter(call => call.path.endsWith("/narration") && call.uploadKey);
        assert.equal(uploads.length, 2); assert.equal(uploads[0].uploadKey, uploads[1].uploadKey);
        assert.equal(await page.locator('[data-narration-select]').inputValue(), 'audio-id');
        await page.getByText('Generate channel narration', {exact: true}).click();
        await page.locator('#editorial-generate-narration').click();
        await page.getByText('Confirm use of the channel voice and budget.', {exact: true}).waitFor();
        assert.equal(calls.filter(call => call.body?.target === 'narration').length, 0);
        await page.locator('#editorial-narration-confirm').check();
        await page.locator('#editorial-narration-limit').fill('0.75');
        await page.locator('#editorial-generate-narration').click();
        await page.getByText(/Generation response lost/).waitFor();
        assert.equal(await page.locator('#editorial-narration-limit').inputValue(), '0.75');
        await page.locator('#editorial-generate-narration').click();
        await page.locator('[data-narration-select] option[value="generated-audio"]').waitFor({state: 'attached'});
        const generations = calls.filter(call => call.body?.target === 'narration');
        assert.equal(generations.length, 2);
        assert.deepEqual(generations[0].body, generations[1].body);
        assert.equal(generations[0].body.max_narration_estimate_usd, 0.75);
        run = {...run, status: 'blocked', artifacts: {...run.artifacts, narration_billing: [{beat_id: 'beat', dispatch_count: 1, estimated_cost_usd: '0.025'}]}};
        await page.locator('#editorial-refresh').click();
        await page.locator('[data-billing-save]').waitFor({state: 'visible'});
        await page.locator('[data-billing-save]').click();
        await page.getByText('Verify the final provider outcome before saving.', {exact: true}).waitFor();
        await page.locator('[data-billing-outcome]').selectOption('not_charged');
        await page.locator('[data-billing-receipt]').fill('Provider confirms synthetic request was rejected.');
        await page.locator('[data-billing-confirm]').check();
        await page.setViewportSize({width: 390, height: 844});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        await page.locator('#editorial-narration-billing').screenshot({path: path.resolve(__dirname, 'test-results/editorial-billing-mobile.png')});
        await page.setViewportSize({width: 1280, height: 900});
        await page.locator('[data-billing-save]').click();
        await page.getByText(/Billing response lost/).waitFor();
        assert.equal(await page.locator('[data-billing-receipt]').inputValue(), 'Provider confirms synthetic request was rejected.');
        await page.locator('[data-billing-save]').click();
        await page.locator('#editorial-narration-billing').waitFor({state: 'hidden'});
        const billings = calls.filter(call => call.path.endsWith('/narration-billing'));
        assert.equal(billings.length, 2);
        assert.deepEqual(billings[0].body, billings[1].body);
        await page.locator('[data-narration-select]').selectOption('generated-audio');
        await page.locator('[data-editorial-stage="preview"]').click();
        await Promise.all([
            page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/runs') && response.request().postDataJSON()?.storyboard?.presentation_mode === 'narrated'),
            page.getByRole('button', {name: 'Create narrated preview', exact: true}).click(),
        ]);
        await page.getByText(/Review approved for this rendered revision/).waitFor();
        const voiced = calls.find(call => call.body?.storyboard?.presentation_mode === 'narrated');
        assert.equal(voiced.body.storyboard.narration_ids.beat, 'generated-audio');
        assert(voiced, 'Expected a narrated storyboard call to exist');
        await page.locator('[data-editorial-stage="storyboard"]').click();
        assert.equal(await page.locator('.editorial-ai-drawer').getAttribute('open'), '');
        await page.locator('#editorial-direct').click();
        await page.getByText(/Visual plan response lost/).waitFor();
        assert.equal(await page.locator('#editorial-storyboard input[type=number]:not([data-region])').inputValue(), '1.5');
        await page.locator('#editorial-direct').click();
        await page.locator('#editorial-direction').filter({hasText: 'Hold attention on the linked interview'}).waitFor();
        const directions = calls.filter(call => call.body?.target === 'direction');
        assert.equal(directions.length, 2);
        assert.deepEqual(directions[0].body, directions[1].body);
        assert.equal(directions[0].body.direction.narration_ids.beat, 'generated-audio');
        await page.setViewportSize({width: 390, height: 844});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
        const frameSummary = page.locator('#editorial-direction summary').filter({hasText: 'Observed frame · 2.500s'});
        await frameSummary.focus();
        await page.keyboard.press('Enter');
        assert.match(await page.locator('#editorial-direction').innerText(), /Sparse sampling; movement is unverified/);
        assert.equal(await page.locator('#editorial-direction script').count(), 0);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        await page.locator('#editorial-direction').screenshot({path: path.resolve(__dirname, 'test-results/editorial-direction-mobile.png')});
        // Rendering the saved plan preserves speed and push-in, independent of manual controls.
        await page.locator('#editorial-render-directed').click();
        await page.getByText(/Review approved for this rendered revision/).waitFor();
        const directedRender = calls.filter(call => call.body?.target === 'render').at(-1);
        assert.equal(directedRender.body.direction_run_id, 'direction');
        assert.equal(directedRender.body.storyboard.beats[0].media[0].playback_rate, .5);
        assert.equal(directedRender.body.storyboard.beats[0].media[0].push_in, 1.1);
        assert.equal(directedRender.body.storyboard.narration_ids.beat, 'generated-audio');
        await page.setViewportSize({width: 1440, height: 1000});
        await page.locator('[data-editorial-stage="assets"]').click();
        await page.getByText('Upload still image', {exact: true}).click();
        await page.locator('#editorial-image-file').setInputFiles({name: 'owned.png', mimeType: 'image/png', buffer: Buffer.from('synthetic upload transport')});
        await page.locator('#editorial-image-title').fill('Original synthetic art');
        await page.locator('#editorial-image-source').fill('Owned art');
        await page.locator('#editorial-image-permission').fill('Created and permitted by operator');
        await page.locator('#editorial-image-illustration').check();
        await page.locator('#editorial-image-upload').click();
        await page.getByText(/Confirm permission to use this image/).waitFor();
        await page.locator('#editorial-image-confirm').check();
        await page.locator('#editorial-image-upload').click();
        await page.getByText(/Image response lost/).waitFor();
        assert.equal(await page.locator('#editorial-image-file').evaluate(input => input.files.length), 1);
        assert.equal(await page.locator('#editorial-image-permission').inputValue(), 'Created and permitted by operator');
        await page.locator('#editorial-image-upload').click();
        await page.locator('#editorial-images').filter({hasText: 'Original synthetic art'}).waitFor();
        const imageUploads = calls.filter(call => call.path.endsWith('/images') && call.method === 'POST');
        assert.equal(imageUploads.length, 2);
        assert.equal(imageUploads[0].query, imageUploads[1].query);
        await page.setViewportSize({width: 390, height: 844});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
        await page.locator('#editorial-image-form').screenshot({path: path.resolve(__dirname, 'test-results/editorial-images-mobile.png')});
        await page.locator('[data-editorial-stage="storyboard"]').click();
        await page.locator('#editorial-storyboard select[data-primary-visual]').selectOption('image:still-image');
        await page.locator('[data-editorial-stage="preview"]').click();
        const imageRenderRequest = page.waitForRequest((request) => {
            if (request.method() !== 'POST') return false;
            if (!new URL(request.url()).pathname.endsWith('/runs')) return false;
            try { return request.postDataJSON()?.target === 'render'; } catch { return false; }
        });
        await page.getByRole('button', {name: 'Create narrated preview', exact: true}).click();
        const imageRender = (await imageRenderRequest).postDataJSON();
        await page.getByText(/Review approved for this rendered revision/).waitFor();
        assert.equal(imageRender.storyboard.beats[0].image_id, 'still-image');
        assert.equal(imageRender.storyboard.beats[0].layout, 'image');
        await page.locator('[data-editorial-stage="storyboard"]').click();
        await page.locator('[data-image-compare]').selectOption('second-image');
        await page.locator('#editorial-storyboard summary').click();
        await page.locator('[data-region="kind"]').selectOption('circle');
        await page.locator('[data-region="target"]').selectOption('1');
        await page.locator('[data-region="x"]').fill('90');
        await page.locator('[data-editorial-stage="preview"]').click();
        await page.getByRole('button', {name: 'Create narrated preview', exact: true}).click();
        await page.getByText(/Keep the annotation within the original image/).waitFor();
        await page.locator('[data-editorial-stage="storyboard"]').click();
        await page.locator('[data-region="x"]').fill('10');
        await page.locator('[data-region="label"]').fill('Compare the symbol');
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        await page.locator('#editorial-storyboard').screenshot({path: path.resolve(__dirname, 'test-results/editorial-image-comparison-mobile.png')});
        await page.locator('[data-editorial-stage="preview"]').click();
        const comparisonRequest = page.waitForRequest(request => request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/runs') && request.postDataJSON()?.storyboard?.beats[0]?.layout === 'image_comparison');
        await page.getByRole('button', {name: 'Create narrated preview', exact: true}).click();
        const compared = (await comparisonRequest).postDataJSON().storyboard.beats[0];
        assert.deepEqual(compared.image_ids, ['still-image', 'second-image']);
        assert.deepEqual(compared.overlays[0], {kind: 'circle', media_index: 1, region: {x: .1, y: .25, width: .5, height: .5}, label: 'Compare the symbol'});
        await page.getByText(/Review approved for this rendered revision/).waitFor();
        await page.locator('[data-editorial-stage="storyboard"]').click();
        assert.equal(await page.locator('[data-image-compare]').inputValue(), 'second-image');
        assert.equal(await page.locator('[data-region="x"]').inputValue(), '10');

        await page.locator('[data-editorial-stage="assets"]').click();
        await page.locator('[data-image-revoke="still-image"]').click();
        await page.locator('[data-editorial-stage="preview"]').click();
        await page.getByText(/Previous approval is no longer valid/).waitFor();
        assert.equal(await page.locator('#editorial-storyboard select[data-primary-visual] option[value="image:still-image"]').count(), 0);
        await page.locator('[data-editorial-stage="assets"]').click();
        await page.locator('#editorial-image-file').setInputFiles({name: 'owned.png', mimeType: 'image/png', buffer: Buffer.from('synthetic upload transport')});
        await page.locator('#editorial-image-confirm').check();
        await page.locator('#editorial-image-upload').click();
        await page.locator('#editorial-images').filter({hasText: 'Original synthetic art'}).waitFor();
        const newImageUpload = calls.filter(call => call.path.endsWith('/images') && call.method === 'POST').at(-1);
        assert.notEqual(new URLSearchParams(newImageUpload.query).get('idempotency_key'), new URLSearchParams(imageUploads[0].query).get('idempotency_key'));
        await page.setViewportSize({width: 1440, height: 1000});
        await page.locator('[data-editorial-stage="script"]').click();
        // Independent history must preserve edits and current review/preview identity.
        await page.locator('[data-beat-narration="0"]').fill('Unsaved current script stays here.');
        const writesBeforeHistory = calls.filter(call => call.method === 'POST').length;
        await page.locator('#editorial-history > summary').click();
        await page.locator('#editorial-history-status').filter({hasText: 'History page 1'}).waitFor();
        assert.equal(await page.locator('[data-history-run]').count(), 20);
        await page.locator('[data-history-run="past-0"]').click();
        await page.locator('#editorial-history-review').filter({hasText: 'Previous approval is no longer valid'}).waitFor();
        assert.match(await page.locator('#editorial-history-script').innerText(), /This might be a connection/);
        await page.locator('#editorial-history-frames summary').click();
        assert.match(await page.locator('#editorial-history-frames').innerText(), /red door <script>unsafe/);
        assert.equal(await page.locator('#editorial-history-frames script').count(), 0);
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
        assert.equal(await page.locator(".editorial-stage-tabs").isVisible(), true);
        assert.equal(await page.locator(".editorial-project-rail").evaluate((node) => getComputedStyle(node).borderRightWidth), "0px");
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
        await historyStarted;
        await page.locator("#channel").selectOption("two");
        assert.equal(await page.locator('#editorial-history').isHidden(), true);
        const lateResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/runs/past-22'));
        await delayedHistory();
        await (await lateResponse).finished();
        assert.equal(await page.locator('#editorial-history-detail').isHidden(), true);

        await page.getByText(/No editorial projects yet/).waitFor();
        assert.equal(await page.locator("#editorial-detail").isHidden(), true);
        assert.equal(await page.locator("#editorial-narration-confirm").isChecked(), false);
        assert.equal(await page.locator("#editorial-prompt").inputValue(), "");
        await page.locator("#channel").selectOption("one");
        await page.getByRole("button", { name: "Open project", exact: true }).waitFor();
        assert.equal(await page.locator("#editorial-prompt").inputValue(), "Investigate the trailer clues");
        assert(calls.filter((call) => call.path.includes("editorial-projects")).every((call) => call.auth === "Bearer fixture-token"));
        assert.deepEqual(errors, []);
        console.log("Editorial project recovery, evidence, script editing, channel isolation and 390px checks passed.");
    } finally { await browser.close(); server.kill(); }
})().catch((error) => { server.kill(); console.error(error); process.exitCode = 1; });
