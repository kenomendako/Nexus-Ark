import { canonicalJson, sha256 } from "./auth";
import {
  listSecretBindingRegistrationMetadata,
  secretBindingMetadata,
  type SecretBindingId,
} from "./security";
import { getActiveMessageRequest, nextSequenceNumber } from "./storage";
import type { Provider } from "./types";

const PROFILE_ID = /^[a-z0-9][a-z0-9_-]{2,99}$/;
const ROUTE_CHANGE_ID = /^[A-Za-z0-9_-]{8,100}$/;
const PROVIDERS = new Set<Provider>(["gemini", "openai", "anthropic", "xai", "openrouter"]);

export interface ProviderProfileRow {
  credential_profile_id: string;
  display_name: string;
  provider: Provider;
  secret_binding_id: SecretBindingId;
  allowed_base_url_id: string;
  enabled: number;
  created_at: string;
}

export interface SafeProviderProfile {
  credential_profile_id: string;
  display_name: string;
  provider: Provider;
  allowed_base_url_id: string;
  enabled: boolean;
}

export interface ProviderRegistrationOptionSlot {
  secret_binding_id: SecretBindingId;
  credential_profile_id: string;
  display_name: string;
  registered_profile_id: string | null;
}

export interface ProviderRegistrationOptions {
  providers: Array<{
    provider: Provider;
    slots: ProviderRegistrationOptionSlot[];
  }>;
}

export interface PersonaRoute {
  travel_session_id: string;
  persona_id: string;
  credential_profile_id: string;
  provider: Provider;
  model_id: string;
  route_epoch: number;
  changed_at: string;
}

interface RouteChangeRow extends PersonaRoute {
  route_change_id: string;
  event_id: string | null;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function requiredText(value: unknown, maximum: number, code: string): string {
  const result = typeof value === "string" ? value.trim() : "";
  if (!result || result.length > maximum) throw new Error(code);
  return result;
}

function safeProfile(row: ProviderProfileRow): SafeProviderProfile {
  return {
    credential_profile_id: row.credential_profile_id,
    display_name: row.display_name,
    provider: row.provider,
    allowed_base_url_id: row.allowed_base_url_id,
    enabled: row.enabled === 1,
  };
}

export async function getProviderProfile(db: D1Database, profileId: string): Promise<ProviderProfileRow | null> {
  return db
    .prepare("SELECT * FROM provider_profiles WHERE credential_profile_id = ?")
    .bind(profileId)
    .first<ProviderProfileRow>();
}

export async function listSafeProviderProfiles(db: D1Database): Promise<SafeProviderProfile[]> {
  const result = await db
    .prepare(
      `SELECT credential_profile_id, display_name, provider, secret_binding_id,
              allowed_base_url_id, enabled, created_at
       FROM provider_profiles ORDER BY provider ASC, display_name ASC, credential_profile_id ASC`,
    )
    .all<ProviderProfileRow>();
  return result.results.map(safeProfile);
}

/**
 * 所有者がSecret登録前にbinding/profileの対応と既存所有を確認するための正本metadata。
 * Secret値は返さず、登録済みbindingのprofile IDだけを所有者向けに付加する。
 */
export async function listProviderRegistrationOptions(db: D1Database): Promise<ProviderRegistrationOptions> {
  const owners = await db
    .prepare("SELECT credential_profile_id, secret_binding_id FROM provider_profiles")
    .all<{ credential_profile_id: string; secret_binding_id: string }>();
  const ownerByBinding = new Map<string, string>();
  for (const row of owners.results) {
    if (row.secret_binding_id && row.credential_profile_id) {
      ownerByBinding.set(row.secret_binding_id, row.credential_profile_id);
    }
  }

  const providers = new Map<Provider, ProviderRegistrationOptionSlot[]>();
  for (const item of listSecretBindingRegistrationMetadata()) {
    const slots = providers.get(item.provider) ?? [];
    slots.push({
      secret_binding_id: item.secretBindingId,
      credential_profile_id: item.credentialProfileId,
      display_name: item.displayName,
      registered_profile_id: ownerByBinding.get(item.secretBindingId) ?? null,
    });
    providers.set(item.provider, slots);
  }
  return {
    providers: [...providers.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([provider, slots]) => ({ provider, slots })),
  };
}

export async function upsertProviderProfile(
  db: D1Database,
  profileId: string,
  value: unknown,
  changedAt: string,
): Promise<SafeProviderProfile> {
  if (!PROFILE_ID.test(profileId)) throw new Error("invalid_credential_profile_id");
  const raw = record(value);
  const allowedKeys = new Set(["display_name", "provider", "secret_binding_id", "allowed_base_url_id", "enabled"]);
  if (Object.keys(raw).some((key) => !allowedKeys.has(key))) throw new Error("provider_profile_field_not_allowed");
  const displayName = requiredText(raw.display_name, 100, "invalid_provider_profile");
  const provider = requiredText(raw.provider, 20, "invalid_provider_profile") as Provider;
  if (!PROVIDERS.has(provider)) throw new Error("invalid_provider_profile");
  const secretBindingId = requiredText(raw.secret_binding_id, 100, "invalid_provider_profile") as SecretBindingId;
  const allowedBaseUrlId = requiredText(raw.allowed_base_url_id, 100, "invalid_provider_profile");
  const metadata = secretBindingMetadata(secretBindingId);
  if (metadata.provider !== provider || metadata.allowedBaseUrlId !== allowedBaseUrlId) {
    throw new Error("provider_profile_binding_mismatch");
  }
  if (raw.enabled !== undefined && typeof raw.enabled !== "boolean") throw new Error("invalid_provider_profile");
  const existing = await getProviderProfile(db, profileId);
  if (
    existing &&
    (existing.provider !== provider ||
      existing.secret_binding_id !== secretBindingId ||
      existing.allowed_base_url_id !== allowedBaseUrlId)
  ) {
    throw new Error("provider_profile_identity_immutable");
  }
  const bindingOwner = await db
    .prepare("SELECT credential_profile_id FROM provider_profiles WHERE secret_binding_id = ?")
    .bind(secretBindingId)
    .first<{ credential_profile_id: string }>();
  if (bindingOwner && bindingOwner.credential_profile_id !== profileId) {
    throw new Error("secret_binding_already_registered");
  }
  await db
    .prepare(
      `INSERT INTO provider_profiles
        (credential_profile_id, display_name, provider, secret_binding_id, allowed_base_url_id, enabled, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(credential_profile_id) DO UPDATE SET
         display_name = excluded.display_name,
         provider = excluded.provider,
         secret_binding_id = excluded.secret_binding_id,
         allowed_base_url_id = excluded.allowed_base_url_id,
         enabled = excluded.enabled`,
    )
    .bind(profileId, displayName, provider, secretBindingId, allowedBaseUrlId, raw.enabled === false ? 0 : 1, changedAt)
    .run();
  const saved = await getProviderProfile(db, profileId);
  if (!saved) throw new Error("provider_profile_save_failed");
  return safeProfile(saved);
}

export async function disableProviderProfile(db: D1Database, profileId: string): Promise<boolean> {
  if (!PROFILE_ID.test(profileId)) throw new Error("invalid_credential_profile_id");
  const result = await db
    .prepare("UPDATE provider_profiles SET enabled = 0 WHERE credential_profile_id = ? AND enabled = 1")
    .bind(profileId)
    .run();
  return Number(result.meta.changes ?? 0) === 1;
}

export async function requireEnabledProfile(db: D1Database, profileId: string): Promise<ProviderProfileRow> {
  const profile = await getProviderProfile(db, profileId);
  if (!profile || profile.enabled !== 1) throw new Error("credential_profile_unavailable");
  const metadata = secretBindingMetadata(profile.secret_binding_id);
  if (metadata.provider !== profile.provider || metadata.allowedBaseUrlId !== profile.allowed_base_url_id) {
    throw new Error("provider_profile_binding_mismatch");
  }
  return profile;
}

export async function getPersonaRoute(
  db: D1Database,
  travelSessionId: string,
  personaId: string,
): Promise<PersonaRoute | null> {
  return db
    .prepare(
      `SELECT travel_session_id, persona_id, credential_profile_id, provider, model_id, route_epoch, changed_at
       FROM persona_routes WHERE travel_session_id = ? AND persona_id = ?`,
    )
    .bind(travelSessionId, personaId)
    .first<PersonaRoute>();
}

function parseRouteChange(value: unknown): {
  routeChangeId: string;
  credentialProfileId: string;
  modelId: string;
} {
  const raw = record(value);
  const allowedKeys = new Set(["route_change_id", "credential_profile_id", "model_id"]);
  if (Object.keys(raw).some((key) => !allowedKeys.has(key))) throw new Error("route_change_field_not_allowed");
  const routeChangeId = requiredText(raw.route_change_id, 100, "invalid_route_change");
  if (!ROUTE_CHANGE_ID.test(routeChangeId)) throw new Error("invalid_route_change");
  const credentialProfileId = requiredText(raw.credential_profile_id, 100, "invalid_route_change");
  if (!PROFILE_ID.test(credentialProfileId)) throw new Error("invalid_route_change");
  const modelId = requiredText(raw.model_id, 200, "invalid_route_change");
  return { routeChangeId, credentialProfileId, modelId };
}

function routeFromChange(row: RouteChangeRow): PersonaRoute {
  return {
    travel_session_id: row.travel_session_id,
    persona_id: row.persona_id,
    credential_profile_id: row.credential_profile_id,
    provider: row.provider,
    model_id: row.model_id,
    route_epoch: row.route_epoch,
    changed_at: row.changed_at,
  };
}

function routeChangeMatches(
  row: RouteChangeRow,
  travelSessionId: string,
  credentialProfileId: string,
  modelId: string,
): boolean {
  return (
    row.travel_session_id === travelSessionId &&
    row.credential_profile_id === credentialProfileId &&
    row.model_id === modelId
  );
}

export async function changePersonaRoute(
  db: D1Database,
  travelSessionId: string,
  value: unknown,
  changedAt: string,
  requestedPersonaId?: string,
): Promise<{ changed: boolean; route: PersonaRoute; event_id: string | null }> {
  const input = parseRouteChange(value);
  const existing = await db
    .prepare("SELECT * FROM route_change_requests WHERE route_change_id = ?")
    .bind(input.routeChangeId)
    .first<RouteChangeRow>();
  if (existing) {
    if (!routeChangeMatches(existing, travelSessionId, input.credentialProfileId, input.modelId)) {
      throw new Error("route_change_id_conflict");
    }
    return { changed: existing.event_id !== null, route: routeFromChange(existing), event_id: existing.event_id };
  }

  const session = await db
    .prepare("SELECT persona_id, status, snapshot_schema_version FROM travel_sessions WHERE travel_session_id = ?")
    .bind(travelSessionId)
    .first<{ persona_id: string; status: string; snapshot_schema_version: number }>();
  if (!session) throw new Error("travel_session_not_found");
  if (session.status !== "active") throw new Error("travel_session_not_active");
  if (await getActiveMessageRequest(db, travelSessionId)) throw new Error("session_message_in_progress");
  const personaId = requestedPersonaId || session.persona_id;
  if (session.snapshot_schema_version === 4) {
    const persona = await db.prepare(
      "SELECT status FROM travel_personas WHERE travel_session_id = ? AND persona_id = ?",
    ).bind(travelSessionId, personaId).first<{ status: string }>();
    if (!persona) throw new Error("travel_persona_not_found");
    if (persona.status !== "active") throw new Error("travel_persona_not_active");
  }
  const current = await getPersonaRoute(db, travelSessionId, personaId);
  if (!current) throw new Error("persona_route_missing");
  const profile = await requireEnabledProfile(db, input.credentialProfileId);

  if (current.credential_profile_id === input.credentialProfileId && current.model_id === input.modelId) {
    try {
      await db
        .prepare(
          `INSERT INTO route_change_requests
            (route_change_id, travel_session_id, persona_id, credential_profile_id, provider,
             model_id, route_epoch, event_id, changed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)`,
        )
        .bind(
          input.routeChangeId,
          travelSessionId,
          personaId,
          current.credential_profile_id,
          current.provider,
          current.model_id,
          current.route_epoch,
          current.changed_at,
        )
        .run();
    } catch (error) {
      const raced = await db
        .prepare("SELECT * FROM route_change_requests WHERE route_change_id = ?")
        .bind(input.routeChangeId)
        .first<RouteChangeRow>();
      if (raced && routeChangeMatches(raced, travelSessionId, input.credentialProfileId, input.modelId)) {
        return { changed: raced.event_id !== null, route: routeFromChange(raced), event_id: raced.event_id };
      }
      if (raced) throw new Error("route_change_id_conflict");
      throw error;
    }
    return { changed: false, route: current, event_id: null };
  }

  const nextEpoch = current.route_epoch + 1;
  const eventId = crypto.randomUUID();
  const sequenceNo = await nextSequenceNumber(db, travelSessionId, personaId);
  const content = canonicalJson({
    credential_profile_id: input.credentialProfileId,
    provider: profile.provider,
    model_id: input.modelId,
    route_epoch: nextEpoch,
  });
  const nextRoute: PersonaRoute = {
    travel_session_id: travelSessionId,
    persona_id: personaId,
    credential_profile_id: input.credentialProfileId,
    provider: profile.provider,
    model_id: input.modelId,
    route_epoch: nextEpoch,
    changed_at: changedAt,
  };
  try {
    await db.batch([
      db
        .prepare(
          `INSERT INTO route_change_requests
            (route_change_id, travel_session_id, persona_id, credential_profile_id, provider,
             model_id, route_epoch, event_id, changed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        )
        .bind(
          input.routeChangeId,
          travelSessionId,
          personaId,
          input.credentialProfileId,
          profile.provider,
          input.modelId,
          nextEpoch,
          eventId,
          changedAt,
        ),
      db
        .prepare(
          `INSERT INTO travel_events
            (event_id, travel_session_id, persona_id, sequence_no, type, created_at, content,
             content_hash, provider, model_requested, model_resolved, route_epoch,
             reply_to_event_id, status)
           VALUES (?, ?, ?, ?, 'route_changed', ?, ?, ?, ?, ?, ?, ?, NULL, 'committed')`,
        )
        .bind(
          eventId,
          travelSessionId,
          personaId,
          sequenceNo,
          changedAt,
          content,
          await sha256(content),
          profile.provider,
          input.modelId,
          input.modelId,
          nextEpoch,
        ),
      db
        .prepare(
          `UPDATE persona_routes
           SET credential_profile_id = ?, provider = ?, model_id = ?, route_epoch = ?, changed_at = ?
           WHERE travel_session_id = ? AND persona_id = ? AND route_epoch = ?`,
        )
        .bind(
          input.credentialProfileId,
          profile.provider,
          input.modelId,
          nextEpoch,
          changedAt,
          travelSessionId,
          personaId,
          current.route_epoch,
        ),
      ...(session.snapshot_schema_version < 4 ? [
        db.prepare(
          `UPDATE travel_sessions SET credential_profile_id = ?, model_id = ?, route_epoch = ?
           WHERE travel_session_id = ? AND route_epoch = ?`,
        ).bind(input.credentialProfileId, input.modelId, nextEpoch, travelSessionId, current.route_epoch),
      ] : []),
    ]);
  } catch (error) {
    const raced = await db
      .prepare("SELECT * FROM route_change_requests WHERE route_change_id = ?")
      .bind(input.routeChangeId)
      .first<RouteChangeRow>();
    if (raced && routeChangeMatches(raced, travelSessionId, input.credentialProfileId, input.modelId)) {
      return { changed: raced.event_id !== null, route: routeFromChange(raced), event_id: raced.event_id };
    }
    if (raced) throw new Error("route_change_id_conflict");
    throw error;
  }
  return { changed: true, route: nextRoute, event_id: eventId };
}
