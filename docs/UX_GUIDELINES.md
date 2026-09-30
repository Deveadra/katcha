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
- Visual acceptance must check computed layout/material properties, not only DOM presence. A page that renders all text but loses its grid, glass panels, spacing, or hierarchy is a regression.
- Shared launcher tests must verify the real CSS/JS assets required by a workspace, not only its HTML document.
