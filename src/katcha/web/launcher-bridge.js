/* The local gateway owns authentication; never expose its token to browser storage. */
(async () => {
    if (location.port !== '8765') return;
    const nav = document.querySelector('nav');
    if (nav) {
        const link = document.createElement('a');
        link.href = '/launcher'; link.textContent = 'Diagnostics & settings'; nav.append(link);
    }
    const form = document.getElementById('connect-form') || document.getElementById('connect');
    const connection = document.getElementById('connection');
    const status = document.getElementById('message');
    if (form) {
        form.hidden = true;
        form.style.display = 'none';
    }
    for (;;) {
        try {
            const response = await fetch('/runtime/status', {cache: 'no-store'});
            if (!response.ok) throw new Error('launcher unavailable');
            const runtime = await response.json();
            if (!runtime.session) return;
            if (runtime.workspace_ready) {
                if (form) form.requestSubmit();
                return;
            }
            if (connection) {
                connection.textContent = 'WARMING';
                connection.classList.remove('online');
            }
            if (status) {
                status.textContent = runtime.desired_running
                    ? 'Katcha is open. Core services are warming in the background…'
                    : 'Katcha is open in safe mode. Open Diagnostics & settings to start services.';
            }
        } catch {
            if (connection) connection.textContent = 'RECONNECTING';
            if (status) status.textContent = 'Reconnecting to the local Katcha supervisor…';
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
    }
})();
if (location.port === '8765') {
    const report = (message) => fetch('/runtime/client-error', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: String(message), path: location.pathname}),
    }).catch(() => {});
    window.addEventListener('error', event => report(event.message));
    window.addEventListener('unhandledrejection', event => report(event.reason?.stack || event.reason));
}
