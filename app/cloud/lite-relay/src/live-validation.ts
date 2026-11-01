import { buildOpenRouterProviderPolicy, normalizeModelList, normalizeProviderStream } from "./adapters";
import { fetchProviderWithoutRedirect, resolveSecret, type SecretBindingId } from "./security";
import type { CanonicalProviderError, NormalizedStream, Provider } from "./types";

const LIVE_PROVIDERS = ["gemini", "openai", "anthropic", "xai", "openrouter"] as const;
const CONFIRMATION = "PHASE0_LIVE_API_APPROVED";
const MAX_OUTPUT_TOKENS = 32;
const CALLS_PER_PROVIDER = 3;
const FIXED_PROMPT = "Synthetic validation only. Reply with exactly: PHASE0_OK";

type LiveProvider = (typeof LIVE_PROVIDERS)[number];

interface ProviderConfig {
  secretBinding: SecretBindingId;
  model: string;
  listUrl: string;
  streamUrl: string;
  inputUsdPerMillion: number;
  outputUsdPerMillion: number;
}

interface LiveValidationInput {
  run_id: unknown;
  provider: unknown;
  confirmation: unknown;
}

interface SafeStreamResult {
  terminal_status: NormalizedStream["terminal_status"];
  text_matches: boolean;
  usage: NormalizedStream["usage"];
  model_requested: string;
  model_resolved: string | null;
  upstream_provider: string | null;
  routing_violation: NormalizedStream["routing_violation"];
  unknown_event_count: number;
  request_id: string | null;
}

interface SafeFailure {
  ok: false;
  error: CanonicalProviderError;
  request_id: string | null;
}

const LIVE_RUN_ID = /^[a-z0-9][a-z0-9-]{0,47}$/;

function safeError(status: number | null, code: string | null): CanonicalProviderError {
  let category: CanonicalProviderError["category"] = "provider_error";
  let retryable = false;
  let safeMessage = "プロバイダでエラーが発生しました。";
  if (status === 401 || status === 403) {
    category = "auth";
    safeMessage = "プロバイダの認証に失敗しました。";
  } else if (status === 429 || code === "insufficient_quota" || code === "credit_balance_too_low") {
    category = "rate_limit";
    safeMessage = "プロバイダの利用上限または残高上限に達しました。";
  } else if (status === 400 || status === 422) {
    category = "invalid_request";
    safeMessage = "プロバイダへ送る内容を確認できませんでした。";
  } else if (status === 404) {
    category = "model_unavailable";
    safeMessage = "選択したモデルを利用できません。";
  } else if (status !== null && status >= 500) {
    retryable = true;
  }
  return {
    category,
    http_status: status,
    provider_code: code,
    retryable,
    retry_after_seconds: null,
    request_may_be_billed:
      code !== "credit_balance_too_low" &&
      status !== null &&
      status >= 200 &&
      status < 500 &&
      status !== 401 &&
      status !== 403,
    safe_message_ja: safeMessage,
  };
}

function requestId(response: Response): string | null {
  return response.headers.get("x-request-id") ?? response.headers.get("request-id") ?? null;
}

async function safeProviderCode(response: Response): Promise<string | null> {
  try {
    const payload = (await response.clone().json()) as Record<string, unknown>;
    const rawError = payload.error;
    if (!rawError || typeof rawError !== "object") return null;
    const error = rawError as Record<string, unknown>;
    const message = typeof error.message === "string" ? error.message.toLowerCase() : "";
    if (message.includes("credit balance") || message.includes("insufficient credit")) {
      return "credit_balance_too_low";
    }
    return typeof error.code === "string" ? error.code : typeof error.type === "string" ? error.type : null;
  } catch {
    return null;
  }
}

function configFor(provider: LiveProvider, env: Env): ProviderConfig {
  const configs: Record<LiveProvider, ProviderConfig> = {
    gemini: {
      secretBinding: "GEMINI_PERSONAL_1",
      model: env.PHASE0_GEMINI_MODEL,
      listUrl: "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
      streamUrl: `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(env.PHASE0_GEMINI_MODEL)}:streamGenerateContent?alt=sse`,
      inputUsdPerMillion: 0.1,
      outputUsdPerMillion: 0.4,
    },
    openai: {
      secretBinding: "OPENAI_PERSONAL_1",
      model: env.PHASE0_OPENAI_MODEL,
      listUrl: "https://api.openai.com/v1/models",
      streamUrl: "https://api.openai.com/v1/responses",
      inputUsdPerMillion: 0.2,
      outputUsdPerMillion: 1.25,
    },
    anthropic: {
      secretBinding: "ANTHROPIC_PERSONAL_1",
      model: env.PHASE0_ANTHROPIC_MODEL,
      listUrl: "https://api.anthropic.com/v1/models?limit=1000",
      streamUrl: "https://api.anthropic.com/v1/messages",
      inputUsdPerMillion: 1,
      outputUsdPerMillion: 5,
    },
    xai: {
      secretBinding: "XAI_PERSONAL_1",
      model: env.PHASE0_XAI_MODEL,
      listUrl: "https://api.x.ai/v1/language-models",
      streamUrl: "https://api.x.ai/v1/chat/completions",
      inputUsdPerMillion: 1.25,
      outputUsdPerMillion: 2.5,
    },
    openrouter: {
      secretBinding: "OPENROUTER_PERSONAL_1",
      model: env.PHASE0_OPENROUTER_MODEL,
      listUrl: "https://openrouter.ai/api/v1/models",
      streamUrl: "https://openrouter.ai/api/v1/chat/completions",
      inputUsdPerMillion: 0.2,
      outputUsdPerMillion: 1.25,
    },
  };
  const config = configs[provider];
  if (!config.model) throw new Error("live_model_not_configured");
  return config;
}

function headersFor(provider: LiveProvider, secret: string): Headers {
  const headers = new Headers({ Accept: "application/json", "Content-Type": "application/json" });
  if (provider === "gemini") headers.set("x-goog-api-key", secret);
  if (provider === "openai" || provider === "xai" || provider === "openrouter") {
    headers.set("Authorization", `Bearer ${secret}`);
  }
  if (provider === "anthropic") {
    headers.set("x-api-key", secret);
    headers.set("anthropic-version", "2023-06-01");
  }
  if (provider === "openrouter") headers.set("X-OpenRouter-Metadata", "enabled");
  return headers;
}

function bodyFor(provider: LiveProvider, model: string, runId: string): string {
  if (provider === "gemini") {
    return JSON.stringify({
      contents: [{ role: "user", parts: [{ text: FIXED_PROMPT }] }],
      generationConfig: { maxOutputTokens: MAX_OUTPUT_TOKENS, temperature: 0 },
    });
  }
  if (provider === "xai") {
    return JSON.stringify({
      model,
      messages: [{ role: "user", content: FIXED_PROMPT }],
      max_tokens: MAX_OUTPUT_TOKENS,
      reasoning_effort: "none",
      stream: true,
      stream_options: { include_usage: true },
    });
  }
  if (provider === "openai") {
    return JSON.stringify({
      model,
      input: FIXED_PROMPT,
      max_output_tokens: MAX_OUTPUT_TOKENS,
      stream: true,
      store: false,
      reasoning: { effort: "none" },
    });
  }
  if (provider === "anthropic") {
    return JSON.stringify({
      model,
      max_tokens: MAX_OUTPUT_TOKENS,
      stream: true,
      temperature: 0,
      messages: [{ role: "user", content: FIXED_PROMPT }],
    });
  }
  return JSON.stringify({
    model,
    messages: [{ role: "user", content: FIXED_PROMPT }],
    max_tokens: MAX_OUTPUT_TOKENS,
    temperature: 0,
    stream: true,
    stream_options: { include_usage: true },
    session_id: `phase0-${runId}`,
    provider: buildOpenRouterProviderPolicy(["OpenAI"]),
  });
}

function parseInput(input: LiveValidationInput): { runId: string; provider: LiveProvider } {
  if (
    typeof input.run_id !== "string" ||
    !LIVE_RUN_ID.test(input.run_id) ||
    typeof input.provider !== "string" ||
    !LIVE_PROVIDERS.includes(input.provider as LiveProvider) ||
    input.confirmation !== CONFIRMATION
  ) {
    throw new Error("invalid_live_validation_request");
  }
  return { runId: input.run_id, provider: input.provider as LiveProvider };
}

async function reserveOnce(db: D1Database, runId: string, provider: LiveProvider): Promise<void> {
  await db
    .prepare(
      "INSERT INTO audit_events (audit_event_id, event_type, occurred_at, outcome, detail_code) VALUES (?, 'phase0_live_validation', ?, 'started', ?)",
    )
    .bind(`phase0-live-${runId}-${provider}`, new Date().toISOString(), provider)
    .run();
}

async function finishAudit(db: D1Database, runId: string, provider: LiveProvider, outcome: string): Promise<void> {
  await db
    .prepare("UPDATE audit_events SET outcome = ? WHERE audit_event_id = ?")
    .bind(outcome, `phase0-live-${runId}-${provider}`)
    .run();
}

async function fetchChecked(
  provider: LiveProvider,
  url: string,
  init: RequestInit,
  fetcher: typeof fetch,
): Promise<Response | SafeFailure> {
  const response = await fetchProviderWithoutRedirect(provider, url, init, fetcher);
  if (response.ok) return response;
  return {
    ok: false,
    error: safeError(response.status, await safeProviderCode(response)),
    request_id: requestId(response),
  };
}

function safeStream(raw: string, provider: LiveProvider, model: string, response: Response): SafeStreamResult {
  const normalized = normalizeProviderStream(provider, raw, model);
  return {
    terminal_status: normalized.terminal_status,
    text_matches: normalized.text.trim() === "PHASE0_OK",
    usage: normalized.usage,
    model_requested: normalized.model_requested,
    model_resolved: normalized.model_resolved,
    upstream_provider: normalized.upstream_provider,
    routing_violation: normalized.routing_violation,
    unknown_event_count: normalized.unknown_event_count,
    request_id: requestId(response),
  };
}

export async function validateLiveProvider(
  input: LiveValidationInput,
  env: Env,
  fetcher: typeof fetch = fetch,
): Promise<Record<string, unknown>> {
  const { runId, provider } = parseInput(input);
  const config = configFor(provider, env);
  const estimatedMaximumUsd =
    CALLS_PER_PROVIDER * ((128 * config.inputUsdPerMillion + MAX_OUTPUT_TOKENS * config.outputUsdPerMillion) / 1_000_000);
  if (estimatedMaximumUsd >= 0.01) throw new Error("live_cost_guard_rejected");

  try {
    await reserveOnce(env.DB, runId, provider);
  } catch {
    throw new Error("live_validation_already_attempted");
  }

  const secret = resolveSecret(config.secretBinding, env);
  const headers = headersFor(provider, secret);
  if (provider === "xai") headers.set("x-grok-conv-id", `phase0-${runId}`);
  const listResult = await fetchChecked(provider, config.listUrl, { method: "GET", headers }, fetcher);
  if (!(listResult instanceof Response)) {
    await finishAudit(env.DB, runId, provider, "failed_known");
    return { ok: false, provider, stage: "models", model: config.model, estimated_maximum_usd: estimatedMaximumUsd, failure: listResult };
  }
  const models = normalizeModelList(provider, await listResult.json());
  const modelAvailable = models.some((model) => model.model_id === config.model);
  if (!modelAvailable) {
    await finishAudit(env.DB, runId, provider, "model_unavailable");
    return {
      ok: false,
      provider,
      stage: "models",
      model: config.model,
      model_count: models.length,
      model_available: false,
      estimated_maximum_usd: estimatedMaximumUsd,
    };
  }

  const normal: Array<SafeStreamResult | SafeFailure> = [];
  for (let index = 0; index < 2; index += 1) {
    const response = await fetchChecked(
      provider,
      config.streamUrl,
      { method: "POST", headers, body: bodyFor(provider, config.model, runId) },
      fetcher,
    );
    if (!(response instanceof Response)) {
      normal.push(response);
      await finishAudit(env.DB, runId, provider, "failed_known");
      return {
        ok: false,
        provider,
        stage: index === 0 ? "stream_first" : "stream_cache_probe",
        model: config.model,
        model_count: models.length,
        model_available: true,
        streams: normal,
        estimated_maximum_usd: estimatedMaximumUsd,
      };
    }
    normal.push(safeStream(await response.text(), provider, config.model, response));
  }

  const cutoffResponse = await fetchChecked(
    provider,
    config.streamUrl,
    { method: "POST", headers, body: bodyFor(provider, config.model, runId) },
    fetcher,
  );
  let cutoff: Record<string, unknown> | SafeFailure;
  if (cutoffResponse instanceof Response) {
    const reader = cutoffResponse.body?.getReader();
    const first = reader ? await reader.read() : { done: true, value: undefined };
    await reader?.cancel("phase0_client_cutoff");
    cutoff = {
      first_chunk_received: !first.done && Boolean(first.value?.byteLength),
      terminal_status: "outcome_unknown",
      request_may_be_billed: true,
      request_id: requestId(cutoffResponse),
    };
  } else {
    cutoff = cutoffResponse;
  }

  const streamsOk = normal.every(
    (result) => "terminal_status" in result && result.terminal_status === "completed" && result.text_matches,
  );
  const routingOk = normal.every((result) => !("routing_violation" in result) || result.routing_violation === null);
  const ok = streamsOk && routingOk && "terminal_status" in cutoff && cutoff.terminal_status === "outcome_unknown";
  await finishAudit(env.DB, runId, provider, ok ? "completed" : "failed_known");

  return {
    ok,
    provider,
    model: config.model,
    model_count: models.length,
    model_available: true,
    model_list_request_id: requestId(listResult),
    streams: normal,
    cutoff,
    fixed_prompt_sha256: "172215d0356215de6e0219bf160223c6dd8be290c9a32244f9d614bae673c580",
    max_output_tokens: MAX_OUTPUT_TOKENS,
    estimated_maximum_usd: estimatedMaximumUsd,
  };
}
