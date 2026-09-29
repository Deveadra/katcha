const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const {mkdirSync,mkdtempSync,cpSync,rmSync} = require('node:fs');
const {tmpdir}=require('node:os');
const {join,resolve}=require('node:path');
const assert = require('node:assert/strict');
(async () => {
    const root=mkdtempSync(join(tmpdir(),'katcha-launcher-test-'));
    cpSync(resolve('../launcher'),join(root,'launcher'),{recursive:true});
    mkdirSync(join(root,'src','katcha'),{recursive:true});
    cpSync(resolve('../src/katcha/web'),join(root,'src','katcha','web'),{recursive:true});
    cpSync(resolve('../.env.example'),join(root,'.env.example'));
    const server = spawn('python3', ['launcher/runtime.py', '--no-start', '--no-browser'], {cwd: root, stdio: 'inherit'});
    let browser;
    try {
        for (let i=0;i<50;i++) {
            try { if ((await fetch('http://localhost:8765/runtime/status')).ok) break; } catch {}
            await new Promise(r=>setTimeout(r,100));
        }
        browser=await chromium.launch({headless:true});
        const page=await browser.newPage({viewport:{width:1440,height:1100}});
        const errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.goto('http://localhost:8765');
        await page.waitForFunction(()=>document.getElementById('phase').textContent==='IDLE');
        assert.equal(await page.locator('#open').getAttribute('aria-disabled'),'false');
        const workspace=await browser.newPage({viewport:{width:1440,height:1100}});
        await workspace.goto('http://localhost:8765/home');
        await workspace.getByRole('heading',{name:/What needs you now/}).waitFor();
        await workspace.waitForFunction(()=>document.getElementById('connection-state').textContent==='WARMING');
        assert.match(await workspace.locator('#status').textContent(),/Start services/);
        await workspace.close();
        await page.locator('[name=KATCHA_OPENAI_API_KEY]').fill('test-secret-never-display');
        await page.getByRole('button',{name:'Save settings'}).click();
        await page.waitForFunction(()=>document.getElementById('notice').textContent.startsWith('Saved.'));
        const snapshot=await (await fetch('http://localhost:8765/runtime/status')).text();
        assert(!snapshot.includes('test-secret-never-display'));
        assert.equal(await page.locator('[name=KATCHA_OPENAI_API_KEY]').inputValue(),'');
        const denied=await fetch('http://localhost:8765/runtime/start',{method:'POST',headers:{Origin:'https://example.com','Content-Type':'application/json'},body:'{}'});
        assert.equal(denied.status,403);
        const exported=await (await fetch('http://localhost:8765/runtime/diagnostics')).text();
        assert(exported.includes('katcha.diagnostic.v1'));
        assert(!exported.includes('test-secret-never-display'));
        mkdirSync('test-results',{recursive:true});
        await page.screenshot({path:'test-results/launcher-desktop.png',fullPage:true});
        await page.setViewportSize({width:390,height:844});
        assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        await page.screenshot({path:'test-results/launcher-mobile.png',fullPage:true});
        assert.deepEqual(errors,[]);
    } finally {
        if(browser)await browser.close();
        server.kill('SIGTERM');
        await new Promise(resolve=>server.once('exit',resolve));
        rmSync(root,{recursive:true,force:true});
    }
})().catch(e=>{console.error(e);process.exitCode=1});
