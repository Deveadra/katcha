/* Project Media Bin uses Editorial's authenticated transport and saved script context. */
window.KatchaMediaPreparation = (() => {
    const el = id => document.getElementById(id);
    const state = {context: null, key: '', epoch: 0, busy: false, run: null, items: [], message: '', readEpoch: 0, recoveryRun: ''};
    let api, identity, reload;
    const labels = {not_assessed: 'Permission not assessed', reference_only: 'Reference only', review_permitted: 'Review permitted', production_cleared: 'Production permission recorded', restricted: 'Restricted use', denied: 'Permission denied', revoked: 'Permission revoked', expired: 'Permission expired'};
    function text(tag, value, className) {
        const node = document.createElement(tag); node.textContent = value ?? '';
        if (className) node.className = className;
        return node;
    }
    function link(url, label) {
        try {
            const parsed = new URL(url);
            if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) return text('span', 'Source link unavailable');
            const node = text('a', label); node.href = parsed.href; node.target = '_blank'; node.rel = 'noopener noreferrer';
            return node;
        } catch { return text('span', 'Source link unavailable'); }
    }
    function base(context = state.context) {
        return `/v1/channels/${encodeURIComponent(context.channel)}/editorial-projects/${encodeURIComponent(context.project_id)}`;
    }
    function render() {
        const context = state.context, run = state.run;
        el('editorial-media-preparation').hidden = !context?.project_id;
        if (!context?.project_id) return;
        const stale = Boolean(run && (run.stale || (context.revision > 0 && run.input_revision !== context.revision)));
        const blocked = state.busy || context.busy || context.unsaved || !context.has_script;
        el('editorial-media-start').disabled = blocked;
        el('editorial-media-search-permission').disabled = state.busy || context.busy;
        el('editorial-media-search-scope').textContent = typeof run?.external_search_authorized === 'boolean'
            ? `The checkbox applies to a new search. Saved search permission: ${run.external_search_authorized ? 'bounded public-provider metadata searches allowed' : 'public-provider searches disabled'}. Continuing uses that saved permission.`
            : 'The checkbox applies to a new search. Continuing saved work uses its saved search permission.';
        el('editorial-media-continue').hidden = !run || run.status === 'completed' || stale;
        el('editorial-media-continue').disabled = blocked;
        el('editorial-media-refresh').disabled = state.busy || context.busy;
        const recovery = run?.stage === 'media_outcome_unknown';
        el('editorial-media-recovery').hidden = !recovery;
        if (state.recoveryRun !== run?.run_id) {
            state.recoveryRun = run?.run_id || '';
            el('editorial-media-worker-stopped').checked = false;
        }
        const recoveryAt = run?.recovery_available_at ? Date.parse(run.recovery_available_at) : NaN;
        const waiting = Number.isFinite(recoveryAt) && recoveryAt > Date.now();
        el('editorial-media-record-unknown').disabled = blocked || !recovery || !Number.isFinite(recoveryAt) || waiting || !el('editorial-media-worker-stopped').checked;
        el('editorial-media-recovery-wait').textContent = !Number.isFinite(recoveryAt)
            ? 'Recovery time is unavailable. Refresh media to check the current search status before recording an outcome.'
            : waiting ? `Recovery can be recorded after ${new Date(recoveryAt).toLocaleString()}. Refresh media after that time and confirm the previous search has stopped.`
            : 'Confirm the previous search has stopped before recording its unknown outcome. Refresh media if it may have finished.';
        el('editorial-media-gate').textContent = context.unsaved
            ? 'Save your script changes before preparing media. Your edits are retained.'
            : !context.has_script ? 'Import a script or save a script revision to prepare media.' : '';
        const progress = run?.progress;
        el('editorial-media-status').textContent = state.message || (run
            ? stale ? 'This search belongs to an older script revision. Prepare media for the current script.'
                : run.status === 'completed' ? 'Media search finished. Review coverage, gaps and permissions below.'
                    : `Saved media search · ${progress?.completed_jobs || 0} of ${progress?.total_jobs || 0} search pages checked. Continue saved work when ready.`
            : 'No media search saved for this project. Prepare the script to see its visual needs and any gaps.');
        const coverage = el('editorial-media-coverage'); coverage.replaceChildren();
        if (run) {
            const requirements = new Map((run.manifest?.requirements || []).map(row => [row.requirement_id, row]));
            const rows = run.coverage?.requirements || [];
            for (const row of rows) {
                const card = text('article', '', 'editorial-beat');
                card.append(text('h5', requirements.get(row.requirement_id)?.intent || 'Script visual need'));
                card.append(text('p', `${String(row.coverage_status || 'unresolved').replaceAll('_', ' ')} · ${row.usable_media_count || 0} cleared media`));
                const gaps = [...(row.gaps || []), ...(run.planning_gaps || []).filter(gap => gap.requirement_id === row.requirement_id)];
                const seen = new Set();
                for (const gap of gaps) {
                    const value = gap.next_action || String(gap.category || 'Needs review').replaceAll('_', ' ');
                    if (!seen.has(value)) card.append(text('p', value));
                    seen.add(value);
                }
                const references = text('ol', '');
                for (const reference of row.references || []) {
                    const line = text('li', '');
                    line.append(link(reference.canonical_url, reference.title || `Inspect ${reference.provider || 'source'} reference`), text('span', ` · ${labels[reference.rights_state] || 'Permission not assessed'}`));
                    line.append(text('p', 'Provider-reported source information. Verify the visual match and permission before use.'));
                    if (reference.explanation) line.append(text('p', reference.explanation));
                    references.append(line);
                }
                if (references.children.length) card.append(references);
                coverage.append(card);
            }
            if (!rows.length) coverage.append(text('p', 'Visual needs are saved. Coverage will appear after the next search step.'));
            if (run.error) coverage.prepend(text('p', 'Saved work needs attention. Refresh progress or continue to recover.', 'error'));
            el('editorial-media-diagnostics').textContent = JSON.stringify({stage: run.stage, error: run.error, progress: run.progress, updated_at: run.updated_at}, null, 2);
        } else el('editorial-media-diagnostics').textContent = '';
        const bin = el('editorial-project-media-bin'); bin.replaceChildren();
        if (!state.items.length) bin.append(text('p', 'Your project Media Bin is empty. Completed searches save source references here. References still need permission review before use.'));
        for (const item of state.items) {
            const card = text('article', '', 'editorial-beat');
            card.append(text('h5', item.source_metadata?.title || item.source_metadata?.observations?.[0]?.title || item.notes || 'Project media'));
            card.append(text('p', `${String(item.status || 'reference_only').replaceAll('_', ' ')} · ${labels[item.rights_status] || 'Permission not assessed'}`));
            if (item.external_reference_url) card.append(link(item.external_reference_url, 'Inspect original source'));
            else card.append(text('p', 'Managed project media · permission and file readiness must both allow its intended use.'));
            const bindings = (run?.bin_bindings || []).filter(binding => binding.media_item_id === item.id);
            const requirementIds = new Set([...(item.requirement_ids || []), ...bindings.flatMap(binding => binding.requirement_ids || [])]);
            if (requirementIds.size) card.append(text('p', `Linked to ${requirementIds.size} script visual need${requirementIds.size === 1 ? '' : 's'}.`));
            const details = text('details', '', 'ae-help'); details.append(text('summary', 'Source history and evidence'), text('pre', JSON.stringify({provenance: item.provenance, source_metadata: item.source_metadata, preparation_bindings: bindings}, null, 2)));
            card.append(details); bin.append(card);
        }
    }
    function context(value) {
        const key = value?.project_id ? `${value.channel}:${value.project_id}:${value.revision}` : '';
        state.context = value;
        if (key !== state.key) {
            state.key = key; state.epoch += 1; state.busy = false; state.run = null; state.items = []; state.message = ''; state.recoveryRun = '';
            el('editorial-media-worker-stopped').checked = false;
            el('editorial-media-search-permission').checked = false;
            render();
            if (key) void refresh();
        } else render();
    }
    async function refresh() {
        if (!state.key || state.busy) return;
        const epoch = state.epoch, readEpoch = ++state.readEpoch, context = {...state.context};
        state.message = 'Loading saved media search and project Media Bin…'; render();
        try {
            const [run, bin] = await Promise.all([
                api(`${base(context)}/media-preparation`),
                context.revision > 0 ? api(`${base(context)}/media-bin?revision=${context.revision}`) : Promise.resolve({items: []}),
            ]);
            if (epoch !== state.epoch || readEpoch !== state.readEpoch) return;
            state.run = run; state.items = bin.items || []; state.message = ''; render();
        } catch (error) {
            if (epoch !== state.epoch) return;
            if (readEpoch !== state.readEpoch) return;
            state.message = `Saved media is unavailable. ${error.message} Use Refresh media to reconnect.`; render();
        }
    }
    async function work(start) {
        if (state.busy || state.context?.unsaved || !state.context?.has_script || state.context?.busy) return;
        const epoch = state.epoch, context = {...state.context};
        state.readEpoch += 1;
        state.busy = true; state.message = start ? 'Saving your script media plan…' : 'Continuing saved media search…'; render();
        try {
            if (start) {
                const payload = {expected_revision: context.revision, external_search_authorized: el('editorial-media-search-permission').checked};
                payload.idempotency_key = identity(`media-preparation.${context.project_id}`, payload);
                const run = await api(`${base(context)}/media-preparation`, {method: 'POST', body: JSON.stringify(payload)});
                if (epoch !== state.epoch) return;
                state.run = run;
            }
            if (!state.run || state.run.stale || (context.revision > 0 && state.run.input_revision !== context.revision)) throw new Error('Refresh media to load the saved search for this script.');
            for (let step = 0; step < 20 && state.run.status !== 'completed'; step += 1) {
                const before = state.run.progress?.completed_jobs || 0;
                const run = await api(`${base(context)}/media-preparation/${encodeURIComponent(state.run.run_id)}/resume`, {method: 'POST', body: '{}'});
                if (epoch !== state.epoch) return;
                state.run = run; state.items = run.media_bin_items || state.items;
                state.message = `Checking source references · ${run.progress?.completed_jobs || 0} of ${run.progress?.total_jobs || 0} search pages.`; render();
                if (run.stale || run.status === 'failed' || (run.status !== 'completed' && (run.progress?.completed_jobs || 0) <= before)) break;
            }
            state.message = state.run.status === 'completed' ? '' : 'Your progress is saved. Review any blockers below, then Continue saved media search or Refresh media.';
            if (state.run.input_revision !== context.revision) await reload(context.channel, context.project_id);
        } catch (error) {
            if (epoch !== state.epoch) return;
            state.message = `Media search paused. ${error.message} Your script and saved progress are retained. Retry the same action or refresh media.`;
        } finally {
            if (epoch === state.epoch) { state.busy = false; render(); }
        }
    }
    async function recordUnknown() {
        if (el('editorial-media-record-unknown').disabled || state.run?.stage !== 'media_outcome_unknown') return;
        const epoch = state.epoch, context = {...state.context}, runId = state.run.run_id;
        state.readEpoch += 1; state.busy = true; state.message = 'Recording the stopped search outcome…'; render();
        try {
            const run = await api(`${base(context)}/media-preparation/${encodeURIComponent(runId)}/reconcile`, {method: 'POST', body: JSON.stringify({confirmed_worker_stopped: true})});
            if (epoch !== state.epoch) return;
            state.run = run; state.items = run.media_bin_items || state.items;
            el('editorial-media-worker-stopped').checked = false;
            state.message = 'Unknown outcome recorded. Its search limit is preserved. Review the gaps and continue the remaining saved work.';
        } catch (error) {
            if (epoch === state.epoch) state.message = `Recovery was not recorded. ${error.message} Refresh media before trying again.`;
        } finally {
            if (epoch === state.epoch) { state.busy = false; render(); }
        }
    }
    function init(transport, requestIdentity, reloadProject) {
        api = transport; identity = requestIdentity; reload = reloadProject;
        el('editorial-media-start').addEventListener('click', () => void work(true));
        el('editorial-media-continue').addEventListener('click', () => void work(false));
        el('editorial-media-refresh').addEventListener('click', () => void refresh());
        el('editorial-media-worker-stopped').addEventListener('change', render);
        el('editorial-media-record-unknown').addEventListener('click', () => void recordUnknown());
    }
    return {init, context};
})();
