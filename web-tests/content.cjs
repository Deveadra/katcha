const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const crypto=require('node:crypto');
const server=spawn('python3',['-m','http.server','8772','--bind','127.0.0.1','--directory',path.resolve(__dirname,'../src/katcha/web')],{stdio:'ignore'});
let browser;
(async()=>{
    for(let i=0;i<30;i++){try{await fetch('http://127.0.0.1:8772/content.html');break;}catch{await new Promise(r=>setTimeout(r,100));}}
    browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH||undefined,args:process.env.CHROMIUM_PATH?['--no-sandbox']:[]});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const requests=[];
    let row=null,loseResponse=true;
    const channel='11111111-1111-4111-8111-111111111111';
    const item='22222222-2222-4222-8222-222222222222';
    const production='33333333-3333-4333-8333-333333333333';
    const root='/v1/channels/'+channel+'/content';
    await page.route('**/v1/**',async route=>{
        const req=route.request(),url=new URL(req.url());
        let body;try{body=req.postDataJSON();}catch{}
        requests.push({path:url.pathname,method:req.method(),body});
        let data;
        if(url.pathname==='/v1/control/session')data={principal_name:'fixture',scopes:['*'],channel_profile_ids:['*']};
        else if(url.pathname==='/v1/channels')data=[{id:channel,profile_metadata:{channel_title:'Fixture Channel'},timezone:'America/Chicago'}];
        else if(url.pathname===root&&req.method()==='POST'){
            assert.equal(body.rights_confirmed,true);
            if(!row)row={id:item,title:body.title,input_kind:'file',filename:body.filename,fingerprint:body.fingerprint,phase:'uploading',upload_offset:0,size_bytes:10,chunk_bytes:5,chunks:[],productions:[],publications:[],events:[],request_key:body.request_key};
            else assert.equal(body.request_key,row.request_key);
            data=row;
        }else if(url.pathname===root&&req.method()==='GET')data={items:row?[row]:[],total:row?1:0,offset:0,limit:25};
        else if(url.pathname===root+'/'+item+'/chunks'){
            const bytes=req.postDataBuffer();
            const offset=Number(url.searchParams.get('offset'));
            assert.equal(offset,row.upload_offset);
            row.chunks.push({offset,size:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')});
            row.upload_offset+=bytes.length;
            if(loseResponse){loseResponse=false;await route.fulfill({status:503,json:{detail:'Fixture connection lost after chunk was saved'}});return;}
            data={upload_offset:row.upload_offset};
        }else if(url.pathname===root+'/'+item+'/complete'){
            row.phase='media_ready';row.clip_id='fixture-clip';row.media={width:1920,height:1080,duration_seconds:10,size_bytes:10};data={clip_id:row.clip_id};
        }else if(url.pathname===root+'/'+item+'/prepare'){
            assert.equal(body.mode,'preserve');
            row.productions=[{id:production,kind:'source_passthrough',status:'approved',stage:'source_passthrough_approved',generation:1}];data=row.productions[0];
        }else if(url.pathname===root+'/packages/history')data={items:[{id:'fixture-package',batch_key:'Fixture package',producer:'Fixture producer',record_count:2,created_at:'2026-10-02T10:00:00Z'}],total:1,offset:0,limit:25};
        else if(url.pathname===root+'/'+item)data=row;
        else throw new Error('Unexpected request '+req.method()+' '+url.pathname);
        await route.fulfill({json:data});
    });
    await page.goto('http://127.0.0.1:8772/content.html?channel='+channel);
    await page.getByText('Connected. Content history refreshes every 8 seconds.').waitFor();
    await page.locator('#title').fill('Fixture finished video');
    await page.locator('#file').setInputFiles({name:'fixture.mp4',mimeType:'video/mp4',buffer:Buffer.from('1234567890')});
    await page.locator('#rights').check();
    await page.locator('#add').click();
    await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('connection lost'));
    assert.equal(await page.locator('#title').inputValue(),'Fixture finished video');
    assert.match(await page.locator('#detail').innerText(),/5 \/ 10 bytes/);
    await page.evaluate(()=>{for(const key of Object.keys(sessionStorage))if(key.startsWith('katcha.intake.'))sessionStorage.removeItem(key);});
    await page.locator('#add').click();
    await page.getByText('Media is ready. Choose a production step in Content details.').waitFor();
    assert.equal(requests.filter(r=>r.path.endsWith('/chunks')).length,2,'Resume does not retransmit the acknowledged chunk');
    await page.getByRole('button',{name:'Use video unchanged',exact:true}).click();
    await page.getByRole('button',{name:'Review for YouTube'}).waitFor();
    assert(!requests.some(r=>r.body?.mode==='ai_short'));
    await page.locator('#load-packages').click();
    await page.getByText('Fixture package',{exact:true}).waitFor();
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'No mobile horizontal overflow');
    await page.getByRole('button',{name:'Use video unchanged',exact:true}).click();
    await page.getByRole('button',{name:'Review for YouTube'}).waitFor();
    await page.screenshot({path:'/tmp/katcha-content-mobile.png',fullPage:true});
    await page.keyboard.press('Tab');
    assert(await page.evaluate(()=>document.activeElement.tagName!=='BODY'));
    assert.deepEqual(errors,[]);
    console.log('Content: durable receipt, uncertain chunk recovery, unchanged video, package history, keyboard and 390px layout passed.');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill();});
