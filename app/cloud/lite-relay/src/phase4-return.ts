import { canonicalJson, hmacSha256, sha256 } from "./auth";
import { getUsageSummary } from "./budget";

interface PersonaRow {
  persona_id: string;
  display_name: string;
  presence_mode: "exclusive" | "parallel";
  status: string;
  snapshot_hash: string;
  home_anchor_hash: string | null;
  last_ack_sequence: number;
  last_ack_payload_hash: string | null;
  acknowledged_at: string | null;
  branch_divergence_possible: number;
  return_high_water_sequence: number | null;
}

async function sessionRow(db: D1Database, sessionId: string) {
  return db.prepare(
    `SELECT travel_session_id, status, retention_days, created_at, snapshot_schema_version,
            activation_mode, branch_divergence_possible
     FROM travel_sessions WHERE travel_session_id = ?`,
  ).bind(sessionId).first<Record<string, unknown>>();
}

async function personas(db: D1Database, sessionId: string): Promise<PersonaRow[]> {
  return (await db.prepare(
    `SELECT persona_id, display_name, presence_mode, status, snapshot_hash, home_anchor_hash,
            last_ack_sequence, last_ack_payload_hash, acknowledged_at, branch_divergence_possible,
            return_high_water_sequence
     FROM travel_personas WHERE travel_session_id = ? ORDER BY persona_id`,
  ).bind(sessionId).all<PersonaRow>()).results;
}

async function highWater(db: D1Database, sessionId: string, personaId: string): Promise<number> {
  const row = await db.prepare(
    `SELECT COALESCE(MAX(sequence_no), 0) AS value FROM travel_events
     WHERE travel_session_id = ? AND persona_id = ? AND status = 'committed'`,
  ).bind(sessionId, personaId).first<{ value: number }>();
  return Number(row?.value || 0);
}

async function frozenHighWater(db: D1Database, sessionId: string, personaId: string): Promise<number> {
  const row = await db.prepare(
    `SELECT status, return_high_water_sequence FROM travel_personas
     WHERE travel_session_id = ? AND persona_id = ?`,
  ).bind(sessionId, personaId).first<{ status: string; return_high_water_sequence: number | null }>();
  if (!row) throw new Error("travel_persona_not_found");
  if (row.return_high_water_sequence === null) throw new Error("return_not_started");
  return Number(row.return_high_water_sequence);
}

async function manifestHighWater(db: D1Database, sessionId: string, persona: PersonaRow): Promise<number> {
  if (persona.status === "active" && persona.return_high_water_sequence === null) {
    return highWater(db, sessionId, persona.persona_id);
  }
  return frozenHighWater(db, sessionId, persona.persona_id);
}

export async function startReturn(db: D1Database, sessionId: string, nowIso: string) {
  const session = await sessionRow(db, sessionId);
  if (!session || Number(session.snapshot_schema_version) !== 4) throw new Error("travel_session_not_found");
  if (!["active", "returning"].includes(String(session.status))) throw new Error("travel_session_not_active");
  if (session.status === "returning") {
    const pending = await db.prepare(
      `SELECT 1 FROM message_requests
       WHERE travel_session_id = ? AND status IN ('reserved', 'provider_started') LIMIT 1`,
    ).bind(sessionId).first();
    if (pending) throw new Error("session_message_in_progress");
  }
  const starting = session.status === "active";
  const results = await db.batch([
    db.prepare(
      `UPDATE travel_sessions SET status = 'returning'
       WHERE travel_session_id = ? AND status = 'active'
         AND NOT EXISTS (
           SELECT 1 FROM message_requests
           WHERE travel_session_id = ? AND status IN ('reserved', 'provider_started')
         )`,
    ).bind(sessionId, sessionId),
    db.prepare(
      `UPDATE travel_personas
       SET status = 'returning',
           return_high_water_sequence = COALESCE(
             return_high_water_sequence,
             (SELECT COALESCE(MAX(te.sequence_no), 0) FROM travel_events te
              WHERE te.travel_session_id = travel_personas.travel_session_id
                AND te.persona_id = travel_personas.persona_id AND te.status = 'committed')
           )
       WHERE travel_session_id = ? AND status IN ('active', 'returning')
         AND EXISTS (
           SELECT 1 FROM travel_sessions s
           WHERE s.travel_session_id = travel_personas.travel_session_id AND s.status = 'returning'
         )`,
    ).bind(sessionId),
  ]);
  const transitioned = Number(results[0]?.meta.changes ?? 0) === 1;
  if (starting && !transitioned) {
    const raced = await sessionRow(db, sessionId);
    if (raced?.status !== "returning") throw new Error("session_message_in_progress");
  }
  if (transitioned) await db.batch([
    db.prepare(
      `INSERT INTO audit_events (audit_event_id, event_type, travel_session_id, occurred_at, outcome, detail_code)
       VALUES (?, 'return_started', ?, ?, 'ok', 'phase4')`,
    ).bind(crypto.randomUUID(), sessionId, nowIso),
  ]);
  return buildReturnManifest(db, sessionId, nowIso);
}

export async function buildReturnManifest(db: D1Database, sessionId: string, nowIso: string) {
  const session = await sessionRow(db, sessionId);
  if (!session || Number(session.snapshot_schema_version) !== 4) throw new Error("travel_session_not_found");
  const items = await personas(db, sessionId);
  return {
    schema_version: 4,
    session: { ...session, usage_summary: await getUsageSummary(db, sessionId, nowIso) },
    personas: await Promise.all(items.map(async (persona) => {
      const { return_high_water_sequence: _returnHighWaterSequence, ...publicPersona } = persona;
      return {
        ...publicPersona,
        high_water_sequence: await manifestHighWater(db, sessionId, persona),
        usage_summary: await getUsageSummary(db, sessionId, nowIso, persona.persona_id),
      };
    })),
  };
}

async function personaPayload(
  db: D1Database,
  sessionId: string,
  personaId: string,
  afterSequence: number,
  exportedAt: string,
) {
  const session = await sessionRow(db, sessionId);
  const persona = await db.prepare(
    `SELECT tp.*, p.snapshot_json, r.credential_profile_id, r.provider, r.model_id, r.route_epoch
     FROM travel_personas tp
     JOIN persona_snapshots p USING (travel_session_id, persona_id)
     JOIN persona_routes r USING (travel_session_id, persona_id)
     WHERE tp.travel_session_id = ? AND tp.persona_id = ?`,
  ).bind(sessionId, personaId).first<Record<string, unknown>>();
  if (!session || !persona || Number(session.snapshot_schema_version) !== 4) throw new Error("travel_persona_not_found");
  const highWaterSequence = await frozenHighWater(db, sessionId, personaId);
  if (!Number.isInteger(afterSequence) || afterSequence < 0 || afterSequence > highWaterSequence) {
    throw new Error("invalid_return_cursor");
  }
  const events = (await db.prepare(
    `SELECT event_id, persona_id, sequence_no, type, created_at, content, content_hash, provider,
            model_requested, model_resolved, route_epoch, reply_to_event_id, status, branch_id
     FROM travel_events WHERE travel_session_id = ? AND persona_id = ?
       AND sequence_no > ? AND sequence_no <= ? AND status = 'committed'
     ORDER BY sequence_no, event_id`,
  ).bind(sessionId, personaId, afterSequence, highWaterSequence).all()).results;
  const receipts = (await db.prepare(
    `SELECT receipt_id, event_id, persona_id, occurred_at, provider, gateway, credential_profile_id,
            model_requested, model_resolved, upstream_provider, input_tokens, output_tokens,
            cache_read_tokens, cache_creation_tokens, provider_reported_cost_usd, route_epoch,
            usage_status, pricing_version, cost_basis, estimate_status, input_cost_usd,
            output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd, cache_storage_cost_usd,
            estimated_cost_usd, estimated_savings_usd, unknown_reason, cache_status,
            cache_ttl_seconds, cache_creation_5m_tokens, cache_creation_1h_tokens
     FROM usage_receipts WHERE travel_session_id = ? AND persona_id = ?
       AND event_id IN (SELECT event_id FROM travel_events WHERE travel_session_id = ? AND persona_id = ?
         AND sequence_no > ? AND sequence_no <= ?)
     ORDER BY occurred_at, receipt_id`,
  ).bind(sessionId, personaId, sessionId, personaId, afterSequence, highWaterSequence).all()).results;
  const snapshot = JSON.parse(String(persona.snapshot_json)) as Record<string, unknown>;
  delete persona.snapshot_json;
  return {
    schema_version: 4,
    exported_at: exportedAt,
    session: {
      travel_session_id: sessionId,
      status: session.status,
      retention_days: session.retention_days,
      created_at: session.created_at,
    },
    persona: {
      persona_id: persona.persona_id,
      persona_display_name: persona.display_name,
      presence_mode: persona.presence_mode,
      snapshot_hash: persona.snapshot_hash,
      home_anchor_hash: persona.home_anchor_hash,
      branch_divergence_possible: Boolean(persona.branch_divergence_possible),
      initial_route: snapshot.initial_route,
      final_route: {
        credential_profile_id: persona.credential_profile_id,
        provider: persona.provider,
        model_id: persona.model_id,
        route_epoch: persona.route_epoch,
      },
      budget: snapshot.budget,
      usage_summary: await getUsageSummary(db, sessionId, exportedAt, personaId),
    },
    cursor: { after_sequence: afterSequence, through_sequence: highWaterSequence },
    events,
    receipts,
  };
}

async function signed(payload: Record<string, unknown>, secret: string) {
  const payloadCanonical = canonicalJson(payload);
  return {
    algorithm: "HMAC-SHA-256",
    payload,
    payload_canonical: payloadCanonical,
    payload_hash: await sha256(payloadCanonical),
    signature: await hmacSha256(payloadCanonical, secret),
  };
}

export async function buildSignedReturnChunk(
  db: D1Database, sessionId: string, personaId: string, afterSequence: number,
  secret: string, exportedAt: string,
) {
  return signed(await personaPayload(db, sessionId, personaId, afterSequence, exportedAt), secret);
}

export async function buildMultiPersonaBundle(db: D1Database, sessionId: string, secret: string, exportedAt: string) {
  const manifest = await buildReturnManifest(db, sessionId, exportedAt);
  const chunks = await Promise.all((manifest.personas as Array<{ persona_id: string }>).map(
    (persona) => personaPayload(db, sessionId, persona.persona_id, 0, exportedAt),
  ));
  return signed({ schema_version: 4, exported_at: exportedAt, manifest, personas: chunks }, secret);
}

export async function acknowledgeReturn(db: D1Database, sessionId: string, value: unknown, nowIso: string) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_return_ack");
  const raw = value as Record<string, unknown>;
  const ackId = typeof raw.ack_id === "string" ? raw.ack_id : "";
  const personaId = typeof raw.persona_id === "string" ? raw.persona_id : "";
  const throughSequence = Number(raw.through_sequence);
  const payloadHash = typeof raw.payload_hash === "string" ? raw.payload_hash : "";
  if (!/^[A-Za-z0-9_-]{8,100}$/.test(ackId) || !personaId || !Number.isInteger(throughSequence) || throughSequence < 0 || !payloadHash) {
    throw new Error("invalid_return_ack");
  }
  const expected = await frozenHighWater(db, sessionId, personaId);
  if (throughSequence !== expected) throw new Error("return_ack_cursor_mismatch");
  const existing = await db.prepare("SELECT * FROM return_ack_requests WHERE ack_id = ?")
    .bind(ackId).first<Record<string, unknown>>();
  if (existing) {
    if (existing.travel_session_id !== sessionId || existing.persona_id !== personaId ||
        Number(existing.through_sequence) !== throughSequence || existing.payload_hash !== payloadHash) {
      throw new Error("return_ack_id_conflict");
    }
    return { acknowledged: true, duplicate: true, all_acknowledged: await allAcknowledged(db, sessionId) };
  }
  await db.batch([
    db.prepare(
      `INSERT INTO return_ack_requests
        (ack_id, travel_session_id, persona_id, through_sequence, payload_hash, acknowledged_at)
       VALUES (?, ?, ?, ?, ?, ?)`,
    ).bind(ackId, sessionId, personaId, throughSequence, payloadHash, nowIso),
    db.prepare(
      `UPDATE travel_personas SET last_ack_sequence = ?, last_ack_payload_hash = ?, acknowledged_at = ?, status = 'closed'
       WHERE travel_session_id = ? AND persona_id = ? AND status IN ('returning', 'emergency_reclaimed')`,
    ).bind(throughSequence, payloadHash, nowIso, sessionId, personaId),
  ]);
  return { acknowledged: true, duplicate: false, all_acknowledged: await allAcknowledged(db, sessionId) };
}

export async function allAcknowledged(db: D1Database, sessionId: string): Promise<boolean> {
  const row = await db.prepare(
    `SELECT COUNT(*) AS total, SUM(CASE WHEN acknowledged_at IS NOT NULL THEN 1 ELSE 0 END) AS acknowledged
     FROM travel_personas WHERE travel_session_id = ?`,
  ).bind(sessionId).first<{ total: number; acknowledged: number }>();
  return Number(row?.total || 0) > 0 && Number(row?.total) === Number(row?.acknowledged || 0);
}

export async function emergencyReclaimPersona(
  db: D1Database, sessionId: string, personaId: string, reason: string, nowIso: string,
) {
  if (!reason.trim() || reason.length > 500) throw new Error("invalid_emergency_reason");
  const persona = await db.prepare(
    "SELECT status FROM travel_personas WHERE travel_session_id = ? AND persona_id = ?",
  ).bind(sessionId, personaId).first<{ status: string }>();
  if (!persona) throw new Error("travel_persona_not_found");
  if (persona.status === "emergency_reclaimed") return { reclaimed: true, duplicate: true };
  if (!["active", "returning"].includes(persona.status)) throw new Error("travel_persona_not_active");
  await db.batch([
    db.prepare(
      `UPDATE travel_personas
       SET status = 'emergency_reclaimed', branch_divergence_possible = 1,
           return_high_water_sequence = COALESCE(
             return_high_water_sequence,
             (SELECT COALESCE(MAX(te.sequence_no), 0) FROM travel_events te
              WHERE te.travel_session_id = travel_personas.travel_session_id
                AND te.persona_id = travel_personas.persona_id AND te.status = 'committed')
           )
       WHERE travel_session_id = ? AND persona_id = ? AND status IN ('active', 'returning')`,
    ).bind(sessionId, personaId),
    db.prepare(
      `INSERT INTO audit_events
        (audit_event_id, event_type, travel_session_id, persona_id, occurred_at, outcome, detail_code)
       VALUES (?, 'emergency_reclaimed', ?, ?, ?, 'ok', ?)`,
    ).bind(crypto.randomUUID(), sessionId, personaId, nowIso, `reason_sha256:${await sha256(reason)}`),
  ]);
  return { reclaimed: true, duplicate: false };
}
