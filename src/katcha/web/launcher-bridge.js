/* Shared workspace navigation + local launcher bridge. */
const KATCHA_WORKSPACES = [
    { key: 'home', label: 'Home', href: '/home', icon: '⌂' },
    { key: 'trends', label: 'Trends', href: '/explorer', icon: '◉' },
    { key: 'ai', label: 'Katcha AI', href: '/ai', icon: '✦' },
    { key: 'sources', label: 'Sources', href: '/ingestion', icon: '↳' },
    { key: 'clips', label: 'Clips', href: '/clips', icon: '▤' },
    { key: 'channel', label: 'Channel Studio', href: '/channels', icon: '▦' },
    { key: 'production', label: 'Production', href: '/editing', icon: '◇' },
    { key: 'studio', label: 'Clip Studio', href: '/studio', icon: '⌁' },
    { key: 'settings', label: 'Settings', href: '/settings', icon: '⚙' },
];

function workspaceKeyFromPath(path = location.pathname) {
    if (path.startsWith('/home') || path.endsWith('/home.html') || path.startsWith('/operations') || path.endsWith('/operations.html')) return 'home';
    if (path.startsWith('/ingestion') || path.endsWith('/ingestion.html')) return 'sources';
    if (path.startsWith('/clips') || path.endsWith('/clips.html')) return 'clips';
    if (path.startsWith('/channels') || path.endsWith('/channels.html')) return 'channel';
    if (path.startsWith('/ai') || path.endsWith('/ai.html')) return 'ai';
    if (path.endsWith('/studio.html') || path.startsWith('/studio')) return 'studio';
    if (path.startsWith('/content') || path.endsWith('/content.html') || path.startsWith('/editing') || path.endsWith('/editing.html')) return 'production';
    if (path.startsWith('/settings') || path.endsWith('/settings.html')) return 'settings';
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

function installTrendsStyleRecovery() {
    if (location.port !== '8765' || currentWorkspace().key !== 'trends') return;
    const shell = document.querySelector('.app-shell');
    if (!shell) return;
    const recover = () => {
        if (getComputedStyle(shell).display === 'grid') return;
        const assets = [
            'styles.css',
            'explorer-refresh.css',
            'system/aerith-tokens.css',
            'system/aerith-base.css',
            'system/aerith-shell.css',
            'system/aerith-components.css',
            'system/aerith-motion.css',
            'pages/trends-aerith.css',
        ];
        for (const asset of assets) {
            if (document.querySelector('link[data-katcha-style-recovery="' + asset + '"]')) continue;
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = '/explorer/assets/' + asset;
            link.dataset.katchaStyleRecovery = asset;
            document.head.append(link);
        }
    };
    if (document.readyState === 'complete') setTimeout(recover, 0);
    else window.addEventListener('load', recover, { once: true });
}

installTrendsStyleRecovery();

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
    const brand = document.querySelector('aside .brand, aside .logo, header .ops-brand');
    const aside = brand?.closest('aside') || brand?.closest('header');
    if (!brand || !aside || aside.querySelector('.workspace-menu')) return;

    brand.href = "/home";
    const current = currentWorkspace();
    const cluster = document.createElement('div');
    cluster.className = 'workspace-nav-cluster';
    brand.before(cluster);
    cluster.append(brand);

    /* Quick switcher stays available everywhere; the persistent tree below removes
       the old "remember what is inside the dropdown" navigation burden. */
    const details = document.createElement('details');
    details.className = 'workspace-menu';
    const links = KATCHA_WORKSPACES.map((workspace) =>
        '<a href="' + workspace.href + '" data-workspace-key="' + workspace.key + '" ' +
        (current.key === workspace.key ? 'aria-current="page"' : '') + '>' +
        '<span aria-hidden="true">' + workspace.icon + '</span>' +
        '<b>' + workspace.label + '</b></a>'
    ).join('');

    details.innerHTML =
        '<summary aria-label="Quick switch workspace">' +
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

    const groups = [
        { key: 'plan', label: 'Plan', workspaces: ['home', 'trends', 'sources'] },
        { key: 'create', label: 'Create', workspaces: ['clips', 'ai', 'production', 'studio'] },
        { key: 'grow', label: 'Grow', workspaces: ['channel'] },
    ];
    const tree = document.createElement('nav');
    tree.className = 'workspace-tree';
    tree.setAttribute('aria-label', 'Katcha workspaces');

    for (const group of groups) {
        const section = document.createElement('details');
        section.className = 'workspace-group';
        section.dataset.workspaceGroup = group.key;

        let savedState = null;
        try {
            savedState = localStorage.getItem('katcha.workspaceGroup.' + group.key);
        } catch {
            savedState = null;
        }
        const containsCurrent = group.workspaces.includes(current.key);
        section.open = containsCurrent || savedState !== 'closed';

        const rows = group.workspaces
            .map((key) => KATCHA_WORKSPACES.find((workspace) => workspace.key === key))
            .filter(Boolean)
            .map((workspace) => {
                const isCurrent = workspace.key === current.key;
                return '<a href="' + workspace.href + '" data-workspace-key="' + workspace.key + '" ' +
                    'class="workspace-tree-link' + (isCurrent ? ' is-current' : '') + '" ' +
                    (isCurrent ? 'aria-label="' + workspace.label + ', current workspace"' : '') + '>' +
                    '<span class="workspace-tree-icon" aria-hidden="true">' + workspace.icon + '</span>' +
                    '<b>' + workspace.label + '</b>' +
                '</a>';
            })
            .join('');

        section.innerHTML =
            '<summary><span>' + group.label + '</span><small>' + group.workspaces.length + '</small></summary>' +
            '<div class="workspace-group-body">' + rows + '</div>';
        section.addEventListener('toggle', () => {
            try {
                localStorage.setItem(
                    'katcha.workspaceGroup.' + group.key,
                    section.open ? 'open' : 'closed',
                );
            } catch {
                /* Navigation remains fully functional when storage is unavailable. */
            }
        });
        tree.append(section);
    }
    cluster.after(tree);

    const legacyNav = aside.querySelector('.rail-nav, nav[aria-label="Workspace"], nav:not(.stats):not(.workspace-tree)');
    if (legacyNav && legacyNav !== tree) legacyNav.remove();
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

function installSettingsShortcut() {
    const aside = document.querySelector('.rail, .sidebar');
    if (!aside || aside.querySelector('.rail-settings-shortcut')) return;
    const shortcut = document.createElement('a');
    shortcut.className = 'rail-settings-shortcut';
    shortcut.href = '/settings';
    shortcut.setAttribute('aria-label', 'Katcha settings');
    shortcut.setAttribute('title', 'Settings');
    shortcut.innerHTML = '<span aria-hidden="true">⚙</span><b>Settings</b>';
    const footer = aside.querySelector('.rail-footer, .sidebar-footer');
    if (footer) footer.before(shortcut);
    else aside.append(shortcut);
}
installSettingsShortcut();

/* The local gateway owns authentication; never expose its token to browser storage. */
(async () => {
    if (location.port !== '8765') return;
    const form = document.getElementById('connect-form') || document.getElementById('connect');
    const connection = document.getElementById('connection') || document.getElementById('connection-state');
    const status = document.getElementById('message') || document.getElementById('status');
    let workspaceConnected = false;

    const renderRuntimeState = (runtime) => {
        if (!connection) return;
        connection.classList.toggle('online', Boolean(runtime.workspace_ready));
        connection.classList.toggle('degraded', runtime.workspace_ready && runtime.phase === 'degraded');

        if (!runtime.workspace_ready) {
            connection.textContent = runtime.desired_running ? 'WARMING' : 'OFFLINE';
            connection.title = runtime.desired_running
                ? 'The local launcher is available, but the Katcha API is not ready yet.'
                : 'Katcha services are not running.';
            return;
        }
        if (runtime.phase === 'degraded') {
            connection.textContent = 'CONNECTED · DEGRADED';
            connection.title = 'The Katcha API is connected, but one or more background services need attention.';
            return;
        }
        if (runtime.phase === 'starting' || runtime.phase === 'reconnecting') {
            connection.textContent = 'CONNECTED · WARMING';
            connection.title = 'The Katcha API is connected while background services finish starting.';
            return;
        }
        connection.textContent = 'CONNECTED';
        connection.title = 'The Katcha API and required background services are healthy.';
    };

    for (;;) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 8000);
        try {
            const response = await fetch('/runtime/status', {cache: 'no-store', signal: controller.signal});
            if (!response.ok) throw new Error('launcher unavailable');
            const runtime = await response.json();
            if (!runtime.session) return;
            renderRuntimeState(runtime);

            if (runtime.workspace_ready && !workspaceConnected) {
                workspaceConnected = true;
                if (form) {
                    form.hidden = true;
                    form.style.display = 'none';
                    form.requestSubmit();
                }
            } else if (!runtime.workspace_ready) {
                workspaceConnected = false;
                if (status && !status.textContent.trim()) {
                    status.textContent = runtime.desired_running
                        ? 'Katcha is open. Core services are warming in the background…'
                        : 'Katcha is open. Start services from the launch console when ready.';
                }
            }
        } catch {
            workspaceConnected = false;
            if (connection) {
                connection.textContent = 'RECONNECTING';
                connection.classList.remove('online', 'degraded');
                connection.title = 'The browser lost contact with the local Katcha supervisor.';
            }
            if (status && !status.textContent.trim()) {
                status.textContent = 'Reconnecting to the local Katcha supervisor…';
            }
        } finally {
            clearTimeout(timeout);
        }
        await new Promise((resolve) => setTimeout(resolve, 3000));
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
