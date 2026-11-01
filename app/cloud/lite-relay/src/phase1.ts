import { canonicalJson, hmacSha256, randomToken, sha256 } from "./auth";
import { getProviderProfile, requireEnabledProfile } from "./phase2-routing";
import { getUsageSummary } from "./budget";
import { buildMultiPersonaBundle, allAcknowledged } from "./phase4-return";

const SNAPSHOT_BASE_KEYS = [
  "schema_version",
  "travel_session_id",
  "persona_id",
  "persona_display_name",
  "system_prompt",
  "core_memory",
  "episodic_summary",
  "recent_messages",
  "retention_days",
  "created_at",
] as const;
const SNAPSHOT_V1_KEYS = new Set([...SNAPSHOT_BASE_KEYS, "model_id"]);
const SNAPSHOT_V2_KEYS = new Set([...SNAPSHOT_BASE_KEYS, "initial_route"]);
const SNAPSHOT_V3_KEYS = new Set([...SNAPSHOT_BASE_KEYS, "initial_route", "budget", "cache_policy"]);
const SNAPSHOT_V4_KEYS = new Set([
  "schema_version", "travel_session_id", "retention_days", "session_budget", "personas", "created_at",
]);
const SNAPSHOT_V4_PERSONA_KEYS = new Set([
  "persona_id", "persona_display_name", "presence_mode", "system_prompt", "core_memory",
  "episodic_summary", "recent_messages", "home_anchor", "initial_route", "budget", "cache_policy",
]);
const MAX_TRAVEL_PERSONAS = 3;
const SENSITIVE_KEY = /(api[-_]?key|secret|password|oauth|access[-_]?token|refresh[-_]?token|mcp)/i;
const ABSOLUTE_PATH = /(^|[\s"'=(])(?:[A-Za-z]:[\\/]|\/(?:home|Users|mnt|root|etc)\/)/;
const SENSITIVE_VALUE = /(?:AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{16,}|Bearer\s+[0-9A-Za-z._~-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)/i;

export interface TravelSnapshotV1 {
  schema_version: 1;
  travel_session_id: string;
  persona_id: string;
  persona_display_name: string;
  system_prompt: string;
  core_memory: string;
  episodic_summary: string;
  recent_messages: Array<{ role: "user" | "assistant"; content: string }>;
  model_id: string;
  retention_days: 0 | 7 | 30;
  created_at: string;
}

export interface TravelSnapshotV2 {
  schema_version: 2;
  travel_session_id: string;
  persona_id: string;
  persona_display_name: string;
  system_prompt: string;
  core_memory: string;
  episodic_summary: string;
  recent_messages: Array<{ role: "user" | "assistant"; content: string }>;
  initial_route: {
    credential_profile_id: string;
    model_id: string;
  };
  retention_days: 0 | 7 | 30;
  created_at: string;
}

export interface TravelSnapshotV3 extends Omit<TravelSnapshotV2, "schema_version"> {
  schema_version: 3;
  budget: {
    daily_limit_usd: number | null;
    session_limit_usd: number | null;
    warning_ratio: number;
    allow_unknown_price: boolean;
    max_output_tokens: number | null;
    timezone: string;
  };
  cache_policy: "off" | "auto" | "gemini_explicit";
}

export interface TravelSnapshotV4Persona {
  persona_id: string;
  persona_display_name: string;
  presence_mode: "exclusive" | "parallel";
  system_prompt: string;
  core_memory: string;
  episodic_summary: string;
  recent_messages: Array<{ role: "user" | "assistant"; content: string }>;
  home_anchor: { created_at: string; log_tail_hash: string };
  initial_route: { credential_profile_id: string; model_id: string };
  budget: { daily_limit_usd: number | null; session_limit_usd: number | null; max_output_tokens: number | null };
  cache_policy: "off" | "auto" | "gemini_explicit";
}

export interface TravelSnapshotV4 {
  schema_version: 4;
  travel_session_id: string;
  retention_days: 0 | 7 | 30;
  session_budget: TravelSnapshotV3["budget"];
  personas: TravelSnapshotV4Persona[];
  created_at: string;
}

export type TravelSnapshot = TravelSnapshotV1 | TravelSnapshotV2 | TravelSnapshotV3 | TravelSnapshotV4;

function messages(value: unknown): Array<{ role: "user" | "assistant"; content: string }> {
  const items = Array.isArray(value) ? value : [];
  if (items.length > 40) throw new Error("snapshot_too_large");
  return items.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("invalid_snapshot");
    const message = item as Record<string, unknown>;
    if (!( ["user", "assistant"] as unknown[]).includes(message.role)) throw new Error("invalid_snapshot");
    return { role: message.role as "user" | "assistant", content: text(message.content, 20_000, true) };
  });
}

function route(value: unknown): TravelSnapshotV2["initial_route"] {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_snapshot");
  const raw = value as Record<string, unknown>;
  if (
    Object.keys(raw).some((key) => !new Set(["credential_profile_id", "model_id"]).has(key)) ||
    typeof raw.credential_profile_id !== "string" ||
    !/^[a-z0-9][a-z0-9_-]{2,99}$/.test(raw.credential_profile_id)
  ) throw new Error("invalid_snapshot");
  return { credential_profile_id: raw.credential_profile_id, model_id: text(raw.model_id, 200, true) };
}

function maxOutputTokens(value: unknown): number | null {
  if (value === null) return null;
  const result = Number(value);
  if (!Number.isInteger(result) || result < 1 || result > 65_536) throw new Error("invalid_snapshot");
  return result;
}

function cachePolicy(value: unknown): TravelSnapshotV3["cache_policy"] {
  if (!["off", "auto", "gemini_explicit"].includes(String(value))) throw new Error("invalid_snapshot");
  return value as TravelSnapshotV3["cache_policy"];
}

function sessionBudget(value: unknown): TravelSnapshotV3["budget"] {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_snapshot");
  const raw = value as Record<string, unknown>;
  const keys = new Set([
    "daily_limit_usd", "session_limit_usd", "warning_ratio", "allow_unknown_price", "max_output_tokens", "timezone",
  ]);
  if (Object.keys(raw).some((key) => !keys.has(key))) throw new Error("snapshot_field_not_allowed");
  const warningRatio = Number(raw.warning_ratio);
  if (!Number.isFinite(warningRatio) || warningRatio <= 0 || warningRatio > 1 || typeof raw.allow_unknown_price !== "boolean") {
    throw new Error("invalid_snapshot");
  }
  return {
    daily_limit_usd: optionalLimit(raw.daily_limit_usd),
    session_limit_usd: optionalLimit(raw.session_limit_usd),
    warning_ratio: warningRatio,
    allow_unknown_price: raw.allow_unknown_price,
    max_output_tokens: maxOutputTokens(raw.max_output_tokens),
    timezone: validTimezone(raw.timezone),
  };
}

function validateSnapshotV4(raw: Record<string, unknown>): TravelSnapshotV4 {
  for (const key of Object.keys(raw)) {
    if (!SNAPSHOT_V4_KEYS.has(key) || SENSITIVE_KEY.test(key)) throw new Error("snapshot_field_not_allowed");
  }
  if (![0, 7, 30].includes(raw.retention_days as number)) throw new Error("invalid_snapshot");
  if (!Array.isArray(raw.personas) || raw.personas.length < 1 || raw.personas.length > MAX_TRAVEL_PERSONAS) {
    throw new Error("invalid_snapshot");
  }
  const seen = new Set<string>();
  const personas = raw.personas.map((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_snapshot");
    const item = value as Record<string, unknown>;
    for (const key of Object.keys(item)) {
      if (!SNAPSHOT_V4_PERSONA_KEYS.has(key) || SENSITIVE_KEY.test(key)) throw new Error("snapshot_field_not_allowed");
    }
    const personaId = text(item.persona_id, 200, true);
    if (seen.has(personaId)) throw new Error("duplicate_persona");
    seen.add(personaId);
    if (!["exclusive", "parallel"].includes(String(item.presence_mode))) throw new Error("invalid_snapshot");
    const anchor = item.home_anchor;
    if (!anchor || typeof anchor !== "object" || Array.isArray(anchor)) throw new Error("invalid_snapshot");
    const anchorRaw = anchor as Record<string, unknown>;
    if (Object.keys(anchorRaw).some((key) => !new Set(["created_at", "log_tail_hash"]).has(key))) {
      throw new Error("snapshot_field_not_allowed");
    }
    const budget = item.budget;
    if (!budget || typeof budget !== "object" || Array.isArray(budget)) throw new Error("invalid_snapshot");
    const budgetRaw = budget as Record<string, unknown>;
    if (Object.keys(budgetRaw).some((key) => !new Set(["daily_limit_usd", "session_limit_usd", "max_output_tokens"]).has(key))) {
      throw new Error("snapshot_field_not_allowed");
    }
    return {
      persona_id: personaId,
      persona_display_name: text(item.persona_display_name, 200, true),
      presence_mode: item.presence_mode as "exclusive" | "parallel",
      system_prompt: text(item.system_prompt, 100_000, true),
      core_memory: text(item.core_memory, 100_000),
      episodic_summary: text(item.episodic_summary, 50_000),
      recent_messages: messages(item.recent_messages),
      home_anchor: {
        created_at: text(anchorRaw.created_at, 40, true),
        log_tail_hash: text(anchorRaw.log_tail_hash, 100, true),
      },
      initial_route: route(item.initial_route),
      budget: {
        daily_limit_usd: optionalLimit(budgetRaw.daily_limit_usd),
        session_limit_usd: optionalLimit(budgetRaw.session_limit_usd),
        max_output_tokens: maxOutputTokens(budgetRaw.max_output_tokens),
      },
      cache_policy: cachePolicy(item.cache_policy),
    };
  });
  const snapshot: TravelSnapshotV4 = {
    schema_version: 4,
    travel_session_id: text(raw.travel_session_id, 100, true),
    retention_days: raw.retention_days as 0 | 7 | 30,
    session_budget: sessionBudget(raw.session_budget),
    personas,
    created_at: text(raw.created_at, 40, true),
  };
  if (canonicalJson(snapshot).length > 600_000) throw new Error("snapshot_too_large");
  return snapshot;
}

function optionalLimit(value: unknown): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1_000_000) {
    throw new Error("invalid_snapshot");
  }
  return value;
}

function validTimezone(value: unknown): string {
  const result = text(value, 100, true);
  try {
    new Intl.DateTimeFormat("en", { timeZone: result }).format(new Date());
  } catch {
    throw new Error("invalid_snapshot");
  }
  return result;
}

function text(value: unknown, max: number, required = false): string {
  if (typeof value !== "string" || value.length > max || (required && !value.trim())) {
    throw new Error("invalid_snapshot");
  }
  if (SENSITIVE_VALUE.test(value)) {
    throw new Error("snapshot_contains_secret");
  }
  if (ABSOLUTE_PATH.test(value)) {
    throw new Error("snapshot_contains_local_path");
  }
  return value;
}

export function validateSnapshot(value: unknown): TravelSnapshot {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid_snapshot");
  }
  const raw = value as Record<string, unknown>;
  if (raw.schema_version === 4) return validateSnapshotV4(raw);
  const snapshotKeys = raw.schema_version === 1
    ? SNAPSHOT_V1_KEYS
    : raw.schema_version === 2
      ? SNAPSHOT_V2_KEYS
      : raw.schema_version === 3
        ? SNAPSHOT_V3_KEYS
        : null;
  if (!snapshotKeys) throw new Error("invalid_snapshot");
  for (const key of Object.keys(raw)) {
    if (!snapshotKeys.has(key) || SENSITIVE_KEY.test(key)) {
      throw new Error("snapshot_field_not_allowed");
    }
  }
  if (![0, 7, 30].includes(raw.retention_days as number)) {
    throw new Error("invalid_snapshot");
  }
  const recentMessages = messages(raw.recent_messages);
  const base = {
    travel_session_id: text(raw.travel_session_id, 100, true),
    persona_id: text(raw.persona_id, 200, true),
    persona_display_name: text(raw.persona_display_name, 200, true),
    system_prompt: text(raw.system_prompt, 100_000, true),
    core_memory: text(raw.core_memory, 100_000),
    episodic_summary: text(raw.episodic_summary, 50_000),
    recent_messages: recentMessages,
    retention_days: raw.retention_days as 0 | 7 | 30,
    created_at: text(raw.created_at, 40, true),
  };
  let snapshot: TravelSnapshot;
  if (raw.schema_version === 1) {
    snapshot = { schema_version: 1, ...base, model_id: text(raw.model_id, 200, true) };
  } else {
    const initialRoute = route(raw.initial_route);
    const routeSnapshot = {
      schema_version: raw.schema_version as 2 | 3,
      ...base,
      initial_route: {
        credential_profile_id: initialRoute.credential_profile_id,
        model_id: initialRoute.model_id,
      },
    };
    if (raw.schema_version === 2) {
      snapshot = routeSnapshot as TravelSnapshotV2;
    } else {
      if (!raw.budget || typeof raw.budget !== "object" || Array.isArray(raw.budget)) {
        throw new Error("invalid_snapshot");
      }
      const budget = raw.budget as Record<string, unknown>;
      const budgetKeys = new Set([
        "daily_limit_usd", "session_limit_usd", "warning_ratio", "allow_unknown_price",
        "max_output_tokens", "timezone",
      ]);
      if (Object.keys(budget).some((key) => !budgetKeys.has(key))) throw new Error("snapshot_field_not_allowed");
      const warningRatio = Number(budget.warning_ratio);
      const outputTokens = maxOutputTokens(budget.max_output_tokens);
      if (
        !Number.isFinite(warningRatio) || warningRatio <= 0 || warningRatio > 1 ||
        typeof budget.allow_unknown_price !== "boolean" ||
        !["off", "auto", "gemini_explicit"].includes(String(raw.cache_policy))
      ) {
        throw new Error("invalid_snapshot");
      }
      snapshot = {
        ...(routeSnapshot as Omit<TravelSnapshotV3, "budget" | "cache_policy">),
        budget: {
          daily_limit_usd: optionalLimit(budget.daily_limit_usd),
          session_limit_usd: optionalLimit(budget.session_limit_usd),
          warning_ratio: warningRatio,
          allow_unknown_price: budget.allow_unknown_price,
          max_output_tokens: outputTokens,
          timezone: validTimezone(budget.timezone),
        },
        cache_policy: raw.cache_policy as TravelSnapshotV3["cache_policy"],
      };
    }
  }
  if (canonicalJson(snapshot).length > 300_000) {
    throw new Error("snapshot_too_large");
  }
  return snapshot;
}

type PairingTtlMinutes = 5 | 15 | 30;

const PAIRING_DEFAULT_TTL_MINUTES: PairingTtlMinutes = 5;
const PAIRING_TTL_MINUTES = new Set<PairingTtlMinutes>([5, 15, 30]);

function validatePairingTtlMinutes(value: unknown): PairingTtlMinutes {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    !PAIRING_TTL_MINUTES.has(value as PairingTtlMinutes)
  ) {
    throw new Error("invalid_pairing_ttl");
  }
  return value as PairingTtlMinutes;
}

export async function createPairingCode(
  db: D1Database,
  now: Date,
  ttlMinutes: unknown = PAIRING_DEFAULT_TTL_MINUTES,
): Promise<{ code: string; expires_at: string; ttl_minutes: PairingTtlMinutes }> {
  const validatedTtlMinutes = validatePairingTtlMinutes(ttlMinutes);
  const code = randomToken(18);
  const codeHash = await sha256(code);
  const expiresAt = new Date(now.getTime() + validatedTtlMinutes * 60_000).toISOString();
  await db
    .prepare(
      `INSERT INTO pairing_codes (pairing_code_id, code_hash, expires_at, created_at)
       VALUES (?, ?, ?, ?)`,
    )
    .bind(crypto.randomUUID(), codeHash, expiresAt, now.toISOString())
    .run();
  return { code, expires_at: expiresAt, ttl_minutes: validatedTtlMinutes };
}

export interface DeviceTokens {
  device_id: string;
  access_token: string;
  access_expires_at: string;
  refresh_token: string;
  refresh_expires_at: string;
}

function deviceTokens(now: Date): DeviceTokens {
  return {
    device_id: crypto.randomUUID(),
    access_token: randomToken(),
    access_expires_at: new Date(now.getTime() + 60 * 60_000).toISOString(),
    refresh_token: randomToken(48),
    refresh_expires_at: new Date(now.getTime() + 30 * 24 * 60 * 60_000).toISOString(),
  };
}

export async function consumePairingCode(
  db: D1Database,
  code: string,
  displayName: string,
  now: Date,
  existingDeviceId?: string,
): Promise<DeviceTokens> {
  const validatedDisplayName = text(displayName, 100, true);
  const tokens = deviceTokens(now);
  if (existingDeviceId) tokens.device_id = existingDeviceId;
  const codeHash = await sha256(code);
  const accessHash = await sha256(tokens.access_token);
  const refreshHash = await sha256(tokens.refresh_token);
  const pairing = await db
    .prepare(
      `SELECT pairing_code_id FROM pairing_codes
       WHERE code_hash = ? AND expires_at > ? AND consumed_at IS NULL`,
    )
    .bind(codeHash, now.toISOString())
    .first<{ pairing_code_id: string }>();
  if (!pairing) {
    throw new Error("pairing_code_invalid");
  }
  const consumed = await db
    .prepare(
      `UPDATE pairing_codes SET consumed_at = ?, consumed_by_device_id = ?
       WHERE pairing_code_id = ? AND consumed_at IS NULL`,
    )
    .bind(now.toISOString(), tokens.device_id, pairing.pairing_code_id)
    .run();
  if (Number(consumed.meta.changes ?? 0) !== 1) {
    throw new Error("pairing_code_invalid");
  }
  if (existingDeviceId) {
    const updated = await db
      .prepare(
        `UPDATE travel_devices
         SET display_name = ?, access_token_hash = ?, access_expires_at = ?,
             refresh_token_hash = ?, refresh_expires_at = ?, last_used_at = ?
         WHERE device_id = ? AND revoked_at IS NULL`,
      )
      .bind(
        validatedDisplayName,
        accessHash,
        tokens.access_expires_at,
        refreshHash,
        tokens.refresh_expires_at,
        now.toISOString(),
        existingDeviceId,
      )
      .run();
    if (Number(updated.meta.changes ?? 0) !== 1) {
      throw new Error("device_repair_failed");
    }
  } else {
    await db
      .prepare(
        `INSERT INTO travel_devices
          (device_id, display_name, access_token_hash, access_expires_at, refresh_token_hash,
           refresh_expires_at, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        tokens.device_id,
        validatedDisplayName,
        accessHash,
        tokens.access_expires_at,
        refreshHash,
        tokens.refresh_expires_at,
        now.toISOString(),
      )
      .run();
  }
  return tokens;
}

export async function refreshDevice(
  db: D1Database,
  refreshToken: string,
  now: Date,
): Promise<Omit<DeviceTokens, "device_id"> & { device_id: string }> {
  const refreshHash = await sha256(refreshToken);
  const device = await db
    .prepare(
      `SELECT device_id FROM travel_devices
       WHERE refresh_token_hash = ? AND refresh_expires_at > ? AND revoked_at IS NULL`,
    )
    .bind(refreshHash, now.toISOString())
    .first<{ device_id: string }>();
  if (!device) {
    throw new Error("refresh_token_invalid");
  }
  const next = deviceTokens(now);
  next.device_id = device.device_id;
  const result = await db
    .prepare(
      `UPDATE travel_devices
       SET access_token_hash = ?, access_expires_at = ?, refresh_token_hash = ?,
           refresh_expires_at = ?, last_refreshed_at = ?
       WHERE device_id = ? AND refresh_token_hash = ? AND revoked_at IS NULL`,
    )
    .bind(
      await sha256(next.access_token),
      next.access_expires_at,
      await sha256(next.refresh_token),
      next.refresh_expires_at,
      now.toISOString(),
      device.device_id,
      refreshHash,
    )
    .run();
  if (Number(result.meta.changes ?? 0) !== 1) {
    throw new Error("refresh_token_invalid");
  }
  return next;
}

export async function revokeDevice(db: D1Database, deviceId: string, now: string): Promise<boolean> {
  const result = await db
    .prepare("UPDATE travel_devices SET revoked_at = COALESCE(revoked_at, ?) WHERE device_id = ?")
    .bind(now, deviceId)
    .run();
  return Number(result.meta.changes ?? 0) === 1;
}

export async function getCurrentSession(db: D1Database): Promise<Record<string, unknown> | null> {
  const current = await db
    .prepare(
      `SELECT travel_session_id, status, retention_days, created_at, snapshot_schema_version
       FROM travel_sessions WHERE status IN ('armed', 'active', 'returning')
       ORDER BY created_at DESC LIMIT 1`,
    )
    .first<Record<string, unknown>>();
  if (!current) return null;
  if (Number(current.snapshot_schema_version) === 4) {
    const personas = await db.prepare(
      `SELECT tp.persona_id, tp.display_name AS persona_display_name, tp.presence_mode, tp.status,
              tp.branch_divergence_possible, r.credential_profile_id, r.provider, r.model_id,
              r.route_epoch, r.changed_at AS route_changed_at, ps.snapshot_json
       FROM travel_personas tp
       JOIN persona_routes r USING (travel_session_id, persona_id)
       JOIN persona_snapshots ps USING (travel_session_id, persona_id)
       WHERE tp.travel_session_id = ? ORDER BY tp.persona_id ASC`,
    ).bind(current.travel_session_id).all();
    return {
      ...current,
      personas: personas.results.map((row) => {
        const snapshot = JSON.parse(String(row.snapshot_json)) as TravelSnapshotV4Persona;
        const { snapshot_json: _snapshotJson, ...safeRow } = row;
        return {
          ...safeRow,
          inherited_recent_messages: snapshot.recent_messages,
          snapshot_created_at: snapshot.home_anchor.created_at,
        };
      }),
    };
  }
  const session = await db
    .prepare(
      `SELECT s.travel_session_id, s.status, s.retention_days, s.created_at, s.persona_id,
              s.snapshot_schema_version, r.credential_profile_id, r.provider, r.model_id,
              r.route_epoch, r.changed_at AS route_changed_at,
              p.snapshot_json
       FROM travel_sessions s
       JOIN persona_snapshots p USING (travel_session_id)
       LEFT JOIN persona_routes r
         ON r.travel_session_id = s.travel_session_id AND r.persona_id = s.persona_id
       WHERE s.status IN ('armed', 'active', 'returning')
       ORDER BY s.created_at DESC LIMIT 1`,
    )
    .first<Record<string, unknown>>();
  if (!session) {
    return null;
  }
  const snapshot = JSON.parse(String(session.snapshot_json)) as TravelSnapshotV1 | TravelSnapshotV2 | TravelSnapshotV3;
  delete session.snapshot_json;
  return {
    ...session,
    persona_display_name: snapshot.persona_display_name,
    inherited_recent_messages: snapshot.recent_messages,
    snapshot_created_at: snapshot.created_at,
  };
}

export async function registerSnapshot(db: D1Database, raw: unknown): Promise<TravelSnapshot> {
  const snapshot = validateSnapshot(raw);
  if (snapshot.schema_version === 4) return registerSnapshotV4(db, snapshot);
  const serialized = canonicalJson(snapshot);
  const snapshotHash = await sha256(serialized);
  const credentialProfileId =
    snapshot.schema_version === 1 ? "gemini-personal-1" : snapshot.initial_route.credential_profile_id;
  const modelId = snapshot.schema_version === 1 ? snapshot.model_id : snapshot.initial_route.model_id;
  const profile = await requireEnabledProfile(db, credentialProfileId);
  const budget = snapshot.schema_version === 3 ? snapshot.budget : {
    daily_limit_usd: null,
    session_limit_usd: null,
    warning_ratio: 0.8,
    allow_unknown_price: true,
    max_output_tokens: null,
    timezone: "UTC",
  };
  const cachePolicy = snapshot.schema_version === 3 ? snapshot.cache_policy : "auto";
  await db.batch([
    db
      .prepare(
        `INSERT INTO travel_sessions
          (travel_session_id, status, retention_days, created_at, persona_id, model_id, snapshot_hash,
           snapshot_schema_version, credential_profile_id, route_epoch, budget_daily_limit_usd,
           budget_session_limit_usd, budget_warning_ratio, budget_allow_unknown_price,
           budget_max_output_tokens, budget_timezone, cache_policy)
         VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        snapshot.travel_session_id,
        snapshot.retention_days,
        snapshot.created_at,
        snapshot.persona_id,
        modelId,
        snapshotHash,
        snapshot.schema_version,
        credentialProfileId,
        budget.daily_limit_usd,
        budget.session_limit_usd,
        budget.warning_ratio,
        budget.allow_unknown_price ? 1 : 0,
        budget.max_output_tokens ?? 0,
        budget.timezone,
        cachePolicy,
      ),
    db
      .prepare(
        `INSERT INTO persona_snapshots
          (travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        snapshot.travel_session_id,
        snapshot.persona_id,
        snapshot.schema_version,
        serialized,
        snapshotHash,
        snapshot.created_at,
      ),
    db
      .prepare(
        `INSERT INTO persona_routes
          (travel_session_id, persona_id, credential_profile_id, provider, model_id, route_epoch, changed_at)
         VALUES (?, ?, ?, ?, ?, 0, ?)`,
      )
      .bind(
        snapshot.travel_session_id,
        snapshot.persona_id,
        credentialProfileId,
        profile.provider,
        modelId,
        snapshot.created_at,
      ),
  ]);
  return snapshot;
}

async function registerSnapshotV4(db: D1Database, snapshot: TravelSnapshotV4): Promise<TravelSnapshotV4> {
  const serialized = canonicalJson(snapshot);
  const sessionHash = await sha256(serialized);
  const rows = await Promise.all(snapshot.personas.map(async (persona) => {
    const profile = await requireEnabledProfile(db, persona.initial_route.credential_profile_id);
    const personaJson = canonicalJson(persona);
    return { persona, profile, personaJson, personaHash: await sha256(personaJson) };
  }));
  const primary = rows[0];
  if (!primary) throw new Error("invalid_snapshot");
  await db.batch([
    db.prepare(
      `INSERT INTO travel_sessions
        (travel_session_id, status, retention_days, created_at, persona_id, model_id, snapshot_hash,
         snapshot_schema_version, credential_profile_id, route_epoch, budget_daily_limit_usd,
         budget_session_limit_usd, budget_warning_ratio, budget_allow_unknown_price,
         budget_max_output_tokens, budget_timezone, cache_policy)
       VALUES (?, 'active', ?, ?, ?, ?, ?, 4, ?, 0, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      snapshot.travel_session_id, snapshot.retention_days, snapshot.created_at,
      primary.persona.persona_id, primary.persona.initial_route.model_id, sessionHash,
      primary.persona.initial_route.credential_profile_id, snapshot.session_budget.daily_limit_usd,
      snapshot.session_budget.session_limit_usd, snapshot.session_budget.warning_ratio,
      snapshot.session_budget.allow_unknown_price ? 1 : 0, snapshot.session_budget.max_output_tokens ?? 0,
      snapshot.session_budget.timezone, primary.persona.cache_policy,
    ),
    ...rows.flatMap(({ persona, profile, personaJson, personaHash }) => [
      db.prepare(
        `INSERT INTO travel_personas
          (travel_session_id, persona_id, display_name, presence_mode, status, snapshot_schema_version,
           snapshot_hash, home_anchor_hash, budget_daily_limit_usd, budget_session_limit_usd,
           budget_max_output_tokens, cache_policy, created_at)
         VALUES (?, ?, ?, ?, 'active', 4, ?, ?, ?, ?, ?, ?, ?)`,
      ).bind(
        snapshot.travel_session_id, persona.persona_id, persona.persona_display_name, persona.presence_mode,
        personaHash, persona.home_anchor.log_tail_hash, persona.budget.daily_limit_usd,
        persona.budget.session_limit_usd, persona.budget.max_output_tokens ?? 0, persona.cache_policy, snapshot.created_at,
      ),
      db.prepare(
        `INSERT INTO persona_snapshots
          (travel_session_id, persona_id, schema_version, snapshot_json, snapshot_hash, created_at)
         VALUES (?, ?, 4, ?, ?, ?)`,
      ).bind(snapshot.travel_session_id, persona.persona_id, personaJson, personaHash, snapshot.created_at),
      db.prepare(
        `INSERT INTO persona_routes
          (travel_session_id, persona_id, credential_profile_id, provider, model_id, route_epoch, changed_at)
         VALUES (?, ?, ?, ?, ?, 0, ?)`,
      ).bind(
        snapshot.travel_session_id, persona.persona_id, persona.initial_route.credential_profile_id,
        profile.provider, persona.initial_route.model_id, snapshot.created_at,
      ),
    ]),
  ]);
  return snapshot;
}

export async function buildSignedBundle(
  db: D1Database, sessionId: string, secret: string, exportedAt: string,
): Promise<{ algorithm: string; payload: any; payload_canonical: string; payload_hash: string; signature: string }> {
  const version = await db.prepare("SELECT snapshot_schema_version FROM travel_sessions WHERE travel_session_id = ?")
    .bind(sessionId).first<{ snapshot_schema_version: number }>();
  if (version?.snapshot_schema_version === 4) return buildMultiPersonaBundle(db, sessionId, secret, exportedAt);
  const sessionRow = await db
    .prepare(
      `SELECT s.travel_session_id, s.status, s.retention_days, s.persona_id, s.model_id,
              s.snapshot_hash, s.created_at, s.snapshot_schema_version, p.snapshot_json,
              r.credential_profile_id AS final_credential_profile_id,
              r.provider AS final_provider, r.model_id AS final_model_id,
              r.route_epoch AS final_route_epoch
       FROM travel_sessions s
       JOIN persona_snapshots p USING (travel_session_id)
       JOIN persona_routes r
         ON r.travel_session_id = s.travel_session_id AND r.persona_id = s.persona_id
       WHERE s.travel_session_id = ?`,
    )
    .bind(sessionId)
    .first<Record<string, unknown>>();
  if (!sessionRow) {
    throw new Error("travel_session_not_found");
  }
  const snapshot = JSON.parse(String(sessionRow.snapshot_json)) as TravelSnapshotV1 | TravelSnapshotV2 | TravelSnapshotV3;
  const initialCredentialProfileId = snapshot.schema_version === 1
    ? "gemini-personal-1"
    : snapshot.initial_route.credential_profile_id;
  const initialModelId = snapshot.schema_version === 1 ? snapshot.model_id : snapshot.initial_route.model_id;
  const initialProfile = await getProviderProfile(db, initialCredentialProfileId);
  if (!initialProfile) throw new Error("credential_profile_unavailable");
  const session = {
    travel_session_id: sessionRow.travel_session_id,
    status: sessionRow.status,
    retention_days: sessionRow.retention_days,
    persona_id: sessionRow.persona_id,
    model_id: sessionRow.model_id,
    snapshot_hash: sessionRow.snapshot_hash,
    created_at: sessionRow.created_at,
    snapshot_schema_version: sessionRow.snapshot_schema_version,
    initial_route: {
      credential_profile_id: initialCredentialProfileId,
      provider: initialProfile.provider,
      model_id: initialModelId,
      route_epoch: 0,
    },
    final_route: {
      credential_profile_id: sessionRow.final_credential_profile_id,
      provider: sessionRow.final_provider,
      model_id: sessionRow.final_model_id,
      route_epoch: sessionRow.final_route_epoch,
    },
  };
  const events = (
    await db
      .prepare(
        `SELECT event_id, persona_id, sequence_no, type, created_at, content, content_hash,
                provider, model_requested, model_resolved, route_epoch, reply_to_event_id, status
         FROM travel_events WHERE travel_session_id = ? ORDER BY sequence_no ASC, event_id ASC`,
      )
      .bind(sessionId)
      .all()
  ).results;
  const receipts = (
    await db
      .prepare(
        `SELECT receipt_id, event_id, persona_id, occurred_at, provider, gateway,
                credential_profile_id, model_requested, model_resolved, upstream_provider,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                provider_reported_cost_usd, route_epoch, usage_status, pricing_version, cost_basis,
                estimate_status, input_cost_usd, output_cost_usd, cache_read_cost_usd,
                cache_creation_cost_usd, cache_storage_cost_usd, estimated_cost_usd,
                estimated_savings_usd, unknown_reason, cache_status, cache_ttl_seconds,
                cache_creation_5m_tokens, cache_creation_1h_tokens
         FROM usage_receipts WHERE travel_session_id = ? ORDER BY occurred_at ASC, receipt_id ASC`,
      )
      .bind(sessionId)
      .all()
  ).results;
  const bundleSchema = snapshot.schema_version === 3 ? 3 : 2;
  const payload = {
    schema_version: bundleSchema,
    exported_at: exportedAt,
    session: bundleSchema === 3
      ? { ...session, budget: snapshot.schema_version === 3 ? snapshot.budget : null, usage_summary: await getUsageSummary(db, sessionId, exportedAt) }
      : session,
    events,
    receipts,
  };
  const canonical = canonicalJson(payload);
  const payloadHash = await sha256(canonical);
  const signature = await hmacSha256(canonical, secret);
  await db
    .prepare(
      `INSERT INTO imported_bundle_exports (export_id, travel_session_id, exported_at, payload_hash)
       VALUES (?, ?, ?, ?) ON CONFLICT(travel_session_id, payload_hash) DO NOTHING`,
    )
    .bind(crypto.randomUUID(), sessionId, exportedAt, payloadHash)
    .run();
  return {
    algorithm: "HMAC-SHA-256",
    payload,
    payload_canonical: canonical,
    payload_hash: payloadHash,
    signature,
  };
}

export async function closeSession(db: D1Database, sessionId: string, now: Date): Promise<void> {
  const session = await db
    .prepare("SELECT retention_days FROM travel_sessions WHERE travel_session_id = ?")
    .bind(sessionId)
    .first<{ retention_days: number }>();
  if (!session) {
    throw new Error("travel_session_not_found");
  }
  const version = await db.prepare("SELECT snapshot_schema_version FROM travel_sessions WHERE travel_session_id = ?")
    .bind(sessionId).first<{ snapshot_schema_version: number }>();
  if (version?.snapshot_schema_version === 4 && !(await allAcknowledged(db, sessionId))) {
    throw new Error("return_ack_incomplete");
  }
  const deleteAfter = new Date(now.getTime() + session.retention_days * 24 * 60 * 60_000).toISOString();
  await db
    .prepare(
      `UPDATE travel_sessions SET status = 'closed', acknowledged_at = ?, content_delete_after = ?
       WHERE travel_session_id = ? AND status IN ('active', 'returning', 'closed')`,
    )
    .bind(now.toISOString(), deleteAfter, sessionId)
    .run();
}

export async function deleteExpiredContent(db: D1Database, now: string): Promise<number> {
  const sessions = await db
    .prepare(
      `SELECT travel_session_id FROM travel_sessions
       WHERE status = 'closed' AND content_deleted_at IS NULL AND content_delete_after <= ?`,
    )
    .bind(now)
    .all<{ travel_session_id: string }>();
  for (const session of sessions.results) {
    await db.batch([
      db
        .prepare(
          `UPDATE travel_events SET content = NULL, content_deleted_at = ?
           WHERE travel_session_id = ? AND content IS NOT NULL`,
        )
        .bind(now, session.travel_session_id),
      db
        .prepare("DELETE FROM persona_snapshots WHERE travel_session_id = ?")
        .bind(session.travel_session_id),
      db
        .prepare("UPDATE travel_sessions SET content_deleted_at = ? WHERE travel_session_id = ?")
        .bind(now, session.travel_session_id),
    ]);
  }
  return sessions.results.length;
}
