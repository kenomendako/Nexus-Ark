import { canonicalJson, sha256 } from "./auth";
import type { ProviderProfileRow, PersonaRoute } from "./phase2-routing";
import { fetchProviderWithoutRedirect, resolveSecret } from "./security";

export const CACHE_STRATEGY_VERSION = "phase3-v1";
const GEMINI_EXPLICIT_CACHE_MIN_TOKENS = 2048;

export interface ProviderCacheContext {
  remoteCacheName: string;
  ttlSeconds: number;
  cacheCreationTokens: number | null;
  justCreated: boolean;
}

interface CacheRow {
  cache_entry_id: string;
  remote_cache_name: string | null;
  cached_tokens: number | null;
  ttl_seconds: number | null;
  expires_at: string | null;
  status: string;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function snapshotStableParts(snapshot: Record<string, unknown>): {
  systemInstruction: { parts: Array<{ text: string }> };
  contents: Array<{ role: "user" | "model"; parts: Array<{ text: string }> }>;
} {
  const systemText = [snapshot.system_prompt, snapshot.core_memory, snapshot.episodic_summary]
    .filter((item): item is string => typeof item === "string" && item.length > 0)
    .join("\n\n");
  const messages = Array.isArray(snapshot.recent_messages) ? snapshot.recent_messages : [];
  return {
    systemInstruction: { parts: [{ text: systemText }] },
    contents: messages.flatMap((item) => {
      const message = record(item);
      if (!(message.role === "user" || message.role === "assistant") || typeof message.content !== "string") return [];
      return [{ role: message.role === "assistant" ? "model" as const : "user" as const, parts: [{ text: message.content }] }];
    }),
  };
}

export async function ensureProviderCache(
  db: D1Database,
  env: Env,
  profile: ProviderProfileRow,
  route: PersonaRoute,
  snapshot: Record<string, unknown>,
  snapshotHash: string,
  fetcher: typeof fetch = fetch,
  now = new Date(),
): Promise<ProviderCacheContext | null> {
  if (route.provider !== "gemini" || snapshot.cache_policy !== "gemini_explicit") return null;
  // Argonはimplicit cacheのみ対応するため、明示cache APIへ送らず通常入力へfallbackする。
  // 料金カタログのcacheWrite有無で判定すると、既存のpreviewモデルや未知モデルまで
  // 明示cache経路を抑止してしまうため、ここは新Argonのexact IDだけを対象にする。
  if (route.model_id === "gemini-4-argon") return null;
  const logicalKey = await sha256(canonicalJson({
    snapshot_hash: snapshotHash,
    provider: route.provider,
    model_id: route.model_id,
    route_epoch: route.route_epoch,
    cache_strategy_version: CACHE_STRATEGY_VERSION,
  }));
  const existing = await db
    .prepare(
      `SELECT cache_entry_id, remote_cache_name, cached_tokens, ttl_seconds, expires_at, status
       FROM provider_cache_entries
       WHERE travel_session_id = ? AND persona_id = ? AND logical_key = ?`,
    )
    .bind(route.travel_session_id, route.persona_id, logicalKey)
    .first<CacheRow>();
  if (
    existing?.status === "ready" && existing.remote_cache_name && existing.expires_at &&
    Date.parse(existing.expires_at) > now.getTime()
  ) {
    await db.prepare("UPDATE provider_cache_entries SET last_used_at = ? WHERE cache_entry_id = ?")
      .bind(now.toISOString(), existing.cache_entry_id).run();
    return {
      remoteCacheName: existing.remote_cache_name,
      ttlSeconds: Number(existing.ttl_seconds || 3600),
      cacheCreationTokens: existing.cached_tokens,
      justCreated: false,
    };
  }

  const cacheEntryId = existing?.cache_entry_id ?? crypto.randomUUID();
  const ttlSeconds = 3600;
  await db
    .prepare(
      `INSERT INTO provider_cache_entries
        (cache_entry_id, travel_session_id, persona_id, credential_profile_id, provider, model_id,
         route_epoch, strategy_version, logical_key, ttl_seconds, created_at, status)
       VALUES (?, ?, ?, ?, 'gemini', ?, ?, ?, ?, ?, ?, 'creating')
       ON CONFLICT(travel_session_id, persona_id, logical_key) DO UPDATE SET
         status = 'creating', failure_code = NULL, created_at = excluded.created_at`,
    )
    .bind(
      cacheEntryId,
      route.travel_session_id,
      route.persona_id,
      profile.credential_profile_id,
      route.model_id,
      route.route_epoch,
      CACHE_STRATEGY_VERSION,
      logicalKey,
      ttlSeconds,
      now.toISOString(),
    )
    .run();
  try {
    const stableParts = snapshotStableParts(snapshot);
    if (stableParts.contents.length === 0) {
      throw new Error("cache_requires_stable_contents");
    }
    const countResponse = await fetchProviderWithoutRedirect(
      "gemini",
      `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(route.model_id)}:countTokens`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": resolveSecret(profile.secret_binding_id, env) },
        body: JSON.stringify({
          generateContentRequest: {
            model: `models/${route.model_id}`,
            ...stableParts,
          },
        }),
      },
      fetcher,
    );
    if (!countResponse.ok) throw new Error(`cache_count_tokens_http_${countResponse.status}`);
    const countBody = record(await countResponse.json());
    const stableTokens = typeof countBody.totalTokens === "number" ? countBody.totalTokens : null;
    if (stableTokens === null) throw new Error("cache_count_tokens_missing");
    if (stableTokens < GEMINI_EXPLICIT_CACHE_MIN_TOKENS) {
      throw new Error(`cache_below_minimum_${GEMINI_EXPLICIT_CACHE_MIN_TOKENS}`);
    }
    const response = await fetchProviderWithoutRedirect(
      "gemini",
      "https://generativelanguage.googleapis.com/v1beta/cachedContents",
      {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-goog-api-key": resolveSecret(profile.secret_binding_id, env) },
        body: JSON.stringify({
          model: `models/${route.model_id}`,
          ...stableParts,
          ttl: `${ttlSeconds}s`,
          displayName: `nexus-travel-${route.travel_session_id.slice(0, 12)}`,
        }),
      },
      fetcher,
    );
    if (!response.ok) throw new Error(`cache_create_http_${response.status}`);
    const body = record(await response.json());
    const name = typeof body.name === "string" ? body.name : "";
    const usage = record(body.usageMetadata);
    const cachedTokens = typeof usage.totalTokenCount === "number" ? usage.totalTokenCount : null;
    if (!name) throw new Error("cache_create_missing_name");
    const expiresAt = typeof body.expireTime === "string"
      ? body.expireTime
      : new Date(now.getTime() + ttlSeconds * 1000).toISOString();
    await db
      .prepare(
        `UPDATE provider_cache_entries
         SET remote_cache_name = ?, cached_tokens = ?, expires_at = ?, last_used_at = ?, status = 'ready'
         WHERE cache_entry_id = ?`,
      )
      .bind(name, cachedTokens, expiresAt, now.toISOString(), cacheEntryId)
      .run();
    return { remoteCacheName: name, ttlSeconds, cacheCreationTokens: cachedTokens, justCreated: true };
  } catch (error) {
    const code = error instanceof Error ? error.message.slice(0, 100) : "cache_create_failed";
    await db
      .prepare("UPDATE provider_cache_entries SET status = 'unavailable', failure_code = ? WHERE cache_entry_id = ?")
      .bind(code, cacheEntryId)
      .run();
    return null;
  }
}

export async function deleteProviderCaches(
  db: D1Database,
  env: Env,
  travelSessionId: string,
  fetcher: typeof fetch = fetch,
  now = new Date(),
): Promise<number> {
  const rows = await db
    .prepare(
      `SELECT c.cache_entry_id, c.remote_cache_name, p.secret_binding_id
       FROM provider_cache_entries c
       JOIN provider_profiles p USING (credential_profile_id)
       WHERE c.travel_session_id = ? AND c.provider = 'gemini' AND c.status = 'ready'
         AND c.remote_cache_name IS NOT NULL`,
    )
    .bind(travelSessionId)
    .all<{ cache_entry_id: string; remote_cache_name: string; secret_binding_id: string }>();
  let deleted = 0;
  for (const row of rows.results) {
    try {
      const response = await fetchProviderWithoutRedirect(
        "gemini",
        `https://generativelanguage.googleapis.com/v1beta/${row.remote_cache_name}`,
        { method: "DELETE", headers: { "x-goog-api-key": resolveSecret(row.secret_binding_id, env) } },
        fetcher,
      );
      if (!response.ok && response.status !== 404) continue;
      await db.prepare("UPDATE provider_cache_entries SET status = 'deleted', deleted_at = ? WHERE cache_entry_id = ?")
        .bind(now.toISOString(), row.cache_entry_id).run();
      deleted += 1;
    } catch {
      // 本文削除やセッションcloseをcache削除障害で妨げない。次回maintenanceで再試行する。
    }
  }
  return deleted;
}
