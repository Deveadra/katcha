# Hosted AI settings

Settings now detects the local launcher or the hosted application. The hosted
app reads existing server environment configuration automatically; blank key
inputs preserve those credentials. A local checkout's `.env` is not implicitly
uploaded to production.

A global authenticated operator can save shared AI provider choices and keys
through `/v1/integrations/ai/settings`. Keys are encrypted with the deployment's
existing credential encryption key in `application_ai_settings`; responses expose
only whether a credential is configured. Audit events contain changed field names,
never values. Channel-scoped principals cannot access this configuration.

API requests and AI worker activities resolve the shared overrides when starting
work. An already-running provider request retains its original settings. Saving
neither calls a provider nor enables paid fallback unless the operator selects it.
Local launcher configuration and its apply/restart behavior remain available.

Migration `0064_application_ai_settings` is additive. It does not copy secrets,
change existing credentials, alter saved channel policies, or restart production.
Prior images ignore this new table, so code rollback does not require deleting it.
