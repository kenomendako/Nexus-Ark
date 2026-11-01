const UI_BUILD = /^v\d{1,8}$/;
const SHA256_HEX = /^[0-9a-f]{64}$/;

export const LITE_UPDATE_METADATA_PATH = "/static/lite-build.json";
export const LITE_UPDATE_TIMEOUT_MS = 3500;
export const LITE_UPDATE_OPERATION_TIMEOUT_MS = 10000;
export const LITE_UPDATE_RESUME_THROTTLE_MS = 60_000;
export const LITE_UPDATE_METADATA_MAX_BYTES = 4096;

export function normalizeLiteBuildMetadata(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const keys = Object.keys(value).sort();
  if (keys.length !== 3 || keys.join("\0") !== "asset_digest\0format_version\0ui_build") {
    return null;
  }
  const formatVersion = value.format_version;
  const uiBuild = value.ui_build;
  const assetDigest = value.asset_digest;
  if (formatVersion !== 1 || typeof uiBuild !== "string" || !UI_BUILD.test(uiBuild)
    || typeof assetDigest !== "string" || !SHA256_HEX.test(assetDigest)) {
    return null;
  }
  return {
    format_version: 1,
    ui_build: uiBuild,
    asset_digest: assetDigest,
  };
}

export function liteBuildMetadataUrl(pathname = globalThis.location?.pathname || "") {
  return String(pathname).startsWith("/lite")
    ? `/lite${LITE_UPDATE_METADATA_PATH}`
    : LITE_UPDATE_METADATA_PATH;
}

export function liteServiceWorkerPaths(pathname = globalThis.location?.pathname || "") {
  return String(pathname).startsWith("/lite")
    ? { scope: "/lite/", script: "/lite/service-worker.js" }
    : { scope: "/", script: "/service-worker.js" };
}

export function isLiteServiceWorkerRegistration(registration, origin, paths) {
  if (!registration || !origin || !paths) {
    return false;
  }
  let scope;
  try {
    scope = new URL(registration.scope, origin);
    const workers = [registration.active, registration.waiting, registration.installing].filter(Boolean);
    if (!workers.length || !workers.every((worker) => {
      const script = new URL(worker.scriptURL, origin);
      return script.origin === new URL(origin).origin && script.pathname === paths.script;
    })) return false;
  } catch {
    return false;
  }
  return scope.origin === new URL(origin).origin
    && scope.pathname === paths.scope;
}

export function liteUpdateBlockers({
  state = {},
  travelPending = null,
  selectedChatFiles = [],
  preparedChatAttachments = [],
} = {}) {
  const reasons = [];
  const busyLabels = [
    [state.sending, "送信中"],
    [state.confirmingSend, "送信結果の確認中"],
    [state.syncing, "同期中"],
    [state.recording, "録音中"],
    [state.transcribing, "音声を文字起こし中"],
    [state.speaking, "音声を再生中"],
    [state.travelRouteChanging, "独立モードのAI設定を変更中"],
    [state.travelEntryPromise, "独立モードを開始中"],
    [state.returningHome, "帰宅処理中"],
    [state.standbyAutoRefreshing, "お出かけ前データを更新中"],
  ];
  for (const [active, label] of busyLabels) {
    if (active) reasons.push(label);
  }
  if (state.pendingSend) reasons.push("本体の送信結果が未確認");
  if (travelPending?.state === "pending" || travelPending?.state === "corrupt") {
    reasons.push("独立モードの送信結果が未確認");
  }
  if (state.mode === "travel" || state.travelSession || state.currentTravelSession) {
    reasons.push("独立モード使用中");
  }
  if (selectedChatFiles.length || preparedChatAttachments.length) {
    reasons.push("添付を送信前に確認中");
  }
  return { blocked: reasons.length > 0, reasons };
}

async function responseTextWithinLimit(response, maxBytes) {
  const contentLength = Number(response.headers?.get?.("content-length"));
  if (Number.isFinite(contentLength) && contentLength > maxBytes) {
    throw new Error("Lite画面の版情報が大きすぎます。");
  }
  const reader = response.body?.getReader?.();
  if (!reader) {
    const text = await response.text();
    if (new TextEncoder().encode(text).byteLength > maxBytes) {
      throw new Error("Lite画面の版情報が大きすぎます。");
    }
    return text;
  }
  const decoder = new TextDecoder();
  const chunks = [];
  let totalBytes = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      totalBytes += part.value.byteLength;
      if (totalBytes > maxBytes) {
        void reader.cancel().catch(() => {});
        throw new Error("Lite画面の版情報が大きすぎます。");
      }
      chunks.push(decoder.decode(part.value, { stream: true }));
    }
    chunks.push(decoder.decode());
    return chunks.join("");
  } finally {
    reader.releaseLock?.();
  }
}

export async function fetchLiteBuildMetadata({
  url,
  fetchImpl = globalThis.fetch,
  timeoutMs = LITE_UPDATE_TIMEOUT_MS,
  maxBytes = LITE_UPDATE_METADATA_MAX_BYTES,
} = {}) {
  if (!url || typeof fetchImpl !== "function") {
    throw new Error("Lite版情報を確認できません。");
  }
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timeoutId = globalThis.setTimeout?.(() => controller?.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller?.signal,
    });
    if (!response.ok) {
      throw new Error(`Lite版情報の取得に失敗しました（HTTP ${response.status}）。`);
    }
    const metadata = normalizeLiteBuildMetadata(JSON.parse(await responseTextWithinLimit(response, maxBytes)));
    if (!metadata) {
      throw new Error("Lite版情報の形式を確認できません。");
    }
    return metadata;
  } finally {
    if (timeoutId !== undefined) {
      globalThis.clearTimeout?.(timeoutId);
    }
  }
}
