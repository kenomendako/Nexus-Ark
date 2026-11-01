import { canonicalJson, sha256 } from "./auth";
import { registerSnapshot, validateSnapshot, type TravelSnapshot } from "./phase1";
import { buildExternalAiExport } from "./external-ai-export";

const DAY_MS = 86_400_000;

interface StandbyRow {
  standby_snapshot_id: string;
  home_instance_id: string;
  generation: number;
  status: string;
  snapshot_schema_version: number;
  manifest_json: string;
  content_hash: string;
  ciphertext: string | null;
  nonce: string | null;
  encryption_key_id: string;
  created_at: string;
  expires_at: string;
  activation_id: string | null;
  activation_mode: string | null;
  activated_session_id: string | null;
  activated_at: string | null;
}

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function unbase64(value: string): Uint8Array {
  const binary = atob(value);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function arrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

async function aesKey(secret: string): Promise<CryptoKey> {
  if (secret.length < 32) throw new Error("standby_encryption_key_unavailable");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret));
  return crypto.subtle.importKey("raw", digest, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function encryptSnapshot(snapshot: TravelSnapshot, secret: string): Promise<{ ciphertext: string; nonce: string }> {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const encrypted = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: nonce }, await aesKey(secret), new TextEncoder().encode(canonicalJson(snapshot)),
  );
  return { ciphertext: base64(new Uint8Array(encrypted)), nonce: base64(nonce) };
}

async function decryptSnapshot(row: StandbyRow, secret: string): Promise<TravelSnapshot> {
  if (!row.ciphertext || !row.nonce) throw new Error("standby_content_unavailable");
  try {
    const decrypted = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: arrayBuffer(unbase64(row.nonce)) },
      await aesKey(secret),
      arrayBuffer(unbase64(row.ciphertext)),
    );
    return validateSnapshot(JSON.parse(new TextDecoder().decode(decrypted)));
  } catch (error) {
    if (error instanceof Error && error.message === "standby_encryption_key_unavailable") throw error;
    throw new Error("standby_decryption_failed");
  }
}

function snapshotPersonas(snapshot: TravelSnapshot): Array<{ persona_id: string; persona_display_name: string }> {
  return snapshot.schema_version === 4
    ? snapshot.personas.map(({ persona_id, persona_display_name }) => ({ persona_id, persona_display_name }))
    : [{ persona_id: snapshot.persona_id, persona_display_name: snapshot.persona_display_name }];
}

function safeManifest(row: StandbyRow): Record<string, unknown> {
  const manifest = JSON.parse(row.manifest_json) as Record<string, unknown>;
  return {
    standby_snapshot_id: row.standby_snapshot_id,
    home_instance_id: row.home_instance_id,
    generation: row.generation,
    status: row.status,
    snapshot_schema_version: row.snapshot_schema_version,
    content_hash: row.content_hash,
    created_at: row.created_at,
    expires_at: row.expires_at,
    activation_id: row.activation_id,
    activation_mode: row.activation_mode,
    activated_session_id: row.activated_session_id,
    activated_at: row.activated_at,
    ...manifest,
  };
}

async function assertNoActivePersona(db: D1Database, snapshot: TravelSnapshot): Promise<void> {
  for (const persona of snapshotPersonas(snapshot)) {
    const active = await db.prepare(
      `SELECT 1 AS found FROM travel_personas
       WHERE persona_id = ? AND status IN ('armed', 'active', 'returning') LIMIT 1`,
    ).bind(persona.persona_id).first();
    if (active) throw new Error("standby_persona_active");
  }
}

export async function registerStandbySnapshot(
  db: D1Database,
  raw: unknown,
  encryptionSecret: string | undefined,
  encryptionKeyId: string,
  now: Date,
): Promise<Record<string, unknown>> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("invalid_standby_snapshot");
  const request = raw as Record<string, unknown>;
  const homeInstanceId = String(request.home_instance_id ?? "");
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{2,99}$/.test(homeInstanceId)) throw new Error("invalid_home_instance_id");
  const retentionDays = Number(request.retention_days ?? 7);
  if (!Number.isInteger(retentionDays) || retentionDays < 1 || retentionDays > 30) {
    throw new Error("invalid_standby_retention");
  }
  if (!encryptionSecret) throw new Error("standby_encryption_key_unavailable");
  const snapshot = validateSnapshot(request.snapshot);
  await assertNoActivePersona(db, snapshot);
  const createdAt = now.toISOString();
  const expiresAt = new Date(now.getTime() + retentionDays * DAY_MS).toISOString();
  const previous = await db.prepare(
    "SELECT generation FROM standby_snapshots WHERE home_instance_id = ? ORDER BY generation DESC LIMIT 1",
  ).bind(homeInstanceId).first<{ generation: number }>();
  const generation = Number(previous?.generation ?? 0) + 1;
  const standbyId = crypto.randomUUID();
  const serialized = canonicalJson(snapshot);
  const contentHash = await sha256(serialized);
  const encrypted = await encryptSnapshot(snapshot, encryptionSecret);
  const personas = snapshotPersonas(snapshot);
  const manifest = canonicalJson({
    personas,
    persona_count: personas.length,
    section_names: ["system_prompt", "core_memory", "episodic_summary", "recent_messages"],
    content_chars: serialized.length,
  });
  const oldDeleteAfter = new Date(now.getTime() + DAY_MS).toISOString();
  await db.batch([
    db.prepare(
      `UPDATE standby_snapshots SET status = 'superseded', superseded_at = ?, content_delete_after = ?
       WHERE home_instance_id = ? AND status = 'ready'`,
    ).bind(createdAt, oldDeleteAfter, homeInstanceId),
    db.prepare(
      `INSERT INTO standby_snapshots
       (standby_snapshot_id, home_instance_id, generation, status, snapshot_schema_version,
        manifest_json, content_hash, ciphertext, nonce, encryption_key_id, created_at, expires_at)
       VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      standbyId, homeInstanceId, generation, snapshot.schema_version, manifest, contentHash,
      encrypted.ciphertext, encrypted.nonce, encryptionKeyId, createdAt, expiresAt,
    ),
  ]);
  const row = await getStandbyRow(db, standbyId);
  if (!row) throw new Error("standby_registration_failed");
  return safeManifest(row);
}

async function getStandbyRow(db: D1Database, standbyId: string): Promise<StandbyRow | null> {
  return db.prepare("SELECT * FROM standby_snapshots WHERE standby_snapshot_id = ?")
    .bind(standbyId).first<StandbyRow>();
}

export async function listStandbySnapshots(db: D1Database, now: string): Promise<Record<string, unknown>[]> {
  await db.prepare(
    `UPDATE standby_snapshots SET status = 'expired'
     WHERE status = 'ready' AND expires_at <= ?`,
  ).bind(now).run();
  const rows = await db.prepare(
    `SELECT * FROM standby_snapshots
     WHERE status IN ('ready', 'activated') ORDER BY created_at DESC`,
  ).all<StandbyRow>();
  return rows.results.map(safeManifest);
}

export async function buildStandbyExternalAiExport(
  db: D1Database,
  standbyId: string,
  personaId: string,
  rawOptions: unknown,
  encryptionSecret: string | undefined,
  now: Date,
): Promise<Record<string, unknown>> {
  const row = await getStandbyRow(db, standbyId);
  if (!row) throw new Error("standby_not_found");
  if (row.status !== "ready") throw new Error("standby_not_ready");
  if (row.expires_at <= now.toISOString()) throw new Error("standby_expired");
  if (!encryptionSecret) throw new Error("standby_encryption_key_unavailable");
  const snapshot = await decryptSnapshot(row, encryptionSecret);
  return buildExternalAiExport(snapshot, personaId, rawOptions, "待機中のお出かけ前データ", row.created_at);
}

export async function activateStandbySnapshot(
  db: D1Database,
  standbyId: string,
  raw: unknown,
  encryptionSecret: string | undefined,
  now: Date,
): Promise<Record<string, unknown>> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("invalid_activation");
  const request = raw as Record<string, unknown>;
  const activationId = String(request.activation_id ?? "");
  const mode = String(request.activation_mode ?? "");
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{7,99}$/.test(activationId)) throw new Error("invalid_activation_id");
  if (!["planned", "recovery_unconfirmed"].includes(mode)) throw new Error("invalid_activation_mode");
  const duplicate = await db.prepare(
    "SELECT * FROM standby_snapshots WHERE activation_id = ?",
  ).bind(activationId).first<StandbyRow>();
  if (duplicate) return { ...safeManifest(duplicate), duplicate: true };
  const row = await getStandbyRow(db, standbyId);
  if (!row) throw new Error("standby_not_found");
  if (row.status !== "ready") throw new Error("standby_not_ready");
  if (row.expires_at <= now.toISOString()) throw new Error("standby_expired");
  if (!encryptionSecret) throw new Error("standby_encryption_key_unavailable");
  const snapshot = await decryptSnapshot(row, encryptionSecret);
  const sessionId = `recovery-${crypto.randomUUID()}`;
  const activatedSnapshot = { ...snapshot, travel_session_id: sessionId } as TravelSnapshot;
  const claimed = await db.prepare(
    `UPDATE standby_snapshots SET status = 'activated', activation_id = ?, activation_mode = ?,
       activated_session_id = ?, activated_at = ?
     WHERE standby_snapshot_id = ? AND status = 'ready' AND activation_id IS NULL`,
  ).bind(activationId, mode, sessionId, now.toISOString(), standbyId).run();
  if (Number(claimed.meta.changes ?? 0) !== 1) throw new Error("standby_activation_conflict");
  try {
    await registerSnapshot(db, activatedSnapshot);
    await db.prepare(
      `UPDATE travel_sessions SET activation_mode = ?, branch_divergence_possible = ?
       WHERE travel_session_id = ?`,
    ).bind(mode, mode === "recovery_unconfirmed" ? 1 : 0, sessionId).run();
    if (mode === "recovery_unconfirmed") {
      await db.prepare(
        "UPDATE travel_personas SET branch_divergence_possible = 1 WHERE travel_session_id = ?",
      ).bind(sessionId).run();
    }
    await db.prepare(
      `UPDATE standby_snapshots SET ciphertext = NULL, nonce = NULL, content_deleted_at = ?
       WHERE standby_snapshot_id = ? AND status = 'activated'`,
    ).bind(now.toISOString(), standbyId).run();
  } catch (error) {
    await db.prepare(
      `UPDATE standby_snapshots SET status = 'ready', activation_id = NULL, activation_mode = NULL,
       activated_session_id = NULL, activated_at = NULL WHERE standby_snapshot_id = ?`,
    ).bind(standbyId).run();
    throw error;
  }
  const activated = await getStandbyRow(db, standbyId);
  return { ...safeManifest(activated!), duplicate: false, branch_divergence_possible: mode === "recovery_unconfirmed" };
}

export async function deleteStandbySnapshot(db: D1Database, standbyId: string, now: string): Promise<boolean> {
  const result = await db.prepare(
    `UPDATE standby_snapshots SET status = 'deleted', ciphertext = NULL, nonce = NULL,
       content_deleted_at = COALESCE(content_deleted_at, ?)
     WHERE standby_snapshot_id = ? AND status NOT IN ('activated', 'deleted')`,
  ).bind(now, standbyId).run();
  return Number(result.meta.changes ?? 0) === 1;
}

export async function deleteExpiredStandbyContent(db: D1Database, now: string): Promise<number> {
  await db.prepare(
    "UPDATE standby_snapshots SET status = 'expired' WHERE status = 'ready' AND expires_at <= ?",
  ).bind(now).run();
  const result = await db.prepare(
    `UPDATE standby_snapshots SET ciphertext = NULL, nonce = NULL, content_deleted_at = ?
     WHERE content_deleted_at IS NULL AND ciphertext IS NOT NULL
       AND ((status = 'expired' AND expires_at <= ?) OR (status = 'superseded' AND content_delete_after <= ?))`,
  ).bind(now, now, now).run();
  return Number(result.meta.changes ?? 0);
}
