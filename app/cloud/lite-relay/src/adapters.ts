import { parseJsonRecord, parseSseText, type SseRecord } from "./sse";
import type {
  CanonicalProviderError,
  CanonicalStreamEvent,
  CanonicalUsage,
  NormalizedStream,
  Provider,
  ProviderModel,
} from "./types";

type JsonObject = Record<string, unknown>;

function object(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonObject) : {};
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function string(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function emptyUsage(status: CanonicalUsage["usage_status"] = "missing"): CanonicalUsage {
  return {
    input_tokens: null,
    output_tokens: null,
    cache_read_tokens: null,
    cache_creation_tokens: null,
    reasoning_tokens: null,
    provider_reported_cost_usd: null,
    cache_creation_5m_tokens: null,
    cache_creation_1h_tokens: null,
    cache_ttl_seconds: null,
    cache_status: "unreported",
    usage_status: status,
  };
}

function mergeUsage(base: CanonicalUsage, patch: Partial<CanonicalUsage>): CanonicalUsage {
  const result = { ...base, ...patch, usage_status: "reported" as const };
  if ((result.cache_read_tokens ?? 0) > 0) result.cache_status = "hit";
  else if ((result.cache_creation_tokens ?? 0) > 0) result.cache_status = "created";
  else if (result.cache_read_tokens === 0 || result.cache_creation_tokens === 0) result.cache_status = "miss";
  return result;
}

function providerError(status: number | null, code: string | null): CanonicalProviderError {
  let category: CanonicalProviderError["category"] = "provider_error";
  let retryable = false;
  let safeMessage = "プロバイダでエラーが発生しました。";
  if (status === 401 || status === 403) {
    category = "auth";
    safeMessage = "プロバイダの認証に失敗しました。";
  } else if (status === 429 || code === "rate_limit_exceeded" || code === "overloaded_error") {
    category = "rate_limit";
    retryable = true;
    safeMessage = "プロバイダの利用上限に達しました。";
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
    request_may_be_billed: false,
    safe_message_ja: safeMessage,
  };
}

function geminiContentRefusal(code: string): CanonicalProviderError {
  return {
    category: "invalid_request",
    http_status: null,
    provider_code: code,
    retryable: false,
    retry_after_seconds: null,
    request_may_be_billed: false,
    safe_message_ja: "Geminiが内容を確定応答として返しませんでした。表現を変えて新しいメッセージを送ってください。",
  };
}

function emitStarted(events: CanonicalStreamEvent[], requested: string, resolved: string | null): void {
  if (!events.some((event) => event.type === "response.started")) {
    events.push({
      schema_version: 1,
      type: "response.started",
      model_requested: requested,
      model_resolved: resolved,
    });
  }
}

function emitText(events: CanonicalStreamEvent[], text: string | null): string {
  if (!text) {
    return "";
  }
  events.push({ schema_version: 1, type: "response.text.delta", text });
  return text;
}

function finishResult(
  provider: Provider,
  modelRequested: string,
  events: CanonicalStreamEvent[],
  text: string,
  usage: CanonicalUsage,
  modelResolved: string | null,
  upstreamProvider: string | null,
  routingViolation: NormalizedStream["routing_violation"],
  completed: boolean,
  failed: boolean,
  unknownEventCount: number,
  partialReason: "stream_interrupted" | "max_output_tokens" = "stream_interrupted",
): NormalizedStream {
  if (usage.usage_status !== "missing") {
    events.push({ schema_version: 1, type: "response.usage", usage });
  }
  let terminalStatus: NormalizedStream["terminal_status"];
  if (failed) {
    terminalStatus = "failed_known";
  } else if (partialReason === "max_output_tokens") {
    terminalStatus = "partial";
    events.push({ schema_version: 1, type: "response.partial", reason: partialReason });
  } else if (completed) {
    terminalStatus = "completed";
    events.push({
      schema_version: 1,
      type: "response.committed",
      model_resolved: modelResolved,
      upstream_provider: upstreamProvider,
    });
  } else {
    terminalStatus = "partial";
    events.push({ schema_version: 1, type: "response.partial", reason: partialReason });
  }
  return {
    provider,
    events,
    terminal_status: terminalStatus,
    text,
    usage,
    model_requested: modelRequested,
    model_resolved: modelResolved,
    upstream_provider: upstreamProvider,
    routing_violation: routingViolation,
    unknown_event_count: unknownEventCount,
  };
}

function normalizeGemini(records: SseRecord[], requested: string): NormalizedStream {
  const events: CanonicalStreamEvent[] = [];
  let text = "";
  let usage = emptyUsage();
  let resolved: string | null = null;
  let completed = false;
  let failed = false;
  let maxOutputReached = false;
  let unknown = 0;

  for (const record of records) {
    const payload = object(parseJsonRecord(record));
    if (Object.keys(payload).length === 0) {
      unknown += 1;
      continue;
    }
    if (payload.error) {
      const rawError = object(payload.error);
      events.push({
        schema_version: 1,
        type: "response.error",
        error: providerError(number(rawError.code), string(rawError.status)),
      });
      failed = true;
      continue;
    }
    const promptBlockReason = string(object(payload.promptFeedback).blockReason);
    if (promptBlockReason && promptBlockReason !== "BLOCK_REASON_UNSPECIFIED") {
      events.push({
        schema_version: 1,
        type: "response.error",
        error: geminiContentRefusal(`prompt_${promptBlockReason}`),
      });
      failed = true;
      continue;
    }
    resolved = string(payload.modelVersion) ?? resolved;
    emitStarted(events, requested, resolved);
    for (const candidate of array(payload.candidates)) {
      const candidateObject = object(candidate);
      const parts = array(object(candidateObject.content).parts);
      for (const part of parts) {
        const delta = string(object(part).text);
        text += emitText(events, delta);
      }
      const finishReason = string(candidateObject.finishReason);
      if (finishReason === "STOP") {
        completed = true;
      } else if (finishReason === "MAX_TOKENS") {
        maxOutputReached = true;
      } else if (
        finishReason &&
        !["FINISH_REASON_UNSPECIFIED", "MAX_TOKENS"].includes(finishReason)
      ) {
        events.push({
          schema_version: 1,
          type: "response.error",
          error: geminiContentRefusal(`candidate_${finishReason}`),
        });
        failed = true;
      }
    }
    const rawUsage = object(payload.usageMetadata);
    if (Object.keys(rawUsage).length > 0) {
      usage = mergeUsage(usage, {
        input_tokens: number(rawUsage.promptTokenCount),
        output_tokens: number(rawUsage.candidatesTokenCount),
        cache_read_tokens: number(rawUsage.cachedContentTokenCount),
      });
    }
  }
  return finishResult(
    "gemini", requested, events, text, usage, resolved, null, null, completed, failed, unknown,
    maxOutputReached ? "max_output_tokens" : "stream_interrupted",
  );
}

function normalizeOpenAi(records: SseRecord[], requested: string): NormalizedStream {
  const events: CanonicalStreamEvent[] = [];
  let text = "";
  let usage = emptyUsage();
  let resolved: string | null = null;
  let completed = false;
  let failed = false;
  let maxOutputReached = false;
  let unknown = 0;

  for (const record of records) {
    const payload = object(parseJsonRecord(record));
    const type = string(payload.type) ?? record.event;
    if (type === "response.created" || type === "response.in_progress") {
      const response = object(payload.response);
      resolved = string(response.model) ?? resolved;
      emitStarted(events, requested, resolved);
    } else if (type === "response.output_text.delta") {
      text += emitText(events, string(payload.delta));
    } else if (type === "response.completed") {
      const response = object(payload.response);
      resolved = string(response.model) ?? resolved;
      const rawUsage = object(response.usage);
      const inputDetails = object(rawUsage.input_tokens_details);
      const outputDetails = object(rawUsage.output_tokens_details);
      usage = mergeUsage(usage, {
        input_tokens: number(rawUsage.input_tokens),
        output_tokens: number(rawUsage.output_tokens),
        cache_read_tokens: number(inputDetails.cached_tokens),
        reasoning_tokens: number(outputDetails.reasoning_tokens),
      });
      completed = true;
    } else if (type === "response.incomplete") {
      const response = object(payload.response);
      resolved = string(response.model) ?? resolved;
      const reason = string(object(response.incomplete_details).reason);
      if (reason === "max_output_tokens") {
        maxOutputReached = true;
      } else {
        failed = true;
      }
    } else if (type === "error" || type === "response.failed") {
      const rawError = object(payload.error ?? object(payload.response).error);
      events.push({
        schema_version: 1,
        type: "response.error",
        error: providerError(null, string(rawError.code)),
      });
      failed = true;
    } else if (type) {
      unknown += 1;
    }
  }
  return finishResult(
    "openai", requested, events, text, usage, resolved, null, null, completed, failed, unknown,
    maxOutputReached ? "max_output_tokens" : "stream_interrupted",
  );
}

function normalizeAnthropic(records: SseRecord[], requested: string): NormalizedStream {
  const events: CanonicalStreamEvent[] = [];
  let text = "";
  let usage = emptyUsage();
  let resolved: string | null = null;
  let completed = false;
  let failed = false;
  let maxOutputReached = false;
  let unknown = 0;

  for (const record of records) {
    const payload = object(parseJsonRecord(record));
    const type = string(payload.type) ?? record.event;
    if (type === "message_start") {
      const message = object(payload.message);
      resolved = string(message.model);
      emitStarted(events, requested, resolved);
      const rawUsage = object(message.usage);
      const cacheCreation = object(rawUsage.cache_creation);
      usage = mergeUsage(usage, {
        input_tokens: number(rawUsage.input_tokens),
        output_tokens: number(rawUsage.output_tokens),
        cache_read_tokens: number(rawUsage.cache_read_input_tokens),
        cache_creation_tokens: number(rawUsage.cache_creation_input_tokens),
        cache_creation_5m_tokens: number(cacheCreation.ephemeral_5m_input_tokens),
        cache_creation_1h_tokens: number(cacheCreation.ephemeral_1h_input_tokens),
        cache_ttl_seconds: number(cacheCreation.ephemeral_1h_input_tokens) ? 3600 : 300,
      });
    } else if (type === "content_block_delta") {
      const delta = object(payload.delta);
      if (string(delta.type) === "text_delta") {
        text += emitText(events, string(delta.text));
      }
    } else if (type === "message_delta") {
      if (string(object(payload.delta).stop_reason) === "max_tokens") maxOutputReached = true;
      const rawUsage = object(payload.usage);
      usage = mergeUsage(usage, { output_tokens: number(rawUsage.output_tokens) });
    } else if (type === "message_stop") {
      completed = !maxOutputReached;
    } else if (type === "error") {
      const rawError = object(payload.error);
      events.push({
        schema_version: 1,
        type: "response.error",
        error: providerError(string(rawError.type) === "overloaded_error" ? 429 : null, string(rawError.type)),
      });
      failed = true;
    } else if (type !== "ping" && type !== "content_block_start" && type !== "content_block_stop") {
      unknown += 1;
    }
  }
  return finishResult(
    "anthropic", requested, events, text, usage, resolved, null, null, completed, failed, unknown,
    maxOutputReached ? "max_output_tokens" : "stream_interrupted",
  );
}

function normalizeChatCompletions(provider: "xai" | "openrouter", records: SseRecord[], requested: string): NormalizedStream {
  const events: CanonicalStreamEvent[] = [];
  let text = "";
  let usage = emptyUsage();
  let resolved: string | null = null;
  let upstream: string | null = null;
  let routingViolation: NormalizedStream["routing_violation"] = null;
  let completed = false;
  let failed = false;
  let maxOutputReached = false;
  let unknown = 0;

  for (const record of records) {
    if (record.data === "[DONE]") {
      completed = true;
      continue;
    }
    const payload = object(parseJsonRecord(record));
    if (Object.keys(payload).length === 0) {
      unknown += 1;
      continue;
    }
    if (payload.error) {
      const rawError = object(payload.error);
      const metadata = object(rawError.metadata);
      events.push({
        schema_version: 1,
        type: "response.error",
        error: providerError(number(rawError.code), string(metadata.error_type) ?? string(rawError.type)),
      });
      failed = true;
      continue;
    }
    resolved = string(payload.model) ?? resolved;
    emitStarted(events, requested, resolved);
    for (const choice of array(payload.choices)) {
      const choiceObject = object(choice);
      text += emitText(events, string(object(choiceObject.delta).content));
      const finishReason = string(choiceObject.finish_reason);
      if (finishReason === "length") {
        maxOutputReached = true;
      } else if (finishReason && finishReason !== "error") {
        completed = true;
      }
    }
    const rawUsage = object(payload.usage);
    if (Object.keys(rawUsage).length > 0) {
      const promptDetails = object(rawUsage.prompt_tokens_details);
      const completionDetails = object(rawUsage.completion_tokens_details);
      const costTicks = number(rawUsage.cost_in_usd_ticks);
      usage = mergeUsage(usage, {
        input_tokens: number(rawUsage.prompt_tokens),
        output_tokens: number(rawUsage.completion_tokens),
        cache_read_tokens: number(promptDetails.cached_tokens),
        cache_creation_tokens: number(promptDetails.cache_write_tokens),
        reasoning_tokens: number(completionDetails.reasoning_tokens),
        provider_reported_cost_usd: number(rawUsage.cost) ?? (costTicks === null ? null : costTicks / 10_000_000_000),
      });
    }
    const routerMetadata = object(payload.openrouter_metadata);
    if (provider === "openrouter") {
      const attempt = number(routerMetadata.attempt);
      if (string(routerMetadata.strategy) === "fallback" || (attempt !== null && attempt > 1)) {
        routingViolation = "unexpected_fallback";
      }
    }
    for (const endpoint of array(object(routerMetadata.endpoints).available)) {
      const endpointObject = object(endpoint);
      if (endpointObject.selected === true) {
        upstream = string(endpointObject.provider) ?? upstream;
      }
    }
  }
  return finishResult(
    provider,
    requested,
    events,
    text,
    usage,
    resolved,
    upstream,
    routingViolation,
    completed,
    failed,
    unknown,
    maxOutputReached ? "max_output_tokens" : "stream_interrupted",
  );
}

export function normalizeProviderStream(provider: Provider, rawSse: string, modelRequested: string): NormalizedStream {
  const records = parseSseText(rawSse);
  if (provider === "gemini") {
    return normalizeGemini(records, modelRequested);
  }
  if (provider === "openai") {
    return normalizeOpenAi(records, modelRequested);
  }
  if (provider === "anthropic") {
    return normalizeAnthropic(records, modelRequested);
  }
  return normalizeChatCompletions(provider, records, modelRequested);
}

export function buildOpenRouterProviderPolicy(allowedUpstreams: string[] = []): JsonObject {
  return {
    allow_fallbacks: false,
    ...(allowedUpstreams.length > 0 ? { only: [...allowedUpstreams] } : {}),
  };
}

export function normalizeModelList(provider: Provider, payload: unknown): ProviderModel[] {
  const root = object(payload);
  const source = provider === "gemini" || provider === "xai" ? array(root.models) : array(root.data);
  const models: ProviderModel[] = [];
  for (const item of source) {
    const raw = object(item);
    const rawId = string(raw.id) ?? string(raw.name);
    if (!rawId) {
      continue;
    }
    const modelId = provider === "gemini" && rawId.startsWith("models/") ? rawId.slice(7) : rawId;
    let textChat: boolean | null = null;
    if (provider === "gemini") {
      textChat = array(raw.supportedGenerationMethods).includes("generateContent");
    } else if (provider === "openrouter") {
      textChat = array(object(raw.architecture).output_modalities).includes("text");
    }
    models.push({
      provider,
      model_id: modelId,
      display_name: string(raw.displayName) ?? string(raw.display_name) ?? string(raw.name) ?? modelId,
      text_chat: textChat,
    });
  }
  return models;
}
