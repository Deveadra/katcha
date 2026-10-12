/* Measured local footage only; reuses Editorial authentication and Media Bin scope. */
window.KatchaSceneIndex = (() => {
    const el = id => document.getElementById(id);
    let api, scope = '', context, epoch = 0, read = 0, index = null, busy = false, timer;
    const node = (tag, value) => { const result = document.createElement(tag); result.textContent = value; return result; };
    const itemId = () => el('editorial-footage-item').value;
    const base = () => `/v1/channels/${encodeURIComponent(context.channel)}/editorial-projects/${encodeURIComponent(context.project_id)}/media-bin`;
    const endpoint = () => `${base()}/${encodeURIComponent(itemId())}/scene-index`;
    function message(value) { el('editorial-footage-status').textContent = value; }
    function controls() {
        const selected = Boolean(itemId());
        const running = index?.status === 'indexing';
        el('editorial-footage-start').disabled = busy || !selected || running || index?.status === 'completed';
        el('editorial-footage-start').textContent = index?.status === 'failed' ? 'Retry footage indexing' : 'Index footage';
        el('editorial-footage-refresh').disabled = busy || !selected;
        el('editorial-footage-cancel').hidden = !running;
        el('editorial-footage-cancel').disabled = busy;
        el('editorial-footage-recovery').hidden = !running || ['queued', 'running'].includes(index?.receipt?.dispatch);
        el('editorial-footage-reconcile').disabled = busy || !el('editorial-footage-stopped').checked;
        el('editorial-footage-search').disabled = busy || index?.status !== 'completed';
        el('editorial-footage-item').disabled = busy;
    }
    function display(value) {
        index = value; controls();
        const details = el('editorial-footage-evidence'); details.replaceChildren();
        el('editorial-footage-results').replaceChildren();
        if (!value) return;
        if (value.status === 'indexing') {
            message(value.receipt?.dispatch === 'queued'
                ? 'Footage indexing is saved and waiting for the media worker. You can leave this page and return. Refresh to check progress.'
                : 'Inspecting footage. You can leave this page. Cancel prevents this attempt from publishing; the bounded decoder may take up to 90 seconds to stop.');
        } else if (value.status === 'failed') {
            message(value.error_category === 'cancelled_by_operator'
                ? 'Indexing cancelled. Your footage and notes are retained. Retry when ready.'
                : 'Footage inspection stopped or failed. Check permission and file readiness, then retry. Your media and notes are retained.');
        } else {
            message('Measured intervals are ready. Sparse keyframes describe observed colors and brightness; they do not verify people, actions, speech or every frame’s content. Indexing does not grant production rights.');
            const evidence = value.evidence, scale = evidence.timebase_numerator / evidence.timebase_denominator;
            details.append(node('p', `Measured duration: ${(evidence.duration_ticks * scale).toFixed(3)} seconds. Source timestamp origin: ${(evidence.source_origin_tick * scale).toFixed(3)} seconds.`));
            const list = node('ol', '');
            for (const shot of evidence.shots || []) {
                list.append(node('li', `${(shot.start_tick * scale).toFixed(3)}–${(shot.end_tick * scale).toFixed(3)} seconds from source start · keyframe ${(shot.keyframe_tick * scale).toFixed(3)} seconds · observed ${shot.visual_tags.join(', ') || 'no visual tags'}. Original source timestamps ${((shot.start_tick + evidence.source_origin_tick) * scale).toFixed(3)}–${((shot.end_tick + evidence.source_origin_tick) * scale).toFixed(3)} seconds.`));
            }
            details.append(list);
        }
        details.append(node('p', `Recorded inspection permission: ${(value.receipt?.permission_evidence_refs || []).join(', ') || 'No permission receipt available'}. Current permission is checked on inspection and search; production use needs its own rights check.`));
        const troubleshooting = node('details', ''); troubleshooting.className = 'ae-help';
        troubleshooting.append(node('summary', 'Index evidence and troubleshooting'), node('pre', JSON.stringify(value, null, 2)));
        details.append(troubleshooting);
    }
    function schedule() {
        clearTimeout(timer);
        if (index?.status === 'indexing') timer = setTimeout(() => void load(), 5000);
    }
    async function load() {
        if (!itemId() || busy) return;
        const current = epoch, request = ++read, url = endpoint();
        message('Loading measured footage evidence…');
        try {
            const value = await api(url);
            if (current !== epoch || request !== read) return;
            display(value); schedule();
        } catch (error) {
            if (current !== epoch || request !== read) return;
            clearTimeout(timer);
            display(null);
            message(/No measured index/.test(error.message) ? 'This footage has no measured index yet. Index footage to inspect its intervals.' : `Footage evidence unavailable. ${error.message} Refresh to recover. Your search text is retained.`);
        }
    }
    async function action(kind) {
        if (busy || !itemId()) return;
        const current = epoch, url = endpoint();
        ++read; clearTimeout(timer); busy = true; controls();
        message(kind === 'start' ? 'Saving footage indexing…' : 'Updating saved indexing…');
        try {
            const payload = kind === 'start' ? {retry_failed: index?.status === 'failed'} : kind === 'reconcile' ? {confirmed_worker_stopped: true} : {};
            const value = await api(`${url}${kind === 'start' ? '' : `/${kind}`}`, {method: 'POST', body: JSON.stringify(payload)});
            if (current !== epoch) return;
            el('editorial-footage-stopped').checked = false; display(value); schedule();
        } catch (error) {
            if (current === epoch) message(`Indexing update unavailable. ${error.message} Refresh before retrying an uncertain start; saved attempts are reused. Your footage and search text are retained.`);
        } finally { if (current === epoch) { busy = false; controls(); } }
    }
    async function search() {
        if (busy || index?.status !== 'completed') return;
        const current = epoch, request = ++read;
        busy = true; controls(); message('Searching observed visual tags…');
        try {
            const results = await api(`${base()}/shot-search`, {method: 'POST', body: JSON.stringify({item_ids: [itemId()], query: el('editorial-footage-query').value, limit: 50})});
            if (current !== epoch || request !== read) return;
            const list = el('editorial-footage-results'); list.replaceChildren();
            for (const hit of results) list.append(node('p', `${hit.source_in_seconds.toFixed(3)}–${hit.source_out_seconds.toFixed(3)} seconds · keyframe ${hit.keyframe_seconds.toFixed(3)} seconds · observed ${hit.visual_tags.join(', ')}`));
            message(results.length ? `${results.length} measured intervals match. These observed tags do not establish semantic content or production permission.` : 'No measured intervals match. Search only red, green, blue, dark, bright or mid-tone. Sparse keyframes may miss relevant visuals; semantic descriptions are unsupported.');
        } catch (error) {
            if (current === epoch) { el('editorial-footage-results').replaceChildren(); message(`Visual search unavailable. ${error.message} Refresh evidence and retry; your search text is retained.`); }
        } finally { if (current === epoch) { busy = false; controls(); } }
    }
    function setContext(value, items) {
        const next = value?.project_id ? `${value.channel}:${value.project_id}:${value.revision}` : '';
        if (next !== scope) {
            scope = next; context = value; ++epoch; ++read; clearTimeout(timer); busy = false; index = null;
            el('editorial-footage-item').replaceChildren(); delete el('editorial-footage-item').dataset.items; el('editorial-footage-query').value = '';
            el('editorial-footage-stopped').checked = false; display(null);
        }
        const select = el('editorial-footage-item'), previous = select.value;
        const eligible = items.filter(item => item.kind === 'video' && item.clip_id && ['acquired', 'ready'].includes(item.status) && !['restricted', 'denied', 'revoked', 'expired', 'reference_only'].includes(item.rights_status));
        const signature = JSON.stringify(eligible.map(item => [item.id, item.notes, item.source_metadata?.title]));
        if (select.dataset.items !== signature) {
            select.dataset.items = signature; select.replaceChildren();
            for (const item of eligible) { const option = node('option', item.source_metadata?.title || item.notes || 'Acquired footage'); option.value = item.id; select.append(option); }
            if (eligible.some(item => item.id === previous)) select.value = previous;
            if (select.value !== previous) { ++epoch; display(null); if (select.value) void load(); }
        }
        if (!eligible.length) { ++epoch; clearTimeout(timer); display(null); message('No acquired video is eligible for inspection. Add owned footage or acquire and validate permitted video. Links, denied media and expired permissions cannot be indexed.'); }
        controls();
    }
    function init(transport) {
        api = transport;
        el('editorial-footage-item').addEventListener('change', () => { ++epoch; ++read; clearTimeout(timer); display(null); el('editorial-footage-stopped').checked = false; void load(); });
        el('editorial-footage-start').addEventListener('click', () => void action('start'));
        el('editorial-footage-cancel').addEventListener('click', () => void action('cancel'));
        el('editorial-footage-refresh').addEventListener('click', () => void load());
        el('editorial-footage-search').addEventListener('click', () => void search());
        el('editorial-footage-reconcile').addEventListener('click', () => void action('reconcile'));
        el('editorial-footage-stopped').addEventListener('change', controls);
    }
    return {init, context: setContext};
})();
