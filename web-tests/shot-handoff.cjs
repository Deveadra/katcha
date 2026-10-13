/* Synthetic operator-selected shot fixtures; no live rights or rendering claim. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const server = spawn('python3', ['-m', 'http.server', '8780', '--bind', '127.0.0.1', '--directory', path.resolve(__dirname, '../src/katcha/web')], {stdio: 'ignore'});
(async () => {
    const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox']});
    try {
        const page = await browser.newPage({viewport: {width: 1280, height: 900}});
        const errors = [], calls = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*.js', route => route.request().url().endsWith('/editorial-shot-handoff.js') ? route.continue() : route.fulfill({body: '', contentType: 'application/javascript'}));
        const binding = {binding_id: 'observed', requirement_id: 'requirement', source_in_seconds: 1, source_out_seconds: 2, explanation: 'A measured color match <unsafe> does not establish an exact scene.', visual_tags: ['red'], confidence_limits: ['unverified_semantics', 'sparse_keyframes'], production_eligible: true};
        const choiceId = '11111111-1111-4111-8111-111111111111';
        let saved = {package_id: 'package', input_revision: 2, current_status: 'current', operator_choices: [], item_gaps: {'unindexed': 'current_measured_index_missing'}, requirements: [{requirement_id: 'requirement', proposals: [binding]}, {requirement_id: 'missing', proposals: [], unresolved_gaps: ['unverified_semantics']} ]};
        let workspace = {version: 1}, failMatch = true, failChoice = true, loseApply = true, confirmedApply = null, handoffs = 0, late;
        await page.route('**/v1/**', async route => {
            const request = route.request(), url = new URL(request.url()), body = request.method() === 'POST' ? request.postDataJSON() : null;
            calls.push({path: url.pathname, method: request.method(), body, auth: request.headers().authorization});
            const send = (value, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(value)});
            if (url.pathname.endsWith('/storyboard/approved-shot')) {
                if (!body.replace_existing) return send({detail: 'This beat has a manual visual. Confirm replacement or choose an unassigned beat'}, 409);
                if (confirmedApply) assert.deepEqual(body, confirmedApply, 'An uncertain handoff must reuse its saved expected version and identity');
                else { confirmedApply = body; workspace = {version: 2}; handoffs++; }
                if (loseApply) { loseApply = false; return send({detail: 'Fixture response lost'}, 503); }
                return send(workspace, 201);
            }
            if (url.pathname.endsWith('/storyboard')) return send(workspace);
            if (url.pathname.endsWith('/shot-proposals/choices')) {
                if (failChoice) { failChoice = false; return send({detail: 'Fixture choice disconnected'}, 503); }
                saved.operator_choices = [{choice_id: choiceId, version: 1, locked: true, note: body.note, current_status: 'current', binding, state: 'operator_selected'}];
                return send(saved.operator_choices[0]);
            }
            if (request.method() === 'POST') {
                if (failMatch) { failMatch = false; return send({detail: 'Fixture proposal disconnected'}, 503); }
                return send(saved);
            }
            if (late) { const wait = late; late = null; await wait; }
            return send(saved);
        });
        for (let attempt = 0; attempt < 20; attempt++) {
            try { await page.goto('http://127.0.0.1:8780/editing.html'); break; }
            catch (error) { if (attempt === 19) throw error; await new Promise(resolve => setTimeout(resolve, 100)); }
        }
        await page.evaluate(() => {
            for (let current = document.getElementById('editorial-media-preparation'); current; current = current.parentElement) current.hidden = false;
            window.beforeApplyCalls = 0; window.reloadCalls = 0;
            window.KatchaShotHandoff.init(async (url, options = {}) => {
                const response = await fetch(url, {...options, headers: {'Authorization': 'Bearer fixture-token', 'Content-Type': 'application/json'}});
                const value = await response.json(); if (!response.ok) throw new Error(value.detail); return value;
            }, (name, payload) => `${name}:${JSON.stringify(payload)}`, async () => { window.beforeApplyCalls++; }, async () => { window.reloadCalls++; });
            window.scope = {channel: 'one', project_id: 'project-one', revision: 2, beats: [{id: 'beat', narration: 'Look.'}], preparation: {run_id: 'preparation'}, items: [{id: 'owned', kind: 'video', clip_id: 'clip', status: 'acquired', source_metadata: {title: 'Owned <fixture>'}}, {id: 'denied', kind: 'video', clip_id: 'blocked', status: 'ready', rights_status: 'revoked'}]};
            window.KatchaShotHandoff.context(window.scope);
        });
        const status = page.locator('#editorial-shot-status');
        assert.equal(await page.locator('#editorial-shot-items input').count(), 1);
        await page.getByLabel('Owned <fixture>').check();
        await page.getByRole('button', {name: 'Propose measured shots'}).click();
        await status.filter({hasText: 'Footage choices and notes are retained'}).waitFor();
        assert.equal(await page.getByLabel('Owned <fixture>').isChecked(), true);
        await page.getByRole('button', {name: 'Propose measured shots'}).click();
        await status.filter({hasText: 'Measured proposals saved'}).waitFor();
        assert.match(await page.locator('#editorial-shot-proposals').textContent(), /unverified semantics/);
        assert.match(await page.locator('#editorial-shot-proposals').textContent(), /exact scene/);
        assert.equal(await page.locator('#editorial-shot-proposals script').count(), 0);
        await page.getByLabel('Selection note').fill('Keep my manual reasoning <unsafe>');
        await page.getByRole('button', {name: 'Save shot selection'}).click();
        await status.filter({hasText: 'Selection not confirmed'}).waitFor();
        assert.equal(await page.getByLabel('Selection note').inputValue(), 'Keep my manual reasoning <unsafe>');
        await page.getByRole('button', {name: 'Save shot selection'}).click();
        await page.getByRole('button', {name: 'Add selected shot to Storyboard'}).waitFor();
        await page.getByRole('button', {name: 'Add selected shot to Storyboard'}).click();
        await status.filter({hasText: 'manual visual'}).waitFor();
        assert.equal(workspace.version, 1);
        await page.locator('#editorial-shot-replace').check();
        await page.getByRole('button', {name: 'Add selected shot to Storyboard'}).click();
        await status.filter({hasText: 'response lost'}).waitFor();
        await page.getByRole('button', {name: 'Add selected shot to Storyboard'}).click();
        await status.filter({hasText: 'Selected measured shot saved to Storyboard'}).waitFor();
        assert.equal(handoffs, 1);
        assert.equal(await page.evaluate(() => window.reloadCalls), 1);
        assert.equal(await page.getByLabel('Selection note').inputValue(), 'Keep my manual reasoning <unsafe>');
        saved = {...saved, current_status: 'stale_source_or_permission', requirements: [{requirement_id: 'requirement', proposals: [{...binding, production_eligible: false}]}], operator_choices: []};
        await page.getByRole('button', {name: 'Refresh saved shot proposals'}).click();
        await status.filter({hasText: 'Saved proposals are stale'}).waitFor();
        assert.equal(await page.getByRole('button', {name: 'Save shot selection'}).isDisabled(), true);
        let release; late = new Promise(resolve => { release = resolve; });
        await page.getByRole('button', {name: 'Refresh saved shot proposals'}).click();
        await status.filter({hasText: 'Checking saved shot proposals'}).waitFor();
        await page.evaluate(() => window.KatchaShotHandoff.context({channel: 'two', project_id: 'another', revision: 1, items: [], beats: []}));
        release();
        await status.filter({hasText: 'Prepare media for the saved script'}).waitFor();
        assert.equal(await page.locator('#editorial-shot-proposals').textContent(), '');
        assert(calls.every(call => call.auth === 'Bearer fixture-token'));
        await page.setViewportSize({width: 390, height: 844});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.locator('#editorial-shot-freeze').focus();
        assert.equal(await page.evaluate(() => document.activeElement.id), 'editorial-shot-freeze');
        assert.deepEqual(errors, []);
        console.log('Shot handoff UI: review limits, note recovery, manual protection, uncertain replay, stale rights, channel isolation and 390px keyboard checks passed (fixtures).');
    } finally { await browser.close(); server.kill(); }
})().catch(error => { server.kill(); console.error(error); process.exit(1); });
