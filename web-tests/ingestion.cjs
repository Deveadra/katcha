const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const server = spawn('python3', ['-m', 'http.server', '8767', '--bind', '127.0.0.1', '--directory', path.resolve(__dirname, '../src/katcha/web')], {stdio: 'ignore'});
const catalog = [
    {key: 'operator_feed', label: 'Operator Feed', supports_imports: true},
    {key: 'web_scout', label: 'Autonomous Web Scout'},
    {key: 'youtube', label: 'YouTube Search'},
    {key: 'reddit', label: 'Reddit Search'},
    {key: 'rss_atom', label: 'RSS / Atom'},
    {key: 'future', label: 'Future connector'},
].map(a => ({version: 'v1', description: 'Installed connection.', supported_platforms: [], ...a}));
(async () => {
    let browser;
    try {
        for (let i = 0; i < 50; i++) {
            try { await fetch('http://127.0.0.1:8767'); break; }
            catch { await new Promise(r => setTimeout(r, 100)); }
        }
        browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH || undefined, args: process.env.CHROMIUM_PATH ? ['--no-sandbox'] : []});
        const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
        const sources = [], runs = [], requests = [], errors = [];
        let channelMode = 'normal', executeFails = false, historyFails = false, loseSaveResponse = false;
        page.on('pageerror', e => errors.push(e.message));
        await page.route('**/v1/**', route => {
            const req = route.request(), url = new URL(req.url());
            const body = req.method() === 'POST' ? req.postDataJSON() : null;
            requests.push({path: url.pathname, body, auth: req.headers().authorization});
            const reply = (data, status = 200) => route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
            if (req.headers().authorization !== 'Bearer test-token') return reply({detail: 'Unauthorized'}, 401);
            if (url.pathname.endsWith('/promote')) return reply({status: 'queued'});
            if (url.pathname.endsWith('/adapters')) return reply(catalog);
            if (url.pathname === '/v1/channels') return channelMode === 'error' ? reply({}, 503) : reply(channelMode === 'empty' ? [] : [{id: 'channel-one', status: 'active', profile_metadata: {channel_title: 'RankSnaxx'}}]);
            if (url.pathname === '/v1/discovery/sources') {
                if (!body) return reply(sources);
                const existing = sources.find(s => s.source_key === body.source_key);
                if (existing) {
                    if (body.create_only) return reply({detail: "Duplicate key"}, 409);
                    Object.assign(existing, body); return reply(existing);
                }
                const row = {...body, id: `source-${sources.length + 1}`, enabled: true}; sources.push(row);
                if (loseSaveResponse) { loseSaveResponse = false; return route.abort(); }
                return reply(row);
            }
            const sourceId = url.pathname.match(/sources\/([^/]+)/)?.[1];
            if (url.pathname.endsWith('/results')) {
                const runId = url.pathname.match(/runs\/([^/]+)\/results/)?.[1];
                const run = runs.find(r => r.id === runId && r.sourceId === sourceId);
                return run ? reply({total: 1, candidates: [{id: 'candidate-one', source_url: 'https://example.com/post', title: '<New creator>', creator: 'Alice'}]}) : reply({}, 404);
            }
            if (url.pathname.endsWith('/imports') || (url.pathname.endsWith('/runs') && body)) {
                const key = body.batch_key || body.idempotency_key;
                let run = runs.find(r => r.run_key === key);
                if (!run) { run = {id: `run-${runs.length + 1}`, sourceId, run_key: key, status: 'queued', created_at: '2026-09-28T12:00:00Z'}; runs.push(run); }
                return reply(url.pathname.endsWith('/imports') ? {discovery_run: run} : run);
            }
            if (url.pathname.endsWith('/execute')) {
                if (executeFails) return reply({}, 503);
                runs.find(r => url.pathname.includes(`/${r.id}/`)).status = 'running';
                return reply({status: 'queued'});
            }
            if (url.pathname.endsWith('/runs')) return historyFails ? reply({}, 503) : reply(runs.filter(r => r.sourceId === sourceId));
            return reply({detail: 'unexpected request'}, 404);
        });
        const choose = async method => {
            await page.locator('[data-source-tab="add"]').click();
            await page.locator('[data-source-view="add"]').waitFor({state: 'visible'});
            await page.locator(`[data-method="${method}"]`).click();
            assert.equal(await page.evaluate(() => document.activeElement.id), 'name');
            await page.locator('#name').fill(`${method} collection`);
        };
        const save = async () => {
            await page.locator('#next').click();
            await page.locator('#save').click();
            await page.waitForFunction(() => !document.querySelector('#step-1').hidden);
        };
        await page.goto('http://127.0.0.1:8767/ingestion.html');
        await page.locator('.workspace-menu').waitFor();
        await page.locator('.workspace-menu > summary').click();
        assert.match(await page.locator('.workspace-menu-popover').innerText(), /Clips/);
        assert.match(await page.locator('.workspace-menu-popover').innerText(), /Clip Studio/);
        assert.match(await page.locator('.workspace-menu-popover').innerText(), /Sources/);
        await page.locator('.workspace-menu > summary').click();
        await page.locator('#connection-panel').waitFor({state: 'visible'});
        assert(await page.locator('[data-method]').count() === 0);
        await page.locator('#token').fill('test-token');
        await page.locator('#connect button').click();
        await page.waitForFunction(() => !document.querySelector('#workspace').disabled);
        assert.equal(await page.locator('#connection-panel').isVisible(), false);
        assert.equal(await page.locator('[data-source-tab]').count(), 2);
        assert.equal(await page.locator('[data-source-tab="add"]').getAttribute('aria-selected'), 'true');
        assert.equal(await page.locator('[data-source-view="add"]').isHidden(), false);
        assert.equal(await page.locator('[data-source-view="library"]').isHidden(), true);
        assert.equal(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight + 2), true);
        assert.equal(await page.locator('#channel option').nth(1).textContent(), 'RankSnaxx');
        assert.equal(await page.locator('#custom-query').isVisible(), false);
        assert.equal(await page.locator('#key').count(), 0);
        assert.equal(await page.locator('#batch').count(), 0);
        await choose('links');
        await page.locator('#channel').selectOption('channel-one');
        assert.equal(await page.locator('#usage').isVisible(), false);
        await page.locator('#next').click();
        assert.match(await page.locator('#review').textContent(), /RankSnaxx/);
        // Lost save responses must recover the same source on retry.
        loseSaveResponse = true;
        await page.locator('#save').click();
        await page.waitForFunction(() => document.querySelector('#setup-error').textContent.includes('could not be reached'));
        await page.locator('#save').click();
        await page.waitForFunction(() => !document.querySelector('#step-1').hidden);
        assert.equal(sources.length, 1);
        assert.equal(sources[0].channel_profile_id, 'channel-one');
        assert.equal(sources[0].usage_mode, 'candidate_review');
        assert.deepEqual(sources[0].query_template, {items: [], urls: []});
        assert.match(sources[0].source_key, /^source-/);
        assert.equal(await page.locator('[data-source-tab="library"]').getAttribute('aria-selected'), 'true');
        assert.equal(await page.locator('[data-source-view="library"]').isHidden(), false);
        await page.locator('#urls').fill('not a link');
        await page.locator('#import button').click();
        assert.equal(requests.filter(r => r.path.endsWith('/imports')).length, 0);
        await page.locator('#urls').fill('https://example.com/clip\nhttps://example.com/clip');
        executeFails = true;
        await page.locator('#import button').click();
        await page.waitForFunction(() => document.querySelector('#message').textContent.includes('request is saved'));
        assert.equal(runs.length, 1);
        await page.locator('#import button').click();
        await page.waitForFunction(() => document.querySelector('#import button').disabled === false);
        assert.equal(runs.length, 1);
        const imports = requests.filter(r => r.path.endsWith('/imports'));
        assert.equal(imports[0].body.batch_key, imports[1].body.batch_key);
        assert.deepEqual(imports[0].body.urls, ['https://example.com/clip']);
        executeFails = false;
        await page.locator('[data-execute]').click();
        await page.waitForFunction(() => document.querySelector('#history').textContent.includes('Finding content'));
        historyFails = true;
        await page.locator('#history-refresh').click();
        await page.waitForFunction(() => document.querySelector('#history').textContent.includes('could not be loaded'));
        assert.match(await page.locator('#history').textContent(), /could not be loaded/);
        historyFails = false;
        await page.locator('#history-refresh').click();
        await choose('youtube');
        await page.locator('#search').fill('new Xbox games');
        await save();
        assert.equal(sources[1].adapter_key, 'youtube');
        assert.equal(sources[1].query_template.q, 'new Xbox games');
        await page.locator('#run').click();
        await page.waitForFunction(() => document.querySelector('#history').textContent.includes('Finding content'));
        assert.equal(runs.length, 2);
        await choose('scout');
        await page.locator('#search').fill('funny gaming clips');
        await page.locator('#scout-platforms').selectOption('social');
        await page.locator('#channel').selectOption('');
        assert.equal(await page.locator('#channel option').first().textContent(), 'Shared collection (unassigned)');
        await page.locator('#next').click();
        assert.match(await page.locator('#review').innerText(), /Shared collection/);
        assert.match(await page.locator('#save-explanation').textContent(), /provider charges/i);
        await page.locator('#save').click();
        await page.waitForFunction(() => !document.querySelector('#step-1').hidden);
        assert.equal(sources[2].adapter_key, 'web_scout');
        assert.equal(sources[2].platform, 'web');
        assert.equal(sources[2].channel_profile_id, null);
        assert.deepEqual(sources[2].query_template.platforms, ['tiktok', 'instagram', 'x', 'bluesky']);
        assert.match(await page.locator('#operation-help').textContent(), /checks this source every/);
        await page.locator('#run').click();
        await page.waitForFunction(() => document.querySelector('#history').textContent.includes('Finding content'));
        runs.at(-1).status = 'completed';
        await page.locator('#history-refresh').click();
        await page.locator('[data-results]').click();
        await page.waitForFunction(() => document.querySelector('.run-results').textContent.includes('item found'));
        assert.match(await page.locator('.run-results').textContent(), /<New creator>/);
        assert.equal(await page.locator('.run-results script').count(), 0);
        await page.locator('[data-add-clip]').click();
        await page.waitForFunction(() => document.querySelector('[data-add-clip]').disabled);
        assert.equal(requests.find(r => r.path.endsWith('/promote')).body.for_review, true);
        await page.locator('#pause-source').click();
        await page.waitForFunction(() => document.querySelector('#pause-source').textContent === 'Resume source');
        assert.equal(sources[2].enabled, false);
        await page.locator('#pause-source').click();
        await page.waitForFunction(() => document.querySelector('#pause-source').textContent === 'Pause source');
        assert.equal(sources[2].enabled, true);

        await choose('reddit');
        await page.locator('#search').fill('indie games');
        await page.locator('#community').fill('r/gaming');
        await save();
        assert.equal(sources[3].query_template.subreddit, 'gaming');
        await choose('feed');
        await page.locator('#feed').fill('bad-feed');
        await page.locator('#next').click();
        assert.match(await page.locator('#setup-error').textContent(), /full http/);
        await page.locator('#feed').fill('https://example.com/feed.xml');
        await save();
        assert.equal(sources[4].adapter_key, 'rss_atom');
        assert.equal(sources[4].query_template.feed_url, 'https://example.com/feed.xml');
        // Empty channels and unavailable channels are distinct, with no silent reassignment.
        channelMode = 'error';
        await choose('links');
        await page.locator('#retry-channels').click();
        await page.waitForFunction(() => !document.querySelector('#retry-channels').disabled);
        await page.locator('#next').click();
        assert.match(await page.locator('#setup-error').textContent(), /Load your channels/);
        assert.equal(await page.locator('#channel-area').isVisible(), false);
        channelMode = 'empty';
        await page.locator('#retry-channels').click();
        await page.waitForFunction(() => !document.querySelector('#retry-channels').disabled);
        assert.match(await page.locator('#channel-help').textContent(), /unassigned shared collection/i);
        assert.equal(await page.locator('#channel-area').isVisible(), true);
        assert.equal(await page.locator('#channel option').first().textContent(), 'Shared collection (unassigned)');
        await save();
        assert.equal(sources[5].channel_profile_id, null);
        // New connectors remain available behind an explicitly advanced path.
        await page.locator('[data-source-tab="add"]').click();
        await page.locator('[data-source-view="add"]').waitFor({state: 'visible'});
        await page.locator('#step-1 summary').click();
        await page.locator('#custom-adapter').selectOption('future@v1');
        await page.locator('#choose-custom').click();
        await page.locator('#name').fill('Custom collection');
        await page.locator('#custom-query').fill('{"topic":"test"}');
        await save();
        assert.equal(sources[6].adapter_key, 'future');
        assert.deepEqual(sources[6].query_template, {topic: 'test'});
        await page.locator('#source').selectOption('source-1');
        await page.waitForFunction(() => document.querySelector('#history').textContent.includes('Finding content'));
        fs.mkdirSync(path.join(__dirname, 'test-results'), {recursive: true});
        await page.locator('#step-1 details').evaluate(el => { el.open = false; });
        await page.evaluate(() => scrollTo(0, 0));
        await page.screenshot({path: path.join(__dirname, 'test-results/ingestion-desktop.png'), fullPage: true});
        await choose('youtube');
        await page.screenshot({path: path.join(__dirname, 'test-results/ingestion-details.png'), fullPage: true});
        await page.setViewportSize({width: 390, height: 844});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({path: path.join(__dirname, 'test-results/ingestion-mobile.png'), fullPage: true});
        await page.setViewportSize({width: 1366, height: 768});
        await page.locator("#back").click();
        await choose('youtube_channel');
        await page.locator('#youtube-channel').fill('https://www.youtube.com/@creator');
        await page.locator('.advanced').filter({has: page.locator('#usage')}).locator('summary').click();
        assert.equal(await page.locator('#usage').inputValue(), 'discovery_only');
        assert.equal(await page.locator('#next').isVisible(), true);
        await page.locator('#next').click();
        assert.match(await page.locator('#review').innerText(), /@creator/);
        await page.locator('#save').click();
        await page.waitForFunction(() => !document.querySelector('#step-1').hidden);
        assert.equal(sources.at(-1).platform, 'youtube');
        assert.equal(sources.at(-1).query_template.channel_reference, 'https://www.youtube.com/@creator');
        assert.equal(sources.at(-1).usage_mode, 'discovery_only');
        assert.equal(await page.locator('#run').innerText(), 'Check channel videos');
        assert.match(await page.locator('#source-info').innerText(), /Watch a YouTube channel/);

        const watchedSource = sources.at(-1);
        runs.push({
            id: 'run-youtube-failed',
            sourceId: watchedSource.id,
            run_key: 'failed-youtube-check',
            status: 'failed',
            created_at: '2026-09-30T12:00:00Z',
            error: 'YouTube search request failed (status 403)',
        });
        await page.locator('#history-refresh').click();
        const diagnostics = page.locator('#history details.run-diagnostics').first();
        await diagnostics.locator('summary').click();
        assert.equal(await diagnostics.evaluate(element => element.open), true);
        assert.match(await diagnostics.locator('pre').innerText(), /run-youtube-failed/);
        assert.match(await diagnostics.locator('pre').innerText(), /status 403/);
        assert.deepEqual(errors, []);
        console.log('PASS: Aerith Sources/Add source workspaces, guided source choices, shared all-channel web scouting, human channel names, empty/error channels, automatic IDs, lost-response recovery, activity retries, custom connectors and responsive layout');
    } finally { if (browser) await browser.close(); server.kill(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
