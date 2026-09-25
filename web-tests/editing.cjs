const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const server = spawn("python3", ["-m", "http.server", "8766", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")], { stdio: "ignore" });
const requests = [];
let brandStaged = false;
let brandActivated = false;

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
    production_id: "production-one",
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
            requests.push({ path: url.pathname, method: request.method(), auth: request.headers().authorization, query: url.search });
            const channel = url.searchParams.get("channel_profile_id");
            const fulfillJson = (data) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });

            if (url.pathname === "/v1/channels") return fulfillJson([{ id: "one", status: "active", profile_metadata: { name: "RankSnaxx" } }, { id: "two", status: "active", profile_metadata: { name: "Movie clips" } }]);
            if (url.pathname.endsWith("/edit-blueprints")) return fulfillJson([{ blueprint_key: "persona_commentary", version: 2, contract_version: "1.0.0", is_active: false, is_default: false, created_at: "2026-09-24T00:00:00Z" }]);
            if (url.pathname.endsWith("/performance/latest")) return fulfillJson(null);
            if (url.pathname === "/v1/short-episodes") return fulfillJson(channel === "one" ? [{ id: "episode-one", premise: "Ranking clips", status: "render_failed", stage: "render", generation: 1, edit_blueprint_key: "persona_commentary", edit_blueprint_version: 2, updated_at: "2026-09-24T00:00:00Z" }] : []);
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
            if (url.pathname.endsWith("/brand-candidates")) return fulfillJson(channel === "one" && !brandStaged ? [{ brand_key: "ranksnaxx", version: 2, contract: v2Contract }] : []);
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
            if (url.pathname.endsWith("/render-attempts")) return fulfillJson([{ attempt_number: 2, status: "dead_letter", error: "Renderer stopped" }]);
            if (url.pathname.endsWith("/render/recover")) return fulfillJson({ child_source_id: "new-generation" });
            if (url.pathname.includes("/edit-blueprints/") && url.pathname.endsWith("/activate")) return fulfillJson({});
            throw new Error(`Unexpected request: ${request.method()} ${url.pathname}`);
        });

        await page.goto("http://127.0.0.1:8766/editing.html");
        await page.locator("#token").fill("fixture-token");
        await page.locator("#connect-form button").click();
        await page.getByText("Ranking clips").waitFor();
        assert.equal(await page.locator("#count-attention").innerText(), "1");
        assert(await page.getByText("ranksnaxx v1").count());

        await page.getByRole("button", { name: "Stage candidate" }).click();
        await page.getByText(/Brand v2 staged/).waitFor();
        await page.getByRole("button", { name: /Render preview/ }).click();
        await page.getByText(/Brand preview verified/).waitFor();
        await page.locator("#brand-preview-video").waitFor();
        assert((await page.locator("#brand-preview-video").getAttribute("src")).startsWith("blob:"));
        await page.getByRole("button", { name: "Activate accepted brand v2" }).click();
        await page.getByText(/Brand v2 activated/).waitFor();

        await page.getByRole("button", { name: "Recover render" }).click();
        await page.getByText(/Render recovery started/).waitFor();
        await page.locator("#channel").selectOption("two");
        await page.getByText("No episodes in this channel yet.").waitFor();
        assert.equal(await page.locator("#count-attention").innerText(), "0");

        assert(requests.some((request) => request.path === "/v1/productions" && request.query.includes("channel_profile_id=one")));
        assert(requests.some((request) => request.path.endsWith("/brand-previews/preview-one/media") && request.auth === "Bearer fixture-token"));
        assert(requests.some((request) => request.path.endsWith("/brands/2/activate") && request.auth === "Bearer fixture-token"));
        assert(requests.some((request) => request.path.endsWith("/render/recover") && request.auth === "Bearer fixture-token"));
        assert.deepEqual(errors, []);
        console.log("Editing control center browser test passed");
    } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => server.kill());
