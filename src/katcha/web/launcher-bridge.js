/* Shared workspace navigation + local launcher bridge. */
const KATCHA_WORKSPACES = [
    { key: 'trends', label: 'Trends', href: '/explorer', icon: '◉' },
    { key: 'ai', label: 'Katcha AI', href: '/ai', icon: '✦' },
    { key: 'sources', label: 'Sources', href: '/ingestion', icon: '↳' },
    { key: 'clips', label: 'Clips', href: '/clips', icon: '▤' },
    { key: 'channel', label: 'Channel Studio', href: '/channels', icon: '▦' },
    { key: 'production', label: 'Production', href: '/editing', icon: '◇' },
    { key: 'studio', label: 'Clip Studio', href: '/studio', icon: '⌁' },
];

function workspaceKeyFromPath(path = location.pathname) {
    if (path.startsWith('/ingestion') || path.endsWith('/ingestion.html')) return 'sources';
    if (path.startsWith('/clips') || path.endsWith('/clips.html')) return 'clips';
    if (path.startsWith('/channels') || path.endsWith('/channels.html')) return 'channel';
    if (path.startsWith('/ai') || path.endsWith('/ai.html')) return 'ai';
    if (path.startsWith('/editing') || path.endsWith('/editing.html')) return 'production';
    if (path.startsWith('/studio') || path.endsWith('/studio.html')) return 'studio';
    return 'trends';
}


function currentWorkspace(path = location.pathname) {
    const key = workspaceKeyFromPath(path);
    return KATCHA_WORKSPACES.find((workspace) => workspace.key === key) || KATCHA_WORKSPACES[0];
}

/* Compatibility protection for routes that do not yet load the shared system directly. */
function installAerithSystem() {
    const styles = [
        ['aerith-tokens', '/system/aerith-tokens.css'],
        ['aerith-base', '/system/aerith-base.css'],
        ['aerith-shell', '/system/aerith-shell.css'],
        ['aerith-components', '/system/aerith-components.css'],
        ['aerith-motion', '/system/aerith-motion.css'],
    ];
    for (const [id, href] of styles) {
        if (document.getElementById(id)) continue;
        const link = document.createElement('link');
        link.id = id;
        link.rel = 'stylesheet';
        link.href = href;
        document.head.append(link);
    }

    document.body.dataset.katchaPage = workspaceKeyFromPath();
    const theme = document.querySelector('meta[name="theme-color"]');
    if (theme) theme.content = '#090b12';
}

installAerithSystem();

function installSkipLink() {
    if (document.querySelector('.ae-skip-link')) return;
    const main = document.querySelector('main');
    if (!main) return;
    if (!main.id) main.id = 'main-content';
    const skip = document.createElement('a');
    skip.className = 'ae-skip-link';
    skip.href = '#' + main.id;
    skip.textContent = 'Skip to main content';
    document.body.prepend(skip);
}

function installWorkspaceMenu() {
    const brand = document.querySelector('aside .brand, aside .logo');
    const aside = brand?.closest('aside');
    if (!brand || !aside || aside.querySelector('.workspace-menu')) return;

    const current = currentWorkspace();
    const cluster = document.createElement('div');
    cluster.className = 'workspace-nav-cluster';
    brand.before(cluster);
    cluster.append(brand);

    const details = document.createElement('details');
    details.className = 'workspace-menu';
    const links = KATCHA_WORKSPACES.map((workspace) =>
        '<a href="' + workspace.href + '" ' +
        (current.key === workspace.key ? 'aria-current="page"' : '') + '>' +
        '<span aria-hidden="true">' + workspace.icon + '</span>' +
        '<b>' + workspace.label + '</b></a>'
    ).join('');

    details.innerHTML =
        '<summary aria-label="Open workspace menu">' +
            '<span class="workspace-menu-copy">' +
                '<small>WORKSPACE</small>' +
                '<strong>' + current.label + '</strong>' +
            '</span>' +
            '<span class="workspace-menu-chevron" aria-hidden="true">⌄</span>' +
        '</summary>' +
        '<div class="workspace-menu-popover">' +
            links +
            (location.port === '8765'
                ? '<a href="/"><span aria-hidden="true">⌂</span><b>System</b></a>'
                : '') +
        '</div>';
    cluster.append(details);

    const legacyNav = aside.querySelector('.rail-nav, nav[aria-label="Workspace"], nav:not(.stats)');
    if (legacyNav) legacyNav.remove();
    const legacyLabel = aside.querySelector('.rail-label, .sidebar-label');
    if (legacyLabel) legacyLabel.remove();

    const summary = details.querySelector('summary');
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape' || !details.open) return;
        details.open = false;
        summary.focus();
    });
    document.addEventListener('pointerdown', (event) => {
        if (details.open && !details.contains(event.target)) details.open = false;
    });
}

installSkipLink();
installWorkspaceMenu();

function installChatShortcut() {
    if (document.getElementById('katcha-chat-shortcut')) return;
    const isChat = currentWorkspace().key === 'ai';
    const shortcut = document.createElement('a');
    shortcut.id = 'katcha-chat-shortcut';
    shortcut.href = isChat ? '#prompt' : '/ai?focus=chat';
    shortcut.setAttribute('aria-label', isChat ? 'Jump to Katcha chat' : 'Chat with Katcha');
    shortcut.setAttribute('title', isChat ? 'Jump to chat' : 'Chat with Katcha');
    shortcut.innerHTML = '<span aria-hidden="true">✦</span><span class="katcha-chat-label">Ask Katcha</span>';
    if (isChat) shortcut.addEventListener('click', event => {
        const prompt = document.getElementById('prompt');
        if (!prompt || prompt.closest('[hidden]')) return;
        event.preventDefault();
        prompt.focus();
    });
    document.body.append(shortcut);
}
installChatShortcut();

/* The local gateway owns authentication; never expose its token to browser storage. */
(async () => {
    if (location.port !== '8765') return;
    const form = document.getElementById('connect-form') || document.getElementById('connect');
    const connection = document.getElementById('connection');
    const status = document.getElementById('message');
    for (;;) {
        try {
            const response = await fetch('/runtime/status', {cache: 'no-store'});
            if (!response.ok) throw new Error('launcher unavailable');
            const runtime = await response.json();
            if (!runtime.session) return;
            if (runtime.workspace_ready) {
                if (form) {
                    form.hidden = true;
                    form.style.display = 'none';
                    form.requestSubmit();
                }
                return;
            }
            if (connection) {
                connection.textContent = 'WARMING';
                connection.classList.remove('online');
            }
            if (status) {
                status.textContent = runtime.desired_running
                    ? 'Katcha is open. Core services are warming in the background…'
                    : 'Katcha is open. Start services from the launch console when ready.';
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
