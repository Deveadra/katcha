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

## ChatGPT plan connection

For normal operator conversation and command interpretation, Katcha can use **Sign in
with ChatGPT** instead of requiring an OpenAI API key. Open **Settings** from the
persistent gear icon and choose **Continue with ChatGPT**. Katcha uses OpenAI's
open-source OAuth flow, keeps the renewable credentials encrypted with Katcha's local
credential key, and uses only models returned for that signed-in ChatGPT account.

ChatGPT-plan inference uses the public Responses API with `store=false` and
`stream=true`. Katcha records this usage as `chatgpt/<model>` with zero API-dollar
cost in its own usage ledger. The user's ChatGPT plan still has its own usage limits.

Provider order for the Command Center is:

1. connected ChatGPT plan;
2. configured API-key providers when the plan is unavailable or exhausted;
3. deterministic stored-data fallback after live providers are exhausted.

Specialist background workloads can continue to use API providers when their native
capabilities are required.

## ChatGPT plan connection

For normal operator conversation and command interpretation, Katcha can use **Sign in
with ChatGPT** instead of requiring an OpenAI API key. Open **Settings** from the
persistent gear icon and choose **Continue with ChatGPT**. Katcha uses OpenAI's
open-source OAuth flow, keeps the renewable credentials encrypted with Katcha's local
credential key, and uses only models returned for that signed-in ChatGPT account.

ChatGPT-plan inference uses the public Responses API with `store=false` and
`stream=true`. Katcha records this usage as `chatgpt/<model>` with zero API-dollar
cost in its own usage ledger. The user's ChatGPT plan still has its own usage limits.

Provider order for the Command Center is:

1. connected ChatGPT plan;
2. configured API-key providers when the plan is unavailable or exhausted;
3. deterministic stored-data fallback after live providers are exhausted.

Specialist background workloads can continue to use API providers when their native
capabilities are required.

## Live mode

`KATCHA_AI_EXECUTION_MODE=live` enables external AI/TTS providers. For
`KATCHA_ENV=production`, `auto` resolves to `live`.

In Settings or the local launch console, selecting **Live — uses configured providers**
also sets `KATCHA_AI_ENABLED=true` when the setting is saved. Restart services
to apply it. The Katcha AI chat displays its effective mode and provider
readiness; fixture mode and failed provider calls are labeled in the answer.
When a natural-language request cannot be interpreted because live planning is
unavailable, the chat keeps the draft for retry instead of silently answering
an unrelated channel-status question. Actions still require their explicit
confirmation step.

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
