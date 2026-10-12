/* Saved matching output only; proposal generation and rights remain server authority. */
window.KatchaShotHandoff = (() => {
    const el = id => document.getElementById(id);
    const text = (tag, value) => { const node = document.createElement(tag); node.textContent = value; return node; };
    let api, identity, beforeApply, reload, context, key = '', epoch = 0, busy = false, saved = null;
    const selected = new Set(), notes = new Map(), pendingApply = new Map();
    const base = () => `/v1/channels/${encodeURIComponent(context.channel)}/editorial-projects/${encodeURIComponent(context.project_id)}`;
    const storage = () => `katcha.shot-proposals.${key}`;
    function remember(packageId) { try { localStorage.setItem(storage(), packageId); } catch {} }
    function remembered() { try { return localStorage.getItem(storage()); } catch { return null; } }
    function status(value) { el('editorial-shot-status').textContent = value; }
    function controls() {
        const blocked = busy || context?.busy || context?.unsaved;
        el('editorial-shot-match').disabled = blocked || !context?.preparation?.run_id || !selected.size;
        el('editorial-shot-refresh').disabled = blocked || !saved?.package_id;
        el('editorial-shot-beat').disabled = blocked;
        el('editorial-shot-replace').disabled = blocked;
        el('editorial-shot-freeze').disabled = blocked;
        for (const button of el('editorial-shot-proposals').querySelectorAll('button')) button.disabled = blocked || button.dataset.blocked === 'true';
        for (const input of el('editorial-shot-items').querySelectorAll('input')) input.disabled = blocked || (selected.size >= 8 && !input.checked);
    }
    function render() {
        const output = el('editorial-shot-proposals'); output.replaceChildren();
        if (!saved) { controls(); return; }
        for (const [item, gap] of Object.entries(saved.item_gaps || {})) output.append(text('p', `Footage needs attention: ${String(gap).replaceAll('_', ' ')}. Index eligible footage or review its permission before matching again.`));
        for (const result of saved.requirements || []) {
            if (!result.proposals?.length) {
                output.append(text('p', `Visual need remains unresolved: ${(result.unresolved_gaps || []).join(', ').replaceAll('_', ' ') || 'No observed match'}.`));
                continue;
            }
            for (const binding of result.proposals) {
                const card = text('article', ''); card.className = 'editorial-beat';
                card.append(text('h5', `${binding.source_in_seconds.toFixed(3)}–${binding.source_out_seconds.toFixed(3)} seconds · observed ${(binding.visual_tags || []).join(', ')}`));
                card.append(text('p', binding.explanation));
                card.append(text('p', `Limits: ${(binding.confidence_limits || []).join(', ').replaceAll('_', ' ')}. Selection does not verify semantic intent or grant rights.`));
                card.append(text('p', binding.production_eligible ? 'Current production permission recorded. Permission and source evidence are rechecked at handoff, compile and render.' : 'Needs production permission before Storyboard use. You may save a review selection; indexing and approval do not clear rights.'));
                const choice = (saved.operator_choices || []).find(value => value.binding?.binding_id === binding.binding_id && value.current_status === 'current');
                const field = text('label', 'Selection note'); const note = document.createElement('textarea'); note.maxLength = 2000; note.rows = 2; note.value = notes.get(binding.binding_id) ?? choice?.note ?? ''; note.addEventListener('input', () => notes.set(binding.binding_id, note.value)); field.append(note); card.append(field);
                const choose = text('button', choice ? 'Update selected shot note' : 'Save shot selection'); choose.type = 'button'; choose.className = 'mini'; choose.dataset.blocked = String(saved.current_status !== 'current');
                choose.addEventListener('click', () => void select(binding, note.value)); card.append(choose);
                if (choice) {
                    card.append(text('p', `Saved operator choice · ${choice.locked ? 'locked' : 'unlocked'} · version ${choice.version}.`));
                    const use = text('button', 'Add selected shot to Storyboard'); use.type = 'button'; use.className = 'mini'; use.dataset.blocked = String(!binding.production_eligible || !context.beats?.length);
                    use.addEventListener('click', () => void apply(choice)); card.append(use);
                }
                const details = text('details', ''); details.className = 'ae-help'; details.append(text('summary', 'Measured source and permission evidence'), text('pre', JSON.stringify({binding, choice}, null, 2))); card.append(details); output.append(card);
            }
        }
        controls();
    }
    async function load(packageId = saved?.package_id) {
        if (!packageId || busy) return;
        const current = epoch; busy = true; controls(); status('Checking saved shot proposals, source bytes and permission…');
        try {
            const result = await api(`${base()}/shot-proposals/${encodeURIComponent(packageId)}`);
            if (current !== epoch) return;
            saved = result; remember(result.package_id); render();
            status(result.current_status === 'current' ? 'Review measured proposals and their limits. Save an explicit selection, then choose its target script beat. Other Storyboard edits are preserved.' : 'Saved proposals are stale. Your choices and notes remain in history. Review current footage and match again before Storyboard use.');
        } catch (error) { if (current === epoch) { saved = null; render(); status(`Saved shot proposals unavailable. ${error.message} Match again or refresh media to recover. Notes are retained.`); } }
        finally { if (current === epoch) { busy = false; controls(); } }
    }
    async function match() {
        if (el('editorial-shot-match').disabled) return;
        const current = epoch, scope = {...context}; busy = true; controls(); status('Checking measured footage against the saved visual needs…');
        try {
            const payload = {preparation_run_id: scope.preparation.run_id, expected_revision: scope.revision, item_ids: [...selected].sort()};
            payload.idempotency_key = identity(`shot-proposals.${scope.project_id}`, payload);
            const result = await api(`${base()}/shot-proposals`, {method: 'POST', body: JSON.stringify(payload)});
            if (current !== epoch) return;
            saved = result; remember(result.package_id); render(); status('Measured proposals saved. Color tags are limited evidence; review gaps and permission before selecting a shot.');
        } catch (error) { if (current === epoch) status(`Shot proposals unavailable. ${error.message} Footage choices and notes are retained. Retry with the same options or refresh saved proposals.`); }
        finally { if (current === epoch) { busy = false; controls(); } }
    }
    async function select(binding, note) {
        if (busy || saved.current_status !== 'current') return;
        const current = epoch, packageId = saved.package_id;
        const latest = (saved.operator_choices || []).find(value => value.binding?.requirement_id === binding.requirement_id);
        busy = true; controls(); status('Saving your measured shot selection…');
        try {
            const payload = {package_id: packageId, binding_id: binding.binding_id, expected_revision: context.revision, expected_choice_version: latest?.version || 0, locked: latest?.locked ?? true, note};
            payload.idempotency_key = identity(`shot-choice.${context.project_id}`, payload);
            await api(`${base()}/shot-proposals/choices`, {method: 'POST', body: JSON.stringify(payload)});
            if (current !== epoch) return;
            busy = false; await load(packageId);
        } catch (error) { if (current === epoch) status(`Selection not confirmed. ${error.message} Your note is retained. Refresh saved proposals before replacing a choice.`); }
        finally { if (current === epoch) { busy = false; controls(); } }
    }
    async function apply(choice) {
        if (busy || !context.beats?.length) return;
        const current = epoch, scope = {...context}, url = base(), beatId = el('editorial-shot-beat').value;
        const replace = el('editorial-shot-replace').checked, freeze = el('editorial-shot-freeze').checked;
        busy = true; controls(); status('Saving pending Storyboard edits and rechecking shot permission…');
        try {
            await beforeApply();
            if (current !== epoch) return;
            const workspace = await api(`${url}/storyboard?script_revision=${scope.revision}`);
            if (current !== epoch) return;
            let payload = {choice_id: choice.choice_id, expected_revision: scope.revision, expected_storyboard_version: workspace?.version || 0, beat_id: beatId, replace_existing: replace, freeze};
            const operation = JSON.stringify([choice.choice_id, beatId, replace, freeze]);
            let previous = pendingApply.get(operation);
            try { previous ||= JSON.parse(localStorage.getItem(`${storage()}.handoff.${operation}`) || 'null'); } catch {}
            if (previous) payload = previous;
            else {
                payload.idempotency_key = identity(`shot-handoff.${scope.project_id}`, payload);
                pendingApply.set(operation, payload);
                try { localStorage.setItem(`${storage()}.handoff.${operation}`, JSON.stringify(payload)); } catch {}
            }
            await api(`${url}/storyboard/approved-shot`, {method: 'POST', body: JSON.stringify(payload)});
            if (current !== epoch) return;
            pendingApply.delete(operation);
            try { localStorage.removeItem(`${storage()}.handoff.${operation}`); } catch {}
            await reload(scope.channel, scope.project_id);
            status('Selected measured shot saved to Storyboard. Other beats and narration are retained; prior edits remain in Undo history. Preview checks permission again and rejects playback outside the approved interval.');
        } catch (error) { if (current === epoch) status(`Storyboard handoff not confirmed. ${error.message} Your selections, note and Storyboard edits are retained. Reload saved Storyboard before retrying an uncertain response.`); }
        finally { if (current === epoch) { busy = false; controls(); } }
    }
    function setContext(value) {
        const next = value?.project_id ? `${value.channel}:${value.project_id}:${value.revision}` : '';
        if (next !== key) {
            key = next; ++epoch; busy = false; saved = null; selected.clear(); notes.clear(); pendingApply.clear();
            delete el('editorial-shot-items').dataset.signature; delete el('editorial-shot-beat').dataset.signature;
            el('editorial-shot-replace').checked = false; el('editorial-shot-freeze').checked = false; render();
        }
        context = value;
        const items = (value?.items || []).filter(item => item.clip_id && item.kind === 'video' && ['acquired', 'ready'].includes(item.status) && !['restricted', 'denied', 'revoked', 'expired', 'reference_only'].includes(item.rights_status));
        const holder = el('editorial-shot-items'), signature = JSON.stringify(items.map(item => [item.id, item.source_metadata?.title, item.notes]));
        if (holder.dataset.signature !== signature) {
            holder.dataset.signature = signature; holder.replaceChildren();
            for (const chosen of selected) if (!items.some(item => item.id === chosen)) selected.delete(chosen);
            for (const item of items) {
                const label = text('label', item.source_metadata?.title || item.notes || 'Acquired footage'); label.className = 'check-row';
                const input = document.createElement('input'); input.type = 'checkbox'; input.checked = selected.has(item.id); input.addEventListener('change', () => { if (input.checked) selected.add(item.id); else selected.delete(item.id); controls(); }); label.prepend(input); holder.append(label);
            }
            if (!items.length) holder.append(text('p', 'No acquired footage available. Index permitted video in the Media Bin before proposing shots.'));
        }
        const beats = el('editorial-shot-beat'), beatSignature = JSON.stringify(value?.beats || []);
        if (beats.dataset.signature !== beatSignature) {
            const previous = beats.value; beats.dataset.signature = beatSignature; beats.replaceChildren();
            for (const [index, beat] of (value?.beats || []).entries()) { const option = text('option', `Beat ${index + 1} · ${beat.narration}`); option.value = beat.id; beats.append(option); }
            if ([...beats.options].some(option => option.value === previous)) beats.value = previous;
        }
        if (!saved && !busy && next && remembered()) void load(remembered());
        if (!value?.preparation?.run_id && !saved) status('Prepare media for the saved script before proposing measured shots.');
        controls();
    }
    function init(transport, requestIdentity, prepare, refresh) {
        api = transport; identity = requestIdentity; beforeApply = prepare; reload = refresh;
        el('editorial-shot-match').addEventListener('click', () => void match());
        el('editorial-shot-refresh').addEventListener('click', () => void load());
    }
    return {init, context: setContext};
})();
