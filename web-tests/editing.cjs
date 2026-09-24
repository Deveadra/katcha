const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");
const server = spawn("python3", ["-m", "http.server", "8766", "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")], { stdio: "ignore" });
const requests = [];
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
            requests.push({ path: url.pathname, method: request.method(), auth: request.headers().authorization });
            const channel = url.searchParams.get("channel_profile_id");
            let data;
            if (url.pathname === "/v1/channels") data = [{ id: "one", status: "active", profile_metadata: { name: "RankSnaxx" } }, { id: "two", status: "active", profile_metadata: { name: "Movie clips" } }];
            else if (url.pathname.endsWith("/edit-blueprints")) data = [{ blueprint_key: "persona_commentary", version: 2, contract_version: "1.0.0", is_active: false, is_default: false, created_at: "2026-09-24T00:00:00Z" }];
            else if (url.pathname.endsWith("/performance/latest")) data = null;
            else if (url.pathname === "/v1/short-episodes") data = channel === "one" ? [{ id: "episode-one", premise: "Ranking clips", status: "render_failed", stage: "render", generation: 1, edit_blueprint_key: "persona_commentary", edit_blueprint_version: 2, updated_at: "2026-09-24T00:00:00Z" }] : [];
            else if (url.pathname.endsWith("/render-attempts")) data = [{ attempt_number: 2, status: "dead_letter", error: "Renderer stopped" }];
            else if (url.pathname.endsWith("/render/recover")) data = { child_source_id: "new-generation" };
            else if (url.pathname.endsWith("/activate")) data = {};
            else throw new Error(`Unexpected request: ${url.pathname}`);
            return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
        });
        await page.goto("http://127.0.0.1:8766/editing.html");
        await page.locator("#token").fill("fixture-token");
        await page.locator("#connect-form button").click();
        await page.getByText("Ranking clips").waitFor();
        assert.equal(await page.locator("#count-attention").innerText(), "1");
        await page.getByRole("button", { name: "Recover render" }).click();
        await page.getByText(/Render recovery started/).waitFor();
        await page.locator("#channel").selectOption("two");
        await page.getByText("No episodes in this channel yet.").waitFor();
        assert.equal(await page.locator("#count-attention").innerText(), "0");
        assert(requests.some((request) => request.path.endsWith("/render/recover") && request.auth === "Bearer fixture-token"));
        assert.deepEqual(errors, []);
        console.log("Editing control center browser test passed");
    } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => server.kill());
