# Katcha product UX guidelines

Katcha must be usable without development knowledge. Apply these rules to new
screens and changes to existing screens; Content sources is the first application
of this standard, not a claim that every older screen has been redesigned.

## Lead with the user's task

- Ask what the user wants to accomplish before asking for configuration.
- Name actions by their outcome: “Search now”, “Add links”, “Save source”. Avoid
  storage or orchestration terminology such as “execute”, “run key”, and “items”.
- Show only the fields needed for the selected path. Use a short summary before
  saving settings with consequences. Explain what saving does and what starts work.
- Generate identifiers and choose integration adapters internally. Never require
  people to invent source keys, batch keys, or idempotency keys.
- Use real channel names. A shared collection means unassigned; it must not imply
  content is automatically sent to every channel.

## Make uncertainty and recovery visible

- Distinguish loading, empty, unavailable, and failed states. An empty dropdown is
  not an explanation. Tell the user what happened and offer a usable next action.
- Do not silently substitute a shared/default target when a channel request fails.
- Preserve entries after validation or connection errors. Reuse request identity
  when retrying an uncertain save or start so failures do not duplicate work.
- Translate status codes into readable progress. Put detailed provider errors,
  IDs and raw configuration behind troubleshooting/advanced disclosures.
- Keep keyboard focus logical, inputs labeled, feedback announced, and controls
  usable at mobile widths. Hidden steps must not trap validation or focus.

## Be precise about capability

- Explain any unavoidable unfamiliar term next to the decision it affects.
- Prefer sensible defaults with optional disclosures over a wall of settings.
- Advanced options must describe their effect. A metadata preference is not an
  enforced approval gate; saving a source is not searching or publishing.
- Never imply an unavailable crawler, scheduler, automatic approval, or connection
  is active. Disabled options must explain why.
- For new adapters, add a task-focused guided form when practical. An advanced
  JSON fallback preserves extensibility but is not a finished newcomer experience.

## Validate the user's journey

For a changed flow, check its successful path and relevant empty/error/retry
states, keyboard use and mobile layout. Verify the requests made by the UI match
its promises. Use mocked providers for repeatable browser tests, label fixtures in
test artifacts, and never represent them as live discovery or production proof.


## Aerith shared-system contract

Katcha's operator workspaces must behave like one product, not seven individually styled pages.

- Canonical workspace labels are **Home**, **Trends**, **Katcha AI**, **Sources**, **Clips**, **Channel Studio**, **Production**, and **Clip Studio**. The runtime workspace registry in `launcher-bridge.js` is the navigation source of truth.
- Every primary page loads the shared Aerith token, base, shell, component, and motion styles in `<head>` before page-specific Aerith overrides. Runtime injection exists only as compatibility protection.
- Shared workspace navigation, the Ask Katcha shortcut, skip navigation, focus behavior, and shell materials belong in the shared system. Do not reimplement them page-by-page.
- Secondary explanation belongs in the reusable `.ae-help` disclosure. Keep failure state, destructive-action consequences, recovery controls, monetization/YPP authority notes, security warnings, and anything required to make a safe decision visible without expansion.
- Workspace menus must close on Escape and outside interaction, expose one `aria-current="page"` item, and retain keyboard-visible focus.
- New or materially changed pages must remain free of document-level horizontal overflow at 390px and preserve usable keyboard navigation.
- `web-tests/system.cjs` is the cross-route contract test. Update it when intentionally changing canonical workspace names or shared-shell behavior.


### Aerith asset and shell ownership

The Aerith design system is the source of truth for primary Katcha workspace presentation.

- `system/aerith-shell.css` owns primary shell geometry: desktop rail/sidebar, main canvas, topbar, responsive shell collapse, shared connection/status presentation, workspace menu, and Ask Katcha.
- `system/aerith-components.css` owns reusable controls, panels, glass materials, focus treatment, and progressive disclosure.
- Workspace-specific Aerith layout belongs in `web/pages/<workspace>-aerith.css`. It may arrange that workspace's content, but it must not recreate the global shell.
- Primary workspace HTML must reference CSS and JavaScript with absolute web-root paths. Route-relative asset URLs are forbidden because launcher routes have different prefixes.
- A new Aerith workspace must not depend on an unrelated legacy stylesheet merely to obtain shell geometry. Legacy CSS may remain temporarily for unmigrated feature internals only.
- Visual acceptance must check computed layout/material properties, not only DOM presence. A page that renders all text but loses its grid, intended material treatment, spacing, or hierarchy is a regression.
- Shared launcher tests must verify the real CSS/JS assets required by a workspace, not only its HTML document.


## Katcha Crystal design-kit contract

The uploaded ALL-OUT design kit is now the visual foundation for Katcha. Google
Material, Apple HIG, and Salesforce Lightning are interaction references; Katcha
keeps its own brand rather than visually cloning any of them.

### Information architecture

- The primary mental model is **Plan → Create → Grow**. Keep top-level workspaces
  visible in the persistent desktop tree; the workspace menu is a quick switcher,
  not the only way to discover navigation.
- Use collapsible section headers for groups of related destinations or controls.
  A collapsed section must never hide an error, destructive consequence, active
  failure, required approval, or other information needed for a safe decision.
- Prefer 3–5 local tabs for peer views within one workspace. Do not nest tabs.
  Advanced or optional settings belong in disclosures, drawers, or focused dialogs.
- Keep Katcha's monetization signals actionable: surface contribution margin,
  growth pressure, monetization progress, and the next evidence-backed action
  before low-value diagnostics.

### Visual hierarchy

- Use the kit's midnight/pearl base. Content surfaces are opaque by default.
  Reserve glass for navigation, overlays, and media controls.
- Prismatic color is emphasis, not wallpaper. Use it for the brand accent, a
  primary action, or a single focal edge/zone. Never use gradients on paragraphs.
- Default corners are precise (2px). Use 6px only where a softer overlay or
  floating control genuinely benefits from it. Avoid indiscriminate pills.
- Use the shared 4px spacing scale and keep ordinary page content within the
  1248px content measure. Do not reintroduce page-specific spacing systems.
- Interactive controls must retain generous targets, visible labels, clear
  keyboard focus, and readable disabled/error states.

### Component behavior

- Prefer native semantic controls and disclosures. Keep advanced fields out of
  the default path unless prior saved state makes them immediately relevant.
- One card should express one task or idea. When several metrics form one status
  summary, use a divided stat strip instead of a field of decorative cards.
- Buttons represent actions; tabs represent views; menu items represent
  destinations or secondary actions. Do not interchange these roles for styling.
- Empty, loading, degraded, failed, and success states remain visually and
  textually distinct. Status must never depend on color alone.
- Motion uses the shared fast/panel/reveal timings and always honors
  `prefers-reduced-motion`.

### Token ownership

`system/aerith-tokens.css` remains the single source of truth for Katcha's
shared palette, spacing, radii, motion, and material primitives. Page styles
should compose those tokens rather than hard-code a parallel theme. Do not add
the design kit's bundled font files to the repository; use approved product/web
font delivery or the system font stack instead.
