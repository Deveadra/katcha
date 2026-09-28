/* Keep storage contracts behind task-focused forms. Never persist credentials. */
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const methods = {
    links: {key: 'operator_feed', title: 'Paste links', icon: '↗', description: 'Collect videos or posts from TikTok, Instagram, X, or any other website.', help: 'Give your collection a name. After saving, paste the links you want Katcha to consider. This option does not search social platforms for you.'},
    youtube: {key: 'youtube', title: 'Search YouTube', icon: '▶', description: 'Find recent videos about a topic, such as new game trailers.', help: 'Save a topic to search for recent YouTube videos. YouTube search access must be configured in Katcha before a search can run.'},
    reddit: {key: 'reddit', title: 'Search Reddit', icon: '◎', description: 'Find discussions and shared links, across Reddit or in one community.', help: 'Save a topic and, optionally, a Reddit community. Reddit search access must be configured in Katcha before a search can run.'},
    feed: {key: 'rss_atom', title: 'Follow a website feed', icon: '≋', description: 'Collect updates from a news site, blog, or release feed using its RSS link.', help: 'Save a website’s RSS or Atom feed. Use “Check for updates” whenever you want to collect new entries. Scheduled checking is not enabled here.'},
};
const usage = {
    candidate_review: ['Review first', 'Keep this source marked for review before deciding what to use.'],
    discovery_only: ['Research and inspiration', 'Mark this source as research material rather than intended video material.'],
    operator_authorized: ['I will decide what can be used', 'Record that you will make the content-use decision.'],
    render_allowed: ['Intended for video production', 'Mark this source as intended video material. Existing production checks still apply.'],
    blocked: ['Blocked', 'This source is marked as blocked.'],
};
let token = '', adapters = [], channels = [], sources = [], selectedMethod = '', step = 1;
let channelsReady = false, historyEpoch = 0, connectionEpoch = 0, busy = false;
const intents = new Map();
const runIntents = new Map();
const linkDrafts = new Map();
let displayedSourceId = null;
function message(text, error = false) {
    $('message').textContent = text;
    $('message').className = error ? 'message error' : 'message';
}
async function api(path, body) {
    let response;
    try {
        response = await fetch(`/v1/${path}`, {
            method: body === undefined ? 'GET' : 'POST',
            headers: {...(token ? {Authorization: `Bearer ${token}`} : {}), ...(body === undefined ? {} : {'Content-Type': 'application/json'})},
            ...(body === undefined ? {} : {body: JSON.stringify(body)}),
        });
    } catch {
        throw new Error('Katcha could not be reached. Check that it is running, then try again. Your entries are still here.');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        const error = new Error(response.status === 401 ? 'Your workspace needs an access token. Enter it below to connect.' : response.status >= 500 ? 'Katcha could not finish this request. Check the launch console for service problems, then try again.' : typeof data.detail === 'string' ? data.detail : 'Some details were not accepted. Check your entries and try again.');
        error.status = response.status;
        throw error;
    }
    return data;
}
function bind(id, event, fn, inline = false) {
    $(id).addEventListener(event, async e => {
        e.preventDefault();
        if (event === 'change') {
            try { await fn(e); } catch (error) { message(error.message, true); }
            return;
        }
        if (busy) return;
        busy = true;
        const button = e.submitter || e.target.closest('button');
        if (button) button.disabled = true;
        $('setup-error').textContent = '';
        try { await fn(e); }
        catch (error) {
            if (inline) $('setup-error').textContent = error.message;
            else message(error.message, true);
        } finally {
            busy = false;
            if (button) button.disabled = false;
        }
    });
}
function installed(key) { return adapters.find(a => a.key === key && a.version === 'v1'); }
function chosenAdapter() {
    return selectedMethod === 'custom' ? adapters.find(a => `${a.key}@${a.version}` === $('custom-adapter').value) : installed(methods[selectedMethod]?.key);
}
function channelName(row) { return row.profile_metadata?.channel_title || row.profile_metadata?.name || 'Unnamed channel'; }
function source() { return sources.find(s => s.id === $('source').value); }
function connectionName(row) { return Object.values(methods).find(m => m.key === row.adapter_key)?.title || 'Custom connection'; }
function sourceChannel(row) { return channels.find(c => c.id === row.channel_profile_id); }
function showStep(next) {
    step = next;
    for (let n = 1; n <= 3; n++) $('step-' + n).hidden = n !== next;
    document.querySelectorAll('.steps li').forEach((li, i) => {
        if (i + 1 === next) li.setAttribute('aria-current', 'step');
        else li.removeAttribute('aria-current');
    });
    $('step-label').textContent = `STEP ${next} OF 3`;
    $('builder-title').textContent = ['Where will the content come from?', 'Make this source yours', 'Ready to add this source?'][next - 1];
    $('wizard-actions').hidden = next === 1;
    $('next').hidden = next !== 2;
    $('save').hidden = next !== 3;
    $('setup-error').textContent = '';
    if (next === 2) $('name').focus();
    if (next === 3) $('save').focus();
}
function choose(method) {
    selectedMethod = method;
    const a = chosenAdapter();
    if (!a) throw new Error('This connection is not installed in your workspace. Choose another source type.');
    $('method-help').textContent = methods[method]?.help || `${a.description} This connection requires advanced configuration.`;
    $('search-fields').hidden = !['youtube', 'reddit'].includes(method);
    $('reddit-fields').hidden = method !== 'reddit';
    $('feed-fields').hidden = method !== 'feed';
    $('custom-fields').hidden = method !== 'custom';
    $('custom-query').value = '{}';
    showStep(2);
}
function intent(key, prefix) {
    if (!intents.has(key)) intents.set(key, `${prefix}-${crypto.randomUUID()}`);
    return intents.get(key);
}
function httpUrl(value, label) {
    try {
        const parsed = new URL(value);
        if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error();
    } catch { throw new Error(`${label} must be a full http:// or https:// link, without a password.`); }
    return value;
}
function details() {
    if (!channelsReady) throw new Error('Load your channels before saving so this source goes to the right place. Use “Try loading channels again.”');
    const name = $('name').value.trim(), a = chosenAdapter();
    if (!name) throw new Error('Give this source a name you will recognize.');
    if (!a) throw new Error('Choose a source type first.');
    let query = {}, platform = selectedMethod;
    if (selectedMethod === 'links') { query = {items: [], urls: []}; platform = 'custom'; }
    if (['youtube', 'reddit'].includes(selectedMethod)) {
        const q = $('search').value.trim();
        if (!q) throw new Error('Enter a topic for Katcha to search for.');
        query = {q, limit: 25, freshness_horizon_hours: 72};
        if (selectedMethod === 'youtube') query.order = 'date';
        else {
            const subreddit = $('community').value.trim().replace(/^\/?r\//, '').replace(/\/$/, '');
            if (subreddit && !/^[A-Za-z0-9_]+$/.test(subreddit)) throw new Error('Enter a community name such as gaming or r/gaming, rather than a full web address.');
            query = {...query, subreddit, sort: 'new', time_filter: 'week'};
        }
    }
    if (selectedMethod === 'feed') { platform = 'web'; query = {feed_url: httpUrl($('feed').value.trim(), 'The website feed'), limit: 50}; }
    if (selectedMethod === 'custom') {
        try { query = JSON.parse($('custom-query').value); } catch { throw new Error('The custom connection settings are not valid JSON.'); }
        if (!query || Array.isArray(query) || typeof query !== 'object') throw new Error('Custom connection settings must be a JSON object.');
        platform = a.supported_platforms[0] || 'custom';
    }
    return {create_only: true, name, adapter_key: a.key, adapter_version: a.version, platform, channel_profile_id: $('channel').value || null, usage_mode: $('usage').value, query_template: query};
}
function review() {
    const d = details();
    const rows = [
        ['Name', d.name], ['Content source', methods[selectedMethod]?.title || chosenAdapter().label],
        ['For', channels.find(c => c.id === d.channel_profile_id) ? channelName(channels.find(c => c.id === d.channel_profile_id)) : 'Shared collection · not assigned to a channel'],
        ['Review preference', usage[d.usage_mode][0]],
    ];
    if (d.query_template.q) rows.push(['Search topic', d.query_template.q]);
    if (d.query_template.subreddit) rows.push(['Community', `r/${d.query_template.subreddit}`]);
    if (d.query_template.feed_url) rows.push(['Website feed', d.query_template.feed_url]);
    rows.push(['Next step', selectedMethod === 'links' ? 'Paste links into your saved collection.' : 'Start a search or check for updates when you are ready.']);
    $('review').innerHTML = rows.map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('');
    showStep(3);
}
async function loadChannels(epoch = connectionEpoch) {
    try {
        const rows = await api('channels');
        if (epoch !== connectionEpoch) return;
        channels = rows.filter(c => c.status === 'active');
        channelsReady = true;
        const previous = $('channel').value;
        $('channel').innerHTML = '<option value="">Shared collection · no channel assigned</option>' + channels.map(c => `<option value="${esc(c.id)}">${esc(channelName(c))}</option>`).join('');
        if (channels.some(c => c.id === previous)) $('channel').value = previous;
        $('channel-area').hidden = !channels.length;
        $('channel-help').textContent = channels.length ? 'Choose one of your channels, or keep this collection unassigned for now. This does not send content to every channel.' : 'No active channels are set up in this workspace yet. You can still save a shared collection. Your YouTube account must also be set up as a channel in Katcha before it appears here.';
        $('retry-channels').hidden = false;
        $('retry-channels').textContent = 'Refresh channels';
    } catch (error) {
        if (epoch !== connectionEpoch) return;
        channelsReady = false;
        $('channel-area').hidden = true;
        $('channel-help').textContent = 'Your channels could not be loaded. Try again before saving; Katcha will not silently save to a different channel.';
        $('retry-channels').hidden = false;
        $('retry-channels').textContent = 'Try loading channels again';
    }
}
async function connect() {
    const epoch = ++connectionEpoch;
    ++historyEpoch;
    $('workspace').disabled = true;
    if ($('token').value) token = $('token').value;
    $('token').value = '';
    $('connection').textContent = 'Connecting…';
    try {
        adapters = await api('discovery/adapters');
        if (epoch !== connectionEpoch) return;
        $('methods').innerHTML = Object.entries(methods).map(([id, method]) => `<button type="button" class="method" data-method="${id}" ${installed(method.key) ? '' : 'disabled'}><span class="method-icon" aria-hidden="true">${method.icon}</span><strong>${method.title}</strong><span>${method.description}${installed(method.key) ? '' : ' Not installed in this workspace.'}</span></button>`).join('');
        $('custom-adapter').innerHTML = adapters.map(a => `<option value="${esc(`${a.key}@${a.version}`)}">${esc(a.label)}</option>`).join('');
        $('choose-custom').disabled = !adapters.length;
        await loadChannels(epoch);
        await refreshSources();
        if (epoch !== connectionEpoch) return;
        $('workspace').disabled = false;
        $('connection').textContent = 'Connected';
        $('connection-panel').hidden = true;
        message('');
    } catch (error) {
        if (epoch !== connectionEpoch) return;
        $('connection').textContent = 'Not connected';
        $('connection-panel').hidden = false;
        $('connect').hidden = false;
        $('connect').style.display = '';
        $('connection-title').textContent = error.status === 401 ? 'Unlock your workspace' : 'Katcha is not ready yet';
        message(error.message, true);
    }
}
async function refreshSources() {
    const selected = $('source').value;
    const rows = await api('discovery/sources');
    sources = rows;
    $('source').innerHTML = sources.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
    if (sources.some(s => s.id === selected)) $('source').value = selected;
    $('empty-sources').hidden = !!sources.length;
    $('source-controls').hidden = !sources.length;
    await selectSource();
}
async function selectSource() {
    const s = source();
    if (displayedSourceId !== s?.id) {
        if (displayedSourceId) linkDrafts.set(displayedSourceId, $('urls').value);
        displayedSourceId = s?.id || null;
        $('urls').value = linkDrafts.get(displayedSourceId) || '';
    }
    if (!s) { ++historyEpoch; return; }
    const canImport = adapters.some(a => a.key === s.adapter_key && a.version === s.adapter_version && a.supports_imports);
    const usable = s.enabled && s.usage_mode !== 'blocked';
    $('import').hidden = !canImport || !usable;
    $('run').hidden = canImport || !usable;
    $('run').textContent = s.adapter_key === 'rss_atom' ? 'Check for updates' : 'Search now';
    const channel = s.channel_profile_id ? channelName(sourceChannel(s) || {profile_metadata: {name: 'Assigned channel (not available)'}}) : 'Shared collection';
    $('source-info').innerHTML = `<strong>${esc(connectionName(s))}</strong><p>${esc(channel)} · ${esc(usage[s.usage_mode]?.[0] || 'Custom review preference')}</p>${s.query_template?.q ? `<p>Topic: ${esc(s.query_template.q)}</p>` : ''}`;
    $('operation-help').textContent = !usable ? 'This source is paused or blocked. Content checks cannot be started here.' : canImport ? 'Add links whenever you find something worth considering. No posting happens here.' : 'Checks start when you press the button. Automatic scheduled checking is not enabled here.';
    await history();
}
async function history() {
    const s = source(), epoch = ++historyEpoch;
    if (!s) return;
    $('history').textContent = 'Loading recent activity…';
    try {
        const rows = await api(`discovery/sources/${encodeURIComponent(s.id)}/runs`);
        if (epoch !== historyEpoch) return;
        for (const row of rows) {
            if (row.status === 'queued') continue;
            for (const [key, id] of runIntents) {
                if (id === row.id) { intents.delete(key); runIntents.delete(key); }
            }
        }
        const labels = {queued: 'Ready to start', running: 'Finding content…', completed: 'Finished checking content', failed: 'Could not finish'};
        $('history').innerHTML = rows.length ? rows.map(r => `<article class="item"><strong>${esc(labels[r.status] || 'Status unavailable')}</strong><p>${esc(new Date(r.created_at).toLocaleString())}</p>${r.status === 'failed' ? '<p>The search could not finish. Check Katcha’s connections, then start a new search or add the links again.</p>' : ''}${r.status === 'queued' && s.enabled && s.usage_mode !== 'blocked' ? `<p>This request is saved, but has not started yet.</p><button class="button secondary" data-execute="${esc(r.id)}">Start now</button>` : ''}${r.error ? `<details><summary>Technical details for troubleshooting</summary><pre>${esc(r.error)}</pre></details>` : ''}</article>`).join('') : '<p class="hint">Nothing checked yet. Add links or start a search to begin.</p>';
    } catch (error) {
        if (epoch !== historyEpoch) return;
        $('history').textContent = 'Recent activity could not be loaded. Use “Refresh activity” to try again.';
    }
}
async function start(run) {
    try { await api(`discovery/runs/${encodeURIComponent(run.id)}/execute`, {}); }
    catch {
        await history();
        throw new Error('Your request is saved, but Katcha could not confirm that the content check started. Refresh activity; if it says “Ready to start,” choose “Start now” to retry without adding it again.');
    }
    for (const [key, id] of runIntents) {
        if (id === run.id) { intents.delete(key); runIntents.delete(key); }
    }
    await history();
    message('Request sent. Refresh activity to see its progress. Nothing has been published.');
}
bind('connect', 'submit', connect);
bind('methods', 'click', e => { const choice = e.target.closest('[data-method]'); if (choice) choose(choice.dataset.method); }, true);
bind('choose-custom', 'click', () => choose('custom'), true);
bind('back', 'click', () => showStep(step - 1));
bind('next', 'click', review, true);
bind('usage', 'change', () => { $('usage-help').textContent = usage[$('usage').value][1]; });
bind('retry-channels', 'click', async () => { await loadChannels(); if (source()) await selectSource(); });
bind('source', 'change', selectSource);
bind('refresh', 'click', refreshSources);
bind('history-refresh', 'click', history);
bind('setup', 'submit', async () => {
    if (step !== 3) { if (step === 2) review(); return; }
    const d = details(), signature = JSON.stringify(d), source_key = intent(signature, 'source');
    // Recover a successful save whose response was lost, rather than duplicating it.
    const existing = (await api('discovery/sources')).find(s => s.source_key === source_key);
    const saved = existing || await api('discovery/sources', {...d, source_key});
    await refreshSources();
    $('source').value = saved.id;
    await selectSource();
    $('name').value = '';
    showStep(1);
    message(`“${saved.name}” is saved. ${saved.adapter_key === 'operator_feed' ? 'Paste links in your collection to get started.' : 'Select it under Saved sources and start a search when you are ready.'}`);
    $('source').focus();
}, true);
bind('run', 'click', async () => {
    const s = source(); if (!s?.enabled || s.usage_mode === 'blocked') return;
    const key = `search:${s.id}`;
    const run = await api(`discovery/sources/${encodeURIComponent(s.id)}/runs`, {idempotency_key: intent(key, 'search')});
    runIntents.set(key, run.id);
    await start(run);
});
bind('import', 'submit', async () => {
    const s = source(); if (!s?.enabled || s.usage_mode === 'blocked') return;
    const urls = [...new Set($('urls').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean))];
    if (!urls.length) throw new Error('Paste at least one content link, one per line.');
    urls.forEach(url => httpUrl(url, 'Each content link'));
    const key = `import:${s.id}:${JSON.stringify(urls)}`;
    const result = await api(`discovery/sources/${encodeURIComponent(s.id)}/imports`, {batch_key: intent(key, 'links'), urls});
    runIntents.set(key, result.discovery_run.id);
    await start(result.discovery_run);
    linkDrafts.set(s.id, '');
    if (displayedSourceId === s.id) $('urls').value = '';
    intents.delete(key);
});
bind('history', 'click', async e => {
    const button = e.target.closest('[data-execute]');
    if (button) await start({id: button.dataset.execute});
});
$('usage-help').textContent = usage.candidate_review[1];
// The launcher bridge connects after startup. Direct API users connect automatically.
if (location.port !== '8765') connect();
