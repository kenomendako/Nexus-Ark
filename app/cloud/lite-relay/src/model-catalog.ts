import { normalizeModelList } from "./adapters";
import { requireEnabledProfile, type ProviderProfileRow } from "./phase2-routing";
import { lookupPrice, PRICING_VERSION } from "./pricing-catalog";
import { fetchProviderWithoutRedirect, resolveSecret } from "./security";
import type { Provider, ProviderModel } from "./types";

const FRESH_SECONDS = 5 * 60;
const STALE_SECONDS = 60 * 60;
const CACHE_SCHEMA_VERSION = 2;

export interface SafeModelOption extends ProviderModel {
  streaming: boolean;
  available: boolean;
  unavailable_reason: "not_text_chat_capable" | "capability_unverified" | null;
  pricing_known: boolean;
  pricing_version: string | null;
}

interface CachedModelCatalog {
  schema_version: 2;
  credential_profile_id: string;
  provider: Provider;
  fetched_at: string;
  models: SafeModelOption[];
}

export interface ModelCatalogResult extends CachedModelCatalog {
  source: "live" | "cache" | "stale";
}

function listRequest(profile: ProviderProfileRow, secret: string): { url: string; init: RequestInit } {
  const headers = new Headers({ Accept: "application/json" });
  let url: string;
  if (profile.provider === "gemini") {
    url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000";
    headers.set("x-goog-api-key", secret);
  } else if (profile.provider === "openai") {
    url = "https://api.openai.com/v1/models";
    headers.set("Authorization", `Bearer ${secret}`);
  } else if (profile.provider === "anthropic") {
    url = "https://api.anthropic.com/v1/models?limit=1000";
    headers.set("x-api-key", secret);
    headers.set("anthropic-version", "2023-06-01");
  } else if (profile.provider === "xai") {
    url = "https://api.x.ai/v1/language-models";
    headers.set("Authorization", `Bearer ${secret}`);
  } else {
    url = "https://openrouter.ai/api/v1/models";
    headers.set("Authorization", `Bearer ${secret}`);
  }
  return { url, init: { method: "GET", headers } };
}

function openAiTextChat(modelId: string): boolean | null {
  if (/^(gpt-|chatgpt-|o\d)/i.test(modelId)) return true;
  if (/(embedding|moderation|image|audio|realtime|transcrib|tts|whisper|dall-e)/i.test(modelId)) return false;
  return null;
}

function capability(provider: Provider, model: ProviderModel): SafeModelOption {
  let textChat = model.text_chat;
  if (provider === "openai") textChat = openAiTextChat(model.model_id);
  if (provider === "anthropic" || provider === "xai") textChat = true;
  const unavailableReason = textChat === false ? "not_text_chat_capable" : textChat === null ? "capability_unverified" : null;
  const price = lookupPrice(provider, model.model_id);
  return {
    ...model,
    text_chat: textChat,
    streaming: textChat === true,
    available: textChat === true,
    unavailable_reason: unavailableReason,
    pricing_known: Boolean(price),
    pricing_version: price ? PRICING_VERSION : null,
  };
}

function safeModels(provider: Provider, payload: unknown): SafeModelOption[] {
  const unique = new Map<string, SafeModelOption>();
  for (const model of normalizeModelList(provider, payload)) {
    if (!model.model_id || model.model_id.length > 200 || unique.has(model.model_id)) continue;
    unique.set(model.model_id, capability(provider, model));
  }
  return [...unique.values()].sort((left, right) =>
    left.display_name.localeCompare(right.display_name, "ja") || left.model_id.localeCompare(right.model_id),
  );
}

function cacheKey(profileId: string): string {
  return `models:v${CACHE_SCHEMA_VERSION}:${profileId}`;
}

function ageSeconds(catalog: CachedModelCatalog, now: Date): number {
  const fetched = Date.parse(catalog.fetched_at);
  return Number.isFinite(fetched) ? Math.max(0, (now.getTime() - fetched) / 1000) : Number.POSITIVE_INFINITY;
}

function validCache(value: unknown, profile: ProviderProfileRow): CachedModelCatalog | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Partial<CachedModelCatalog>;
  if (
    item.schema_version !== CACHE_SCHEMA_VERSION ||
    item.credential_profile_id !== profile.credential_profile_id ||
    item.provider !== profile.provider ||
    typeof item.fetched_at !== "string" ||
    !Array.isArray(item.models)
  ) return null;
  return item as CachedModelCatalog;
}

export async function listModelsForProfile(
  db: D1Database,
  cache: KVNamespace | undefined,
  env: Env,
  profileId: string,
  forceRefresh = false,
  fetcher: typeof fetch = fetch,
  now: Date = new Date(),
): Promise<ModelCatalogResult> {
  const profile = await requireEnabledProfile(db, profileId);
  let cached: CachedModelCatalog | null = null;
  if (cache) {
    try {
      cached = validCache(await cache.get(cacheKey(profileId), { type: "json" }), profile);
    } catch {
      cached = null;
    }
  }
  if (cached && !forceRefresh && ageSeconds(cached, now) <= FRESH_SECONDS) {
    return { ...cached, source: "cache" };
  }

  try {
    const request = listRequest(profile, resolveSecret(profile.secret_binding_id, env));
    const response = await fetchProviderWithoutRedirect(profile.provider, request.url, request.init, fetcher);
    if (!response.ok) throw new Error("model_catalog_provider_failed");
    const catalog: CachedModelCatalog = {
      schema_version: CACHE_SCHEMA_VERSION,
      credential_profile_id: profile.credential_profile_id,
      provider: profile.provider,
      fetched_at: now.toISOString(),
      models: safeModels(profile.provider, await response.json()),
    };
    if (cache) {
      try {
        await cache.put(cacheKey(profileId), JSON.stringify(catalog), { expirationTtl: STALE_SECONDS });
      } catch {
        // KV障害で公式モデル一覧の安全なlive結果まで失敗扱いにしない。
      }
    }
    return { ...catalog, source: "live" };
  } catch {
    if (cached && ageSeconds(cached, now) <= STALE_SECONDS) return { ...cached, source: "stale" };
    throw new Error("model_catalog_unavailable");
  }
}
