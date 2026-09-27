/* Same-origin control plane; credentials live only in memory. */
const $ = (id) => document.getElementById(id);
let token = '', adapters = [], sources = [], historyEpoch = 0;
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
async function api(path, body) {
    const r = await fetch(`/v1/${path}`, {method: body === undefined ? 'GET' : 'POST', headers: {...(token ? {Authorization: `Bearer ${token}`} : {}), ...(body === undefined ? {} : {'Content-Type':'application/json'})}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const data = await r.json();
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${r.status}): ${JSON.stringify(data.detail)}`);
    return data;
}
function action(id, event, fn) {
    $(id).addEventListener(event, async e => {
        e.preventDefault();
        const button = e.submitter || (e.target.tagName === 'BUTTON' ? e.target : null);
        if (button) button.disabled = true;
        try { await fn(e); } catch (error) { message(error.message, true); }
        finally { if (button) button.disabled = false; }
    });
}
function adapter() { return adapters[Number($('adapter').value)]; }
function source() { return sources.find(row => row.id === $('source').value); }
function configure() {
    const a = adapter();
    if (!a) return;
    $('capability').textContent = `${a.description} Credentials on server: ${a.required_credentials.join(', ') || 'none'}.`;
    $('platforms').innerHTML = a.supported_platforms.map(p => `<option value="${esc(p)}"></option>`).join('');
    $('platform').value = a.supported_platforms[0] || 'custom';
    $('query-fields').replaceChildren();
    // The installed catalog supplies defaults and field names, including future adapters.
    for (const key of new Set([...a.query_fields, ...Object.keys(a.sample_query)])) {
        const value = a.sample_query[key];
        const label = document.createElement('label'); label.textContent = key.replaceAll('_', ' ');
        const input = document.createElement(typeof value === 'object' && value !== null ? 'textarea' : 'input');
        input.dataset.key = key;
        input.dataset.kind = ['items','urls'].includes(key) ? 'object' : value === undefined ? 'optional' : typeof value;
        // Never turn catalog example media into an operator's real batch.
        input.value = ['items','urls'].includes(key) ? '[]' : value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
        label.append(input); $('query-fields').append(label);
    }
}
async function refreshSources() {
    const selected = $('source').value;
    sources = await api('discovery/sources');
    $('source').innerHTML = sources.map(s => `<option value="${esc(s.id)}">${esc(s.name)} · ${esc(s.platform)}</option>`).join('');
    if (sources.some(s => s.id === selected)) $('source').value = selected;
    await selectSource();
}
async function selectSource() {
    const s = source();
    $('run').disabled = !s?.enabled;
    $('import').hidden = !s?.enabled || !adapters.some(a => a.key === s.adapter_key && a.version === s.adapter_version && a.supports_imports);
    $('source-info').textContent = s ? `${s.source_key} · ${s.enabled ? 'Enabled' : 'Disabled'} · ${s.usage_mode} · ${s.channel_profile_id || 'Shared source'}` : 'No sources configured yet.';
    await history();
}
async function history() {
    const epoch = ++historyEpoch, s = source();
    $('history').textContent = s ? 'Loading…' : 'Add your first source to begin.';
    if (!s) return;
    const rows = await api(`discovery/sources/${encodeURIComponent(s.id)}/runs`);
    if (epoch !== historyEpoch) return;
    $('history').innerHTML = rows.length ? rows.map(r => `<article class="item"><strong>${esc(r.status)}</strong><p>${esc(new Date(r.created_at).toLocaleString())}</p><p>${esc(r.run_key)}</p>${r.error ? `<p class="error">${esc(r.error)}</p>` : ''}${r.status === 'queued' ? `<button class="button secondary" data-execute="${esc(r.id)}">Execute</button>` : ''}</article>`).join('') : 'No runs yet.';
}
action('connect', 'submit', async () => {
    $('workspace').disabled = true; ++historyEpoch;
    token = $('token').value; $('token').value = '';
    const result = await Promise.all([api('discovery/adapters'), api('channels')]);
    adapters = result[0];
    $('adapter').innerHTML = adapters.map((a,i) => `<option value="${i}">${esc(a.label)} · ${esc(a.version)}</option>`).join('');
    $('channel').innerHTML = '<option value="">Shared source</option>' + result[1].map(c => `<option value="${esc(c.id)}">${esc(c.profile_metadata?.name || c.id)}</option>`).join('');
    configure(); await refreshSources(); $('workspace').disabled = false; message('Connected. Showing saved sources.');
});
action('adapter', 'change', configure);
action('source', 'change', selectSource);
action('refresh', 'click', refreshSources);
action('history-refresh', 'click', history);
action('setup', 'submit', async () => {
    const key = $('key').value.trim().toLowerCase();
    const current = await api('discovery/sources');
    if (current.some(s => s.source_key === key)) throw new Error('That source key already exists. Choose a unique key.');
    const a = adapter(), query = {};
    for (const input of $('query-fields').querySelectorAll('[data-key]')) {
        const value = input.value.trim(), kind = input.dataset.kind;
        if (kind === 'optional' && !value) continue;
        query[input.dataset.key] = ['object','number','boolean'].includes(kind) ? JSON.parse(value) : value;
    }
    const s = await api('discovery/sources', {create_only:true,source_key:key,name:$('name').value,channel_profile_id:$('channel').value || null,adapter_key:a.key,adapter_version:a.version,platform:$('platform').value,usage_mode:$('usage').value,query_template:query});
    await refreshSources(); $('source').value = s.id; await selectSource(); message('Source saved. Create a run or import a URL batch.');
});
action('run', 'click', async () => {
    const s = source(); if (!s?.enabled) return;
    await api(`discovery/sources/${encodeURIComponent(s.id)}/runs`, {idempotency_key:`source-ui:${s.id}:${crypto.randomUUID()}`});
    await history(); message('Run created. Choose Execute to start discovery.');
});
action('import', 'submit', async () => {
    const s = source(), urls = $('urls').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (!urls.length) throw new Error('Enter at least one URL.');
    for (const value of urls) { const url = new URL(value); if (!['https:','http:'].includes(url.protocol)) throw new Error('Use HTTP or HTTPS URLs.'); }
    await api(`discovery/sources/${encodeURIComponent(s.id)}/imports`, {batch_key:$('batch').value.trim(), urls});
    await history(); message('Batch registered. Choose Execute on its queued run.');
});
action('history', 'click', async e => {
    const button = e.target.closest('[data-execute]'); if (!button) return;
    await api(`discovery/runs/${encodeURIComponent(button.dataset.execute)}/execute`, {});
    message('Discovery submitted. Refresh runs to inspect progress.'); await history();
});
