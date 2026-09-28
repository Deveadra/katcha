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
