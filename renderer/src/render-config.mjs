const positiveInteger = (raw, fallback, minimum = 1) => {
  const parsed = Number.parseInt(String(raw ?? ''), 10);
  if (!Number.isFinite(parsed) || parsed < minimum) {
    return fallback;
  }
  return parsed;
};

export const resolveRenderSettings = (env = process.env) => ({
  concurrency: positiveInteger(env.KATCHA_RENDER_CONCURRENCY, 1),
  timeoutInMilliseconds: positiveInteger(
    env.KATCHA_RENDER_TIMEOUT_MS,
    120000,
    30000,
  ),
});
