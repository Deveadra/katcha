/* Operator-only recovery. Provider content is untrusted; never insert it as HTML. */
(() => {
    const $ = (id) => document.getElementById(id);
    const params = new URLSearchParams(location.search);
    const channel = params.get('channel');
    const job = params.get('job');
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    const valid = uuid.test(channel || '') && uuid.test(job || '');
    const base = valid ? `/v1/channels/${channel}/acquisition-jobs/${job}` : '';
    const state = { challenge: null, job: null, snapshot: null, busy: false, pending: null, timer: null };
    const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    function message(id, text, error = false) {
        $(id).textContent = text;
        $(id).dataset.error = String(error);
    }
    async function api(path, method = 'GET', body = null) {
        const headers = { Accept: 'application/json' };
        const token = location.port === '8765' ? '' : sessionStorage.getItem('katcha.controlToken');
        if (token) headers.Authorization = `Bearer ${token}`;
        if (body !== null) headers['Content-Type'] = 'application/json';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 12000);
        try {
            const response = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                ...(body === null ? {} : { body: JSON.stringify(body) }) });
            if (!response.ok) { const error = new Error('Request unavailable'); error.status = response.status; throw error; }
            return await response.json();
        } finally { clearTimeout(timeout); }
    }
    async function access(error) {
        if (![401, 403].includes(error.status)) return;
        $('access').hidden = false;
        message('access-message', error.status === 403 ? 'Your account cannot access this channel or perform this action. Use an authorized operator account.' : 'Sign in with your authorized Katcha account to continue.');
        $('access-login').hidden = true;
        try {
            const info = await api('/v1/control/access-login?return_to=' + encodeURIComponent(location.pathname + location.search));
            const login = new URL(info.login_url, location.origin);
            if (info.enabled && login.protocol === 'https:' && (login.hostname === location.hostname || login.hostname.endsWith('.cloudflareaccess.com'))) {
                $('access-login').href = login.href;
                $('access-login').hidden = false;
            }
        } catch { /* Keep the access explanation visible. */ }
    }
    function waiting() {
        return state.job?.status === 'waiting_human' && state.challenge?.state === 'waiting' && Date.parse(state.challenge.expires_at) > Date.now();
    }
    function controls() {
        const enabled = waiting() && !state.busy && !state.pending;
        $('open-browser').disabled = !enabled;
        $('refresh-browser').disabled = !waiting() || state.busy || (!state.snapshot && !state.pending);
        $('retry').disabled = !enabled || !state.snapshot;
        $('cancel').disabled = state.busy || !['waiting_human', 'queued', 'leased'].includes(state.job?.status);
        $('browser-view').querySelectorAll('input, select, button').forEach((control) => { control.disabled = !enabled; });
    }
    function clearBrowser(clearInputs = true) {
        state.snapshot = null;
        $('browser-image').removeAttribute('src');
        $('browser-screen').hidden = true;
        $('browser-view').hidden = true;
        if (clearInputs) { $('browser-text').value = ''; $('element-text').value = ''; }
        $('page-text').textContent = '';
    }
    async function refresh() {
        if (!valid) { message('status', 'This recovery link is incomplete. Open the full link in your notification or return to Katcha AI.', true); return; }
        try {
            state.job = await api(base);
            $('access').hidden = true;
            let challenge = null;
            if (state.job.status === 'waiting_human') {
                try { challenge = await api(base + '/human'); }
                catch (error) { if (error.status !== 404) throw error; }
            }
            if (challenge?.id !== state.challenge?.id) { clearBrowser(); state.pending = null; }
            state.challenge = challenge;
            const statuses = {
                queued: 'Your download is queued. You can leave this page; the saved request will continue.',
                leased: 'Katcha is attempting the download. Access and media are not verified yet.',
                awaiting_validation: 'The download arrived. Katcha is validating the media before continuing.',
                completed: 'The media passed validation. Your saved request can continue in Katcha.',
                cancelled: 'This download was cancelled. The original request remains in Katcha.',
                failed: 'The download could not finish. Return to Katcha AI to review the failure and choose another source or upload.',
                recovery_required: 'This request needs operator review. Katcha has preserved its state; it will not repeat an uncertain download automatically.',
            };
            const expired = challenge && Date.parse(challenge.expires_at) <= Date.now();
            let text = statuses[state.job.status] || 'Checking your saved request…';
            if (state.job.status === 'waiting_human') {
                text = !challenge ? 'Verification recovery is unavailable for this request. Refresh status or return to Katcha AI.'
                    : expired || challenge.state === 'closed' ? 'This verification window has ended. Return to Katcha AI to review the request; no download was automatically repeated.'
                    : ['retry_requested', 'retrying'].includes(challenge.state) ? 'Your request to check access is saved. Waiting for the download worker; success has not been confirmed.'
                    : 'Waiting for you to verify access with the source. Your original request is saved.';
            }
            message('status', text, ['failed', 'recovery_required'].includes(state.job.status) || !!expired);
            $('verification').hidden = !challenge || challenge.state === 'closed' || !!expired;
            if (!waiting()) clearBrowser();
            $('expiry').textContent = challenge && !expired ? 'Verification window ends ' + new Date(challenge.expires_at).toLocaleString() + '.' : '';
            $('notification').textContent = challenge && ['unknown', 'failed', 'unavailable', 'not_configured'].includes(challenge.notification_state) ? 'Notification delivery could not be confirmed. You can complete verification on this page.' : '';
            if (challenge?.code === 'session_expired' || challenge?.code === 'browser_unavailable') {
                clearBrowser(false);
                message('browser-status', challenge.code === 'session_expired' ? 'The browser session expired or restarted. Open verification and sign in again before checking access.' : 'The download worker’s browser is unavailable. Refresh status after the worker reconnects.', true);
            }
            controls();
        } catch (error) {
            message('status', error.status === 404 ? 'This saved request is not available in this channel. Check the notification link.' : 'Katcha could not refresh this request. Your entries are preserved. Refresh status to reconnect.', true);
            await access(error);
        }
    }
    async function pollCommand(id) {
        for (let attempt = 0; attempt < 32; attempt++) {
            let record;
            try { record = await api(base + '/human/commands/' + encodeURIComponent(id)); }
            catch (error) {
                if (error.status === 404 || error.status === 409) state.pending = null;
                throw error;
            }
            if (record.state === 'completed') { state.pending = null; return record.result; }
            if (!['queued', 'claimed'].includes(record.state) || Date.parse(record.expires_at) <= Date.now()) {
                state.pending = null;
                throw new Error('Command expired');
            }
            message('browser-status', record.state === 'queued' ? 'Waiting for the download worker. This action is saved and will not be submitted again.' : 'The browser is processing your action. Waiting for its result…');
            await delay(1500);
        }
        throw new Error('Command outcome unknown');
    }
    async function submit(action, onSubmitted = () => {}) {
        const id = crypto.randomUUID();
        state.pending = id;
        try {
            const record = await api(base + '/human/commands', 'POST', { id, challenge_id: state.challenge.id, action });
            onSubmitted();
            if (record.state === 'completed') { state.pending = null; return record.result; }
        } catch (error) {
            if (error.status) { state.pending = null; throw error; }
            onSubmitted();
            // A lost POST response is uncertain. Look up the same ID; never resend a form action.
        }
        return pollCommand(id);
    }
    function render(result) {
        if (!result?.available) {
            clearBrowser(false);
            message('browser-status', result?.code === 'session_expired' ? 'The browser session expired or the worker restarted. Open verification and sign in again.' : 'The browser could not complete this action. Refresh the page view to inspect the result before repeating any submission.', true);
            return false;
        }
        if (result.image) {
            if (/^[A-Za-z0-9+/=]+$/.test(result.image) && result.image.length < 2800000) {
                $('browser-image').src = 'data:image/png;base64,' + result.image;
                $('browser-screen').hidden = false;
            }
            return true;
        }
        state.snapshot = result.snapshot_id;
        $('browser-view').hidden = false;
        $('browser-title').textContent = result.title || 'Source browser';
        try { const url = new URL(result.url); $('browser-location').textContent = url.origin; } catch { $('browser-location').textContent = ''; }
        $('page-text').textContent = result.text || 'No readable page text. Use the image or refresh the page view.';
        $('element').replaceChildren(...(result.elements || []).map((element) => {
            const option = new Option(element.name || element.type || element.tag || 'Page control', element.ref);
            option.disabled = !!element.disabled;
            return option;
        }));
        $('browser-tab').replaceChildren(...(result.tabs || []).map((tab) => new Option('Tab ' + (tab.index + 1), String(tab.index))));
        $('browser-tab').value = String(result.active_tab || 0);
        return true;
    }
    async function action(arguments_, input = null) {
        if (!waiting() || state.busy) return;
        state.busy = true;
        controls();
        try {
            message('browser-status', 'Connecting to the download worker…');
            const result = state.pending ? await pollCommand(state.pending) : await submit(arguments_, () => { if (input) input.value = ''; });
            if (render(result) && state.snapshot) {
                const screenshot = await submit({ action: 'screenshot', snapshot_id: state.snapshot });
                if (render(screenshot)) message('browser-status', 'Browser view updated. Complete verification, then choose Check access and continue.');
            }
        } catch (error) {
            message('browser-status', state.pending ? 'The connection was interrupted. The action may have run. Refresh page view to check the saved result; do not repeat a submission.' : 'The action could not be confirmed. Refresh page view to inspect the browser before repeating a submission. If the worker is unavailable, refresh status later.', true);
            await access(error);
        } finally { state.busy = false; controls(); }
    }
    $('refresh-status').onclick = refresh;
    $('open-browser').onclick = () => action({ action: 'open' });
    $('refresh-browser').onclick = () => action({ action: 'snapshot' });
    $('type-form').onsubmit = (event) => {
        event.preventDefault();
        const text = $('browser-text').value;
        if (!text || state.busy || state.pending || !waiting()) return;
        void action({ action: 'type', text, snapshot_id: state.snapshot }, $('browser-text'));
    };
    $('element-form').onsubmit = (event) => {
        event.preventDefault();
        if (state.busy || state.pending || !waiting() || !$('element').value) return;
        const text = $('element-text').value;
        void action({ action: $('element-action').value, ref: $('element').value, text, key: 'Enter', snapshot_id: state.snapshot }, $('element-text'));
    };
    $('browser-tab').onchange = () => action({ action: 'switch_tab', tab: Number($('browser-tab').value) });
    document.querySelectorAll('[data-key]').forEach((button) => { button.onclick = () => action({ action: 'press', key: button.dataset.key, snapshot_id: state.snapshot }); });
    document.querySelectorAll('[data-scroll]').forEach((button) => { button.onclick = () => action({ action: 'scroll', scroll_y: Number(button.dataset.scroll) }); });
    $('browser-screen').onclick = (event) => {
        // Keyboard activation uses the explicit labelled position controls below.
        if (event.detail === 0) { $('position-form').closest('details').open = true; $('position-x').focus(); return; }
        const rect = $('browser-image').getBoundingClientRect();
        void action({ action: 'mouse_click', x: Math.min(1279, Math.max(0, Math.floor((event.clientX - rect.left) * 1280 / rect.width))), y: Math.min(799, Math.max(0, Math.floor((event.clientY - rect.top) * 800 / rect.height))), snapshot_id: state.snapshot });
    };
    $('position-form').onsubmit = (event) => {
        event.preventDefault();
        void action({ action: $('position-action').value, x: Number($('position-x').value), y: Number($('position-y').value), x2: Number($('position-x2').value), y2: Number($('position-y2').value), snapshot_id: state.snapshot });
    };
    $('retry').onclick = async () => {
        if (!waiting() || state.busy || state.pending || !state.snapshot) return;
        state.busy = true; controls();
        try {
            await api(base + '/human/retry', 'POST', { challenge_id: state.challenge.id });
            clearBrowser();
            await refresh();
        } catch (error) {
            message('status', 'Katcha could not confirm the continuation request. Refresh status before trying again. No success is assumed.', true);
            await access(error);
        } finally { state.busy = false; controls(); }
    };
    $('cancel').onclick = async () => {
        if (state.busy) return;
        state.busy = true; controls();
        try { await api(base + '/cancel', 'POST'); clearBrowser(); await refresh(); }
        catch (error) { message('status', 'Cancellation could not be confirmed. Refresh status to check the saved request.', true); await access(error); }
        finally { state.busy = false; controls(); }
    };
    if (valid) $('ask-katcha').href = '/ai?channel=' + encodeURIComponent(channel);
    void refresh();
    state.timer = setInterval(() => { if (!document.hidden && !state.busy) void refresh(); }, 10000);
    window.addEventListener('pagehide', () => { clearInterval(state.timer); clearBrowser(); });
})();
