/* Synthetic measured-footage fixtures; no live media/provider or rights proof. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const server = spawn('python3', ['-m', 'http.server', '8779', '--bind', '127.0.0.1', '--directory', path.resolve(__dirname, '../src/katcha/web')], {stdio: 'ignore'});
(async () => {
    const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox']});
    try {
        const page = await browser.newPage({viewport: {width: 1280, height: 900}});
        const errors = [], calls = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*.js', route => route.request().url().endsWith('/editorial-scene-index.js') ? route.continue() : route.fulfill({body: '', contentType: 'application/javascript'}));
        let index = null, loseStart = true, failSearch = false, failRead = false, lateRead;
        const completed = {status: 'completed', receipt: {permission_evidence_refs: ['upload:fixture'], dispatch: 'running'}, evidence: {timebase_numerator: 1, timebase_denominator: 10, source_origin_tick: 50, duration_ticks: 30, shots: [{start_tick: 0, end_tick: 30, keyframe_tick: 10, visual_tags: ['red', 'bright']}], coverage: 'all_decoded_frames_visual_change_sparse_keyframes'}};
        await page.route('**/v1/**', async route => {
            const request = route.request(), url = new URL(request.url());
            const body = request.method() === 'POST' ? request.postDataJSON() : null;
            calls.push({path: url.pathname, method: request.method(), body, auth: request.headers().authorization});
            const send = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
            if (url.pathname.endsWith('/shot-search')) {
                if (failSearch) return send({detail: 'Fixture search disconnected'}, 503);
                return send(body.query === 'red bright' ? [{source_in_seconds: 0, source_out_seconds: 3, keyframe_seconds: 1, visual_tags: ['red', 'bright']}] : []);
            }
            if (url.pathname.endsWith('/cancel')) { index = {...index, status: 'failed', error_category: 'cancelled_by_operator'}; return send(index); }
            if (url.pathname.endsWith('/reconcile')) { assert.equal(body.confirmed_worker_stopped, true); index = {...index, status: 'failed'}; return send(index); }
            if (request.method() === 'POST') {
                index = {status: 'indexing', evidence: {}, receipt: {dispatch: 'queued', permission_evidence_refs: ['upload:fixture']}};
                if (loseStart) { loseStart = false; return send({detail: 'Fixture response lost'}, 503); }
                return send(index, 202);
            }
            if (lateRead) { const wait = lateRead; lateRead = null; await wait; return send(completed); }
            if (failRead) return send({detail: 'Fixture evidence disconnected'}, 503);
            return index ? send(index) : send({detail: 'No measured index for the current media bytes and policy'}, 404);
        });
        for (let attempt = 0; attempt < 20; attempt++) {
            try { await page.goto('http://127.0.0.1:8779/editing.html'); break; }
            catch (error) { if (attempt === 19) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
        }
        await page.evaluate(() => {
            const panel = document.getElementById('editorial-media-preparation');
            for (let current = panel; current; current = current.parentElement) current.hidden = false;
            window.KatchaSceneIndex.init(async (url, options = {}) => {
                const response = await fetch(url, {...options, headers: {'Authorization': 'Bearer fixture-token', 'Content-Type': 'application/json'}});
                const value = await response.json();
                if (!response.ok) throw new Error(value.detail);
                return value;
            });
            window.footageItems = [{id: 'owned-video', kind: 'video', clip_id: 'owned', status: 'acquired', rights_status: 'not_assessed', source_metadata: {title: 'Owned fixture <unsafe>'}}, {id: 'link', kind: 'video', status: 'reference_only'}, {id: 'denied', kind: 'video', clip_id: 'denied', status: 'ready', rights_status: 'revoked'}];
            window.KatchaSceneIndex.context({channel: 'one', project_id: 'project-one', revision: 1}, window.footageItems);
        });
        const status = page.locator('#editorial-footage-status');
        await status.filter({hasText: 'no measured index'}).waitFor();
        assert.equal(await page.locator('#editorial-footage-item option').count(), 1);
        assert.equal(await page.locator('#editorial-footage-item option').textContent(), 'Owned fixture <unsafe>');
        await page.locator('#editorial-footage-query').fill('red bright');
        await page.getByRole('button', {name: 'Index footage', exact: true}).click();
        await status.filter({hasText: 'Refresh before retrying'}).waitFor();
        assert.equal(await page.locator('#editorial-footage-query').inputValue(), 'red bright');
        await page.getByRole('button', {name: 'Refresh footage evidence'}).click();
        await status.filter({hasText: 'waiting for the media worker'}).waitFor();
        assert.equal(await page.locator('#editorial-footage-start').isDisabled(), true);
        await page.getByRole('button', {name: 'Cancel indexing'}).click();
        await status.filter({hasText: 'Indexing cancelled'}).waitFor();
        await page.getByRole('button', {name: 'Retry footage indexing'}).click();
        await status.filter({hasText: 'waiting for the media worker'}).waitFor();
        assert.equal(calls.filter(call => call.method === 'POST' && call.path.endsWith('/scene-index'))[1].body.retry_failed, true);
        // Returning to the same saved project restores progress without an indexing POST.
        const starts = calls.filter(call => call.method === 'POST' && call.path.endsWith('/scene-index')).length;
        await page.evaluate(() => { window.KatchaSceneIndex.context(null, []); window.KatchaSceneIndex.context({channel: 'one', project_id: 'project-one', revision: 1}, window.footageItems); });
        await status.filter({hasText: 'waiting for the media worker'}).waitFor();
        assert.equal(calls.filter(call => call.method === 'POST' && call.path.endsWith('/scene-index')).length, starts);
        index = completed;
        await page.getByRole('button', {name: 'Refresh footage evidence'}).click();
        await status.filter({hasText: 'Measured intervals are ready'}).waitFor();
        assert.match(await page.locator('#editorial-footage-evidence').textContent(), /Original source timestamps 5.000–8.000/);
        assert.match(await page.locator('#editorial-footage-evidence').textContent(), /upload:fixture/);
        await page.locator('#editorial-footage-query').fill('red bright');
        await page.getByRole('button', {name: 'Search observed tags'}).click();
        await status.filter({hasText: '1 measured intervals match'}).waitFor();
        assert.match(await page.locator('#editorial-footage-results').textContent(), /0.000–3.000/);
        failSearch = true;
        await page.getByRole('button', {name: 'Search observed tags'}).click();
        await status.filter({hasText: 'Visual search unavailable'}).waitFor();
        assert.equal(await page.locator('#editorial-footage-query').inputValue(), 'red bright');
        assert.equal(await page.locator('#editorial-footage-results').textContent(), '');
        failSearch = false;
        await page.locator('#editorial-footage-query').fill('person dancing');
        await page.getByRole('button', {name: 'Search observed tags'}).click();
        await status.filter({hasText: 'semantic descriptions are unsupported'}).waitFor();
        index = {status: 'indexing', receipt: {dispatch: 'synchronous'}, evidence: {}};
        await page.getByRole('button', {name: 'Refresh footage evidence'}).click();
        await page.locator('#editorial-footage-recovery').waitFor({state: 'visible'});
        assert.equal(await page.locator('#editorial-footage-reconcile').isDisabled(), true);
        await page.locator('#editorial-footage-stopped').check();
        await page.getByRole('button', {name: 'Record interrupted indexing'}).click();
        await status.filter({hasText: 'stopped or failed'}).waitFor();
        failRead = true;
        await page.getByRole('button', {name: 'Refresh footage evidence'}).click();
        await status.filter({hasText: 'Footage evidence unavailable'}).waitFor();
        assert.equal(await page.locator('#editorial-footage-query').inputValue(), 'person dancing');
        failRead = false;
        let release;
        lateRead = new Promise(resolve => { release = resolve; });
        await page.getByRole('button', {name: 'Refresh footage evidence'}).click();
        await status.filter({hasText: 'Loading measured'}).waitFor();
        await page.evaluate(() => window.KatchaSceneIndex.context({channel: 'two', project_id: 'project-two', revision: 1}, []));
        release();
        await status.filter({hasText: 'No acquired video is eligible'}).waitFor();
        assert.equal(await page.locator('#editorial-footage-evidence').textContent(), '');
        assert.equal(await page.locator('#editorial-footage-query').inputValue(), '');
        assert(calls.every(call => call.auth === 'Bearer fixture-token'));
        await page.setViewportSize({width: 390, height: 844});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
        await page.locator('#editorial-footage-query').focus();
        assert.equal(await page.evaluate(() => document.activeElement.id), 'editorial-footage-query');
        assert.deepEqual(errors, []);
        console.log('Scene index UI: queued recovery, cancellation, search errors, sparse evidence, channel switch, keyboard and mobile passed (synthetic fixtures).');
    } finally { await browser.close(); server.kill(); }
})().catch(error => { server.kill(); console.error(error); process.exit(1); });
