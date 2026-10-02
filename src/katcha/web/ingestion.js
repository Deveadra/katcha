/* Keep storage contracts behind task-focused forms. Never persist credentials. */
const $ = (id) => document.getElementById(id);
const launchParams = new URLSearchParams(location.search);
const requestedChannel = launchParams.get("channel") || sessionStorage.getItem("katcha.channel") || "";
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const methods = {
    links: {key: 'operator_feed', title: 'Paste links', icon: '↗', description: 'Add specific videos or posts from any supported site.', help: 'Give your collection a name. After saving, paste the links you want Katcha to consider. This option does not search social platforms for you.'},
    scout: {key: 'web_scout', title: 'Discover new sources', icon: '✦', description: 'Find public creators, posts, communities, and sites around a topic.', help: 'Tell Katcha what to look for. Web scouting uses grounded public-web search. Saving creates the source; the final step lets you decide whether to run one check immediately.'},
    youtube: {key: 'youtube', title: 'Search YouTube', icon: '▶', description: 'Find recent YouTube videos around a topic.', help: 'Save a topic as a reusable source. YouTube access must be configured before a check can run.'},
    youtube_channel: {key: 'youtube', title: 'Watch a YouTube channel', icon: '▶', description: 'Track a creator and inspect its recent public videos.', help: 'Add a creator’s @handle, channel link, or channel ID. Katcha can collect recent videos and engagement signals when you run a check.'},
    reddit: {key: 'reddit', title: 'Search Reddit', icon: '◎', description: 'Find discussions and shared links on Reddit.', help: 'Save a topic and, optionally, a community. Saving does not start a recurring search.'},
    feed: {key: 'rss_atom', title: 'Follow a website feed', icon: '≋', description: 'Check a site’s RSS or Atom feed for new entries.', help: 'Save the feed as a source, then choose whether to check it immediately.'},
};
const usage = {
    candidate_review: ['Find content for review', 'Finds may be offered to Clips for operator review. Nothing is published automatically.'],
    discovery_only: ['Research only', 'Finds are used as trend, packaging, audience, and editorial context and are not offered as clip candidates.'],
    operator_authorized: ['Operator decides each use', 'Record that the operator will make the content-use decision.'],
    render_allowed: ['Production-intended', 'Mark this source as production-intended after normal rights, originality, and approval checks.'],
    blocked: ['Blocked', 'This source is blocked from normal use.'],
};
let token = sessionStorage.getItem("katcha.controlToken") || '', adapters = [], channels = [], sources = [], selectedMethod = '', step = 1;
let sourceView = 'library';
let handoffInbox = {counts: {incoming: 0, processed: 0, failed: 0}, items: [], incoming_path: 'handoff/incoming'};
let sourcePage = {total: 0, limit: 50, offset: 0, items: []};
let findsPage = {total: 0, limit: 25, offset: 0, items: []};
let selectedOverview = null;
let editingSource = null;
let channelsReady = false, historyEpoch = 0, connectionEpoch = 0, libraryEpoch = 0, detailEpoch = 0, findsEpoch = 0, busy = false;
let searchTimer = null, findsSearchTimer = null;
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
function handoffProgress(title, detail = '', percent = null, state = '') {
    const panel = $('handoff-progress');
    panel.hidden = false;
    panel.className = 'handoff-progress' + (state ? ' is-' + state : '');
    $('handoff-progress-title').textContent = title;
    $('handoff-progress-detail').textContent = detail;
    const bar = $('handoff-progress-bar');
    if (percent == null) {
        bar.removeAttribute('value');
        $('handoff-progress-percent').textContent = '';
    } else {
        const bounded = Math.max(0, Math.min(100, Math.round(percent)));
        bar.value = bounded;
        $('handoff-progress-percent').textContent = bounded + '%';
    }
}

function handoffHttpError(status, data, fallback) {
    const detail = typeof data?.detail === 'string'
        ? data.detail
        : typeof data?.error === 'string'
            ? data.error
            : fallback;
    const error = new Error(status === 401 ? 'Your workspace needs an access token.' : detail);
    error.status = status;
    return error;
}

function handoffUpload(file) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        const launcher = location.port === '8765';
        const url = launcher
            ? '/runtime/handoff/upload?filename=' + encodeURIComponent(file.name)
            : '/v1/intelligence-ingest/inbox/files?process=true';

        request.open('POST', url);
        if (launcher) {
            request.setRequestHeader('Content-Type', 'application/octet-stream');
        } else if (token) {
            request.setRequestHeader('Authorization', 'Bearer ' + token);
        }

        request.upload.onprogress = event => {
            if (!event.lengthComputable) {
                handoffProgress(
                    'Uploading “' + file.name + '”…',
                    'Sending the selected intelligence batch to Katcha.',
                    null,
                );
                return;
            }
            const percent = event.total ? (event.loaded / event.total) * 100 : 0;
            handoffProgress(
                'Uploading “' + file.name + '”…',
                event.loaded.toLocaleString() + ' of ' + event.total.toLocaleString() + ' bytes sent.',
                percent,
            );
        };
        request.upload.onload = () => {
            handoffProgress(
                'Reading and validating batch…',
                'Upload complete. Katcha is validating records and committing the batch.',
                null,
            );
        };
        request.onerror = () => reject(
            new Error(
                'The handoff transport disconnected before Katcha replied. ' +
                'The selected file is still available; check the launch console and retry.'
            )
        );
        request.ontimeout = () => reject(
            new Error('Katcha did not finish the handoff request before the connection timed out.')
        );
        request.onload = () => {
            let data = {};
            try { data = JSON.parse(request.responseText || '{}'); } catch {}
            if (request.status < 200 || request.status >= 300) {
                reject(handoffHttpError(
                    request.status,
                    data,
                    'The handoff file could not be imported.',
                ));
                return;
            }
            resolve(data);
        };

        if (launcher) {
            request.send(file);
        } else {
            const form = new FormData();
            form.append('file', file, file.name);
            request.send(form);
        }
    });
}

async function handoffProcessPending() {
    handoffProgress(
        'Reading pending files…',
        'Katcha is scanning the handoff inbox and validating each pending batch.',
        null,
    );
    if (location.port !== '8765') {
        return api('intelligence-ingest/inbox/process', {});
    }

    let response;
    try {
        response = await fetch('/runtime/handoff/process', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({limit: 50}),
        });
    } catch {
        throw new Error(
            'The local handoff transport disconnected while processing the inbox. ' +
            'The pending files have not been removed.'
        );
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw handoffHttpError(
            response.status,
            data,
            'Katcha could not process the pending handoff files.',
        );
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
function selectedUsage() {
    const advanced = $('usage').value;
    if (advanced) return advanced;
    return document.querySelector('input[name="source-purpose"]:checked')?.value || 'candidate_review';
}
function afterSaveMode() {
    return document.querySelector('input[name="after-save"]:checked')?.value || 'save';
}
function adapterSupportsImports(row) {
    return adapters.some(a => a.key === row.adapter_key && a.version === row.adapter_version && a.supports_imports);
}
function targetSummary(row) {
    if (!row) return 'No target';
    if (row.query_template?.channel_reference) return row.query_template.channel_reference;
    if (row.query_template?.q) return row.query_template.q;
    if (row.query_template?.subreddit) return 'r/' + row.query_template.subreddit;
    if (row.query_template?.feed_url) {
        try { return new URL(row.query_template.feed_url).hostname; } catch {}
        return row.query_template.feed_url;
    }
    if (row.adapter_key === 'operator_feed') return 'Link collection';
    return row.platform || row.adapter_key;
}
function safeExternalUrl(value) {
    try {
        const url = new URL(value);
        return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : '';
    } catch { return ''; }
}
function formatWhen(value) {
    if (!value) return 'Never';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Unknown' : date.toLocaleString();
}
function runDiagnostics(run, selectedSource) {
    return [
        `Run ID: ${run.id}`,
        `Source: ${selectedSource?.name || 'Unknown'}`,
        `Adapter: ${selectedSource ? `${selectedSource.adapter_key}@${selectedSource.adapter_version}` : 'Unknown'}`,
        `Status: ${run.status || 'unknown'}`,
        `Created: ${run.created_at || 'unknown'}`,
        '',
        run.error || 'No technical error was recorded.',
    ].join('\n');
}
function copyText(text) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand('copy');
    field.remove();
    return copied ? Promise.resolve() : Promise.reject(new Error('Copy failed'));
}
function connectionName(row) {
    if (row.adapter_key === 'youtube' && row.query_template?.channel_reference) {
        return methods.youtube_channel.title;
    }
    return Object.values(methods).find(m => m.key === row.adapter_key)?.title || 'Custom connection';
}
function sourceChannel(row) { return channels.find(c => c.id === row.channel_profile_id); }
function channelHelp() {
    if (channels.length) {
        return 'Choose a channel to keep this source channel-specific, or choose “Shared collection (unassigned)” to collect discoveries without assigning a channel.';
    }
    return 'No active channels are set up yet. This source will stay in an unassigned shared collection until you choose a channel.';
}
function sourceViewFromHash() {
    const hash = location.hash.replace(/^#/, '');
    if (hash === 'sources') return 'library';
    if (hash === 'add') return 'add';
    if (hash === 'handoff') return 'handoff';
    return null;
}
function setSourceView(view, {focus = false, updateHash = true} = {}) {
    const selected = ['library', 'add', 'handoff'].includes(view) ? view : 'library';
    sourceView = selected;
    document.querySelectorAll('[data-source-tab]').forEach(button => {
        const active = button.dataset.sourceTab === selected;
        button.setAttribute('aria-selected', active ? 'true' : 'false');
        button.tabIndex = active ? 0 : -1;
        if (active && focus) button.focus();
    });
    document.querySelectorAll('[data-source-view]').forEach(section => {
        section.hidden = section.dataset.sourceView !== selected;
    });
    if (updateHash) {
        const hash = selected === 'library' ? '#sources' : selected === 'add' ? '#add' : '#handoff';
        window.history.replaceState(null, '', location.pathname + location.search + hash);
    }
    if (selected === 'handoff' && !$('workspace').disabled) {
        refreshHandoffInbox().catch(error => message(error.message, true));
    }
}
function installSourceWorkspaceTabs() {
    const tabs = [...document.querySelectorAll('[data-source-tab]')];
    tabs.forEach((button, index) => {
        button.addEventListener('click', () => setSourceView(button.dataset.sourceTab));
        button.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            let next = index;
            if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
            if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
            if (event.key === 'Home') next = 0;
            if (event.key === 'End') next = tabs.length - 1;
            setSourceView(tabs[next].dataset.sourceTab, {focus: true});
        });
    });
    window.addEventListener('hashchange', () => {
        const view = sourceViewFromHash();
        if (view) setSourceView(view, {updateHash: false});
    });
    $('empty-add-source')?.addEventListener('click', () => setSourceView('add', {focus: true}));
}

installSourceWorkspaceTabs();

function showStep(next) {
    step = next;
    for (let n = 1; n <= 3; n++) $('step-' + n).hidden = n !== next;
    document.querySelectorAll('.steps li').forEach((li, i) => {
        if (i + 1 === next) li.setAttribute('aria-current', 'step');
        else li.removeAttribute('aria-current');
    });
    $('step-label').textContent = `${next} OF 3`;
    $('builder-title').textContent = editingSource
        ? ['Edit source', 'Edit source', 'Review changes'][next - 1]
        : ['Choose a source', 'Source details', 'Review source'][next - 1];
    $('cancel-source-edit').hidden = !editingSource;
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
    $('search-fields').hidden = !['youtube', 'reddit', 'scout'].includes(method);
    $('search').placeholder = method === 'scout' ? 'For example, funny gaming clips from new creators' : 'For example, Xbox game announcements';
    $('scout-fields').hidden = method !== 'scout';
    if (channelsReady) $('channel-help').textContent = channelHelp();
    $('youtube-channel-fields').hidden = method !== 'youtube_channel';
    const purpose = method === 'youtube_channel' ? 'discovery_only' : 'candidate_review';
    const purposeInput = document.querySelector('input[name="source-purpose"][value="' + purpose + '"]');
    if (purposeInput) purposeInput.checked = true;
    $('usage').value = '';
    $('usage-help').textContent = '';
    $('reddit-fields').hidden = method !== 'reddit';
    $('feed-fields').hidden = method !== 'feed';
    $('custom-fields').hidden = method !== 'custom';
    $('custom-query').value = '{}';
    const canRunAfterSave = method !== 'links' && !a.supports_imports;
    $('after-save-fields').hidden = !canRunAfterSave;
    const after = document.querySelector('input[name="after-save"][value="' + (canRunAfterSave ? 'run' : 'save') + '"]');
    if (after) after.checked = true;
    showStep(2);
}
function sourceMethod(row) {
    if (!row) return '';
    if (row.adapter_key === 'operator_feed') return 'links';
    if (row.adapter_key === 'web_scout') return 'scout';
    if (row.adapter_key === 'rss_atom') return 'feed';
    if (row.adapter_key === 'reddit') return 'reddit';
    if (row.adapter_key === 'youtube') {
        return row.query_template?.channel_reference ? 'youtube_channel' : 'youtube';
    }
    return 'custom';
}

function resetSourceBuilder() {
    editingSource = null;
    selectedMethod = '';
    $('name').value = '';
    $('search').value = '';
    $('youtube-channel').value = '';
    $('community').value = '';
    $('feed').value = '';
    $('custom-query').value = '{}';
    $('scout-platforms').value = 'all';
    $('usage').value = '';
    const purpose = document.querySelector('input[name="source-purpose"][value="candidate_review"]');
    if (purpose) purpose.checked = true;
    const after = document.querySelector('input[name="after-save"][value="run"]');
    if (after) after.checked = true;
    $('cancel-source-edit').hidden = true;
    showStep(1);
}

function beginAddSource() {
    resetSourceBuilder();
    setSourceView('add', {focus: true});
}

function beginEditSource(row) {
    if (!row) return;
    editingSource = {
        ...row,
        query_template: {...(row.query_template || {})},
        source_metadata: {...(row.source_metadata || {})},
        default_candidate_metadata: {...(row.default_candidate_metadata || {})},
    };
    const method = sourceMethod(row);
    if (method === 'custom') {
        $('custom-adapter').value = row.adapter_key + '@' + row.adapter_version;
    }
    choose(method);
    $('name').value = row.name || '';
    $('channel').value = row.channel_profile_id || '';

    if (['candidate_review', 'discovery_only'].includes(row.usage_mode)) {
        const purpose = document.querySelector(
            'input[name="source-purpose"][value="' + row.usage_mode + '"]',
        );
        if (purpose) purpose.checked = true;
        $('usage').value = '';
    } else {
        const purpose = document.querySelector(
            'input[name="source-purpose"][value="candidate_review"]',
        );
        if (purpose) purpose.checked = true;
        $('usage').value = row.usage_mode || '';
    }
    $('usage-help').textContent = usage[row.usage_mode]?.[1] || '';

    const query = row.query_template || {};
    $('search').value = query.q || '';
    $('youtube-channel').value = query.channel_reference || '';
    $('community').value = query.subreddit ? 'r/' + query.subreddit : '';
    $('feed').value = query.feed_url || '';
    $('custom-query').value = JSON.stringify(query, null, 2);

    const platforms = Array.isArray(query.platforms) ? query.platforms : [];
    if (platforms.length === 1 && ['tiktok', 'instagram', 'x', 'bluesky'].includes(platforms[0])) {
        $('scout-platforms').value = platforms[0];
    } else if (
        ['tiktok', 'instagram', 'x', 'bluesky'].every(value => platforms.includes(value))
    ) {
        $('scout-platforms').value = 'social';
    } else {
        $('scout-platforms').value = 'all';
    }

    $('after-save-fields').hidden = true;
    showStep(2);
    setSourceView('add', {focus: true});
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
    if (['youtube', 'reddit', 'scout'].includes(selectedMethod)) {
        const q = $('search').value.trim();
        if (!q) throw new Error('Enter a topic for Katcha to search for.');
        query = {q, limit: 25, freshness_horizon_hours: 72};
        if (selectedMethod === 'youtube') query.order = 'date';
        else if (selectedMethod === 'scout') {
            const choice = $('scout-platforms').value;
            query = {q, limit: 40, freshness_horizon_hours: 72,
                ...(choice === 'all' ? {} : {platforms: choice === 'social' ? ['tiktok', 'instagram', 'x', 'bluesky'] : [choice]})};
            platform = 'web';
        } else {
            const subreddit = $('community').value.trim().replace(/^\/?r\//, '').replace(/\/$/, '');
            if (subreddit && !/^[A-Za-z0-9_]+$/.test(subreddit)) throw new Error('Enter a community name such as gaming or r/gaming, rather than a full web address.');
            query = {...query, subreddit, sort: 'new', time_filter: 'week'};
        }
    }
    if (selectedMethod === 'youtube_channel') {
        const reference = $('youtube-channel').value.trim();
        if (!reference) throw new Error('Enter the YouTube channel’s @handle or channel link.');
        if (!/^@[\S]+$/.test(reference) && !/^UC[A-Za-z0-9_-]{22}$/.test(reference) && !/^https:\/\/(www\.|m\.)?youtube\.com\/(@[^/?#]+|channel\/UC[A-Za-z0-9_-]{22})(\/[^?#]*)?(\?[^#]*)?$/.test(reference)) throw new Error('Use a YouTube @handle, /@handle link, or /channel/ link rather than a video link.');
        platform = 'youtube';
        query = {channel_reference: reference, order: 'date', limit: 25, freshness_horizon_hours: 720};
    }
    if (selectedMethod === 'feed') { platform = 'web'; query = {feed_url: httpUrl($('feed').value.trim(), 'The website feed'), limit: 50}; }
    if (selectedMethod === 'custom') {
        try { query = JSON.parse($('custom-query').value); } catch { throw new Error('The custom connection settings are not valid JSON.'); }
        if (!query || Array.isArray(query) || typeof query !== 'object') throw new Error('Custom connection settings must be a JSON object.');
        platform = a.supported_platforms[0] || 'custom';
    }
    const payload = {
        create_only: !editingSource,
        name,
        adapter_key: a.key,
        adapter_version: a.version,
        platform,
        channel_profile_id: $('channel').value || null,
        usage_mode: selectedUsage(),
        query_template: query,
        default_candidate_metadata: editingSource
            ? {...(editingSource.default_candidate_metadata || {})}
            : {},
        poll_interval_minutes: editingSource?.poll_interval_minutes || 60,
        source_metadata: {
            ...(editingSource?.source_metadata || {}),
            execution_mode: 'manual',
        },
        enabled: editingSource?.enabled ?? true,
    };
    if (editingSource) payload.source_key = editingSource.source_key;
    return payload;
}
function review() {
    const d = details();
    const immediate = !$('after-save-fields').hidden && afterSaveMode() === 'run';
    const rows = [
        ['Name', d.name],
        ['Source type', methods[selectedMethod]?.title || chosenAdapter().label],
        ['Used by', channels.find(c => c.id === d.channel_profile_id) ? channelName(channels.find(c => c.id === d.channel_profile_id)) : 'Shared collection (unassigned)'],
        ['Purpose', usage[d.usage_mode]?.[0] || d.usage_mode],
    ];
    if (d.query_template.channel_reference) rows.push(['YouTube channel', d.query_template.channel_reference]);
    if (d.query_template.q) rows.push(['Search topic', d.query_template.q]);
    if (d.query_template.platforms) rows.push(['Search area', d.query_template.platforms.join(', ')]);
    if (d.query_template.subreddit) rows.push(['Community', 'r/' + d.query_template.subreddit]);
    if (d.query_template.feed_url) rows.push(['Website feed', d.query_template.feed_url]);
    rows.push(['After saving', selectedMethod === 'links' ? 'Save the collection, then paste links.' : immediate ? 'Save the source and run one check immediately.' : 'Save the source only. No search starts.']);
    $('review').innerHTML = rows.map(([label, value]) => '<dt>' + esc(label) + '</dt><dd>' + esc(value) + '</dd>').join('');
    $('save').textContent = editingSource
        ? 'Save changes'
        : selectedMethod === 'links' || !immediate
            ? 'Save source'
            : 'Save & check now';
    $('save-explanation').textContent = editingSource
        ? 'Katcha will update this source in place. Existing checks, finds, and source history are preserved.'
        : selectedMethod === 'links'
            ? 'Saving creates the source only. Add links from the Source Library when you are ready.'
            : immediate
                ? 'Katcha will save this source, then start one check. This does not create a recurring schedule.'
                : 'Katcha will save this source without starting a search or recurring schedule.';
    if (selectedMethod === 'scout') $('save-explanation').textContent += ' Web scouting can use live AI and public web search; provider charges may apply when API fallback is used.';
    showStep(3);
}
async function loadChannels(epoch = connectionEpoch) {
    try {
        const rows = await api('channels');
        if (epoch !== connectionEpoch) return;
        channels = rows.filter(c => c.status === 'active');
        channelsReady = true;
        const previous = $('channel').value;
        $('channel').innerHTML = '<option value="">Shared collection (unassigned)</option>' + channels.map(c => `<option value="${esc(c.id)}">${esc(channelName(c))}</option>`).join('');
        if (channels.some(c => c.id === previous)) $('channel').value = previous;
        else if (channels.some(c => c.id === requestedChannel)) $('channel').value = requestedChannel;
        if ($('channel').value) sessionStorage.setItem("katcha.channel", $('channel').value);
        $('channel-area').hidden = false;
        $('channel-help').textContent = channelHelp();
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
        renderLibraryFilters();
        await refreshSources();
        if (epoch !== connectionEpoch) return;
        const requestedView = sourceViewFromHash();
        setSourceView(requestedView || (sourcePage.total ? 'library' : 'add'), {updateHash: false});
        $('workspace').disabled = false;
        if (sourceView === 'handoff') await refreshHandoffInbox();
        if (location.port !== '8765') $('connection').textContent = 'Connected';
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
function renderLibraryFilters() {
    const currentChannel = $('source-channel-filter').value || 'all';
    $('source-channel-filter').innerHTML =
        '<option value="all">All channels</option>' +
        '<option value="shared">Shared / unassigned</option>' +
        channels.map(row => '<option value="channel:' + esc(row.id) + '">' + esc(channelName(row)) + '</option>').join('');
    if ([...$('source-channel-filter').options].some(option => option.value === currentChannel)) {
        $('source-channel-filter').value = currentChannel;
    }

    const currentType = $('source-type-filter').value || 'all';
    const typeRows = [...new Map(adapters.map(row => [row.key, row])).values()]
        .sort((a, b) => String(a.label || a.key).localeCompare(String(b.label || b.key)));
    $('source-type-filter').innerHTML =
        '<option value="all">All types</option>' +
        typeRows.map(row => '<option value="' + esc(row.key) + '">' + esc(row.label || row.key) + '</option>').join('');
    if ([...$('source-type-filter').options].some(option => option.value === currentType)) {
        $('source-type-filter').value = currentType;
    }
}

function libraryQuery() {
    const params = new URLSearchParams({
        limit: String(sourcePage.limit),
        offset: String(sourcePage.offset),
        sort: $('source-sort').value || 'recent',
    });
    const q = $('source-search').value.trim();
    if (q) params.set('q', q);

    const channelFilter = $('source-channel-filter').value;
    if (channelFilter === 'shared') params.set('shared_only', 'true');
    else if (channelFilter?.startsWith('channel:')) {
        params.set('channel_profile_id', channelFilter.slice('channel:'.length));
    }

    const type = $('source-type-filter').value;
    if (type && type !== 'all') params.set('adapter_key', type);

    const state = $('source-status-filter').value;
    if (state === 'active') params.set('enabled', 'true');
    if (state === 'paused') params.set('enabled', 'false');

    const purpose = $('source-purpose-filter').value;
    if (purpose && purpose !== 'all') params.set('usage_mode', purpose);
    return params;
}

function sourceIcon(row) {
    if (row.adapter_key === 'youtube') return '▶';
    if (row.adapter_key === 'reddit') return '◎';
    if (row.adapter_key === 'rss_atom') return '≋';
    if (row.adapter_key === 'web_scout') return '✦';
    if (row.adapter_key === 'operator_feed') return '↗';
    return '◇';
}

function sourceScopeLabel(row) {
    if (!row.channel_profile_id) return 'Shared';
    return channelName(sourceChannel(row) || {profile_metadata: {name: 'Assigned channel'}});
}

function renderSourceList() {
    const selectedId = $('source').value;
    const start = sourcePage.total ? sourcePage.offset + 1 : 0;
    const finish = Math.min(sourcePage.offset + sources.length, sourcePage.total);
    $('source-count').textContent = sourcePage.total + ' source' + (sourcePage.total === 1 ? '' : 's');
    $('source-page-label').textContent = start + '–' + finish;
    $('source-prev').disabled = sourcePage.offset <= 0;
    $('source-next').disabled = sourcePage.offset + sourcePage.limit >= sourcePage.total;

    $('source-list').innerHTML = sources.map(row => {
        const selected = row.id === selectedId;
        const purpose = usage[row.usage_mode]?.[0] || row.usage_mode;
        return '<button type="button" class="source-row" role="option" data-source-id="' + esc(row.id) + '" aria-selected="' + (selected ? 'true' : 'false') + '">' +
            '<span class="source-row-icon" aria-hidden="true">' + esc(sourceIcon(row)) + '</span>' +
            '<span class="source-row-copy"><strong>' + esc(row.name) + '</strong><small>' + esc(targetSummary(row)) + '</small><span>' +
                '<em>' + esc(sourceScopeLabel(row)) + '</em><em>' + esc(purpose) + '</em>' +
            '</span></span>' +
            '<span class="source-row-state ' + (row.enabled ? 'is-active' : 'is-paused') + '">' + (row.enabled ? 'Active' : 'Paused') + '</span>' +
        '</button>';
    }).join('');

    $('empty-sources').hidden = sourcePage.total > 0;
    $('source-controls').hidden = sourcePage.total === 0;
}

function renderFinds(page, row) {
    const findings = page.items || [];
    const startIndex = page.total ? page.offset + 1 : 0;
    const finish = Math.min(page.offset + findings.length, page.total);
    $('finds-count').textContent =
        page.total + ' find' + (page.total === 1 ? '' : 's');
    $('finds-page-label').textContent = startIndex + '–' + finish;
    $('finds-prev').disabled = page.offset <= 0;
    $('finds-next').disabled = page.offset + page.limit >= page.total;

    if (!findings.length) {
        $('all-finds').innerHTML =
            '<p class="source-empty-copy">' +
            ($('finds-search').value.trim()
                ? 'No finds match this search.'
                : 'Nothing found yet. Run a check to start building source history.') +
            '</p>';
        return;
    }
    const canPromote = !['discovery_only', 'blocked'].includes(row.usage_mode);
    $('all-finds').innerHTML = findings.map(item => {
        const candidate = item.candidate || {};
        const url = safeExternalUrl(candidate.source_url);
        return '<article class="source-find">' +
            '<div class="source-find-copy"><strong>' +
                esc(candidate.title || candidate.source_url || 'Untitled find') +
            '</strong><small>' +
                esc(candidate.creator || candidate.platform || 'Unknown creator') +
                ' · ' + esc(formatWhen(item.observed_at)) +
            '</small><span>' + esc(candidate.status || 'discovered') + '</span></div>' +
            '<div class="source-find-actions">' +
                (url ? '<a class="text-button" href="' + esc(url) +
                    '" target="_blank" rel="noopener noreferrer">Open ↗</a>' : '') +
                (canPromote && candidate.id
                    ? '<button type="button" class="text-button" data-add-clip="' +
                        esc(candidate.id) + '">Add to Clips</button>'
                    : '') +
            '</div>' +
        '</article>';
    }).join('');
}

async function loadFinds({reset = false} = {}) {
    const row = source();
    if (!row) return;
    if (reset) {
        findsPage.offset = 0;
        $('finds-search').value = '';
    }
    const epoch = ++findsEpoch;
    const params = new URLSearchParams({
        limit: String(findsPage.limit),
        offset: String(findsPage.offset),
    });
    const q = $('finds-search').value.trim();
    if (q) params.set('q', q);
    $('all-finds').innerHTML = '<p class="source-empty-copy">Loading finds…</p>';
    const page = await api(
        'discovery/sources/' + encodeURIComponent(row.id) +
        '/finds?' + params.toString(),
    );
    if (epoch !== findsEpoch || source()?.id !== row.id) return;
    findsPage = page;
    renderFinds(page, row);
}

function renderHistory(overview, row) {
    const rows = overview.recent_runs || [];
    const labels = {
        queued: 'Ready to start',
        running: 'Finding content…',
        completed: 'Finished',
        failed: 'Failed',
    };
    $('history').innerHTML = rows.length ? rows.map(run => {
        const diagnostics = runDiagnostics(run, row);
        return '<article class="source-run is-' + esc(run.status || 'unknown') + '">' +
            '<div class="source-run-head"><div><strong>' + esc(labels[run.status] || 'Status unavailable') + '</strong><small>' + esc(formatWhen(run.created_at)) + '</small></div>' +
            '<span>' + esc(run.status || 'unknown') + '</span></div>' +
            (run.status === 'failed' && run.error ? '<p class="source-run-error">' + esc(run.error) + '</p>' : '') +
            (run.status === 'completed' ? '<button type="button" class="text-button" data-results="' + esc(run.id) + '">Finds from this check</button><div class="run-results" role="status"></div>' : '') +
            (['queued', 'running'].includes(run.status) && row.enabled && row.usage_mode !== 'blocked'
                ? '<button type="button" class="button secondary" data-execute="' + esc(run.id) + '">' +
                    (run.status === 'running' ? 'Resume now' : 'Start now') + '</button>'
                : '') +
            (run.status === 'failed' && row.enabled && row.usage_mode !== 'blocked'
                ? '<button type="button" class="button secondary" data-restart="' + esc(run.id) + '">Restart check</button>'
                : '') +
            (run.error ? '<details class="run-diagnostics"><summary>Technical details</summary><pre>' + esc(diagnostics) + '</pre><button type="button" class="text-button diagnostics-copy" data-copy-diagnostics>Copy diagnostics</button><span class="diagnostics-copy-status" role="status"></span></details>' : '') +
        '</article>';
    }).join('') : '<p class="source-empty-copy">No checks have run yet.</p>';
}

function renderSourceOverview() {
    const overview = selectedOverview;
    const row = overview?.source;
    if (!row) return;

    const canImport = adapterSupportsImports(row);
    const usable = row.enabled && row.usage_mode !== 'blocked';
    const latest = overview.recent_runs?.[0] || null;
    const latestFailed = latest?.status === 'failed' ? latest : null;

    $('source-inspector-empty').hidden = true;
    $('source-inspector-content').hidden = false;
    $('source-name').textContent = row.name;
    $('source-subtitle').textContent = connectionName(row) + ' · ' + targetSummary(row);
    $('source-platform-badge').textContent = String(row.platform || row.adapter_key || 'source').toUpperCase();
    $('source-state-badge').textContent = row.enabled ? 'Active' : 'Paused';
    $('source-state-badge').className = 'source-state-badge ' + (row.enabled ? 'is-active' : 'is-paused');

    $('run').hidden = canImport || !usable;
    $('run').textContent = row.query_template?.channel_reference
        ? 'Check channel now'
        : row.adapter_key === 'rss_atom'
            ? 'Check for updates'
            : 'Search now';
    $('pause-source').hidden = row.usage_mode === 'blocked';
    $('pause-source').textContent = row.enabled ? 'Pause' : 'Resume';
    $('import').hidden = !canImport || !usable;

    $('stat-finds').textContent = String(overview.unique_candidate_count || 0);
    $('stat-finds-note').textContent = (overview.discovery_count || 0) + ' total observations';
    $('stat-runs').textContent = String(overview.run_count || 0);
    $('stat-runs-note').textContent = (overview.failed_runs || 0) + ' failed';
    $('stat-success').textContent = overview.success_rate == null ? '—' : Math.round(overview.success_rate * 100) + '%';
    $('stat-last').textContent = latest ? formatWhen(latest.created_at) : 'Never';
    $('stat-last-note').textContent = latest ? (latest.status || 'unknown') : 'No checks yet';

    $('source-alert').hidden = !latestFailed;
    if (latestFailed) {
        $('source-alert').innerHTML =
            '<strong>Latest check failed</strong><p>' + esc(latestFailed.error || 'No provider detail was recorded.') + '</p>' +
            '<small>Edit the source if its configuration is wrong, or restart the failed check after the issue is fixed.</small>';
    } else {
        $('source-alert').textContent = '';
    }

    const usedBy = overview.channel_name || (row.channel_profile_id ? 'Assigned channel' : 'Shared collection (unassigned)');
    const purpose = usage[row.usage_mode]?.[0] || row.usage_mode;
    const purposeHelp = usage[row.usage_mode]?.[1] || '';
    $('source-info').innerHTML =
        '<dt>Used by</dt><dd>' + esc(usedBy) + '</dd>' +
        '<dt>Purpose</dt><dd><strong>' + esc(purpose) + '</strong><small>' + esc(purposeHelp) + '</small></dd>' +
        '<dt>Watches</dt><dd>' + esc(targetSummary(row)) + '</dd>' +
        '<dt>Source type</dt><dd>' + esc(connectionName(row)) + '</dd>' +
        '<dt>Checking</dt><dd>Manual checks<small>Saving a source does not create a recurring schedule.</small></dd>';

    $('operation-help').textContent = !usable
        ? 'This source is paused or blocked.'
        : canImport
            ? 'Paste known links below when you want Katcha to inspect them.'
            : 'This source checks only when you choose the action above. Recurring source schedules are configured separately.';

    renderHistory(overview, row);
}

async function loadOverview() {
    const row = source();
    const epoch = ++detailEpoch;
    if (!row) {
        selectedOverview = null;
        $('source-inspector-empty').hidden = false;
        $('source-inspector-content').hidden = true;
        return;
    }
    $('source-inspector-empty').hidden = false;
    $('source-inspector-empty').querySelector('h3').textContent = 'Loading source…';
    $('source-inspector-empty').querySelector('p').textContent = 'Fetching source health and recent finds.';
    $('source-inspector-content').hidden = true;
    try {
        const overview = await api('discovery/sources/' + encodeURIComponent(row.id) + '/overview');
        if (epoch !== detailEpoch || source()?.id !== row.id) return;
        selectedOverview = overview;
        const index = sources.findIndex(item => item.id === row.id);
        if (index >= 0) sources[index] = overview.source;
        $('source').value = overview.source.id;
        renderSourceList();
        renderSourceOverview();
    } catch (error) {
        if (epoch !== detailEpoch) return;
        $('source-inspector-empty').hidden = false;
        $('source-inspector-content').hidden = true;
        $('source-inspector-empty').querySelector('h3').textContent = 'Source details unavailable';
        $('source-inspector-empty').querySelector('p').textContent = error.message;
    }
}

function renderHandoffInbox() {
    const counts = handoffInbox.counts || {};
    $('handoff-count-incoming').textContent = String(counts.incoming || 0);
    $('handoff-count-processed').textContent = String(counts.processed || 0);
    $('handoff-count-failed').textContent = String(counts.failed || 0);
    $('handoff-path').textContent = handoffInbox.incoming_path || 'handoff/incoming';

    const items = handoffInbox.items || [];
    $('handoff-list').innerHTML = items.length ? items.map(item => {
        const state = item.status || 'unknown';
        const summary = item.batch_key
            ? esc(item.batch_key) + (item.record_count == null ? '' : ' · ' + esc(item.record_count) + ' records')
            : 'Batch details unavailable';
        const knownChannel = channels.find(row => row.id === item.channel_profile_id);
        const channel = item.channel_profile_id
            ? '<small>Channel ' + esc(knownChannel ? channelName(knownChannel) : item.channel_profile_id) + '</small>'
            : '';
        const error = item.error ? '<p class="handoff-error">' + esc(item.error) + '</p>' : '';
        const countsText = item.receipt && state === 'processed'
            ? '<small>' + esc(item.receipt.created_count || 0) + ' created · ' +
                esc(item.receipt.updated_count || 0) + ' updated' +
                (item.receipt.replayed ? ' · replayed safely' : '') + '</small>'
            : '';
        return '<article class="handoff-item is-' + esc(state) + '">' +
            '<div class="handoff-item-head"><div><strong>' + esc(item.filename) + '</strong>' +
            '<span>' + summary + '</span>' + channel + countsText + '</div>' +
            '<div><span class="handoff-status">' + esc(state) + '</span><small>' + esc(formatWhen(item.modified_at)) + '</small></div></div>' +
            error +
        '</article>';
    }).join('') : '<div class="empty-state handoff-empty"><span aria-hidden="true">⇢</span><h3>No handoffs yet</h3><p>Import a batch file here, or drop one into the local inbox folder.</p></div>';
}

async function refreshHandoffInbox() {
    handoffInbox = await api('intelligence-ingest/inbox?limit=100');
    renderHandoffInbox();
}

async function refreshSources({selectId = null, preserveSelection = true} = {}) {
    const epoch = ++libraryEpoch;
    const previous = selectId || (preserveSelection ? $('source').value : '');
    const page = await api('discovery/source-library?' + libraryQuery().toString());
    if (epoch !== libraryEpoch) return;

    sourcePage = page;
    sources = page.items || [];
    $('source').innerHTML = sources.map(row => '<option value="' + esc(row.id) + '">' + esc(row.name) + '</option>').join('');

    const selected = sources.some(row => row.id === previous)
        ? previous
        : (sources[0]?.id || '');
    $('source').value = selected;
    renderSourceList();

    if (selected) {
        await selectSource(selected);
    } else {
        selectedOverview = null;
        ++detailEpoch;
        $('source-inspector-empty').hidden = false;
        $('source-inspector-content').hidden = true;
        $('source-inspector-empty').querySelector('h3').textContent = 'No source selected';
        $('source-inspector-empty').querySelector('p').textContent = sourcePage.total
            ? 'Choose a source from this page.'
            : 'Adjust the filters or add a source.';
    }
}

async function selectSource(sourceId = $('source').value) {
    const next = sourceId || '';
    if (displayedSourceId !== next) {
        if (displayedSourceId) linkDrafts.set(displayedSourceId, $('urls').value);
        displayedSourceId = next || null;
        $('urls').value = linkDrafts.get(displayedSourceId) || '';
    }
    if (next && $('source').value !== next) $('source').value = next;
    if (next) sessionStorage.setItem('katcha.sourceId', next);
    renderSourceList();
    await loadOverview();
    await loadFinds({reset: true});
}

async function loadHistory() {
    await loadOverview();
    await loadFinds();
}
async function start(run) {
    try { await api(`discovery/runs/${encodeURIComponent(run.id)}/execute`, {}); }
    catch {
        await loadHistory();
        throw new Error('Your request is saved, but Katcha could not confirm that the content check started. Refresh activity; if it says “Ready to start,” choose “Start now” to retry without adding it again.');
    }
    for (const [key, id] of runIntents) {
        if (id === run.id) { intents.delete(key); runIntents.delete(key); }
    }
    await loadHistory();
    message('Request sent. Refresh activity to see its progress. Nothing has been published.');
}
function resetLibraryFilters() {
    sourcePage.offset = 0;
    $('source-search').value = '';
    $('source-channel-filter').value = 'all';
    $('source-type-filter').value = 'all';
    $('source-status-filter').value = 'all';
    $('source-purpose-filter').value = 'all';
    $('source-sort').value = 'recent';
}

async function runSelectedSource() {
    const s = source();
    if (!s?.enabled || s.usage_mode === 'blocked') return;
    const key = 'search:' + s.id;
    const run = await api(
        'discovery/sources/' + encodeURIComponent(s.id) + '/runs',
        {idempotency_key: intent(key, 'search')},
    );
    runIntents.set(key, run.id);
    await start(run);
}

async function handleInspectorClick(e) {
    const summary = e.target.closest('summary');
    if (summary) return;

    const copy = e.target.closest('[data-copy-diagnostics]');
    if (copy) {
        e.preventDefault();
        const details = copy.closest('details');
        const status = details?.querySelector('.diagnostics-copy-status');
        const diagnosticText = details?.querySelector('pre')?.textContent || '';
        try {
            await copyText(diagnosticText);
            if (status) status.textContent = 'Copied.';
        } catch {
            if (status) status.textContent = 'Copy failed. Select the text above manually.';
        }
        return;
    }

    const execute = e.target.closest('[data-execute]');
    if (execute) {
        e.preventDefault();
        if (execute.disabled) return;
        execute.disabled = true;
        try { await start({id: execute.dataset.execute}); }
        catch (error) { message(error.message, true); }
        finally { if (execute.isConnected) execute.disabled = false; }
        return;
    }

    const restart = e.target.closest('[data-restart]');
    if (restart) {
        e.preventDefault();
        if (restart.disabled) return;
        restart.disabled = true;
        const key = 'restart:' + restart.dataset.restart;
        try {
            const result = await api(
                'discovery/runs/' + encodeURIComponent(restart.dataset.restart) + '/restart',
                {idempotency_key: intent(key, 'restart')},
            );
            intents.delete(key);
            await loadHistory();
            message(
                'Restarted failed check as a new attempt. Previous history was preserved.',
            );
        } catch (error) {
            message(error.message, true);
            restart.disabled = false;
        }
        return;
    }

    const addClip = e.target.closest('[data-add-clip]');
    if (addClip) {
        e.preventDefault();
        if (addClip.disabled) return;
        addClip.disabled = true;
        try {
            await api(
                'discovery/candidates/' + encodeURIComponent(addClip.dataset.addClip) + '/promote',
                {actor: 'operator', for_review: true},
            );
            addClip.textContent = 'Added to Clips';
            message('Find added to Clips for review. Nothing has been published.');
        } catch (error) {
            message(error.message, true);
            addClip.disabled = false;
        }
        return;
    }

    const results = e.target.closest('[data-results]');
    if (!results) return;
    e.preventDefault();
    const output = results.nextElementSibling;
    const selected = source();
    if (!selected || !output) return;
    output.textContent = 'Loading discoveries…';
    try {
        const data = await api(
            'discovery/sources/' + encodeURIComponent(selected.id) +
            '/runs/' + encodeURIComponent(results.dataset.results) + '/results',
        );
        if (source()?.id !== selected.id || !output.isConnected) return;
        const count = Number(data.total) || 0;
        output.innerHTML = count
            ? '<p>' + count + ' ' + (count === 1 ? 'item' : 'items') +
                ' found. Showing up to five.</p><ul>' +
                data.candidates.map(candidate => {
                    const safe = safeExternalUrl(candidate.source_url);
                    const canPromote = candidate.id &&
                        selected.usage_mode !== 'discovery_only' &&
                        selected.usage_mode !== 'blocked';
                    return '<li>' +
                        (safe
                            ? '<a href="' + esc(safe) + '" target="_blank" rel="noopener noreferrer">' +
                                esc(candidate.title || candidate.source_url) + '</a>'
                            : esc(candidate.title || 'Source link unavailable')) +
                        (candidate.creator ? ' · ' + esc(candidate.creator) : '') +
                        (canPromote
                            ? ' <button type="button" class="text-button" data-add-clip="' +
                                esc(candidate.id) + '">Add to Clips</button>'
                            : '') +
                        '</li>';
                }).join('') +
                '</ul>'
            : '<p>No new items were found in this check.</p>';
    } catch {
        if (output.isConnected) output.textContent = 'Results could not be loaded. Try again.';
    }
}

bind('handoff-upload', 'submit', async () => {
    const file = $('handoff-file').files?.[0];
    if (!file) throw new Error('Choose a Katcha intelligence batch JSON file.');
    if (!file.name.toLowerCase().endsWith('.json')) throw new Error('Choose a .json handoff file.');
    if (file.size > 10 * 1024 * 1024) throw new Error('Handoff files must be 10 MiB or smaller.');

    try {
        const result = await handoffUpload(file);
        await refreshHandoffInbox();
        if (result.status === 'failed') {
            handoffProgress(
                'Batch failed validation',
                result.error || 'Katcha retained the file in the failed queue for review.',
                100,
                'error',
            );
            throw new Error(result.error || 'Katcha retained the file in the failed queue for review.');
        }
        $('handoff-file').value = '';
        handoffProgress(
            'Batch imported',
            (result.record_count || 0) + ' intelligence records are now available to Katcha.',
            100,
            'success',
        );
        message(
            'Imported “' + result.filename + '”: ' +
            (result.record_count || 0) + ' intelligence records are now available to Katcha.',
        );
    } catch (error) {
        handoffProgress(
            'Import failed',
            error.message,
            100,
            'error',
        );
        throw error;
    }
});

bind('handoff-process', 'click', async () => {
    try {
        const results = await handoffProcessPending();
        await refreshHandoffInbox();
        const failed = results.filter(item => item.status === 'failed').length;
        const processed = results.length - failed;
        const detail = results.length
            ? processed + ' processed' + (failed ? '; ' + failed + ' failed and were retained.' : '.')
            : 'No pending handoff files were found.';
        handoffProgress(
            failed ? 'Pending processing completed with failures' : 'Pending processing complete',
            detail,
            100,
            failed ? 'error' : 'success',
        );
        message(
            results.length
                ? processed + ' handoff' + (processed === 1 ? '' : 's') + ' processed' +
                    (failed ? '; ' + failed + ' failed and were retained for review.' : '.')
                : 'No pending handoff files were found.',
            failed > 0,
        );
    } catch (error) {
        handoffProgress('Pending processing failed', error.message, 100, 'error');
        throw error;
    }
});
bind('handoff-refresh', 'click', refreshHandoffInbox);
$('handoff-file').addEventListener('change', () => {
    const file = $('handoff-file').files?.[0];
    if (!file) {
        $('handoff-progress').hidden = true;
        return;
    }
    handoffProgress(
        'Ready to import “' + file.name + '”',
        file.size.toLocaleString() + ' bytes selected. Nothing has been sent yet.',
        0,
    );
});

bind('connect', 'submit', connect);
bind('methods', 'click', e => {
    const choice = e.target.closest('[data-method]');
    if (choice) choose(choice.dataset.method);
}, true);
bind('choose-custom', 'click', () => choose('custom'), true);
bind('back', 'click', () => showStep(step - 1));
bind('next', 'click', review, true);
bind('usage', 'change', () => {
    const advanced = $('usage').value;
    $('usage-help').textContent = advanced ? usage[advanced][1] : '';
});
bind('retry-channels', 'click', async () => {
    await loadChannels();
    renderLibraryFilters();
    await refreshSources();
});
bind('source', 'change', () => selectSource());
bind('refresh', 'click', () => refreshSources());
bind('history-refresh', 'click', loadHistory);

$('header-add-source').addEventListener('click', beginAddSource);
$('cancel-source-edit').addEventListener('click', () => {
    resetSourceBuilder();
    setSourceView('library', {focus: true});
});
$('edit-source').addEventListener('click', () => beginEditSource(source()));
$('source-list').addEventListener('click', async event => {
    const row = event.target.closest('[data-source-id]');
    if (!row) return;
    await selectSource(row.dataset.sourceId);
});

$('source-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
        sourcePage.offset = 0;
        try { await refreshSources({preserveSelection: false}); }
        catch (error) { message(error.message, true); }
    }, 250);
});
for (const id of [
    'source-channel-filter',
    'source-type-filter',
    'source-status-filter',
    'source-purpose-filter',
    'source-sort',
]) {
    $(id).addEventListener('change', async () => {
        sourcePage.offset = 0;
        try { await refreshSources({preserveSelection: false}); }
        catch (error) { message(error.message, true); }
    });
}
$('finds-search').addEventListener('input', () => {
    clearTimeout(findsSearchTimer);
    findsSearchTimer = setTimeout(async () => {
        findsPage.offset = 0;
        try { await loadFinds(); }
        catch (error) { message(error.message, true); }
    }, 250);
});
$('finds-prev').addEventListener('click', async () => {
    findsPage.offset = Math.max(0, findsPage.offset - findsPage.limit);
    await loadFinds();
});
$('finds-next').addEventListener('click', async () => {
    if (findsPage.offset + findsPage.limit >= findsPage.total) return;
    findsPage.offset += findsPage.limit;
    await loadFinds();
});

$('source-prev').addEventListener('click', async () => {
    sourcePage.offset = Math.max(0, sourcePage.offset - sourcePage.limit);
    await refreshSources({preserveSelection: false});
});
$('source-next').addEventListener('click', async () => {
    if (sourcePage.offset + sourcePage.limit >= sourcePage.total) return;
    sourcePage.offset += sourcePage.limit;
    await refreshSources({preserveSelection: false});
});

document.querySelectorAll('input[name="source-purpose"]').forEach(input => {
    input.addEventListener('change', () => {
        if (input.checked) {
            $('usage').value = '';
            $('usage-help').textContent = usage[input.value]?.[1] || '';
        }
    });
});
document.querySelectorAll('input[name="after-save"]').forEach(input => {
    input.addEventListener('change', () => {
        if (step === 3) review();
    });
});

bind('setup', 'submit', async () => {
    if (step !== 3) {
        if (step === 2) review();
        return;
    }
    const d = details();
    const isEdit = Boolean(editingSource);
    const shouldRun = !isEdit && !$('after-save-fields').hidden && afterSaveMode() === 'run';

    if (isEdit) {
        const saved = await api('discovery/sources', d);
        resetLibraryFilters();
        resetSourceBuilder();
        await refreshSources({selectId: saved.id, preserveSelection: false});
        setSourceView('library');
        message(
            '“' + saved.name + '” updated. Existing checks and finds were preserved.',
        );
        return;
    }

    const signature = JSON.stringify(d);
    const source_key = intent(signature, 'source');

    // Recover a successful save whose response was lost without scanning the entire library.
    const recovery = await api(
        'discovery/source-library?q=' + encodeURIComponent(source_key) + '&limit=20&offset=0',
    );
    const existing = (recovery.items || []).find(row => row.source_key === source_key);
    const saved = existing || await api('discovery/sources', {...d, source_key});

    resetLibraryFilters();
    await refreshSources({selectId: saved.id, preserveSelection: false});
    resetSourceBuilder();
    setSourceView('library');
    intents.delete(signature);

    if (saved.adapter_key === 'operator_feed') {
        message('“' + saved.name + '” is saved. Paste links in the source inspector when you are ready.');
        return;
    }

    if (!shouldRun) {
        message('“' + saved.name + '” is saved. No check was started.');
        return;
    }

    await runSelectedSource();
    message('“' + saved.name + '” is saved and its first check has started.');
}, true);

bind('run', 'click', runSelectedSource);

bind('import', 'submit', async () => {
    const s = source();
    if (!s?.enabled || s.usage_mode === 'blocked') return;
    const urls = [...new Set(
        $('urls').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
    )];
    if (!urls.length) throw new Error('Paste at least one content link, one per line.');
    urls.forEach(url => httpUrl(url, 'Each content link'));
    const key = 'import:' + s.id + ':' + JSON.stringify(urls);
    const result = await api(
        'discovery/sources/' + encodeURIComponent(s.id) + '/imports',
        {batch_key: intent(key, 'links'), urls},
    );
    runIntents.set(key, result.discovery_run.id);
    await start(result.discovery_run);
    linkDrafts.set(s.id, '');
    if (displayedSourceId === s.id) $('urls').value = '';
    intents.delete(key);
});

$('history').addEventListener('click', handleInspectorClick);
$('all-finds').addEventListener('click', handleInspectorClick);

$('usage-help').textContent = '';
// The launcher bridge connects after startup. Direct API users connect automatically.
if (location.port !== '8765') connect();

$('pause-source').onclick = async () => {
    const selected = source();
    if (!selected) return;
    const button = $('pause-source');
    button.disabled = true;
    try {
        await api('discovery/sources', {
            ...selected,
            create_only: false,
            enabled: !selected.enabled,
        });
        await refreshSources({selectId: selected.id});
        message(
            selected.enabled
                ? 'Source paused. Manual and scheduled checks are disabled while paused.'
                : 'Source resumed. You can run a check when ready.',
        );
    } catch (error) {
        message(error.message, true);
    } finally {
        button.disabled = false;
    }
};
