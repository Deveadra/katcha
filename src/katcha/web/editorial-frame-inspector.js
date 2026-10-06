/* Authenticated, read-only inspection; late responses cannot reopen a closed frame. */
window.KatchaFrameInspector = (() => {
    const el = suffix => document.getElementById(`editorial-frame-${suffix}`);
    let api, apiBlob, epoch = 0, target = null, imageUrl = null;
    function clear() {
        epoch += 1;
        el('canvas').replaceChildren();
        if (imageUrl) URL.revokeObjectURL(imageUrl);
        imageUrl = null;
    }
    function reset() {
        clear(); target = null;
        el('dialog').close();
        el('status').textContent = '';
        el('description').textContent = '';
        el('notes').replaceChildren();
    }
    function annotations(items) {
        const layer = document.createElement('div');
        layer.className = 'editorial-frame-regions';
        layer.hidden = !el('marks').checked;
        for (const item of items) {
            const region = item.region;
            const mark = document.createElement('div');
            mark.className = `editorial-frame-region editorial-frame-${item.kind}`;
            Object.assign(mark.style, {left: `${region.x * 100}%`, top: `${region.y * 100}%`, width: `${region.width * 100}%`, height: `${region.height * 100}%`});
            mark.setAttribute('aria-label', item.label || item.kind);
            if (item.kind === 'arrow') {
                const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svg.setAttribute('viewBox', '0 0 100 100'); svg.setAttribute('preserveAspectRatio', 'none');
                const path = document.createElementNS(svg.namespaceURI, 'path');
                path.setAttribute('d', 'M 5 5 L 88 88 M 48 88 L 88 88 L 88 48');
                path.setAttribute('fill', 'none'); path.setAttribute('stroke', '#ffe09a');
                path.setAttribute('stroke-width', '3'); path.setAttribute('vector-effect', 'non-scaling-stroke');
                svg.append(path); mark.append(svg);
            }
            layer.append(mark);
        }
        return layer;
    }
    async function load() {
        if (!target) return;
        clear();
        const ticket = epoch;
        const {channel, project, run, index} = target;
        const base = `/v1/channels/${encodeURIComponent(channel)}/editorial-projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(run)}/frames/${encodeURIComponent(index)}`;
        el('status').textContent = 'Checking source clearance and loading the cited frame…';
        el('description').textContent = ''; el('notes').replaceChildren();
        el('retry').disabled = true;
        let pendingUrl = null;
        try {
            const info = await api(base);
            if (ticket !== epoch) return;
            const blob = await apiBlob(`${base}/image?evidence_digest=${encodeURIComponent(info.evidence_digest)}`);
            if (ticket !== epoch) return;
            pendingUrl = URL.createObjectURL(blob);
            const img = new Image(); img.alt = `Cited source frame at ${Number(info.sample_seconds).toFixed(3)} seconds`;
            img.src = pendingUrl;
            await img.decode();
            if (ticket !== epoch) return;
            imageUrl = pendingUrl; pendingUrl = null;
            el('canvas').append(img, annotations(info.overlays || []));
            el('description').textContent = `${Number(info.sample_seconds).toFixed(3)}s · ${info.observation}`;
            const notes = [...(info.limitations || []), ...(info.regions?.limitations || []), ...(info.overlays || []).map(item => `${item.kind}${item.label ? ` · ${item.label}` : ''}`)];
            notes.forEach(text => { const p = document.createElement('p'); p.textContent = text; el('notes').append(p); });
            el('status').textContent = info.frozen ? 'Exact sampled freeze. Regions are suggestions; verify their meaning before approval.' : 'Sampled frame only. This does not establish continuous visibility or motion.';
        } catch (error) {
            if (ticket === epoch) el('status').textContent = `Frame unavailable: ${error.message}. Retry inspection to check again. Your edits are unchanged.`;
        } finally {
            if (pendingUrl) URL.revokeObjectURL(pendingUrl);
            if (ticket === epoch) el('retry').disabled = false;
        }
    }
    function init(transport, blobTransport) {
        api = transport; apiBlob = blobTransport;
        document.addEventListener('click', event => {
            const button = event.target.closest('[data-frame-run]');
            if (!button) return;
            target = {channel: button.dataset.frameChannel, project: button.dataset.frameProject, run: button.dataset.frameRun, index: button.dataset.frameIndex};
            el('marks').checked = true;
            if (!el('dialog').open) el('dialog').showModal();
            void load();
        });
        el('close').addEventListener('click', reset);
        el('dialog').addEventListener('cancel', event => { event.preventDefault(); reset(); });
        el('retry').addEventListener('click', () => void load());
        el('marks').addEventListener('change', () => {
            const layer = el('canvas').querySelector('.editorial-frame-regions');
            if (layer) layer.hidden = !el('marks').checked;
        });
    }
    return {init, reset};
})();
