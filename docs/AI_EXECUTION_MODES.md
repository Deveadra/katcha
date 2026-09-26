# AI execution modes

Katcha separates development acceptance from paid provider execution.

## Fixture mode

`KATCHA_AI_EXECUTION_MODE=fixture` guarantees the built-in development path does not
call OpenAI or Gemini for analysis, scripting, packaging, long-form editorial, or
voice generation.

Fixture mode still exercises the real Katcha orchestration and media pipeline:

- deterministic AI-shaped analysis results;
- deterministic short and ranked-episode scripts;
- deterministic packaging and long-form editorial fixtures;
- local `espeak-ng` narration WAVs;
- normal Temporal workflows, database persistence, MinIO assets, Remotion rendering,
  review gates, and optional PRIVATE YouTube upload.

The local voice is deliberately a development voice. It validates timing, captions,
ducking, manifests, rendering, and recovery; it is not RankSnaxx's production voice.

For `KATCHA_ENV=development`, `KATCHA_AI_EXECUTION_MODE=auto` resolves to
`fixture`. This keeps development zero-cost even when provider keys are present.

## Live mode

`KATCHA_AI_EXECUTION_MODE=live` enables external AI/TTS providers. For
`KATCHA_ENV=production`, `auto` resolves to `live`.

The default live routing policy is `free_first`. Katcha prefers the configured
Gemini route first, then falls back to the configured OpenAI route when an explicit
safe rejection occurs. Existing legacy `balanced` channel strategies are treated
as free-first so current channels receive the new behavior without rewriting stored
strategy history.

Free-first is a routing preference, not a billing guarantee. Whether a request is
actually free depends on the provider account, project, model, quota, and billing
state.

Use:

```env
KATCHA_AI_ENABLED=true
KATCHA_AI_EXECUTION_MODE=live
KATCHA_AI_LIVE_ROUTING_MODE=free_first
```

The normal Katcha monthly/channel budget protections still apply in live mode.

## Runtime visibility

Authenticated clients can inspect:

```text
GET /v1/runtime/ai
```

It reports only non-secret state:

```json
{
  "execution_mode": "fixture",
  "live_routing_mode": "free_first",
  "external_provider_calls_enabled": false
}
```

The RankSnaxx private acceptance runner prints this state before it starts so a test
cannot silently become a paid run.
