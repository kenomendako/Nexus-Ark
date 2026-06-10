const state = {
  apiBase: localStorage.getItem("nexusLite.apiBase") || window.location.origin,
  token: localStorage.getItem("nexusLite.token") || "",
  rooms: [],
  roomId: localStorage.getItem("nexusLite.roomId") || "",
  connected: false,
  sending: false,
  syncing: false,
  recording: false,
  transcribing: false,
  speaking: false,
  stopRequested: false,
  notificationPermission: "unsupported",
  ttsMode: localStorage.getItem("nexusLite.ttsMode") || "trim",
  managementLoaded: false,
  twitterDrafts: [],
  currentAudio: null,
  stopCurrentPlayback: null,
  mediaRecorder: null,
  recordingStream: null,
  audioChunks: [],
  recordingStartedAt: 0,
  recordingTimer: null,
  recordingTimeout: null,
  pushSubscriptionCount: 0,
  responsePreviewEnabled: true,
  theme: localStorage.getItem("nexusLite.theme") || "green",
  colorScheme: localStorage.getItem("nexusLite.colorScheme") || "auto",
  redactionEnabled: false,
  redactionRules: [],
  chatMessages: [],
  pendingSend: readPendingSend()
};

const VOICE_RECORDING_MAX_MS = 60000;
const RECENT_SUBMIT_GUARD_MS = 3000;
const PENDING_RESEND_GRACE_MS = 3000;

const els = {
  apiBaseInput: document.querySelector("#api-base-input"),
  tokenInput: document.querySelector("#token-input"),
  connectButton: document.querySelector("#connect-button"),
  connectionDetails: document.querySelector("#connection-details"),
  connectionSummaryUrl: document.querySelector("#connection-summary-url"),
  refreshButton: document.querySelector("#refresh-button"),
  connectionStatus: document.querySelector("#connection-status"),
  roomTitle: document.querySelector("#room-title"),
  roomSelect: document.querySelector("#room-select"),
  syncStatus: document.querySelector("#sync-status"),
  syncButton: document.querySelector("#sync-button"),
  ttsModeSelect: document.querySelector("#tts-mode-select"),
  stopAudioButton: document.querySelector("#stop-audio-button"),
  secureOriginNotice: document.querySelector("#secure-origin-notice"),
  managementDetails: document.querySelector("#management-details"),
  managementSummaryStatus: document.querySelector("#management-summary-status"),
  draftRefreshButton: document.querySelector("#draft-refresh-button"),
  draftSelect: document.querySelector("#draft-select"),
  draftContent: document.querySelector("#draft-content"),
  draftMeta: document.querySelector("#draft-meta"),
  draftApproveButton: document.querySelector("#draft-approve-button"),
  draftRejectButton: document.querySelector("#draft-reject-button"),
  locationSelect: document.querySelector("#location-select"),
  autonomyMeta: document.querySelector("#autonomy-meta"),
  autonomyQuietButton: document.querySelector("#autonomy-quiet-button"),
  autonomyNormalButton: document.querySelector("#autonomy-normal-button"),
  noteMeta: document.querySelector("#note-meta"),
  noteTypeSelect: document.querySelector("#note-type-select"),
  noteRefreshButton: document.querySelector("#note-refresh-button"),
  noteHeadingSelect: document.querySelector("#note-heading-select"),
  noteShowSectionButton: document.querySelector("#note-show-section-button"),
  noteViewer: document.querySelector("#note-viewer"),
  notificationMeta: document.querySelector("#notification-meta"),
  notificationEnableButton: document.querySelector("#notification-enable-button"),
  notificationTestButton: document.querySelector("#notification-test-button"),
  notificationUnsubscribeCurrentButton: document.querySelector("#notification-unsubscribe-current-button"),
  notificationDetail: document.querySelector("#notification-detail"),
  eventNotificationEnabled: document.querySelector("#event-notification-enabled"),
  responsePreviewEnabled: document.querySelector("#response-preview-enabled"),
  eventNotificationMinimum: document.querySelector("#event-notification-minimum"),
  eventNotificationCooldown: document.querySelector("#event-notification-cooldown"),
  eventNotificationSourceCooldowns: document.querySelector("#event-notification-source-cooldowns"),
  eventNotificationSaveButton: document.querySelector("#event-notification-save-button"),
  pushDeviceList: document.querySelector("#push-device-list"),
  expressionValue: document.querySelector("#expression-value"),
  arousalValue: document.querySelector("#arousal-value"),
  locationValue: document.querySelector("#location-value"),
  driveBoredom: document.querySelector("#drive-boredom"),
  driveCuriosity: document.querySelector("#drive-curiosity"),
  driveGoal: document.querySelector("#drive-goal"),
  driveRelated: document.querySelector("#drive-related"),
  messages: document.querySelector("#messages"),
  chatForm: document.querySelector("#chat-form"),
  messageInput: document.querySelector("#message-input"),
  imageInput: document.querySelector("#image-input"),
  voiceButton: document.querySelector("#voice-button"),
  attachmentName: document.querySelector("#attachment-name"),
  imageDialog: document.querySelector("#image-dialog"),
  imageDialogImg: document.querySelector("#image-dialog-img"),
  closeImageDialog: document.querySelector("#close-image-dialog"),
  sendButton: document.querySelector("#send-button"),
  personaAvatar: document.querySelector("#persona-avatar"),
  themeDetails: document.querySelector("#theme-details"),
  themeSummaryStatus: document.querySelector("#theme-summary-status"),
  themeSelect: document.querySelector("#theme-select"),
  colorSchemeSelect: document.querySelector("#color-scheme-select"),
  redactionDetails: document.querySelector("#redaction-details"),
  redactionSummaryStatus: document.querySelector("#redaction-summary-status"),
  redactionEnabledCheckbox: document.querySelector("#redaction-enabled-checkbox"),
  ruleFindInput: document.querySelector("#rule-find-input"),
  ruleReplaceInput: document.querySelector("#rule-replace-input"),
  ruleColorInput: document.querySelector("#rule-color-input"),
  addRuleButton: document.querySelector("#add-rule-button"),
  rulesList: document.querySelector("#rules-list")
};

function readPendingSend() {
  try {
    return JSON.parse(localStorage.getItem("nexusLite.pendingSend") || "null");
  } catch {
    return null;
  }
}

function writePendingSend(value) {
  state.pendingSend = value;
  if (value) {
    localStorage.setItem("nexusLite.pendingSend", JSON.stringify(value));
  } else {
    localStorage.removeItem("nexusLite.pendingSend");
  }
}

function updatePendingSendPatch(patch) {
  if (!state.pendingSend) {
    return null;
  }
  const updated = {
    ...state.pendingSend,
    ...patch
  };
  writePendingSend(updated);
  return updated;
}

function pendingAgeMs(pending) {
  const timestamp = Date.parse(pending?.sentAt || "");
  return Number.isFinite(timestamp) ? Date.now() - timestamp : Number.POSITIVE_INFINITY;
}

function selectedFileSignature(file) {
  if (!file) {
    return null;
  }
  return {
    name: file.name || "",
    size: Number(file.size || 0)
  };
}

function canReleaseUnconfirmedPending(pending) {
  if (!pending || pending.roomId !== state.roomId) {
    return false;
  }
  if (pending.confirmation !== "not_found") {
    return false;
  }
  if (pendingAgeMs(pending) < PENDING_RESEND_GRACE_MS) {
    return false;
  }
  return true;
}

function markPendingResponseNotificationWanted() {
  const pending = state.pendingSend;
  if (!pending || pending.notifyOnResponse) {
    return;
  }
  writePendingSend({
    ...pending,
    notifyOnResponse: true
  });
}

async function notifyResponseIfWanted(message) {
  const pending = state.pendingSend;
  if (!pending?.notifyOnResponse) {
    return false;
  }
  if (state.pushSubscriptionCount > 0) {
    return false;
  }
  return showLiteNotification("Nexus Ark Lite", message || responseNotificationText());
}

function currentRoomDisplayName() {
  const room = state.rooms.find((item) => item.room_id === state.roomId);
  return room?.display_name || state.roomId || "Nexus Ark Lite";
}

function responseNotificationText() {
  return `${currentRoomDisplayName()}からのメッセージがあります。`;
}

function responseNotificationBody(reply) {
  const speaker = currentRoomDisplayName();
  if (!state.responsePreviewEnabled) {
    return responseNotificationText();
  }
  const excerpt = String(reply || "").replace(/\s+/g, " ").trim();
  if (!excerpt) {
    return responseNotificationText();
  }
  const limit = 42;
  const clipped = excerpt.length > limit ? `${excerpt.slice(0, limit).trim()}...` : excerpt;
  return `${speaker}「${clipped}」`;
}

function clearSelectedImage() {
  els.imageInput.value = "";
  els.attachmentName.textContent = "";
}

function normalizeBase(value) {
  return String(value || "").trim().replace(/\/$/, "");
}

function setConnectionStatus(text, mode = "idle") {
  els.connectionStatus.textContent = text;
  els.connectionStatus.dataset.mode = mode;
}

function setSyncStatus(text, mode = "idle") {
  els.syncStatus.textContent = text || "";
  els.syncStatus.dataset.mode = mode;
}

function formatConnectionError(error) {
  const message = String(error?.message || error || "");
  if (message.startsWith("401 ")) {
    return "Tokenを入力して接続してください。";
  }
  if (message.startsWith("403 ")) {
    return "Tokenが未設定または一致していません。";
  }
  return message || "接続できませんでした。";
}

function showConnectionError(error) {
  const message = formatConnectionError(error);
  state.connected = false;
  els.roomTitle.textContent = "接続できません";
  setConnectionStatus("接続エラー", "error");
  setSyncStatus(message, "warn");
  els.connectionDetails.open = true;
}

function setNotificationDetail(text, mode = "idle") {
  if (!els.notificationDetail) {
    return;
  }
  els.notificationDetail.textContent = text || "";
  els.notificationDetail.dataset.mode = mode;
}

function withTimeout(promise, ms, label) {
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = window.setTimeout(() => reject(new Error(`${label}が${ms / 1000}秒以内に完了しませんでした。`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => window.clearTimeout(timeoutId));
}

function isLocalHost(hostname) {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

function isSecureVoiceOrigin() {
  return window.location.protocol === "https:" || isLocalHost(window.location.hostname);
}

function isNotificationSupported() {
  return "Notification" in window;
}

function isWebPushSupported() {
  return isNotificationSupported() && "serviceWorker" in navigator && "PushManager" in window;
}

function updateNotificationStatus() {
  if (!isNotificationSupported()) {
    state.notificationPermission = "unsupported";
    els.notificationMeta.textContent = "未対応";
    els.notificationEnableButton.disabled = true;
    els.notificationTestButton.disabled = true;
    return;
  }
  state.notificationPermission = Notification.permission;
  if (!isSecureVoiceOrigin()) {
    els.notificationMeta.textContent = "HTTPSが必要";
    els.notificationEnableButton.disabled = true;
    els.notificationTestButton.disabled = true;
    return;
  }
  const labels = {
    granted: "許可済み",
    denied: "拒否済み",
    default: "未許可"
  };
  const pushText = isWebPushSupported() ? " / Push対応" : " / Push未対応";
  els.notificationMeta.textContent = `${labels[state.notificationPermission] || state.notificationPermission}${pushText}`;
  els.notificationEnableButton.disabled = state.notificationPermission === "granted" || state.notificationPermission === "denied";
  els.notificationTestButton.disabled = state.notificationPermission !== "granted";
}

function applyThemeSettings() {
  document.documentElement.dataset.theme = state.theme;
  document.documentElement.dataset.colorScheme = state.colorScheme;

  if (els.themeSelect) {
    els.themeSelect.value = state.theme;
  }
  if (els.colorSchemeSelect) {
    els.colorSchemeSelect.value = state.colorScheme;
  }

  if (els.themeSummaryStatus) {
    const themeNameMap = {
      green: "グリーン",
      blue: "ブルー",
      red: "レッド",
      purple: "パープル",
      orange: "オレンジ",
      yellow: "イエロー"
    };
    const modeNameMap = {
      auto: "自動",
      light: "ライト",
      dark: "ダーク"
    };
    const tName = themeNameMap[state.theme] || state.theme;
    const mName = modeNameMap[state.colorScheme] || state.colorScheme;
    els.themeSummaryStatus.textContent = `${tName} / ${mName}`;
  }
}

function applyRedactionSettings() {
  state.redactionEnabled = localStorage.getItem("nexusLite.redactionEnabled") === "true";
  try {
    state.redactionRules = JSON.parse(localStorage.getItem("nexusLite.redactionRules")) || [
      { find: "ユーザー", replace: "ゲスト", color: "#62827e" }
    ];
  } catch {
    state.redactionRules = [
      { find: "ユーザー", replace: "ゲスト", color: "#62827e" }
    ];
  }

  if (els.redactionEnabledCheckbox) {
    els.redactionEnabledCheckbox.checked = state.redactionEnabled;
  }
  if (els.redactionSummaryStatus) {
    els.redactionSummaryStatus.textContent = state.redactionEnabled ? "有効" : "オフ";
    els.redactionSummaryStatus.className = state.redactionEnabled ? "status-pill ok" : "status-pill";
  }
  renderRulesList();
}

function renderRulesList() {
  if (!els.rulesList) return;
  els.rulesList.innerHTML = "";
  if (state.redactionRules.length === 0) {
    const empty = document.createElement("li");
    empty.className = "rule-item";
    empty.style.justifyContent = "center";
    empty.style.color = "var(--muted)";
    empty.textContent = "ルールがありません";
    els.rulesList.appendChild(empty);
    return;
  }
  
  state.redactionRules.forEach((rule, idx) => {
    const li = document.createElement("li");
    li.className = "rule-item";
    
    const content = document.createElement("div");
    content.className = "rule-item-content";
    
    const findText = document.createElement("span");
    findText.textContent = `${rule.find} ➔ `;
    content.appendChild(findText);
    
    const replaceBadge = document.createElement("span");
    replaceBadge.className = "rule-badge";
    replaceBadge.textContent = rule.replace || "(空)";
    if (rule.color) {
      replaceBadge.style.backgroundColor = rule.color;
    } else {
      replaceBadge.style.backgroundColor = "var(--muted)";
    }
    content.appendChild(replaceBadge);
    
    li.appendChild(content);
    
    const delBtn = document.createElement("button");
    delBtn.className = "delete-rule-btn";
    delBtn.type = "button";
    delBtn.innerHTML = "✖";
    delBtn.title = "削除";
    delBtn.addEventListener("click", () => {
      state.redactionRules.splice(idx, 1);
      localStorage.setItem("nexusLite.redactionRules", JSON.stringify(state.redactionRules));
      renderRulesList();
      renderChatMessages();
    });
    li.appendChild(delBtn);
    
    els.rulesList.appendChild(li);
  });
}

function applyRedactions(text) {
  let result = escapeHtml(text || "");
  if (!state.redactionEnabled || !state.redactionRules || state.redactionRules.length === 0) {
    return result;
  }
  for (const rule of state.redactionRules) {
    const findStr = rule.find;
    if (!findStr) continue;
    const replaceStr = rule.replace || "";
    const color = rule.color;
    
    const escapedFind = escapeHtml(findStr);
    const escapedReplace = escapeHtml(replaceStr);
    
    const escapedRegex = escapedFind.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&');
    const regex = new RegExp(escapedRegex, 'g');
    
    if (color) {
      const replacementHtml = `<span style="background-color: ${color}; color: #ffffff; padding: 2px 4px; border-radius: 3px; font-weight: bold;">${escapedReplace}</span>`;
      result = result.replace(regex, replacementHtml);
    } else {
      result = result.replace(regex, escapedReplace);
    }
  }
  return result;
}

function escapeHtml(string) {
  if (typeof string !== 'string') {
    return string;
  }
  return string.replace(/[&<>"']/g, function (match) {
    const map = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    };
    return map[match];
  });
}

async function requestNotificationPermission() {
  if (!isNotificationSupported() || !isSecureVoiceOrigin()) {
    updateNotificationStatus();
    setSyncStatus("通知にはHTTPSまたはlocalhostが必要です。", "warn");
    return false;
  }
  try {
    const permission = await Notification.requestPermission();
    state.notificationPermission = permission;
    updateNotificationStatus();
    if (permission !== "granted") {
      setSyncStatus("通知が許可されませんでした。", "warn");
      return false;
    }
    if (state.connected && state.roomId) {
      await subscribeWebPush().catch((error) => setSyncStatus(`Push購読を保存できませんでした: ${error.message}`, "warn"));
    }
    setSyncStatus("通知を許可しました。");
    return true;
  } catch (error) {
    setSyncStatus(`通知許可を取得できませんでした: ${error.message}`, "warn");
    updateNotificationStatus();
    return false;
  }
}

async function ensureServiceWorkerRegistration() {
  if (!("serviceWorker" in navigator)) {
    return null;
  }
  const registration = await navigator.serviceWorker.register("/lite/service-worker.js", { scope: "/lite/" });
  if (registration.active) {
    return registration;
  }
  const worker = registration.installing || registration.waiting;
  if (!worker) {
    return registration;
  }
  await new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => reject(new Error("Service Workerの有効化が完了しませんでした。")), 5000);
    worker.addEventListener("statechange", () => {
      if (worker.state === "activated") {
        window.clearTimeout(timeoutId);
        resolve();
      }
    });
  });
  return registration;
}

async function focusLiteWindow() {
  try {
    window.focus();
  } catch {
    // focus may be blocked by the browser, but assigning location still helps fallback notifications.
  }
  if (!window.location.pathname.startsWith("/lite")) {
    window.location.href = "/lite/";
  }
}

function urlBase64ToUint8Array(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = `${value}${padding}`.replace(/-/g, "+").replace(/_/g, "/");
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let index = 0; index < rawData.length; index += 1) {
    outputArray[index] = rawData.charCodeAt(index);
  }
  return outputArray;
}

function arrayBufferEquals(left, right) {
  if (!left || !right || left.byteLength !== right.byteLength) {
    return false;
  }
  const leftView = new Uint8Array(left);
  const rightView = new Uint8Array(right);
  for (let index = 0; index < leftView.length; index += 1) {
    if (leftView[index] !== rightView[index]) {
      return false;
    }
  }
  return true;
}

async function subscribeWebPush({ updateDetail = true } = {}) {
  if (!isWebPushSupported() || Notification.permission !== "granted" || !state.connected || !state.roomId) {
    return null;
  }
  const showStep = (text) => {
    if (updateDetail) {
      setNotificationDetail(text);
    }
  };
  showStep("Push購読: VAPID公開鍵取得中...");
  const keyResponse = await api("/api/v1/push/vapid-public-key", { timeoutMs: 8000 });
  const applicationServerKey = urlBase64ToUint8Array(keyResponse.public_key);

  showStep("Push購読: Service Worker準備中...");
  const registration = await withTimeout(ensureServiceWorkerRegistration(), 8000, "Service Worker準備");
  if (!registration?.pushManager) {
    throw new Error("PushManagerを利用できません。PWAとしてインストール済みか確認してください。");
  }

  showStep("Push購読: 既存購読確認中...");
  let subscription = await withTimeout(registration.pushManager.getSubscription(), 5000, "既存Push購読確認");
  if (subscription?.options?.applicationServerKey && !arrayBufferEquals(subscription.options.applicationServerKey, applicationServerKey)) {
    showStep("Push購読: 古い購読を解除中...");
    await withTimeout(subscription.unsubscribe(), 5000, "古いPush購読解除");
    subscription = null;
  }
  if (!subscription) {
    showStep("Push購読: ブラウザ購読作成中...");
    subscription = await withTimeout(
      registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey
      }),
      10000,
      "ブラウザPush購読作成"
    );
  }
  showStep("Push購読: API Gatewayへ保存中...");
  const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/push/subscriptions`, {
    method: "POST",
    body: JSON.stringify({
      ...subscription.toJSON(),
      user_agent: navigator.userAgent || ""
    }),
    timeoutMs: 8000
  });
  state.pushSubscriptionCount = Number(response.subscription_count || 0);
  els.notificationMeta.textContent = `Push保存済み ${response.subscription_count}`;
  if (updateDetail) {
    const cleanupText = response.detail ? ` / ${response.detail}` : "";
    setNotificationDetail(`Push保存: subscriptions=${response.subscription_count}${cleanupText}`);
  }
  return response;
}

function describePushDevices(response) {
  const subscriptions = Array.isArray(response?.subscriptions) ? response.subscriptions : [];
  const deviceText = subscriptions
    .slice(0, 3)
    .map((item, index) => {
      const label = item.endpoint_host || `Push端末 ${index + 1}`;
      const failures = Number(item.failure_count || 0);
      return failures > 0 ? `${label}(失敗${failures})` : label;
    })
    .join(", ");
  const cleanupText = response?.cleaned_count ? ` / 古い購読掃除 ${response.cleaned_count}` : "";
  return `Push保存: subscriptions=${response?.subscription_count || 0}${deviceText ? ` / ${deviceText}` : ""}${cleanupText}`;
}

function formatPushDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function renderPushDevices(response) {
  if (!els.pushDeviceList) {
    return;
  }
  els.pushDeviceList.replaceChildren();
  const subscriptions = Array.isArray(response?.subscriptions) ? response.subscriptions : [];
  if (!subscriptions.length) {
    const empty = document.createElement("div");
    empty.className = "push-device-empty";
    empty.textContent = "保存済みPush端末はありません。";
    els.pushDeviceList.appendChild(empty);
    return;
  }
  subscriptions.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "push-device-row";

    const text = document.createElement("div");
    text.className = "push-device-text";
    const title = document.createElement("strong");
    title.textContent = item.endpoint_host || `Push端末 ${index + 1}`;
    const meta = document.createElement("span");
    const failureText = Number(item.failure_count || 0) > 0 ? ` / 失敗 ${item.failure_count}` : "";
    meta.textContent = `更新 ${formatPushDate(item.updated_at)} / 成功 ${formatPushDate(item.last_success_at)}${failureText}`;
    text.append(title, meta);

    const button = document.createElement("button");
    button.className = "secondary-button compact-button";
    button.type = "button";
    button.textContent = "削除";
    button.addEventListener("click", () => deletePushSubscription(item.id));

    row.append(text, button);
    els.pushDeviceList.appendChild(row);
  });
}

async function refreshPushStatus({ updateDetail = true } = {}) {
  if (!state.connected || !state.roomId || Notification.permission !== "granted") {
    updateNotificationStatus();
    renderPushDevices({ subscriptions: [] });
    return null;
  }
  const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/push/status`);
  state.pushSubscriptionCount = Number(response.subscription_count || 0);
  els.notificationMeta.textContent = `許可済み / Push保存 ${response.subscription_count}`;
  renderPushDevices(response);
  if (updateDetail) {
    setNotificationDetail(describePushDevices(response));
  }
  return response;
}

async function deletePushSubscription(subscriptionId) {
  if (!state.connected || !state.roomId || !subscriptionId) {
    return;
  }
  const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/push/subscriptions/${encodeURIComponent(subscriptionId)}`, {
    method: "DELETE",
    timeoutMs: 8000
  });
  state.pushSubscriptionCount = Number(response.subscription_count || 0);
  setNotificationDetail(`Push端末削除: status=${response.status} subscriptions=${response.subscription_count}`);
  await refreshPushStatus({ updateDetail: false });
}

async function unsubscribeCurrentPushDevice() {
  if (!isWebPushSupported()) {
    setNotificationDetail("このブラウザではPush解除を利用できません。", "warn");
    return;
  }
  try {
    const registration = await withTimeout(ensureServiceWorkerRegistration(), 8000, "Service Worker準備");
    const subscription = await withTimeout(registration?.pushManager?.getSubscription(), 5000, "既存Push購読確認");
    if (!subscription) {
      setNotificationDetail("この端末のPush購読はありません。");
      await refreshPushStatus({ updateDetail: false });
      return;
    }
    const endpoint = subscription.endpoint || "";
    const subscriptionId = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(endpoint))
      .then((buffer) => Array.from(new Uint8Array(buffer)).slice(0, 8).map((byte) => byte.toString(16).padStart(2, "0")).join(""));
    await withTimeout(subscription.unsubscribe(), 5000, "ブラウザPush購読解除");
    await deletePushSubscription(subscriptionId);
    setNotificationDetail("この端末のPush購読を解除しました。");
  } catch (error) {
    setNotificationDetail(`Push解除に失敗しました: ${error.message}`, "warn");
  }
}

async function showLiteNotification(title, body) {
  if (!isNotificationSupported() || Notification.permission !== "granted") {
    return false;
  }
  const options = {
    body: String(body || ""),
    icon: "/lite/icon.png",
    badge: "/lite/badge.png",
    tag: "nexus-ark-lite",
    data: { url: `${window.location.origin}/lite/` }
  };
  try {
    const registration = await ensureServiceWorkerRegistration();
    if (registration?.showNotification) {
      await registration.showNotification(title, options);
    } else {
      const notification = new Notification(title, options);
      notification.onclick = () => {
        notification.close();
        focusLiteWindow();
      };
    }
    return true;
  } catch (error) {
    setSyncStatus(`通知を表示できませんでした: ${error.message}`, "warn");
    return false;
  }
}

async function testLiteNotification() {
  if (!state.connected || !state.roomId) {
    setSyncStatus("Push通知テストにはAPI接続が必要です。", "warn");
    setNotificationDetail("Push通知テストにはAPI接続が必要です。", "warn");
    return;
  }
  if (Notification.permission !== "granted") {
    const granted = await requestNotificationPermission();
    if (!granted) {
      return;
    }
  }
  try {
    setNotificationDetail("Web Pushテスト送信中...");
    await subscribeWebPush({ updateDetail: true });
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/push/test`, {
      method: "POST",
      body: JSON.stringify({ title: "Nexus Ark Lite", body: "Web Pushテストです。" }),
      timeoutMs: 15000
    });
    const message = response.status === "sent" || response.status === "partial"
      ? `Web Pushテスト送信: subscriptions=${response.subscription_count} sent=${response.sent} failed=${response.failed}`
      : `Web Pushテスト未送信: subscriptions=${response.subscription_count || 0} status=${response.status} detail=${response.detail || "-"}`;
    if (response.status === "sent" || response.status === "partial") {
      setSyncStatus(message);
      setNotificationDetail(message);
    } else {
      setSyncStatus(message, "warn");
      setNotificationDetail(message, "warn");
    }
  } catch (error) {
    setSyncStatus(`Web Pushテストに失敗しました: ${error.message}`, "warn");
    setNotificationDetail(`Web Pushテストに失敗しました: ${error.message}`, "warn");
  }
  await refreshPushStatus({ updateDetail: false }).catch(() => updateNotificationStatus());
}

function renderSecureOriginNotice() {
  if (isSecureVoiceOrigin()) {
    els.secureOriginNotice.hidden = true;
    els.secureOriginNotice.textContent = "";
    return;
  }
  let hostHint = "";
  try {
    const url = new URL(state.apiBase || window.location.origin);
    if (url.hostname.endsWith(".ts.net")) {
      hostHint = `https://${url.host}/lite`;
    } else if (url.hostname.startsWith("100.")) {
      hostHint = "https://<PCのTailscale DNS名>.ts.net/lite";
    }
  } catch {
    hostHint = "";
  }
  els.secureOriginNotice.hidden = false;
  els.secureOriginNotice.textContent = hostHint
    ? `録音にはHTTPSが必要です。音声入力は ${hostHint} で開くと使えます。`
    : "録音にはHTTPSまたはlocalhostが必要です。Tailscale HTTPS URLで開くと音声入力が使えます。";
}

function updateConnectionSummary() {
  try {
    const url = new URL(state.apiBase);
    els.connectionSummaryUrl.textContent = url.host || state.apiBase || "接続設定";
  } catch {
    els.connectionSummaryUrl.textContent = state.apiBase || "接続設定";
  }
}

function headers() {
  const h = { "Content-Type": "application/json" };
  if (state.token) {
    h.Authorization = `Bearer ${state.token}`;
  }
  return h;
}

function authHeaders() {
  const h = {};
  if (state.token) {
    h.Authorization = `Bearer ${state.token}`;
  }
  return h;
}

async function api(path, options = {}) {
  const { timeoutMs, ...fetchOptions } = options;
  const controller = timeoutMs ? new AbortController() : null;
  const timeoutId = timeoutMs
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;
  const response = await fetch(`${state.apiBase}${path}`, {
    ...fetchOptions,
    signal: controller?.signal || fetchOptions.signal,
    headers: {
      ...headers(),
      ...(fetchOptions.headers || {})
    }
  }).catch((error) => {
    if (error.name === "AbortError") {
      throw new Error(`${path} が${timeoutMs / 1000}秒以内に応答しませんでした。`);
    }
    throw error;
  }).finally(() => {
    if (timeoutId) {
      window.clearTimeout(timeoutId);
    }
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${body || response.statusText}`);
  }
  return response.json();
}

async function loadAttachmentImage(img, attachmentPath) {
  try {
    const response = await fetch(`${state.apiBase}/api/v1/assets?path=${encodeURIComponent(attachmentPath)}`, {
      headers: authHeaders()
    });
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    img.src = objectUrl;
    img.dataset.fullSrc = objectUrl;
  } catch {
    img.replaceWith(document.createTextNode("画像を読み込めませんでした。"));
  }
}

async function uploadSelectedImage() {
  const file = els.imageInput.files?.[0];
  if (!file) {
    return null;
  }
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${state.apiBase}/api/v1/rooms/${encodeURIComponent(state.roomId)}/uploads`, {
    method: "POST",
    headers: authHeaders(),
    body: formData
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${body || response.statusText}`);
  }
  return response.json();
}

function pickAudioMimeType() {
  if (!window.MediaRecorder) {
    return "";
  }
  for (const mimeType of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported(mimeType)) {
      return mimeType;
    }
  }
  return "";
}

function setVoiceButton(text, busy = false) {
  els.voiceButton.textContent = text;
  els.voiceButton.disabled = busy;
  els.voiceButton.classList.toggle("is-recording", state.recording);
}

function formatElapsed(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const min = String(Math.floor(seconds / 60)).padStart(2, "0");
  const sec = String(seconds % 60).padStart(2, "0");
  return `${min}:${sec}`;
}

function updateRecordingTimer() {
  if (!state.recordingStartedAt) {
    return;
  }
  const elapsed = Date.now() - state.recordingStartedAt;
  setVoiceButton(`停止 ${formatElapsed(elapsed)} / ${formatElapsed(VOICE_RECORDING_MAX_MS)}`);
}

function startRecordingTimer() {
  state.recordingStartedAt = Date.now();
  updateRecordingTimer();
  clearInterval(state.recordingTimer);
  clearTimeout(state.recordingTimeout);
  state.recordingTimer = setInterval(updateRecordingTimer, 1000);
  state.recordingTimeout = setTimeout(() => {
    if (state.recording && state.mediaRecorder?.state === "recording") {
      setVoiceButton("上限到達", true);
      setSyncStatus("録音上限に達したため、文字起こしを開始します。");
      state.mediaRecorder.stop();
    }
  }, VOICE_RECORDING_MAX_MS);
}

function stopRecordingTimer() {
  clearInterval(state.recordingTimer);
  clearTimeout(state.recordingTimeout);
  state.recordingTimer = null;
  state.recordingTimeout = null;
  state.recordingStartedAt = 0;
}

function appendTranscriptToInput(text) {
  const transcript = String(text || "").trim();
  if (!transcript) {
    return;
  }
  const current = els.messageInput.value.trim();
  els.messageInput.value = current ? `${current}\n${transcript}` : transcript;
  els.messageInput.dispatchEvent(new Event("input"));
  els.messageInput.focus();
}

function stopRecordingStream() {
  for (const track of state.recordingStream?.getTracks?.() || []) {
    track.stop();
  }
  state.recordingStream = null;
}

async function transcribeAudioBlob(blob) {
  if (!state.connected || !state.roomId) {
    throw new Error("APIに接続してください。");
  }
  const extension = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  const formData = new FormData();
  formData.append("file", blob, `voice.${extension}`);
  const response = await fetch(`${state.apiBase}/api/v1/rooms/${encodeURIComponent(state.roomId)}/voice/transcribe`, {
    method: "POST",
    headers: authHeaders(),
    body: formData
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${body || response.statusText}`);
  }
  return response.json();
}

async function synthesizeSpeech(text) {
  if (!state.connected || !state.roomId) {
    throw new Error("APIに接続してください。");
  }
  const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/tts`, {
    method: "POST",
    body: JSON.stringify({ text, mode: state.ttsMode })
  });
  const audioIds = response.audio_ids?.length ? response.audio_ids : [response.audio_id];
  const blobs = [];
  for (const audioId of audioIds) {
    const audioResponse = await fetch(`${state.apiBase}/api/v1/audio?path=${encodeURIComponent(audioId)}`, {
      headers: authHeaders()
    });
    if (!audioResponse.ok) {
      const body = await audioResponse.text();
      throw new Error(`${audioResponse.status} ${body || audioResponse.statusText}`);
    }
    blobs.push(await audioResponse.blob());
  }
  return {
    blobs,
    notice: response.notice || "",
    segmentCount: response.segment_count || blobs.length
  };
}

function playAudioBlob(blob, label) {
  return new Promise((resolve, reject) => {
    const objectUrl = URL.createObjectURL(blob);
    const audio = new Audio(objectUrl);
    let settled = false;
    state.currentAudio = audio;
    const cleanup = () => {
      URL.revokeObjectURL(objectUrl);
      if (state.currentAudio === audio) {
        state.currentAudio = null;
      }
      if (state.stopCurrentPlayback) {
        state.stopCurrentPlayback = null;
      }
    };
    audio.addEventListener("ended", () => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      resolve();
    }, { once: true });
    audio.addEventListener("error", () => {
      if (settled) {
        return;
      }
      settled = true;
      cleanup();
      reject(new Error(`${label || "音声"}を再生できませんでした。`));
    }, { once: true });
    state.stopCurrentPlayback = () => {
      if (settled) {
        return;
      }
      settled = true;
      state.stopRequested = true;
      audio.pause();
      cleanup();
      resolve();
    };
    audio.play().catch((error) => {
      if (!settled) {
        settled = true;
        cleanup();
      }
      reject(error);
    });
  });
}

function setStopAudioVisible(visible) {
  els.stopAudioButton.hidden = !visible;
}

function stopCurrentAudio() {
  state.stopRequested = true;
  if (state.stopCurrentPlayback) {
    state.stopCurrentPlayback();
  } else if (state.currentAudio) {
    state.currentAudio.pause();
    state.currentAudio = null;
  }
  setStopAudioVisible(false);
  setSyncStatus("音声再生を停止しました。");
}

async function playMessageAudio(text, button) {
  const speechText = String(text || "").trim();
  if (!speechText || state.speaking) {
    return;
  }
  state.speaking = true;
  state.stopRequested = false;
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "生成中";
  setSyncStatus("音声を生成中...");
  setStopAudioVisible(true);
  let speech = button._pendingSpeech || null;
  try {
    if (state.currentAudio) {
      state.currentAudio.pause();
      state.currentAudio = null;
    }
    if (!speech) {
      speech = await synthesizeSpeech(speechText);
      button._pendingSpeech = speech;
    }
    button.textContent = speech.blobs.length > 1 ? "再生準備完了" : "再生中";
    if (speech.notice) {
      setSyncStatus(speech.notice);
    }
    for (let index = 0; index < speech.blobs.length; index += 1) {
      const total = speech.blobs.length;
      const label = total > 1 ? `${index + 1}/${total}` : "音声";
      button.textContent = total > 1 ? `再生 ${label}` : "再生中";
      setSyncStatus(total > 1 ? `${label}を再生しています。` : "音声を再生しています。");
      await playAudioBlob(speech.blobs[index], label);
      if (state.stopRequested) {
        break;
      }
    }
    if (!state.stopRequested) {
      button._pendingSpeech = null;
      setSyncStatus(speech.notice || "音声を再生しました。");
    }
  } catch (error) {
    if (error.name === "NotAllowedError" && speech) {
      button._pendingSpeech = speech;
      setSyncStatus("音声生成は完了しました。もう一度「再生」を押してください。");
    } else {
      setSyncStatus(`音声再生に失敗しました: ${error.message}`);
    }
  } finally {
    state.speaking = false;
    state.stopRequested = false;
    state.stopCurrentPlayback = null;
    setStopAudioVisible(false);
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function finishVoiceRecording() {
  const blob = new Blob(state.audioChunks, { type: state.mediaRecorder?.mimeType || "audio/webm" });
  state.audioChunks = [];
  state.mediaRecorder = null;
  stopRecordingStream();
  stopRecordingTimer();
  state.recording = false;
  if (!blob.size) {
    setVoiceButton("録音");
    setSyncStatus("録音できませんでした。もう一度録音してください。", "warn");
    return;
  }

  state.transcribing = true;
  setVoiceButton("処理中", true);
  setSyncStatus("文字起こし中...");
  try {
    const result = await transcribeAudioBlob(blob);
    if (result.uncertain) {
      setSyncStatus(`低信頼候補: ${result.text || "聞き取れませんでした。"}`);
    } else if (result.text) {
      appendTranscriptToInput(result.text);
      setSyncStatus("文字起こししました。送信前に確認してください。");
    } else {
      setSyncStatus("聞き取れませんでした。もう一度録音してください。", "warn");
    }
  } catch (error) {
    setSyncStatus(`文字起こしに失敗しました: ${error.message}。もう一度録音してください。`, "warn");
  } finally {
    state.transcribing = false;
    setVoiceButton("録音");
  }
}

async function toggleVoiceRecording() {
  if (state.transcribing) {
    return;
  }
  if (state.recording && state.mediaRecorder) {
    setVoiceButton("停止中", true);
    state.mediaRecorder.stop();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    renderSecureOriginNotice();
    setSyncStatus("録音にはHTTPSまたはlocalhostが必要です。", "warn");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = pickAudioMimeType();
    state.audioChunks = [];
    state.recordingStream = stream;
    state.mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    state.mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data?.size) {
        state.audioChunks.push(event.data);
      }
    });
    state.mediaRecorder.addEventListener("stop", () => {
      finishVoiceRecording().catch((error) => setSyncStatus(`文字起こしに失敗しました: ${error.message}`));
    });
    state.mediaRecorder.start();
    state.recording = true;
    startRecordingTimer();
    setSyncStatus("録音中...");
  } catch (error) {
    stopRecordingStream();
    stopRecordingTimer();
    state.recording = false;
    setVoiceButton("録音");
    renderSecureOriginNotice();
    setSyncStatus(`録音を開始できませんでした: ${error.message}`, "warn");
  }
}

function createMessageId() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\))/g;
  let cursor = 0;
  for (const match of String(text || "").matchAll(pattern)) {
    if (match.index > cursor) {
      parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
    }
    if (match[2]) {
      const strong = document.createElement("strong");
      strong.textContent = match[2];
      parent.appendChild(strong);
    } else if (match[3] && match[4]) {
      const link = document.createElement("a");
      link.href = match[4];
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = match[3];
      parent.appendChild(link);
    }
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) {
    parent.appendChild(document.createTextNode(text.slice(cursor)));
  }
}

function linkElementsFromMarkdown(text) {
  const links = [];
  const pattern = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  for (const match of String(text || "").matchAll(pattern)) {
    const link = document.createElement("a");
    link.href = match[2];
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = match[1];
    links.push(link);
  }
  return links;
}

function renderMusicCard(text) {
  const card = document.createElement("article");
  card.className = "music-card";
  const lines = String(text || "")
    .replace(/^🛠️\s*/, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const heading = document.createElement("h3");
  heading.textContent = "音楽推薦カード";
  card.appendChild(heading);

  let currentTrack = null;
  for (const line of lines) {
    if (line === "## 音楽推薦カード") {
      continue;
    }
    const sceneMatch = line.match(/^\*\*気分\/場面:\*\*\s*(.+)$/);
    if (sceneMatch) {
      const meta = document.createElement("p");
      meta.className = "music-card-meta";
      meta.textContent = sceneMatch[1];
      card.appendChild(meta);
      continue;
    }
    const reasonMatch = line.match(/^\*\*推薦したい理由:\*\*\s*(.+)$/);
    if (reasonMatch) {
      const reason = document.createElement("p");
      reason.className = "music-card-reason";
      reason.textContent = reasonMatch[1];
      card.appendChild(reason);
      continue;
    }
    if (line.startsWith("※")) {
      const note = document.createElement("p");
      note.className = "music-card-note";
      note.textContent = line;
      card.appendChild(note);
      continue;
    }
    const trackMatch = line.match(/^\d+\.\s+\*\*(.+?)\*\*(?:\s+-\s+(.+))?$/);
    if (trackMatch) {
      currentTrack = document.createElement("section");
      currentTrack.className = "music-track";
      const title = document.createElement("strong");
      title.textContent = trackMatch[1];
      currentTrack.appendChild(title);
      if (trackMatch[2]) {
        const artist = document.createElement("span");
        artist.textContent = trackMatch[2];
        currentTrack.appendChild(artist);
      }
      card.appendChild(currentTrack);
      continue;
    }
    if (currentTrack && line.startsWith("- 理由:")) {
      const reason = document.createElement("p");
      reason.textContent = line.replace(/^- 理由:\s*/, "");
      currentTrack.appendChild(reason);
      continue;
    }
    if (currentTrack && line.startsWith("- 聴く/探す:")) {
      const links = document.createElement("div");
      links.className = "music-links";
      for (const link of linkElementsFromMarkdown(line)) {
        links.appendChild(link);
      }
      currentTrack.appendChild(links);
      continue;
    }
    const paragraph = document.createElement("p");
    appendInlineMarkdown(paragraph, line);
    card.appendChild(paragraph);
  }
  return card;
}

function renderMessageContent(item, text) {
  if (String(text || "").includes("## 音楽推薦カード")) {
    item.classList.add("music-message");
    item.appendChild(renderMusicCard(applyRedactions(text)));
    return;
  }
  item.innerHTML = applyRedactions(text);
}

function appendMessage(role, text) {
  const item = document.createElement("div");
  item.className = `message ${role}`;
  renderMessageContent(item, text);
  if (role === "agent" && String(text || "").trim()) {
    const actions = document.createElement("div");
    actions.className = "message-actions";
    const speakButton = document.createElement("button");
    speakButton.className = "speak-button";
    speakButton.type = "button";
    speakButton.textContent = "再生";
    speakButton.addEventListener("click", () => playMessageAudio(text, speakButton));
    actions.appendChild(speakButton);
    item.appendChild(actions);
  }
  els.messages.appendChild(item);
  els.messages.scrollTop = els.messages.scrollHeight;
  return item;
}

function appendHistoryMessage(message) {
  const item = appendMessage(message.role || "system", message.content || "");
  appendAttachmentImages(item, message.attachments || []);
  return item;
}

function appendAttachmentImages(item, attachments) {
  for (const attachmentPath of attachments || []) {
    const img = document.createElement("img");
    img.className = "message-image";
    img.alt = "添付画像";
    img.addEventListener("click", () => openImageDialog(img.dataset.fullSrc || img.src));
    item.appendChild(img);
    loadAttachmentImage(img, attachmentPath);
  }
  return item;
}

function openImageDialog(src) {
  if (!src) {
    return;
  }
  els.imageDialogImg.src = src;
  els.imageDialog.showModal();
}

function removeMessage(item) {
  if (item && item.parentElement) {
    item.parentElement.removeChild(item);
  }
}

function clearMessages() {
  els.messages.replaceChildren();
}

function renderChatMessages() {
  clearMessages();
  for (const message of state.chatMessages || []) {
    appendHistoryMessage(message);
  }
}

async function loadHistory() {
  if (!state.roomId) {
    return [];
  }
  const history = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/chat/history?limit=12`, { timeoutMs: 15000 });
  state.chatMessages = history.messages || [];
  renderChatMessages();
  updateSendConfirmation(state.chatMessages);
  return state.chatMessages;
}

function updateSendConfirmation(messages) {
  const pending = state.pendingSend;
  if (!pending || pending.roomId !== state.roomId) {
    setSyncStatus("");
    return;
  }
  const sentIndex = messages.findIndex((message) => {
    if (pending.id && message.client_message_id === pending.id) {
      return true;
    }
    return message.role === "user" && String(message.content || "").trim() === pending.message;
  });
  if (sentIndex < 0) {
    updatePendingSendPatch({
      confirmation: "not_found",
      checkedAt: new Date().toISOString(),
      notFoundCount: (Number(pending.notFoundCount) || 0) + 1
    });
    setSyncStatus("前回の送信はまだ履歴で確認できません。");
    return;
  }
  const hasReply = messages.slice(sentIndex + 1).some((message) => message.role === "agent");
  if (hasReply) {
    const shouldNotify = Boolean(pending.notifyOnResponse);
    if (els.messageInput.value.trim() === pending.message) {
      els.messageInput.value = "";
      els.messageInput.style.height = "";
    }
    if (shouldNotify && state.pushSubscriptionCount <= 0) {
      const reply = messages.slice(sentIndex + 1).find((message) => message.role === "agent");
      showLiteNotification("Nexus Ark Lite", responseNotificationBody(reply?.content || ""));
    }
    clearSelectedImage();
    writePendingSend(null);
    setSyncStatus("前回の応答を確認しました。");
    return;
  }
  updatePendingSendPatch({
    confirmation: "sent",
    checkedAt: new Date().toISOString()
  });
  setSyncStatus("前回の送信は記録済みです。応答待ちの可能性があります。");
}

function renderRooms() {
  els.roomSelect.replaceChildren();
  for (const room of state.rooms) {
    const option = document.createElement("option");
    option.value = room.room_id;
    option.textContent = room.display_name || room.room_id;
    els.roomSelect.appendChild(option);
  }
  if (!state.roomId && state.rooms.length) {
    state.roomId = state.rooms[0].room_id;
  }
  if (state.roomId && state.rooms.some((room) => room.room_id === state.roomId)) {
    els.roomSelect.value = state.roomId;
  } else if (state.rooms.length) {
    state.roomId = state.rooms[0].room_id;
    els.roomSelect.value = state.roomId;
  }
  localStorage.setItem("nexusLite.roomId", state.roomId);
}

function setMeter(el, value) {
  el.value = Math.max(0, Math.min(1, Number(value) || 0));
}

function renderStatus(status) {
  els.roomTitle.textContent = status.display_name || status.room_id;
  els.expressionValue.textContent = status.current_expression || "neutral";
  els.arousalValue.textContent = Number(status.arousal ?? 0.5).toFixed(2);
  els.locationValue.textContent = status.current_location || "-";
  setMeter(els.driveBoredom, status.drives?.boredom);
  setMeter(els.driveCuriosity, status.drives?.curiosity);
  setMeter(els.driveGoal, status.drives?.goal_drive);
  setMeter(els.driveRelated, status.drives?.relatedness);
  updatePersonaAvatar(status.profile_image_path);

  const currLoc = status.current_location;
  if (currLoc) {
    for (const option of els.locationSelect.options) {
      const parts = option.textContent.split(" / ");
      const nameOnly = parts[parts.length - 1];
      if (option.value === currLoc || nameOnly === currLoc || option.textContent === currLoc) {
        option.selected = true;
        break;
      }
    }
  }
}

async function updatePersonaAvatar(imagePath) {
  if (!els.personaAvatar) {
    return;
  }
  if (!imagePath) {
    if (els.personaAvatar.src && els.personaAvatar.src.startsWith("blob:")) {
      URL.revokeObjectURL(els.personaAvatar.src);
    }
    els.personaAvatar.hidden = true;
    els.personaAvatar.src = "";
    return;
  }
  try {
    const response = await fetch(`${state.apiBase}/api/v1/assets?path=${encodeURIComponent(imagePath)}`, {
      headers: authHeaders()
    });
    if (!response.ok) {
      throw new Error(response.statusText);
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    if (els.personaAvatar.src && els.personaAvatar.src.startsWith("blob:")) {
      URL.revokeObjectURL(els.personaAvatar.src);
    }
    els.personaAvatar.src = objectUrl;
    els.personaAvatar.hidden = false;
  } catch (error) {
    console.error("Failed to load persona avatar:", error);
    els.personaAvatar.hidden = true;
    els.personaAvatar.src = "";
  }
}

async function refreshStatus() {
  if (!state.roomId || !state.connected) {
    return;
  }
  const status = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/status`, { timeoutMs: 15000 });
  renderStatus(status);
}

function setManagementStatus(text, mode = "idle") {
  els.managementSummaryStatus.textContent = text;
  els.managementSummaryStatus.dataset.mode = mode;
}

function selectedDraft() {
  return state.twitterDrafts.find((draft) => draft.id === els.draftSelect.value) || null;
}

function renderSelectedDraft() {
  const draft = selectedDraft();
  els.draftContent.value = draft?.content || "";
  els.draftContent.disabled = !draft;
  els.draftApproveButton.disabled = !draft;
  els.draftRejectButton.disabled = !draft;
  if (!draft) {
    els.draftMeta.textContent = "承認待ち下書きはありません。";
    return;
  }
  const warningText = draft.warnings?.length ? ` / ${draft.warnings.join(" / ")}` : "";
  const mediaText = draft.media_paths?.length ? ` / 添付 ${draft.media_paths.length}件` : "";
  els.draftMeta.textContent = `${draft.twitter_length}/${draft.limit}${mediaText}${warningText}`;
}

function renderTwitterDrafts(drafts) {
  state.twitterDrafts = drafts || [];
  els.draftSelect.replaceChildren();
  if (!state.twitterDrafts.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "承認待ちなし";
    els.draftSelect.appendChild(option);
    renderSelectedDraft();
    return;
  }
  for (const draft of state.twitterDrafts) {
    const option = document.createElement("option");
    option.value = draft.id;
    const preview = String(draft.content || "").replace(/\s+/g, " ").slice(0, 32);
    option.textContent = `${draft.timestamp ? draft.timestamp.slice(5, 16).replace("T", " ") : draft.id} ${preview}`;
    els.draftSelect.appendChild(option);
  }
  renderSelectedDraft();
}

async function loadTwitterDrafts() {
  const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/twitter/drafts`);
  renderTwitterDrafts(response.drafts || []);
  return response.drafts?.length || 0;
}

function renderLocations(response) {
  els.locationSelect.replaceChildren();
  if (!response.locations?.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "移動先なし";
    els.locationSelect.appendChild(option);
    return;
  }
  for (const location of response.locations || []) {
    const option = document.createElement("option");
    option.value = location.id;
    option.textContent = location.area ? `${location.area} / ${location.name}` : location.name;
    els.locationSelect.appendChild(option);
    if (location.name === response.current_location || location.id === response.current_location) {
      option.selected = true;
    }
  }
}

async function loadLocations() {
  const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/locations`);
  renderLocations(response);
}

function renderAutonomy(response) {
  const stateText = response.enabled ? "通常" : "静か";
  els.autonomyMeta.textContent = `${stateText} / 間隔 ${response.inactivity_minutes}分 / 静穏 ${response.quiet_hours_start}-${response.quiet_hours_end}`;
  els.autonomyQuietButton.disabled = !response.enabled;
  els.autonomyNormalButton.disabled = response.enabled;
}

async function loadAutonomy() {
  const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/autonomy`);
  renderAutonomy(response);
}

function formatNoteDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function renderNote(response) {
  const content = String(response.content || "").trim();
  els.noteViewer.textContent = content || "このノートは空です。";
  const sizeKb = Math.max(0, Number(response.size || 0) / 1024).toFixed(1);
  els.noteMeta.textContent = `${response.title || "ノート"} / 更新 ${formatNoteDate(response.updated_at)} / ${sizeKb}KB`;
}

async function loadNoteHeadings() {
  if (!state.connected || !state.roomId) {
    return;
  }
  els.noteRefreshButton.disabled = true;
  els.noteMeta.textContent = "見出し取得中";
  try {
    const noteType = els.noteTypeSelect.value || "research";
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/notes/${encodeURIComponent(noteType)}?headings_only=true`, {
      timeoutMs: 8000
    });
    const headings = response.headings || [];
    els.noteHeadingSelect.replaceChildren();
    if (!headings.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "（見出しなし）";
      els.noteHeadingSelect.appendChild(option);
      els.noteHeadingSelect.disabled = true;
      els.noteShowSectionButton.disabled = true;
      els.noteViewer.textContent = "見出しが見つかりませんでした。";
    } else {
      const allOption = document.createElement("option");
      allOption.value = "__all__";
      allOption.textContent = `全文表示（${headings.length}件の見出し）`;
      els.noteHeadingSelect.appendChild(allOption);
      for (const heading of headings) {
        const option = document.createElement("option");
        option.value = heading;
        option.textContent = heading.replace(/^#+\s*/, "");
        els.noteHeadingSelect.appendChild(option);
      }
      els.noteHeadingSelect.disabled = false;
      els.noteShowSectionButton.disabled = false;
      els.noteViewer.textContent = "見出しを選んで「表示」を押してください。";
    }
    const sizeKb = Math.max(0, Number(response.size || 0) / 1024).toFixed(1);
    els.noteMeta.textContent = `${response.title || "ノート"} / ${sizeKb}KB / ${headings.length}見出し`;
  } catch (error) {
    els.noteMeta.textContent = "失敗";
    els.noteViewer.textContent = `見出しを読み込めませんでした: ${error.message}`;
  } finally {
    els.noteRefreshButton.disabled = false;
  }
}

async function loadNoteSection() {
  if (!state.connected || !state.roomId) {
    return;
  }
  const selectedHeading = els.noteHeadingSelect.value;
  if (!selectedHeading) {
    return;
  }
  els.noteShowSectionButton.disabled = true;
  els.noteViewer.textContent = "読込中...";
  try {
    const noteType = els.noteTypeSelect.value || "research";
    let url = `/api/v1/rooms/${encodeURIComponent(state.roomId)}/notes/${encodeURIComponent(noteType)}`;
    if (selectedHeading !== "__all__") {
      url += `?heading=${encodeURIComponent(selectedHeading)}`;
    }
    const response = await api(url, { timeoutMs: 15000 });
    const content = String(response.content || "").trim();
    els.noteViewer.textContent = content || "このセクションは空です。";
    const sizeKb = Math.max(0, Number(response.size || 0) / 1024).toFixed(1);
    els.noteMeta.textContent = `${response.title || "ノート"} / 更新 ${formatNoteDate(response.updated_at)} / ${sizeKb}KB`;
  } catch (error) {
    els.noteViewer.textContent = `ノートを読み込めませんでした: ${error.message}`;
  } finally {
    els.noteShowSectionButton.disabled = false;
  }
}

function renderEventNotificationSettings(response) {
  els.eventNotificationEnabled.checked = Boolean(response.enabled);
  state.responsePreviewEnabled = response.response_preview_enabled !== false;
  els.responsePreviewEnabled.checked = state.responsePreviewEnabled;
  els.eventNotificationMinimum.value = response.minimum_importance || "high";
  els.eventNotificationCooldown.value = String(response.default_cooldown_seconds ?? 300);
  els.eventNotificationSourceCooldowns.value = JSON.stringify(response.source_cooldowns || {}, null, 2);
}

async function loadEventNotificationSettings() {
  const response = await api("/api/v1/notifications/events/settings");
  renderEventNotificationSettings(response);
}

function readSourceCooldownsInput() {
  const raw = els.eventNotificationSourceCooldowns.value.trim();
  if (!raw) {
    return {};
  }
  const parsed = JSON.parse(raw);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("送信元別の通知間隔JSONはオブジェクトで入力してください。");
  }
  const normalized = {};
  for (const [source, seconds] of Object.entries(parsed)) {
    const key = String(source || "").trim();
    const value = Number(seconds);
    if (!key || !Number.isFinite(value) || value < 0 || value > 86400) {
      throw new Error("送信元別の通知間隔は 0-86400 秒の数値で入力してください。");
    }
    normalized[key] = Math.trunc(value);
  }
  return normalized;
}

async function saveEventNotificationSettings() {
  els.eventNotificationSaveButton.disabled = true;
  try {
    const cooldown = Number(els.eventNotificationCooldown.value);
    if (!Number.isFinite(cooldown) || cooldown < 0 || cooldown > 86400) {
      throw new Error("既定クールダウン秒は 0-86400 で入力してください。");
    }
    const response = await api("/api/v1/notifications/events/settings", {
      method: "PUT",
      body: JSON.stringify({
        enabled: els.eventNotificationEnabled.checked,
        response_preview_enabled: els.responsePreviewEnabled.checked,
        minimum_importance: els.eventNotificationMinimum.value,
        default_cooldown_seconds: Math.trunc(cooldown),
        source_cooldowns: readSourceCooldownsInput()
      })
    });
    renderEventNotificationSettings(response);
    setNotificationDetail(`通知設定を保存しました: 外部イベント通知 ${response.enabled ? "ON" : "OFF"} / ${response.minimum_importance}以上`);
  } catch (error) {
    setNotificationDetail(`通知設定の保存に失敗しました: ${error.message}`, "warn");
  } finally {
    els.eventNotificationSaveButton.disabled = false;
  }
}

async function refreshManagement({ force = false } = {}) {
  if (!state.connected || !state.roomId || !els.managementDetails.open) {
    return;
  }
  if (state.managementLoaded && !force) {
    return;
  }
  setManagementStatus("読込中", "busy");
  els.draftRefreshButton.disabled = true;
  try {
    const draftCount = await loadTwitterDrafts();
    await loadAutonomy();
    await loadNoteHeadings();
    await loadEventNotificationSettings();
    state.managementLoaded = true;
    setManagementStatus(`下書き ${draftCount}`, "ok");
  } catch (error) {
    setManagementStatus("失敗", "error");
    setSyncStatus(`管理情報の取得に失敗しました: ${error.message}`, "warn");
  } finally {
    els.draftRefreshButton.disabled = false;
  }
}

async function approveSelectedDraft() {
  const draft = selectedDraft();
  if (!draft) {
    return;
  }
  const content = els.draftContent.value.trim();
  if (!content) {
    setSyncStatus("投稿内容が空です。", "warn");
    return;
  }
  if (!window.confirm("この下書きを承認して投稿しますか？")) {
    return;
  }
  els.draftApproveButton.disabled = true;
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/twitter/drafts/${encodeURIComponent(draft.id)}/approve`, {
      method: "POST",
      body: JSON.stringify({
        content,
        reply_to_url: draft.reply_to_url || null,
        media_paths: draft.media_paths || []
      })
    });
    setSyncStatus(response.error || response.detail || "Twitter下書きを処理しました。", response.error ? "warn" : "idle");
    state.managementLoaded = false;
    await refreshManagement({ force: true });
  } catch (error) {
    setSyncStatus(`Twitter承認に失敗しました: ${error.message}`, "warn");
  } finally {
    els.draftApproveButton.disabled = false;
  }
}

async function rejectSelectedDraft() {
  const draft = selectedDraft();
  if (!draft || !window.confirm("この下書きを却下しますか？")) {
    return;
  }
  els.draftRejectButton.disabled = true;
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/twitter/drafts/${encodeURIComponent(draft.id)}/reject`, {
      method: "POST"
    });
    setSyncStatus(response.detail || "Twitter下書きを却下しました。");
    state.managementLoaded = false;
    await refreshManagement({ force: true });
  } catch (error) {
    setSyncStatus(`Twitter却下に失敗しました: ${error.message}`, "warn");
  } finally {
    els.draftRejectButton.disabled = false;
  }
}

async function setSelectedLocation() {
  if (!els.locationSelect.value) {
    return;
  }
  els.locationSelect.disabled = true;
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/location`, {
      method: "POST",
      body: JSON.stringify({ location_id: els.locationSelect.value })
    });
    setSyncStatus("現在地を更新しました。");
    await refreshStatus();
    await loadLocations();
  } catch (error) {
    setSyncStatus(`現在地の更新に失敗しました: ${error.message}`, "warn");
  } finally {
    els.locationSelect.disabled = false;
  }
}

async function setAutonomyPreset(preset) {
  els.autonomyQuietButton.disabled = true;
  els.autonomyNormalButton.disabled = true;
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/autonomy/preset`, {
      method: "POST",
      body: JSON.stringify({ preset })
    });
    renderAutonomy(response);
    setSyncStatus(response.status || "自律行動設定を更新しました。");
    await refreshStatus();
  } catch (error) {
    setSyncStatus(`自律行動設定の更新に失敗しました: ${error.message}`, "warn");
  } finally {
    els.autonomyQuietButton.disabled = false;
    els.autonomyNormalButton.disabled = false;
    renderAutonomy(await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/autonomy`).catch(() => ({
      enabled: preset !== "quiet",
      inactivity_minutes: 120,
      schedule_cooldown_minutes: 60,
      quiet_hours_start: "00:00",
      quiet_hours_end: "07:00"
    })));
  }
}

async function syncNow() {
  if (!state.connected || !state.roomId) {
    return;
  }
  if (els.syncButton) {
    els.syncButton.disabled = true;
  }
  setSyncStatus("履歴を再取得中...");
  try {
    await loadHistory();
    await refreshStatus();
    state.managementLoaded = false;
    setConnectionStatus("接続済み", "ok");
    refreshManagement({ force: true }).catch((error) => setSyncStatus(`管理情報の取得に失敗しました: ${error.message}`, "warn"));
  } finally {
    if (els.syncButton) {
      els.syncButton.disabled = false;
    }
  }
}

async function refreshConnectionExtras() {
  const tasks = [];
  if (Notification.permission === "granted") {
    tasks.push(
      subscribeWebPush().catch((error) => {
        setSyncStatus(`Push購読を保存できませんでした: ${error.message}`, "warn");
      }),
      refreshPushStatus().catch(() => {})
    );
  }
  state.managementLoaded = false;
  tasks.push(refreshManagement({ force: true }));
  await Promise.allSettled(tasks);
}

async function loadPrimaryRoomData() {
  const results = await Promise.allSettled([refreshStatus(), loadHistory(), loadLocations()]);
  const failed = results.filter((result) => result.status === "rejected");
  if (failed.length) {
    const detail = failed.map((result) => result.reason?.message || "unknown").join(" / ");
    setSyncStatus(`状態または履歴の取得に失敗しました: ${detail}`, "warn");
  }
  return failed.length === 0;
}

async function connect({ collapse = true, useInputs = false } = {}) {
  if (useInputs) {
    state.apiBase = normalizeBase(els.apiBaseInput.value) || window.location.origin;
    state.token = els.tokenInput.value.trim();
    localStorage.setItem("nexusLite.apiBase", state.apiBase);
    localStorage.setItem("nexusLite.token", state.token);
  } else {
    state.apiBase = state.apiBase || localStorage.getItem("nexusLite.apiBase") || window.location.origin;
    state.token = state.token || localStorage.getItem("nexusLite.token") || "";
  }
  updateConnectionSummary();
  renderSecureOriginNotice();
  setConnectionStatus("接続中", "busy");
  els.connectButton.disabled = true;
  try {
    state.rooms = await api("/api/v1/rooms", { timeoutMs: 15000 });
    renderRooms();
    state.connected = true;
    setConnectionStatus("接続済み", "ok");
    setSyncStatus("状態と履歴を取得中...");
    const success = await loadPrimaryRoomData();
    if (!success) {
      throw new Error("初期データの取得に一部失敗しました。");
    }
    if (collapse) {
      els.connectionDetails.open = false;
    }
    refreshConnectionExtras().catch((error) => setSyncStatus(`補助情報の取得に失敗しました: ${error.message}`, "warn"));
  } finally {
    els.connectButton.disabled = false;
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const message = els.messageInput.value.trim();
  const selectedFile = els.imageInput.files?.[0] || null;
  if ((!message && !selectedFile) || !state.roomId || state.sending) {
    return;
  }
  const submitKey = `${state.roomId}\n${message}\n${selectedFile?.name || ""}\n${selectedFile?.size || 0}`;
  const now = Date.now();
  if (state.lastSubmitKey === submitKey && now - (state.lastSubmitAt || 0) < RECENT_SUBMIT_GUARD_MS) {
    return;
  }
  state.lastSubmitKey = submitKey;
  state.lastSubmitAt = now;
  if (state.pendingSend && state.pendingSend.roomId === state.roomId) {
    appendMessage("system", "前回の送信結果を確認中です。再送する前に履歴を再取得します。");
    try {
      await syncNow();
    } catch {
      setSyncStatus("前回の送信結果を確認できません。通信状態を確認してください。");
    }
    if (state.pendingSend && state.pendingSend.roomId === state.roomId) {
      if (canReleaseUnconfirmedPending(state.pendingSend)) {
        appendMessage("system", "前回の送信は履歴に見つかりませんでした。保留状態を解除して送信します。");
        writePendingSend(null);
      } else {
        return;
      }
    }
  }
  state.sending = true;
  const clientMessageId = createMessageId();
  writePendingSend({
    id: clientMessageId,
    roomId: state.roomId,
    message,
    file: selectedFileSignature(selectedFile),
    confirmation: "sending",
    notifyOnResponse: document.hidden || !document.hasFocus(),
    sentAt: new Date().toISOString()
  });
  setSyncStatus("送信中...");
  els.messageInput.value = "";
  els.messageInput.style.height = "";
  els.sendButton.disabled = true;
  els.sendButton.textContent = "送信中";
  appendMessage("user", selectedFile ? `${message || "画像を送ります。"}\n[添付: ${selectedFile.name}]` : message);
  const pendingMessage = appendMessage("pending", "考えています...");
  try {
    const uploaded = await uploadSelectedImage();
    const attachments = uploaded ? [uploaded.attachment_id] : [];
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/chat`, {
      method: "POST",
      body: JSON.stringify({
        user_id: "mobile_lite",
        message: message || "添付画像を見てください。",
        source: "mobile_lite",
        stream: false,
        attachments,
        client_message_id: clientMessageId
      })
    });
    removeMessage(pendingMessage);
    clearSelectedImage();
    setSyncStatus("応答を受信しました。");
    await notifyResponseIfWanted(responseNotificationBody(response.reply || ""));
    writePendingSend(null);
    try {
      await loadHistory();
    } catch {
      const agentMessage = appendMessage("agent", response.reply || "（応答なし）");
      appendAttachmentImages(agentMessage, response.attachments || []);
    }
    await refreshStatus();
  } catch (error) {
    removeMessage(pendingMessage);
    els.messageInput.value = message;
    appendMessage("system", "通信が中断されました。↻ ボタンで履歴を再読み込みしてください。");
    setSyncStatus("送信結果を確認できません。↻ ボタンで再読み込みしてください。");
    if (!document.hidden) {
      try {
        await syncNow();
      } catch {
        // 回線復帰前なら、次の画面復帰時に再同期する。
      }
    }
  } finally {
    state.sending = false;
    els.sendButton.disabled = false;
    els.sendButton.textContent = "送信";
    els.messageInput.focus();
  }
}

els.apiBaseInput.value = state.apiBase;
els.tokenInput.value = state.token;
els.ttsModeSelect.value = state.ttsMode === "split" ? "split" : "trim";
updateConnectionSummary();
renderSecureOriginNotice();
els.connectButton.addEventListener("click", () => connect({ collapse: true, useInputs: true }).catch((error) => {
  els.connectButton.disabled = false;
  showConnectionError(error);
}));
const handleRefresh = async () => {
  if (state.syncing) {
    return;
  }
  state.syncing = true;
  try {
    if (!state.connected) {
      await connect({ collapse: true, useInputs: false });
    } else {
      await syncNow();
    }
  } finally {
    state.syncing = false;
  }
};

els.refreshButton.addEventListener("click", () => handleRefresh().catch((error) => showConnectionError(error)));
if (els.syncButton) {
  els.syncButton.addEventListener("click", () => handleRefresh().catch((error) => {
    setSyncStatus("再取得に失敗しました。");
    showConnectionError(error);
  }));
}
els.ttsModeSelect.addEventListener("change", () => {
  state.ttsMode = els.ttsModeSelect.value === "split" ? "split" : "trim";
  localStorage.setItem("nexusLite.ttsMode", state.ttsMode);
});
els.stopAudioButton.addEventListener("click", stopCurrentAudio);
els.roomSelect.addEventListener("change", async () => {
  state.roomId = els.roomSelect.value;
  localStorage.setItem("nexusLite.roomId", state.roomId);
  state.managementLoaded = false;
  await loadPrimaryRoomData();
  refreshConnectionExtras().catch((error) => setSyncStatus(`補助情報の取得に失敗しました: ${error.message}`, "warn"));
});
els.chatForm.addEventListener("submit", sendMessage);
els.voiceButton.addEventListener("click", toggleVoiceRecording);
els.managementDetails.addEventListener("toggle", () => refreshManagement().catch((error) => {
  setManagementStatus("失敗", "error");
  setSyncStatus(`管理情報の取得に失敗しました: ${error.message}`, "warn");
}));
els.draftRefreshButton.addEventListener("click", () => {
  state.managementLoaded = false;
  refreshManagement({ force: true }).catch((error) => setSyncStatus(`Twitter下書きの取得に失敗しました: ${error.message}`, "warn"));
});
els.draftSelect.addEventListener("change", renderSelectedDraft);
els.draftContent.addEventListener("input", () => {
  const draft = selectedDraft();
  if (!draft) {
    return;
  }
  const length = Array.from(els.draftContent.value).length;
  const warningText = draft.warnings?.length ? ` / ${draft.warnings.join(" / ")}` : "";
  els.draftMeta.textContent = `${length}/${draft.limit}${warningText}`;
});
els.draftApproveButton.addEventListener("click", approveSelectedDraft);
els.draftRejectButton.addEventListener("click", rejectSelectedDraft);
els.locationSelect.addEventListener("change", setSelectedLocation);
els.autonomyQuietButton.addEventListener("click", () => setAutonomyPreset("quiet"));
els.autonomyNormalButton.addEventListener("click", () => setAutonomyPreset("normal"));
els.noteRefreshButton.addEventListener("click", loadNoteHeadings);
els.noteTypeSelect.addEventListener("change", loadNoteHeadings);
els.noteShowSectionButton.addEventListener("click", loadNoteSection);
els.notificationEnableButton.addEventListener("click", requestNotificationPermission);
els.notificationTestButton.addEventListener("click", testLiteNotification);
els.notificationUnsubscribeCurrentButton.addEventListener("click", unsubscribeCurrentPushDevice);
els.eventNotificationSaveButton.addEventListener("click", saveEventNotificationSettings);
els.closeImageDialog.addEventListener("click", () => els.imageDialog.close());
els.imageDialog.addEventListener("click", (event) => {
  if (event.target === els.imageDialog) {
    els.imageDialog.close();
  }
});
els.messageInput.addEventListener("input", () => {
  els.messageInput.style.height = "auto";
  els.messageInput.style.height = `${Math.min(140, els.messageInput.scrollHeight)}px`;
});
els.imageInput.addEventListener("change", () => {
  const file = els.imageInput.files?.[0];
  els.attachmentName.textContent = file ? `添付: ${file.name}` : "";
});
els.themeSelect.addEventListener("change", () => {
  state.theme = els.themeSelect.value;
  localStorage.setItem("nexusLite.theme", state.theme);
  applyThemeSettings();
});
els.colorSchemeSelect.addEventListener("change", () => {
  state.colorScheme = els.colorSchemeSelect.value;
  localStorage.setItem("nexusLite.colorScheme", state.colorScheme);
  applyThemeSettings();
});
els.redactionEnabledCheckbox.addEventListener("change", () => {
  state.redactionEnabled = els.redactionEnabledCheckbox.checked;
  localStorage.setItem("nexusLite.redactionEnabled", state.redactionEnabled);
  if (els.redactionSummaryStatus) {
    els.redactionSummaryStatus.textContent = state.redactionEnabled ? "有効" : "オフ";
    els.redactionSummaryStatus.className = state.redactionEnabled ? "status-pill ok" : "status-pill";
  }
  renderChatMessages();
});
els.addRuleButton.addEventListener("click", () => {
  const findVal = els.ruleFindInput.value.trim();
  const replaceVal = els.ruleReplaceInput.value.trim();
  const colorVal = els.ruleColorInput.value;
  
  if (!findVal) {
    alert("元の文字列を入力してください。");
    return;
  }
  
  const exists = state.redactionRules.some(r => r.find === findVal);
  if (exists) {
    alert("既に同じ検索語のルールが存在します。");
    return;
  }
  
  state.redactionRules.push({
    find: findVal,
    replace: replaceVal,
    color: colorVal
  });
  
  localStorage.setItem("nexusLite.redactionRules", JSON.stringify(state.redactionRules));
  els.ruleFindInput.value = "";
  els.ruleReplaceInput.value = "";
  
  renderRulesList();
  renderChatMessages();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden && (state.sending || state.pendingSend)) {
    markPendingResponseNotificationWanted();
  }
  if (!document.hidden && state.connected && state.roomId) {
    syncNow()
      .catch((error) => showConnectionError(error));
  }
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/lite/service-worker.js", { scope: "/lite/" }).catch(() => {});
}

updateNotificationStatus();
applyThemeSettings();
applyRedactionSettings();

if (state.apiBase) {
  connect({ collapse: true, useInputs: false }).catch((error) => showConnectionError(error));
}
