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
    { file: "home.html", label: "Home" },
    { file: "index.html", label: "Trends" },
    { file: "ai.html", label: "Katcha AI", help: 1 },
    { file: "ingestion.html", label: "Sources", help: 1 },
    { file: "clips.html", label: "Clips" },
    { file: "channels.html", label: "Channel Studio", help: 2 },
    { file: "editing.html", label: "Production", help: 3 },
    { file: "studio.html", label: "Clip Studio", help: 3 },
    { file: "settings.html", label: "Settings" },
];
const labels = workspaces.map((row) => row.label);
const groupForWorkspace = new Map([
    ["Home", "Plan"],
    ["Trends", "Plan"],
    ["Sources", "Plan"],
    ["Clips", "Create"],
    ["Katcha AI", "Create"],
    ["Production", "Create"],
    ["Clip Studio", "Create"],
    ["Channel Studio", "Grow"],
]);

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
            await menu.locator("summary").waitFor({ state: "attached" });

            const routeRelativeAssets = await page
                .locator('link[rel="stylesheet"][href], script[src]')
                .evaluateAll((nodes) =>
                    nodes
                        .map((node) => node.getAttribute("href") || node.getAttribute("src"))
                        .filter((value) => value && !value.startsWith("/") && !/^https?:/.test(value)),
                );
            assert.deepEqual(
                routeRelativeAssets,
                [],
                workspace.label + ": route-relative CSS/JS assets are not allowed",
            );

            assert.deepEqual(
                await menu.locator(".workspace-menu-popover a b").allTextContents(),
                labels,
                workspace.label + ": shared workspace order drifted",
            );
            const workspaceTree = page.locator(".workspace-tree");
            assert.equal(await workspaceTree.count(), 1, workspace.label + ": persistent workspace tree missing");
            assert.deepEqual(
                await workspaceTree.locator(".workspace-group > summary > span").allTextContents(),
                ["Plan", "Create", "Grow"],
                workspace.label + ": task-oriented navigation groups drifted",
            );
            assert.deepEqual(
                await workspaceTree.locator(".workspace-tree-link b").allTextContents(),
                ["Home", "Trends", "Sources", "Clips", "Katcha AI", "Production", "Clip Studio", "Channel Studio"],
                workspace.label + ": persistent workspace navigation drifted",
            );
            assert.equal(
                await menu.isVisible(),
                false,
                workspace.label + ": duplicate workspace switcher should stay hidden on desktop",
            );
            assert.equal(
                await workspaceTree.isVisible(),
                true,
                workspace.label + ": grouped workspace tree should be visible on desktop",
            );
            const expectedOpenGroup = groupForWorkspace.get(workspace.label);
            assert.deepEqual(
                await workspaceTree.locator(".workspace-group[open] > summary > span").allTextContents(),
                expectedOpenGroup ? [expectedOpenGroup] : [],
                workspace.label + ": only the relevant task group should open by default",
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
            const chatShortcut = page.locator("#katcha-chat-shortcut");
            assert.equal((await chatShortcut.innerText()).includes("Ask Katcha"), true);
            assert.equal(await chatShortcut.evaluate((node) => getComputedStyle(node).position), "fixed");
            const chatStyle = await chatShortcut.evaluate((node) => ({
                backgroundImage: getComputedStyle(node).backgroundImage,
                backdropFilter: getComputedStyle(node).backdropFilter || getComputedStyle(node).webkitBackdropFilter,
            }));
            assert.match(chatStyle.backgroundImage, /linear-gradient/);
            assert.equal(chatStyle.backdropFilter, "none");
            assert.equal(
                await page.locator(".ae-skip-link").evaluate((node) => getComputedStyle(node).position),
                "fixed",
            );

            if (workspace.help) {
                assert.ok(
                    await page.locator(".ae-help").count() >= workspace.help,
                    workspace.label + ": expected progressive disclosure help",
                );
            }

            await page.setViewportSize({ width: 390, height: 844 });
            assert.equal(
                await workspaceTree.isVisible(),
                false,
                workspace.label + ": desktop workspace tree should collapse on mobile",
            );
            assert.equal(
                await menu.isVisible(),
                true,
                workspace.label + ": quick workspace switcher should replace the tree on mobile",
            );
            assert.equal(
                await menu.locator(".workspace-menu-popover").isVisible(),
                false,
                workspace.label + ": workspace popover must be hidden while closed",
            );

            await menu.locator("summary").click();
            assert.equal(await menu.evaluate((node) => node.open), true);
            await page.keyboard.press("Escape");
            assert.equal(await menu.evaluate((node) => node.open), false);
            assert.equal(await menu.locator("summary").evaluate((node) => document.activeElement === node), true);

            await menu.locator("summary").click();
            await page.locator("main").click({ position: { x: 5, y: 5 } });
            assert.equal(await menu.evaluate((node) => node.open), false);

            assert.equal(
                await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
                false,
                workspace.label + ": horizontal overflow at 390px",
            );
            await page.setViewportSize({ width: 1440, height: 900 });
        }

        console.log("PASS: shared Katcha navigation, task grouping, keyboard behavior, progressive disclosure, skip navigation and mobile width");
    } finally {
        await browser.close();
    }
})()
    .catch((error) => {
        console.error(error);
        process.exitCode = 1;
    })
    .finally(() => server.kill());
