const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const server = spawn("python3", ["-m", "http.server", "8766", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")], { stdio: "ignore" });
const requests = [];
let brandStaged = false;
let brandActivated = false;

const personaBlueprintContract = {
    key: "persona_commentary",
    version: "1.0.0",
    composition: "blueprint_video",
    source_layout: {
        mode: "full_frame",
        fit: "contain",
        background_mode: "blurred_fill",
        header_height_px: 0,
    },
    narration: {
        mode: "persona_voice",
        required: true,
        captions_enabled: true,
        source_audio_policy: "duck",
        source_audio_volume: 0.35,
        narration_duck_volume: 0.14,
    },
    header: {
        required: false,
        max_chars: 0,
        background: "#000000",
        foreground: "#ffffff",
        font_size_px: 54,
        font_weight: 850,
        horizontal_padding_px: 56,
    },
    transition: "punch_cut",
    quality: {
        min_source_seconds: 2,
        max_duration_seconds: 60,
        max_narration_ratio: 0.48,
    },
};
const headerBlueprintContract = {
    key: "header_explainer",
    version: "1.0.0",
    composition: "blueprint_video",
    source_layout: {
        mode: "header_panel",
        fit: "contain",
        background_mode: "solid",
        header_height_px: 360,
    },
    narration: {
        mode: "text_only",
        required: false,
        captions_enabled: false,
        source_audio_policy: "retain",
        source_audio_volume: 0.72,
        narration_duck_volume: 0.18,
    },
    header: {
        required: true,
        max_chars: 220,
        background: "#000000",
        foreground: "#ffffff",
        font_size_px: 52,
        font_weight: 850,
        horizontal_padding_px: 56,
    },
    transition: "cut",
    quality: {
        min_source_seconds: 2,
        max_duration_seconds: 60,
        max_narration_ratio: 0,
    },
};
const blueprintTemplates = [
    {
        key: "persona_commentary",
        display_name: "Persona commentary",
        description: "Host-led edits with narration, captions, audio ducking, and punch cuts.",
        contract: personaBlueprintContract,
    },
    {
        key: "header_explainer",
        display_name: "Header explainer",
        description: "Clip-led edits with a persistent explanatory header and retained source audio.",
        contract: headerBlueprintContract,
    },
];
let blueprintRows = [{
    id: "blueprint-v1",
    blueprint_key: "persona_commentary",
    version: 1,
    contract_version: "1.0.0",
    is_active: true,
    is_default: true,
    contract: personaBlueprintContract,
    blueprint_metadata: {
        display_name: "RankSnaxx commentary",
        description: "Fast host-led commentary with punch cuts.",
    },
    created_at: "2026-09-24T00:00:00Z",
}];

const v1 = {
    id: "brand-v1",
    version: 1,
    brand_key: "ranksnaxx",
    is_active: true,
    contract: { visual: { brand_key: "ranksnaxx", version: 1 } },
    created_at: "2026-09-24T00:00:00Z",
};
const v2Contract = {
    brand_key: "ranksnaxx",
    version: 2,
    visual: {
        brand_key: "ranksnaxx",
        version: 2,
        reaction_pack: {
            brand_key: "ranksnaxx",
            pack_key: "host_emotes",
            version: 1,
            assets: {
                meme_cry: {
                    storage_key: "brands/ranksnaxx/reactions/host_emotes/v1/meme_cry.png",
                },
            },
        },
    },
};
const v2 = {
    id: "brand-v2",
    version: 2,
    brand_key: "ranksnaxx",
    is_active: false,
    contract: v2Contract,
    created_at: "2026-09-25T00:00:00Z",
};
const verifiedPreview = {
    id: "preview-one",
    channel_profile_id: "one",
    production_id: null,
    short_episode_id: "episode-one",
    brand_version_id: "brand-v2",
    brand_key: "ranksnaxx",
    brand_version: 2,
    workflow_id: "preview-workflow",
    workflow_attempt: 1,
    status: "verified",
    source_lineage: {},
    brand_snapshot: v2Contract,
    reaction_cue: { asset_key: "meme_cry" },
    render_manifest: {},
    output_key: "previews/brands/one/v2/preview-one.mp4",
    verification: { duration_seconds: 8.2, width: 1080, height: 1920 },
    error: null,
    created_at: "2026-09-25T00:00:00Z",
    updated_at: "2026-09-25T00:00:00Z",
};

(async () => {
    for (let i = 0; i < 50; i++) {
        try { await fetch("http://127.0.0.1:8766"); break; }
        catch { await new Promise((resolve) => setTimeout(resolve, 100)); }
    }
    const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: process.env.CHROMIUM_PATH ? ["--no-sandbox"] : [] });
    try {
        const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
        const errors = []; page.on("pageerror", (error) => errors.push(error.message));
        await page.route("**/v1/**", (route) => {
            const request = route.request(), url = new URL(request.url());
            let body = null;
            try { body = request.postDataJSON(); } catch {}
            requests.push({ path: url.pathname, method: request.method(), auth: request.headers().authorization, query: url.search, body });
            const channel = url.searchParams.get("channel_profile_id");
            const pathChannel = url.pathname.match(/^\/v1\/channels\/([^/]+)/)?.[1] || null;
            const fulfillJson = (data) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });

            if (url.pathname === "/v1/channels") return fulfillJson([{ id: "one", status: "active", profile_metadata: { name: "RankSnaxx" } }, { id: "two", status: "active", profile_metadata: { name: "Movie clips" } }]);
            if (url.pathname === "/v1/channels/edit-blueprint-templates") return fulfillJson(blueprintTemplates);
            if (url.pathname === "/v1/integrations/providers") return fulfillJson([
                { provider: "elevenlabs", capability: "text_to_speech", configured: true, mode: "api", detail: "Direct ElevenLabs TTS is ready." },
                { provider: "invideo", capability: "external_edit", configured: true, mode: "manual_bridge", detail: "Tracked external edit bridge is ready." },
            ]);
            if (url.pathname.endsWith("/edit-blueprints") && request.method() === "GET") {
                return fulfillJson(pathChannel === "one" ? blueprintRows : []);
            }
            if (url.pathname.endsWith("/edit-blueprints") && request.method() === "POST") {
                for (const row of blueprintRows) {
                    if (row.blueprint_key === body.contract.key) {
                        row.is_active = false;
                        row.is_default = false;
                    }
                    if (body.set_default) row.is_default = false;
                }
                const familyVersions = blueprintRows
                    .filter((row) => row.blueprint_key === body.contract.key)
                    .map((row) => row.version);
                const created = {
                    id: `blueprint-${body.contract.key}-${Math.max(0, ...familyVersions) + 1}`,
                    blueprint_key: body.contract.key,
                    version: Math.max(0, ...familyVersions) + 1,
                    contract_version: body.contract.version,
                    is_active: true,
                    is_default: Boolean(body.set_default) || !blueprintRows.some((row) => row.is_default),
                    contract: body.contract,
                    blueprint_metadata: {
                        display_name: body.display_name,
                        description: body.description || "",
                    },
                    created_at: "2026-09-26T00:00:00Z",
                };
                if (created.is_default) {
                    for (const row of blueprintRows) row.is_default = false;
                }
                blueprintRows.push(created);
                return fulfillJson(created);
            }
            if (url.pathname.includes("/edit-blueprints/") && url.pathname.endsWith("/activate")) {
                const match = url.pathname.match(/\/edit-blueprints\/([^/]+)\/(\d+)\/activate$/);
                const target = blueprintRows.find((row) => row.blueprint_key === match?.[1] && row.version === Number(match?.[2]));
                if (!target) throw new Error("Unknown blueprint activation");
                for (const row of blueprintRows) {
                    if (row.blueprint_key === target.blueprint_key) row.is_active = false;
                    if (body.set_default) row.is_default = false;
                }
                target.is_active = true;
                if (body.set_default) target.is_default = true;
                return fulfillJson(target);
            }
            if (url.pathname.endsWith("/performance/latest")) return fulfillJson(null);
            if (url.pathname === "/v1/short-episodes") return fulfillJson(channel === "one" ? [{
                id: "episode-one",
                premise: "Ranking clips",
                selected_script_id: "script-one",
                status: "render_failed",
                stage: "render",
                generation: 1,
                edit_blueprint_key: "persona_commentary",
                edit_blueprint_version: 2,
                render_manifest: {
                    version: "ranked-episode-render-v1",
                    overlays: [{ sequence: 4, text: "Opening narration", start_seconds: 0.4, duration_seconds: 1.5 }],
                },
                updated_at: "2026-09-24T00:00:00Z",
            }, {
                id: "episode-active",
                premise: "Active editorial fixture",
                status: "scripted",
                stage: "editorial",
                generation: 1,
                edit_blueprint_key: "persona_commentary",
                edit_blueprint_version: 2,
                render_manifest: null,
                updated_at: "2026-09-25T00:00:00Z",
            }] : []);
            if (url.pathname === "/v1/productions") return fulfillJson(channel === "one" ? [{
                id: "production-one",
                clip_id: "clip-one",
                status: "review",
                stage: "render_verified",
                generation: 1,
                render_manifest: {
                    version: "short-render-v1",
                    title_angle: "Actual frozen clip",
                    overlays: [{ text: "Narration", start_seconds: 0.5, duration_seconds: 1.5 }],
                },
                updated_at: "2026-09-25T00:00:00Z",
            }] : []);
            if (url.pathname.endsWith("/brand-candidates")) return fulfillJson(pathChannel === "one" && !brandStaged ? [{ brand_key: "ranksnaxx", version: 2, contract: v2Contract }] : []);
            if (url.pathname.endsWith("/brands") && request.method() === "GET") {
                if (channel === "two" || url.pathname.includes("/two/")) return fulfillJson([]);
                const rows = [{ ...v1, is_active: !brandActivated }];
                if (brandStaged) rows.push({ ...v2, is_active: brandActivated });
                return fulfillJson(rows);
            }
            if (url.pathname.endsWith("/brands") && request.method() === "POST") { brandStaged = true; return fulfillJson(v2); }
            if (url.pathname.endsWith("/brands/2/previews")) return fulfillJson(verifiedPreview);
            if (url.pathname.endsWith("/brand-previews/preview-one/media")) return route.fulfill({ status: 200, contentType: "video/mp4", body: Buffer.from("fixture-video") });
            if (url.pathname.endsWith("/brands/2/activate")) { brandActivated = true; return fulfillJson({ ...v2, is_active: true }); }
            if (url.pathname.endsWith("/render-attempts")) return fulfillJson(url.pathname.includes("episode-active") ? [] : [{ attempt_number: 2, status: "dead_letter", error: "Renderer stopped" }]);
            if (url.pathname === "/v1/integrations/invideo/handoffs" && request.method() === "POST") {
                return fulfillJson({
                    id: "handoff-one",
                    provider: "invideo",
                    source_type: "short_episode",
                    source_id: body.source_id,
                    generation: 1,
                    status: "prepared",
                    package_manifest_key: "external-edit/invideo/handoff-one/manifest.json",
                    output_key: null,
                    external_project_id: null,
                    handoff_metadata: { transport: "manual_bridge" },
                });
            }
            if (url.pathname.endsWith("/render/recover")) return fulfillJson({ child_source_id: "new-generation" });
            throw new Error(`Unexpected request: ${request.method()} ${url.pathname}`);
        });

        await page.goto("http://127.0.0.1:8766/editing.html");
        await page.locator(".workspace-menu").waitFor();
        await page.locator(".workspace-menu > summary").click();
        assert.match(await page.locator(".workspace-menu-popover").innerText(), /Clip library/);
        assert.match(await page.locator(".workspace-menu-popover").innerText(), /Clip Studio/);
        await page.locator(".workspace-menu > summary").click();
        await page.locator("#token").fill("fixture-token");
        await page.locator("#connect-form button").click();
        await page.getByText("Ranking clips").waitFor();
        assert.equal(await page.locator(".logo").getAttribute("href"), "/explorer");
        assert.equal(await page.locator("[data-production-tab]").count(), 3);
        assert.equal(
            await page.locator('[data-production-tab="queue"]').getAttribute("aria-selected"),
            "true",
        );
        assert.equal(await page.locator("#editorial-pipeline").isHidden(), false);
        assert.equal(await page.locator("#blueprints-panel").isHidden(), true);
        assert.equal(await page.locator("#brand-acceptance").isHidden(), true);
        assert.equal(await page.locator(".stat-link").count(), 4);
        assert.equal(await page.locator("#count-total").innerText(), "2");
        assert.equal(await page.locator("#count-active").innerText(), "1");
        assert.equal(await page.locator("#count-attention").innerText(), "1");
        assert.equal(await page.locator(".episode-item.status-attention").count(), 1);
        assert.equal(await page.locator(".episode-item.status-active").count(), 1);
        assert.match(await page.locator("#provider-status").innerText(), /ELEVENLABS/);
        assert.match(await page.locator("#provider-status").innerText(), /INVIDEO/);
        assert.equal(await page.getByRole("button", { name: "Send to InVideo" }).count(), 1);
        await page.getByRole("button", { name: "Send to InVideo" }).click();
        await page.locator("#invideo-dialog").waitFor({ state: "visible" });
        assert.match(await page.locator("#invideo-handoff-state").innerText(), /PREPARED/);
        assert(
            requests.some(
                (request) =>
                    request.path === "/v1/integrations/invideo/handoffs"
                    && request.method === "POST"
                    && request.body.source_id === "episode-one",
            ),
        );
        await page.locator("#close-invideo-dialog").click();
        assert.equal(await page.locator("#count-blueprints").innerText(), "1");
        await page.locator('a[href="#blueprints-panel"]').click();
        await page
            .locator('[data-production-tab="recipes"][aria-selected="true"]')
            .waitFor();
        await page.locator("#blueprints-panel").waitFor({ state: "visible" });
        assert.equal(await page.locator("#blueprints-panel").isHidden(), false);
        assert.equal(await page.evaluate(() => location.hash), "#blueprints-panel");
        assert.match(await page.locator(".recipe-card").innerText(), /RankSnaxx commentary/);
        assert.match(await page.locator(".recipe-card").innerText(), /DEFAULT RECIPE/);
        assert(await page.getByText("ranksnaxx v1").count());

        await page.getByRole("button", { name: "Edit recipe" }).click();
        await page.locator("#blueprint-editor-panel").waitFor();
        assert.equal(await page.locator("#bp-name").inputValue(), "RankSnaxx commentary");
        assert.equal(await page.locator("#bp-layout").inputValue(), "full_frame");
        assert.equal(await page.locator("#bp-max-duration").isDisabled(), true);
        await page.locator("#bp-fit").selectOption("cover");
        await page.locator("#bp-captions").uncheck();
        await page.locator("#blueprint-editor button[type=submit]").click();
        await page.getByText(/v2 saved/).waitFor();
        assert.equal(await page.locator("#count-blueprints").innerText(), "1");
        assert.match(await page.locator(".recipe-card").innerText(), /v2/);
        const editedRecipe = requests.find(
            (request) =>
                request.path.endsWith("/edit-blueprints")
                && request.method === "POST"
                && request.body.contract.key === "persona_commentary",
        );
        assert.equal(editedRecipe.body.contract.source_layout.fit, "cover");
        assert.equal(editedRecipe.body.contract.narration.captions_enabled, false);
        assert.equal(editedRecipe.body.contract.quality.max_duration_seconds, 60);
        assert.equal(editedRecipe.body.set_default, true);
        assert.equal(editedRecipe.body.display_name, "RankSnaxx commentary");

        await page.getByRole("button", { name: "+ New recipe" }).click();
        await page.locator("#bp-template").selectOption("header_explainer");
        await page.locator("#bp-name").fill("Quick explainer");
        await page.locator("#blueprint-editor button[type=submit]").click();
        await page.getByText(/Quick explainer v1 saved/).waitFor();
        assert.equal(await page.locator("#count-blueprints").innerText(), "2");
        const customRecipe = requests.find(
            (request) =>
                request.path.endsWith("/edit-blueprints")
                && request.method === "POST"
                && request.body.contract.key === "header_explainer_custom",
        );
        assert.equal(customRecipe.body.contract.source_layout.mode, "header_panel");
        assert.equal(customRecipe.body.contract.narration.mode, "text_only");
        await page.getByRole("button", { name: "Make default" }).click();
        assert(
            requests.some(
                (request) =>
                    request.path.endsWith("/edit-blueprints/header_explainer_custom/1/activate")
                    && request.body.set_default === true,
            ),
        );

        await page.locator('[data-summary-filter="active"]').click();
        await page
            .locator('[data-production-tab="queue"][aria-selected="true"]')
            .waitFor();
        await page.locator("#editorial-pipeline").waitFor({ state: "visible" });
        assert.equal(await page.locator("#editorial-pipeline").isHidden(), false);
        assert.equal(await page.locator("#filter").inputValue(), "active");
        assert.equal(await page.locator(".episode-item").count(), 1);
        assert.match(await page.locator(".episode-item").innerText(), /Active editorial fixture/);
        await page.locator('[data-summary-filter="attention"]').click();
        assert.equal(await page.locator("#filter").inputValue(), "attention");
        assert.equal(await page.locator(".episode-item").count(), 1);
        assert.match(await page.locator(".episode-item").innerText(), /Ranking clips/);
        await page.locator('[data-summary-filter="all"]').click();

        await page.locator('[data-production-tab="brand"]').click();
        assert.equal(await page.locator("#brand-acceptance").isHidden(), false);
        assert.equal(await page.locator("#editorial-pipeline").isHidden(), true);
        await page.getByRole("button", { name: "Stage candidate" }).click();
        await page.getByText(/Brand v2 staged/).waitFor();
        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(
            await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
            false,
        );
        assert.equal(await page.locator("[data-production-tab]").count(), 3);
        await page.getByRole("button", { name: /Render preview/ }).click();
        await page.getByText(/Brand preview verified/).waitFor();
        await page.locator("#brand-preview-video").waitFor();
        assert((await page.locator("#brand-preview-video").getAttribute("src")).startsWith("blob:"));
        await page.getByRole("button", { name: "Activate accepted brand v2" }).click();
        await page.getByText(/Brand v2 activated/).waitFor();

        await page.locator('[data-production-tab="queue"]').click();
        assert.equal(await page.locator("#editorial-pipeline").isHidden(), false);
        await page.getByRole("button", { name: "Recover render" }).click();
        await page.getByText(/Render recovery started/).waitFor();
        await page.locator("#channel").selectOption("two");
        await page.getByText("No episodes in this channel yet.").waitFor();
        assert.equal(await page.locator("#count-attention").innerText(), "0");

        const previewRequest = requests.find((request) => request.path.endsWith("/brands/2/previews"));
        assert.equal(previewRequest.body.short_episode_id, "episode-one");
        assert.equal(previewRequest.body.production_id, undefined);
        assert.equal(previewRequest.body.reaction_cue.line_ref, 4);
        assert(requests.some((request) => request.path === "/v1/productions" && request.query.includes("channel_profile_id=one")));
        assert(requests.some((request) => request.path.endsWith("/brand-previews/preview-one/media") && request.auth === "Bearer fixture-token"));
        assert(requests.some((request) => request.path.endsWith("/brands/2/activate") && request.auth === "Bearer fixture-token"));
        assert(requests.some((request) => request.path.endsWith("/render/recover") && request.auth === "Bearer fixture-token"));
        assert.deepEqual(errors, []);
        console.log("Production browser test passed");
    } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => server.kill());
