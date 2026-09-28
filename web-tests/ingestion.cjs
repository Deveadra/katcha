const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const server = spawn('python3', ['-m','http.server','8767','--bind','127.0.0.1','--directory',path.resolve(__dirname,'../src/katcha/web')], {stdio:'ignore'});
(async () => {
    let browser;
    try {
        for (let i=0;i<50;i++) { try { await fetch('http://127.0.0.1:8767'); break; } catch { await new Promise(r=>setTimeout(r,100)); } }
        browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH || undefined, args:process.env.CHROMIUM_PATH ? ['--no-sandbox'] : []});
        const page = await browser.newPage({viewport:{width:1440,height:1000}});
        const sources = [], runs = [], requests = [], errors = [];
        page.on('pageerror', e=>errors.push(e.message));
        await page.route('**/v1/**', route => {
            const req = route.request(), url = new URL(req.url());
            const body = req.method() === 'POST' ? req.postDataJSON() : null;
            requests.push({path:url.pathname,body,auth:req.headers().authorization});
            const reply = (data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
            if (req.headers().authorization !== 'Bearer test-token') return reply({detail:'Unauthorized'},401);
            if (url.pathname.endsWith('/adapters')) return reply([{key:'operator_feed',version:'v1',label:'Operator Feed',description:'Import clips from any site.',required_credentials:[],supported_platforms:['tiktok'],query_fields:['feed_key','items','urls'],sample_query:{feed_key:'drops',items:[{source_url:'https://example.com/sample'}]},supports_imports:true}]);
            if (url.pathname === '/v1/channels') return reply([{id:'channel-one',profile_metadata:{name:'RankSnaxx'}}]);
            if (url.pathname === '/v1/discovery/sources') {
                if (body) { const row={...body,id:'source-one',enabled:true}; sources.push(row); return reply(row); }
                return reply(sources);
            }
            if (url.pathname.endsWith('/imports')) { const run={id:'run-import',run_key:body.batch_key,status:'queued',created_at:'2026-09-27T00:00:00Z'}; if (!runs.some(r=>r.id===run.id)) runs.push(run); return reply({discovery_run:run}); }
            if (url.pathname.endsWith('/execute')) { runs.find(r=>r.id === 'run-import').status='running'; return reply({status:'queued'}); }
            if (url.pathname.endsWith('/runs')) {
                if (body) { const row={id:'manual-run',run_key:body.idempotency_key,status:'queued',created_at:'2026-09-27T00:00:00Z'}; runs.push(row); return reply(row); }
                return reply(runs);
            }
            return reply({detail:'unexpected request'},404);
        });
        await page.goto('http://127.0.0.1:8767/ingestion.html');
        await page.locator('.workspace-menu').waitFor();
        await page.locator('.workspace-menu > summary').click();
        assert.match(await page.locator('.workspace-menu-popover').innerText(),/Clip library/);
        await page.locator('.workspace-menu > summary').click();
        await page.locator('#connect button').click();
        await page.waitForFunction(()=>document.querySelector('#message').textContent === 'Unauthorized');
        assert(await page.locator('#key').isDisabled());
        await page.locator('#token').fill('test-token'); await page.locator('#connect button').click();
        await page.waitForFunction(()=>!document.querySelector('#workspace').disabled);
        assert.equal(await page.locator('[data-key="items"]').inputValue(),'[]');
        await page.locator('#key').fill('rank-clips'); await page.locator('#name').fill('RankSnaxx clips');
        await page.locator('#channel').selectOption('channel-one'); await page.locator('#setup button').click();
        await page.waitForFunction(()=>document.querySelector('#message').textContent.startsWith('Source saved'));
        assert.equal(sources[0].channel_profile_id,'channel-one'); assert.deepEqual(sources[0].query_template.items,[]);
        await page.locator('#setup button').click();
        await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('already exists'));
        assert.equal(sources.length,1);
        await page.locator('#batch').fill('batch-one'); await page.locator('#urls').fill('https://example.com/clip\nhttps://example.com/clip2');
        await page.locator('#import button').click();
        await page.waitForFunction(()=>document.querySelector('#history').textContent.includes('batch-one'));
        assert.deepEqual(requests.find(r=>r.path.endsWith('/imports')).body.urls,['https://example.com/clip','https://example.com/clip2']);
        await page.locator('[data-execute]').click();
        await page.waitForFunction(()=>document.querySelector('#history').textContent.includes('running'));
        await page.locator('#run').click();
        await page.waitForFunction(()=>document.querySelectorAll('#history .item').length===2);
        assert(requests.find(r=>r.body?.idempotency_key)?.body.idempotency_key.startsWith('source-ui:'));
        fs.mkdirSync(path.join(__dirname,'test-results'),{recursive:true});
        await page.screenshot({path:path.join(__dirname,'test-results/ingestion-desktop.png'),fullPage:true});
        await page.setViewportSize({width:390,height:844});
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        await page.screenshot({path:path.join(__dirname,'test-results/ingestion-mobile.png'),fullPage:true});
        assert.deepEqual(errors,[]);
        console.log('Ingestion onboarding, auth, imports, execution and responsive layout passed');
    } finally { if(browser) await browser.close(); server.kill(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
