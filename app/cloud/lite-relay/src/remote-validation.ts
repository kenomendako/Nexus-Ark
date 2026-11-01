const RUN_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,47}$/;
const CREATED_AT = "2026-07-15T00:00:00.000Z";

async function sha256(value: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

export async function hasValidationToken(request: Request, expected: string | undefined): Promise<boolean> {
  if (!expected) return false;
  const authorization = request.headers.get("Authorization") ?? "";
  const supplied = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  const [actualDigest, expectedDigest] = await Promise.all([sha256(supplied), sha256(expected)]);
  let difference = 0;
  for (let index = 0; index < expectedDigest.length; index += 1) {
    difference |= actualDigest[index]! ^ expectedDigest[index]!;
  }
  return supplied.length > 0 && difference === 0;
}

export function syntheticSseResponse(): Response {
  const encoder = new TextEncoder();
  const frames = [
    'event: delta\ndata: {"type":"text_delta","text":"PHASE0_"}\n\n',
    'event: delta\ndata: {"type":"text_delta","text":"OK"}\n\n',
    'event: usage\ndata: {"input_tokens":1,"output_tokens":1,"usage_status":"synthetic"}\n\n',
    'event: done\ndata: {"status":"completed"}\n\n',
  ];
  let frameIndex = 0;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        if (frameIndex > 0) {
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
        const frame = frames[frameIndex];
        if (frame === undefined) {
          controller.close();
          return;
        }
        controller.enqueue(encoder.encode(frame));
        frameIndex += 1;
        if (frameIndex === frames.length) controller.close();
      } catch {
        // Client cancellation is an expected Phase 0 validation case.
      }
    },
  });
  return new Response(stream, {
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/event-stream; charset=utf-8",
      "X-Accel-Buffering": "no",
    },
  });
}

function idsFor(runId: string) {
  const prefix = `phase0-remote-${runId}`;
  return {
    sessionId: `${prefix}-session`,
    rollbackSessionId: `${prefix}-rollback`,
    requestId: `${prefix}-request`,
    eventId: `${prefix}-event-1`,
    gapEventId: `${prefix}-event-3`,
    receiptId: `${prefix}-receipt`,
  };
}

export function parseRunId(value: unknown): string {
  if (typeof value !== "object" || value === null || !("run_id" in value)) {
    throw new Error("invalid_run_id");
  }
  const runId = (value as { run_id: unknown }).run_id;
  if (typeof runId !== "string" || !RUN_ID_PATTERN.test(runId)) {
    throw new Error("invalid_run_id");
  }
  return runId;
}

export async function cleanupRemoteValidation(db: D1Database, runId: string): Promise<void> {
  const { sessionId, rollbackSessionId } = idsFor(runId);
  await db.batch([
    db.prepare("DELETE FROM usage_receipts WHERE travel_session_id = ?").bind(sessionId),
    db.prepare("DELETE FROM travel_events WHERE travel_session_id = ?").bind(sessionId),
    db.prepare("DELETE FROM message_requests WHERE travel_session_id = ?").bind(sessionId),
    db.prepare("DELETE FROM travel_sessions WHERE travel_session_id IN (?, ?)").bind(sessionId, rollbackSessionId),
  ]);
}

export async function validateRemoteD1(db: D1Database, runId: string) {
  const ids = idsFor(runId);
  await cleanupRemoteValidation(db, runId);

  await db.batch([
    db.prepare(
      "INSERT INTO travel_sessions (travel_session_id, status, retention_days, created_at) VALUES (?, 'active', 0, ?)",
    ).bind(ids.sessionId, CREATED_AT),
    db.prepare(
      "INSERT INTO message_requests (client_message_id, travel_session_id, persona_id, status, provider, model_requested, event_id, receipt_id, reserved_at, provider_started_at, finalized_at) VALUES (?, ?, 'phase0-persona', 'completed', 'synthetic', 'phase0-model', ?, ?, ?, ?, ?)",
    ).bind(ids.requestId, ids.sessionId, ids.eventId, ids.receiptId, CREATED_AT, CREATED_AT, CREATED_AT),
    db.prepare(
      "INSERT INTO travel_events (event_id, travel_session_id, persona_id, sequence_no, type, created_at, content, content_hash, provider, model_requested, model_resolved, status) VALUES (?, ?, 'phase0-persona', 1, 'assistant_message', ?, 'PHASE0_OK', 'synthetic-hash-1', 'synthetic', 'phase0-model', 'phase0-model', 'committed')",
    ).bind(ids.eventId, ids.sessionId, CREATED_AT),
    db.prepare(
      "INSERT INTO travel_events (event_id, travel_session_id, persona_id, sequence_no, type, created_at, content, content_hash, status) VALUES (?, ?, 'phase0-persona', 3, 'phase0_gap_probe', ?, NULL, 'synthetic-hash-3', 'committed')",
    ).bind(ids.gapEventId, ids.sessionId, CREATED_AT),
    db.prepare(
      "INSERT INTO usage_receipts (receipt_id, event_id, travel_session_id, persona_id, occurred_at, provider, credential_profile_id, model_requested, model_resolved, input_tokens, output_tokens, usage_status, signature) VALUES (?, ?, ?, 'phase0-persona', ?, 'synthetic', 'phase0-profile', 'phase0-model', 'phase0-model', 1, 1, 'reported', 'phase0-synthetic-signature')",
    ).bind(ids.receiptId, ids.eventId, ids.sessionId, CREATED_AT),
  ]);

  let rollbackErrorObserved = false;
  try {
    await db.batch([
      db.prepare(
        "INSERT INTO travel_sessions (travel_session_id, status, retention_days, created_at) VALUES (?, 'active', 0, ?)",
      ).bind(ids.rollbackSessionId, CREATED_AT),
      db.prepare(
        "INSERT INTO travel_sessions (travel_session_id, status, retention_days, created_at) VALUES (?, 'active', 0, ?)",
      ).bind(ids.rollbackSessionId, CREATED_AT),
    ]);
  } catch {
    rollbackErrorObserved = true;
  }

  const stored = await db
    .prepare(
      "SELECT (SELECT COUNT(*) FROM travel_events WHERE travel_session_id = ?) AS event_count, (SELECT COUNT(*) FROM usage_receipts WHERE travel_session_id = ?) AS receipt_count, (SELECT COUNT(*) FROM message_requests WHERE travel_session_id = ?) AS request_count, (SELECT COUNT(*) FROM travel_sessions WHERE travel_session_id = ?) AS rollback_count",
    )
    .bind(ids.sessionId, ids.sessionId, ids.sessionId, ids.rollbackSessionId)
    .first<{ event_count: number; receipt_count: number; request_count: number; rollback_count: number }>();
  const events = await db
    .prepare("SELECT sequence_no, content FROM travel_events WHERE travel_session_id = ? ORDER BY sequence_no")
    .bind(ids.sessionId)
    .all<{ sequence_no: number; content: string | null }>();
  const sequences = events.results.map((event) => event.sequence_no);

  return {
    ok:
      stored?.event_count === 2 &&
      stored.receipt_count === 1 &&
      stored.request_count === 1 &&
      rollbackErrorObserved &&
      stored.rollback_count === 0 &&
      sequences.join(",") === "1,3",
    atomic_commit: stored?.event_count === 2 && stored.receipt_count === 1 && stored.request_count === 1,
    rollback_error_observed: rollbackErrorObserved,
    rollback_row_count: stored?.rollback_count ?? -1,
    sequences,
    cursor_gap_detected: sequences.join(",") === "1,3",
    content: events.results[0]?.content ?? null,
  };
}
