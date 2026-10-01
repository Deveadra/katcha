// Synthetic provider responses; no live account inference.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const port = 8781;
const server = spawn('python3', ['-m', 'http.server', String(port), '--bind', '127.0.0.1', '--directory', path.resolve(__dirname, '../src/katcha/web')], {stdio: 'ignore'});
(async () => {
    let browser;
    try {
        for (let i = 0; i < 50; i++) {
            try {await fetch(`http://127.0.0.1:${port}`); break;}
            catch {await new Promise(r => setTimeout(r, 100));}
        }
        browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: process.env.CHROMIUM_PATH ? ['--no-sandbox'] : []});
        const page = await browser.newPage({viewport: {width: 390, height: 844}});
        const errors = [];
        let failCheck = false;
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/runtime/status', route => route.fulfill({json: {settings: {KATCHA_AI_EXECUTION_MODE: 'live'}}}));
        await page.route('**/v1/**', route => {
            const url = new URL(route.request().url());
            if (url.pathname.endsWith('/system-check')) return route.fulfill({status: failCheck ? 503 : 200, json: failCheck ? {detail: 'Runtime unavailable. Retry the check.'} : {
                live_inference_verified: false, checks: [
                    {key: 'ingestion', label: 'Video downloading', status: 'unavailable', detail: 'Restart Katcha to resume downloads.'},
                    {key: 'ai', label: 'AI inference', status: 'configured', detail: 'Use Test response to verify live inference.'},
                ],
            }});
            if (url.pathname.endsWith('/codex/status')) return route.fulfill({json: {connected: true, selected_model: 'fixture', email: 'fixture@example.com'}});
            if (url.pathname.endsWith('/codex/models')) return route.fulfill({json: [{slug: 'fixture', display_name: 'Fixture'}]});
            if (url.pathname.endsWith('/codex/usage')) return route.fulfill({status: 503, json: {detail: 'Usage telemetry unavailable'}});
            if (url.pathname.endsWith('/codex/test')) return route.fulfill({json: {selected_model: 'fixture', response: 'OK', usage_error: 'Unavailable'}});
            if (url.pathname.endsWith('/chatgpt/status')) return route.fulfill({json: {connected: false}});
            return route.fulfill({json: {}});
        });
        await page.goto(`http://127.0.0.1:${port}/settings.html`);
        await page.waitForFunction(() => document.querySelector('#codex-primary-value').textContent === 'Unavailable');
        await page.locator('#codex-test').click();
        await page.waitForFunction(() => document.querySelector('#settings-message').textContent.includes('real inference verified'));
        assert.equal(await page.locator('#settings-message').getAttribute('class'), 'settings-message good');
        await page.locator('#check-systems').focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(() => document.querySelector('#system-check-results').textContent.includes('Restart Katcha'));
        assert.match(await page.locator('#system-check-results').textContent(), /configured/);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
        failCheck = true;
        await page.locator('#check-systems').click();
        await page.waitForFunction(() => document.querySelector('#system-check-results').textContent.includes('Runtime unavailable'));
        assert.equal(await page.locator('#check-systems').isEnabled(), true);
        assert.deepEqual(errors, []);
        console.log('Settings recovery, telemetry outage, keyboard and mobile tests passed (fixtures).');
    } finally {if (browser) await browser.close(); server.kill();}
})().catch(error => {console.error(error); process.exitCode = 1;});
