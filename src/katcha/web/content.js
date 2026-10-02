const params = new URLSearchParams(location.search);
const contentState = {channel: params.get('channel') || sessionStorage.getItem('katcha.channel') || '', item: params.get('item') || '', offset: 0, total: 0, busy: false, pause: false, epoch: 0};
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const human = value => String(value || '').replaceAll('_', ' ');
const message = text => { $('message').textContent = text; };
$('token').value = sessionStorage.getItem('katcha.controlToken') || '';
async function api(path, options = {}) {
    const token = sessionStorage.getItem('katcha.controlToken');
    const response = await fetch(path, {...options, headers: {...(token ? {Authorization: 'Bearer '+token} : {}), ...(options.body instanceof Blob ? {} : {'Content-Type':'application/json'}), ...options.headers}});
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Request failed. Check your inputs and try again.');
    return body;
}
function endpoint(suffix='') { return '/v1/channels/'+contentState.channel+'/content'+suffix; }
function rememberItem(id) {
    contentState.item=id;
    history.replaceState(null, '', '/content?channel='+encodeURIComponent(contentState.channel)+'&item='+encodeURIComponent(id));
}
async function load() {
    if (!contentState.channel) return;
    const epoch=++contentState.epoch;
    const page=await api(endpoint('?offset='+contentState.offset+'&limit=25&q='+encodeURIComponent($('search').value)));
    if (epoch!==contentState.epoch) return;
    contentState.total=page.total;
    $('items').innerHTML=page.items.length ? page.items.map(row=>`<button type="button" class="content-row" data-item="${esc(row.id)}" aria-pressed="${row.id===contentState.item}"><strong>${esc(row.title)}</strong><span>${esc(human(row.phase))} · ${esc(human(row.input_kind))}</span>${row.error ? `<small>Needs attention: ${esc(row.error)}</small>` : ''}</button>`).join('') : '<p>No content matches this channel and search. Add a video above or use Sources for a package.</p>';
    $('range').textContent=page.total ? `${page.offset+1}–${Math.min(page.offset+25,page.total)} of ${page.total}` : '0 items';
    $('previous').disabled=!page.offset; $('next').disabled=page.offset+25>=page.total;
    if(contentState.item) {
        const row=page.items.find(i=>i.id===contentState.item) || await api(endpoint('/'+contentState.item));
        if(epoch===contentState.epoch) detail(row);
    }
}
function detail(row) {
    contentState.selected=row;
    const bytes=(offset,size)=>size ? `${Number(offset||0).toLocaleString()} / ${Number(size).toLocaleString()} bytes (${Math.floor(Number(offset||0)/Number(size)*100)}%)` : 'Waiting for upload size';
    $('detail').innerHTML=`<h2>${esc(row.title)}</h2><p>${esc(human(row.phase))}</p><details><summary>Troubleshooting</summary><small>Content ID: ${esc(row.id)}</small></details>
    ${row.error ? `<p role="alert">${esc(row.error)}</p>`:''}
    ${row.input_kind==='file' ? `<p>Received: ${esc(bytes(row.upload_offset,row.size_bytes))}</p>`:''}
    ${row.phase==='uploading' ? '<p>Reselect the same file and choose Add content to resume. Completed chunks are verified before continuing.</p>':''}
    ${row.phase==='failed'||(row.error && !row.clip_id && row.input_kind==='url') ? '<button class="button secondary" data-action="retry-download">Retry download</button>':''}
    ${row.media ? `<p>${esc(row.media.width)} × ${esc(row.media.height)} · ${esc(row.media.duration_seconds)} seconds</p><a href="/clips?channel=${encodeURIComponent(contentState.channel)}&clip=${encodeURIComponent(row.clip_id)}">Inspect media and source history in Clips</a><p>Use the finished video unchanged, or start an optional AI short. Existing rights checks still apply. Permission does not establish original authorship or YouTube monetization eligibility.</p>${row.readiness ? `<p>Production readiness: ${esc(row.readiness.reason)}</p>` : ''}<button class="button primary" data-action="preserve"${row.readiness?.eligible===false?' disabled':''}>Use video unchanged</button> <button class="button secondary" data-action="ai_short"${row.readiness?.eligible===false?' disabled':''}>Create AI short</button><p><a href="/studio?channel=${encodeURIComponent(contentState.channel)}&clip=${encodeURIComponent(row.clip_id)}">Edit in Clip Studio</a></p>`:''}
    <h3>Production</h3>${row.productions.length ? row.productions.map(p=>`<p>${esc(human(p.kind))}: ${esc(human(p.stage||p.status))}${p.error ? ' · '+esc(p.error):''}</p>${p.status==='approved' && !row.publications.some(v=>v.production_id===p.id) ? `<button class="button secondary" data-production="${esc(p.id)}">Review for YouTube</button>`:''}`).join(''):'<p>No production yet. Media must finish arriving before preparation can start.</p>'}
    <h3>Publishing</h3>${row.publications.length ? row.publications.map(p=>`<p>${esc(p.title)} · ${esc(human(p.stage))}<br>Upload: ${esc(bytes(p.upload_offset,p.upload_size))}${p.publish_at?'<br>Scheduled: '+esc(new Date(p.publish_at).toLocaleString()):''}${p.error?'<br>Needs attention: '+esc(p.error):''}</p><a href="/channels?channel=${encodeURIComponent(contentState.channel)}&publication=${encodeURIComponent(p.id)}">Open metadata and release controls</a>`).join(''):'<p>No publication draft yet. Review an approved production to set title, description, visibility, and schedule.</p>'}
    <h3>Recent history</h3><ul>${row.events.map(e=>`<li>${esc(human(e.event_type))} · ${esc(new Date(e.created_at).toLocaleString())}</li>`).join('')||'<li>Receipt saved. Waiting for the next step.</li>'}</ul>`;
}
async function connect() {
    sessionStorage.setItem('katcha.controlToken',$('token').value.trim());
    const channels=await api('/v1/channels');
    $('channel').innerHTML='<option value="">Choose a channel</option>'+channels.map(c=>`<option value="${esc(c.id)}">${esc(c.profile_metadata?.channel_title||c.id)}</option>`).join('');
    const unavailable=Boolean(contentState.channel&&!channels.some(c=>c.id===contentState.channel));
    if(unavailable){contentState.channel='';contentState.item='';}
    else if(!contentState.channel)contentState.channel=channels[0]?.id||'';
    $('channel').value=contentState.channel;
    sessionStorage.setItem('katcha.channel',contentState.channel);
    $('channel-link').href='/channels?channel='+encodeURIComponent(contentState.channel);
    $('add').disabled=!contentState.channel;
    if(params.get('clip')&&!contentState.clipPrefilled) {contentState.clipPrefilled=true;$('input-kind').value='clip';$('clip-id').value=params.get('clip');$('selected-clip').textContent=params.get('title')||'Clip selected. Review the media before publishing.';if(!$('title').value)$('title').value=params.get('title')||'';method();}
    $('browse-clips').href='/clips?channel='+encodeURIComponent(contentState.channel);
    await load();
    message(unavailable ? 'The requested channel is unavailable. Choose a channel before adding content.' : channels.length ? 'Connected. Content history refreshes every 8 seconds.':'Create a channel in Channel Studio before adding content.');
}
function method(){for(const kind of ['file','url','clip']) $(kind+'-input').hidden=$('input-kind').value!==kind;}
$('input-kind').addEventListener('change',method);
$('connect-form').addEventListener('submit',e=>{e.preventDefault();connect().catch(e=>message(e.message));});
$('channel').addEventListener('change',()=>{contentState.channel=$('channel').value;contentState.item='';contentState.selected=null;contentState.offset=0;packageOffset=0;$('packages').innerHTML='';$('more-packages').hidden=true;sessionStorage.setItem('katcha.channel',contentState.channel);$('detail').innerHTML='<p>Select content to see its progress.</p>';$('channel-link').href='/channels?channel='+encodeURIComponent(contentState.channel);$('add').disabled=!contentState.channel;$('browse-clips').href='/clips?channel='+encodeURIComponent(contentState.channel);load().catch(e=>message(e.message));});
$('search-form').addEventListener('submit',e=>{e.preventDefault();contentState.offset=0;load().catch(e=>message(e.message));});
$('previous').onclick=()=>{contentState.offset=Math.max(0,contentState.offset-25);load().catch(e=>message(e.message));};
$('next').onclick=()=>{contentState.offset+=25;load().catch(e=>message(e.message));};
$('items').onclick=e=>{const button=e.target.closest('[data-item]');if(button){rememberItem(button.dataset.item);load().catch(e=>message(e.message));}};
$('pause').onclick=()=>{contentState.pause=true;message('Pausing after the current chunk. Your uploaded bytes remain saved.');};
$('intake-form').addEventListener('submit',async e=>{
    e.preventDefault();if(contentState.busy||!contentState.channel)return;
    contentState.busy=true;contentState.pause=false;$('add').disabled=true;$('channel').disabled=true;
    try {
        const kind=$('input-kind').value,file=$('file').files[0];
        if(kind==='file'&&!file)throw new Error('Choose a video file.');
        if(kind==='clip'&&!$('clip-id').value)throw new Error('Choose a clip in Clips and select Create video first.');
        if(kind==='file'&&file.size>4*1024**3)throw new Error('Choose a video export up to 4 GiB.');
        // A retry uses the saved identity. Every saved chunk is checked against the reselected file.
        const inputIdentity=kind==='file'?`${file.name}:${file.size}:${file.lastModified}`:kind==='url'?$('url').value:$('clip-id').value;
        let fingerprint=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(inputIdentity)))).map(b=>b.toString(16).padStart(2,'0')).join('');
        const storageKey='katcha.intake.'+contentState.channel+'.'+fingerprint;
        const selected=contentState.selected;
        const resume=kind==='file'&&selected?.input_kind==='file'&&selected.phase==='uploading'&&selected.filename===file.name&&selected.size_bytes===file.size;
        const key=resume&&selected.request_key ? selected.request_key : sessionStorage.getItem(storageKey)||crypto.randomUUID();
        if(resume&&selected.fingerprint)fingerprint=selected.fingerprint;
        sessionStorage.setItem(storageKey,key);
        const receipt=await api(endpoint(),{method:'POST',body:JSON.stringify({request_key:key,input_kind:kind,title:$('title').value,rights_confirmed:$('rights').checked,...(kind==='file'?{filename:file.name,size_bytes:file.size,fingerprint}:kind==='url'?{url:$('url').value}:{clip_id:$('clip-id').value})})});
        rememberItem(receipt.id);await load();
        if(kind==='file'&&!receipt.clip_id){
            $('pause').hidden=false;$('upload-progress').hidden=false;
            for(const chunk of receipt.chunks||[]){
                const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.slice(chunk.offset,chunk.offset+chunk.size).arrayBuffer()))).map(b=>b.toString(16).padStart(2,'0')).join('');
                if(digest!==chunk.sha256)throw new Error('The reselected file differs from the saved upload. Choose the original file.');
            }
            let offset=receipt.upload_offset;
            while(offset<file.size&&!contentState.pause){
                const result=await api(endpoint('/'+receipt.id+'/chunks?offset='+offset),{method:'PUT',body:file.slice(offset,Math.min(offset+(receipt.chunk_bytes||8*1024**2),file.size))});
                offset=result.upload_offset;$('upload-progress').value=offset/file.size*100;message(`Received ${offset.toLocaleString()} of ${file.size.toLocaleString()} bytes.`);
            }
            if(contentState.pause){message('Upload paused. Reselect the same file to resume.');await load();return;}
            message('All bytes received. Validating video…');await api(endpoint('/'+receipt.id+'/complete'),{method:'POST'});
        }
        await load();message(kind==='url'?'Receipt saved. Check download progress in Content details.':'Media is ready. Choose a production step in Content details.');
    }catch(error){message(error.message+' Your entered values are retained. Check content history for a saved receipt before retrying.');await load().catch(()=>{});}
    finally{contentState.busy=false;$('add').disabled=false;$('channel').disabled=false;$('pause').hidden=true;}
});
$('detail').onclick=async e=>{
    const button=e.target.closest('[data-action],[data-production]');if(!button||contentState.busy)return;
    button.disabled=true;contentState.busy=true;
    try {
        if(button.dataset.production){const p=await api(endpoint('/'+contentState.item+'/publication'),{method:'POST',body:JSON.stringify({production_id:button.dataset.production,title:$('detail h2')?.textContent.slice(0,100)||'Video'})});location.href='/channels?channel='+encodeURIComponent(contentState.channel)+'&publication='+encodeURIComponent(p.id);return;}
        const action=button.dataset.action;
        await api(endpoint('/'+contentState.item+(action==='retry-download'?'/retry-download':'/prepare')),{method:'POST',body:action==='retry-download'?undefined:JSON.stringify({mode:action})});
        await load();message(action==='retry-download'?'Download queued. Follow the receipt for results.':'Production registered. See its status and next action below.');
    }catch(error){message(error.message);}finally{contentState.busy=false;button.disabled=false;}
};
setInterval(()=>{if(!document.hidden&&!contentState.busy&&contentState.channel)load().catch(e=>message('Refresh failed: '+e.message+'. Saved content remains available.'));},8000);
connect().catch(e=>message(e.message));

let packageOffset=0;
async function loadPackages(append=false){
    if(!contentState.channel)return;
    const channel=contentState.channel;
    const page=await api(endpoint('/packages/history?offset='+packageOffset+'&limit=25'));
    if(channel!==contentState.channel)return;
    const markup=page.items.map(row=>`<article class="content-row"><strong>${esc(row.batch_key)}</strong><span>${esc(row.producer)} · ${esc(row.record_count)} records · ${esc(new Date(row.created_at).toLocaleString())}</span><small>Import stored. Inspect Sources for records and Content history for acquired media.</small></article>`).join('')||'<p>No successful package imports for this channel yet.</p>';
    if(append)$('packages').insertAdjacentHTML('beforeend',markup);else $('packages').innerHTML=markup;
    $('more-packages').hidden=page.offset+25>=page.total;
}
$('load-packages').onclick=()=>{packageOffset=0;loadPackages().catch(e=>message(e.message));};
$('more-packages').onclick=()=>{packageOffset+=25;loadPackages(true).catch(e=>message(e.message));};
