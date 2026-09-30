const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");

const port = 8776;
const server = spawn(
    "python3",
    ["-m", "http.server", String(port), "--bind", "127.0.0.1", "--directory", path.resolve(__dirname, "../src/katcha/web")],
    { stdio: "ignore" },
);

const workspaces = [
    { file: "operations.html", label: "Home" },
    { file: "index.html", label: "Trends" },
    { file: "ai.html", label: "Katcha AI", help: 1 },
    { file: "ingestion.html", label: "Sources", help: 1 },
    { file: "clips.html", label: "Clips" },
    { file: "channels.html", label: "Channel Studio", help: 2 },
    { file: "editing.html", label: "Production", help: 3 },
    { file: "studio.html", label: "Clip Studio", help: 3 },
];
const labels = workspaces.map((row) => row.label);

(async () => {
    for (let i = 0; i < 50; i++) {
        try {
            await fetch("http://127.0.0.1:" + port + "/index.html");
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

    try {
        for (const workspace of workspaces) {
            await page.goto("http://127.0.0.1:" + port + "/" + workspace.file, { waitUntil: "domcontentloaded" });
            const menu = page.locator(".workspace-menu");
            await menu.locator("summary").waitFor();

            assert.deepEqual(
                await menu.locator(".workspace-menu-popover a b").allTextContents(),
                labels,
                workspace.label + ": shared workspace order drifted",
            );
            const currentWorkspace = menu.locator('[aria-current="page"]');
            assert.equal(
                await currentWorkspace.count(),
                1,
                workspace.label + ": expected exactly one current workspace",
            );
            assert.equal(
                (await currentWorkspace.locator("b").textContent()).trim(),
                workspace.label,
                workspace.label + ": current workspace state missing",
            );
            assert.equal(await page.locator(".ae-skip-link").count(), 1);
            assert.equal(await page.locator("main#main-content").count(), 1);
            assert.equal((await page.locator("#katcha-chat-shortcut").innerText()).includes("Ask Katcha"), true);

            await menu.locator("summary").click();
            assert.equal(await menu.evaluate((node) => node.open), true);
            await page.keyboard.press("Escape");
            assert.equal(await menu.evaluate((node) => node.open), false);
            assert.equal(await menu.locator("summary").evaluate((node) => document.activeElement === node), true);

            await menu.locator("summary").click();
            await page.locator("main").click({ position: { x: 5, y: 5 } });
            assert.equal(await menu.evaluate((node) => node.open), false);

            if (workspace.help) {
                assert.ok(
                    await page.locator(".ae-help").count() >= workspace.help,
                    workspace.label + ": expected progressive disclosure help",
                );
            }

            await page.setViewportSize({ width: 390, height: 844 });
            assert.equal(
                await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
                false,
                workspace.label + ": horizontal overflow at 390px",
            );
            await page.setViewportSize({ width: 1440, height: 900 });
        }

        console.log("PASS: shared Aerith navigation, terminology, keyboard behavior, progressive disclosure, skip navigation and mobile width");
    } finally {
        await browser.close();
    }
})()
    .catch((error) => {
        console.error(error);
        process.exitCode = 1;
    })
    .finally(() => server.kill());
