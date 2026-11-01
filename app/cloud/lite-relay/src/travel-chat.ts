import { createProviderDeadline, PROVIDER_TOTAL_TIMEOUT_MS } from "./request-deadline";
import { normalizeProviderStream } from "./adapters";
import { canonicalJson, hmacSha256, sha256 } from "./auth";
import {
  getPersonaRoute,
  requireEnabledProfile,
  type PersonaRoute,
  type ProviderProfileRow,
} from "./phase2-routing";
import { BudgetLimitError, getUsageSummary, holdBudget, releaseBudget, reserveBudget } from "./budget";
import { buildProviderRequest, type CanonicalMessage } from "./provider-requests";
import { ensureProviderCache, type ProviderCacheContext } from "./provider-cache";
import { estimateUsageCost } from "./pricing-catalog";
import { fetchProviderWithoutRedirect } from "./security";
import { parseJsonRecord, parseSseText } from "./sse";
import {
  commitConversationTurn,
  getActiveMessageRequest,
  getMessageRequest,
  markProviderStarted,
  markRequestTerminal,
  nextSequenceNumber,
  recordProviderHttpFailure,
  recordProviderTerminalDiagnostics,
  reserveMessage,
} from "./storage";
import type { CanonicalStreamEvent, ErrorCategory, Provider } from "./types";

interface ChatInput {
  client_message_id: string;
  message: string;
  persona_id: string | null;
}

export interface StreamTravelLifecycle {
  signal?: AbortSignal;
  waitUntil?: (promise: Promise<unknown>) => void;
}

interface SessionContext extends PersonaRoute {
  snapshot_json: string;
  snapshot_hash: string;
  snapshot_schema_version: number;
}

const PROVIDER_NAMES: Record<Provider, string> = {
  gemini: "Gemini",
  openai: "OpenAI",
  anthropic: "Anthropic",
  xai: "xAI",
  openrouter: "OpenRouter",
};

function providerHttpFailure(
  provider: Provider,
  status: number,
): { error: string; status: number; safe_message_ja: string; requestMayBeBilled: boolean } {
  const name = PROVIDER_NAMES[provider];
  if (status === 429) {
    return {
      error: "provider_rate_limited",
      status: 429,
      safe_message_ja: `${name}の一時的な利用上限に達しました。時間を置き、同じ内容を自動再送しないでください。`,
      requestMayBeBilled: false,
    };
  }
  if (status === 401 || status === 403) {
    return {
      error: "provider_auth_failed",
      status: 502,
      safe_message_ja: `${name}の認証を確認できませんでした。本体側で設定を確認してください。`,
      requestMayBeBilled: false,
    };
  }
  if (status === 404) {
    return {
      error: "provider_model_unavailable",
      status: 409,
      safe_message_ja: `${name}で選択したモデルを利用できません。モデル設定を確認してください。`,
      requestMayBeBilled: false,
    };
  }
  if (status === 400 || status === 422) {
    return {
      error: "provider_rejected_request",
      status: 422,
      safe_message_ja: `${name}が要求を受理しませんでした。内容または持ち出し設定を確認してください。`,
      requestMayBeBilled: false,
    };
  }
  if (status >= 500) {
    return {
      error: "provider_unavailable",
      status: 503,
      safe_message_ja: `${name}が一時的に利用できません。同じ内容を自動再送せず、時間を置いてください。`,
      requestMayBeBilled: true,
    };
  }
  return {
    error: "provider_rejected_request",
    status: 502,
    safe_message_ja: `${name}が要求を確定応答として受理しませんでした。`,
    requestMayBeBilled: true,
  };
}

function terminalDiagnostics(provider: Provider, raw: string): { code: string | null; recordCount: number } {
  const records = parseSseText(raw);
  let code: string | null = null;
  for (const record of records) {
    if (record.data === "[DONE]") {
      code = "DONE";
      continue;
    }
    const parsed = parseJsonRecord(record);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) continue;
    const payload = parsed as Record<string, unknown>;
    if (provider === "gemini") {
      const feedback = payload.promptFeedback;
      if (feedback && typeof feedback === "object" && !Array.isArray(feedback)) {
        const reason = (feedback as Record<string, unknown>).blockReason;
        if (typeof reason === "string" && reason) code = `prompt_${reason}`;
      }
      if (Array.isArray(payload.candidates)) {
        for (const candidate of payload.candidates) {
          if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) continue;
          const reason = (candidate as Record<string, unknown>).finishReason;
          if (typeof reason === "string" && reason) code = reason;
        }
      }
    } else if (provider === "openai" && typeof payload.type === "string") {
      if (["response.completed", "response.failed", "error"].includes(payload.type)) code = payload.type;
    } else if (provider === "anthropic" && payload.type === "message_stop") {
      code = "message_stop";
    } else if (provider === "xai" || provider === "openrouter") {
      if (!Array.isArray(payload.choices)) continue;
      for (const choice of payload.choices) {
        if (!choice || typeof choice !== "object" || Array.isArray(choice)) continue;
        const reason = (choice as Record<string, unknown>).finish_reason;
        if (typeof reason === "string" && reason) code = reason;
      }
    }
  }
  return { code, recordCount: records.length };
}

function sse(event: CanonicalStreamEvent | Record<string, unknown>): Uint8Array {
  return new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`);
}

function safeError(category: ErrorCategory, message: string): CanonicalStreamEvent {
  return {
    schema_version: 1,
    type: "response.error",
    error: {
      category,
      http_status: null,
      provider_code: null,
      retryable: false,
      retry_after_seconds: null,
      request_may_be_billed: category === "persistence_failed" || category === "stream_interrupted",
      safe_message_ja: message,
    },
  };
}

function parseInput(value: unknown): ChatInput {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_message_request");
  const raw = value as Record<string, unknown>;
  const clientMessageId = typeof raw.client_message_id === "string" ? raw.client_message_id.trim() : "";
  const message = typeof raw.message === "string" ? raw.message.trim() : "";
  if (!/^[A-Za-z0-9_-]{8,100}$/.test(clientMessageId) || !message || message.length > 20_000) {
    throw new Error("invalid_message_request");
  }
  const personaId = typeof raw.persona_id === "string" ? raw.persona_id.trim() : null;
  if (personaId !== null && (!personaId || personaId.length > 200)) throw new Error("invalid_message_request");
  return { client_message_id: clientMessageId, message, persona_id: personaId };
}

async function sessionContext(db: D1Database, sessionId: string, requestedPersonaId: string | null): Promise<SessionContext | null> {
  return db
    .prepare(
      `SELECT r.travel_session_id, r.persona_id, r.credential_profile_id, r.provider,
              r.model_id, r.route_epoch, r.changed_at, p.snapshot_json, p.snapshot_hash
              , s.snapshot_schema_version
       FROM travel_sessions s
       JOIN persona_routes r ON r.travel_session_id = s.travel_session_id
       JOIN persona_snapshots p
         ON p.travel_session_id = r.travel_session_id AND p.persona_id = r.persona_id
       LEFT JOIN travel_personas tp
         ON tp.travel_session_id = r.travel_session_id AND tp.persona_id = r.persona_id
       WHERE s.travel_session_id = ? AND s.status = 'active'
         AND r.persona_id = COALESCE(?, s.persona_id)
         AND (s.snapshot_schema_version < 4 OR tp.status = 'active')`,
    )
    .bind(sessionId, requestedPersonaId)
    .first<SessionContext>();
}

async function historyFor(db: D1Database, session: SessionContext): Promise<CanonicalMessage[]> {
  const rows = await db
    .prepare(
      `SELECT type, content FROM (
         SELECT sequence_no, type, content FROM travel_events
         WHERE travel_session_id = ? AND persona_id = ? AND status = 'committed'
           AND type IN ('user_message', 'assistant_message') AND content IS NOT NULL
         ORDER BY sequence_no DESC LIMIT 100
       )
       ORDER BY sequence_no ASC`,
    )
    .bind(session.travel_session_id, session.persona_id)
    .all<{ type: string; content: string }>();
  return rows.results.map((row) => ({
    role: row.type === "assistant_message" ? "assistant" : "user",
    content: row.content,
  }));
}

function sameRoute(left: PersonaRoute | null, right: PersonaRoute): boolean {
  return Boolean(
    left &&
      left.route_epoch === right.route_epoch &&
      left.credential_profile_id === right.credential_profile_id &&
      left.provider === right.provider &&
      left.model_id === right.model_id,
  );
}

export async function streamTravelMessage(
  db: D1Database,
  env: Env,
  sessionId: string,
  rawInput: unknown,
  fetcher: typeof fetch = fetch,
  clock: () => Date = () => new Date(),
  providerTimeoutMs = PROVIDER_TOTAL_TIMEOUT_MS,
  lifecycle: StreamTravelLifecycle = {},
): Promise<Response> {
  const input = parseInput(rawInput);
  const session = await sessionContext(db, sessionId, input.persona_id);
  if (!session) return Response.json({ error: "travel_session_not_active" }, { status: 409 });
  if (!env.BUNDLE_SIGNING_KEY) return Response.json({ error: "signing_key_unavailable" }, { status: 503 });

  let profile: ProviderProfileRow;
  let snapshot: Record<string, unknown>;
  let committedHistory: CanonicalMessage[];
  let provisionalRequest;
  try {
    profile = await requireEnabledProfile(db, session.credential_profile_id);
    snapshot = JSON.parse(session.snapshot_json) as Record<string, unknown>;
    committedHistory = await historyFor(db, session);
    provisionalRequest = buildProviderRequest(
      env,
      profile,
      session,
      snapshot,
      committedHistory,
      input.message,
    );
  } catch (error) {
    const code = error instanceof Error ? error.message : "provider_profile_unavailable";
    return Response.json({ error: code }, { status: code === "secret_binding_unavailable" ? 503 : 409 });
  }

  const reservedAt = clock().toISOString();
  let reservation;
  try {
    reservation = await reserveMessage(db, {
      clientMessageId: input.client_message_id,
      travelSessionId: sessionId,
      personaId: session.persona_id,
      provider: session.provider,
      credentialProfileId: session.credential_profile_id,
      modelRequested: session.model_id,
      routeEpoch: session.route_epoch,
      reservedAt,
    });
  } catch (error) {
    if (await getActiveMessageRequest(db, sessionId)) {
      return Response.json(
        { error: "session_message_in_progress" },
        { status: 409, headers: { "Cache-Control": "no-store" } },
      );
    }
    if (error instanceof Error && error.message === "travel_session_not_active") {
      return Response.json({ error: "travel_session_not_active" }, { status: 409 });
    }
    throw error;
  }
  if (!reservation.created) {
    return Response.json(
      { error: "message_already_reserved", status: reservation.request.status },
      { status: 409, headers: { "Cache-Control": "no-store" } },
    );
  }

  const requestBytes = new TextEncoder().encode(String(provisionalRequest.init.body ?? "")).byteLength;
  try {
    await reserveBudget(db, {
      clientMessageId: input.client_message_id,
      travelSessionId: sessionId,
      provider: session.provider,
      model: session.model_id,
      inputTokenUpperBound: requestBytes,
      nowIso: reservedAt,
      ...(session.snapshot_schema_version === 4 ? { personaId: session.persona_id } : {}),
    });
  } catch (error) {
    await markRequestTerminal(db, input.client_message_id, "failed_known", clock().toISOString());
    const code = error instanceof Error ? error.message : "budget_reservation_failed";
    return Response.json(
      {
        error: code,
        ...(error instanceof BudgetLimitError ? { next_request_reserved_usd: error.nextRequestReservedUsd } : {}),
        safe_message_ja: code === "budget_limit_exceeded"
          ? "この送信は設定した概算予算を超えるため、プロバイダへ送信しませんでした。"
          : code === "budget_unknown_price_blocked"
            ? "選択モデルの料金を安全に確認できないため、プロバイダへ送信しませんでした。"
            : "予算を安全に予約できなかったため、プロバイダへ送信しませんでした。",
      },
      { status: 409, headers: { "Cache-Control": "no-store" } },
    );
  }

  if (!sameRoute(await getPersonaRoute(db, sessionId, session.persona_id), session)) {
    await releaseBudget(db, input.client_message_id, clock().toISOString());
    await markRequestTerminal(db, input.client_message_id, "failed_known", clock().toISOString());
    return Response.json({ error: "route_changed_before_provider_start" }, { status: 409 });
  }
  if (!(await markProviderStarted(db, input.client_message_id, clock().toISOString()))) {
    return Response.json({ error: "message_start_conflict" }, { status: 409 });
  }

  const deadline = createProviderDeadline(fetcher, providerTimeoutMs, lifecycle.signal);
  let terminalCleanup: Promise<void> | null = null;
  let cleanupScheduled = false;
  let removeLifecycleAbortListener: (() => void) | null = null;
  const settleOutcomeUnknown = (): Promise<void> => {
    if (terminalCleanup) return terminalCleanup;
    terminalCleanup = (async () => {
      try {
        const current = await getMessageRequest(db, input.client_message_id);
        if (!current || (current.status !== "reserved" && current.status !== "provider_started")) return;
        await markRequestTerminal(db, input.client_message_id, "outcome_unknown", clock().toISOString());
        const finalized = await getMessageRequest(db, input.client_message_id);
        if (finalized?.status === "outcome_unknown") {
          // statusを先に結果不明へ遷移させ、既知失敗／completedを後からheldへ巻き戻さない。
          await holdBudget(db, input.client_message_id);
        }
      } catch {
        // D1障害時はCron回収へ委ね、abort経路から未処理rejectionを出さない。
      }
    })();
    return terminalCleanup;
  };
  const scheduleOutcomeUnknown = (): Promise<void> => {
    const cleanup = settleOutcomeUnknown();
    if (lifecycle.waitUntil && !cleanupScheduled) {
      cleanupScheduled = true;
      try {
        lifecycle.waitUntil(cleanup.catch(() => undefined));
      } catch {
        // waitUntilが利用できないテスト／終了間際でも同期cleanupは継続する。
      }
    }
    return cleanup;
  };
  const removeAbortListener = () => {
    removeLifecycleAbortListener?.();
    removeLifecycleAbortListener = null;
  };
  const outcomeUnknownResponse = async (): Promise<Response> => {
    deadline.stop();
    deadline.abort();
    removeAbortListener();
    await scheduleOutcomeUnknown();
    return Response.json({ error: "provider_outcome_unknown" }, { status: 502 });
  };
  const onClientAbort = () => {
    deadline.stop();
    deadline.abort();
    void scheduleOutcomeUnknown().catch(() => undefined);
  };
  if (lifecycle.signal) {
    const listener = () => onClientAbort();
    lifecycle.signal.addEventListener("abort", listener, { once: true });
    removeLifecycleAbortListener = () => lifecycle.signal?.removeEventListener("abort", listener);
    if (lifecycle.signal.aborted) onClientAbort();
  }
  let cacheContext: ProviderCacheContext | null = null;
  if (deadline.aborted()) return await outcomeUnknownResponse();
  const cachePreparation = ensureProviderCache(
    db,
    env,
    profile,
    session,
    snapshot,
    session.snapshot_hash,
    deadline.fetch,
    clock(),
  );
  try {
    cacheContext = await deadline.wait(cachePreparation);
  } catch {
    void cachePreparation.catch(() => undefined);
    if (deadline.aborted()) return await outcomeUnknownResponse();
    cacheContext = null;
  }
  if (deadline.aborted()) return await outcomeUnknownResponse();
  let providerResponse: Response;
  try {
    const providerRequest = buildProviderRequest(
      env,
      profile,
      session,
      snapshot,
      committedHistory,
      input.message,
      cacheContext,
    );

    providerResponse = await fetchProviderWithoutRedirect(
      providerRequest.provider,
      providerRequest.url,
      providerRequest.init,
      deadline.fetch,
    );
  } catch {
    return await outcomeUnknownResponse();
  }
  // providerのResponseが遅れて到着しても、先行したclient abortを既知失敗へ確定しない。
  if (deadline.aborted()) return await outcomeUnknownResponse();
  if (!providerResponse.ok || !providerResponse.body) {
    deadline.stop();
    deadline.abort();
    removeAbortListener();
    const failure = providerHttpFailure(session.provider, providerResponse.status);
    await recordProviderHttpFailure(db, input.client_message_id, providerResponse.status, failure.error);
    if (failure.requestMayBeBilled) await holdBudget(db, input.client_message_id);
    else await releaseBudget(db, input.client_message_id, clock().toISOString());
    await markRequestTerminal(db, input.client_message_id, "failed_known", clock().toISOString());
    return Response.json(
      { error: failure.error, safe_message_ja: failure.safe_message_ja },
      { status: failure.status, headers: { "Cache-Control": "no-store" } },
    );
  }

  let providerReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
      try {
        reader = providerResponse.body!.getReader();
        providerReader = reader;
        if (cancelled) throw new Error("travel_client_disconnected");
        const decoder = new TextDecoder();
        let raw = "";
        let buffer = "";
        let started = false;
        const emitRecord = (record: string) => {
          const normalized = normalizeProviderStream(session.provider, record + "\n\n", session.model_id);
          for (const event of normalized.events) {
            if (event.type === "response.started") {
              if (started) continue;
              started = true;
            }
            if (["response.started", "response.text.delta", "response.usage"].includes(event.type)) {
              controller.enqueue(sse(event));
            }
          }
        };
        while (true) {
          const chunk = await deadline.read(reader!);
          if (chunk.done) break;
          const text = decoder.decode(chunk.value, { stream: true });
          raw += text;
          buffer += text;
          const records = buffer.split(/\r?\n\r?\n/);
          buffer = records.pop() ?? "";
          for (const record of records) emitRecord(record);
        }
        const tail = decoder.decode();
        raw += tail;
        buffer += tail;
        // 正本の全文解析が受理するEOF直前のrecordも、画面へ一度だけ送る。
        if (buffer.trim()) emitRecord(buffer);
        const normalized = normalizeProviderStream(session.provider, raw, session.model_id);
        const terminal = terminalDiagnostics(session.provider, raw);
        await recordProviderTerminalDiagnostics(db, input.client_message_id, {
          terminalCode: terminal.code,
          streamRecordCount: terminal.recordCount,
          textChars: normalized.text.length,
          usageStatus: normalized.usage.usage_status,
          unknownEventCount: normalized.unknown_event_count,
        });

        if (normalized.routing_violation) {
          await holdBudget(db, input.client_message_id);
          await markRequestTerminal(db, input.client_message_id, "failed_known", clock().toISOString());
          controller.enqueue(
            sse(safeError("provider_error", "OpenRouterが禁止されたフォールバックを報告したため、応答を保存しませんでした。")),
          );
          controller.close();
          return;
        }
        if (normalized.terminal_status === "failed_known") {
          const knownError = normalized.events.find((event) => event.type === "response.error");
          // 本文や生の上流message/codeではなく、正規化された失敗分類だけを記録する。
          const rawStatus = knownError?.type === "response.error" ? knownError.error.http_status : null;
          const httpStatus = typeof rawStatus === "number" && Number.isInteger(rawStatus)
            && rawStatus >= 400 && rawStatus <= 599 ? rawStatus : null;
          await recordProviderHttpFailure(
            db, input.client_message_id, httpStatus,
            knownError?.type === "response.error" ? knownError.error.category : "provider_error",
          );
          await holdBudget(db, input.client_message_id);
          await markRequestTerminal(db, input.client_message_id, "failed_known", clock().toISOString());
          controller.enqueue(
            sse(
              knownError ??
                safeError("provider_error", `${PROVIDER_NAMES[session.provider]}が要求を確定応答として処理できませんでした。`),
            ),
          );
          controller.close();
          return;
        }
        if (normalized.terminal_status !== "completed" || !normalized.text) {
          await holdBudget(db, input.client_message_id);
          await markRequestTerminal(db, input.client_message_id, "partial", clock().toISOString());
          const partial = normalized.events.find((event) => event.type === "response.partial");
          const outputLimitReached = partial?.type === "response.partial" && partial.reason === "max_output_tokens";
          controller.enqueue(sse(safeError(
            outputLimitReached ? "output_limit" : "stream_interrupted",
            outputLimitReached
              ? "回答が選択中のモデルの最大長に達しました。必要な場合は続けて質問してください。"
              : "応答が途中で切断されました。自動再送はしません。",
          )));
          controller.close();
          return;
        }

        deadline.assertActive();
        const committedAt = clock().toISOString();
        const userSequenceNo = await nextSequenceNumber(db, sessionId, session.persona_id);
        const userEventId = crypto.randomUUID();
        const assistantEventId = crypto.randomUUID();
        const receiptId = crypto.randomUUID();
        const gateway = session.provider === "openrouter" ? "openrouter" : null;
        if (cacheContext) {
          normalized.usage.cache_ttl_seconds = cacheContext.ttlSeconds;
          if (cacheContext.justCreated && cacheContext.cacheCreationTokens !== null) {
            normalized.usage.cache_creation_tokens = cacheContext.cacheCreationTokens;
            normalized.usage.cache_status = "created";
          }
        } else if (session.provider === "gemini" && snapshot.cache_policy === "gemini_explicit") {
          normalized.usage.cache_status = "unavailable";
        }
        const pricing = estimateUsageCost(
          session.provider,
          normalized.model_resolved || session.model_id,
          normalized.usage,
          {
            cacheTtlSeconds: normalized.usage.cache_ttl_seconds ?? null,
            cacheStorageTokenHours: cacheContext?.justCreated && cacheContext.cacheCreationTokens !== null
              ? cacheContext.cacheCreationTokens * cacheContext.ttlSeconds / 3600
              : null,
            asOf: new Date(committedAt),
          },
        );
        const receiptPayload = {
          receipt_id: receiptId,
          event_id: assistantEventId,
          travel_session_id: sessionId,
          persona_id: session.persona_id,
          occurred_at: committedAt,
          route_epoch: session.route_epoch,
          credential_profile_id: session.credential_profile_id,
          provider: session.provider,
          gateway,
          model_requested: session.model_id,
          model_resolved: normalized.model_resolved,
          upstream_provider: normalized.upstream_provider,
          usage: normalized.usage,
          pricing,
        };
        const userContentHash = await sha256(input.message);
        const contentHash = await sha256(normalized.text);
        const signature = await hmacSha256(canonicalJson(receiptPayload), env.BUNDLE_SIGNING_KEY!);
        deadline.assertActive();
        await commitConversationTurn(db, {
          clientMessageId: input.client_message_id,
          userEventId,
          userSequenceNo,
          userContent: input.message,
          userContentHash,
          assistantSequenceNo: userSequenceNo + 1,
          eventId: assistantEventId,
          receiptId,
          travelSessionId: sessionId,
          personaId: session.persona_id,
          sequenceNo: userSequenceNo + 1,
          createdAt: committedAt,
          content: normalized.text,
          contentHash,
          provider: session.provider,
          routeEpoch: session.route_epoch,
          gateway,
          credentialProfileId: session.credential_profile_id,
          modelRequested: session.model_id,
          modelResolved: normalized.model_resolved,
          upstreamProvider: normalized.upstream_provider,
          usage: normalized.usage,
          pricing,
          signature,
        });
        const committed = normalized.events.find((event) => event.type === "response.committed");
        if (committed) {
          const usageSummary = await getUsageSummary(db, sessionId, committedAt);
          controller.enqueue(
            sse({
              ...committed,
              provider: session.provider,
              gateway,
              credential_profile_id: session.credential_profile_id,
              model_requested: session.model_id,
              route_epoch: session.route_epoch,
              usage_summary: usageSummary,
            }),
          );
        }
        controller.close();
      } catch {
        await scheduleOutcomeUnknown();
        if (cancelled) return;
        controller.enqueue(sse(deadline.expired()
          ? safeError("stream_interrupted", "応答が時間内に完了しませんでした。結果と利用額は未確定として保留しています。自動再送はしません。")
          : deadline.aborted()
            ? safeError("stream_interrupted", "クライアント接続が終了したため、応答の結果と利用額は未確定として保留しています。")
          : safeError("persistence_failed", "応答の確定状態を確認できません。自動再送はしません。")));
        controller.close();
      } finally {
        deadline.stop();
        deadline.abort();
        removeAbortListener();
        if (reader) void reader.cancel().catch(() => {});
      }
    },
    async cancel() {
      cancelled = true;
      deadline.stop();
      deadline.abort();
      removeAbortListener();
      if (providerReader) await providerReader.cancel("travel_client_disconnected").catch(() => {});
      await scheduleOutcomeUnknown();
    },
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-store",
      Connection: "keep-alive",
    },
  });
}

// Phase 1 callers keep this name while the implementation follows the persisted Phase 2 route.
export const streamGeminiMessage = streamTravelMessage;

export async function messageRequestStatus(db: D1Database, clientMessageId: string): Promise<Record<string, unknown> | null> {
  const request = await getMessageRequest(db, clientMessageId);
  if (!request) return null;
  return {
    client_message_id: request.client_message_id,
    travel_session_id: request.travel_session_id,
    persona_id: request.persona_id,
    status: request.status,
    event_id: request.event_id,
    reserved_at: request.reserved_at,
    finalized_at: request.finalized_at,
  };
}
