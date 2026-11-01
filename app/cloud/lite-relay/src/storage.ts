import { REQUEST_EXPIRY_MS } from "./request-deadline";
import type { CostBreakdown } from "./pricing-catalog";
import type { CanonicalUsage, MessageRequestRow, Provider } from "./types";

export interface ReserveMessageInput {
  clientMessageId: string;
  travelSessionId: string;
  personaId: string;
  provider: Provider;
  credentialProfileId?: string | null;
  modelRequested: string;
  routeEpoch?: number;
  reservedAt: string;
}

export interface CommitMessageInput {
  clientMessageId: string;
  eventId: string;
  receiptId: string;
  travelSessionId: string;
  personaId: string;
  sequenceNo: number;
  createdAt: string;
  content: string;
  contentHash: string;
  provider: Provider;
  routeEpoch?: number;
  gateway: string | null;
  credentialProfileId: string;
  modelRequested: string;
  modelResolved: string | null;
  upstreamProvider: string | null;
  usage: CanonicalUsage;
  pricing: CostBreakdown;
  signature: string;
}

export interface CommitConversationTurnInput extends CommitMessageInput {
  userEventId: string;
  userSequenceNo: number;
  userContent: string;
  userContentHash: string;
  assistantSequenceNo: number;
}

export interface TravelEventRow {
  event_id: string;
  travel_session_id: string;
  persona_id: string;
  sequence_no: number;
  type: string;
  created_at: string;
  content: string | null;
  content_hash: string;
  provider: string | null;
  model_requested: string | null;
  model_resolved: string | null;
  route_epoch: number;
  reply_to_event_id: string | null;
  status: "committed" | "partial";
  content_deleted_at: string | null;
}

export interface EventPage {
  events: TravelEventRow[];
  next_cursor: number;
  missing_sequences: number[];
}

export async function createTravelSession(
  db: D1Database,
  input: { travelSessionId: string; status?: "armed" | "active"; retentionDays: 0 | 7 | 30; createdAt: string },
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO travel_sessions
        (travel_session_id, status, retention_days, created_at)
       VALUES (?, ?, ?, ?)`,
    )
    .bind(input.travelSessionId, input.status ?? "active", input.retentionDays, input.createdAt)
    .run();
}

export async function reserveMessage(
  db: D1Database,
  input: ReserveMessageInput,
): Promise<{ created: boolean; request: MessageRequestRow }> {
  const result = await db
    .prepare(
      `INSERT INTO message_requests
        (client_message_id, travel_session_id, persona_id, status, provider, credential_profile_id,
         model_requested, route_epoch, reserved_at)
       SELECT ?, ?, ?, 'reserved', ?, ?, ?, ?, ?
       WHERE EXISTS (
         SELECT 1 FROM travel_sessions s
         LEFT JOIN travel_personas tp
           ON tp.travel_session_id = s.travel_session_id AND tp.persona_id = ?
         WHERE s.travel_session_id = ? AND s.status = 'active'
           AND (s.snapshot_schema_version < 4 OR tp.status = 'active')
       )
       ON CONFLICT(client_message_id) DO NOTHING`,
    )
    .bind(
      input.clientMessageId,
      input.travelSessionId,
      input.personaId,
      input.provider,
      input.credentialProfileId ?? null,
      input.modelRequested,
      input.routeEpoch ?? 0,
      input.reservedAt,
      input.personaId,
      input.travelSessionId,
    )
    .run();
  const request = await getMessageRequest(db, input.clientMessageId);
  if (!request) {
    throw new Error("travel_session_not_active");
  }
  return { created: Number(result.meta.changes ?? 0) === 1, request };
}

export async function getMessageRequest(db: D1Database, clientMessageId: string): Promise<MessageRequestRow | null> {
  return db
    .prepare("SELECT * FROM message_requests WHERE client_message_id = ?")
    .bind(clientMessageId)
    .first<MessageRequestRow>();
}

export async function getActiveMessageRequest(
  db: D1Database,
  travelSessionId: string,
): Promise<MessageRequestRow | null> {
  return db
    .prepare(
      `SELECT * FROM message_requests
       WHERE travel_session_id = ? AND status IN ('reserved', 'provider_started')
       ORDER BY reserved_at ASC LIMIT 1`,
    )
    .bind(travelSessionId)
    .first<MessageRequestRow>();
}

export async function markProviderStarted(db: D1Database, clientMessageId: string, startedAt: string): Promise<boolean> {
  const result = await db
    .prepare(
      `UPDATE message_requests
       SET status = 'provider_started', provider_started_at = ?
       WHERE client_message_id = ? AND status = 'reserved'
         AND EXISTS (
           SELECT 1 FROM travel_sessions s
           LEFT JOIN travel_personas tp
             ON tp.travel_session_id = s.travel_session_id
            AND tp.persona_id = message_requests.persona_id
           WHERE s.travel_session_id = message_requests.travel_session_id
             AND s.status = 'active'
             AND (s.snapshot_schema_version < 4 OR tp.status = 'active')
         )`,
    )
    .bind(startedAt, clientMessageId)
    .run();
  return Number(result.meta.changes ?? 0) === 1;
}

export async function markRequestTerminal(
  db: D1Database,
  clientMessageId: string,
  status: "partial" | "failed_known" | "outcome_unknown",
  finalizedAt: string,
): Promise<void> {
  await db
    .prepare(
      `UPDATE message_requests SET status = ?, finalized_at = ?
       WHERE client_message_id = ? AND status IN ('reserved', 'provider_started')`,
    )
    .bind(status, finalizedAt, clientMessageId)
    .run();
}

export async function recordProviderTerminalDiagnostics(
  db: D1Database,
  clientMessageId: string,
  diagnostics: {
    terminalCode: string | null;
    streamRecordCount: number;
    textChars: number;
    usageStatus: string;
    unknownEventCount: number;
  },
): Promise<void> {
  await db
    .prepare(
      `UPDATE message_requests
       SET provider_terminal_code = ?, provider_stream_record_count = ?, provider_text_chars = ?,
           provider_usage_status = ?, provider_unknown_event_count = ?
       WHERE client_message_id = ?`,
    )
    .bind(
      diagnostics.terminalCode,
      diagnostics.streamRecordCount,
      diagnostics.textChars,
      diagnostics.usageStatus,
      diagnostics.unknownEventCount,
      clientMessageId,
    )
    .run();
}

export async function recordProviderHttpFailure(
  db: D1Database,
  clientMessageId: string,
  httpStatus: number | null,
  errorCode: string,
): Promise<void> {
  await db
    .prepare(
      `UPDATE message_requests SET provider_http_status = ?, provider_error_code = ?
       WHERE client_message_id = ?`,
    )
    .bind(httpStatus, errorCode, clientMessageId)
    .run();
}

export async function commitCompletedMessage(db: D1Database, input: CommitMessageInput): Promise<void> {
  const request = await getMessageRequest(db, input.clientMessageId);
  const routeEpoch = input.routeEpoch ?? 0;
  if (
    !request ||
    request.status !== "provider_started" ||
    request.travel_session_id !== input.travelSessionId ||
    request.persona_id !== input.personaId ||
    request.provider !== input.provider ||
    request.model_requested !== input.modelRequested ||
    request.route_epoch !== routeEpoch ||
    (request.credential_profile_id !== null && request.credential_profile_id !== input.credentialProfileId)
  ) {
    throw new Error("message_request_not_committable");
  }
  const results = await db.batch([
    db
      .prepare(
        `INSERT INTO travel_events
          (event_id, travel_session_id, persona_id, sequence_no, type, created_at, content, content_hash,
           provider, model_requested, model_resolved, route_epoch, status)
         SELECT ?, ?, ?, ?, 'assistant_message', ?, ?, ?, ?, ?, ?, ?, 'committed'
         WHERE EXISTS (
           SELECT 1 FROM message_requests WHERE client_message_id = ? AND status = 'provider_started'
         )`,
      )
      .bind(
        input.eventId,
        input.travelSessionId,
        input.personaId,
        input.sequenceNo,
        input.createdAt,
        input.content,
        input.contentHash,
        input.provider,
        input.modelRequested,
        input.modelResolved,
        routeEpoch,
        input.clientMessageId,
      ),
    db
      .prepare(
        `INSERT INTO usage_receipts
          (receipt_id, event_id, travel_session_id, persona_id, occurred_at, provider, gateway,
           credential_profile_id, model_requested, model_resolved, upstream_provider, route_epoch, input_tokens,
           output_tokens, cache_read_tokens, cache_creation_tokens, provider_reported_cost_usd,
           usage_status, pricing_version, cost_basis, estimate_status, input_cost_usd, output_cost_usd,
           cache_read_cost_usd, cache_creation_cost_usd, cache_storage_cost_usd, estimated_cost_usd,
           estimated_savings_usd, unknown_reason, cache_status, cache_ttl_seconds,
           cache_creation_5m_tokens, cache_creation_1h_tokens, signature)
         SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
         WHERE EXISTS (
           SELECT 1 FROM message_requests WHERE client_message_id = ? AND status = 'provider_started'
         )`,
      )
      .bind(
        input.receiptId,
        input.eventId,
        input.travelSessionId,
        input.personaId,
        input.createdAt,
        input.provider,
        input.gateway,
        input.credentialProfileId,
        input.modelRequested,
        input.modelResolved,
        input.upstreamProvider,
        routeEpoch,
        input.usage.input_tokens,
        input.usage.output_tokens,
        input.usage.cache_read_tokens,
        input.usage.cache_creation_tokens,
        input.usage.provider_reported_cost_usd,
        input.usage.usage_status,
        input.pricing.pricing_version,
        input.pricing.cost_basis,
        input.pricing.estimate_status,
        input.pricing.input_cost_usd,
        input.pricing.output_cost_usd,
        input.pricing.cache_read_cost_usd,
        input.pricing.cache_creation_cost_usd,
        input.pricing.cache_storage_cost_usd,
        input.pricing.estimated_cost_usd,
        input.pricing.estimated_savings_usd,
        input.pricing.unknown_reason,
        input.usage.cache_status ?? "unreported",
        input.usage.cache_ttl_seconds ?? null,
        input.usage.cache_creation_5m_tokens ?? null,
        input.usage.cache_creation_1h_tokens ?? null,
        input.signature,
        input.clientMessageId,
      ),
    db
      .prepare(
        `UPDATE message_requests
         SET status = 'completed', event_id = ?, receipt_id = ?, finalized_at = ?,
             budget_state = CASE WHEN budget_reserved_usd IS NULL THEN NULL ELSE ? END,
             budget_settled_usd = ?, budget_settled_at = ?
         WHERE client_message_id = ? AND status = 'provider_started'`,
      )
      .bind(
        input.eventId,
        input.receiptId,
        input.createdAt,
        input.pricing.estimated_cost_usd === null ? "held" : "settled",
        input.pricing.estimated_cost_usd,
        input.createdAt,
        input.clientMessageId,
      ),
  ]);
  // 回収・取消済みの要求は、事前SELECTが古くても確定成功として扱わない。
  if (results.some((result) => Number(result.meta.changes ?? 0) !== 1)) {
    throw new Error("message_request_not_committable");
  }
}

export async function commitConversationTurn(db: D1Database, input: CommitConversationTurnInput): Promise<void> {
  const request = await getMessageRequest(db, input.clientMessageId);
  const routeEpoch = input.routeEpoch ?? 0;
  if (
    !request ||
    request.status !== "provider_started" ||
    request.travel_session_id !== input.travelSessionId ||
    request.persona_id !== input.personaId ||
    input.assistantSequenceNo !== input.userSequenceNo + 1 ||
    request.provider !== input.provider ||
    request.model_requested !== input.modelRequested ||
    request.route_epoch !== routeEpoch ||
    (request.credential_profile_id !== null && request.credential_profile_id !== input.credentialProfileId)
  ) {
    throw new Error("message_request_not_committable");
  }
  const results = await db.batch([
    db
      .prepare(
        `INSERT INTO travel_events
          (event_id, travel_session_id, persona_id, sequence_no, type, created_at, content, content_hash,
           route_epoch, status)
         SELECT ?, ?, ?, ?, 'user_message', ?, ?, ?, ?, 'committed'
         WHERE EXISTS (
           SELECT 1 FROM message_requests WHERE client_message_id = ? AND status = 'provider_started'
         )`,
      )
      .bind(
        input.userEventId,
        input.travelSessionId,
        input.personaId,
        input.userSequenceNo,
        input.createdAt,
        input.userContent,
        input.userContentHash,
        routeEpoch,
        input.clientMessageId,
      ),
    db
      .prepare(
        `INSERT INTO travel_events
          (event_id, travel_session_id, persona_id, sequence_no, type, created_at, content, content_hash,
           provider, model_requested, model_resolved, route_epoch, reply_to_event_id, status)
         SELECT ?, ?, ?, ?, 'assistant_message', ?, ?, ?, ?, ?, ?, ?, ?, 'committed'
         WHERE EXISTS (
           SELECT 1 FROM message_requests WHERE client_message_id = ? AND status = 'provider_started'
         )`,
      )
      .bind(
        input.eventId,
        input.travelSessionId,
        input.personaId,
        input.assistantSequenceNo,
        input.createdAt,
        input.content,
        input.contentHash,
        input.provider,
        input.modelRequested,
        input.modelResolved,
        routeEpoch,
        input.userEventId,
        input.clientMessageId,
      ),
    db
      .prepare(
        `INSERT INTO usage_receipts
          (receipt_id, event_id, travel_session_id, persona_id, occurred_at, provider, gateway,
           credential_profile_id, model_requested, model_resolved, upstream_provider, route_epoch, input_tokens,
           output_tokens, cache_read_tokens, cache_creation_tokens, provider_reported_cost_usd,
           usage_status, pricing_version, cost_basis, estimate_status, input_cost_usd, output_cost_usd,
           cache_read_cost_usd, cache_creation_cost_usd, cache_storage_cost_usd, estimated_cost_usd,
           estimated_savings_usd, unknown_reason, cache_status, cache_ttl_seconds,
           cache_creation_5m_tokens, cache_creation_1h_tokens, signature)
         SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
         WHERE EXISTS (
           SELECT 1 FROM message_requests WHERE client_message_id = ? AND status = 'provider_started'
         )`,
      )
      .bind(
        input.receiptId,
        input.eventId,
        input.travelSessionId,
        input.personaId,
        input.createdAt,
        input.provider,
        input.gateway,
        input.credentialProfileId,
        input.modelRequested,
        input.modelResolved,
        input.upstreamProvider,
        routeEpoch,
        input.usage.input_tokens,
        input.usage.output_tokens,
        input.usage.cache_read_tokens,
        input.usage.cache_creation_tokens,
        input.usage.provider_reported_cost_usd,
        input.usage.usage_status,
        input.pricing.pricing_version,
        input.pricing.cost_basis,
        input.pricing.estimate_status,
        input.pricing.input_cost_usd,
        input.pricing.output_cost_usd,
        input.pricing.cache_read_cost_usd,
        input.pricing.cache_creation_cost_usd,
        input.pricing.cache_storage_cost_usd,
        input.pricing.estimated_cost_usd,
        input.pricing.estimated_savings_usd,
        input.pricing.unknown_reason,
        input.usage.cache_status ?? "unreported",
        input.usage.cache_ttl_seconds ?? null,
        input.usage.cache_creation_5m_tokens ?? null,
        input.usage.cache_creation_1h_tokens ?? null,
        input.signature,
        input.clientMessageId,
      ),
    db
      .prepare(
        `UPDATE message_requests
         SET status = 'completed', event_id = ?, receipt_id = ?, finalized_at = ?,
             budget_state = CASE WHEN budget_reserved_usd IS NULL THEN NULL ELSE ? END,
             budget_settled_usd = ?, budget_settled_at = ?
         WHERE client_message_id = ? AND status = 'provider_started'`,
      )
      .bind(
        input.eventId,
        input.receiptId,
        input.createdAt,
        input.pricing.estimated_cost_usd === null ? "held" : "settled",
        input.pricing.estimated_cost_usd,
        input.createdAt,
        input.clientMessageId,
      ),
  ]);
  // 回収・取消済みの要求は、事前SELECTが古くても確定成功として扱わない。
  if (results.some((result) => Number(result.meta.changes ?? 0) !== 1)) {
    throw new Error("message_request_not_committable");
  }
}

export async function nextSequenceNumber(
  db: D1Database,
  travelSessionId: string,
  personaId: string,
): Promise<number> {
  const row = await db
    .prepare(
      `SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
       FROM travel_events WHERE travel_session_id = ? AND persona_id = ?`,
    )
    .bind(travelSessionId, personaId)
    .first<{ next_sequence: number }>();
  return Number(row?.next_sequence ?? 1);
}

export async function getEventsAfterCursor(
  db: D1Database,
  travelSessionId: string,
  personaId: string,
  afterSequence: number,
  limit = 100,
): Promise<EventPage> {
  const result = await db
    .prepare(
      `SELECT * FROM travel_events
       WHERE travel_session_id = ? AND persona_id = ? AND sequence_no > ?
       ORDER BY sequence_no ASC, event_id ASC LIMIT ?`,
    )
    .bind(travelSessionId, personaId, Math.max(0, afterSequence), Math.min(Math.max(1, limit), 500))
    .all<TravelEventRow>();
  const events = result.results;
  const missing: number[] = [];
  let expected = Math.max(0, afterSequence) + 1;
  for (const event of events) {
    while (expected < event.sequence_no) {
      missing.push(expected);
      expected += 1;
    }
    expected = event.sequence_no + 1;
  }
  return {
    events,
    next_cursor: events.at(-1)?.sequence_no ?? Math.max(0, afterSequence),
    missing_sequences: missing,
  };
}

export async function deleteSessionContent(db: D1Database, travelSessionId: string, deletedAt: string): Promise<void> {
  await db.batch([
    db
      .prepare(
        `UPDATE travel_events
         SET content = NULL, content_deleted_at = COALESCE(content_deleted_at, ?)
         WHERE travel_session_id = ? AND content IS NOT NULL`,
      )
      .bind(deletedAt, travelSessionId),
    db
      .prepare(
        `UPDATE travel_sessions
         SET content_deleted_at = COALESCE(content_deleted_at, ?)
         WHERE travel_session_id = ?`,
      )
      .bind(deletedAt, travelSessionId),
  ]);
}

export async function recoverAmbiguousRequests(db: D1Database, before: string, finalizedAt: string): Promise<number> {
  const result = await db
    .prepare(
      `UPDATE message_requests
       SET status = 'outcome_unknown', finalized_at = ?,
           budget_state = CASE WHEN budget_state = 'reserved' THEN 'held' ELSE budget_state END
       WHERE status = 'provider_started' AND julianday(COALESCE(provider_started_at, reserved_at)) < julianday(?)`,
    )
    .bind(finalizedAt, before)
    .run();
  return Number(result.meta.changes ?? 0);
}

// 状態と予約額を同じUPDATEで遷移させ、開始権・確定batchとの競合をD1上で直列化する。
export async function recoverStaleRequests(db: D1Database, now: string): Promise<number> {
  const nowMs = Date.parse(now);
  if (!Number.isFinite(nowMs)) throw new Error("invalid_request_expiry_time");
  const cutoff = new Date(nowMs - REQUEST_EXPIRY_MS).toISOString();
  const result = await db.prepare(
    `UPDATE message_requests
     SET status = CASE WHEN status = 'reserved' AND provider_started_at IS NULL THEN 'failed_known' ELSE 'outcome_unknown' END,
         finalized_at = ?,
         budget_settled_usd = CASE WHEN status = 'reserved' AND provider_started_at IS NULL
           AND budget_state IN ('reserved', 'unknown_allowed') THEN 0 ELSE budget_settled_usd END,
         budget_settled_at = CASE WHEN status = 'reserved' AND provider_started_at IS NULL
           AND budget_state IN ('reserved', 'unknown_allowed') THEN ? ELSE budget_settled_at END,
         budget_state = CASE
           WHEN status = 'reserved' AND provider_started_at IS NULL AND budget_state IN ('reserved', 'unknown_allowed') THEN 'released'
           WHEN budget_state = 'reserved' THEN 'held'
           ELSE budget_state END
     WHERE status IN ('reserved', 'provider_started')
       AND julianday(COALESCE(provider_started_at, reserved_at)) <= julianday(?)`
  ).bind(now, now, cutoff).run();
  return Number(result.meta.changes ?? 0);
}
