const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const origin = "https://app.katcha.stream";
const webDir = path.resolve(__dirname, "../src/katcha/web");
const pages = [
    ["home.html", "home"],
    ["index.html", "trends"],
    ["clips.html", "clips"],
    ["channels.html", "channel"],
    ["editing.html", "production"],
];

async function openHosted(page, routeName, sessionStatus = 200) {
    const requests = [];
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.route(origin + "/**", async (route) => {
        const request = route.request();
        const url = new URL(request.url());
        const pathname = url.pathname;
        if (pathname.startsWith("/v1/")) {
            requests.push({
                path: pathname,
                authorization: request.headers().authorization || null,
            });
            if (pathname === "/v1/control/session") {
                return route.fulfill({
                    status: sessionStatus,
                    json: sessionStatus === 200
                        ? { actor: "control-principal:cloudflare-access:operator" }
                        : { detail: sessionStatus === 403 ? "email not mapped" : "authorization required" },
                });
            }
            if (pathname === "/v1/control/access-login") {
                return route.fulfill({
                    json: {
                        enabled: true,
                        login_url: "https://katcha.cloudflareaccess.com/cdn-cgi/access/login/app.katcha.stream",
                    },
                });
            }
            if (pathname === "/v1/channels" ||
                pathname === "/v1/integrations/youtube" ||
                pathname === "/v1/channels/edit-blueprint-templates" ||
                pathname === "/v1/integrations/providers") {
                return route.fulfill({ json: [] });
            }
            return route.fulfill({ json: {} });
        }
        const file = path.resolve(webDir, "." + pathname);
        if (!file.startsWith(webDir + path.sep) || !fs.existsSync(file) ||
            !fs.statSync(file).isFile()) {
            return route.fulfill({ status: 404, body: "Not found" });
        }
        const extension = path.extname(file);
        const contentType = ({
            ".js": "application/javascript",
            ".css": "text/css",
            ".html": "text/html",
            ".svg": "image/svg+xml",
        })[extension] || "application/octet-stream";
        return route.fulfill({ body: fs.readFileSync(file), contentType });
    });
    await page.goto(origin + "/" + routeName, { waitUntil: "domcontentloaded" });
    return { requests, pageErrors };
}

(async () => {
    const browser = await chromium.launch({
        headless: true,
        executablePath: process.env.CHROMIUM_PATH || undefined,
        args: process.env.CHROMIUM_PATH ? ["--no-sandbox"] : [],
    });
    try {
        for (const [file, pageName] of pages) {
            const page = await browser.newPage();
            try {
                const result = await openHosted(page, file);
                await page.waitForFunction(
                    () => document.querySelector(".hosted-auth-notice") &&
                        document.querySelector(".hosted-auth-notice").hidden,
                );
                await page.waitForFunction(
                    () => window.__ignoredForTest === undefined &&
                        document.body.dataset.katchaPage !== "",
                );
                await page.waitForTimeout(250);
                assert(result.requests.some((r) => r.path === "/v1/control/session"),
                    pageName + " did not verify the Access operator session");
                assert(result.requests.some((r) => r.path === "/v1/channels"),
                    pageName + " did not attempt the hosted channel connection");
                assert(result.requests.every((r) => !r.authorization),
                    pageName + " exposed a bearer token for Access sign-in");
                assert.deepEqual(result.pageErrors, [], pageName + " script errors");
            } finally {
                await page.close();
            }
        }

        for (const denied of [401, 403]) {
            const page = await browser.newPage();
            try {
                const result = await openHosted(page, "index.html", denied);
                const notice = page.locator(".hosted-auth-notice");
                await notice.waitFor({ state: "visible" });
                await page.getByText("Sign in with Cloudflare Access").waitFor();
                assert.equal(result.requests.some((r) => r.path === "/v1/channels"), false,
                    "Unauthorized API session must not load channel data");
                const text = await notice.innerText();
                assert.match(text, denied === 403 ? /not mapped/ : /Sign in/);
                assert.equal(await notice.locator("a").getAttribute("href"),
                    "https://katcha.cloudflareaccess.com/cdn-cgi/access/login/app.katcha.stream");
            } finally {
                await page.close();
            }
        }
        console.log("Hosted Cloudflare Access workspace bootstrap passed");
    } finally {
        await browser.close();
    }
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
