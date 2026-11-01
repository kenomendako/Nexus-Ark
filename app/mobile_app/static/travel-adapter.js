// 接続確認・JSON読取の期限。AI生成のSSEには適用しない。
export const LITE_CONNECTION_TIMEOUT_MS = 5000;

export async function withConnectionTimeout(operation, {
  timeoutMs = LITE_CONNECTION_TIMEOUT_MS,
  signal,
} = {}) {
  const controller = new AbortController();
  let timer;
  let abort;
  const deadline = new Promise((_, reject) => {
    abort = () => {
      const error = new Error("通信を中止しました。");
      error.name = "AbortError";
      reject(error);
      controller.abort();
    };
    if (signal?.aborted) {
      abort();
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    timer = globalThis.setTimeout(() => {
      const error = new Error("接続確認が時間内に完了しませんでした。");
      error.name = "TimeoutError";
      reject(error);
      controller.abort();
    }, timeoutMs);
  });
  try {
    return await Promise.race([
      deadline,
      Promise.resolve().then(() => {
        controller.signal.throwIfAborted();
        return operation(controller.signal);
      }),
    ]);
  } finally {
    globalThis.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

const KEYS = {
  base: "nexusLite.travel.apiBase",
  access: "nexusLite.travel.device.accessToken",
  refresh: "nexusLite.travel.device.refreshToken",
  deviceId: "nexusLite.travel.device.id",
  sessionId: "nexusLite.travel.sessionId",
  personaId: "nexusLite.travel.personaId",
  draft: "nexusLite.travel.draft",
  pending: "nexusLite.travel.pendingMessage",
  pendingArchive: "nexusLite.travel.pendingArchive",
  lastSendFailure: "nexusLite.travel.lastSendFailure",
  lastFailedMessage: "nexusLite.travel.lastFailedMessage",
};
const SUPPORTED_API_SCHEMA_VERSION = 10;
const REQUIRED_D1_SCHEMA_VERSION = 10;
const FINAL_PENDING_STATUSES = new Set(["completed", "partial", "failed_known"]);

function inspectPendingMessage() {
  const raw = localStorage.getItem(KEYS.pending);
  if (raw === null) return { state: "none", pending: null, raw: null };
  try {
    const pending = JSON.parse(raw);
    if (!pending || typeof pending !== "object" || Array.isArray(pending) || typeof pending.client_message_id !== "string" || !pending.client_message_id) {
      return { state: "corrupt", pending: null, raw };
    }
    return { state: "pending", pending, raw };
  } catch {
    return { state: "corrupt", pending: null, raw };
  }
}

function safeBrowserError(error) {
  return {
    name: String(error?.name || "Error").slice(0, 40),
    message: String(error?.message || "").replace(/[\r\n]+/g, " ").slice(0, 160),
  };
}

export class TravelAdapterError extends Error {
  constructor(message, code, status = 0) {
    super(message);
    this.name = "TravelAdapterError";
    this.code = code;
    this.status = status;
  }
}

// Workerが正規化した分類だけを表示・保存する。本文や上流のエラー文は取り込まない。
const SEND_FAILURE_MESSAGES = {
  provider_auth_failed: "AIサービスの認証を確認できませんでした。PC本体のLite用APIキー設定を確認してください。",
  provider_rate_limited: "AIサービスの利用上限に達しました。時間を置いてから状態を確認してください。",
  provider_model_unavailable: "AIサービスで選択したモデルを利用できません。チャットの「今回使うAI」でモデル設定を確認してください。",
  provider_rejected_request: "AIサービスが送信内容またはお出かけ設定を受け付けませんでした。",
  provider_unavailable: "AIサービスが一時的に利用できません。時間を置いてから状態を確認してください。",
  provider_error: "AIサービスで応答を完了できませんでした。",
  budget_limit_exceeded: "設定した概算予算を超えるため、AIサービスへ送信しませんでした。",
  persona_budget_limit_exceeded: "このペルソナの概算予算を超えるため、AIサービスへ送信しませんでした。",
  budget_unknown_price_blocked: "選択したモデルの料金を確認できないため、AIサービスへ送信しませんでした。",
  stream_interrupted: "応答が途中で切断されたか、時間内に完了しませんでした。送信結果を確認してください。",
  output_limit: "回答が最大長に達し、完了した会話として保存されませんでした。",
  persistence_failed: "応答の保存完了を確認できません。送信結果を確認してください。",
  worker_unreachable: "Lite用クラウドへ接続できません。送信結果を確認してください。",
  provider_outcome_unknown: "AIサービスの送信結果を確認できません。前回の送信を確認してください。",
  secret_binding_unavailable: "Lite用APIキーをクラウドで確認できません。PC本体のLite用APIキー設定を確認してください。",
  provider_profile_unavailable: "Lite用AIサービスの接続設定を確認できません。",
  provider_profile_disabled: "選択中のLite用AIサービスが利用停止中です。",
  credential_profile_unavailable: "選択中のLite用AIサービスの接続設定を確認できないか、利用停止中です。",
  signing_key_unavailable: "Lite用クラウドで署名設定を確認できません。PC本体のクラウド準備状態を確認してください。",
  travel_session_not_active: "このお出かけは会話できる状態ではありません。PC本体のお出かけ状態を確認してください。",
  route_changed_before_provider_start: "AIサービスの接続設定が変わったため、送信を停止しました。",
  session_message_in_progress: "このお出かけには処理中の送信があります。前回の送信結果を確認してください。",
  message_already_reserved: "この送信の処理はすでに開始されています。前回の送信結果を確認してください。",
  travel_send_failed: "Liteの送信が完了しませんでした。送信結果と接続状態を確認してください。",
};

function sendFailure(code, status = 0) {
  const safeCode = typeof code === "string" && Object.hasOwn(SEND_FAILURE_MESSAGES, code) ? code : "travel_send_failed";
  return {
    code: safeCode,
    httpStatus: Number.isInteger(status) && status >= 100 && status <= 599 ? status : 0,
    message: SEND_FAILURE_MESSAGES[safeCode],
  };
}

function rememberSendFailure(pending, failure) {
  try {
    localStorage.setItem(KEYS.lastSendFailure, JSON.stringify({
      worker_url: pending.worker_url, session_id: pending.session_id, persona_id: pending.persona_id,
      client_message_id: pending.client_message_id, code: failure.code, http_status: failure.httpStatus,
    }));
  } catch {
    // 保存領域の障害が送信記録や、今回のエラー表示を妨げないようにする。
  }
}

function lastSendFailure(session, personaId) {
  try {
    const failure = JSON.parse(localStorage.getItem(KEYS.lastSendFailure) || "null");
    if (!failure || !session?.travel_session_id || !personaId || failure.worker_url !== base()
      || failure.session_id !== session.travel_session_id || failure.persona_id !== personaId) return null;
    return sendFailure(failure.code, failure.http_status);
  } catch {
    return null;
  }
}

// 正常な会話や診断とは分け、照合済みの失敗本文をこの端末だけに保持する。
function failedMessage(session, personaId) {
  try {
    const saved = JSON.parse(localStorage.getItem(KEYS.lastFailedMessage) || "null");
    if (!saved || saved.status !== "failed_known" || saved.worker_url !== base()
      || !session?.travel_session_id || !personaId
      || saved.session_id !== session.travel_session_id || saved.persona_id !== personaId
      || typeof saved.client_message_id !== "string" || !saved.client_message_id
      || typeof saved.message !== "string" || !saved.message.trim()) return null;
    const failure = sendFailure(saved.code, saved.http_status);
    return {
      clientMessageId: saved.client_message_id, message: saved.message,
      createdAt: typeof saved.created_at === "string" ? saved.created_at : "",
      code: failure.code, httpStatus: failure.httpStatus,
    };
  } catch {
    return null;
  }
}

function rememberFailedMessage(pending) {
  if (typeof pending.message !== "string" || !pending.message.trim()) return;
  let failure = null;
  try {
    const saved = JSON.parse(localStorage.getItem(KEYS.lastSendFailure) || "null");
    if (saved?.worker_url === pending.worker_url && saved.session_id === pending.session_id
      && saved.persona_id === pending.persona_id && saved.client_message_id === pending.client_message_id) {
      failure = sendFailure(saved.code, saved.http_status);
    }
  } catch { /* 本文は保持し、理由を復元できない時は一般的な分類を使う。 */ }
  const record = {
    status: "failed_known", worker_url: pending.worker_url || base(),
    session_id: pending.session_id, persona_id: pending.persona_id,
    client_message_id: pending.client_message_id, message: pending.message,
    created_at: pending.created_at || "", code: failure?.code || "travel_send_failed",
    http_status: failure?.httpStatus || 0,
  };
  try {
    localStorage.setItem(KEYS.lastFailedMessage, JSON.stringify(record));
  } catch {
    throw new TravelAdapterError("失敗した送信文を端末に保存できません。元の送信記録は保持しています。端末の空き容量を確認してください。", "failed_message_save_failed");
  }
}

function normalizeWorkerUrl(workerUrl) {
  const parsed = new URL(workerUrl);
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && ["localhost", "127.0.0.1"].includes(parsed.hostname))) {
    throw new Error("Worker URLはHTTPSで指定してください。");
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error("Worker URLに認証情報・クエリ・fragmentは指定できません。");
  return parsed.href.replace(/\/+$/, "");
}

function base() {
  return (localStorage.getItem(KEYS.base) || "").replace(/\/+$/, "");
}

function accessToken() {
  return localStorage.getItem(KEYS.access) || "";
}

function saveCredentials(value) {
  localStorage.setItem(KEYS.access, value.access_token);
  localStorage.setItem(KEYS.refresh, value.refresh_token);
  localStorage.setItem(KEYS.deviceId, value.device_id);
}

function clearCredentials() {
  [KEYS.access, KEYS.refresh, KEYS.deviceId].forEach((key) => localStorage.removeItem(key));
  connectionRevision += 1;
}

let refreshAccessInFlight = null;
let pendingOperationInFlight = false;
let connectionRevision = 0;

function connectionContext() {
  return {
    base: base(),
    deviceId: localStorage.getItem(KEYS.deviceId) || "",
    revision: connectionRevision,
  };
}
function assertConnection(context) {
  if (
    context.base !== base()
    || context.deviceId !== (localStorage.getItem(KEYS.deviceId) || "")
    || context.revision !== connectionRevision
  ) {
    throw new TravelAdapterError("接続先または端末認証が変更されました。元の接続先と記録を確認してください。", "pending_origin_mismatch");
  }
}
function connectionKey() {
  const context = connectionContext();
  return `${context.base}\u0000${context.deviceId}\u0000${context.revision}`;
}
async function withPendingGuard(operation) {
  const busy = () => new TravelAdapterError("別のLite送信確認を処理中です。少し待って再確認してください。", "pending_send_unresolved");
  if (pendingOperationInFlight) throw busy();
  pendingOperationInFlight = true;
  try {
    if (globalThis.navigator?.locks?.request) {
      return await navigator.locks.request("nexusLite.travel.pending", { ifAvailable: true }, (lock) => {
        if (!lock) throw busy();
        return operation();
      });
    }
    return await operation();
  } finally {
    pendingOperationInFlight = false;
  }
}

function listPendingArchive() {
  const raw = localStorage.getItem(KEYS.pendingArchive);
  if (raw === null) return [];
  try {
    const records = JSON.parse(raw);
    if (!Array.isArray(records) || records.some((item) =>
      !item || typeof item.raw !== "string" || typeof item.review_id !== "string"
      || typeof item.reviewed_at !== "string" || !["unknown", "home_recovered"].includes(item.mode)
      || typeof item.worker_url !== "string")) throw new Error();
    return records;
  } catch {
    throw new TravelAdapterError("保管した記録を読み取れないため、元の送信記録を保持しています。", "pending_archive_corrupt");
  }
}

function assertPendingRaw(raw) {
  if (localStorage.getItem(KEYS.pending) !== raw) {
    throw new TravelAdapterError("送信記録が変わりました。状態を再確認してください。", "pending_changed");
  }
}

function pendingIdentityMatches(body, pending) {
  return body?.client_message_id === pending.client_message_id
    && (!pending.session_id || body.travel_session_id === pending.session_id)
    && (!pending.persona_id || body.persona_id === pending.persona_id);
}

async function inspectRemotePending(inspected, context) {
  const pending = inspected.pending;
  if (pending.worker_url && pending.worker_url !== context.base) {
    throw new TravelAdapterError("保存済みの送信は別の接続先で開始されています。元の接続先へ戻してください。", "pending_origin_mismatch");
  }
  const body = await jsonRequest("/v1/message-requests/" + encodeURIComponent(pending.client_message_id));
  assertConnection(context);
  assertPendingRaw(inspected.raw);
  if (!pendingIdentityMatches(body, pending)) {
    throw new TravelAdapterError("送信記録の照合に失敗しました。元の記録を保持しています。", "pending_identity_mismatch");
  }
  return body;
}

async function pendingStatusInternal() {
  const inspected = inspectPendingMessage();
  if (inspected.state === "none") return null;
  if (inspected.state === "corrupt") {
    throw new TravelAdapterError("前回のLite送信状態を読み取れません。元の記録を保持しています。", "pending_send_unresolved");
  }
  const context = connectionContext();
  const body = await inspectRemotePending(inspected, context);
  assertConnection(context);
  assertPendingRaw(inspected.raw);
  if (FINAL_PENDING_STATUSES.has(body.status)) {
    if (body.status === "failed_known") rememberFailedMessage(inspected.pending);
    assertConnection(context);
    assertPendingRaw(inspected.raw);
    localStorage.removeItem(KEYS.pending);
    return null;
  }
  return body;
}

async function archivePendingInternal(expectedRaw, { mode, confirmed } = {}) {
  if (confirmed !== true || !["unknown", "home_recovered"].includes(mode)) {
    throw new TravelAdapterError("記録を保管する前に確認が必要です。", "pending_confirmation_required");
  }
  if (typeof expectedRaw !== "string") {
    throw new TravelAdapterError("保管する送信記録がありません。", "pending_changed");
  }
  assertPendingRaw(expectedRaw);
  const inspected = inspectPendingMessage();
  const context = connectionContext();
  if (mode === "unknown") {
    const pending = inspected.pending;
    if (!pending?.worker_url || !pending.session_id || !pending.persona_id) {
      throw new TravelAdapterError("元の送信先を確実に照合できません。本体側で状態を確認してください。", "pending_identity_mismatch");
    }
    const body = await inspectRemotePending(inspected, context);
    if (body.status !== "outcome_unknown") {
      throw new TravelAdapterError("結果不明として保管できる状態ではありません。再確認してください。", "pending_send_unresolved");
    }
  }
  assertConnection(context);
  assertPendingRaw(expectedRaw);
  const archive = listPendingArchive();
  if (!archive.some((item) => item.raw === expectedRaw)) {
    if (archive.length >= 100) {
      throw new TravelAdapterError("保管記録が上限の100件に達しています。元の送信記録は保持しています。", "pending_archive_full");
    }
    archive.push({ review_id: crypto.randomUUID(), reviewed_at: new Date().toISOString(),
      mode, worker_url: inspected.pending?.worker_url || context.base, raw: expectedRaw });
    try {
      localStorage.setItem(KEYS.pendingArchive, JSON.stringify(archive));
    } catch {
      throw new TravelAdapterError("記録を保管できません。端末の空き容量を確認してください。元の送信記録は保持しています。", "pending_archive_failed");
    }
  }
  assertConnection(context);
  assertPendingRaw(expectedRaw);
  localStorage.removeItem(KEYS.pending);
}


async function performRefreshAccess() {
  return withConnectionTimeout(async (signal) => {
    const context = connectionContext();
    const refreshToken = localStorage.getItem(KEYS.refresh);
    if (!refreshToken || !base()) return { ok: false, reason: "credentials_missing" };
    let response;
    try {
      response = await fetch(`${base()}/v1/devices/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
        cache: "no-store",
        signal,
      });
    } catch (error) {
      return { ok: false, reason: "worker_unreachable", error };
    }
    signal.throwIfAborted();
    assertConnection(context);
    if (localStorage.getItem(KEYS.refresh) !== refreshToken) throw new TravelAdapterError("端末登録が変わりました。状態を再確認してください。", "pending_changed");
    if (response.status === 401 || response.status === 403) {
      clearCredentials();
      return { ok: false, reason: "re_pair_required", status: response.status };
    }
    if (!response.ok) return { ok: false, reason: "refresh_failed", status: response.status };
    const credentials = await response.json();
    signal.throwIfAborted();
    assertConnection(context);
    if (localStorage.getItem(KEYS.refresh) !== refreshToken) throw new TravelAdapterError("端末登録が変わりました。状態を再確認してください。", "pending_changed");
    saveCredentials(credentials);
    return { ok: true };
  }).catch((error) => {
    if (error.name === "TimeoutError" || error.name === "AbortError") {
      return { ok: false, reason: "worker_unreachable" };
    }
    throw error;
  });
}

function refreshAccess() {
  if (!refreshAccessInFlight) {
    refreshAccessInFlight = performRefreshAccess().finally(() => {
      refreshAccessInFlight = null;
    });
  }
  return refreshAccessInFlight;
}

async function request(path, options = {}, retry = true, classifyUnauthorized = true, context = connectionContext()) {
  assertConnection(context);
  options.signal?.throwIfAborted();
  if (!base()) throw new Error("Worker URLが未設定です。");
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  if (accessToken()) headers.set("Authorization", `Bearer ${accessToken()}`);
  let response;
  try {
    response = await fetch(`${base()}${path}`, { ...options, headers, cache: "no-store" });
  } catch (error) {
    throw new TravelAdapterError("Workerへ接続できません。", "worker_unreachable");
  }
  assertConnection(context);
  options.signal?.throwIfAborted();
  if (response.status === 401 && retry && classifyUnauthorized) {
    const refreshed = await refreshAccess();
    if (refreshed.ok) return request(path, options, false, classifyUnauthorized, context);
    if (refreshed.reason === "worker_unreachable") {
      throw new TravelAdapterError("Workerへ接続できないため端末認証を更新できません。", "worker_unreachable");
    }
    if (refreshed.reason === "refresh_failed") {
      throw new TravelAdapterError(
        "端末認証を更新できません。Workerの状態を確認して再試行してください。",
        "refresh_failed",
        refreshed.status,
      );
    }
    if (refreshed.reason === "re_pair_required") {
      throw new TravelAdapterError(
        "端末の認証期限が切れているか、端末が失効されています。再ペアリングが必要です。",
        "re_pair_required",
        refreshed.status || 401,
      );
    }
    assertConnection(context);
    clearCredentials();
    throw new TravelAdapterError(
      "端末の認証期限が切れているか、端末が失効されています。再ペアリングが必要です。",
      "re_pair_required",
      refreshed.status || 401,
    );
  }
  if (response.status === 401 && classifyUnauthorized) {
    clearCredentials();
    throw new TravelAdapterError(
      "端末の認証期限が切れているか、端末が失効されています。再ペアリングが必要です。",
      "re_pair_required",
      401,
    );
  }
  return response;
}

async function jsonRequest(path, options = {}, classifyUnauthorized = true, requestContext = connectionContext()) {
  const read = async (signal) => {
    const context = requestContext;
    const response = await request(path, { ...options, signal }, classifyUnauthorized, classifyUnauthorized, context);
    const body = await response.json().catch(() => ({}));
    signal?.throwIfAborted();
    assertConnection(context);
    if (!response.ok) {
      const code = typeof body?.error === "string" && /^[a-z][a-z0-9_]{0,79}$/.test(body.error)
        ? body.error
        : "worker_request_failed";
      throw new TravelAdapterError(code, code, response.status);
    }
    return body;
  };
  // 出発・帰宅などの書込みやAI送信を短い読取期限で打ち切らない。
  if ((options.method || "GET").toUpperCase() !== "GET") return read(options.signal);
  try {
    return await withConnectionTimeout(read, { signal: options.signal });
  } catch (error) {
    if (error.name === "TimeoutError" || error.name === "AbortError") {
      throw new TravelAdapterError("Workerへ接続できません。状態を再確認してください。", "worker_unreachable");
    }
    throw error;
  }
}

function parseSse(text) {
  let answer = "";
  let terminal = "";
  let error = null;
  for (const line of text.split(/\r?\n/)) {
    if (!line.startsWith("data:")) continue;
    try {
      const event = JSON.parse(line.slice(5).trim());
      if (event.type === "response.text.delta") answer += event.text || "";
      if (["response.committed", "response.partial", "response.error"].includes(event.type)) terminal = event.type;
      if (event.type === "response.error") {
        const codes = {
          auth: "provider_auth_failed", rate_limit: "provider_rate_limited",
          invalid_request: "provider_rejected_request", model_unavailable: "provider_model_unavailable",
          provider_error: "provider_error", stream_interrupted: "stream_interrupted",
          output_limit: "output_limit", persistence_failed: "persistence_failed",
        };
        error = sendFailure(Object.hasOwn(codes, event.error?.category) ? codes[event.error.category] : "travel_send_failed", event.error?.http_status);
      } else if (event.type === "response.partial") {
        error = sendFailure(event.reason === "max_output_tokens" ? "output_limit" : "stream_interrupted");
      } else if (event.type === "response.committed") {
        error = null;
      }
    } catch {
      // 未知イベントは表示せず、確定照会可能なpendingを維持する。
    }
  }
  if (!terminal) error = sendFailure("stream_interrupted");
  return { answer, terminal, ...(error ? { error } : {}) };
}

export const travelAdapter = {
  keys: KEYS,
  configure(workerUrl) {
    const nextBase = normalizeWorkerUrl(workerUrl);
    const currentBase = base();
    if (currentBase !== nextBase) {
      connectionRevision += 1;
      clearCredentials();
      localStorage.removeItem(KEYS.sessionId);
      localStorage.removeItem(KEYS.personaId);
    }
    localStorage.setItem(KEYS.base, nextBase);
  },
  configuredBase: base,
  inspectPending: inspectPendingMessage,
  listPendingArchive,
  lastSendFailure,
  failedMessage,
  archivePending: (raw, options) => withPendingGuard(() => archivePendingInternal(raw, options)),
  paired: () => Boolean(accessToken()),
  connectionKey,
  errorCode: (error) => error instanceof TravelAdapterError ? error.code : "",
  httpStatus: (error) => error instanceof TravelAdapterError ? error.status : 0,
  async health() {
    return withConnectionTimeout(async (signal) => {
      if (!base()) return { ok: false, error: "worker_url_missing" };
      let response;
      try {
        response = await fetch(`${base()}/v1/health`, { cache: "no-store", signal });
      } catch (error) {
        const primaryError = safeBrowserError(error);
        try {
          await fetch(`${base()}/v1/health`, { cache: "no-store", mode: "no-cors", signal });
          return {
            ok: false,
            error: "cors_rejected",
            browser_error: primaryError.name,
            browser_message: primaryError.message,
          };
        } catch (fallbackError) {
          const fallback = safeBrowserError(fallbackError);
          return {
            ok: false,
            error: "worker_unreachable",
            browser_error: primaryError.name,
            browser_message: primaryError.message,
            fallback_error: fallback.name,
            fallback_message: fallback.message,
          };
        }
      }
      let body;
      try {
        body = await response.json();
      } catch (error) {
        const parsed = safeBrowserError(error);
        return {
          ok: false,
          error: "worker_invalid_response",
          http_status: response.status,
          content_type: response.headers.get("content-type") || "",
          browser_error: parsed.name,
          browser_message: parsed.message,
        };
      }
      if (!response.ok || !Number.isInteger(body.api_schema_version)) {
        return {
          ok: false,
          error: "worker_invalid_response",
          http_status: response.status,
          content_type: response.headers.get("content-type") || "",
          api_schema_version: body.api_schema_version,
        };
      }
      if (body.api_schema_version > SUPPORTED_API_SCHEMA_VERSION) {
        return { ok: false, error: "pwa_update_required", api_schema_version: body.api_schema_version };
      }
      if (body.api_schema_version < SUPPORTED_API_SCHEMA_VERSION) {
        return { ok: false, error: "worker_update_required", api_schema_version: body.api_schema_version };
      }
      if (
        body.storage_schema_ready === false
        || (Number.isInteger(body.d1_schema_version) && body.d1_schema_version < REQUIRED_D1_SCHEMA_VERSION)
      ) {
        return {
          ok: false,
          error: "worker_update_required",
          api_schema_version: body.api_schema_version,
          d1_schema_version: body.d1_schema_version,
          storage_schema_ready: body.storage_schema_ready,
        };
      }
      return body;
    }).catch((error) => ({
      ok: false,
      error: "worker_unreachable",
      browser_error: safeBrowserError(error).name,
      browser_message: safeBrowserError(error).message,
    }));
  },
  async pair(code, displayName) {
    const body = await jsonRequest("/v1/devices/pair", {
      method: "POST",
      body: JSON.stringify({ code, display_name: displayName }),
    }, false);
    saveCredentials(body);
    connectionRevision += 1;
    return body;
  },
  listStandby: () => jsonRequest("/v1/standby-snapshots"),
  externalAiExportFromStandby(standbySnapshotId, personaId) {
    return jsonRequest(`/v1/standby-snapshots/${encodeURIComponent(standbySnapshotId)}/external-ai-export`, {
      method: "POST",
      body: JSON.stringify({
        persona_id: personaId,
        disclosure_confirmed: true,
        include_core_memory: true,
        include_episodic_summary: true,
        recent_message_limit: 40,
      }),
    });
  },
  externalAiExportFromSession(sessionId, personaId) {
    return jsonRequest(
      `/v1/travel-sessions/${encodeURIComponent(sessionId)}/personas/${encodeURIComponent(personaId)}/external-ai-export`,
      {
        method: "POST",
        body: JSON.stringify({
          disclosure_confirmed: true,
          include_core_memory: true,
          include_episodic_summary: true,
          recent_message_limit: 40,
        }),
      },
    );
  },
  async activate(standbySnapshotId, activationMode) {
    const activationId = crypto.randomUUID();
    const body = await jsonRequest(`/v1/standby-snapshots/${encodeURIComponent(standbySnapshotId)}/activate`, {
      method: "POST",
      body: JSON.stringify({ activation_id: activationId, activation_mode: activationMode }),
    });
    localStorage.setItem(KEYS.sessionId, body.activated_session_id || "");
    return body;
  },
  async currentSession() {
    const body = await jsonRequest("/v1/travel-sessions/current");
    const session = body.session || null;
    if (session?.travel_session_id) localStorage.setItem(KEYS.sessionId, session.travel_session_id);
    return session;
  },
  async events(session, personaId) {
    const path = Array.isArray(session.personas)
      ? `/v1/travel-sessions/${encodeURIComponent(session.travel_session_id)}/personas/${encodeURIComponent(personaId)}/events`
      : `/v1/travel-sessions/${encodeURIComponent(session.travel_session_id)}/events`;
    const context = connectionContext();
    const pageSize = 100;
    let afterSequence = 0;
    const events = [];
    const missingSequences = [];
    while (true) {
      const page = await jsonRequest(`${path}?after_sequence=${afterSequence}`, {}, true, context);
      if (!page || typeof page !== "object" || Array.isArray(page)
        || !Array.isArray(page.events) || !Array.isArray(page.missing_sequences)) {
        throw new TravelAdapterError("Lite履歴の応答形式を確認できません。", "travel_history_invalid_response");
      }
      const nextCursor = page.next_cursor;
      if (!Number.isSafeInteger(nextCursor) || nextCursor < 0 || nextCursor < afterSequence) {
        throw new TravelAdapterError("Lite履歴の次の位置を確認できません。", "travel_history_invalid_cursor");
      }
      if (page.events.length > pageSize) {
        throw new TravelAdapterError("Lite履歴の1ページが大きすぎます。", "travel_history_invalid_response");
      }
      events.push(...page.events);
      missingSequences.push(...page.missing_sequences);
      if (page.events.length < pageSize) {
        return { ...page, events, missing_sequences: missingSequences };
      }
      if (nextCursor <= afterSequence) {
        throw new TravelAdapterError("Lite履歴の読み込み位置が進みません。", "travel_history_cursor_stalled");
      }
      afterSequence = nextCursor;
    }
  },
  providerProfiles: () => jsonRequest("/v1/provider-profiles"),
  models(credentialProfileId, refresh = false) {
    const suffix = refresh ? "?refresh=1" : "";
    return jsonRequest(
      `/v1/provider-profiles/${encodeURIComponent(credentialProfileId)}/models${suffix}`,
    );
  },
  async changeRoute(session, personaId, credentialProfileId, modelId) {
    const path = Array.isArray(session.personas)
      ? `/v1/travel-sessions/${encodeURIComponent(session.travel_session_id)}/personas/${encodeURIComponent(personaId)}/route`
      : `/v1/travel-sessions/${encodeURIComponent(session.travel_session_id)}/route`;
    return jsonRequest(path, {
      method: "PUT",
      body: JSON.stringify({
        route_change_id: crypto.randomUUID().replaceAll("-", "_"),
        credential_profile_id: credentialProfileId,
        model_id: modelId,
      }),
    });
  },
  changeBudget(session, payload) {
    // 書込み結果が不明でも再試行せず、GETで現在の上限を再確認する。
    return withConnectionTimeout((signal) => jsonRequest(`/v1/travel-sessions/${encodeURIComponent(session.travel_session_id)}/budget`, {
      method: "PUT", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }), { timeoutMs: 15000 });
  },
  usageSummary(session, personaId) {
    const query = Array.isArray(session.personas) && personaId
      ? `?persona_id=${encodeURIComponent(personaId)}`
      : "";
    return jsonRequest(
      `/v1/travel-sessions/${encodeURIComponent(session.travel_session_id)}/usage-summary${query}`,
    );
  },
  async send(session, personaId, message, clientMessageId) {
    return withPendingGuard(async () => {
      const context = connectionContext();
      const pendingStatus = await pendingStatusInternal();
      assertConnection(context);
      if (pendingStatus) {
        throw new TravelAdapterError("前回のLite送信結果が未確定です。状態を確認してください。", "pending_send_unresolved");
      }
      assertPendingRaw(null);
      const pending = {
        client_message_id: clientMessageId,
        session_id: session.travel_session_id,
        persona_id: personaId,
        message,
        worker_url: base(),
        device_id: localStorage.getItem(KEYS.deviceId) || "",
        created_at: new Date().toISOString(),
      };
      const pendingRaw = JSON.stringify(pending);
      localStorage.setItem(KEYS.pending, pendingRaw);
      let response;
      try {
        response = await request("/v1/travel-sessions/" + encodeURIComponent(session.travel_session_id) + "/messages", {
          method: "POST",
          body: JSON.stringify({ client_message_id: clientMessageId, persona_id: personaId, message }),
        });
      } catch (error) {
        assertConnection(context);
        assertPendingRaw(pendingRaw);
        if (error instanceof TravelAdapterError && error.code === "worker_unreachable") {
          rememberSendFailure(pending, sendFailure("worker_unreachable"));
        }
        throw error;
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        assertConnection(context);
        assertPendingRaw(pendingRaw);
        const failure = sendFailure(body?.error, response.status);
        rememberSendFailure(pending, failure);
        const error = new TravelAdapterError(failure.message, failure.code, failure.httpStatus);
        if (["budget_limit_exceeded", "persona_budget_limit_exceeded"].includes(failure.code)
          && typeof body.next_request_reserved_usd === "number" && Number.isFinite(body.next_request_reserved_usd)
          && body.next_request_reserved_usd >= 0) error.nextRequestReservedUsd = body.next_request_reserved_usd;
        throw error;
      }
      let streamText;
      try {
        streamText = await response.text();
      } catch {
        assertConnection(context);
        assertPendingRaw(pendingRaw);
        const failure = sendFailure("stream_interrupted");
        rememberSendFailure(pending, failure);
        throw new TravelAdapterError(failure.message, failure.code);
      }
      const result = parseSse(streamText);
      assertConnection(context);
      assertPendingRaw(pendingRaw);
      if (result.terminal === "response.committed") {
        localStorage.removeItem(KEYS.pending);
        if (lastSendFailure(session, personaId)) localStorage.removeItem(KEYS.lastSendFailure);
        if (failedMessage(session, personaId)) localStorage.removeItem(KEYS.lastFailedMessage);
      } else if (result.error) {
        rememberSendFailure(pending, result.error);
      }
      return result;
    });
  },
  pendingStatus: () => withPendingGuard(pendingStatusInternal),
  saveDraft(value) { localStorage.setItem(KEYS.draft, value); },
  draft: () => localStorage.getItem(KEYS.draft) || "",
  clear() {
    connectionRevision += 1;
    Object.values(KEYS).filter((key) => key !== KEYS.base && key !== KEYS.pendingArchive).forEach((key) => localStorage.removeItem(key));
  },
};
