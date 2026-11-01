import { buildOpenRouterProviderPolicy } from "./adapters";
import type { ProviderProfileRow, PersonaRoute } from "./phase2-routing";
import { resolveSecret } from "./security";
import type { ProviderCacheContext } from "./provider-cache";

export interface CanonicalMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ProviderRequest {
  provider: PersonaRoute["provider"];
  url: string;
  init: RequestInit;
}

const MAX_MANUAL_OUTPUT_TOKENS = 65_536;
const ANTHROPIC_AUTO_MAX_TOKENS = 16_384;

function snapshotMessages(snapshot: Record<string, unknown>): CanonicalMessage[] {
  if (!Array.isArray(snapshot.recent_messages)) return [];
  return snapshot.recent_messages.flatMap((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const message = item as Record<string, unknown>;
    if (!(message.role === "user" || message.role === "assistant") || typeof message.content !== "string") return [];
    return [{ role: message.role, content: message.content }];
  });
}

function systemText(snapshot: Record<string, unknown>): string {
  return [snapshot.system_prompt, snapshot.core_memory, snapshot.episodic_summary]
    .filter((item): item is string => typeof item === "string" && item.length > 0)
    .join("\n\n");
}

function headers(profile: ProviderProfileRow, secret: string, travelSessionId: string): Headers {
  const result = new Headers({ Accept: "text/event-stream", "Content-Type": "application/json" });
  if (["openai", "xai", "openrouter"].includes(profile.provider)) {
    result.set("Authorization", `Bearer ${secret}`);
  }
  if (profile.provider === "gemini") result.set("x-goog-api-key", secret);
  if (profile.provider === "anthropic") {
    result.set("x-api-key", secret);
    result.set("anthropic-version", "2023-06-01");
  }
  if (profile.provider === "xai") result.set("x-grok-conv-id", travelSessionId);
  if (profile.provider === "openrouter") result.set("X-OpenRouter-Metadata", "enabled");
  return result;
}

export function buildProviderRequest(
  env: Env,
  profile: ProviderProfileRow,
  route: PersonaRoute,
  snapshot: Record<string, unknown>,
  committedHistory: CanonicalMessage[],
  message: string,
  cacheContext: ProviderCacheContext | null = null,
): ProviderRequest {
  if (profile.provider !== route.provider || profile.credential_profile_id !== route.credential_profile_id) {
    throw new Error("provider_profile_route_mismatch");
  }
  const secret = resolveSecret(profile.secret_binding_id, env);
  const requestHeaders = headers(profile, secret, route.travel_session_id);
  const history = [
    ...(cacheContext ? [] : snapshotMessages(snapshot)),
    ...committedHistory,
    { role: "user" as const, content: message },
  ];
  const stableSystem = systemText(snapshot);
  const snapshotBudget = snapshot.budget && typeof snapshot.budget === "object" && !Array.isArray(snapshot.budget)
    ? snapshot.budget as Record<string, unknown>
    : {};
  const configuredMax = snapshotBudget.max_output_tokens;
  const numericMax = configuredMax === null || configuredMax === undefined ? null : Number(configuredMax);
  const maxOutputTokens = numericMax !== null && Number.isInteger(numericMax) && numericMax > 0
    ? Math.min(MAX_MANUAL_OUTPUT_TOKENS, numericMax)
    : null;
  let url: string;
  let body: Record<string, unknown>;

  if (route.provider === "gemini") {
    url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(
      route.model_id,
    )}:streamGenerateContent?alt=sse`;
    body = {
      ...(cacheContext
        ? { cachedContent: cacheContext.remoteCacheName }
        : { systemInstruction: { parts: [{ text: stableSystem }] } }),
      contents: history.map((item) => ({
        role: item.role === "assistant" ? "model" : "user",
        parts: [{ text: item.content }],
      })),
      generationConfig: {
        temperature: 0.8,
        ...(maxOutputTokens === null ? {} : { maxOutputTokens }),
      },
    };
  } else if (route.provider === "openai") {
    url = "https://api.openai.com/v1/responses";
    body = {
      model: route.model_id,
      instructions: stableSystem,
      input: history.map((item) => ({ role: item.role, content: item.content })),
      ...(maxOutputTokens === null ? {} : { max_output_tokens: maxOutputTokens }),
      stream: true,
      store: false,
    };
  } else if (route.provider === "anthropic") {
    url = "https://api.anthropic.com/v1/messages";
    body = {
      model: route.model_id,
      system: stableSystem,
      messages: history,
      max_tokens: maxOutputTokens ?? ANTHROPIC_AUTO_MAX_TOKENS,
      temperature: 0.8,
      stream: true,
      ...(snapshot.cache_policy === "off" ? {} : { cache_control: { type: "ephemeral", ttl: "5m" } }),
    };
  } else if (route.provider === "xai") {
    url = "https://api.x.ai/v1/chat/completions";
    body = {
      model: route.model_id,
      messages: [{ role: "system", content: stableSystem }, ...history],
      ...(maxOutputTokens === null ? {} : { max_tokens: maxOutputTokens }),
      temperature: 0.8,
      stream: true,
      stream_options: { include_usage: true },
    };
  } else {
    url = "https://openrouter.ai/api/v1/chat/completions";
    body = {
      model: route.model_id,
      messages: [{ role: "system", content: stableSystem }, ...history],
      ...(maxOutputTokens === null ? {} : { max_tokens: maxOutputTokens }),
      temperature: 0.8,
      stream: true,
      stream_options: { include_usage: true },
      session_id: route.travel_session_id,
      provider: buildOpenRouterProviderPolicy(),
    };
  }

  return {
    provider: route.provider,
    url,
    init: { method: "POST", headers: requestHeaders, body: JSON.stringify(body) },
  };
}
