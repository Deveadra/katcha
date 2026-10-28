// Browser fixtures only: no live source, credential, Telegram or acquisition claim.
const { launch } = require('./browser.cjs');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const origin = 'https://app.katcha.stream';
const web = path.resolve(__dirname, '../src/katcha/web');
const channel = '11111111-1111-4111-8111-111111111111';
const job = '22222222-2222-4222-8222-222222222222';
const challengeId = '33333333-3333-4333-8333-333333333333';
const base = `/v1/channels/${channel}/acquisition-jobs/${job}`;
const picture = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';

async function fixture(browser, options = {}) {
    const page = await browser.newPage({viewport: {width: options.mobile ? 390 : 1280, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const data = {status: 'waiting_human', state: 'waiting', code: null, commands: [], records: new Map(),
        failStatus: false, reject: false, losePost: false, expireCommand: false, unknownCommand: false,
        auth: options.auth || 200, retries: 0, cancellations: 0};
    const expires = () => new Date(Date.now() + 30 * 60000).toISOString();
    const snapshot = () => ({available: true, snapshot_id: 'snapshot-1', title: '<img src=x onerror=alert(1)>',
        url: 'https://www.youtube.com/watch?token=do-not-display', text: 'Fixture source verification',
        elements: [{ref: 'e1', name: 'Verification code', tag: 'input', type: 'text'}], tabs: [{index: 0}], active_tab: 0});
    await page.route(origin + '/**', async route => {
        const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
        if (pathname === '/v1/control/access-login') return route.fulfill({json: {enabled: true, login_url: 'https://katcha.cloudflareaccess.com/cdn-cgi/access/login/app.katcha.stream'}});
        if (pathname.startsWith('/v1/')) {
            if (data.auth !== 200) return route.fulfill({status: data.auth, json: {detail: 'fixture denied'}});
            if (pathname === '/v1/control/session') return route.fulfill({json: {actor: 'fixture-operator', scopes: ['*'], channel_access: {all_channels: true}}});
            if (pathname === base) return data.failStatus ? route.abort('failed') : route.fulfill({json: {id: job, channel_profile_id: channel, status: data.status}});
            if (pathname === base + '/human') return route.fulfill({json: {id: challengeId, generation: 1, job_id: job, channel_profile_id: channel, state: data.state, code: data.code, expires_at: expires(), notification_state: 'not_configured'}});
            if (pathname === base + '/human/commands' && req.method() === 'POST') {
                const body = req.postDataJSON();
                data.commands.push(body);
                if (data.reject) { data.reject = false; return route.fulfill({status: 409, json: {detail: 'fixture conflict'}}); }
                const result = body.action.action === 'screenshot' ? {available: true, image: picture} : snapshot();
                data.records.set(body.id, {id: body.id, state: data.expireCommand ? 'expired' : 'completed', result, expires_at: expires()});
                if (data.losePost) { data.losePost = false; return route.abort('failed'); }
                return route.fulfill({json: {id: body.id, state: 'queued', expires_at: expires(), result: null}});
            }
            if (pathname.startsWith(base + '/human/commands/')) {
                const record = data.records.get(pathname.split('/').at(-1));
                if (data.unknownCommand || !record) return route.fulfill({status: 404, json: {detail: 'not saved'}});
                return route.fulfill({json: record});
            }
            if (pathname === base + '/human/retry') { data.retries++; data.state = 'retry_requested'; return route.fulfill({json: {state: data.state}}); }
            if (pathname === base + '/cancel') { data.cancellations++; data.status = 'cancelled'; return route.fulfill({json: {status: data.status}}); }
            return route.fulfill({json: pathname === '/v1/channels' ? [] : {}});
        }
        const filename = path.resolve(web, '.' + pathname);
        if (!filename.startsWith(web + path.sep) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) return route.fulfill({status: 404, body: ''});
        return route.fulfill({body: fs.readFileSync(filename), contentType: ({'.js':'application/javascript','.css':'text/css','.html':'text/html'})[path.extname(filename)] || 'application/octet-stream'});
    });
    await page.goto(origin + '/acquisition-recovery.html' + (options.invalid ? '' : `?channel=${channel}&job=${job}`));
    return {page, data, errors};
}
async function ready(page) {
    await page.waitForFunction(() => !document.getElementById('open-browser').disabled);
}
async function open(page) {
    await ready(page);
    await page.getByRole('button', {name: 'Open verification', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('browser-status').textContent.includes('Browser view updated'));
}
(async () => {
    const browser = await launch();
    try {
        const {page, data, errors} = await fixture(browser, {mobile: true});
        await open(page);
        assert.equal(await page.locator('#browser-title img').count(), 0, 'untrusted provider title stays text');
        assert.equal(await page.locator('#browser-location').textContent(), 'https://www.youtube.com');
        assert.equal(await page.locator('#browser-image').isVisible(), true);
        assert.equal(data.commands[0].action.action, 'open');
        assert.equal(data.commands[0].action.url, undefined, 'source URL is not client selected');
        assert.equal(data.commands[0].challenge_id, challengeId);
        assert.equal(data.commands[1].action.snapshot_id, 'snapshot-1');
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, '390px has no horizontal overflow');
        assert.equal(await page.evaluate(() => getComputedStyle(document.querySelector('.recovery-content')).display), 'grid', 'workspace layout styles loaded');
        await page.locator('#browser-screen').focus();
        await page.keyboard.press('Enter');
        assert.equal(await page.locator('#position-x').evaluate(el => el === document.activeElement), true, 'keyboard opens usable pointer controls');
        await page.locator('#browser-text').fill('keep-on-rejection');
        data.reject = true;
        await page.getByRole('button', {name: 'Type into selected field'}).click();
        await page.waitForFunction(() => document.getElementById('browser-status').dataset.error === 'true');
        assert.equal(await page.locator('#browser-text').inputValue(), 'keep-on-rejection');
        data.failStatus = true;
        await page.getByRole('button', {name: 'Refresh status', exact: true}).click();
        await page.waitForFunction(() => document.getElementById('status').dataset.error === 'true');
        assert.equal(await page.locator('#browser-text').inputValue(), 'keep-on-rejection');
        data.failStatus = false;
        const before = data.commands.length;
        data.losePost = true;
        await page.getByRole('button', {name: 'Type into selected field'}).click();
        await page.waitForFunction(() => document.getElementById('browser-status').textContent.includes('Browser view updated'));
        assert.equal(data.commands.slice(before).filter(c => c.action.action === 'type').length, 1, 'lost response reconciles saved action without replay');
        assert.equal(await page.locator('#browser-text').inputValue(), '', 'submitted credential cleared');
        await page.getByRole('button', {name: 'Check access and continue'}).click();
        await page.waitForFunction(() => document.getElementById('status').textContent.includes('success has not been confirmed'));
        assert.equal(data.retries, 1);
        assert.equal(await page.locator('#retry').isDisabled(), true);
        assert.equal(await page.locator('#browser-image').getAttribute('src'), null);
        data.status = 'awaiting_validation';
        await page.getByRole('button', {name: 'Refresh status', exact: true}).click();
        await page.waitForFunction(() => document.getElementById('status').textContent.includes('validating'));
        data.status = 'completed';
        await page.getByRole('button', {name: 'Refresh status', exact: true}).click();
        await page.waitForFunction(() => document.getElementById('status').textContent.includes('passed validation'));
        assert.deepEqual(errors, []);
        await page.close();

        const second = await fixture(browser);
        await open(second.page);
        second.data.expireCommand = true;
        await second.page.getByRole('button', {name: 'Press Enter', exact: true}).click();
        await second.page.waitForFunction(() => document.getElementById('browser-status').dataset.error === 'true');
        assert.equal(second.data.commands.filter(c => c.action.action === 'press').length, 1);
        second.data.expireCommand = false;
        await second.page.getByRole('button', {name: 'Refresh page view'}).click();
        await second.page.waitForFunction(() => document.getElementById('browser-status').textContent.includes('Browser view updated'));
        second.data.code = 'session_expired';
        await second.page.getByRole('button', {name: 'Refresh status', exact: true}).click();
        await second.page.waitForFunction(() => document.getElementById('browser-status').textContent.includes('expired or restarted'));
        assert.equal(await second.page.locator('#retry').isDisabled(), true);
        await second.page.getByRole('button', {name: 'Cancel download'}).click();
        await second.page.waitForFunction(() => document.getElementById('status').textContent.includes('cancelled'));
        assert.equal(second.data.cancellations, 1);
        assert.deepEqual(second.errors, []);
        await second.page.close();
        for (const auth of [401, 403]) {
            const denied = await fixture(browser, {auth});
            await denied.page.waitForFunction(() => !document.getElementById('access').hidden);
            assert.match(await denied.page.locator('#access-message').textContent(), auth === 401 ? /Sign in/ : /cannot access/);
            assert.equal(denied.data.commands.length, 0);
            await denied.page.close();
        }
        const invalid = await fixture(browser, {invalid: true});
        assert.match(await invalid.page.locator('#status').textContent(), /incomplete/);
        await invalid.page.close();
        console.log('Acquisition recovery: verified flow, safe output, mobile/keyboard, auth, expiry and uncertain response fixtures passed');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
