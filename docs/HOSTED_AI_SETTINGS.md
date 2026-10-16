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

## Provider verification and recovery

A configured API key means only that a value is stored; it does not prove quota, model access, billing, or a successful generation. **Test OpenAI API** and **Test Gemini API** perform bounded live inference, and **Test inference** checks the Codex subscription separately. A test may consume a small amount of provider quota.

A saved Codex/ChatGPT connection can outlive its short-lived access token. Settings displays **REFRESH DUE** when the stored access token has expired, without claiming the refresh credential is invalid. Run the live test to attempt renewal; reconnect only if the test fails. A fresh green saved-session indicator is also not proof of live inference.

The hosted configuration endpoint requires a global wildcard-scoped operator principal. A 403 indicates insufficient operator scope and must not be worked around by loosening the endpoint's authorization policy. Settings stored in PostgreSQL apply to subsequent AI work; neither saves nor tests enable paid API fallback automatically. Production deployment still requires the separately approved guarded release process.
