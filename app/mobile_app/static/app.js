import { travelAdapter, withConnectionTimeout, LITE_CONNECTION_TIMEOUT_MS } from "./travel-adapter.js?v=116";
import { parsePairingHandoff } from "./pairing-handoff.js?v=116";
import { liteContinuityState, readableApiError, normalizeHomeApiBase } from "./lite-continuity-state.js?v=116";
import {
  LITE_UPDATE_RESUME_THROTTLE_MS,
  LITE_UPDATE_TIMEOUT_MS,
  LITE_UPDATE_OPERATION_TIMEOUT_MS,
  fetchLiteBuildMetadata,
  isLiteServiceWorkerRegistration,
  liteBuildMetadataUrl,
  liteServiceWorkerPaths,
  liteUpdateBlockers,
} from "./lite-update-state.js?v=116";

const LITE_UI_BUILD = "v116";

function isCloudHostedLite() {
  return !window.location.pathname.startsWith("/lite");
}

function homeDefaultBase() {
  return isCloudHostedLite() ? "" : window.location.origin;
}

function initialHomeBase() {
  const saved = normalizeHomeApiBase(readConnectionValue("nexusLite.apiBase"));
  if (isCloudHostedLite() && saved.replace(/\/$/, "") === window.location.origin) return "";
  return saved || homeDefaultBase();
}

function readConnectionValue(key) {
  for (const storage of [localStorage, sessionStorage]) {
    try {
      const value = storage.getItem(key);
      if (value) return value;
    } catch {
      // 片方の保存領域が利用できない場合も、もう片方を試す。
    }
  }
  return "";
}

function writeConnectionValue(key, value) {
  for (const storage of [localStorage, sessionStorage]) {
    try {
      storage.setItem(key, value);
    } catch {
      // PWA実装によって片方だけ利用できない場合があるため、接続自体は継続する。
    }
  }
}

const state = {
  apiBase: initialHomeBase(),
  token: readConnectionValue("nexusLite.token"),
  rooms: [],
  roomId: localStorage.getItem("nexusLite.roomId") || "",
  connected: false,
  homeConnectPromise: null,
  homeReconnectPromise: null,
  homeConnectionError: "",
  lastHomeReconnectAt: 0,
  connectionsInitialized: false,
  sending: false,
  confirmingSend: false,
  syncing: false,
  itemGenerating: false,
  itemSharing: false,
  statusRefreshContext: null,
  statusSnapshot: null,
  statusReadFailed: false,
  chatLocation: "",
  temporaryLocation: null,
  temporaryLocationVersion: 0,
  temporaryLocationSaved: [],
  temporaryLocationSavedVersion: 0,
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
  historyLimit: 12,
  homeHistoryLoadId: 0,
  activePage: "chat",
  items: [],
  preparedChatAttachments: [],
  selectedChatFiles: [],
  currentNote: null,
  noteEditing: false,
  noteEditable: false,
  noteLoadId: 0,
  pendingSend: readPendingSend(),
  mode: "home",
  travelSession: null,
  currentTravelSession: null,
  returningHome: false,
  travelPersonaId: "",
  travelProfiles: [],
  travelModels: [],
  travelModelLoadId: 0,
  travelHistoryLoadId: 0,
  travelUsageLoadId: 0,
  travelReadinessRevision: 0,
  travelContextRevision: 0,
  travelEntryPromise: null,
  travelRouteChanging: false,
  travelRouteUsable: true,
  travelBudgetStopped: false,
  travelBudgetChanging: false,
  travelBudgetSummary: null,
  travelBudgetContext: null,
  travelBudgetQuote: null,
  homeReachable: false,
  externalAiExportSource: null,
  latestStandbySnapshot: null,
  standbyAutoRefreshing: false,
  deferredInstallPrompt: null,
  liteUpdate: {
    metadata: null,
    available: false,
    checking: false,
    updating: false,
    error: "",
    lastCheckedAt: 0,
    reloadStarted: false,
  },
  connectivity: {
    home: { code: "checking" },
    worker: { code: "checking" },
    device: { code: "checking" },
    standby: { code: "checking" },
  },
};

const VOICE_RECORDING_MAX_MS = 60000;
const RECENT_SUBMIT_GUARD_MS = 3000;
const PENDING_RESEND_GRACE_MS = 3000;
const STATUS_REFRESH_INTERVAL_MS = 15000;
const STANDBY_FRESHNESS_WARNING_MS = 6 * 60 * 60 * 1000;
const STANDBY_AUTO_CHECK_INTERVAL_MS = 60 * 60 * 1000;

const els = {
  chatConnectionPanel: document.querySelector("#chat-connection-panel"),
  chatConnectionMessage: document.querySelector("#chat-connection-message"),
  chatConnectionDetail: document.querySelector("#chat-connection-detail"),
  chatRetryButton: document.querySelector("#chat-retry-button"),
  chatTravelButton: document.querySelector("#chat-travel-button"),
  chatSettingsButton: document.querySelector("#chat-settings-button"),
  chatContext: document.querySelector("#chat-context"),
  chatContextDetails: document.querySelector("#chat-context-details"),
  chatRoomName: document.querySelector("#chat-room-name"),
  chatLocationName: document.querySelector("#chat-location-name"),
  chatConnectionButton: document.querySelector("#chat-connection-button"),
  liteUpdateBanner: document.querySelector("#lite-update-banner"),
  liteUpdateTitle: document.querySelector("#lite-update-title"),
  liteUpdateMessage: document.querySelector("#lite-update-message"),
  liteUpdateButton: document.querySelector("#lite-update-button"),
  chatModeBadge: document.querySelector("#chat-mode-badge"),
  liteModeLabel: document.querySelector("#lite-mode-label"),
  standbyShortcutButton: document.querySelector("#standby-shortcut-button"),
  snapshotFreshness: document.querySelector("#snapshot-freshness"),
  travelReadiness: document.querySelector("#travel-readiness"),
  travelSendFailure: document.querySelector("#travel-send-failure"),
  travelSendFailureDetail: document.querySelector("#travel-send-failure-detail"),
  travelPendingPanel: document.querySelector("#travel-pending-panel"),
  travelPendingStatus: document.querySelector("#travel-pending-status"),
  travelPendingContext: document.querySelector("#travel-pending-context"),
  travelPendingCheck: document.querySelector("#travel-pending-check"),
  travelPendingSettings: document.querySelector("#travel-pending-settings"),
  travelPendingRestore: document.querySelector("#travel-pending-restore"),
  travelPendingExport: document.querySelector("#travel-pending-export"),
  travelPendingTitle: document.querySelector("#travel-pending-title"),
  travelPendingUnknownActions: document.querySelector("#travel-pending-unknown-actions"),
  travelPendingUnknownConfirm: document.querySelector("#travel-pending-unknown-confirm"),
  travelPendingAcknowledge: document.querySelector("#travel-pending-acknowledge"),
  travelPendingManual: document.querySelector("#travel-pending-manual"),
  travelPendingHomeConfirm: document.querySelector("#travel-pending-home-confirm"),
  travelPendingHomeRecovered: document.querySelector("#travel-pending-home-recovered"),
  travelPendingHistory: document.querySelector("#travel-pending-history"),
  travelPendingHistoryTitle: document.querySelector("#travel-pending-history-title"),
  travelPendingHistoryList: document.querySelector("#travel-pending-history-list"),
  homeModeButton: document.querySelector("#home-mode-button"),
  travelModeButton: document.querySelector("#travel-mode-button"),
  returnHomeButton: document.querySelector("#return-home-button"),
  travelReturnStatus: document.querySelector("#travel-return-status"),
  travelWorkerUrl: document.querySelector("#travel-worker-url"),
  travelDeviceName: document.querySelector("#travel-device-name"),
  travelPairingCode: document.querySelector("#travel-pairing-code"),
  pairingHandoffNotice: document.querySelector("#pairing-handoff-notice"),
  travelPairButton: document.querySelector("#travel-pair-button"),
  standbyRefreshButton: document.querySelector("#standby-refresh-button"),
  standbyDataPreset: document.querySelector("#standby-data-preset"),
  standbyDataPresetStatus: document.querySelector("#standby-data-preset-status"),
  standbyDataCustomDetails: document.querySelector("#standby-data-custom-details"),
  standbyIncludeCoreMemoryCheckbox: document.querySelector("#standby-include-core-memory-checkbox"),
  standbyIncludeEpisodicMemoryCheckbox: document.querySelector("#standby-include-episodic-memory-checkbox"),
  standbyEpisodicMemoryDays: document.querySelector("#standby-episodic-memory-days"),
  standbyRecentMessageLimit: document.querySelector("#standby-recent-message-limit"),
  standbyAutoRefreshCheckbox: document.querySelector("#standby-auto-refresh-checkbox"),
  standbyStatus: document.querySelector("#standby-status"),
  installCard: document.querySelector("#install-card"),
  installStatus: document.querySelector("#install-status"),
  installAppButton: document.querySelector("#install-app-button"),
  browserPairingConfirmation: document.querySelector("#browser-pairing-confirmation"),
  browserPairingConfirmationCheckbox: document.querySelector("#browser-pairing-confirmation-checkbox"),
  externalAiExportDetails: document.querySelector("#external-ai-export-details"),
  externalAiPersonaSelect: document.querySelector("#external-ai-persona-select"),
  externalAiDisclosureCheckbox: document.querySelector("#external-ai-disclosure-checkbox"),
  externalAiShowButton: document.querySelector("#external-ai-show-button"),
  externalAiExportStatus: document.querySelector("#external-ai-export-status"),
  externalAiExportResult: document.querySelector("#external-ai-export-result"),
  externalAiExportText: document.querySelector("#external-ai-export-text"),
  externalAiCopyButton: document.querySelector("#external-ai-copy-button"),
  externalAiClearButton: document.querySelector("#external-ai-clear-button"),
  storageContext: document.querySelector("#storage-context"),
  connectionCompactButton: document.querySelector("#connection-compact-button"),
  connectionCompactIndicator: document.querySelector("#connection-compact-indicator"),
  connectionCompactText: document.querySelector("#connection-compact-text"),
  connectionCheckButton: document.querySelector("#connection-check-button"),
  connectionNextButton: document.querySelector("#connection-next-button"),
  connectionWizardNextText: document.querySelector("#connection-wizard-next-text"),
  homeConnectionStep: document.querySelector("#home-connection-step"),
  homeConnectionState: document.querySelector("#home-connection-state"),
  homeConnectionNext: document.querySelector("#home-connection-next"),
  workerConnectionStep: document.querySelector("#worker-connection-step"),
  workerConnectionState: document.querySelector("#worker-connection-state"),
  workerConnectionNext: document.querySelector("#worker-connection-next"),
  deviceConnectionStep: document.querySelector("#device-connection-step"),
  deviceConnectionState: document.querySelector("#device-connection-state"),
  deviceConnectionNext: document.querySelector("#device-connection-next"),
  standbyConnectionStep: document.querySelector("#standby-connection-step"),
  standbyConnectionState: document.querySelector("#standby-connection-state"),
  standbyConnectionNext: document.querySelector("#standby-connection-next"),
  apiBaseInput: document.querySelector("#api-base-input"),
  homeApiUrlCorrection: document.querySelector("#home-api-url-correction"),
  tokenInput: document.querySelector("#token-input"),
  connectButton: document.querySelector("#connect-button"),
  connectionDetails: document.querySelector("#connection-details"),
  connectionWizardDetails: document.querySelector("#connection-wizard-details"),
  travelPreparationDetails: document.querySelector("#travel-preparation-details"),
  connectionSummaryUrl: document.querySelector("#connection-summary-url"),
  refreshButton: document.querySelector("#refresh-button"),
  loadMoreButton: document.querySelector("#load-more-button"),
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
  letterboxSection: document.querySelector("#letterbox-section"),
  letterboxSummaryStatus: document.querySelector("#letterbox-summary-status"),
  letterboxList: document.querySelector("#letterbox-list"),
  letterboxRefreshButton: document.querySelector("#letterbox-refresh-button"),
  letterboxReader: document.querySelector("#letterbox-reader"),
  letterboxReaderTitle: document.querySelector("#letterbox-reader-title"),
  letterboxReaderMeta: document.querySelector("#letterbox-reader-meta"),
  letterboxReaderBody: document.querySelector("#letterbox-reader-body"),
  draftRefreshButton: document.querySelector("#draft-refresh-button"),
  draftSelect: document.querySelector("#draft-select"),
  draftContent: document.querySelector("#draft-content"),
  draftMeta: document.querySelector("#draft-meta"),
  draftMediaGrid: document.querySelector("#draft-media-grid"),
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
  noteViewer: document.querySelector("#note-viewer"),
  noteEditor: document.querySelector("#note-editor"),
  noteEditButton: document.querySelector("#note-edit-button"),
  noteSaveButton: document.querySelector("#note-save-button"),
  noteCancelButton: document.querySelector("#note-cancel-button"),
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
  statusDetails: document.querySelector("#status-details"),
  statusRefreshButton: document.querySelector("#status-refresh-button"),
  statusDataMeta: document.querySelector("#status-data-meta"),
  persistentEmotionsStatus: document.querySelector("#persistent-emotions-status"),
  persistentEmotionsList: document.querySelector("#persistent-emotions-list"),
  locationValue: document.querySelector("#location-value"),
  driveBoredom: document.querySelector("#drive-boredom"),
  driveCuriosity: document.querySelector("#drive-curiosity"),
  driveGoal: document.querySelector("#drive-goal"),
  driveRelated: document.querySelector("#drive-related"),
  messages: document.querySelector("#messages"),
  newMessageButton: document.querySelector("#new-message-button"),
  chatForm: document.querySelector("#chat-form"),
  messageInput: document.querySelector("#message-input"),
  imageInput: document.querySelector("#image-input"),
  itemButton: document.querySelector("#item-button"),
  voiceButton: document.querySelector("#voice-button"),
  attachmentName: document.querySelector("#attachment-name"),
  redactionShortcutButton: document.querySelector("#redaction-shortcut-button"),
  redactionDialog: document.querySelector("#redaction-dialog"),
  closeRedactionDialog: document.querySelector("#close-redaction-dialog"),
  imageDialog: document.querySelector("#image-dialog"),
  imageDialogImg: document.querySelector("#image-dialog-img"),
  itemDialog: document.querySelector("#item-dialog"),
  closeItemDialog: document.querySelector("#close-item-dialog"),
  itemTargetSelect: document.querySelector("#item-target-select"),
  itemSelect: document.querySelector("#item-select"),
  itemPreview: document.querySelector("#item-preview"),
  itemActionSelect: document.querySelector("#item-action-select"),
  itemAmountInput: document.querySelector("#item-amount-input"),
  itemFurnitureField: document.querySelector("#item-furniture-field"),
  itemFurnitureInput: document.querySelector("#item-furniture-input"),
  itemDetail: document.querySelector("#item-detail"),
  itemRefreshButton: document.querySelector("#item-refresh-button"),
  itemExecuteButton: document.querySelector("#item-execute-button"),
  menuDetails: document.querySelector("#menu-details"),
  pageTitle: document.querySelector("#page-title"),
  closeImageDialog: document.querySelector("#close-image-dialog"),
  sendButton: document.querySelector("#send-button"),
  travelRoutePanel: document.querySelector("#travel-route-panel"),
  travelCurrentRoute: document.querySelector("#travel-current-route"),
  travelProfileSelect: document.querySelector("#travel-profile-select"),
  travelModelSelect: document.querySelector("#travel-model-select"),
  travelModelRefreshButton: document.querySelector("#travel-model-refresh-button"),
  travelModelStatus: document.querySelector("#travel-model-status"),
  travelRouteApplyButton: document.querySelector("#travel-route-apply-button"),
  travelRouteStatus: document.querySelector("#travel-route-status"),
  travelUsageKnown: document.querySelector("#travel-usage-known"),
  travelUsageBudget: document.querySelector("#travel-usage-budget"),
  travelUsageWarning: document.querySelector("#travel-usage-warning"),
  travelBudgetAccess: document.querySelector("#travel-budget-access"),
  travelBudgetOpen: document.querySelector("#travel-budget-open"),
  travelBudgetNotice: document.querySelector("#travel-budget-notice"),
  travelBudgetDialog: document.querySelector("#travel-budget-dialog"),
  travelBudgetTotals: document.querySelector("#travel-budget-totals"),
  travelBudgetQuote: document.querySelector("#travel-budget-quote"),
  travelBudgetScope: document.querySelector("#travel-budget-scope"),
  travelBudgetCurrent: document.querySelector("#travel-budget-current"),
  travelBudgetDaily: document.querySelector("#travel-budget-daily"),
  travelBudgetSession: document.querySelector("#travel-budget-session"),
  travelBudgetConfirm: document.querySelector("#travel-budget-confirm"),
  travelBudgetResult: document.querySelector("#travel-budget-result"),
  travelBudgetRefresh: document.querySelector("#travel-budget-refresh"),
  travelBudgetSave: document.querySelector("#travel-budget-save"),
  travelBudgetClose: document.querySelector("#travel-budget-close"),
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

function draftStorageKey(roomId = state.roomId) {
  return `nexusLite.draft.${roomId || "default"}`;
}

function saveComposerDraft() {
  const value = els.messageInput.value;
  if (value) {
    localStorage.setItem(draftStorageKey(), value);
  } else {
    localStorage.removeItem(draftStorageKey());
  }
}

function restoreComposerDraft() {
  ++state.noteLoadId;
  state.selectedChatFiles = [];
  els.imageInput.value = "";
  if (state.pendingSend?.roomId === state.roomId && state.pendingSend.message) {
    els.messageInput.value = state.pendingSend.message;
    state.preparedChatAttachments = state.pendingSend.attachments || [];
  } else {
    els.messageInput.value = localStorage.getItem(draftStorageKey()) || "";
    state.preparedChatAttachments = [];
  }
  // 表示の復元をユーザー入力扱いにせず、別に保存済みの下書きも保持する。
  resizeComposer();
  renderComposerAttachments();
}

function setActivePage(page) {
  const normalized = ["chat", "records", "management", "settings"].includes(page) ? page : "chat";
  state.activePage = normalized;
  document.body.dataset.activePage = normalized;
  const labels = { records: "記録", management: "管理", settings: "設定" };
  if (els.pageTitle) {
    els.pageTitle.textContent = labels[normalized] || "";
  }
  for (const button of document.querySelectorAll("[data-nav-page]")) {
    const active = button.dataset.navPage === normalized;
    button.classList.toggle("active", active);
    button.toggleAttribute("aria-current", active);
  }
  if (normalized !== "chat") {
    els.menuDetails.open = true;
    els.managementDetails.open = true;
    refreshManagement({ force: true }).catch((error) => setSyncStatus(`管理情報の取得に失敗しました: ${error.message}`, "warn"));
    if (normalized === "records") {
      refreshLetterbox();
    }
  }
  window.scrollTo({ top: 0, behavior: "auto" });
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
    size: Number(file.size || 0),
    lastModified: Number(file.lastModified || 0)
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

async function notifyResponseIfWanted(message, pending = state.pendingSend) {
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
  state.selectedChatFiles = [];
  state.preparedChatAttachments = [];
  renderComposerAttachments();
}

function renderComposerAttachments() {
  els.attachmentName.replaceChildren();
  const addAttachment = (label, remove) => {
    const chip = document.createElement("span");
    chip.className = "attachment-chip";
    const name = document.createElement("span");
    name.textContent = label;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "×";
    button.setAttribute("aria-label", `${label}の添付を取り消す`);
    button.disabled = state.sending || state.confirmingSend || state.itemSharing;
    button.addEventListener("click", () => {
      if (state.sending || state.confirmingSend || state.itemSharing) return;
      remove();
      renderComposerAttachments();
    });
    chip.append(name, button);
    els.attachmentName.appendChild(chip);
  };
  state.selectedChatFiles.forEach((file, index) => addAttachment(file.name, () => {
    state.selectedChatFiles.splice(index, 1);
  }));
  state.preparedChatAttachments.forEach((path, index) => addAttachment(`アイテム画像 ${index + 1}`, () => {
    state.preparedChatAttachments.splice(index, 1);
  }));
  els.attachmentName.hidden = !els.attachmentName.childElementCount;
}

function setComposerAttachmentControls() {
  const busy = state.sending || state.confirmingSend || state.itemSharing;
  const disabled = state.mode === "travel" || busy;
  els.imageInput.disabled = disabled;
  els.itemButton.disabled = disabled;
  if (state.mode === "home") {
    els.messageInput.disabled = busy;
    els.roomSelect.disabled = busy;
    els.sendButton.disabled = busy;
  }
  renderComposerAttachments();
  applyTravelSessionControls();
}

function openRedactionSettings() {
  els.redactionDetails.open = true;
  els.redactionDialog.showModal();
}

function updateRedactionShortcut() {
  els.redactionShortcutButton.textContent = "マスク";
  els.redactionShortcutButton.classList.toggle("is-active", state.redactionEnabled);
  els.redactionShortcutButton.setAttribute("aria-label", `文字置き換え（マスク）設定を開く・${state.redactionEnabled ? "有効" : "オフ"}`);
  els.redactionShortcutButton.title = state.redactionEnabled ? "文字置き換え（マスク）: 有効" : "文字置き換え（マスク）: オフ";
}

function normalizeBase(value) {
  return normalizeHomeApiBase(value);
}

function showHomeApiUrlCorrection(raw, normalized) {
  const corrected = normalized && String(raw || "").trim() !== normalized;
  els.homeApiUrlCorrection.hidden = !corrected;
  els.homeApiUrlCorrection.textContent = corrected
    ? "本体接続用のAPI URLに整えました。画面用の末尾 /lite は付けず、入力欄のURLを使います。"
    : "";
}

function setConnectionStatus(text, mode = "idle") {
  els.connectionStatus.textContent = text;
  els.connectionStatus.dataset.mode = mode;
  renderRoomStatusFreshness();
}

function setSyncStatus(text, mode = "idle") {
  els.syncStatus.textContent = text || "";
  els.syncStatus.dataset.mode = mode;
}

function setTravelReturnStatus(text, mode = "idle") {
  els.travelReturnStatus.textContent = text || "";
  els.travelReturnStatus.dataset.mode = mode;
  els.travelReturnStatus.hidden = !text;
}

function currentLiteUpdateBlockers() {
  let travelPending = null;
  try {
    travelPending = travelAdapter.inspectPending?.() || null;
  } catch {
    travelPending = { state: "corrupt" };
  }
  return liteUpdateBlockers({
    state: { ...state, syncing: state.syncing || state.itemGenerating || Boolean(state.homeReconnectPromise || state.homeConnectPromise) },
    travelPending,
    selectedChatFiles: state.selectedChatFiles,
    preparedChatAttachments: state.preparedChatAttachments,
  });
}

function saveLiteDraft() {
  if (state.mode === "travel") {
    travelAdapter.saveDraft(els.messageInput.value);
  } else {
    saveComposerDraft();
  }
}

function renderLiteUpdateBanner() {
  if (!els.liteUpdateBanner || !els.liteUpdateButton) return;
  const update = state.liteUpdate;
  if (!update.available && !update.updating && !update.error) {
    els.liteUpdateBanner.hidden = true;
    return;
  }
  const blockers = currentLiteUpdateBlockers();
  els.liteUpdateBanner.hidden = false;
  els.liteUpdateBanner.dataset.mode = update.error || update.updating ? "warn" : blockers.blocked ? "hold" : "ready";
  if (update.updating) {
    els.liteUpdateTitle.textContent = "画面を更新しています";
    els.liteUpdateMessage.textContent = "この画面を閉じずに、しばらくお待ちください。";
    els.liteUpdateButton.textContent = "更新中…";
    els.liteUpdateButton.disabled = true;
    return;
  }
  if (update.error) {
    els.liteUpdateTitle.textContent = "画面を更新できませんでした";
    els.liteUpdateMessage.textContent = update.error;
    els.liteUpdateButton.textContent = "画面を更新";
    els.liteUpdateButton.disabled = false;
    return;
  }
  els.liteUpdateTitle.textContent = "画面を更新できます";
  if (blockers.blocked) {
    els.liteUpdateMessage.textContent = `処理が終わってから「画面を更新」できます（${blockers.reasons[0]}）。`;
    els.liteUpdateButton.textContent = "画面を更新";
  } else {
    els.liteUpdateMessage.textContent = "下書きと接続設定を保ったまま更新します。";
    els.liteUpdateButton.textContent = "画面を更新";
  }
  els.liteUpdateButton.disabled = false;
}

async function checkLiteBuildManifest({ force = false, timeoutMs = LITE_UPDATE_TIMEOUT_MS } = {}) {
  const update = state.liteUpdate;
  const now = Date.now();
  if (update.checking || (!force && now - update.lastCheckedAt < LITE_UPDATE_RESUME_THROTTLE_MS)) {
    return update.metadata;
  }
  update.checking = true;
  update.lastCheckedAt = now;
  try {
    const metadata = await fetchLiteBuildMetadata({
      url: liteBuildMetadataUrl(window.location.pathname),
      timeoutMs,
    });
    update.metadata = metadata;
    update.available = metadata.ui_build !== LITE_UI_BUILD;
    update.error = "";
    renderLiteUpdateBanner();
    return metadata;
  } catch {
    // 版確認は補助情報。取得できない時も、チャット・接続・独立モードを継続する。
    return null;
  } finally {
    update.checking = false;
    renderLiteUpdateBanner();
  }
}

function liteUpdateRemaining(deadline) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    throw new Error("画面の更新が時間内に完了しませんでした。");
  }
  return Math.min(LITE_UPDATE_TIMEOUT_MS, remaining);
}

async function getLiteServiceWorkerRegistration(deadline = Date.now() + LITE_UPDATE_OPERATION_TIMEOUT_MS) {
  if (!("serviceWorker" in navigator)) return null;
  const paths = liteServiceWorkerPaths(window.location.pathname);
  const scopeUrl = new URL(paths.scope, window.location.origin).href;
  let registration = await withTimeout(
    navigator.serviceWorker.getRegistration(scopeUrl),
    liteUpdateRemaining(deadline),
    "Service Worker登録の確認",
  );
  if (!registration) {
    registration = await withTimeout(
      ensureServiceWorkerRegistration(),
      liteUpdateRemaining(deadline),
      "Service Worker登録の準備",
    );
  }
  return isLiteServiceWorkerRegistration(registration, window.location.origin, paths)
    ? registration
    : null;
}

function waitForWorkerState(worker, timeoutMs) {
  if (!worker || worker.state === "activated") return Promise.resolve();
  if (worker.state === "redundant") return Promise.reject(new Error("Service Workerの更新に失敗しました。"));
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      worker.removeEventListener("statechange", onStateChange);
      reject(new Error("Service Workerの有効化を確認できませんでした。"));
    }, timeoutMs);
    const onStateChange = () => {
      if (worker.state === "activated") {
        window.clearTimeout(timeoutId);
        worker.removeEventListener("statechange", onStateChange);
        resolve();
      } else if (worker.state === "redundant") {
        window.clearTimeout(timeoutId);
        worker.removeEventListener("statechange", onStateChange);
        reject(new Error("Service Workerの更新に失敗しました。"));
      }
    };
    worker.addEventListener("statechange", onStateChange);
    onStateChange();
  });
}

async function waitForLiteServiceWorkerUpdate(registration, deadline = Date.now() + LITE_UPDATE_OPERATION_TIMEOUT_MS) {
  await withTimeout(registration.update(), liteUpdateRemaining(deadline), "Service Worker更新の確認");
  const workerDeadline = Math.min(deadline, Date.now() + LITE_UPDATE_TIMEOUT_MS);
  let worker = registration.installing || registration.waiting;
  while (!worker && Date.now() < workerDeadline) {
    await new Promise((resolve) => window.setTimeout(resolve, Math.min(50, workerDeadline - Date.now())));
    worker = registration.installing || registration.waiting;
  }
  if (worker) {
    await waitForWorkerState(worker, Math.max(1, liteUpdateRemaining(deadline)));
  }
}

function setConnectivityStep(key, code, mode, label, next) {
  state.connectivity[key] = { code, mode, label, next };
  const prefix = `${key}Connection`;
  els[`${prefix}Step`].dataset.mode = mode;
  els[`${prefix}State`].textContent = label;
  els[`${prefix}Next`].textContent = next;
  renderConnectivityNextAction();
  renderConnectivityCompact();
}

function renderChatContextName(name = "") {
  const selected = els.roomSelect.selectedOptions[0];
  const label = name || selected?.textContent || "ルームを選択";
  els.chatRoomName.textContent = label;
  els.chatRoomName.title = label;
  renderChatContextLocation();
}

function renderChatContextLocation() {
  const current = state.mode === "home" ? state.chatLocation : "";
  const option = [...els.locationSelect.options].find((item) =>
    item.value === current || item.textContent.split(" / ").at(-1) === current);
  const virtualLabel = current ? option?.textContent || current : "";
  const temporary = state.mode === "home" ? state.temporaryLocation : null;
  const appliedName = String(temporary?.location_name || "").trim();
  const label = state.mode !== "home" ? "" : temporary?.active
    ? appliedName
      ? `📍 ${appliedName}${temporary.has_image ? "（写真あり）" : ""}`
      : `📍 一時的現在地を適用中${temporary.has_image ? "（写真あり）" : ""}`
    : virtualLabel;
  els.chatLocationName.textContent = label;
  els.chatLocationName.title = label;
  els.chatLocationName.hidden = !label;
  const detail = document.getElementById("chat-location-detail");
  detail.hidden = state.mode !== "home";
  detail.textContent = state.mode !== "home" ? "" : [
    `仮想現在地：${virtualLabel || "未確認"}`,
    temporary ? temporary.active
      ? `一時的現在地：適用中${appliedName ? `「${appliedName}」` : ""}${temporary.has_image ? "（写真あり）" : "（写真なし）"}\n${temporary.scenery_text || ""}`
      : `一時的現在地：未適用${appliedName ? `「${appliedName}」` : ""}${temporary.has_image ? "（写真を保存済み）" : ""}`
      : "一時的現在地：状態を未確認",
  ].join("\n");
  detail.style.whiteSpace = "pre-line";
  if (state.mode === "home" && !temporary && virtualLabel) {
    els.chatLocationName.textContent = `${virtualLabel}｜一時状態は未確認`;
    els.chatLocationName.title = els.chatLocationName.textContent;
  }
}

function renderChatConnectivity() {
  const travel = state.mode === "travel";
  const { home, worker, device, standby } = state.connectivity;
  const homeUnavailable = !travel && home.code !== "connected";
  const checking = home.code === "checking" || Boolean(state.homeReconnectPromise || state.homeConnectPromise);
  const needsSettings = ["missing", "auth_error"].includes(home.code);
  els.chatConnectionButton.hidden = !homeUnavailable;
  els.chatConnectionButton.textContent = checking ? "本体へ再接続中…"
    : needsSettings ? "接続設定を確認" : "本体に接続できません";
  els.chatConnectionButton.setAttribute("aria-label", "本体への接続状態と利用できる操作を開く");
  els.chatModeBadge.hidden = !travel;
  els.chatContext.dataset.connection = travel ? "travel" : homeUnavailable ? "disconnected" : "home";
  if (!homeUnavailable) els.chatConnectionPanel.hidden = true;
  els.chatConnectionButton.setAttribute("aria-expanded", String(!els.chatConnectionPanel.hidden));
  els.chatConnectionMessage.textContent = checking ? "保存済みの設定で本体への接続を確認しています。"
    : home.code === "missing" ? "本体の接続先が未設定です。"
      : home.code === "auth_error" ? "本体の認証または権限を確認できませんでした。接続用Tokenを確認してください。"
        : "本体から応答がありません。接続設定は保持しています。保存済みの会話は引き続き表示できます。";
  els.chatRetryButton.hidden = needsSettings;
  els.chatRetryButton.disabled = checking || state.sending || state.confirmingSend;
  const available = worker.code === "connected" && device.code === "paired"
    && ["ready", "in_use"].includes(standby.code);
  els.chatTravelButton.hidden = !available;
  els.chatTravelButton.disabled = Boolean(state.sending || state.confirmingSend || state.travelEntryPromise);
  els.chatSettingsButton.textContent = needsSettings ? "本体接続設定を開く"
    : standby.code === "returning" ? "帰宅の状態を確認" : "独立モードの準備を確認";
  els.chatSettingsButton.dataset.action = needsSettings ? "home" : "travel";
  els.chatConnectionDetail.textContent = state.homeConnectionError || "";
  els.chatConnectionDetail.closest("details").hidden = !state.homeConnectionError;
}

function renderConnectivityCompact() {
  if (!els.connectionCompactText) return;
  const { home, worker, device, standby } = state.connectivity;
  const checking = [home, worker, device, standby].some((item) => item.code === "checking");
  let text = "接続状態を確認しています";
  let icon = "◌";
  let mode = "checking";

  if (!checking && device.code === "re_pair_required") {
    text = "スマホの再接続が必要です";
    icon = "!";
    mode = "error";
  } else if (!checking && worker.code !== "connected") {
    text = "Lite用クラウドの設定を確認してください";
    icon = "!";
    mode = "warn";
  } else if (!checking && device.code !== "paired") {
    text = "このスマホを接続してください";
    icon = "!";
    mode = "warn";
  } else if (!checking && standby.code === "returning") {
    text = "帰宅処理を再開してください";
    icon = "!";
    mode = "warn";
  } else if (!checking && standby.code === "in_use") {
    text = state.mode === "travel" ? "独立モード使用中です" : "出発済みです。独立モードを開いてください";
    icon = state.mode === "travel" ? "✓" : "!";
    mode = state.mode === "travel" ? "ok" : "warn";
  } else if (!checking && standby.code !== "ready") {
    text = "お出かけ前のデータ準備が必要です";
    icon = "!";
    mode = "warn";
  } else if (!checking && home.code === "connected") {
    text = "接続準備済み";
    icon = "✓";
    mode = "ok";
  } else if (!checking && worker.code === "connected" && device.code === "paired" && standby.code === "ready") {
    text = "独立モードの準備ができています";
    icon = "✓";
    mode = "ok";
  } else if (!checking) {
    text = "本体へ接続できません";
    icon = "!";
    mode = "error";
  }

  els.connectionCompactText.textContent = text;
  els.connectionCompactIndicator.textContent = icon;
  els.connectionCompactButton.dataset.mode = mode;
  renderChatConnectivity();
}

function renderStorageContext() {
  const standalone = isInstalledDisplayMode();
  els.storageContext.textContent = standalone
    ? `現在: インストール済みPWA（UI ${LITE_UI_BUILD} / 接続元 ${window.location.origin}）。通常ブラウザと認証が共有されない場合があります。`
    : `現在: 通常ブラウザ（UI ${LITE_UI_BUILD} / 接続元 ${window.location.origin}）。インストール済みPWAとは認証が共有されない場合があります。`;
}

function renderTravelPairingState(paired, justCompleted = false) {
  els.travelPairButton.textContent = paired ? "ペアリング済み" : "ペアリング";
  els.travelPairButton.disabled = paired;
  if (paired) {
    els.pairingHandoffNotice.hidden = false;
    els.pairingHandoffNotice.textContent = justCompleted
      ? "✓ この画面のペアリングが完了しました。状態カードの「端末」もペアリング済みです。"
      : "✓ この画面はLite用クラウドとペアリング済みです。";
  } else if (els.pairingHandoffNotice.textContent.startsWith("✓")) {
    els.pairingHandoffNotice.hidden = true;
    els.pairingHandoffNotice.textContent = "";
  }
}

function isInstalledDisplayMode() {
  return window.matchMedia?.("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function snapshotAgeLabel(createdAt, now = Date.now()) {
  const timestamp = Date.parse(createdAt || "");
  if (!Number.isFinite(timestamp)) return { text: "お出かけ前データ 保存時刻不明", stale: true };
  const ageMs = Math.max(0, now - timestamp);
  const minutes = Math.floor(ageMs / 60_000);
  let elapsed;
  if (minutes < 1) elapsed = "たった今";
  else if (minutes < 60) elapsed = `${minutes}分前`;
  else if (minutes < 48 * 60) elapsed = `${Math.floor(minutes / 60)}時間前`;
  else elapsed = `${Math.floor(minutes / (24 * 60))}日前`;
  const saved = new Intl.DateTimeFormat("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp));
  return {
    text: `お出かけ前データ 保存済み・${saved}（${elapsed}）${ageMs >= STANDBY_FRESHNESS_WARNING_MS ? "・更新おすすめ" : ""}`,
    stale: ageMs >= STANDBY_FRESHNESS_WARNING_MS,
  };
}

function travelSessionStatus() {
  return String(state.currentTravelSession?.status || state.travelSession?.status || "");
}

function travelSessionBlocksStandby() {
  return liteContinuityState(travelSessionStatus(), state.mode, state.returningHome).blocksStandby;
}

function applyTravelSessionControls() {
  const view = liteContinuityState(travelSessionStatus(), state.mode, state.returningHome);
  els.standbyRefreshButton.disabled = view.blocksStandby || state.returningHome;
  els.standbyRefreshButton.title = view.blocksStandby
    ? view.status === "returning"
      ? "帰宅処理を完了してから、お出かけ前データを更新できます。"
      : "独立モードの使用中です。署名付き帰宅後に更新できます。"
    : "現在の状態をお出かけ前データとして保存します。";
  els.homeModeButton.textContent = view.homeLabel;
  els.homeModeButton.disabled = view.homeDisabled || Boolean(state.sending || state.confirmingSend || state.travelEntryPromise);
  els.travelModeButton.textContent = view.status === "active" && state.mode === "home"
    ? "独立モードを開く" : "独立モード";
  els.travelModeButton.disabled = view.travelDisabled || Boolean(state.sending || state.confirmingSend || state.travelEntryPromise);
  els.returnHomeButton.hidden = true;
}

function rememberLatestStandby(snapshot) {
  if (!snapshot?.created_at) return;
  state.latestStandbySnapshot = {
    created_at: snapshot.created_at,
    generation: snapshot.generation,
  };
  localStorage.setItem("nexusLite.latestStandbySnapshot", JSON.stringify(state.latestStandbySnapshot));
  renderSnapshotFreshness();
}

function forgetLatestStandby() {
  state.latestStandbySnapshot = null;
  localStorage.removeItem("nexusLite.latestStandbySnapshot");
  renderSnapshotFreshness();
}

function renderSnapshotFreshness() {
  if (!els.snapshotFreshness) return;
  const view = liteContinuityState(travelSessionStatus(), state.mode, state.returningHome);
  if (view.blocksStandby) {
    els.snapshotFreshness.textContent = view.freshnessText;
    els.snapshotFreshness.dataset.mode = "warn";
    els.snapshotFreshness.title = view.status === "returning"
      ? "署名付き帰宅を再開してください。"
      : "独立モードで使用中のため、新しいデータは帰宅後に保存できます。";
    return;
  }
  const snapshot = state.latestStandbySnapshot;
  if (!snapshot) {
    els.snapshotFreshness.textContent = "お出かけ前データ 未保存";
    els.snapshotFreshness.dataset.mode = "warn";
    els.snapshotFreshness.title = "本体接続中に、お出かけ前のデータを準備してください。";
    return;
  }
  const age = snapshotAgeLabel(snapshot.created_at);
  els.snapshotFreshness.textContent = age.text;
  els.snapshotFreshness.dataset.mode = age.stale ? "warn" : "ok";
  els.snapshotFreshness.title = `世代${snapshot.generation} / ${snapshot.created_at}`;
}

function openStandbySettings() {
  setActivePage("settings");
  els.travelPreparationDetails.open = true;
  els.standbyStatus.scrollIntoView({ behavior: "smooth", block: "center" });
  window.setTimeout(() => els.standbyStatus.focus(), 250);
}

function installFallbackText() {
  const isiOS = /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (isiOS) return "Safariの共有ボタンから「ホーム画面に追加」を選んでください。追加したLiteを開いてからペアリングします。";
  return "ブラウザのメニューから「アプリをインストール」または「ホーム画面に追加」を選んでください。追加したLiteを開いてからペアリングします。";
}

function renderInstallState() {
  const installed = isInstalledDisplayMode();
  els.browserPairingConfirmation.hidden = installed;
  if (installed) {
    els.installStatus.textContent = "インストール済みPWAです。この画面でペアリングすると持ち出し時にも設定を使えます。";
    els.installAppButton.hidden = true;
    els.browserPairingConfirmationCheckbox.checked = true;
    return;
  }
  els.installAppButton.hidden = false;
  els.installAppButton.textContent = state.deferredInstallPrompt ? "この端末へインストール" : "インストール方法を表示";
  els.installStatus.textContent = "現在は通常ブラウザです。持ち出す場合は、先にインストールして開き直すのがおすすめです。";
}

async function installLiteApp() {
  if (!state.deferredInstallPrompt) {
    window.alert(installFallbackText());
    return;
  }
  const prompt = state.deferredInstallPrompt;
  state.deferredInstallPrompt = null;
  await prompt.prompt();
  const choice = await prompt.userChoice;
  if (choice.outcome === "accepted") {
    els.installStatus.textContent = "インストールを開始しました。ホーム画面のLiteを開いてからペアリングしてください。";
    els.installAppButton.hidden = true;
  } else {
    renderInstallState();
    els.installStatus.textContent = "インストールは行いませんでした。通常ブラウザへペアリングする場合は保存先確認が必要です。";
  }
}

function workerFailureDiagnostic(health) {
  let workerOrigin = "未設定";
  try {
    workerOrigin = new URL(travelAdapter.configuredBase()).origin;
  } catch {
    // 不正URLは「未設定」のまま表示し、元文字列を再表示しない。
  }
  const primary = [health.browser_error, health.browser_message].filter(Boolean).join(": ") || "記録なし";
  const fallback = [health.fallback_error, health.fallback_message].filter(Boolean).join(": ") || "記録なし";
  const response = health.http_status
    ? `HTTP ${health.http_status} / ${health.content_type || "type不明"} / schema ${health.api_schema_version ?? "不明"}`
    : "HTTP応答なし";
  return `UI ${LITE_UI_BUILD} / PWA ${window.location.origin} / Worker ${workerOrigin} / online ${navigator.onLine} / secure ${window.isSecureContext} / ${response} / fetch ${primary} / no-cors ${fallback}`;
}

function renderConnectivityNextAction() {
  if (!els.connectionNextButton) return;
  const { home, worker, device, standby } = state.connectivity;
  let text = "接続状態を確認しています。";
  let label = "";
  let action = "";

  if (home.code === "checking" || worker.code === "checking" || device.code === "checking" || standby.code === "checking") {
    text = "4つの接続状態を確認しています。";
  } else if (worker.code !== "connected") {
    if (worker.code === "pwa_update_required") {
      text = "Lite用クラウドは更新済みです。「画面を更新」で最新版を読み込みます。再ペアリングは不要です。";
      label = "画面を更新";
      action = "pwa_update";
    } else if (worker.code === "cors_rejected") {
      text = `このPWAの接続元 ${window.location.origin} がLite用クラウドで許可されていません。本体側の許可URLと一致させてください。`;
      label = "Lite用クラウド設定を開く";
      action = "worker";
    } else {
      text = worker.code === "worker_update_required"
        ? "Nexus Ark本体からLite用クラウドを更新してください。"
        : "次にLite用クラウドを設定し、接続を確認してください。";
      label = "Lite用クラウド設定を開く";
      action = "worker";
    }
  } else if (device.code !== "paired") {
    text = device.code === "re_pair_required"
      ? "この画面の端末認証は無効です。新しい短期コードで再ペアリングしてください。"
      : "実際に持ち出すこの画面で端末をペアリングしてください。";
    label = device.code === "re_pair_required" ? "再ペアリングする" : "ペアリングする";
    action = "pair";
  } else if (standby.code === "in_use" && state.mode === "home") {
    text = "PCで出発済みです。独立モードを開くと、使用中のお出かけデータで会話できます。出発をやり直す必要はありません。";
    label = "独立モードを開く";
    action = "travel";
  } else if (["in_use", "returning"].includes(standby.code)) {
    text = standby.code === "returning"
      ? "署名付き帰宅が途中です。完了すると、お出かけ前のデータを更新できます。"
      : "独立モードで使用中です。署名付き帰宅後に、お出かけ前のデータを更新できます。";
    label = standby.code === "returning" ? "帰宅を再開" : "署名付き帰宅";
    action = "home_return";
  } else if (home.code !== "connected" && standby.code !== "ready") {
    text = "本体へ接続できないため、この画面ではお出かけ前のデータを準備できません。PC本体で準備するか、本体接続を設定してから準備してください。";
    label = "本体接続設定を開く";
    action = "home";
  } else if (standby.code !== "ready") {
    text = "最後に、本体接続中の状態をお出かけ前のデータとして準備してください。";
    label = "お出かけ前のデータを準備";
    action = "standby";
  } else if (home.code !== "connected") {
    text = "本体へ接続できませんが、独立モードの準備はできています。自動では開始しません。";
    label = "独立モードを確認して開始";
    action = "travel";
  } else {
    text = "準備完了です。本体停止時も、独立モードは確認操作後にだけ開始します。";
  }

  els.connectionWizardNextText.textContent = text;
  els.connectionNextButton.hidden = !action;
  els.connectionNextButton.textContent = label;
  els.connectionNextButton.dataset.action = action;
}

function openConnectionSettings(focusTarget) {
  setActivePage("settings");
  const panel = focusTarget?.closest("details.page-menu-section") || els.connectionWizardDetails;
  panel.open = true;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  window.setTimeout(() => focusTarget?.focus(), 250);
}

function runConnectivityNextAction() {
  const action = els.connectionNextButton.dataset.action;
  if (action === "travel") {
    els.travelModeButton.click();
  } else if (action === "home") {
    openConnectionSettings(els.apiBaseInput);
  } else if (action === "worker") {
    openConnectionSettings(els.travelWorkerUrl);
  } else if (action === "pwa_update") {
    updatePwaShell().catch((error) => setSyncStatus(`画面を更新できませんでした: ${error.message}`, "warn"));
  } else if (action === "pair") {
    openConnectionSettings(els.travelPairingCode);
  } else if (action === "standby") {
    openStandbySettings();
    els.standbyRefreshButton.click();
  } else if (action === "home_return") {
    els.homeModeButton.click();
  }
}

async function updatePwaShell() {
  const deadline = Date.now() + LITE_UPDATE_OPERATION_TIMEOUT_MS;
  await checkLiteBuildManifest({ force: true, timeoutMs: liteUpdateRemaining(deadline) });
  const blockers = currentLiteUpdateBlockers();
  if (blockers.blocked) {
    renderLiteUpdateBanner();
    setSyncStatus(`画面の更新は保留中です。${blockers.reasons[0]}が終わってから再試行してください。`, "warn");
    return false;
  }
  if (state.liteUpdate.reloadStarted || state.liteUpdate.updating) {
    return false;
  }
  try {
    saveLiteDraft();
  } catch (error) {
    state.liteUpdate.error = "下書きを保存できないため、更新を保留しました。";
    renderLiteUpdateBanner();
    setSyncStatus(`画面の更新を保留しました: ${error.message}`, "warn");
    return false;
  }
  state.liteUpdate.updating = true;
  state.liteUpdate.error = "";
  renderLiteUpdateBanner();
  setSyncStatus("画面の最新版を確認しています…");
  try {
    const registration = await getLiteServiceWorkerRegistration(deadline);
    if (!registration) {
      throw new Error("このLiteのService Workerを確認できませんでした。");
    }
    await waitForLiteServiceWorkerUpdate(registration, deadline);
    const finalBlockers = currentLiteUpdateBlockers();
    if (finalBlockers.blocked) {
      state.liteUpdate.updating = false;
      renderLiteUpdateBanner();
      setSyncStatus(`画面の更新は保留中です。${finalBlockers.reasons[0]}が終わってから再試行してください。`, "warn");
      return false;
    }
    liteUpdateRemaining(deadline);
    saveLiteDraft();
    state.liteUpdate.reloadStarted = true;
    const target = new URL(window.location.href);
    target.searchParams.set("lite_update", String(Date.now()));
    window.location.replace(target.href);
    return true;
  } catch (error) {
    state.liteUpdate.updating = false;
    state.liteUpdate.error = "画面を更新できませんでした。下書きと接続設定は保持しています。";
    renderLiteUpdateBanner();
    setSyncStatus(`画面の更新を保留しました: ${error.message}`, "warn");
    return false;
  }
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
  if (state.mode !== "home") {
    setSyncStatus(formatConnectionError(error), "warn");
    return;
  }
  const status = Number(error?.status) || Number(String(error?.message || "").match(/^(401|403)\b/)?.[1]);
  const auth = status === 401 || status === 403;
  state.connected = false;
  state.homeConnectionError = String(error?.message || "応答を確認できませんでした。");
  const code = !state.apiBase ? "missing" : auth ? "auth_error" : "connection_error";
  const message = code === "missing" ? "本体の接続先を設定してください。"
    : auth ? "本体の認証または権限を確認できません。接続設定を確認してください。"
      : "本体から履歴を取得できませんでした。保存済みの会話を表示しています。";
  setConnectionStatus(auth ? "認証・権限を確認してください" : "本体に接続できません", "warn");
  setSyncStatus(message, "warn");
  setConnectivityStep("home", code, "warn", auth ? "認証・権限の確認が必要" : "本体に接続できません",
    auth ? "接続用Tokenを確認してください。" : "保存済み設定で再試行できます。独立モードの準備状況も確認できます。");
}

function canReconnectHome() {
  return state.mode === "home" && !state.travelSession && !state.currentTravelSession
    && !state.travelEntryPromise && !state.returningHome && !state.sending && !state.confirmingSend
    && !state.itemGenerating && !state.itemSharing && !state.recording && !state.transcribing;
}

async function retryHomeConnection({ automatic = false, refresh = false } = {}) {
  if (state.homeReconnectPromise) return state.homeReconnectPromise;
  if (!canReconnectHome()) return false;
  if (automatic && (!state.connectionsInitialized || document.hidden
    || ["missing", "auth_error"].includes(state.connectivity.home.code)
    || Date.now() - state.lastHomeReconnectAt < STATUS_REFRESH_INTERVAL_MS)) return false;
  if (state.connected && !refresh) return true;
  state.lastHomeReconnectAt = Date.now();
  const base = state.apiBase, token = state.token;
  const travelRevision = state.travelContextRevision;
  const current = () => state.mode === "home" && state.apiBase === base && state.token === token
    && state.travelContextRevision === travelRevision && !state.travelEntryPromise
    && !state.travelSession && !state.currentTravelSession && !state.returningHome;
  const operation = (async () => {
    setConnectivityStep("home", "checking", "checking", "再接続中", "保存済みの設定で本体への接続を確認しています。");
    try {
      if (state.connected) {
        await syncNow();
      } else {
        if (!await probeHome()) throw new Error("本体からの応答を確認できませんでした。");
        if (!current() || !canReconnectHome()) return false;
        // 復帰確認でPush登録やお出かけ準備などの書込みを起動しない。
        await connect({ collapse: false, useInputs: false, readOnly: true });
      }
      if (!current()) return false;
      state.homeConnectionError = "";
      setConnectivityStep("home", "connected", "ok", "接続済み", "本体へ接続しています。");
      return true;
    } catch (error) {
      if (current()) showConnectionError(error);
      return false;
    }
  })();
  state.homeReconnectPromise = operation;
  renderChatConnectivity();
  try { return await operation; }
  finally {
    if (state.homeReconnectPromise === operation) state.homeReconnectPromise = null;
    if (current() && !state.connected && state.connectivity.home.code === "checking") {
      setConnectivityStep("home", "connection_error", "warn", "再接続を待機中", "処理が落ち着いてから保存済み設定で再試行します。");
    }
    renderChatConnectivity();
  }
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
  updateRedactionShortcut();
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
    const color = /^#[0-9a-f]{6}$/i.test(rule.color || "") ? rule.color : "";

    const escapedFind = escapeHtml(findStr);
    const escapedReplace = escapeHtml(replaceStr);

    const escapedRegex = escapedFind.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&');
    const regex = new RegExp(escapedRegex, 'g');

    if (color) {
      const replacementHtml = `<span style="background-color: ${color}; color: #ffffff; padding: 2px 4px; border-radius: 3px; font-weight: bold;">${escapedReplace}</span>`;
      result = result.replace(regex, () => replacementHtml);
    } else {
      result = result.replace(regex, () => escapedReplace);
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
  const registration = await withTimeout(
    navigator.serviceWorker.register("/lite/service-worker.js", { scope: "/lite/" }),
    LITE_UPDATE_TIMEOUT_MS,
    "Service Worker登録",
  );
  if (registration.active) {
    return registration;
  }
  const worker = registration.installing || registration.waiting;
  if (!worker) {
    return registration;
  }
  await new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => reject(new Error("Service Workerの有効化が完了しませんでした。")), LITE_UPDATE_TIMEOUT_MS);
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
  if (!state.apiBase) {
    els.connectionSummaryUrl.textContent = "本体接続（未設定）";
    return;
  }
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
  const { timeoutMs: requestedTimeout, ...fetchOptions } = options;
  const timeoutMs = requestedTimeout ?? ((fetchOptions.method || "GET").toUpperCase() === "GET"
    ? LITE_CONNECTION_TIMEOUT_MS : 0);
  const operation = async (signal) => {
    const response = await fetch(`${state.apiBase}${path}`, {
      ...fetchOptions,
      signal,
      headers: { ...headers(), ...(fetchOptions.headers || {}) },
    });
    if (!response.ok) {
      const body = await response.text();
      let code = "";
      try { code = JSON.parse(body)?.detail?.code || ""; } catch { /* 通常のテキストエラー */ }
      const error = new Error(readableApiError(
        response.status, response.statusText, response.headers.get("content-type"), body,
      ));
      error.code = code;
      error.status = response.status;
      throw error;
    }
    return response.json();
  };
  if (!timeoutMs) return operation(fetchOptions.signal);
  try {
    return await withConnectionTimeout(operation, { timeoutMs, signal: fetchOptions.signal });
  } catch (error) {
    if (error.name === "TimeoutError") {
      const timeout = new Error(`${path} が${timeoutMs / 1000}秒以内に応答しませんでした。`);
      timeout.name = "TimeoutError";
      throw timeout;
    }
    throw error;
  }
}

function setLiteMode(mode) {
  const nextMode = mode === "travel" ? "travel" : "home";
  if (nextMode !== state.mode) {
    resetHomeFeatures({ preserveStatus: true });
    state.travelBudgetSummary = null;
    if (els.travelBudgetDialog.open) els.travelBudgetDialog.close();
    state.travelBudgetContext = null;
  }
  state.mode = nextMode;
  renderRoomStatusFreshness();
  renderChatContextLocation();
  document.body.dataset.liteMode = state.mode;
  els.liteModeLabel.textContent = state.mode === "travel" ? "🧳 独立モード" : "🏠 本体接続";
  renderHomeProbeState();
  applyTravelSessionControls();
  els.locationSelect.disabled = state.mode === "travel";
  els.imageInput.disabled = state.mode === "travel" || state.sending;
  els.itemButton.disabled = state.mode === "travel" || state.sending;
  els.voiceButton.disabled = state.mode === "travel";
  renderConnectivityNextAction();
  renderConnectivityCompact();
  renderTravelSendFailure();
  renderLiteUpdateBanner();
}

function renderTravelSendFailure() {
  const failure = state.mode === "travel"
    ? travelAdapter.lastSendFailure(state.travelSession, state.travelPersonaId) : null;
  els.travelBudgetAccess.hidden = state.mode !== "travel" || !state.travelSession;
  const budgetFailure = ["budget_limit_exceeded", "persona_budget_limit_exceeded"].includes(failure?.code);
  els.travelBudgetNotice.textContent = budgetFailure || state.travelBudgetStopped
    ? "予算で送信を停止しました。確認して上限を変更できます。"
    : state.travelBudgetSummary?.warning ? "概算予算の上限に近づいています。" : "";
  els.travelSendFailure.hidden = !failure;
  els.travelSendFailureDetail.textContent = failure
    ? `${failure.message}（確認コード: ${failure.code}${failure.httpStatus ? ` / HTTP ${failure.httpStatus}` : ""}）`
    : "";
  renderTravelFailedMessage();
}

function renderTravelFailedMessage() {
  const failed = state.mode === "travel"
    ? travelAdapter.failedMessage(state.travelSession, state.travelPersonaId) : null;
  if (failed) {
    // 履歴取得に失敗して仮表示が残っていても、同じ送信を二重に見せない。
    els.messages.querySelectorAll(".message.user[data-travel-client-message-id]").forEach((item) => {
      if (item.dataset.travelClientMessageId === failed.clientMessageId) item.remove();
    });
  }
  const previous = els.messages.querySelector(".travel-failed-send");
  if (failed && previous?.dataset.clientMessageId === failed.clientMessageId) return;
  const scrollTop = els.messages.scrollTop;
  const follow = isNearMessageBottom();
  previous?.remove();
  if (!failed) return;
  const item = appendMessage("user", failed.message, { timestamp: failed.createdAt });
  item.classList.add("travel-failed-send");
  item.dataset.clientMessageId = failed.clientMessageId;
  const label = document.createElement("strong");
  label.className = "travel-failed-send-label";
  label.textContent = "送信に失敗しました（会話として未保存）";
  item.prepend(label);
  const note = document.createElement("div");
  note.className = "travel-failed-send-note";
  note.textContent = "この端末にだけ表示しています。自動再送しません。";
  item.appendChild(note);
  if (follow) scrollMessagesToBottom();
  else els.messages.scrollTop = scrollTop;
}

function travelPersonas() {
  const session = state.travelSession;
  if (!session) return [];
  return Array.isArray(session.personas) ? session.personas : [{
    persona_id: session.persona_id,
    persona_display_name: session.persona_display_name || session.persona_id,
    credential_profile_id: session.credential_profile_id,
    provider: session.provider,
    model_id: session.model_id,
    route_epoch: session.route_epoch,
    route_changed_at: session.route_changed_at,
  }];
}

const TRAVEL_PROVIDER_LABELS = {
  gemini: "Gemini",
  openai: "OpenAI",
  anthropic: "Anthropic",
  xai: "xAI",
  openrouter: "OpenRouter",
};

function travelProviderKey(provider) {
  return typeof provider === "string" ? provider.trim().toLowerCase() : "";
}

function travelProviderLabel(provider) {
  const key = typeof provider === "string" ? provider.trim() : "";
  return TRAVEL_PROVIDER_LABELS[key] || key || "AIサービス";
}

function travelProfileDisplayName(profile, fallbackProvider = "") {
  const name = typeof profile?.display_name === "string"
    ? profile.display_name.trim()
    : "";
  if (name) return name;
  const provider = travelProviderLabel(profile?.provider || fallbackProvider);
  return provider === "AIサービス" ? "AI接続" : `${provider}接続`;
}

function travelProfileLabel(profile, fallbackProvider = "") {
  const name = travelProfileDisplayName(profile, fallbackProvider);
  const provider = travelProviderLabel(profile?.provider || fallbackProvider);
  return typeof profile?.display_name === "string" && profile.display_name.trim()
    ? `${name}（${provider}）`
    : name;
}

function currentTravelPersona() {
  return travelPersonas().find((item) => item.persona_id === state.travelPersonaId) || null;
}

function renderTravelCurrentRoute() {
  const route = currentTravelPersona();
  if (!route) {
    els.travelCurrentRoute.textContent = "使用するAIが未設定です";
    return;
  }
  const profile = state.travelProfiles.find(
    (item) => item.credential_profile_id === route.credential_profile_id,
  );
  els.travelCurrentRoute.textContent =
    `${travelProfileLabel(profile, route.provider)} / ${route.model_id || "不明"}`;
}

function selectedTravelModel() {
  return state.travelModels.find((item) => item.model_id === els.travelModelSelect.value) || null;
}

function setTravelRouteControls() {
  const busy = state.sending || state.travelRouteChanging || state.travelBudgetChanging;
  els.roomSelect.disabled = busy;
  els.travelProfileSelect.disabled = busy || state.travelProfiles.length === 0;
  els.travelModelSelect.disabled = busy || state.travelModels.length === 0;
  els.travelModelRefreshButton.disabled = busy || !els.travelProfileSelect.value;
  els.travelRouteApplyButton.disabled = busy || !selectedTravelModel()?.available;
  if (state.mode === "travel") {
    els.sendButton.disabled =
      busy || !state.travelRouteUsable || state.travelBudgetStopped;
  }
}

function travelModelReason(model) {
  if (model.unavailable_reason === "not_text_chat_capable") return "テキスト会話非対応";
  if (model.unavailable_reason === "capability_unverified") return "対応未確認";
  return "";
}

async function loadTravelModels(profileId, refresh = false) {
  const loadId = ++state.travelModelLoadId;
  state.travelModels = [];
  els.travelModelSelect.replaceChildren();
  els.travelModelStatus.textContent = "モデル一覧を確認しています。";
  setTravelRouteControls();
  let body;
  try {
    body = await travelAdapter.models(profileId, refresh);
  } catch (error) {
    if (loadId !== state.travelModelLoadId) return;
    els.travelModelStatus.textContent =
      "モデル一覧を取得できません。現在使用中のAIは変更していません。";
    setTravelRouteControls();
    throw error;
  }
  if (loadId !== state.travelModelLoadId || profileId !== els.travelProfileSelect.value) return;
  state.travelModels = Array.isArray(body.models) ? body.models : [];
  for (const model of state.travelModels) {
    const option = document.createElement("option");
    const reason = travelModelReason(model);
    const price = model.pricing_known ? "料金確認済み" : "料金不明";
    option.value = model.model_id;
    option.disabled = !model.available;
    option.textContent =
      `${model.display_name || model.model_id}${model.display_name && model.display_name !== model.model_id ? ` — ${model.model_id}` : ""}` +
      `${reason ? `（${reason}）` : `（${price}）`}`;
    els.travelModelSelect.append(option);
  }
  const persona = currentTravelPersona();
  const currentProfile = persona?.credential_profile_id === profileId;
  const selectedProfile = state.travelProfiles.find(
    (item) => item.credential_profile_id === profileId,
  );
  const sameProvider = Boolean(
    persona?.provider &&
    selectedProfile?.provider &&
    travelProviderKey(persona.provider) === travelProviderKey(selectedProfile.provider),
  );
  const current = sameProvider
    ? state.travelModels.find((item) => item.model_id === persona.model_id)
    : null;
  const firstAvailable = state.travelModels.find((item) => item.available);
  if (current?.available) els.travelModelSelect.value = current.model_id;
  else if (firstAvailable) els.travelModelSelect.value = firstAvailable.model_id;
  if (currentProfile) {
    state.travelRouteUsable = Boolean(current?.available);
  }
  const source = body.source === "live"
    ? "最新一覧"
    : body.source === "stale"
      ? "保存済み一覧（一時的に更新できません）"
      : "最近確認した一覧";
  const routeWarning = currentProfile && !current?.available
    ? " 現在のモデルを利用できるか確認できないため送信を停止しました。利用できるAIとモデルを選んでください。"
    : "";
  els.travelModelStatus.textContent =
    `${state.travelModels.length}件（${source}）。利用不能・能力未確認モデルは選択できません。${routeWarning}`;
  setTravelRouteControls();
}

function budgetDialogContextCurrent(context) {
  return context && state.mode === "travel" && state.travelSession === context.session
    && state.travelPersonaId === context.personaId && travelAdapter.configuredBase() === context.base;
}

function budgetMoney(value) { return value == null ? "上限なし" : `$${Number(value).toFixed(4)}`; }

function renderTravelBudgetEditor() {
  const context = state.travelBudgetContext;
  if (!budgetDialogContextCurrent(context) || !context.summary) return;
  const selected = els.travelBudgetScope.value === "persona";
  const summary = context.summary;
  const whole = summary.session_summary || summary;
  const source = selected ? summary.persona_budget : whole.budget;
  els.travelBudgetScope.querySelector('[value="persona"]').disabled = !summary.persona_budget;
  if (!source) return;
  els.travelBudgetTotals.textContent = `全体: 今回 ${budgetMoney(whole.known_cost_usd)}・今日 ${budgetMoney(whole.daily_known_cost_usd)}・保留 ${budgetMoney(whole.pending_reserved_usd)}。選択中: 今回 ${budgetMoney(summary.known_cost_usd)}・今日 ${budgetMoney(summary.daily_known_cost_usd)}・保留 ${budgetMoney(summary.pending_reserved_usd)}。`;
  const quote = state.travelBudgetQuote;
  els.travelBudgetQuote.textContent = quote?.sessionId === context.session.travel_session_id && quote?.personaId === context.personaId && Number.isFinite(quote.amount)
    ? `直前に止めた送信の回答用の最大見込み額: ${budgetMoney(quote.amount)}（再送時には再計算）。`
    : "次の送信の見込み額は、送信内容・モデル・回答の長さで変わります。";
  els.travelBudgetCurrent.textContent = `現在の${selected ? "このペルソナ" : "全体"}の上限: 1日 ${budgetMoney(source.daily_limit_usd)} / 今回 ${budgetMoney(source.session_limit_usd)}`;
  els.travelBudgetDaily.value = source.daily_limit_usd ?? "";
  els.travelBudgetSession.value = source.session_limit_usd ?? "";
  els.travelBudgetDaily.disabled = source.daily_limit_usd == null;
  els.travelBudgetSession.disabled = source.session_limit_usd == null;
  els.travelBudgetConfirm.checked = false;
  els.travelBudgetSave.disabled = state.travelBudgetChanging;
}

async function openTravelBudgetEditor() {
  if (state.mode !== "travel" || !state.travelSession || state.travelBudgetChanging) return;
  const context = { session: state.travelSession, personaId: state.travelPersonaId, base: travelAdapter.configuredBase() };
  state.travelBudgetContext = context;
  els.travelBudgetScope.value = travelAdapter.lastSendFailure(context.session, context.personaId)?.code === "persona_budget_limit_exceeded" ? "persona" : "session";
  els.travelBudgetResult.textContent = "予算を確認しています…";
  els.travelBudgetSave.disabled = true;
  if (!els.travelBudgetDialog.open) els.travelBudgetDialog.showModal();
  try {
    const summary = await travelAdapter.usageSummary(context.session, context.personaId);
    if (state.travelBudgetContext !== context || !budgetDialogContextCurrent(context)) return;
    context.summary = summary;
    renderTravelBudgetEditor();
    els.travelBudgetResult.textContent = "変更する上限を入力し、確認欄にチェックしてください。";
  } catch {
    if (state.travelBudgetContext === context) els.travelBudgetResult.textContent = "予算を取得できません。接続を確認して「状態を再確認」を押してください。";
  }
}

async function saveTravelBudgetChange() {
  const context = state.travelBudgetContext;
  if (state.travelBudgetChanging || !budgetDialogContextCurrent(context) || !context.summary) return;
  if (!els.travelBudgetConfirm.checked) { els.travelBudgetResult.textContent = "費用の確認欄にチェックしてください。"; return; }
  const scope = els.travelBudgetScope.value;
  const previous = scope === "persona" ? context.summary.persona_budget : (context.summary.session_summary || context.summary).budget;
  if (!previous) return;
  const limit = (input, old) => old == null ? null : input.value.trim() === "" ? NaN : Number(input.value);
  const limits = { daily_limit_usd: limit(els.travelBudgetDaily, previous.daily_limit_usd), session_limit_usd: limit(els.travelBudgetSession, previous.session_limit_usd) };
  // 既存の無制限はそのまま保持。増額のみ、空欄で制限を消さない。
  if (Object.entries(limits).some(([key, value]) => previous[key] != null && (!Number.isFinite(value) || value < previous[key]))
    || !Object.entries(limits).some(([key, value]) => previous[key] != null && value > previous[key])) {
    els.travelBudgetResult.textContent = "現在以上の金額を入力し、少なくとも一つの上限を増やしてください。"; return;
  }
  state.travelBudgetChanging = true;
  els.travelBudgetSave.disabled = true;
  els.travelBudgetClose.disabled = true;
  els.travelBudgetRefresh.disabled = true;
  els.travelBudgetScope.disabled = true;
  els.travelBudgetResult.textContent = "上限を変更しています…（15秒以内に完了を確認できなければ、状態の再確認へ進みます）";
  setTravelRouteControls();
  try {
    await travelAdapter.changeBudget(context.session, { scope, ...(scope === "persona" ? { persona_id: context.personaId } : {}), confirmed: true,
      expected: { daily_limit_usd: previous.daily_limit_usd, session_limit_usd: previous.session_limit_usd }, limits });
    if (state.travelBudgetContext !== context || !budgetDialogContextCurrent(context)) return;
    await loadTravelUsage();
    if (!budgetDialogContextCurrent(context)) return;
    context.summary = state.travelBudgetSummary;
    renderTravelBudgetEditor();
    els.travelBudgetResult.textContent = "今回の上限を変更しました。自動再送はしません。別の上限も不足する場合は「変更する範囲」を切り替えて確認してください。閉じて文章を確認してから送信できます。";
  } catch (error) {
    if (state.travelBudgetContext !== context || !budgetDialogContextCurrent(context)) return;
    const code = travelAdapter.errorCode(error);
    els.travelBudgetResult.textContent = error.status === 404 || code === "not_found"
      ? "このクラウドは予算変更に未対応です。本体からLite用クラウドとスマホ版を更新してください。"
      : code === "budget_change_conflict" ? "別の画面で予算が変わった可能性があります。状態を再確認して変更してください。"
      : code === "session_message_in_progress" ? "送信中の処理が残っています。送信結果を確認してから変更してください。"
      : "変更の完了を確認できません。自動再試行はしません。状態を再確認してください。";
    context.summary = null;
  } finally {
    state.travelBudgetChanging = false;
    els.travelBudgetSave.disabled = !context.summary;
    els.travelBudgetClose.disabled = false;
    els.travelBudgetRefresh.disabled = false;
    els.travelBudgetScope.disabled = false;
    setTravelRouteControls();
  }
}

async function loadTravelUsage() {
  if (!state.travelSession || !state.travelPersonaId) return;
  const session = state.travelSession;
  const persona = state.travelPersonaId;
  const base = travelAdapter.configuredBase();
  const requestId = state.travelUsageLoadId = (state.travelUsageLoadId || 0) + 1;
  const summary = await travelAdapter.usageSummary(session, persona);
  if (state.mode !== "travel" || state.travelSession !== session || state.travelPersonaId !== persona
    || travelAdapter.configuredBase() !== base || state.travelUsageLoadId !== requestId) return;
  const known = Number(summary.known_cost_usd || 0);
  const pending = Number(summary.pending_reserved_usd || 0);
  const limit = summary.persona_budget
    ? summary.persona_budget.session_limit_usd
    : summary.budget?.session_limit_usd;
  const stopped = summary.stopped === true || summary.session_summary?.stopped === true;
  state.travelBudgetStopped = stopped;
  state.travelBudgetSummary = summary;
  els.travelUsageKnown.textContent =
    `利用額: 約$${known.toFixed(4)}${summary.unknown_price_count ? `・金額不明 ${summary.unknown_price_count}件` : ""}`;
  els.travelUsageBudget.textContent =
    limit == null
      ? `応答用に一時確保: 約$${pending.toFixed(4)} / 上限なし`
      : `応答用に一時確保: 約$${pending.toFixed(4)} / 上限 $${Number(limit).toFixed(4)}`;
  els.travelUsageWarning.textContent = stopped
    ? "予算上限に到達しています。送信を停止します。"
    : summary.warning
      ? "予算上限に近づいています。"
      : "";
  renderTravelSendFailure();
  setTravelRouteControls();
}

async function loadTravelRouteControls() {
  renderTravelCurrentRoute();
  els.travelRouteStatus.textContent = "";
  const body = await travelAdapter.providerProfiles();
  state.travelProfiles = Array.isArray(body.profiles) ? body.profiles : [];
  els.travelProfileSelect.replaceChildren();
  for (const profile of state.travelProfiles) {
    const option = document.createElement("option");
    option.value = profile.credential_profile_id;
    option.disabled = !profile.enabled;
    option.textContent = `${travelProfileLabel(profile)}${profile.enabled ? "" : " — 利用停止中"}`;
    els.travelProfileSelect.append(option);
  }
  renderTravelCurrentRoute();
  const currentProfileId = currentTravelPersona()?.credential_profile_id || "";
  els.travelProfileSelect.value = currentProfileId;
  const currentProfile = state.travelProfiles.find(
    (item) => item.credential_profile_id === currentProfileId,
  );
  if (currentProfile?.enabled) {
    await loadTravelModels(els.travelProfileSelect.value);
  } else {
    els.travelModelStatus.textContent = currentProfile
      ? "現在のAI接続は利用停止中です。利用できるAIサービスとモデルを選んでください。"
      : "現在のAI接続を確認できません。利用できるAIサービスとモデルを選んでください。";
    state.travelRouteUsable = false;
    setTravelRouteControls();
  }
}

async function refreshTravelPersonaView() {
  renderChatContextName();
  renderRoomStatusFreshness();
  state.travelBudgetStopped = false;
  state.travelBudgetSummary = null;
  state.travelRouteUsable = true;
  renderTravelSendFailure();
  await Promise.all([
    loadTravelRouteControls(),
    loadTravelUsage(),
    loadTravelHistory(),
  ]);
}

async function applyTravelRouteChange() {
  if (!state.travelSession || state.sending || state.travelRouteChanging) return;
  const profile = state.travelProfiles.find(
    (item) => item.credential_profile_id === els.travelProfileSelect.value,
  );
  const model = selectedTravelModel();
  if (!profile?.enabled || !model?.available) return;
  const persona = currentTravelPersona();
  if (
    profile.credential_profile_id === persona?.credential_profile_id &&
    model.model_id === persona?.model_id
  ) {
    els.travelRouteStatus.textContent = "すでに現在使用中のAIです。";
    return;
  }
  const next = `${travelProfileLabel(profile)} / ${model.model_id}`;
  const priceWarning = model.pricing_known
    ? ""
    : " このモデルの料金はまだ概算表示に対応していません。利用額には金額不明として記録されます。";
  if (!window.confirm(
    `今回使うAIを ${next} へ変更します。これまでの会話内容は引き継がれます。送信中の内容を自動で送り直すことはありません。${priceWarning} よろしいですか？`,
  )) return;
  state.travelRouteChanging = true;
  els.travelRouteStatus.textContent = "使用するAIを変更しています。";
  setTravelRouteControls();
  try {
    const result = await travelAdapter.changeRoute(
      state.travelSession,
      state.travelPersonaId,
      profile.credential_profile_id,
      model.model_id,
    );
    const routeTarget = Array.isArray(state.travelSession.personas)
      ? persona
      : state.travelSession;
    Object.assign(routeTarget, result.route || {});
    state.travelRouteUsable = true;
    renderTravelCurrentRoute();
    els.travelRouteStatus.textContent = result.changed
      ? `${next} へ切り替えました。`
      : "使用するAIは変更されませんでした。";
    await Promise.all([loadTravelHistory(), loadTravelUsage()]);
  } catch (error) {
    els.travelRouteStatus.textContent = error.message === "session_message_in_progress"
      ? "送信中のため切り替えられません。応答確定後に再試行してください。"
      : "使用するAIを変更できませんでした。現在のAIを維持します。";
  } finally {
    state.travelRouteChanging = false;
    setTravelRouteControls();
  }
}

function renderTravelRooms() {
  const personas = travelPersonas();
  els.roomSelect.replaceChildren();
  for (const persona of personas) {
    const option = document.createElement("option");
    option.value = persona.persona_id;
    option.textContent = persona.persona_display_name || persona.display_name || persona.persona_id;
    els.roomSelect.append(option);
  }
  state.travelPersonaId = personas.some((item) => item.persona_id === state.travelPersonaId)
    ? state.travelPersonaId
    : personas[0]?.persona_id || "";
  els.roomSelect.value = state.travelPersonaId;
  renderChatContextName();
  renderRoomStatusFreshness();
}

async function loadTravelHistory() {
  const session = state.travelSession;
  const sessionId = String(session?.travel_session_id || "");
  const personaId = String(state.travelPersonaId || "");
  if (!session || !sessionId || !personaId) return [];
  const loadId = ++state.travelHistoryLoadId;
  const connectionKey = typeof travelAdapter.connectionKey === "function"
    ? travelAdapter.connectionKey()
    : null;
  const body = await travelAdapter.events(session, personaId);
  if (
    loadId !== state.travelHistoryLoadId
    || state.travelSession !== session
    || String(state.travelSession?.travel_session_id || "") !== sessionId
    || state.travelPersonaId !== personaId
    || (connectionKey !== null
      && (typeof travelAdapter.connectionKey !== "function" || travelAdapter.connectionKey() !== connectionKey))
  ) {
    return state.chatMessages;
  }
  const persona = currentTravelPersona();
  const inherited = Array.isArray(persona?.inherited_recent_messages)
    ? persona.inherited_recent_messages.filter((message) => message?.content).map((message) => ({
      role: message.role === "assistant" ? "agent" : "user",
      content: message.content,
      timestamp: persona.snapshot_created_at || "",
      inherited: true,
    }))
    : [];
  const inheritedHeading = inherited.length ? [{
    role: "system",
    content: "お出かけ前から引き継いだ直近の会話",
    inherited: true,
  }] : [];
  const travelEvents = (body.events || []).filter((event) => event.content).map((event) => ({
    role: event.type === "user_message" ? "user" : event.type === "assistant_message" ? "agent" : "system",
    content: event.type === "route_changed" ? travelRouteMarker(event) : event.content,
    timestamp: event.created_at,
    model: event.model_resolved || event.model_requested || "",
  }));
  state.chatMessages = [...inheritedHeading, ...inherited, ...travelEvents];
  renderChatMessages();
  return state.chatMessages;
}

function travelRouteMarker(event) {
  let provider = event.provider || "不明";
  let model = event.model_resolved || event.model_requested || "不明";
  try {
    const detail = JSON.parse(event.content || "{}");
    provider = detail.provider || provider;
    model = detail.model_id || model;
  } catch {
    // 旧イベントでも安全な列値から表示する。
  }
  return `今回使うAIを ${TRAVEL_PROVIDER_LABELS[provider] || provider} / ${model} へ変更しました。`;
}

async function refreshTravelReadiness() {
  const contextRevision = state.travelContextRevision || 0;
  const requestId = state.travelReadinessRevision = (state.travelReadinessRevision || 0) + 1;
  const base = travelAdapter.configuredBase();
  const current = () => (state.travelContextRevision || 0) === contextRevision
    && state.travelReadinessRevision === requestId && travelAdapter.configuredBase() === base;
  els.travelWorkerUrl.value = travelAdapter.configuredBase();
  const health = await travelAdapter.health().catch((error) => ({
    ok: false,
    error: "worker_unreachable",
    browser_error: String(error?.name || "Error").slice(0, 40),
    browser_message: String(error?.message || "").replace(/[\r\n]+/g, " ").slice(0, 160),
  }));
  if (!current()) return null;
  if (!health.ok) {
    const missing = health.error === "worker_url_missing";
    const pwaUpdate = health.error === "pwa_update_required";
    const workerUpdate = health.error === "worker_update_required";
    const corsRejected = health.error === "cors_rejected";
    els.travelReadiness.textContent = missing
      ? "Lite用クラウド未設定"
      : pwaUpdate
        ? "PWAの更新が必要"
        : workerUpdate
          ? "Lite用クラウド更新が必要"
          : corsRejected
            ? "PWAの接続元が未許可"
          : "Lite用クラウドへ接続できません";
    setConnectivityStep(
      "worker",
      missing ? "missing" : pwaUpdate ? "pwa_update_required" : workerUpdate ? "worker_update_required" : corsRejected ? "cors_rejected" : "unreachable",
      missing ? "warn" : "error",
      missing ? "未設定" : pwaUpdate ? "PWA更新が必要" : workerUpdate ? "クラウド更新が必要" : corsRejected ? "接続元が未許可" : "接続不可",
      missing
        ? "Lite用クラウドのURLを設定してください。"
        : pwaUpdate
          ? "「画面を更新」を押してください。再ペアリングは不要です。"
          : workerUpdate
            ? "本体側でLite用クラウドを更新してください。"
            : corsRejected
              ? `現在のPWA: ${window.location.origin}。本体側の許可URLと一致させてください。`
            : `共有用診断: ${workerFailureDiagnostic(health)}`,
    );
    setConnectivityStep(
      "device",
      travelAdapter.paired() ? "unchecked" : "unpaired",
      "warn",
      travelAdapter.paired() ? "未確認" : "未ペアリング",
      "Lite用クラウド接続後に端末認証を確認します。",
    );
    setConnectivityStep("standby", "blocked", "warn", "確認できません", "Lite用クラウド接続後にお出かけ前のデータを確認します。");
    return { health, snapshots: [], deviceState: travelAdapter.paired() ? "unchecked" : "unpaired" };
  }
  setConnectivityStep("worker", "connected", "ok", "接続済み", "次に端末認証を確認します。");
  if (!travelAdapter.paired()) {
    renderTravelPairingState(false);
    els.travelReadiness.textContent = "Lite用クラウド接続済み・端末未ペアリング";
    setConnectivityStep("device", "unpaired", "warn", "未ペアリング", "この画面で短期ペアリングコードを入力してください。");
    setConnectivityStep("standby", "blocked", "warn", "確認できません", "ペアリング後にお出かけ前のデータを確認します。");
    return { health, snapshots: [], deviceState: "unpaired" };
  }
  renderTravelPairingState(true);
  let standby;
  let currentSession;
  try {
    [standby, currentSession] = await Promise.all([
      travelAdapter.listStandby(),
      travelAdapter.currentSession(),
    ]);
  } catch (error) {
    if (!current()) return null;
    const code = travelAdapter.errorCode(error);
    if (code === "re_pair_required") {
      renderTravelPairingState(false);
      els.travelReadiness.textContent = "再ペアリングが必要";
      els.standbyStatus.textContent = "お出かけ前のデータ: 端末認証を確認できません（再ペアリングが必要）";
      setConnectivityStep("device", "re_pair_required", "error", "再ペアリングが必要", "本体で新しい短期コードを発行し、この画面で入力してください。");
      setConnectivityStep("standby", "auth_blocked", "warn", "認証待ち", "再ペアリング後にお出かけ前のデータを確認します。");
      return { health, snapshots: [], deviceState: "re_pair_required" };
    }
    const workerUnavailable = code === "worker_unreachable";
    els.travelReadiness.textContent = workerUnavailable ? "Lite用クラウドへ接続できません" : "端末認証を確認できません";
    setConnectivityStep(
      "device",
      code || "auth_check_failed",
      "warn",
      "確認できません",
      workerUnavailable ? "Lite用クラウドへの通信を確認して再試行してください。" : "Lite用クラウドの状態を確認して再試行してください。",
    );
    setConnectivityStep("standby", "blocked", "warn", "確認できません", "端末認証の確認後に再試行します。");
    return { health, snapshots: [], deviceState: code || "auth_check_failed" };
  }
  if (!current()) return null;
  setConnectivityStep("device", "paired", "ok", "ペアリング済み", "次にお出かけ前のデータを確認します。");
  const ready = (standby.snapshots || []).filter((item) => item.status === "ready");
  state.currentTravelSession = currentSession || null;
  applyTravelSessionControls();
  renderSnapshotFreshness();
  if (!currentSession && !state.travelSession) {
    setExternalAiExportSource(ready[0] ? { kind: "standby", value: ready[0] } : null);
  }
  if (ready[0]) rememberLatestStandby(ready[0]);
  if (travelSessionBlocksStandby()) {
    const view = liteContinuityState(travelSessionStatus(), state.mode, state.returningHome);
    els.travelReadiness.textContent = view.readinessText;
    els.standbyStatus.textContent = view.standbyStatusText;
    setConnectivityStep(
      "standby",
      view.standbyCode,
      "warn",
      view.standbyLabel,
      view.standbyNext,
    );
    renderSnapshotFreshness();
    return { health, snapshots: ready, deviceState: "paired", currentSession };
  }
  els.travelReadiness.textContent = ready.length ? `お出かけ前データ ${ready.length}件` : "お出かけ前データなし";
  if (ready[0]) {
    const freshness = snapshotAgeLabel(ready[0].created_at);
    const stale = freshness.stale ? "・更新推奨" : "";
    els.standbyStatus.textContent = `お出かけ前のデータ: 世代${ready[0].generation} / ${ready[0].created_at}${stale}`;
    setConnectivityStep(
      "standby",
      "ready",
      stale ? "warn" : "ok",
      `準備済み（世代${ready[0].generation}）${stale}`,
      stale ? "本体接続中に更新すると現在の状態へ近づきます。" : "必要時に独立モードを明示開始できます。",
    );
  } else {
    forgetLatestStandby();
    els.standbyStatus.textContent = "お出かけ前のデータ: 未準備";
    setConnectivityStep("standby", "missing", "warn", "未準備", "本体接続中にお出かけ前のデータを準備してください。");
  }
  return { health, snapshots: ready, deviceState: "paired", currentSession };
}

let travelPendingView = null;
let travelPendingChecking = false;
let travelPendingRefreshPromise = null;
let travelPendingRefreshQueued = false;
let travelPendingMutationPromise = null;
let travelPendingMutationInFlight = false;
let travelPendingViewGeneration = 0;

function travelPendingConnectionKey() {
  try {
    if (typeof travelAdapter.connectionKey === "function") return travelAdapter.connectionKey();
    if (typeof travelAdapter.configuredBase === "function") return travelAdapter.configuredBase();
  } catch {
    // 接続先の読み取りに失敗した確認結果は、現在の画面へ適用しない。
  }
  return null;
}

function invalidateTravelPendingView() {
  travelPendingViewGeneration += 1;
  if (travelPendingRefreshPromise) travelPendingRefreshQueued = true;
}

function travelPendingCheckIsCurrent(check, latestInspection, remote, failed = false) {
  if (check.generation !== travelPendingViewGeneration) return false;
  const currentConnectionKey = travelPendingConnectionKey();
  if (check.connectionKey == null || currentConnectionKey == null) return false;
  if (check.connectionKey !== currentConnectionKey) return false;
  if (latestInspection.raw === check.raw) return true;
  // pendingStatus()が確定状態を確認して同じタブで記録を消した場合だけ、
  // rawの消失を現在の結果として受け入れる。
  return !failed && remote === null && latestInspection.state === "none";
}

function renderTravelPendingRecovery() {
  const view = travelPendingView;
  let archive = [];
  let archiveFailed = false;
  try { archive = travelAdapter.listPendingArchive(); } catch { archiveFailed = true; }
  const active = Boolean(view && view.inspection.state !== "none");
  els.travelPendingPanel.hidden = !active && !archive.length && !archiveFailed;
  if (els.travelPendingPanel.hidden) return;
  // 新しい未確定送信は操作を開示し、保管後は会話へ戻す。同じ状態の再描画では開閉を保つ。
  const recoveryState = active ? "pending" : "archived";
  if (els.travelPendingPanel.dataset.recoveryState !== recoveryState) {
    els.travelPendingPanel.open = active || archiveFailed;
    els.travelPendingPanel.dataset.recoveryState = recoveryState;
  }
  els.travelPendingTitle.textContent = active ? "前回の送信を確認" : "保管した送信記録";
  els.travelPendingHistory.hidden = !archive.length;
  els.travelPendingHistoryTitle.textContent = "保管した送信記録（" + archive.length + "件）";
  els.travelPendingHistoryList.replaceChildren();
  for (const record of archive.slice(-10).reverse()) {
    const row = document.createElement("li");
    const date = Number.isFinite(Date.parse(record.reviewed_at)) ? new Date(record.reviewed_at).toLocaleString() : "日時不明";
    row.textContent = date + " — " + (record.mode === "unknown" ? "結果不明を確認して保管" : "本体での復旧を確認して保管");
    els.travelPendingHistoryList.append(row);
  }
  els.travelPendingManual.hidden = !active;
  els.travelPendingCheck.hidden = !active;
  els.travelPendingSettings.hidden = !active;
  els.travelPendingRestore.hidden = true;
  els.travelPendingUnknownActions.hidden = true;
  if (!active) {
    els.travelPendingStatus.textContent = archiveFailed ? "保管した記録を読み取れません。記録を消さずに保持しています。" : "保管した記録はこの端末に残っています。クラウドの未確定結果や保留中の利用額は変更していません。";
    els.travelPendingContext.textContent = archive.length > 10 ? "最新10件を表示しています。確認用の保存ファイルには全件の確認情報を含みます。" : "";
    return;
  }
  const pending = view.inspection.pending;
  const originMismatch = view.errorCode === "pending_origin_mismatch";
  let message = "送信結果を確認しています。新しい送信は確認後に再開できます。";
  if (view.inspection.state === "corrupt") {
    message = "この端末の送信記録を読み取れません。記録は消さずに保持しています。確認用の情報を保存し、本体側のお出かけ状態も確認してください。";
  } else if (originMismatch) {
    message = "前回の送信先と違うLite用クラウドへ接続しています。元の接続先へ戻し、必要なら端末を再登録してから結果を確認してください。";
  } else if (view.errorCode === "re_pair_required") {
    message = "結果を確認するには端末の再登録が必要です。本体で新しい短期コードを発行し、接続設定で登録してください。";
  } else if (view.httpStatus === 404) {
    message = "この接続先では前回の送信記録を確認できません。接続先と端末登録を確認してください。元の記録は保持しています。";
  } else if (view.errorCode) {
    message = "送信結果を確認できません。接続を確認してから「送信結果を再確認」を押してください。元の文章は自動で再送しません。";
  } else if (view.remote?.status === "outcome_unknown") {
    message = "応答や料金の結果を確定できません。元の送信記録と保留中の利用額は保持されています。";
    if (!pending?.worker_url) message += " 旧形式の記録で元の送信先を確実に照合できないため、本体側で状態を確認してください。";
  } else if (view.remote) {
    message = "前回の送信処理が終わったことをまだ確認できません。しばらくしてから結果を再確認してください。";
    message += " 通信の切断を確認できず未確定のまま残った送信は、処理開始から30分経過後に定期回収の対象になります。元の文章は自動で再送しません。";
    const createdAt = Date.parse(view.remote.reserved_at || pending?.created_at);
    if (Number.isFinite(createdAt)) {
      const minutes = Math.max(0, Math.floor((Date.now() - createdAt) / 60000));
      message += ` 送信から約${minutes}分が経過しています。`;
      if (minutes >= 35) {
        message += " 通常の回収目安を過ぎています。追加送信せず、下の「確認用の記録」を保存して状況を確認してください。定期処理の遅延などで、さらに時間がかかる場合があります。";
      }
    }
  }
  if (!navigator.locks?.request) message += " このブラウザーでは複数タブの同時操作を避けてください。";
  els.travelPendingStatus.textContent = message;
  let host = "";
  try { host = new URL(pending?.worker_url || travelAdapter.configuredBase()).host; } catch {}
  els.travelPendingContext.textContent = host ? "確認する接続先: " + host : "接続先を確認してください。";
  els.travelPendingCheck.disabled = travelPendingChecking || view.inspection.state === "corrupt";
  els.travelPendingSettings.hidden = !(view.errorCode || view.inspection.state === "corrupt");
  els.travelPendingRestore.hidden = !originMismatch || !pending?.worker_url;
  els.travelPendingRestore.disabled = travelPendingChecking;
  const mayAcknowledge = !view.errorCode && view.remote?.status === "outcome_unknown"
    && Boolean(pending?.worker_url && pending.session_id && pending.persona_id);
  els.travelPendingUnknownActions.hidden = !mayAcknowledge;
  els.travelPendingAcknowledge.disabled = travelPendingChecking || !mayAcknowledge || !els.travelPendingUnknownConfirm.checked;
  els.travelPendingHomeRecovered.disabled = travelPendingChecking || !els.travelPendingHomeConfirm.checked;
  els.travelPendingCheck.textContent = travelPendingChecking ? "確認しています…" : "送信結果を再確認";
  if (!travelPendingChecking && view.checkedAt) {
    const checkedAt = new Date(view.checkedAt).toLocaleTimeString();
    els.travelPendingContext.textContent += ` / 最終確認 ${checkedAt}`;
  }
}

async function refreshTravelPendingRecoveryOnce() {
  const inspection = travelAdapter.inspectPending();
  const check = {
    generation: travelPendingViewGeneration,
    connectionKey: travelPendingConnectionKey(),
    raw: inspection.raw,
  };
  els.travelPendingUnknownConfirm.checked = false;
  els.travelPendingHomeConfirm.checked = false;
  travelPendingView = { inspection, remote: null, errorCode: "", httpStatus: 0, checkedAt: new Date().toISOString() };
  if (inspection.state !== "pending") {
    renderTravelPendingRecovery();
    return;
  }
  travelPendingChecking = true;
  let accepted = false;
  try {
    renderTravelPendingRecovery();
    const remote = await travelAdapter.pendingStatus();
    const latestInspection = travelAdapter.inspectPending();
    accepted = travelPendingCheckIsCurrent(check, latestInspection, remote);
    if (!accepted) {
      travelPendingRefreshQueued = true;
      return;
    }
    travelPendingView.remote = remote;
    travelPendingView.inspection = latestInspection;
    if (!remote) {
      if (state.mode === "travel" && state.travelSession?.travel_session_id === inspection.pending.session_id) {
        try {
          await loadTravelHistory();
          await loadTravelUsage();
        } catch {
          setSyncStatus("送信の終了は確認できましたが、履歴と利用額を読み込めません。接続を確認して再読み込みしてください。", "warn");
          return;
        }
      }
      setSyncStatus("前回の送信処理の終了を確認しました。履歴と利用額を確認してから続けられます。", "ok");
    }
  } catch (error) {
    let latestInspection;
    try { latestInspection = travelAdapter.inspectPending(); } catch { latestInspection = { state: "corrupt", raw: null }; }
    accepted = travelPendingCheckIsCurrent(check, latestInspection, undefined, true);
    if (!accepted) {
      travelPendingRefreshQueued = true;
      return;
    }
    travelPendingView.errorCode = travelAdapter.errorCode(error) || "check_failed";
    travelPendingView.httpStatus = Number(error.status || 0);
  } finally {
    if (accepted) travelPendingView.checkedAt = new Date().toISOString();
    travelPendingChecking = false;
    if (accepted) renderTravelPendingRecovery();
  }
}

function refreshTravelPendingRecovery() {
  if (travelPendingMutationInFlight) {
    travelPendingRefreshQueued = true;
    return travelPendingMutationPromise || Promise.resolve();
  }
  if (travelPendingRefreshPromise) {
    travelPendingRefreshQueued = true;
    return travelPendingRefreshPromise;
  }
  const operation = (async () => {
    let passes = 0;
    do {
      travelPendingRefreshQueued = false;
      await refreshTravelPendingRecoveryOnce();
      passes += 1;
    } while (travelPendingRefreshQueued && !travelPendingMutationInFlight && passes < 2);
    if (passes >= 2) travelPendingRefreshQueued = false;
  })();
  const tracked = operation.finally(() => {
    if (travelPendingRefreshPromise === tracked) travelPendingRefreshPromise = null;
  });
  travelPendingRefreshPromise = tracked;
  return tracked;
}

async function archiveTravelPending(mode) {
  const confirmed = mode === "unknown" ? els.travelPendingUnknownConfirm.checked : els.travelPendingHomeConfirm.checked;
  if (!confirmed || travelPendingChecking || travelPendingView?.inspection.raw == null) return;
  travelPendingMutationInFlight = true;
  travelPendingChecking = true;
  let operation;
  operation = (async () => {
    try {
      renderTravelPendingRecovery();
      const previous = travelPendingView.inspection.pending;
      await travelAdapter.archivePending(travelPendingView.inspection.raw, { mode, confirmed });
      if (mode === "home_recovered") {
        if (previous?.session_id === state.travelSession?.travel_session_id) {
          state.travelSession = null;
          state.travelPersonaId = null;
        }
        await refreshTravelReadiness();
        setSyncStatus("記録を保管し、この端末の待機を解除しました。接続状態を確認してモードを選び直してください。", "ok");
      } else {
        setSyncStatus("結果不明の記録を保管しました。元の文章を再送せず、次の操作へ進めます。", "ok");
      }
    } catch (error) {
      setSyncStatus(error.message || "送信記録を保管できませんでした。元の記録は保持しています。", "warn");
    } finally {
      travelPendingChecking = false;
      travelPendingMutationInFlight = false;
      if (travelPendingMutationPromise === operation) travelPendingMutationPromise = null;
      invalidateTravelPendingView();
      await refreshTravelPendingRecovery();
    }
  })();
  travelPendingMutationPromise = operation;
  return operation;
}

function resetTravelContextAfterWorkerChange(previousBase) {
  if (previousBase === travelAdapter.configuredBase()) return;
  invalidateTravelPendingView();
  state.travelContextRevision = (state.travelContextRevision || 0) + 1;
  state.travelSession = null;
  state.currentTravelSession = null;
  state.travelPersonaId = null;
  setExternalAiExportSource(null);
  applyTravelSessionControls();
}

async function restoreTravelPendingOrigin() {
  const inspection = travelAdapter.inspectPending();
  const url = inspection.pending?.worker_url;
  if (!url) return;
  if (!window.confirm("前回送信したLite用クラウドへ接続先を戻します。端末の再登録が必要になる場合があります。続けますか？")) return;
  const previousBase = travelAdapter.configuredBase();
  travelAdapter.configure(url);
  resetTravelContextAfterWorkerChange(previousBase);
  els.travelWorkerUrl.value = travelAdapter.configuredBase();
  await refreshTravelReadiness();
  await refreshTravelPendingRecovery();
}

async function downloadTravelPendingDiagnostic() {
  const inspection = travelAdapter.inspectPending();
  const pending = inspection.pending || {};
  const id = (value) => /^[a-zA-Z0-9_-]{1,200}$/.test(String(value || "")) ? String(value) : null;
  let origin = null;
  try { origin = new URL(pending.worker_url || travelAdapter.configuredBase()).origin; } catch {}
  let archive = [];
  let archiveReadable = true;
  try { archive = travelAdapter.listPendingArchive(); } catch { archiveReadable = false; }
  const record = {
    format: "nexus-lite-pending-diagnostic-v1",
    ui_build: LITE_UI_BUILD,
    archive_readable: archiveReadable,
    saved_at: new Date().toISOString(),
    request_created_at: Number.isFinite(Date.parse(pending.created_at)) ? new Date(pending.created_at).toISOString() : null,
    reserved_at: Number.isFinite(Date.parse(travelPendingView?.remote?.reserved_at)) ? new Date(travelPendingView.remote.reserved_at).toISOString() : null,
    finalized_at: Number.isFinite(Date.parse(travelPendingView?.remote?.finalized_at)) ? new Date(travelPendingView.remote.finalized_at).toISOString() : null,
    last_checked_at: Number.isFinite(Date.parse(travelPendingView?.checkedAt)) ? new Date(travelPendingView.checkedAt).toISOString() : null,
    storage_state: inspection.state,
    worker_origin: origin,
    client_message_id: id(pending.client_message_id),
    session_id: id(pending.session_id),
    status: ["reserved", "provider_started", "completed", "partial", "failed_known", "outcome_unknown"].includes(travelPendingView?.remote?.status) ? travelPendingView.remote.status : null,
    check_error: travelPendingView?.errorCode ? "check_failed" : null,
    http_status: travelPendingView?.httpStatus || null,
    archived: archive.map((item) => {
      let saved = {};
      try { saved = JSON.parse(item.raw); } catch {}
      let savedOrigin = null;
      try { savedOrigin = new URL(item.worker_url).origin; } catch {}
      return { reviewed_at: Number.isFinite(Date.parse(item.reviewed_at)) ? new Date(item.reviewed_at).toISOString() : null,
        mode: ["unknown", "home_recovered"].includes(item.mode) ? item.mode : null, worker_origin: savedOrigin,
        client_message_id: id(saved?.client_message_id), session_id: id(saved?.session_id) };
    }),
  };
  const blob = new Blob([JSON.stringify(record, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "lite-pending-diagnostic.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function reconcileTravelPendingOnStartup() {
  await refreshTravelPendingRecovery();
}

function refreshTravelPendingOnResume() {
  return refreshTravelPendingRecovery().catch(() => {});
}

function setExternalAiExportSource(source) {
  state.externalAiExportSource = source;
  const personas = source?.kind === "session"
    ? (source.value.personas || [{
      persona_id: source.value.persona_id,
      persona_display_name: source.value.persona_display_name,
    }])
    : (source?.value?.personas || []);
  els.externalAiPersonaSelect.replaceChildren();
  for (const persona of personas) {
    const option = document.createElement("option");
    option.value = persona.persona_id;
    option.textContent = persona.persona_display_name || persona.display_name || persona.persona_id;
    els.externalAiPersonaSelect.append(option);
  }
  els.externalAiShowButton.disabled = !personas.length;
  els.externalAiExportStatus.textContent = personas.length
    ? source.kind === "session"
      ? "独立モード開始時点の内容から文面を作れます。"
      : `待機データ 世代${source.value.generation}（${source.value.created_at}）から文面を作れます。`
    : "利用できる準備済みデータがありません。本体接続中に先に準備してください。";
}

function clearExternalAiExport() {
  els.externalAiExportText.value = "";
  els.externalAiExportResult.hidden = true;
  els.externalAiDisclosureCheckbox.checked = false;
}

async function showExternalAiExport() {
  if (!els.externalAiDisclosureCheckbox.checked) {
    throw new Error("外部AIとクリップボードへ渡る内容を確認し、チェックを入れてください。");
  }
  const source = state.externalAiExportSource;
  const personaId = els.externalAiPersonaSelect.value;
  if (!source || !personaId) throw new Error("利用できるペルソナデータがありません。");
  const result = source.kind === "session"
    ? await travelAdapter.externalAiExportFromSession(source.value.travel_session_id, personaId)
    : await travelAdapter.externalAiExportFromStandby(source.value.standby_snapshot_id, personaId);
  els.externalAiExportText.value = result.text || "";
  els.externalAiExportResult.hidden = false;
  els.externalAiExportStatus.textContent = `${result.persona_display_name} / ${result.content_chars}文字。内容を確認してからコピーしてください。`;
}

async function copyExternalAiExport() {
  const text = els.externalAiExportText.value;
  if (!text) throw new Error("先に外部AI用の文面を表示してください。");
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    els.externalAiExportText.focus();
    els.externalAiExportText.select();
    if (!document.execCommand("copy")) {
      throw new Error("自動コピーできませんでした。文面を長押ししてコピーしてください。");
    }
  }
  els.externalAiExportStatus.textContent = "コピーしました。外部AIの最初のメッセージ欄へ貼り付けてください。";
}

function renderHomeProbeState() {
  if (!state.apiBase) {
    setConnectivityStep("home", "missing", "warn", "未設定", "Nexus Ark本体のAPI URLを設定してください。");
  } else if (state.connectivity.home.code === "auth_error") {
    // 到達性の確認やモード変更だけでは、既知の認証エラーを解除しない。
  } else if (state.mode === "travel" || state.travelSession || state.currentTravelSession) {
    // 独立中はhome接続を再開しない。以前の接続成功も認証済みの証拠にはしない。
    if (state.homeReachable) {
      setConnectivityStep("home", "reachable", "idle", "本体へ到達可能", "帰宅時に接続と認証を確認します。独立モード中は自動接続しません。");
    } else {
      setConnectivityStep("home", "unreachable", "error", "接続不可", "スマホから帰宅する時に、本体の起動、API URL、ネットワークを確認してください。");
    }
  } else if (state.homeReachable && state.connected) {
    setConnectivityStep("home", "connected", "ok", "接続済み", "次にLite用クラウドの状態を確認します。");
  } else if (state.homeReachable) {
    setConnectivityStep("home", "reachable", "checking", "再接続を確認中", "保存済み設定で本体へ再接続できます。");
  } else {
    setConnectivityStep("home", "unreachable", "error", "接続不可", "本体の起動、API URL、ネットワークを確認してください。");
  }
}

async function probeHome() {
  if (!state.apiBase) {
    state.homeReachable = false;
    setConnectivityStep("home", "missing", "warn", "未設定", "Nexus Ark本体のAPI URLを設定してください。");
    return false;
  }
  const base = state.apiBase;
  let reachable = false;
  try {
    reachable = await withConnectionTimeout(async (signal) => {
      const response = await fetch(`${base}/health`, { cache: "no-store", signal });
      return response.ok;
    }, { timeoutMs: 3500 });
  } catch {
    reachable = false;
  }
  if (state.apiBase !== base) return false;
  state.homeReachable = reachable;
  renderHomeProbeState();
  return state.homeReachable;
}

async function enterTravelMode() {
  // ボタン連打・起動時復元との競合でも、明示確認とactivationは1系列だけ。
  if (state.travelEntryPromise) return state.travelEntryPromise;
  const contextRevision = state.travelContextRevision || 0;
  state.travelEntryPromise = (async () => {
    if ((state.travelContextRevision || 0) !== contextRevision) return;
    if (state.sending || state.confirmingSend) throw new Error("送信結果を確認してからモードを切り替えてください。");
    const [readiness] = await Promise.all([refreshTravelReadiness(), probeHome()]);
    if ((state.travelContextRevision || 0) !== contextRevision) return;
    if (state.sending || state.confirmingSend) throw new Error("送信結果を確認してからモードを切り替えてください。");
    if (!readiness) return;
    if (!readiness.health.ok) throw new Error("Lite用クラウドへ接続できません。");
    if (readiness.deviceState === "re_pair_required") throw new Error("端末認証が失効しています。新しい短期コードで再ペアリングしてください。");
    if (!travelAdapter.paired()) throw new Error("先に独立モード端末をペアリングしてください。");
    let session = await travelAdapter.currentSession();
    if ((state.travelContextRevision || 0) !== contextRevision) return;
    if (state.sending || state.confirmingSend) throw new Error("送信結果を確認してからモードを切り替えてください。");
    if (!session) {
      const standby = readiness.snapshots[0];
      if (!standby) throw new Error("利用できるお出かけ前のデータがありません。");
      const mode = state.homeReachable ? "planned" : "recovery_unconfirmed";
      const warning = mode === "planned"
        ? "本体へ接続できています。独立モードを開始しますか？"
        : "本体状態を確認できません。home側と分岐する可能性を記録して独立モードを開始しますか？";
      if (!window.confirm(warning)) return;
      await travelAdapter.activate(standby.standby_snapshot_id, mode);
      session = await travelAdapter.currentSession();
    }
    if (!session) throw new Error("独立モードsessionを取得できません。");
    if ((state.travelContextRevision || 0) !== contextRevision) return;
    if (state.sending || state.confirmingSend) throw new Error("送信結果を確認してからモードを切り替えてください。");
    if (session.status === "returning" || state.returningHome) {
      throw new Error("帰宅処理中です。独立モードでの会話を再開せず、帰宅を完了してください。");
    }
    state.travelSession = session;
    state.currentTravelSession = session;
    setExternalAiExportSource({ kind: "session", value: session });
    setLiteMode("travel");
    const activeView = liteContinuityState(travelSessionStatus(), state.mode, state.returningHome);
    els.travelReadiness.textContent = activeView.readinessText;
    els.standbyStatus.textContent = activeView.standbyStatusText;
    setConnectivityStep("standby", activeView.standbyCode, "warn", activeView.standbyLabel, activeView.standbyNext);
    renderTravelRooms();
    els.messageInput.value = travelAdapter.draft();
    try {
      await refreshTravelPersonaView();
    } catch (error) {
      els.travelRouteStatus.textContent =
        "経路または利用額を確認できません。現在の経路は変更していません。";
      await loadTravelHistory();
      setSyncStatus(`独立モードの経路確認に失敗しました: ${error.message}`, "warn");
    }
    setConnectionStatus("Lite用クラウド接続中", "ok");
    if (!els.travelRouteStatus.textContent) {
      setSyncStatus("独立モードです。本体へ自動切替しません。");
    }
  })().finally(() => {
    state.travelEntryPromise = null;
    applyTravelSessionControls();
  });
  applyTravelSessionControls();
  return state.travelEntryPromise;
}

async function enterHomeMode() {
  if (state.sending || state.confirmingSend) throw new Error("送信結果を確認してからモードを切り替えてください。");
  if (!state.homeReachable) throw new Error("本体へまだ接続できません。");
  if (state.travelSession) throw new Error("独立モードを署名付き帰宅してから本体へ戻ってください。");
  setLiteMode("home");
  renderRooms();
  restoreComposerDraft();
  await loadPrimaryRoomData();
  setConnectionStatus("接続済み", "ok");
}

async function pairTravelDevice() {
  const previousBase = travelAdapter.configuredBase();
  travelAdapter.configure(els.travelWorkerUrl.value.trim());
  resetTravelContextAfterWorkerChange(previousBase);
  if (travelAdapter.paired()) {
    throw new Error("このPWAには端末情報が保存済みです。先に「状態を再確認」または「画面を更新」を行ってください。");
  }
  if (!isInstalledDisplayMode() && !els.browserPairingConfirmationCheckbox.checked) {
    throw new Error("通常ブラウザとPWAは設定が分かれることがあります。上の案内からインストールするか、このブラウザを持ち出しに使う確認へチェックしてください。");
  }
  if (!els.travelPairingCode.value.trim()) throw new Error("短期ペアリングコードを入力してください。");
  await travelAdapter.pair(els.travelPairingCode.value.trim(), els.travelDeviceName.value.trim() || "Nexus Ark Lite");
  els.travelPairingCode.value = "";
  await refreshTravelReadiness();
  renderTravelPairingState(true, true);
  await refreshTravelPendingRecovery();
  setSyncStatus("独立モード端末をペアリングしました。", "ok");
}

function consumePairingHandoff() {
  const handoff = parsePairingHandoff(window.location.href);
  if (!handoff.present) return false;
  window.history.replaceState(window.history.state, "", handoff.cleanUrl);
  els.travelPreparationDetails.open = true;
  els.pairingHandoffNotice.hidden = false;
  if (!handoff.valid) {
    const message = handoff.reason === "expired"
      ? "QRの短期コードは期限切れです。本体で再発行してください。"
      : "QRの短期情報を安全に取り込めませんでした。Lite用クラウドのURLとコードを確認してください。";
    els.pairingHandoffNotice.textContent = message;
    setSyncStatus(message, "warn");
    return true;
  }
  const previousBase = travelAdapter.configuredBase();
  travelAdapter.configure(handoff.workerUrl);
  resetTravelContextAfterWorkerChange(previousBase);
  els.travelWorkerUrl.value = handoff.workerUrl;
  els.travelPairingCode.value = handoff.code;
  els.pairingHandoffNotice.textContent =
    "QRからLite用クラウドのURLと短期コードを取り込みました。この画面の保存領域を確認し、「ペアリング」を押してください。自動では実行しません。";
  setSyncStatus("短期ペアリング情報を取り込みました。内容を確認してからペアリングしてください。", "ok");
  return true;
}

const STANDBY_DATA_PRESETS = {
  recommended: {
    includeCoreMemory: true,
    includeEpisodicMemory: true,
    episodicMemoryDays: 2,
    recentMessageLimit: 40,
    status: "おすすめ: 人柄や最近の出来事、直近の会話をバランスよく持ち出します。",
  },
  minimal: {
    includeCoreMemory: false,
    includeEpisodicMemory: false,
    episodicMemoryDays: 0,
    recentMessageLimit: 0,
    status: "最小限: 会話に必須の人格設定だけを持ち出します。",
  },
};

function applyStandbyDataPreset(preset, { persist = true } = {}) {
  const selected = ["recommended", "minimal", "custom"].includes(preset)
    ? preset
    : "recommended";
  els.standbyDataPreset.value = selected;
  const custom = selected === "custom";
  if (!custom) {
    const values = STANDBY_DATA_PRESETS[selected];
    els.standbyIncludeCoreMemoryCheckbox.checked = values.includeCoreMemory;
    els.standbyIncludeEpisodicMemoryCheckbox.checked = values.includeEpisodicMemory;
    els.standbyEpisodicMemoryDays.value = String(values.episodicMemoryDays);
    els.standbyRecentMessageLimit.value = String(values.recentMessageLimit);
    els.standbyDataPresetStatus.textContent = values.status;
  } else {
    els.standbyDataPresetStatus.textContent =
      "自分で選ぶ: 下の項目で、持ち出す記憶と会話の量を調整できます。";
    els.standbyDataCustomDetails.open = true;
  }
  els.standbyIncludeCoreMemoryCheckbox.disabled = !custom;
  els.standbyIncludeEpisodicMemoryCheckbox.disabled = !custom;
  els.standbyEpisodicMemoryDays.disabled = !custom || !els.standbyIncludeEpisodicMemoryCheckbox.checked;
  els.standbyRecentMessageLimit.disabled = !custom;
  if (persist) localStorage.setItem("nexusLite.standbyDataPreset", selected);
}

async function prepareStandbyFromLite({ automatic = false, silent = false } = {}) {
  if (travelSessionBlocksStandby()) {
    throw new Error(travelSessionStatus() === "returning"
      ? "署名付き帰宅が途中です。「帰宅を再開」で完了してから更新してください。"
      : "独立モードで使用中です。署名付き帰宅後に更新できます。");
  }
  if (!state.homeReachable || !state.roomId) throw new Error("本体接続中に実行してください。");
  const includeCoreMemory = els.standbyIncludeCoreMemoryCheckbox.checked;
  const includeEpisodicMemory = els.standbyIncludeEpisodicMemoryCheckbox.checked;
  const episodicMemoryDays = Math.max(0, Math.min(30, Number(els.standbyEpisodicMemoryDays.value) || 0));
  els.standbyEpisodicMemoryDays.value = String(episodicMemoryDays);
  const recentMessageLimit = Math.max(0, Math.min(40, Number(els.standbyRecentMessageLimit.value) || 0));
  els.standbyRecentMessageLimit.value = String(recentMessageLimit);
  if (!automatic && !silent && !window.confirm(
    `次の範囲をLite用クラウドへ準備します。\n\n人格設定: 含める（必須）\n永続記憶: ${includeCoreMemory ? "含める" : "含めない"}\nエピソード記憶: ${includeEpisodicMemory ? `直近${episodicMemoryDays}日（今日を除く）` : "含めない"}\n直近の会話: 最大${recentMessageLimit}件\n\n続けますか？`
  )) return;
  const manifest = await api("/api/v1/lite-travel/standby", {
    method: "POST",
    body: JSON.stringify({
      room_ids: [state.roomId],
      parallel_room_ids: [],
      retention_days: 7,
      automatic,
      include_core_memory: includeCoreMemory,
      include_episodic_memory: includeEpisodicMemory,
      episodic_memory_days: episodicMemoryDays,
      recent_message_limit: recentMessageLimit,
    }),
    timeoutMs: 120000,
  });
  const skipped = manifest.automatic_skipped === "minimum_interval"
    ? " / 最短間隔内のため据え置き"
    : manifest.automatic_skipped === "unchanged" ? " / 内容変更なし" : "";
  els.standbyStatus.textContent = `お出かけ前のデータ: 世代${manifest.generation} / ${manifest.created_at}${skipped}`;
  rememberLatestStandby(manifest);
  await refreshTravelReadiness();
}

async function maybeAutoRefreshStandby({ forceCheck = false, forceSave = false } = {}) {
  if (
    !els.standbyAutoRefreshCheckbox.checked
    || document.hidden
    || state.mode !== "home"
    || !state.connected
    || !state.homeReachable
    || !state.roomId
    || !travelAdapter.paired()
    || travelSessionBlocksStandby()
    || state.sending
    || state.syncing
    || state.statusRefreshing
    || state.standbyAutoRefreshing
  ) return false;
  const lastAttempt = Number(localStorage.getItem("nexusLite.standbyAutoAttemptAt") || 0);
  if (!forceCheck && Date.now() - lastAttempt < STANDBY_AUTO_CHECK_INTERVAL_MS) return false;
  state.standbyAutoRefreshing = true;
  localStorage.setItem("nexusLite.standbyAutoAttemptAt", String(Date.now()));
  try {
    await prepareStandbyFromLite({ automatic: !forceSave, silent: forceSave });
    return true;
  } finally {
    state.standbyAutoRefreshing = false;
  }
}

async function returnTravelToHome() {
  if (state.returningHome) return;
  if (state.travelEntryPromise) throw new Error("独立モードを開き終えてから帰宅してください。");
  const session = state.currentTravelSession || state.travelSession;
  if (!session) throw new Error("独立モードsessionがありません。");
  state.returningHome = true;
  applyTravelSessionControls();
  let importCompleted = false;
  setTravelReturnStatus("帰宅の準備中です。本体への接続を確認しています…", "checking");
  try {
    if (!await probeHome()) {
      throw new Error("本体に接続できないため、帰宅は開始していません。PCのNexus Arkと、利用している場合はTailscaleを起動し、本体の接続設定を確認してください。");
    }
    const resuming = String(session.status) === "returning";
    if (!window.confirm(resuming
      ? "途中の署名付き帰宅を再開し、本体への統合を完了しますか？"
      : "独立モード側の未確定送信を確認し、署名付き差分を本体へ統合しますか？")) {
      setTravelReturnStatus("帰宅を取りやめました。お出かけの状態は変更していません。", "idle");
      return;
    }
    setTravelReturnStatus("送信結果を確認しています。まだ本体への取り込みは開始していません…", "checking");
    let pending;
    try {
      pending = await travelAdapter.pendingStatus();
    } catch {
      await refreshTravelPendingRecovery().catch(() => {});
      throw new Error("送信結果を確認できないため、帰宅は開始していません。接続を確認し、チャットの「前回の送信を確認」を開いてください。");
    }
    if (pending) {
      await refreshTravelPendingRecovery().catch(() => {});
      throw new Error(pending.status === "outcome_unknown"
        ? "結果不明の送信記録が残っているため、帰宅は開始していません。チャットの「前回の送信を確認」で内容を確認し、「結果不明の記録を保管して続ける」を押してから帰宅してください。"
        : "送信結果がまだ確定していないため、帰宅は開始していません。チャットの「前回の送信を確認」から結果を再確認してください。");
    }
    await refreshTravelPendingRecovery().catch(() => {});
    state.travelContextRevision = (state.travelContextRevision || 0) + 1;
    setTravelReturnStatus("帰宅処理中です。署名を確認して会話を本体へ取り込んでいます。完了表示までお待ちください…", "checking");
    await api("/api/v1/lite-travel/return", {
      method: "POST",
      body: JSON.stringify({ travel_session_id: session.travel_session_id }),
      timeoutMs: 120000,
    });
    importCompleted = true;
    state.travelSession = null;
    state.currentTravelSession = null;
    setTravelReturnStatus("本体への取り込みが完了しました。本体接続へ切り替えています…", "checking");
    await enterHomeMode();
    await refreshTravelReadiness();
    setSyncStatus("署名付き帰宅を完了し、本体接続へ戻りました。お出かけ前データを更新しています。");
    if (els.standbyAutoRefreshCheckbox.checked) {
      const refreshed = await maybeAutoRefreshStandby({ forceCheck: true, forceSave: true }).catch((error) => {
        els.standbyStatus.textContent = `帰宅は完了しました。お出かけ前データは未保存です: ${error.message}`;
        setSyncStatus("帰宅は完了しました。お出かけ前データは未保存のため、接続を確認して更新してください。", "warn");
        return false;
      });
      if (refreshed) setSyncStatus("署名付き帰宅と、お出かけ前データの更新を完了しました。");
    } else {
      setSyncStatus("署名付き帰宅を完了しました。お出かけ前データは未保存です。必要なら設定から更新してください。", "warn");
    }
    setTravelReturnStatus("帰宅が完了しました。本体へ戻りました。通常どおりご利用いただけます。", "ok");
  } catch (error) {
    const failure = importCompleted
      ? new Error("会話の本体への取り込みは完了していますが、画面の接続更新を完了できませんでした。PC本体の帰宅画面で状態を確認してください。")
      : error;
    setTravelReturnStatus(failure.message || "帰宅の完了を確認できません。PC本体の帰宅画面で状態を確認してください。", "warn");
    await refreshTravelReadiness().catch(() => {});
    throw failure;
  } finally {
    state.returningHome = false;
    applyTravelSessionControls();
    renderSnapshotFreshness();
  }
}

async function handleHomeModeAction() {
  if (state.travelEntryPromise) throw new Error("独立モードを開き終えてから本体へ戻ってください。");
  if (travelSessionBlocksStandby() || state.travelSession) {
    await returnTravelToHome();
    return;
  }
  await enterHomeMode();
}

async function sendTravelMessage(message) {
  if (!state.travelSession || !state.travelPersonaId) throw new Error("独立モードsessionがありません。");
  const clientMessageId = `travel_${crypto.randomUUID().replaceAll("-", "_")}`;
  travelAdapter.saveDraft(message);
  let unresolved;
  try {
    unresolved = await travelAdapter.pendingStatus();
  } catch (error) {
    await refreshTravelPendingRecovery().catch(() => {});
    throw error;
  }
  if (unresolved) {
    await refreshTravelPendingRecovery().catch(() => {});
    throw new Error("前回の独立モード送信結果が未確定です。「前回の送信を確認」欄で状態を確認してください。");
  }
  await refreshTravelPendingRecovery().catch(() => {});
  setSyncStatus("送信中です。AIの応答を待っています…");
  const userMessage = appendMessage("user", message, { travelClientMessageId: clientMessageId });
  const pending = appendMessage("pending", "考えています...");
  let result;
  try {
    result = await travelAdapter.send(state.travelSession, state.travelPersonaId, message, clientMessageId);
  } catch (error) {
    removeMessage(pending);
    if (travelAdapter.errorCode(error) === "pending_send_unresolved") {
      removeMessage(userMessage);
    }
    if (["budget_limit_exceeded", "persona_budget_limit_exceeded"].includes(travelAdapter.errorCode(error))) {
      state.travelBudgetQuote = { sessionId: state.travelSession.travel_session_id, personaId: state.travelPersonaId,
        amount: error.nextRequestReservedUsd };
      await loadTravelUsage().catch(() => {});
    }
    await refreshTravelPendingRecovery().catch(() => {});
    throw error;
  }
  removeMessage(pending);
  if (result.answer) appendMessage("agent", result.answer);
  if (result.terminal === "response.committed") {
    travelAdapter.saveDraft("");
    els.messageInput.value = "";
    await refreshTravelPendingRecovery().catch(() => {});
    await loadTravelUsage();
  } else {
    await refreshTravelPendingRecovery().catch(() => {});
    if (result.error) {
      throw new Error(`${result.error.message}（確認コード: ${result.error.code}${result.error.httpStatus ? ` / HTTP ${result.error.httpStatus}` : ""}）`);
    }
    throw new Error("送信結果が未確定です。別モードへ自動再送しません。「前回の送信を確認」欄で状態を確認してください。");
  }
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

async function uploadSelectedImage(file, roomId, options = {}) {
  if (!file) {
    return null;
  }
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${options.apiBase || state.apiBase}/api/v1/rooms/${encodeURIComponent(roomId)}/uploads`, {
    method: "POST",
    headers: options.headers || authHeaders(),
    signal: options.signal,
    body: formData
  });
  if (!response.ok) {
    const body = await response.text();
    const error = new Error(`${response.status} ${body || response.statusText}`);
    error.status = response.status;
    throw error;
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
  text = String(text || "");
  const pattern = /(`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
    const tag = match[2] ? "code" : match[3] || match[4] ? "strong" : match[5] ? "em" : "a";
    const element = document.createElement(tag);
    element.textContent = match[2] || match[3] || match[4] || match[5] || match[6];
    if (tag === "a") {
      element.href = match[7];
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    parent.appendChild(element);
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) parent.appendChild(document.createTextNode(text.slice(cursor)));
}

function renderBasicMarkdown(parent, text) {
  let code = null;
  for (const line of text.split(/\r?\n/)) {
    if (/^\s*```/.test(line)) {
      if (code) code = null;
      else {
        const pre = document.createElement("pre");
        code = document.createElement("code");
        pre.appendChild(code);
        parent.appendChild(pre);
      }
      continue;
    }
    if (code) { code.appendChild(document.createTextNode(line + "\n")); continue; }
    const heading = line.match(/^#{1,6}\s+(.+)$/);
    const quote = line.match(/^>\s?(.*)$/);
    const list = line.match(/^\s*(?:[-+*]|\d+\.)\s+(.+)$/);
    const row = document.createElement(heading ? "h4" : quote ? "blockquote" : "div");
    if (list) row.className = "markdown-list-line";
    appendInlineMarkdown(row, heading?.[1] ?? quote?.[1] ?? (list ? "• " + list[1] : line));
    if (!line) row.appendChild(document.createElement("br"));
    parent.appendChild(row);
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
    .replace(/^\s*🛠️\s*/, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const heading = document.createElement("h3");
  heading.textContent = "音楽推薦カード";
  card.appendChild(heading);

  let currentTrack = null;
  for (const line of lines) {
    if (/^#{1,6}\s*音楽推薦カード\s*$/.test(line)) {
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
  if (/^\s*(?:🛠️\s*)?#{1,6}\s*音楽推薦カード\s*$/m.test(String(text || ""))) {
    item.classList.add("music-message");
    item.appendChild(renderMusicCard(redactedPlainText(text)));
    return;
  }
  const content = document.createElement("div");
  content.className = "message-markdown";
  renderBasicMarkdown(content, String(text || ""));
  // マスクは書式化後のテキストだけに適用し、HTMLやリンク先へ置換しない。
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const replacement = document.createElement("span");
    replacement.innerHTML = applyRedactions(node.textContent);
    node.replaceWith(...replacement.childNodes);
  }
  if (state.redactionEnabled) {
    // マスクした文章から元の文字列をリンク先で露出させない。
    for (const link of content.querySelectorAll("a")) link.replaceWith(...link.childNodes);
  }
  item.appendChild(content);
}

function isNearMessageBottom() {
  return els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < 96;
}

function redactedPlainText(text) {
  const fragment = document.createElement("div");
  fragment.innerHTML = applyRedactions(text);
  return fragment.textContent || "";
}

async function copyMessageText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    const active = document.activeElement;
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.setAttribute("aria-hidden", "true");
    document.body.appendChild(textarea);
    try {
      textarea.select();
      if (!document.execCommand("copy")) throw new Error("コピーできません。");
    } finally {
      textarea.remove();
      active?.focus({ preventScroll: true });
    }
  }
}

function showHistoryReloadNotice() {
  const notice = appendMessage("system", "応答は受信済みですが、履歴を取得できませんでした。音楽推薦カードなどの追加結果は履歴の再取得後に確認できます。");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary-button compact-button";
  button.textContent = "履歴を再取得";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await loadHistory();
    } catch {
      setSyncStatus("履歴を取得できませんでした。接続を確認して再取得してください。", "warn");
    } finally {
      button.disabled = false;
    }
  });
  notice.appendChild(button);
}

function scrollMessagesToBottom() {
  els.messages.scrollTop = els.messages.scrollHeight;
  els.newMessageButton.hidden = true;
}

function formatMessageTimestamp(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat("ja-JP", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function appendThoughtDetails(item, thoughts) {
  const thoughtText = String(thoughts || "").trim();
  if (!thoughtText) {
    return;
  }
  const details = document.createElement("details");
  details.className = "message-thoughts";
  const summary = document.createElement("summary");
  summary.textContent = "思考ログ";
  details.appendChild(summary);
  const content = document.createElement("pre");
  content.className = "message-thoughts-content";
  // Keep the existing redaction rules while assigning through textContent so
  // persisted thought text can never become executable markup.
  content.textContent = redactedPlainText(thoughtText);
  details.appendChild(content);
  const actions = document.createElement("div");
  actions.className = "message-thoughts-actions";
  const translateButton = document.createElement("button");
  translateButton.type = "button";
  translateButton.className = "speak-button thought-translate-button";
  translateButton.textContent = "翻訳";
  const copyButton = document.createElement("button");
  copyButton.type = "button";
  copyButton.className = "speak-button thought-copy-button";
  copyButton.textContent = "思考ログをコピー";
  actions.append(translateButton, copyButton);
  details.appendChild(actions);
  const help = document.createElement("p");
  help.className = "message-thoughts-help";
  help.textContent = "翻訳は本体の思考ログ翻訳モデルを使います。設定によってAPI利用料がかかります。";
  details.appendChild(help);
  const status = document.createElement("p");
  status.className = "message-thoughts-help";
  status.setAttribute("role", "status");
  details.appendChild(status);

  // Cache only in this rendered message. Room/mode/redaction/history changes
  // detach it, so late results cannot update a different conversation.
  const roomId = state.roomId;
  const apiBase = state.apiBase;
  const token = state.token;
  const current = () => details.isConnected && state.connected && state.mode === "home"
    && state.roomId === roomId && state.apiBase === apiBase && state.token === token;
  let translatedText = null;
  let showingTranslation = false;
  translateButton.addEventListener("click", async () => {
    if (!current()) return;
    if (translatedText !== null) {
      showingTranslation = !showingTranslation;
      content.textContent = redactedPlainText(showingTranslation ? translatedText : thoughtText);
      translateButton.textContent = showingTranslation ? "原文に戻す" : "訳文を表示";
      copyButton.textContent = "思考ログをコピー";
      return;
    }
    translateButton.disabled = true;
    translateButton.textContent = "翻訳中…";
    status.textContent = "";
    try {
      const response = await api(`/api/v1/rooms/${encodeURIComponent(roomId)}/chat/thoughts/translate`, {
        method: "POST",
        body: JSON.stringify({ text: content.textContent }),
        timeoutMs: 120000
      });
      if (!current()) return;
      if (typeof response.text !== "string" || !response.text.trim()) throw new Error("Empty translation");
      translatedText = response.text;
      showingTranslation = true;
      content.textContent = redactedPlainText(translatedText);
      translateButton.textContent = "原文に戻す";
      copyButton.textContent = "思考ログをコピー";
    } catch {
      if (!current()) return;
      status.textContent = "翻訳できませんでした。接続や本体の翻訳モデル設定を確認してください。";
      translateButton.textContent = "翻訳";
    } finally {
      if (current()) translateButton.disabled = false;
    }
  });
  copyButton.addEventListener("click", async () => {
    copyButton.disabled = true;
    try {
      await copyMessageText(content.textContent || "");
      if (current()) copyButton.textContent = "コピー済み";
    } catch {
      if (current()) status.textContent = "コピーできませんでした。思考ログを選択してコピーしてください。";
    } finally {
      if (current()) copyButton.disabled = false;
    }
  });
  item.appendChild(details);
}

function appendMessage(role, text, metadata = {}) {
  const shouldFollow = isNearMessageBottom() || role === "user" || role === "pending";
  const item = document.createElement("div");
  item.className = `message ${role}`;
  if (typeof metadata.travelClientMessageId === "string") {
    item.dataset.travelClientMessageId = metadata.travelClientMessageId;
  }
  if (role === "agent" && state.mode === "home") {
    appendThoughtDetails(item, metadata.thoughts);
  }
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
    const copyButton = document.createElement("button");
    copyButton.className = "speak-button copy-button";
    copyButton.type = "button";
    copyButton.textContent = "コピー";
    copyButton.addEventListener("click", async () => {
      copyButton.disabled = true;
      try {
        await copyMessageText(redactedPlainText(text));
        copyButton.textContent = "コピー済み";
      } catch {
        setSyncStatus("コピーできませんでした。本文を選択してコピーしてください。", "warn");
      } finally {
        copyButton.disabled = false;
      }
    });
    actions.appendChild(copyButton);
    if (metadata.regeneratable && metadata.messageId) {
      const regenerateButton = document.createElement("button");
      regenerateButton.className = "speak-button regenerate-button";
      regenerateButton.type = "button";
      regenerateButton.textContent = "再生成";
      regenerateButton.addEventListener("click", () => regenerateLatestMessage(metadata.messageId, regenerateButton));
      actions.appendChild(regenerateButton);
    }
    item.appendChild(actions);
  }
  const metaParts = [formatMessageTimestamp(metadata.timestamp), metadata.model].filter(Boolean);
  if (metaParts.length) {
    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = metaParts.join(" · ");
    meta.title = metaParts.join(" · ");
    item.appendChild(meta);
  }
  els.messages.appendChild(item);
  if (shouldFollow) {
    scrollMessagesToBottom();
  } else {
    els.newMessageButton.hidden = false;
  }
  return item;
}

function appendHistoryMessage(message, regeneratable = false) {
  const item = appendMessage(message.role || "system", message.content || "", {
    timestamp: message.timestamp,
    model: message.model,
    messageId: message.message_id,
    thoughts: message.thoughts,
    regeneratable
  });
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
  const messages = state.chatMessages || [];
  let lastAgentIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "agent") {
      lastAgentIndex = index;
      break;
    }
  }
  messages.forEach((message, index) => {
    appendHistoryMessage(message, index === lastAgentIndex);
  });
  renderTravelFailedMessage();
  scrollMessagesToBottom();
}

async function regenerateLatestMessage(messageId, button) {
  if (!state.connected || state.sending || !messageId) {
    return;
  }
  if (!window.confirm("最新の応答を作り直します。アイテム使用などの外部操作は再実行しません。よろしいですか？")) {
    return;
  }
  state.sending = true;
  button.disabled = true;
  button.textContent = "再生成中";
  setSyncStatus("応答を再生成しています...");
  try {
    await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/chat/regenerate`, {
      method: "POST",
      body: JSON.stringify({
        target_message_id: messageId,
        client_message_id: `lite-regen-${createMessageId()}`
      }),
      timeoutMs: 600000
    });
    await loadHistory();
    setSyncStatus("応答を再生成しました。");
  } catch (error) {
    setSyncStatus(`再生成に失敗しました: ${error.message}`, "warn");
    await loadHistory().catch(() => {});
  } finally {
    state.sending = false;
    button.disabled = false;
    button.textContent = "再生成";
  }
}

async function loadHistory() {
  if (!state.roomId) {
    return [];
  }
  const query = new URLSearchParams({ limit: String(state.historyLimit) });
  if (state.mode === "home") {
    query.set("include_thoughts", "true");
  }
  const room = state.roomId;
  const base = state.apiBase;
  const requestId = state.homeHistoryLoadId = (state.homeHistoryLoadId || 0) + 1;
  const history = await api(`/api/v1/rooms/${encodeURIComponent(room)}/chat/history?${query.toString()}`);
  if (state.mode !== "home" || state.roomId !== room || state.apiBase !== base
    || state.homeHistoryLoadId !== requestId) return [];
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
      const savedDraft = localStorage.getItem(draftStorageKey()) || "";
      if (savedDraft.trim() !== pending.message) {
        els.messageInput.value = savedDraft;
        resizeComposer();
      } else {
        els.messageInput.value = "";
        els.messageInput.style.height = "";
        saveComposerDraft();
      }
    }
    if (shouldNotify && state.pushSubscriptionCount <= 0) {
      const reply = messages.slice(sentIndex + 1).find((message) => message.role === "agent");
      showLiteNotification("Nexus Ark Lite", responseNotificationBody(reply?.content || ""));
    }
    const pendingFiles = (pending.files || [pending.file]).filter(Boolean).slice();
    state.selectedChatFiles = state.selectedChatFiles.filter((file) => {
      const index = pendingFiles.findIndex((signature) => signature.name === file.name && signature.size === file.size && (signature.lastModified == null || signature.lastModified === file.lastModified));
      if (index < 0) return true;
      pendingFiles.splice(index, 1);
      return false;
    });
    state.preparedChatAttachments = state.preparedChatAttachments.filter((path) => !(pending.attachments || []).includes(path));
    renderComposerAttachments();
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
  const previousRoom = state.roomId;
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
  if (state.roomId !== previousRoom) resetHomeFeatures();
  renderChatContextName();
}

function setMeter(el, value) {
  el.value = Math.max(0, Math.min(1, Number(value) || 0));
}

function statusDatetime(value) {
  if (!value) return "日時不明";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("ja-JP") : "日時不明";
}

function statusNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
    ? value.toFixed(2) : "—";
}

function renderPersistentEmotions(status) {
  els.persistentEmotionsList.replaceChildren();
  if (!status) {
    els.persistentEmotionsStatus.textContent = "未読込";
    return;
  }
  if (status.persistent_emotions_available !== true || !Array.isArray(status.persistent_emotions)) {
    els.persistentEmotionsStatus.textContent = Object.hasOwn(status, "persistent_emotions_available")
      ? "持続感情を取得できませんでした。未申告かどうかは判断できません。"
      : "この本体は持続感情の表示に未対応です。本体の更新後に再取得してください。";
    return;
  }
  const labels = {
    active: "申告中", unreported: "未申告", settled: "本人が終了", expired: "提示期間終了",
    below_threshold: "参考値が提示基準未満", disabled: "使用停止中", invalid: "記録・設定を確認してください",
    unsupported: "未対応の項目",
  };
  els.persistentEmotionsStatus.textContent = status.persistent_emotions.length
    ? "" : "表示できる感情項目がありません。";
  const items = [...status.persistent_emotions].sort((left, right) =>
    Number(String(right.category).startsWith("custom_")) - Number(String(left.category).startsWith("custom_")));
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "persistent-emotion-card";
    card.setAttribute("role", "listitem");
    const name = document.createElement("strong");
    name.textContent = String(item.name || "名前不明の項目");
    const label = document.createElement("span");
    label.className = "field-info";
    label.textContent = labels[item.presentation_status] || "状態を確認できません";
    const values = document.createElement("dl");
    for (const [title, value] of [
      ["申告値", statusNumber(item.intensity)],
      ["取得時の参考値", statusNumber(item.current_intensity)],
      ["申告日時", item.started_at ? statusDatetime(item.started_at) : "—"],
    ]) {
      const key = document.createElement("dt");
      key.textContent = title;
      const text = document.createElement("dd");
      text.textContent = value;
      values.append(key, text);
    }
    card.append(name, label, values);
    els.persistentEmotionsList.append(card);
  }
}

function renderRoomStatusFreshness() {
  const room = state.mode === "travel"
    ? state.travelPersonaId || travelPersonas()[0]?.persona_id || state.roomId : state.roomId;
  const saved = state.statusSnapshot;
  const same = saved && saved.room === room && saved.base === state.apiBase;
  els.statusRefreshButton.disabled = state.mode !== "home" || !state.connected || Boolean(state.statusRefreshContext);
  if (!same) {
    state.statusSnapshot = null;
    els.statusDataMeta.textContent = state.mode === "travel"
      ? "独立モードでは本体の状態を取得できません。本体へ接続すると更新できます。"
      : "本体へ接続して状態を取得してください。";
    els.expressionValue.textContent = "—";
    for (const meter of [els.driveBoredom, els.driveCuriosity, els.driveGoal, els.driveRelated]) setMeter(meter, 0);
    renderPersistentEmotions(null);
    if (state.statusReadFailed && state.mode === "home") {
      els.statusDataMeta.textContent = "状態を取得できませんでした。本体へ接続して再取得してください。";
      els.persistentEmotionsStatus.textContent = "持続感情を取得できませんでした。未申告かどうかは判断できません。";
    }
    return;
  }
  const note = state.mode === "travel" ? "独立モードでは更新していません。本体へ接続すると更新できます。"
    : !state.connected ? "本体へ接続できないため更新していません。"
      : state.statusReadFailed ? "取得に失敗したため更新していません。前回取得時の値です。" : "取得時点の値です。";
  els.statusDataMeta.textContent = `最終取得: ${statusDatetime(saved.receivedAt)}。${note}`;
}

function renderStatus(status) {
  state.statusSnapshot = { room: state.roomId, base: state.apiBase, value: status, receivedAt: new Date().toISOString() };
  state.statusReadFailed = false;
  els.roomTitle.textContent = status.display_name || status.room_id;
  renderChatContextName(status.display_name || status.room_id);
  els.expressionValue.textContent = status.current_expression || "neutral";
  renderPersistentEmotions(status);
  renderRoomStatusFreshness();
  els.locationValue.textContent = status.current_location || "-";
  state.chatLocation = status.current_location || "";
  renderChatContextLocation();
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
  if (!state.roomId || !state.connected || state.mode !== "home") {
    renderRoomStatusFreshness();
    return;
  }
  const context = { room: state.roomId, base: state.apiBase, generation: homeFeatureGeneration,
    temporaryVersion: state.temporaryLocationVersion };
  const previous = state.statusRefreshContext;
  if (previous?.room === context.room && previous.base === context.base && previous.generation === context.generation) return;
  state.statusRefreshContext = context;
  renderRoomStatusFreshness();
  try {
    const [status, temporary] = await Promise.allSettled([
      api(`/api/v1/rooms/${encodeURIComponent(context.room)}/status`, { timeoutMs: 15000 }),
      api(homeFeaturePath(context.room, "temporary-location"), { timeoutMs: 15000 }),
    ]);
    if (state.mode !== "home" || state.roomId !== context.room || state.apiBase !== context.base
      || homeFeatureGeneration !== context.generation) return;
    if (state.temporaryLocationVersion === context.temporaryVersion) {
      if (temporary.status === "fulfilled") renderTemporaryLocation(temporary.value);
      else {
        state.temporaryLocation = null;
        homeElement("temporary-location-status").textContent = "一時的現在地の状態を確認できませんでした。現在の内容を取得して再確認してください。";
        renderChatContextLocation();
      }
    }
    if (status.status === "fulfilled") {
      if (status.value.room_id !== context.room) throw new Error("取得した状態のルームが一致しません。");
      renderStatus(status.value);
    }
    else throw status.reason;
  } catch (error) {
    if (state.mode === "home" && state.roomId === context.room && state.apiBase === context.base
      && homeFeatureGeneration === context.generation) {
      state.statusReadFailed = true;
      if (!state.statusSnapshot) els.persistentEmotionsStatus.textContent = "持続感情を取得できませんでした。";
    }
    throw error;
  } finally {
    if (state.statusRefreshContext === context) state.statusRefreshContext = null;
    renderRoomStatusFreshness();
  }
}

function setManagementStatus(text, mode = "idle") {
  els.managementSummaryStatus.textContent = text;
  els.managementSummaryStatus.dataset.mode = mode;
}

function setLetterboxStatus(text, mode = "idle") {
  els.letterboxSummaryStatus.textContent = text;
  els.letterboxSummaryStatus.dataset.mode = mode;
}

function formatLetterDatetime(value) {
  if (!value) {
    return "";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleString("ja-JP", { dateStyle: "medium", timeStyle: "short" });
}

function renderLetterbox(response) {
  els.letterboxList.replaceChildren();
  els.letterboxReader.hidden = true;
  const letters = response.letters || [];
  if (!letters.length) {
    const empty = document.createElement("li");
    empty.className = "letterbox-empty";
    empty.textContent = "手紙はまだありません。";
    els.letterboxList.appendChild(empty);
    return;
  }
  for (const letter of letters) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "letterbox-item";
    if (!letter.read) {
      button.classList.add("unread");
    }
    const title = document.createElement("span");
    title.className = "letterbox-item-title";
    title.textContent = `${letter.read ? "" : "● "}${letter.title}`;
    const meta = document.createElement("span");
    meta.className = "letterbox-item-meta";
    meta.textContent = formatLetterDatetime(letter.created_at);
    button.appendChild(title);
    button.appendChild(meta);
    button.addEventListener("click", () => openLetter(letter.id, button));
    item.appendChild(button);
    els.letterboxList.appendChild(item);
  }
}

async function openLetter(letterId, button) {
  try {
    const letter = await api(
      `/api/v1/rooms/${encodeURIComponent(state.roomId)}/letters/${encodeURIComponent(letterId)}`
    );
    els.letterboxReaderTitle.textContent = letter.title;
    els.letterboxReaderMeta.textContent = `届いた日時: ${formatLetterDatetime(letter.created_at)}`;
    els.letterboxReaderBody.textContent = letter.body;
    els.letterboxReader.hidden = false;
    if (button) {
      button.classList.remove("unread");
      const title = button.querySelector(".letterbox-item-title");
      if (title) {
        title.textContent = letter.title;
      }
    }
    // 開いた手紙はサーバー側で既読になるため、未読数の表示を更新する
    const unread = els.letterboxList.querySelectorAll(".letterbox-item.unread").length;
    setLetterboxStatus(unread > 0 ? `未読 ${unread}` : "未読なし", unread > 0 ? "ok" : "idle");
    els.letterboxReader.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    setSyncStatus(`手紙の取得に失敗しました: ${error.message}`, "warn");
  }
}

async function refreshLetterbox() {
  if (!state.connected || !state.roomId || !els.managementDetails?.open || state.activePage !== "records") {
    return;
  }
  setLetterboxStatus("読込中", "busy");
  els.letterboxRefreshButton.disabled = true;
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/letters?limit=100`);
    renderLetterbox(response);
    const unread = response.unread_count || 0;
    setLetterboxStatus(unread > 0 ? `未読 ${unread}` : "未読なし", unread > 0 ? "ok" : "idle");
  } catch (error) {
    setLetterboxStatus("失敗", "error");
    setSyncStatus(`手紙箱の取得に失敗しました: ${error.message}`, "warn");
  } finally {
    els.letterboxRefreshButton.disabled = false;
  }
}

function selectedDraft() {
  return state.twitterDrafts.find((draft) => draft.id === els.draftSelect.value) || null;
}

function renderSelectedDraft() {
  const draft = selectedDraft();
  els.draftMediaGrid.replaceChildren();
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
  for (const path of draft.media_paths || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "draft-media-button";
    button.title = "添付画像を拡大";
    const img = document.createElement("img");
    img.alt = "Twitter下書きの添付画像";
    button.appendChild(img);
    button.addEventListener("click", () => openImageDialog(img.dataset.fullSrc || img.src));
    els.draftMediaGrid.appendChild(button);
    loadAttachmentImage(img, path);
  }
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
    const media = draft.media_paths?.length ? ` 📷${draft.media_paths.length}` : "";
    option.textContent = `${draft.timestamp ? draft.timestamp.slice(5, 16).replace("T", " ") : draft.id}${media} ${preview}`;
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
    if (!state.chatLocation) state.chatLocation = response.current_location || "";
    renderChatContextLocation();
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
  if (!state.chatLocation) state.chatLocation = response.current_location || "";
  renderChatContextLocation();
}

async function loadLocations() {
  const room = state.roomId, base = state.apiBase, generation = homeFeatureGeneration;
  const response = await api(`/api/v1/rooms/${encodeURIComponent(room)}/locations`);
  if (state.mode === "home" && state.roomId === room && state.apiBase === base
    && homeFeatureGeneration === generation) renderLocations(response);
}

function renderAutonomy(response) {
  const stateText = response.enabled ? "通常" : "静か";
  homeElement("autonomy-minutes").value = response.inactivity_minutes;
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

function setNoteEditing(editing) {
  state.noteEditing = editing;
  els.noteViewer.hidden = editing;
  els.noteEditor.hidden = !editing;
  els.noteEditButton.hidden = editing;
  els.noteSaveButton.hidden = !editing;
  els.noteCancelButton.hidden = !editing;
  els.noteRefreshButton.disabled = editing;
  document.querySelector("#note-latest-button").disabled = editing;
  els.noteTypeSelect.disabled = editing;
  els.noteHeadingSelect.disabled = editing || !els.noteHeadingSelect.options.length;
}

function renderNote(response) {
  const content = String(response.content || "").trim();
  els.noteViewer.textContent = content || "このノートは空です。";
  const sizeKb = Math.max(0, Number(response.size || 0) / 1024).toFixed(1);
  els.noteMeta.textContent = `${response.title || "ノート"} / 更新 ${formatNoteDate(response.updated_at)} / ${sizeKb}KB`;
  state.currentNote = {
    type: response.note_type || els.noteTypeSelect.value || "research",
    content: String(response.content || ""),
    contentHash: response.content_hash || "",
    editable: Boolean(response.editable)
  };
  state.noteEditable = Boolean(response.editable);
  els.noteEditButton.disabled = !state.currentNote.editable;
  if (state.noteEditing) {
    els.noteEditor.value = state.currentNote.content;
  }
}

async function loadNoteHeadings() {
  if (!state.connected || !state.roomId) {
    return;
  }
  const requestId = ++state.noteLoadId;
  const roomId = state.roomId;
  const noteType = els.noteTypeSelect.value || "research";
  const isCurrent = () => requestId === state.noteLoadId && roomId === state.roomId && noteType === els.noteTypeSelect.value;
  els.noteRefreshButton.disabled = true;
  homeElement("note-latest-button").disabled = false;
  els.noteMeta.textContent = "見出し取得中";
  setNoteEditing(false);
  els.noteHeadingSelect.disabled = true;
  els.noteEditButton.disabled = true;
  state.noteEditable = false;
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/notes/${encodeURIComponent(noteType)}?headings_only=true`, {
      timeoutMs: 8000
    });
    if (!isCurrent()) return;
    state.noteEditable = Boolean(response.editable);
    const headings = response.headings || [];
    els.noteHeadingSelect.replaceChildren();
    if (!headings.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "（見出しなし）";
      els.noteHeadingSelect.appendChild(option);
      els.noteHeadingSelect.disabled = true;
      const fullResponse = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/notes/${encodeURIComponent(noteType)}`, {
        timeoutMs: 8000
      });
      if (!isCurrent()) return;
      renderNote(fullResponse);
    } else {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "（見出しを選んで表示）";
      placeholder.disabled = true;
      placeholder.selected = true;
      els.noteHeadingSelect.appendChild(placeholder);
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
      els.noteViewer.textContent = "見出しを選ぶと表示します。";
      state.currentNote = null;
      els.noteEditButton.disabled = !response.editable;
    }
    const sizeKb = Math.max(0, Number(response.size || 0) / 1024).toFixed(1);
    els.noteMeta.textContent = `${response.title || "ノート"} / ${sizeKb}KB / ${headings.length}見出し`;
  } catch (error) {
    if (!isCurrent()) return;
    els.noteMeta.textContent = "失敗";
    els.noteViewer.textContent = `見出しを読み込めませんでした: ${error.message}`;
  } finally {
    if (isCurrent()) els.noteRefreshButton.disabled = false;
  }
}

async function loadNoteSection() {
  if (!state.connected || !state.roomId || state.noteEditing) {
    return;
  }
  const selectedHeading = els.noteHeadingSelect.value;
  if (!selectedHeading) {
    return;
  }
  const requestId = ++state.noteLoadId;
  const roomId = state.roomId;
  const noteType = els.noteTypeSelect.value || "research";
  const isCurrent = () => requestId === state.noteLoadId && roomId === state.roomId && noteType === els.noteTypeSelect.value;
  els.noteEditButton.disabled = true;
  els.noteViewer.textContent = "読込中...";
  try {
    let url = `/api/v1/rooms/${encodeURIComponent(state.roomId)}/notes/${encodeURIComponent(noteType)}`;
    if (selectedHeading !== "__all__") {
      url += `?heading=${encodeURIComponent(selectedHeading)}`;
    }
    const response = await api(url, { timeoutMs: 15000 });
    if (!isCurrent()) return;
    state.noteEditable = Boolean(response.editable);
    const content = String(response.content || "").trim();
    els.noteViewer.textContent = content || "このセクションは空です。";
    state.currentNote = {
      type: response.note_type || noteType,
      content: selectedHeading === "__all__" ? String(response.content || "") : "",
      contentHash: response.content_hash || "",
      editable: Boolean(response.editable)
    };
    els.noteEditButton.disabled = !response.editable;
    const sizeKb = Math.max(0, Number(response.size || 0) / 1024).toFixed(1);
    els.noteMeta.textContent = `${response.title || "ノート"} / 更新 ${formatNoteDate(response.updated_at)} / ${sizeKb}KB`;
  } catch (error) {
    if (!isCurrent()) return;
    els.noteViewer.textContent = `ノートを読み込めませんでした: ${error.message}`;
    els.noteEditButton.disabled = !state.noteEditable;
  }
}

async function startNoteEdit() {
  if (!["research", "creative"].includes(els.noteTypeSelect.value) || (state.currentNote && !state.currentNote.editable)) {
    return;
  }
  if (!state.connected || !state.roomId) {
    return;
  }
  const requestId = ++state.noteLoadId;
  const roomId = state.roomId;
  const noteType = els.noteTypeSelect.value || "research";
  const isCurrent = () => requestId === state.noteLoadId && roomId === state.roomId && noteType === els.noteTypeSelect.value;
  els.noteEditButton.disabled = true;
  els.noteMeta.textContent = "全文読込中";
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(roomId)}/notes/${encodeURIComponent(noteType)}`, {
      timeoutMs: 15000
    });
    if (!isCurrent()) return;
    renderNote(response);
    els.noteEditor.value = String(response.content || "");
    setNoteEditing(true);
    els.noteMeta.textContent = `${response.title || "ノート"} / 編集中`;
  } catch (error) {
    if (!isCurrent()) return;
    els.noteViewer.textContent = `ノートを編集用に読み込めませんでした: ${error.message}`;
  } finally {
    if (isCurrent()) els.noteEditButton.disabled = false;
  }
}

function cancelNoteEdit() {
  setNoteEditing(false);
  if (state.currentNote) {
    els.noteViewer.textContent = state.currentNote.content || "このノートは空です。";
  }
}

async function saveNoteEdit() {
  if (!state.connected || !state.roomId || !state.currentNote) {
    return;
  }
  els.noteSaveButton.disabled = true;
  els.noteCancelButton.disabled = true;
  els.noteMeta.textContent = "保存中";
  const noteType = state.currentNote.type || els.noteTypeSelect.value || "research";
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(state.roomId)}/notes/${encodeURIComponent(noteType)}`, {
      method: "PUT",
      timeoutMs: 15000,
      body: JSON.stringify({
        content: els.noteEditor.value,
        base_hash: state.currentNote.contentHash || null
      })
    });
    renderNote(response);
    setNoteEditing(false);
    els.noteMeta.textContent = `${response.title || "ノート"} / 保存しました`;
  } catch (error) {
    const message = String(error.message || "");
    if (message.startsWith("409 ")) {
      els.noteMeta.textContent = "他の場所で更新されています。再読込してください。";
      setSyncStatus("ノートは保存されていません。再読込してから編集し直してください。", "warn");
    } else {
      els.noteMeta.textContent = `保存失敗: ${message}`;
    }
  } finally {
    els.noteSaveButton.disabled = false;
    els.noteCancelButton.disabled = false;
  }
}

function selectedItem() {
  return state.items.find((item) => item.id === els.itemSelect.value) || null;
}

function renderItemActions() {
  const target = els.itemTargetSelect.value;
  const options = target === "location"
    ? [["pickup", "拾う"], ["consume_location", "その場で使う"]]
    : [["gift", "ペルソナに贈る"], ["consume", "自分で使う"], ["place", "現在地に置く"]];
  const previous = els.itemActionSelect.value;
  els.itemActionSelect.replaceChildren();
  for (const [value, label] of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    els.itemActionSelect.appendChild(option);
  }
  if (options.some(([value]) => value === previous)) {
    els.itemActionSelect.value = previous;
  }
  els.itemFurnitureField.hidden = els.itemActionSelect.value !== "place";
  renderItemActionHelp();
}

function renderItemActionHelp() {
  const action = els.itemActionSelect.value;
  const append = action === "consume" || (action === "consume_location" && selectedItem()?.item_type === "food");
  els.itemExecuteButton.textContent = append ? "使用して入力欄に追加" : "実行";
  document.getElementById("item-action-help").textContent = append
    ? "使用したアイテムの情報と画像を入力欄へ追加します。内容を確認して送信できます。"
    : action === "gift"
      ? "所持品をペルソナへ移し、本体と同じシステム通知を記録します。操作だけで贈与が完了します。"
      : "本体と同じ所持品・場所のデータを更新します。操作だけで完了します。";
}

function renderItemPreview() {
  const item = selectedItem();
  els.itemPreview.replaceChildren();
  if (!item) {
    els.itemDetail.textContent = "利用できるアイテムがありません。";
    els.itemExecuteButton.disabled = true;
    return;
  }
  els.itemExecuteButton.disabled = false;
  if (item.image_path) {
    const img = document.createElement("img");
    img.alt = item.name || "アイテム画像";
    img.addEventListener("click", () => openImageDialog(img.dataset.fullSrc || img.src));
    els.itemPreview.appendChild(img);
    loadAttachmentImage(img, item.image_path);
  }
  const text = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = `${item.name || "アイテム"} ×${item.amount}`;
  const description = document.createElement("p");
  description.textContent = item.description || item.category || "詳細なし";
  text.append(title, description);
  els.itemPreview.appendChild(text);
  els.itemDetail.textContent = item.furniture ? `配置場所: ${item.furniture}` : "操作内容を選んで実行してください。";
  renderItemActionHelp();
}

async function loadItems() {
  if (!state.connected || !state.roomId) {
    return;
  }
  const room = state.roomId, base = state.apiBase, generation = homeFeatureGeneration;
  const current = () => room === state.roomId && base === state.apiBase && generation === homeFeatureGeneration;
  els.itemRefreshButton.disabled = true;
  els.itemDetail.textContent = "アイテムを読み込んでいます...";
  try {
    const target = els.itemTargetSelect.value;
    const location = target === "location" ? els.locationSelect.value : "";
    const response = await api(
      `/api/v1/rooms/${encodeURIComponent(room)}/items?target=${encodeURIComponent(target)}&location=${encodeURIComponent(location)}`,
      { timeoutMs: 10000 }
    );
    if (!current()) return;
    state.items = response.items || [];
    els.itemSelect.replaceChildren();
    for (const item of state.items) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.name || item.id} ×${item.amount}${item.furniture ? ` / ${item.furniture}` : ""}`;
      els.itemSelect.appendChild(option);
    }
    renderItemActions();
    renderItemPreview();
  } catch (error) {
    if (!current()) return;
    state.items = [];
    els.itemSelect.replaceChildren();
    els.itemDetail.textContent = `アイテムを取得できませんでした: ${error.message}`;
    els.itemExecuteButton.disabled = true;
  } finally {
    if (current()) els.itemRefreshButton.disabled = false;
  }
}

async function openItemDialog() {
  if (!state.connected || !state.roomId) {
    setSyncStatus("APIに接続してください。", "warn");
    return;
  }
  els.itemDialog.showModal();
  await loadItems();
}

async function executeItemAction() {
  const item = selectedItem();
  if (!item || state.sending || state.confirmingSend || els.itemExecuteButton.disabled || state.mode === "travel") {
    return;
  }
  const actionLabel = els.itemActionSelect.selectedOptions[0]?.textContent || "操作";
  const action = els.itemActionSelect.value;
  const actionText = { gift: "ペルソナに贈ります", consume: "自分で使います", place: "現在地に置きます", pickup: "拾います", consume_location: "その場で使います" }[action] || actionLabel;
  const amount = Math.max(1, Number(els.itemAmountInput.value || 1));
  if (!window.confirm(`「${item.name}」を${amount}個、${actionText}。よろしいですか？`)) {
    return;
  }
  els.itemExecuteButton.disabled = true;
  els.itemExecuteButton.textContent = "実行中";
  const roomId = state.roomId;
  const generation = homeFeatureGeneration, base = state.apiBase;
  const payload = {
    action, item_id: item.id, amount,
    location: els.locationSelect.value || "",
    furniture: action === "place" ? els.itemFurnitureInput.value.trim() : (item.furniture || ""),
  };
  const signature = JSON.stringify([base, roomId, payload]);
  if (!pendingItemAction || pendingItemAction.signature !== signature) {
    pendingItemAction = { signature, payload: { ...payload, client_action_id: `lite-item-${createMessageId()}` } };
  }
  const request = pendingItemAction;
  try {
    const response = await api(`/api/v1/rooms/${encodeURIComponent(roomId)}/items/actions`, {
      method: "POST",
      body: JSON.stringify(request.payload),
      timeoutMs: 20000
    });
    if (pendingItemAction === request) pendingItemAction = null;
    if (roomId !== state.roomId || state.mode !== "home" || base !== state.apiBase || generation !== homeFeatureGeneration) return;
    els.itemDialog.close();
    setActivePage("chat");
    if (["consume", "consume_location"].includes(response.action) && response.chat_context) {
      els.messageInput.value = [els.messageInput.value, response.chat_context].filter(Boolean).join("\n\n");
      if (response.item?.image_path) {
        state.preparedChatAttachments = [...new Set([...state.preparedChatAttachments, response.item.image_path])];
      }
      els.messageInput.dispatchEvent(new Event("input"));
      renderComposerAttachments();
      els.messageInput.focus();
      setSyncStatus("アイテム操作を入力欄へ追加しました。文章と画像を確認して送信できます。");
    } else {
      setSyncStatus(action === "gift" ? "アイテムをペルソナへ贈りました。" : "アイテム操作を完了しました。");
      await loadHistory().catch(() => showHistoryReloadNotice());
    }
  } catch (error) {
    if (roomId === state.roomId && state.mode === "home" && generation === homeFeatureGeneration) {
      els.itemDetail.textContent = `アイテム操作の結果を確認できませんでした: ${error.message}。同じ操作の再確認は重複実行を防ぎます。`;
    }
  } finally {
    if (roomId === state.roomId && state.mode === "home" && generation === homeFeatureGeneration) {
      els.itemExecuteButton.disabled = false;
      renderItemActionHelp();
    }
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
  if (!state.connected || !state.roomId || !els.managementDetails.open || state.activePage === "chat") {
    return;
  }
  if (state.managementLoaded && !force) {
    return;
  }
  setManagementStatus("読込中", "busy");
  els.draftRefreshButton.disabled = true;
  try {
    let draftCount = 0;
    if (state.activePage === "management") {
      draftCount = await loadTwitterDrafts();
      await loadAutonomy();
    } else if (state.activePage === "settings") {
      await loadEventNotificationSettings();
    }
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
  if (state.mode === "travel") {
    await loadTravelHistory();
    setSyncStatus("独立モード履歴を再取得しました。");
    return;
  }
  if (!state.connected || !state.roomId) {
    return;
  }
  const room = state.roomId, base = state.apiBase;
  const current = () => state.mode === "home" && state.roomId === room && state.apiBase === base
    && !state.travelEntryPromise && !state.returningHome;
  if (els.syncButton) {
    els.syncButton.disabled = true;
  }
  setSyncStatus("履歴を再取得中...");
  try {
    await loadHistory();
    if (!current()) return;
    await refreshStatus();
    if (!current()) return;
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
  let history;
  try {
    history = await loadHistory();
  } catch (firstError) {
    const message = String(firstError?.message || "");
    if (message.startsWith("401 ") || message.startsWith("403 ")) throw firstError;
    await new Promise((resolve) => window.setTimeout(resolve, 800));
    history = await loadHistory();
  }

  if (state.mode !== "home") return history;
  const auxiliary = await Promise.allSettled([refreshStatus(), loadLocations()]);
  const failed = auxiliary.filter((result) => result.status === "rejected");
  if (failed.length) {
    setSyncStatus("チャットは接続済みです。補助情報の一部を取得できませんでした。", "warn");
  }
  return history;
}

async function connect(options = {}) {
  if (state.homeConnectPromise) return state.homeConnectPromise;
  const operation = connectHomeOnce(options);
  state.homeConnectPromise = operation;
  try { return await operation; }
  finally {
    if (state.homeConnectPromise === operation) state.homeConnectPromise = null;
    renderChatConnectivity();
  }
}

async function connectHomeOnce({ collapse = true, useInputs = false, readOnly = false } = {}) {
  if (useInputs) {
    resetHomeFeatures();
    const rawHomeBase = els.apiBaseInput.value;
    state.apiBase = normalizeBase(rawHomeBase) || homeDefaultBase();
    els.apiBaseInput.value = state.apiBase;
    showHomeApiUrlCorrection(rawHomeBase, state.apiBase);
    state.token = els.tokenInput.value.trim();
    writeConnectionValue("nexusLite.apiBase", state.apiBase);
    writeConnectionValue("nexusLite.token", state.token);
  } else {
    state.apiBase = state.apiBase || initialHomeBase();
    state.token = state.token || readConnectionValue("nexusLite.token");
  }
  const base = state.apiBase;
  const token = state.token;
  const current = () => state.mode === "home" && state.apiBase === base && state.token === token
    && !state.travelEntryPromise && !state.travelSession && !state.currentTravelSession && !state.returningHome;
  updateConnectionSummary();
  renderSecureOriginNotice();
  if (!state.apiBase) {
    setConnectionStatus("本体接続は未設定です", "warn");
    throw new Error("本体のAPI URLを入力してください。独立モードのみ使う場合は、下のLite用クラウドの状態を確認してください。");
  }
  setConnectionStatus("再接続中", "busy");
  setConnectivityStep("home", "checking", "checking", "再接続中", "保存済みの設定で接続を確認しています。");
  els.connectButton.disabled = true;
  try {
    try {
      const rooms = await api("/api/v1/rooms");
      if (!current()) return;
      state.rooms = rooms;
    } catch (firstError) {
      const message = String(firstError?.message || "");
      if (message.startsWith("401 ") || message.startsWith("403 ")) throw firstError;
      await new Promise((resolve) => window.setTimeout(resolve, 800));
      if (!current()) return;
      const rooms = await api("/api/v1/rooms");
      if (!current()) return;
      state.rooms = rooms;
    }
    state.homeReachable = true;
    const previousRoom = state.roomId;
    renderRooms();
    if (state.roomId !== previousRoom) restoreComposerDraft();
    state.connected = true;
    setConnectionStatus("接続済み", "ok");
    setSyncStatus("状態と履歴を取得中...");
    await loadPrimaryRoomData();
    if (!current()) return;
    state.homeConnectionError = "";
    setConnectivityStep("home", "connected", "ok", "接続済み", "本体へ接続しています。");
    if (collapse) {
      els.connectionDetails.open = false;
    }
    if (!readOnly) refreshConnectionExtras().catch((error) => setSyncStatus(`補助情報の取得に失敗しました: ${error.message}`, "warn"));
  } catch (error) {
    if (!current()) return;
    state.connected = false;
    throw error;
  } finally {
    els.connectButton.disabled = false;
  }
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.itemSharing) return;
  let message = els.messageInput.value.trim();
  if (state.mode === "travel") {
    if (
      !message ||
      state.sending ||
      state.travelRouteChanging ||
      !state.travelRouteUsable ||
      state.travelBudgetStopped
    ) return;
    state.sending = true;
    setTravelRouteControls();
    setSyncStatus("送信を準備しています…");
    try {
      await sendTravelMessage(message);
      setSyncStatus("独立モードの応答を受信しました。本体へ自動切替しません。");
    } catch (error) {
      els.messageInput.value = message;
      travelAdapter.saveDraft(message);
      setSyncStatus(error.message, "warn");
    } finally {
      state.sending = false;
      setTravelRouteControls();
      renderTravelSendFailure();
    }
    return;
  }
  if ((!message && !state.selectedChatFiles.length && !state.preparedChatAttachments.length) || !state.roomId || state.sending || state.confirmingSend) {
    return;
  }
  if (state.pendingSend && state.pendingSend.roomId !== state.roomId) {
    setSyncStatus("別のルームの送信結果が未確認です。元のルームで履歴を確認してから送信してください。", "warn");
    return;
  }
  if (state.pendingSend && state.pendingSend.roomId === state.roomId) {
    appendMessage("system", "前回の送信結果を確認中です。再送する前に履歴を再取得します。");
    try {
      state.confirmingSend = true;
      setComposerAttachmentControls();
      await syncNow();
    } catch {
      setSyncStatus("前回の送信結果を確認できません。通信状態を確認してください。");
    } finally {
      state.confirmingSend = false;
      setComposerAttachmentControls();
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
  // 履歴再確認で添付が解消された場合は、その前の添付スナップショットを再送しない。
  message = els.messageInput.value.trim();
  if (!message && !state.selectedChatFiles.length && !state.preparedChatAttachments.length) return;
  const roomId = state.roomId;
  const selectedFiles = [...state.selectedChatFiles];
  const preparedAttachments = [...state.preparedChatAttachments];
  const submitKey = `${roomId}\n${message}\n${JSON.stringify(selectedFiles.map(selectedFileSignature))}\n${preparedAttachments.join("\n")}`;
  const now = Date.now();
  if (state.lastSubmitKey === submitKey && now - (state.lastSubmitAt || 0) < RECENT_SUBMIT_GUARD_MS) return;
  state.lastSubmitKey = submitKey;
  state.lastSubmitAt = now;
  state.sending = true;
  homeElement("stop-response-button").hidden = false;
  setComposerAttachmentControls();
  const clientMessageId = createMessageId();
  writePendingSend({
    id: clientMessageId,
    roomId: state.roomId,
    message,
    file: selectedFileSignature(selectedFiles[0]),
    files: selectedFiles.map(selectedFileSignature),
    attachments: preparedAttachments,
    confirmation: "sending",
    notifyOnResponse: document.hidden || !document.hasFocus(),
    sentAt: new Date().toISOString()
  });
  setSyncStatus("送信中...");
  els.messageInput.value = "";
  localStorage.removeItem(draftStorageKey());
  els.messageInput.style.height = "";
  els.sendButton.disabled = true;
  els.sendButton.textContent = "送信中";
  appendMessage("user", selectedFiles.length ? `${message || "画像を送ります。"}\n[添付: ${selectedFiles.map((file) => file.name).join("、")}]` : message);
  const pendingMessage = appendMessage("pending", "考えています...");
  try {
    const attachments = [...preparedAttachments];
    for (const file of selectedFiles) {
      const uploaded = await uploadSelectedImage(file, roomId);
      attachments.push(uploaded.attachment_id);
    }
    const response = await api(`/api/v1/rooms/${encodeURIComponent(roomId)}/chat`, {
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
    const responsePending = state.pendingSend;
    clearSelectedImage();
    writePendingSend(null);
    setSyncStatus("応答を受信しました。");
    await notifyResponseIfWanted(responseNotificationBody(response.reply || ""), responsePending).catch(() => {});
    try {
      await loadHistory();
    } catch {
      const agentMessage = appendMessage("agent", response.reply || "（応答なし）", {
        timestamp: response.timestamp,
        model: response.model
      });
      appendAttachmentImages(agentMessage, response.attachments || []);
      showHistoryReloadNotice();
    }
    await refreshStatus().catch(() => {
      setSyncStatus("応答は受信済みです。本体の状態は再読み込みしてください。", "warn");
    });
  } catch (error) {
    removeMessage(pendingMessage);
    if (error.code === "response_stopped") {
      writePendingSend(null);
      clearSelectedImage();
      setSyncStatus("応答要請を停止しました。自動では再送しません。");
      await loadHistory().catch(() => {});
      return;
    }
    els.messageInput.value = message;
    state.preparedChatAttachments = preparedAttachments;
    renderComposerAttachments();
    els.messageInput.dispatchEvent(new Event("input"));
    saveComposerDraft();
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
    homeElement("stop-response-button").hidden = true;
    setComposerAttachmentControls();
    els.sendButton.disabled = false;
    els.sendButton.textContent = "送信";
  }
}

els.apiBaseInput.value = state.apiBase;
showHomeApiUrlCorrection(readConnectionValue("nexusLite.apiBase"), state.apiBase);
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
    if (state.mode === "travel") {
      await syncNow();
    } else {
      await retryHomeConnection({ refresh: true });
    }
  } finally {
    state.syncing = false;
  }
};

// 管理情報は明示操作で取得。一時的現在地の適用状態だけは通常の状態更新にも含める。
// 返答が古いルームや接続先へ描画されないよう世代も照合する。
let homeFeatureGeneration = 0;
let userMemoHash = null;
const homeFeatureSequences = new Map();
const homeElement = (id) => document.getElementById(id);
const newItemGenerationDraft = () => ({
  profile: null, imagePath: null, photoFile: null, photoUrl: null, photoPath: null,
  imageSource: "photo", saved: false, busy: false, sharing: false, requests: {}, uploadController: null,
});
let itemGenerationDraft = newItemGenerationDraft();
const newTemporaryLocationDraft = () => ({
  file: null, url: null, uploadedPath: null, imagePath: null, imageAssetUrl: null,
  imagePresent: false, imageUnavailable: false, imageSnapshotCaptured: false,
  clearImage: false, imageDirty: false,
  dirty: false, version: 0, busy: false, uploadController: null,
  locationName: "", sceneryText: "", sourceDescription: "", aiHint: "", savedName: "",
  requests: { generate: null },
});
let temporaryLocationDraft = newTemporaryLocationDraft();
let itemGenerationInFlight = 0;
let pendingItemAction = null;

function itemGenerationInput() {
  const kind = homeElement("item-gen-kind").value;
  return {
    kind, name: homeElement("item-gen-name").value.trim(),
    category: homeElement("item-gen-category").value.trim() || kind,
    base_description: homeElement("item-gen-description").value.trim(),
  };
}

function itemManualProfile(input) {
  const profile = { description: input.base_description, flavor_text: homeElement("item-create-sensory").value.trim() };
  if (input.kind === "food") profile.taste_profile = {};
  return profile;
}

function itemChosenImage(draft) {
  return draft.imageSource === "generated" ? draft.imagePath : draft.photoPath;
}

function generatedProfileText(profile) {
  const labels = {
    flavor_text: "紹介文", description: "説明", appearance: "外観", color: "色", design: "形・デザイン",
    taste_profile: "味覚", sweetness: "甘味", saltiness: "塩味", sourness: "酸味", bitterness: "苦味", umami: "旨味",
    time_profile: "味の変化", time: "味の変化", top: "第一印象", middle: "中盤", last: "後味",
    onset: "口に入れた時", development: "味わっている時", finish: "余韻", design_detail: "デザイン",
    physical_sensation: "身体感覚", physical: "物理的な特徴", temperature: "温度", texture: "質感",
    astringency: "渋み", viscosity: "とろみ", weight: "重み", synesthesia: "イメージ", emotion: "感情", landscape: "情景",
  };
  const lines = (data, depth = 0) => Object.entries(data || {}).flatMap(([key, value]) => {
    if (["name", "category", "kind", "base_description", "id", "creator", "amount", "image_path", "is_new"].includes(key) || value == null) return [];
    if (key === "physical" && data.physical_sensation) return [];
    const prefix = "  ".repeat(depth);
    const label = labels[key] || (key.endsWith("_desc") && labels[key.slice(0, -5)]
      ? `${labels[key.slice(0, -5)]}の特徴` : key);
    return typeof value === "object" && !Array.isArray(value)
      ? [`${prefix}${label}`, ...lines(value, depth + 1)]
      : [`${prefix}${label}: ${Array.isArray(value) ? value.join("、") : String(value)}`];
  });
  return lines(profile).join("\n");
}

function renderItemGeneration() {
  const draft = itemGenerationDraft, pendingSave = draft.requests.save;
  const locked = draft.busy || Boolean(pendingSave);
  const preview = homeElement("item-gen-profile-preview");
  preview.textContent = draft.profile ? generatedProfileText(draft.profile) : "";
  preview.hidden = !draft.profile;
  const photoPreview = homeElement("item-photo-preview");
  if (photoPreview.dataset.path !== (draft.photoUrl || "")) {
    photoPreview.replaceChildren();
    photoPreview.dataset.path = draft.photoUrl || "";
    if (draft.photoUrl) {
      const img = document.createElement("img");
      img.src = draft.photoUrl;
      img.alt = "添付した写真";
      photoPreview.append(img);
    }
  }
  photoPreview.hidden = !draft.photoFile;
  photoPreview.classList.toggle("selected-item-image", Boolean(draft.photoFile) && draft.imageSource === "photo");
  homeElement("item-create-image-remove").hidden = !draft.photoFile;
  homeElement("item-photo-reference-field").hidden = !draft.photoFile;
  const imagePreview = homeElement("item-gen-image-preview");
  if (imagePreview.dataset.path !== (draft.imagePath || "")) {
    for (const img of imagePreview.querySelectorAll("img")) if (img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
    imagePreview.replaceChildren();
    imagePreview.dataset.path = draft.imagePath || "";
    if (draft.imagePath) {
      const img = document.createElement("img");
      img.alt = "生成したアイテム画像";
      imagePreview.append(img);
      loadAttachmentImage(img, draft.imagePath).then(() => {
        if (!img.isConnected && img.src.startsWith("blob:")) URL.revokeObjectURL(img.src);
      });
    }
  }
  imagePreview.hidden = !draft.imagePath;
  imagePreview.classList.toggle("selected-item-image", Boolean(draft.imagePath) && draft.imageSource === "generated");
  const source = homeElement("item-gen-image-source");
  source.querySelector('[value="photo"]').disabled = !draft.photoFile;
  source.querySelector('[value="generated"]').disabled = !draft.imagePath;
  source.value = draft.imageSource;
  homeElement("item-gen-image-source-field").hidden = !(draft.photoFile && draft.imagePath);
  homeElement("item-share-image-status").textContent = `アイテムと一緒に共有する画像：${draft.imageSource === "photo" && draft.photoFile
    ? "添付した写真（画像生成の参照チェックに関係なく保存・共有します）"
    : draft.imagePath ? "生成した画像" : "なし"}`;
  homeElement("item-create-sensory-label").textContent = itemGenerationInput().kind === "food"
    ? "味・香り・食感（任意）" : "見た目・手触りなど（任意）";
  for (const element of homeElement("item-create-details").querySelectorAll("input, select, textarea, button")) element.disabled = locked;
  for (const [operation, id, label] of [
    ["profile", "item-gen-profile-button", "AIで説明を補完"],
    ["image", "item-gen-image-button", "画像を生成"],
  ]) homeElement(id).textContent = draft.requests[operation] ? "結果を確認" : label;
  for (const [id, share, label] of [
    ["item-gen-save-button", false, "所持品へ保存"],
    ["item-gen-share-button", true, "保存して入力欄へ追加"],
  ]) {
    const matching = pendingSave && Boolean(pendingSave.payload.prepare_chat) === share;
    homeElement(id).disabled = draft.busy || draft.saved || Boolean(pendingSave && !matching)
      || (share && (state.sending || state.confirmingSend));
    homeElement(id).textContent = matching ? "結果を確認" : label;
  }
  state.itemGenerating = itemGenerationInFlight > 0;
  state.itemSharing = Boolean(draft.busy && draft.sharing);
  setComposerAttachmentControls();
  renderLiteUpdateBanner();
}

function resetItemGeneration(clearFields = false) {
  const old = itemGenerationDraft;
  old.uploadController?.abort();
  itemGenerationDraft = newItemGenerationDraft();
  if (clearFields) {
    if (old.photoUrl) URL.revokeObjectURL(old.photoUrl);
    for (const id of ["item-gen-name", "item-gen-description", "item-create-sensory", "item-gen-image-prompt", "item-create-image-input"]) homeElement(id).value = "";
    homeElement("item-gen-kind").value = "food";
    homeElement("item-gen-category").value = "food";
    homeElement("item-gen-amount").value = "1";
    homeElement("item-photo-reference").checked = false;
  } else {
    for (const key of ["photoFile", "photoUrl", "photoPath", "imagePath", "imageSource"]) itemGenerationDraft[key] = old[key];
  }
  homeElement("item-gen-status").textContent = "手入力で保存するか、AIで説明を補完してください。入力欄へ追加しても自動送信されません。";
  renderItemGeneration();
}

async function uploadItemPhoto(draft, room, base, current) {
  if (!draft.photoFile || draft.photoPath) return;
  const controller = new AbortController();
  draft.uploadController = controller;
  const timeout = window.setTimeout(() => controller.abort(), 120000);
  try {
    homeElement("item-gen-status").textContent = "添付写真を本体へ送っています...";
    const response = await uploadSelectedImage(draft.photoFile, room, { apiBase: base, headers: authHeaders(), signal: controller.signal });
    if (!current()) return;
    if (!response.attachment_id) throw new Error("写真の保存先を確認できませんでした。");
    draft.photoPath = response.attachment_id;
  } finally {
    window.clearTimeout(timeout);
    draft.uploadController = null;
  }
}

async function runItemGeneration(operation, prepareChat = false) {
  const draft = itemGenerationDraft;
  if (draft.busy || !state.connected || state.mode !== "home" || !state.roomId) return;
  if (draft.requests.save && (operation !== "save" || Boolean(draft.requests.save.payload.prepare_chat) !== prepareChat)) return;
  const input = itemGenerationInput(), status = homeElement("item-gen-status");
  if (!input.name) { status.textContent = "名前を入力してください。"; return; }
  if (operation === "save" && draft.saved) return;
  if (prepareChat && (state.sending || state.confirmingSend)) { status.textContent = "チャット送信が終わってから入力欄へ追加してください。"; return; }
  const amount = Math.min(99, Math.max(1, Math.trunc(Number(homeElement("item-gen-amount").value) || 1)));
  const referencePhoto = operation === "image" && Boolean(draft.photoFile) && homeElement("item-photo-reference").checked;
  const chosenPhoto = operation !== "image" && draft.imageSource === "photo" && Boolean(draft.photoFile);
  let request = draft.requests[operation];
  if (!request) {
    const prompt = operation === "save"
      ? `「${input.name}」を${amount}個、所持品へ保存します。${prepareChat ? "共有文と画像を入力欄へ追加しますが、自動送信・消費・贈与は行いません。" : ""}よろしいですか？`
      : `本体の設定で${operation === "image" ? "画像を生成" : "説明を補完"}します。入力${(chosenPhoto || referencePhoto || (operation === "profile" && draft.imagePath)) ? "と選択した画像" : ""}をAIへ送信し、利用料がかかる場合があります。実行しますか？`;
    if (!window.confirm(prompt)) return;
  }
  const room = state.roomId, generation = homeFeatureGeneration, base = state.apiBase;
  const current = () => draft === itemGenerationDraft && state.mode === "home" && room === state.roomId
    && base === state.apiBase && generation === homeFeatureGeneration;
  draft.busy = true;
  draft.sharing = prepareChat;
  itemGenerationInFlight += 1;
  renderItemGeneration();
  try {
    if (chosenPhoto || referencePhoto) await uploadItemPhoto(draft, room, base, current);
    if (!current()) return;
    const sensory = homeElement("item-create-sensory").value.trim();
    const payload = operation === "image"
      ? { prompt: homeElement("item-gen-image-prompt").value.trim() || `${input.name}\n${input.base_description}`,
          reference_image_paths: referencePhoto ? [draft.photoPath] : [] }
      : operation === "save"
        ? { ...input, amount, profile: draft.profile || itemManualProfile(input), image_path: itemChosenImage(draft), prepare_chat: prepareChat }
        : { ...input, base_description: [input.base_description, sensory ? `${input.kind === "food" ? "味・香り・食感" : "感想"}: ${sensory}` : ""].filter(Boolean).join("\n"), image_path: itemChosenImage(draft) };
    const signature = JSON.stringify(payload);
    if (request && request.signature !== signature) throw new Error("確認中の要求と入力が一致しません。");
    if (!request) {
      request = { signature, payload: { ...payload, client_request_id: `lite-item-${operation}-${createMessageId()}` } };
      draft.requests[operation] = request;
    }
    status.textContent = operation === "save" ? "保存結果を確認中..." : "生成結果を確認中...";
    const tail = { profile: "generate-profile", image: "generate-image", save: "generated" }[operation];
    const response = await api(homeFeaturePath(room, `items/${tail}`), {
      method: "POST", body: JSON.stringify(request.payload), timeoutMs: 120000,
    });
    if (!current()) return;
    if (response.status === "in_progress") { status.textContent = "本体で処理が続いています。「結果を確認」で同じ処理の結果を取得できます。"; return; }
    if (response.status === "failed") { draft.requests[operation] = null; status.textContent = response.error || "処理に失敗しました。"; return; }
    if (!["succeeded", "replayed", "saved"].includes(response.status)) throw new Error("処理結果を確認できませんでした。");
    if (operation === "profile") {
      if (!response.profile || !Object.keys(response.profile).length) throw new Error("生成内容を確認できませんでした。");
      draft.profile = response.profile;
      draft.saved = false;
      draft.requests.save = null;
      status.textContent = "説明を補完しました。内容と保存する画像を確認してください。";
    } else if (operation === "image") {
      if (!response.image_path) throw new Error("生成画像を確認できませんでした。");
      draft.imagePath = response.image_path;
      draft.imageSource = "generated";
      draft.saved = false;
      draft.requests.save = null;
      status.textContent = "画像を生成しました。添付写真がある場合は、保存する画像を選べます。";
    } else {
      if (!response.item?.id) throw new Error("保存結果を確認できませんでした。");
      if (request.payload.prepare_chat && typeof response.chat_context !== "string") throw new Error("共有文を確認できませんでした。");
      draft.saved = true;
      if (request.payload.prepare_chat) {
        const heading = `${request.payload.kind === "food" ? "食べ物" : "一般アイテム"}「${response.item.name || input.name}」を作りました。`;
        const sharingText = [heading, response.chat_context].filter(Boolean).join("\n");
        els.messageInput.value = [els.messageInput.value.trimEnd(), sharingText].filter(Boolean).join("\n\n");
        const image = response.image_path || response.item.image_path;
        if (image) state.preparedChatAttachments = [...new Set([...state.preparedChatAttachments, image])];
        els.messageInput.dispatchEvent(new Event("input"));
        renderComposerAttachments();
        saveComposerDraft();
        els.itemDialog.close();
        setActivePage("chat");
        els.messageInput.focus();
        setSyncStatus("アイテムの共有文と画像を入力欄へ追加しました。内容を確認して送信できます。");
      }
      status.textContent = `「${response.item.name || input.name}」を所持品へ保存しました。${request.payload.prepare_chat ? "共有文と画像を入力欄へ追加しました。" : "「アイテムを使用」から使えます。"}`;
    }
    draft.requests[operation] = null;
    if (operation === "save") await loadItems();
  } catch (error) {
    if (current()) {
      if (error.status >= 400 && error.status < 500 && error.status !== 429) draft.requests[operation] = null;
      status.textContent = `結果を確認できませんでした: ${error.message}${draft.requests[operation] ? "。「結果を確認」で同じ処理を照合できます。" : "。入力・写真を確認してもう一度操作してください。"}`;
    }
  } finally {
    itemGenerationInFlight -= 1;
    if (current()) draft.busy = false;
    renderItemGeneration();
  }
}

for (const [operation, id, share] of [["profile", "item-gen-profile-button", false], ["image", "item-gen-image-button", false], ["save", "item-gen-save-button", false], ["save", "item-gen-share-button", true]]) {
  homeElement(id).addEventListener("click", () => runItemGeneration(operation, share));
}
for (const id of ["item-gen-kind", "item-gen-name", "item-gen-category", "item-gen-description", "item-create-sensory"]) {
  homeElement(id).addEventListener("input", () => {
    if (itemGenerationDraft.busy || itemGenerationDraft.requests.save) return;
    if (id === "item-gen-kind") homeElement("item-gen-category").value = homeElement(id).value;
    const hadProfile = Boolean(itemGenerationDraft.profile);
    resetItemGeneration();
    if (hadProfile) homeElement("item-gen-status").textContent = "入力が変わったためAIの補完結果を解除しました。手入力で保存するか、もう一度補完してください。";
  });
}
homeElement("item-gen-amount").addEventListener("input", () => {
  if (itemGenerationDraft.busy || itemGenerationDraft.requests.save) return;
  itemGenerationDraft.saved = false;
  renderItemGeneration();
});
homeElement("item-gen-image-prompt").addEventListener("input", () => {
  if (itemGenerationDraft.busy || itemGenerationDraft.requests.save) return;
  itemGenerationDraft.requests.image = null;
  renderItemGeneration();
});
homeElement("item-photo-reference").addEventListener("change", () => {
  if (itemGenerationDraft.busy || itemGenerationDraft.requests.save) return;
  itemGenerationDraft.requests.image = null;
  renderItemGeneration();
});
homeElement("item-gen-image-source").addEventListener("change", () => {
  if (itemGenerationDraft.busy || itemGenerationDraft.requests.save) return;
  itemGenerationDraft.imageSource = homeElement("item-gen-image-source").value;
  resetItemGeneration();
});
homeElement("item-create-image-input").addEventListener("change", () => {
  if (itemGenerationDraft.busy || itemGenerationDraft.requests.save) return;
  const file = homeElement("item-create-image-input").files?.[0];
  if (!file) return;
  if (file.size > 8 * 1024 * 1024 || !/\.(png|jpe?g|webp|gif)$/i.test(file.name)) {
    homeElement("item-gen-status").textContent = "PNG・JPEG・WebP・GIFの画像を8MB以内で選んでください。";
    homeElement("item-create-image-input").value = "";
    return;
  }
  const oldUrl = itemGenerationDraft.photoUrl;
  resetItemGeneration();
  if (oldUrl) URL.revokeObjectURL(oldUrl);
  Object.assign(itemGenerationDraft, { photoFile: file, photoUrl: URL.createObjectURL(file), photoPath: null, imageSource: "photo" });
  homeElement("item-gen-status").textContent = "写真を添付しました。必要な操作時に本体へ送り、保存・AI補完に使えます。";
  renderItemGeneration();
});
homeElement("item-create-image-remove").addEventListener("click", () => {
  if (itemGenerationDraft.busy || itemGenerationDraft.requests.save) return;
  const oldUrl = itemGenerationDraft.photoUrl;
  resetItemGeneration();
  if (oldUrl) URL.revokeObjectURL(oldUrl);
  Object.assign(itemGenerationDraft, { photoFile: null, photoUrl: null, photoPath: null,
    imageSource: itemGenerationDraft.imagePath ? "generated" : "photo" });
  homeElement("item-create-image-input").value = "";
  homeElement("item-photo-reference").checked = false;
  renderItemGeneration();
});

function homeFeaturePath(roomId, tail) {
  return `/api/v1/rooms/${encodeURIComponent(roomId)}/${tail}`;
}
async function homeFeatureAction(buttonId, statusId, action) {
  const button = homeElement(buttonId);
  const status = homeElement(statusId);
  if (temporaryLocationDraft.busy && buttonId.startsWith("temporary-location-")) {
    status.textContent = "一時的現在地の別操作を処理中です。";
    return;
  }
  if (!state.connected || !state.roomId || state.mode !== "home") {
    status.textContent = "本体へ接続してから操作してください。";
    return;
  }
  const roomId = state.roomId;
  const apiBase = state.apiBase;
  const generation = homeFeatureGeneration;
  const sequence = (homeFeatureSequences.get(statusId) || 0) + 1;
  homeFeatureSequences.set(statusId, sequence);
  const sameContext = () => roomId === state.roomId && apiBase === state.apiBase
    && generation === homeFeatureGeneration && state.mode === "home";
  const isCurrent = () => sameContext() && sequence === homeFeatureSequences.get(statusId);
  button.disabled = true;
  status.textContent = "処理中...";
  try {
    await action(roomId, isCurrent);
  } catch (error) {
    if (isCurrent()) status.textContent = `失敗: ${error.message}`;
  } finally {
    if (sameContext()) button.disabled = false;
  }
}
function resetHomeFeatures({ preserveStatus = false } = {}) {
  homeFeatureGeneration += 1;
  if (!preserveStatus) {
    state.statusSnapshot = null;
    state.statusReadFailed = false;
    renderRoomStatusFreshness();
  }
  resetItemGeneration(true);
  state.items = [];
  els.itemSelect.replaceChildren();
  els.itemPreview.replaceChildren();
  els.itemDetail.textContent = "一覧を読み込んでください。";
  els.itemExecuteButton.disabled = true;
  els.itemRefreshButton.disabled = false;
  state.chatLocation = "";
  state.temporaryLocation = null;
  state.temporaryLocationVersion += 1;
  state.temporaryLocationSaved = [];
  state.temporaryLocationSavedVersion += 1;
  resetTemporaryLocationDraft();
  renderChatContextLocation();
  userMemoHash = null;
  homeFeatureSequences.clear();
  for (const id of ["home-key-refresh", "autonomy-span-button", "temporary-location-refresh", "temporary-location-apply", "temporary-location-disable", "leave-message-refresh", "request-control-refresh", "stop-response-button"]) homeElement(id).disabled = false;
  for (const id of ["temporary-location-name", "temporary-location-text", "temporary-location-source-description",
    "temporary-location-ai-hint", "leave-message-text", "autonomy-minutes"]) homeElement(id).value = "";
  homeElement("leave-message-save").disabled = true;
  homeResponseSettings = null;
  homeResponseBusy = false;
  homeElement("home-key-select").replaceChildren();
  homeElement("home-response-model").replaceChildren();
  homeElement("home-response-scope").value = "common";
  homeElement("home-response-rotation").value = "common";
  homeElement("home-response-effective").textContent = "未読込";
  homeElement("home-response-common").textContent = "";
  updateHomeResponseControls();
  homeElement("request-control-list").replaceChildren();
  renderTemporaryLocationSavedList();
  homeElement("temporary-location-generation-status").textContent = "";
  homeElement("temporary-location-saved-status").textContent = "未読込";
  for (const id of ["home-key-status", "temporary-location-status", "leave-message-status", "request-control-status"]) homeElement(id).textContent = "未読込";
}
let homeResponseSettings = null;
let homeResponseBusy = false;

function responseRotationLabel(value) {
  return value ? "オン" : "オフ";
}

function updateHomeResponseControls() {
  const ready = Boolean(homeResponseSettings?.supported) && !homeResponseBusy;
  const individual = homeElement("home-response-scope").value === "room";
  homeElement("home-response-fields").hidden = !individual;
  homeElement("home-response-scope").disabled = !ready;
  for (const id of ["home-key-select", "home-response-model", "home-response-rotation"]) {
    homeElement(id).disabled = !ready || !individual;
  }
  homeElement("home-key-apply").disabled = !ready || (!individual && homeResponseSettings.common_provider !== "google");
  homeElement("home-key-refresh").disabled = homeResponseBusy;
  homeElement("home-key-apply").textContent = individual ? "このルームへ適用" : "応答設定を共通に戻す";
}

function renderHomeResponseSettings(response, { saved = false } = {}) {
  homeResponseSettings = response;
  const keys = homeElement("home-key-select");
  keys.replaceChildren();
  for (const key of response.keys || []) {
    const option = document.createElement("option");
    option.value = key.name;
    option.textContent = `${key.name}${key.paid ? " / 有料設定" : ""}`;
    keys.appendChild(option);
  }
  const models = homeElement("home-response-model");
  models.replaceChildren();
  const modelNames = [...new Set([...(response.models || []), response.effective_model_name].filter(Boolean))];
  for (const name of modelNames) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    models.appendChild(option);
  }
  const keyNames = (response.keys || []).map(key => key.name);
  keys.value = keyNames.includes(response.room_key_name) ? response.room_key_name : response.effective_key_name || "";
  models.value = modelNames.includes(response.room_model_name) ? response.room_model_name : response.effective_model_name || "";
  homeElement("home-response-scope").value = response.scope === "room" ? "room" : "common";
  homeElement("home-response-rotation").value = response.room_rotation == null ? "common" : response.room_rotation ? "on" : "off";
  const scope = response.scope === "room" ? "ルーム個別" : "共通設定";
  homeElement("home-response-effective").textContent = `現在の応答設定: ${scope} / ${response.effective_model_name || "モデル未設定"} / キー: ${response.effective_key_name || "未設定"} / ローテーション: ${responseRotationLabel(response.effective_rotation)}`;
  homeElement("home-response-common").textContent = response.common_provider === "google"
    ? `共通設定: ${response.common_model_name || "未設定"} / キー: ${response.common_key_name || "未設定"} / ローテーション: ${responseRotationLabel(response.common_rotation)}`
    : `共通設定のAIサービスは ${response.common_provider} です。共通設定へ戻す操作は本体で行ってください。`;
  homeElement("home-key-status").textContent = response.supported
    ? saved ? "応答設定を保存しました。次の応答から使用します。" : "現在の設定を取得しました。"
    : "この機能は本体接続モードのGeminiが対象です。他のAIサービスの設定は本体で変更してください。";
  updateHomeResponseControls();
}

async function homeResponseAction(buttonId, action) {
  if (homeResponseBusy) return;
  const generation = homeFeatureGeneration;
  homeResponseBusy = true;
  updateHomeResponseControls();
  try {
    await homeFeatureAction(buttonId, "home-key-status", action);
  } finally {
    if (generation === homeFeatureGeneration) {
      homeResponseBusy = false;
      updateHomeResponseControls();
    }
  }
}

homeElement("home-response-scope").addEventListener("change", updateHomeResponseControls);
homeElement("home-key-refresh").addEventListener("click", () => homeResponseAction("home-key-refresh", async (room, current) => {
  const response = await api(homeFeaturePath(room, "response-settings"), { timeoutMs: 10000 });
  if (current()) renderHomeResponseSettings(response);
}));
homeElement("home-key-apply").addEventListener("click", () => homeResponseAction("home-key-apply", async (room, current) => {
  if (!homeResponseSettings?.revision || !homeResponseSettings.supported) throw new Error("現在の設定を取得してから適用してください。");
  const individual = homeElement("home-response-scope").value === "room";
  const payload = { base_revision: homeResponseSettings.revision };
  if (individual) {
    payload.key_name = homeElement("home-key-select").value;
    payload.model_name = homeElement("home-response-model").value;
    const rotation = homeElement("home-response-rotation").value;
    payload.enable_api_key_rotation = rotation === "common" ? null : rotation === "on";
    if (!payload.key_name || !payload.model_name) throw new Error("登録済みのキーと応答モデルを選んでください。");
  }
  try {
    const response = await api(homeFeaturePath(room, "response-settings"), {
      method: individual ? "PUT" : "DELETE", body: JSON.stringify(payload),
    });
    if (current()) renderHomeResponseSettings(response, { saved: true });
  } catch (error) {
    if (current() && error.status === 409) {
      homeResponseSettings = null;
      throw new Error("本体または別の画面で設定が変わりました。「現在の設定を取得」で確認し直してください。");
    }
    throw error;
  }
}));
homeElement("autonomy-span-button").addEventListener("click", () => homeFeatureAction("autonomy-span-button", "autonomy-meta", async (room, current) => {
  const minutes = Number(homeElement("autonomy-minutes").value);
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 10080) throw new Error("1〜10080分の整数を入力してください。");
  const response = await api(homeFeaturePath(room, "autonomy/settings"), { method: "PUT", body: JSON.stringify({ inactivity_minutes: minutes }) });
  if (current()) { renderAutonomy(response); setSyncStatus("自律行動の間隔を保存しました。"); }
}));
function resetTemporaryLocationDraft() {
  const draft = temporaryLocationDraft;
  draft.uploadController?.abort();
  if (draft.url) URL.revokeObjectURL(draft.url);
  temporaryLocationDraft = newTemporaryLocationDraft();
  homeElement("temporary-location-image-input").value = "";
  renderTemporaryLocationPhoto();
}
function readTemporaryLocationDraft() {
  const draft = temporaryLocationDraft;
  draft.locationName = homeElement("temporary-location-name").value.trim();
  draft.sceneryText = homeElement("temporary-location-text").value.trim();
  draft.sourceDescription = homeElement("temporary-location-source-description").value.trim();
  draft.aiHint = homeElement("temporary-location-ai-hint").value.trim();
  return draft;
}

function setTemporaryLocationDraftForm(response, { saved = false } = {}) {
  const draft = temporaryLocationDraft;
  draft.locationName = String(response?.location_name || response?.name || "").trim();
  draft.sceneryText = String(response?.scenery_text || "");
  draft.sourceDescription = String(response?.source_description || "").trim();
  draft.aiHint = "";
  draft.savedName = saved ? draft.locationName : "";
  draft.imagePath = response?.image_path || null;
  draft.imageAssetUrl = response?.image_asset_url || null;
  draft.imagePresent = Boolean(response?.has_image) && !response?.image_unavailable;
  draft.imageUnavailable = Boolean(response?.image_unavailable);
  draft.imageSnapshotCaptured = true;
  draft.imageDirty = Boolean(saved);
  draft.clearImage = Boolean(saved && (!response?.has_image || response?.image_unavailable));
  draft.dirty = Boolean(saved);
  draft.requests = { generate: null };
  homeElement("temporary-location-name").value = draft.locationName;
  homeElement("temporary-location-text").value = draft.sceneryText;
  homeElement("temporary-location-source-description").value = draft.sourceDescription;
  homeElement("temporary-location-ai-hint").value = draft.aiHint;
  homeElement("temporary-location-image-input").value = "";
}

function temporaryLocationImagePath(draft = temporaryLocationDraft) {
  if (draft.clearImage) return "";
  return draft.uploadedPath || draft.imagePath
    || (!draft.dirty && !draft.imageDirty ? state.temporaryLocation?.image_path : "") || "";
}

function temporaryLocationDraftSignature(draft = temporaryLocationDraft) {
  return JSON.stringify({
    location_name: draft.locationName,
    scenery_text: draft.sceneryText,
    source_description: draft.sourceDescription,
    user_hint: draft.aiHint,
    image_path: temporaryLocationImagePath(draft),
  });
}

function temporaryLocationAssetUrl(path) {
  if (!path) return "";
  const room = state.roomId;
  if (!room) return "";
  try {
    const base = new URL(state.apiBase || window.location.origin, window.location.origin);
    const url = new URL(path, base);
    if (!["http:", "https:"].includes(url.protocol) || url.origin !== base.origin) return "";
    const expectedPaths = [
      homeFeaturePath(room, "temporary-location/image"),
      homeFeaturePath(room, "temporary-location/saved-image"),
    ].map((value) => new URL(value, base).pathname);
    if (!expectedPaths.includes(url.pathname)) return "";
    return url.href;
  } catch {
    return "";
  }
}

async function loadTemporaryLocationPhoto(img, assetUrl = "") {
  const room = state.roomId, base = state.apiBase, generation = homeFeatureGeneration;
  try {
    const url = temporaryLocationAssetUrl(assetUrl || homeFeaturePath(room, "temporary-location/image"));
    if (!url) throw new Error("写真の取得先を確認できませんでした。");
    const response = await fetch(url, { headers: authHeaders() });
    if (!response.ok) throw new Error("写真を確認できませんでした。");
    const blob = await response.blob();
    if (!img.isConnected || room !== state.roomId || base !== state.apiBase || generation !== homeFeatureGeneration) return;
    const objectUrl = URL.createObjectURL(blob);
    img.src = objectUrl;
    img.dataset.fullSrc = objectUrl;
    img.dataset.objectUrl = objectUrl;
  } catch {
    if (img.isConnected) img.replaceWith(document.createTextNode("写真を読み込めませんでした。"));
  }
}

function renderTemporaryLocationPhoto() {
  const draft = temporaryLocationDraft, current = state.temporaryLocation;
  const preview = homeElement("temporary-location-image-preview");
  const draftPhotoSnapshot = draft.dirty || draft.imageDirty;
  const path = draft.url || temporaryLocationImagePath(draft);
  const hasPhoto = Boolean(draft.file || (!draft.clearImage && (draftPhotoSnapshot ? draft.imagePresent : current?.has_image)));
  const assetUrl = draft.url ? "" : (draftPhotoSnapshot ? draft.imageAssetUrl : current?.image_asset_url || "");
  const key = `${path}|${hasPhoto}|${assetUrl}|${draft.url ? "draft" : draftPhotoSnapshot ? "snapshot" : current?.created_at || ""}`;
  if (preview.dataset.path !== key) {
    for (const img of preview.querySelectorAll("img")) {
      if (img.dataset.objectUrl) URL.revokeObjectURL(img.dataset.objectUrl);
    }
    preview.replaceChildren();
    preview.dataset.path = key;
    if (path && hasPhoto) {
      const img = document.createElement("img");
      img.alt = "一時的現在地の写真";
      preview.append(img);
      if (draft.url) img.src = draft.url;
      else if (assetUrl || !draftPhotoSnapshot) loadTemporaryLocationPhoto(img, assetUrl);
      else preview.textContent = "写真を保存済みです（プレビューなし）。";
    } else if (hasPhoto) preview.textContent = "写真を保存済みです（プレビューなし）。";
  }
  preview.hidden = !hasPhoto;
  homeElement("temporary-location-image-remove").hidden = !hasPhoto && !draft.imageUnavailable;
  const ids = [
    "temporary-location-name", "temporary-location-text", "temporary-location-source-description",
    "temporary-location-ai-hint", "temporary-location-image-input", "temporary-location-image-remove",
    "temporary-location-refresh", "temporary-location-generate", "temporary-location-saved-refresh",
    "temporary-location-saved-load", "temporary-location-save", "temporary-location-apply", "temporary-location-disable",
  ];
  for (const id of ids) homeElement(id).disabled = draft.busy;
  homeElement("temporary-location-saved-select").disabled = draft.busy || !state.temporaryLocationSaved.length;
  const generateRequest = draft.requests.generate;
  homeElement("temporary-location-generate").textContent = generateRequest
    && generateRequest.signature === temporaryLocationDraftSignature(draft)
    ? "結果を確認"
    : "写真からAIで情景文を生成";
}

function renderTemporaryLocationDraftStatus() {
  const draft = temporaryLocationDraft, current = state.temporaryLocation;
  const appliedName = String(current?.location_name || "").trim();
  let message = current?.active
    ? `一時的現在地「${appliedName || "名前なし"}」を適用中${current.has_image ? "（写真あり）" : "（写真なし）"}`
    : "仮想現在地を使用中（一時的現在地の情景・写真は保持されています）";
  if (draft.dirty) {
    message += draft.savedName
      ? ` ／ 保存済み「${draft.savedName}」の下書き（未適用）`
      : " ／ 編集中の下書き（未適用）";
  }
  if (draft.imageUnavailable) {
    message += " ／ 保存写真を読み込めませんでした。写真を添付し直すか、写真を外してから保存・適用してください。";
  }
  homeElement("temporary-location-status").textContent = message;
  renderTemporaryLocationPhoto();
}

function renderTemporaryLocation(response, { updateForm = false } = {}) {
  state.temporaryLocation = response;
  if (updateForm || (!temporaryLocationDraft.dirty && !temporaryLocationDraft.busy)) {
    setTemporaryLocationDraftForm(response);
  } else if (!temporaryLocationDraft.imageDirty && !temporaryLocationDraft.imageSnapshotCaptured) {
    // 入力がcurrent GETより先に始まっても、最初に確認できた安全な写真snapshotだけ取り込む。
    temporaryLocationDraft.imagePath = response?.image_path || null;
    temporaryLocationDraft.imageAssetUrl = response?.image_asset_url || null;
    temporaryLocationDraft.imagePresent = Boolean(response?.has_image) && !response?.image_unavailable;
    temporaryLocationDraft.imageUnavailable = Boolean(response?.image_unavailable);
    temporaryLocationDraft.imageSnapshotCaptured = true;
  }
  renderChatContextLocation();
  renderTemporaryLocationDraftStatus();
}

function renderTemporaryLocationSavedList() {
  const select = homeElement("temporary-location-saved-select");
  const selected = select.value;
  select.replaceChildren();
  for (const location of state.temporaryLocationSaved) {
    const option = document.createElement("option");
    option.value = location.name;
    option.textContent = `${location.name}${location.has_image ? "（写真あり）" : ""}`;
    select.appendChild(option);
  }
  if (!state.temporaryLocationSaved.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "保存した場所はありません";
    select.appendChild(option);
  } else if (state.temporaryLocationSaved.some((location) => location.name === selected)) {
    select.value = selected;
  }
  select.disabled = !state.temporaryLocationSaved.length || temporaryLocationDraft.busy;
}

async function uploadTemporaryLocationPhoto(draft, room, base, current) {
  if (!draft.file || draft.uploadedPath) return;
  const controller = new AbortController();
  draft.uploadController = controller;
  const timeout = window.setTimeout(() => controller.abort(), 120000);
  try {
    homeElement("temporary-location-status").textContent = "写真を本体へ送っています...";
    const uploaded = await uploadSelectedImage(draft.file, room, { apiBase: base, headers: authHeaders(), signal: controller.signal });
    if (!current() || draft !== temporaryLocationDraft) return;
    if (!uploaded.attachment_id) throw new Error("写真の保存先を確認できませんでした。");
    draft.uploadedPath = uploaded.attachment_id;
    draft.imagePath = uploaded.attachment_id;
    draft.imageAssetUrl = null;
    draft.imagePresent = true;
    draft.imageUnavailable = false;
    draft.imageSnapshotCaptured = true;
  } finally {
    window.clearTimeout(timeout);
    draft.uploadController = null;
  }
}

function addTemporaryLocationImagePayload(payload, draft, { preserveSnapshot = false } = {}) {
  if (!draft.imageDirty && !preserveSnapshot) return;
  if (draft.clearImage) payload.clear_image = true;
  else if (draft.uploadedPath || draft.imagePath) payload.image_path = draft.uploadedPath || draft.imagePath;
}

function ensureTemporaryLocationPhotoAvailable(draft) {
  if (draft.imageUnavailable && !draft.file && !draft.uploadedPath && !draft.imagePath) {
    throw new Error("保存写真を読み込めませんでした。写真を添付し直すか、写真を外してから保存・適用してください。");
  }
}

for (const id of ["temporary-location-name", "temporary-location-text", "temporary-location-source-description", "temporary-location-ai-hint"]) {
  homeElement(id).addEventListener("input", () => {
    const draft = temporaryLocationDraft;
    if (draft.busy) return;
    readTemporaryLocationDraft();
    draft.version += 1;
    draft.dirty = true;
    draft.savedName = "";
    if (draft.requests.generate && draft.requests.generate.signature !== temporaryLocationDraftSignature()) {
      homeElement("temporary-location-generation-status").textContent = "入力が変わりました。古い生成結果は適用せず、新しい内容で生成してください。";
    }
    renderTemporaryLocationDraftStatus();
  });
}

homeElement("temporary-location-image-input").addEventListener("change", () => {
  const draft = temporaryLocationDraft, file = homeElement("temporary-location-image-input").files?.[0];
  if (!file || draft.busy) return;
  if (file.size > 8 * 1024 * 1024 || !/\.(png|jpe?g|webp|gif)$/i.test(file.name)) {
    homeElement("temporary-location-image-input").value = "";
    homeElement("temporary-location-status").textContent = "8MB以下のPNG・JPEG・WebP・GIFを選んでください。";
    return;
  }
  if (draft.url) URL.revokeObjectURL(draft.url);
  Object.assign(draft, {
    file, url: URL.createObjectURL(file), uploadedPath: null, imagePath: null, imageAssetUrl: null,
    imagePresent: true, imageUnavailable: false, imageSnapshotCaptured: true,
    clearImage: false, imageDirty: true, dirty: true, savedName: "",
  });
  draft.version += 1;
  homeElement("temporary-location-status").textContent = "写真を選びました。内容を確認してから保存または適用してください。";
  renderTemporaryLocationPhoto();
});

homeElement("temporary-location-image-remove").addEventListener("click", () => {
  const draft = temporaryLocationDraft;
  if (draft.busy) return;
  if (draft.url) URL.revokeObjectURL(draft.url);
  Object.assign(draft, {
    file: null, url: null, uploadedPath: null, imagePath: null, imageAssetUrl: null,
    imagePresent: false, imageUnavailable: false, imageSnapshotCaptured: true,
    clearImage: true, imageDirty: true, dirty: true, savedName: "",
  });
  draft.version += 1;
  homeElement("temporary-location-image-input").value = "";
  homeElement("temporary-location-status").textContent = "写真を外しました。内容を確認してから保存または適用してください。";
  renderTemporaryLocationPhoto();
});

homeElement("temporary-location-refresh").addEventListener("click", () => homeFeatureAction("temporary-location-refresh", "temporary-location-status", async (room, current) => {
  if (temporaryLocationDraft.dirty && !window.confirm("編集中の内容を破棄して、現在の情景と写真を読み直しますか？")) {
    homeElement("temporary-location-status").textContent = "編集中の内容を保持しました。変更はまだ適用していません。";
    return;
  }
  state.temporaryLocationVersion += 1;
  const draft = temporaryLocationDraft;
  draft.busy = true;
  renderTemporaryLocationPhoto();
  try {
    const response = await api(homeFeaturePath(room, "temporary-location"), { timeoutMs: 10000 });
    if (current()) {
      state.temporaryLocationVersion += 1;
      state.temporaryLocation = response;
      resetTemporaryLocationDraft();
      renderTemporaryLocation(response, { updateForm: true });
    }
  } finally {
    draft.busy = false;
    if (current()) { temporaryLocationDraft.busy = false; renderTemporaryLocationPhoto(); }
  }
}));

homeElement("temporary-location-saved-refresh").addEventListener("click", () => homeFeatureAction("temporary-location-saved-refresh", "temporary-location-saved-status", async (room, current) => {
  if (temporaryLocationDraft.busy) return;
  const version = ++state.temporaryLocationSavedVersion;
  const response = await api(homeFeaturePath(room, "temporary-location/saved"), { timeoutMs: 10000 });
  if (!current() || version !== state.temporaryLocationSavedVersion) return;
  state.temporaryLocationSaved = Array.isArray(response.locations) ? response.locations : [];
  renderTemporaryLocationSavedList();
  homeElement("temporary-location-saved-status").textContent = `${state.temporaryLocationSaved.length}件の保存地点を読み込みました。`;
}));

homeElement("temporary-location-saved-load").addEventListener("click", () => homeFeatureAction("temporary-location-saved-load", "temporary-location-saved-status", async (room, current) => {
  if (temporaryLocationDraft.busy) return;
  if (temporaryLocationDraft.dirty && !window.confirm("編集中の内容を破棄して、保存した場所を下書きへ呼び出しますか？")) {
    homeElement("temporary-location-saved-status").textContent = "編集中の内容を保持しました。保存地点は呼び出していません。";
    return;
  }
  const name = homeElement("temporary-location-saved-select").value.trim();
  if (!name) throw new Error("呼び出す保存地点を選んでください。");
  const draft = temporaryLocationDraft;
  const draftVersion = draft.version;
  const signature = temporaryLocationDraftSignature(draft);
  draft.busy = true;
  renderTemporaryLocationPhoto();
  try {
    const response = await api(homeFeaturePath(room, `temporary-location/saved-entry?name=${encodeURIComponent(name)}`), { timeoutMs: 10000 });
    if (!current() || draft !== temporaryLocationDraft) return;
    if (draft.version !== draftVersion || temporaryLocationDraftSignature(draft) !== signature) {
      homeElement("temporary-location-saved-status").textContent = "入力が変わったため、保存地点を下書きへ反映しませんでした。";
      return;
    }
    resetTemporaryLocationDraft();
    setTemporaryLocationDraftForm(response, { saved: true });
    homeElement("temporary-location-edit-details").open = true;
    homeElement("temporary-location-saved-status").textContent = `「${name}」を未適用の下書きへ呼び出しました。`;
    homeElement("temporary-location-generation-status").textContent = response.image_unavailable
      ? "保存写真を読み込めませんでした。写真を添付し直すか、写真を外してから保存・適用してください。"
      : "呼び出した内容は未適用です。確認してから保存または適用してください。";
    renderTemporaryLocationDraftStatus();
  } finally {
    draft.busy = false;
    if (current() && draft === temporaryLocationDraft) {
      temporaryLocationDraft.busy = false;
      renderTemporaryLocationPhoto();
    }
  }
}));

homeElement("temporary-location-generate").addEventListener("click", () => homeFeatureAction("temporary-location-generate", "temporary-location-generation-status", async (room, current) => {
  const draft = temporaryLocationDraft, base = state.apiBase;
  readTemporaryLocationDraft();
  draft.busy = true;
  renderTemporaryLocationPhoto();
  try {
    await uploadTemporaryLocationPhoto(draft, room, base, current);
    if (!current() || draft !== temporaryLocationDraft) return;
    const imagePath = temporaryLocationImagePath(draft);
    if (!imagePath) throw new Error("写真を選んでからAI生成を実行してください。");
    const signature = temporaryLocationDraftSignature(draft);
    let request = draft.requests.generate;
    if (!request || request.signature !== signature) {
      request = {
        signature,
        payload: {
          image_path: imagePath,
          user_hint: draft.aiHint,
          client_request_id: `lite-location-generate-${createMessageId()}`,
        },
      };
      draft.requests.generate = request;
    }
    const response = await api(homeFeaturePath(room, "temporary-location/generate"), {
      method: "POST", body: JSON.stringify(request.payload), timeoutMs: 120000,
    });
    if (!current() || draft !== temporaryLocationDraft) return;
    if (response.status === "in_progress") {
      homeElement("temporary-location-generation-status").textContent = "本体で生成中です。「結果を確認」で同じ要求の結果を取得できます。";
      return;
    }
    if (response.status === "failed") {
      homeElement("temporary-location-generation-status").textContent = response.error || "AI生成に失敗しました。「結果を確認」で同じ要求を再確認できます。";
      return;
    }
    if (!["succeeded", "replayed"].includes(response.status)) throw new Error("AI生成結果を確認できませんでした。");
    readTemporaryLocationDraft();
    if (temporaryLocationDraftSignature(draft) !== request.signature) {
      draft.requests.generate = null;
      homeElement("temporary-location-generation-status").textContent = "入力が変わったため、古い生成結果は下書きへ反映しませんでした。";
      return;
    }
    draft.sceneryText = String(response.scenery_text || "").trim();
    draft.sourceDescription = String(response.source_description || "").trim();
    if (response.image_path) {
      draft.imagePath = response.image_path;
      draft.imageDirty = true;
      draft.clearImage = false;
      draft.imagePresent = true;
      draft.imageUnavailable = false;
      draft.imageSnapshotCaptured = true;
    }
    draft.requests.generate = null;
    draft.dirty = true;
    homeElement("temporary-location-text").value = draft.sceneryText;
    homeElement("temporary-location-source-description").value = draft.sourceDescription;
    homeElement("temporary-location-generation-status").textContent = "AI生成結果を未適用の下書きへ反映しました。内容を確認して保存または適用してください。";
    renderTemporaryLocationDraftStatus();
  } catch (error) {
    if (current() && draft === temporaryLocationDraft) {
      homeElement("temporary-location-generation-status").textContent = `結果を確認できませんでした: ${error.message}。「結果を確認」で同じ要求を確認できます。`;
    }
  } finally {
    draft.busy = false;
    if (current() && draft === temporaryLocationDraft) renderTemporaryLocationPhoto();
  }
}));

homeElement("temporary-location-save").addEventListener("click", () => homeFeatureAction("temporary-location-save", "temporary-location-status", async (room, current) => {
  const draft = temporaryLocationDraft, base = state.apiBase;
  readTemporaryLocationDraft();
  if (!draft.locationName) throw new Error("保存する場所名を入力してください。");
  ensureTemporaryLocationPhotoAvailable(draft);
  draft.busy = true;
  renderTemporaryLocationPhoto();
  try {
    await uploadTemporaryLocationPhoto(draft, room, base, current);
    if (!current() || draft !== temporaryLocationDraft) return;
    const payload = {
      name: draft.locationName,
      scenery_text: draft.sceneryText,
      source_description: draft.sourceDescription,
      overwrite: false,
    };
    addTemporaryLocationImagePayload(payload, draft, { preserveSnapshot: true });
    let response;
    try {
      response = await api(homeFeaturePath(room, "temporary-location/saved"), { method: "POST", body: JSON.stringify(payload) });
    } catch (error) {
      if (error.status !== 409 || error.code !== "saved_location_conflict" || !current()) throw error;
      if (!window.confirm(`「${draft.locationName}」はすでに保存されています。内容を上書きしますか？`)) {
        homeElement("temporary-location-status").textContent = "同名の保存地点は上書きせず、保存を中止しました。";
        return;
      }
      payload.overwrite = true;
      response = await api(homeFeaturePath(room, "temporary-location/saved"), { method: "POST", body: JSON.stringify(payload) });
    }
    if (!current() || draft !== temporaryLocationDraft) return;
    const detail = response || {};
    const location = {
      name: detail.location_name || draft.locationName,
      has_image: Boolean(detail.has_image),
    };
    state.temporaryLocationSaved = [
      ...state.temporaryLocationSaved.filter((item) => item.name !== location.name), location,
    ].sort((a, b) => a.name.localeCompare(b.name, "ja"));
    renderTemporaryLocationSavedList();
    homeElement("temporary-location-saved-select").value = location.name;
    draft.savedName = location.name;
    draft.dirty = true;
    homeElement("temporary-location-status").textContent = `「${location.name}」を保存しました。現在地は変わっていません。保存内容は未適用の下書きです。`;
    renderTemporaryLocationPhoto();
  } finally {
    draft.busy = false;
    if (current() && draft === temporaryLocationDraft) renderTemporaryLocationPhoto();
  }
}));

for (const [id, active] of [["temporary-location-apply", true], ["temporary-location-disable", false]]) {
  homeElement(id).addEventListener("click", () => homeFeatureAction(id, "temporary-location-status", async (room, current) => {
    const draft = temporaryLocationDraft, base = state.apiBase;
    readTemporaryLocationDraft();
    if (active && !draft.sceneryText) throw new Error("情景を入力してください。");
    if (active) ensureTemporaryLocationPhotoAvailable(draft);
    state.temporaryLocationVersion += 1;
    draft.busy = true;
    renderTemporaryLocationPhoto();
    try {
      const payload = { active, scenery_text: active ? draft.sceneryText : "" };
      if (active) {
        // 入力欄を空にした意図も保持できるよう、確認済みの値を常に送る。
        payload.location_name = draft.locationName;
        payload.source_description = draft.sourceDescription;
        await uploadTemporaryLocationPhoto(draft, room, base, current);
        addTemporaryLocationImagePayload(payload, draft);
      }
      if (!current() || draft !== temporaryLocationDraft) return;
      const response = await api(homeFeaturePath(room, "temporary-location"), { method: "PUT", body: JSON.stringify(payload) });
      if (current()) {
        state.temporaryLocationVersion += 1;
        state.temporaryLocation = response;
        if (active) resetTemporaryLocationDraft();
        renderTemporaryLocation(response, { updateForm: active });
      }
    } finally {
      draft.busy = false;
      if (current()) { temporaryLocationDraft.busy = false; renderTemporaryLocationPhoto(); }
    }
  }));
}
homeElement("leave-message-save").disabled = true;
homeElement("leave-message-refresh").addEventListener("click", () => homeFeatureAction("leave-message-refresh", "leave-message-status", async (room, current) => {
  const response = await api(homeFeaturePath(room, "user-memo"), { timeoutMs: 10000 });
  if (!current()) return;
  homeElement("leave-message-text").value = response.body || "";
  userMemoHash = response.content_hash;
  homeElement("leave-message-save").disabled = false;
  homeElement("leave-message-status").textContent = "読込済み。次回の自律行動へ渡す内容を編集できます。空で保存するとクリアします。";
}));
homeElement("leave-message-save").addEventListener("click", () => homeFeatureAction("leave-message-save", "leave-message-status", async (room, current) => {
  if (userMemoHash === null) throw new Error("現在の内容を取得してください。");
  const body = homeElement("leave-message-text").value;
  const response = await api(homeFeaturePath(room, "user-memo"), { method: "PUT", body: JSON.stringify({ body, base_hash: userMemoHash }) });
  if (!current()) return;
  userMemoHash = response.content_hash;
  homeElement("leave-message-text").value = response.body || "";
  homeElement("leave-message-status").textContent = "保存しました。次回の自律行動時に渡されます。";
}));
async function stopHomeResponse(room, requestId, current = () => room === state.roomId) {
  const response = await api(homeFeaturePath(room, "chat/stop"), { method: "POST", body: JSON.stringify({ client_message_id: requestId }) });
  if (current()) setSyncStatus(response.accepted ? "停止を受け付けました。進行中の外部要求は完了を待ち、追加の再試行を止めます。" : "進行中の要請が見つかりません。↻で履歴を確認してください。");
  return response;
}
homeElement("stop-response-button").addEventListener("click", () => homeFeatureAction("stop-response-button", "sync-status", async (room, current) => {
  const pending = state.pendingSend;
  if (!pending || pending.roomId !== room) throw new Error("対象の送信を確認できません。");
  await stopHomeResponse(room, pending.id, current);
}));
homeElement("request-control-refresh").addEventListener("click", () => homeFeatureAction("request-control-refresh", "request-control-status", async (room, current) => {
  const response = await api(homeFeaturePath(room, "chat/active"), { timeoutMs: 10000 });
  if (!current()) return;
  const list = homeElement("request-control-list");
  list.replaceChildren();
  for (const request of response.requests || []) {
    const row = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = `${request.request_id.slice(0, 8)}… / ${request.stop_requested ? "停止受付済み" : "進行中"} `;
    const button = document.createElement("button");
    button.className = "secondary-button";
    button.textContent = "この要請を停止";
    button.disabled = request.stop_requested;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await stopHomeResponse(room, request.request_id, current); }
      catch (error) { if (current()) { homeElement("request-control-status").textContent = error.message; button.disabled = false; } }
    });
    row.append(label, button);
    list.appendChild(row);
  }
  homeElement("request-control-status").textContent = response.requests?.length ? `${response.requests.length}件の要請` : "進行中の要請はありません。";
}));
homeElement("note-latest-button").addEventListener("click", async () => {
  if (!state.connected || !state.roomId || state.mode !== "home" || state.noteEditing) return;
  const room = state.roomId;
  const type = els.noteTypeSelect.value;
  const loadId = ++state.noteLoadId;
  const button = homeElement("note-latest-button");
  const current = () => loadId === state.noteLoadId && room === state.roomId && type === els.noteTypeSelect.value;
  button.disabled = true;
  els.noteViewer.textContent = "最新の一件を取得中...";
  try {
    const response = await api(homeFeaturePath(room, `notes/${encodeURIComponent(type)}?latest=true`), { timeoutMs: 10000 });
    if (current()) { renderNote(response); els.noteMeta.textContent = `${response.title} / 最新の一件`; }
  } catch (error) {
    if (current()) els.noteViewer.textContent = `取得失敗: ${error.message}`;
  } finally { if (current()) button.disabled = false; }
});
els.roomSelect.addEventListener("change", resetHomeFeatures);

els.refreshButton.addEventListener("click", () => handleRefresh().catch((error) => showConnectionError(error)));
els.loadMoreButton.addEventListener("click", async () => {
  state.historyLimit = Math.min(50, state.historyLimit + 20);
  els.loadMoreButton.disabled = true;
  try {
    await loadHistory();
    setSyncStatus(`直近${state.historyLimit}件を表示しています。`);
  } finally {
    els.loadMoreButton.disabled = state.historyLimit >= 50;
  }
});
els.newMessageButton.addEventListener("click", scrollMessagesToBottom);
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
  if (state.mode === "home" && (state.sending || state.confirmingSend)) {
    els.roomSelect.value = state.roomId;
    return;
  }
  if (state.mode === "travel") {
    if (state.sending || state.travelRouteChanging) {
      els.roomSelect.value = state.travelPersonaId;
      return;
    }
    state.travelPersonaId = els.roomSelect.value;
    await refreshTravelPersonaView().catch((error) => {
      setSyncStatus(`ペルソナのAI設定・利用額を更新できません: ${error.message}`, "warn");
    });
    return;
  }
  saveComposerDraft();
  state.roomId = els.roomSelect.value;
  localStorage.setItem("nexusLite.roomId", state.roomId);
  state.managementLoaded = false;
  state.historyLimit = 12;
  restoreComposerDraft();
  await loadPrimaryRoomData();
  refreshLetterbox();
  refreshConnectionExtras().catch((error) => setSyncStatus(`補助情報の取得に失敗しました: ${error.message}`, "warn"));
});
els.travelProfileSelect.addEventListener("change", () => {
  els.travelRouteStatus.textContent = "選択はまだ適用されていません。";
  loadTravelModels(els.travelProfileSelect.value).catch(() => {
    els.travelModelStatus.textContent =
      "モデル一覧を取得できません。現在使用中のAIは変更していません。";
  });
});
els.travelModelSelect.addEventListener("change", () => {
  els.travelRouteStatus.textContent = "選択はまだ適用されていません。";
  setTravelRouteControls();
});
els.travelModelRefreshButton.addEventListener("click", () => {
  loadTravelModels(els.travelProfileSelect.value, true).catch(() => {
    els.travelModelStatus.textContent =
      "モデル一覧を更新できません。現在使用中のAIは変更していません。";
  });
});
els.travelBudgetOpen.addEventListener("click", openTravelBudgetEditor);
els.travelBudgetRefresh.addEventListener("click", openTravelBudgetEditor);
els.travelBudgetScope.addEventListener("change", renderTravelBudgetEditor);
els.travelBudgetSave.addEventListener("click", saveTravelBudgetChange);
els.travelBudgetClose.addEventListener("click", () => { els.travelBudgetDialog.close(); state.travelBudgetContext = null; });
els.travelBudgetDialog.addEventListener("close", () => { state.travelBudgetContext = null; });
els.travelBudgetDialog.addEventListener("cancel", (event) => { if (state.travelBudgetChanging) event.preventDefault(); });
els.travelRouteApplyButton.addEventListener("click", () => {
  applyTravelRouteChange().catch(() => {
    els.travelRouteStatus.textContent =
      "AIの変更を確認できませんでした。現在のAIを維持します。";
    state.travelRouteChanging = false;
    setTravelRouteControls();
  });
});
els.chatForm.addEventListener("submit", sendMessage);
els.voiceButton.addEventListener("click", toggleVoiceRecording);
els.managementDetails.addEventListener("toggle", () => refreshManagement().catch((error) => {
  setManagementStatus("失敗", "error");
  setSyncStatus(`管理情報の取得に失敗しました: ${error.message}`, "warn");
}));
els.managementDetails.addEventListener("toggle", () => refreshLetterbox());
els.statusDetails.addEventListener("toggle", () => {
  if (els.statusDetails.open) refreshStatus().catch(() => {});
});
els.statusRefreshButton.addEventListener("click", () => refreshStatus().catch(() => {}));
els.letterboxRefreshButton.addEventListener("click", () => refreshLetterbox());
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
els.noteHeadingSelect.addEventListener("change", loadNoteSection);
els.noteEditButton.addEventListener("click", startNoteEdit);
els.noteSaveButton.addEventListener("click", saveNoteEdit);
els.noteCancelButton.addEventListener("click", cancelNoteEdit);
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
function resizeComposer() {
  const minimum = Number.parseFloat(getComputedStyle(els.messageInput).minHeight) || 0;
  els.messageInput.style.height = "auto";
  els.messageInput.style.height = `${Math.min(240, Math.max(minimum, els.messageInput.scrollHeight))}px`;
}

els.messageInput.addEventListener("input", () => {
  resizeComposer();
  if (state.mode === "travel") travelAdapter.saveDraft(els.messageInput.value);
  else saveComposerDraft();
});
els.itemButton.addEventListener("click", () => openItemDialog());
els.closeItemDialog.addEventListener("click", () => els.itemDialog.close());
els.itemTargetSelect.addEventListener("change", loadItems);
els.itemSelect.addEventListener("change", renderItemPreview);
els.itemActionSelect.addEventListener("change", () => {
  els.itemFurnitureField.hidden = els.itemActionSelect.value !== "place";
  renderItemActionHelp();
});
els.itemRefreshButton.addEventListener("click", loadItems);
els.itemExecuteButton.addEventListener("click", executeItemAction);
els.itemDialog.addEventListener("click", (event) => {
  if (event.target === els.itemDialog) {
    els.itemDialog.close();
  }
});
for (const button of document.querySelectorAll("[data-nav-page]")) {
  button.addEventListener("click", () => setActivePage(button.dataset.navPage));
}
els.imageInput.addEventListener("change", () => {
  if (state.mode === "travel" || state.sending) return;
  state.selectedChatFiles.push(...Array.from(els.imageInput.files || []));
  els.imageInput.value = "";
  renderComposerAttachments();
});
els.redactionShortcutButton.addEventListener("click", openRedactionSettings);
els.closeRedactionDialog.addEventListener("click", () => els.redactionDialog.close());
els.redactionDialog.addEventListener("click", (event) => {
  if (event.target === els.redactionDialog) els.redactionDialog.close();
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
  updateRedactionShortcut();
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
  if (!document.hidden) {
    refreshTravelPendingOnResume();
    if (state.mode === "travel") {
      if (state.connected && state.roomId) syncNow().catch((error) => setSyncStatus(error.message, "warn"));
    } else {
      retryHomeConnection({ automatic: true, refresh: true }).catch(() => {});
    }
    checkLiteBuildManifest().catch(() => {});
  }
});
window.addEventListener("focus", () => {
  if (!document.hidden && state.mode === "home") {
    retryHomeConnection({ automatic: true, refresh: true }).catch(() => {});
  }
});
window.addEventListener("pageshow", () => {
  checkLiteBuildManifest().catch(() => {});
  if (!document.hidden && state.mode === "home") {
    retryHomeConnection({ automatic: true, refresh: true }).catch(() => {});
  }
});
window.addEventListener("online", () => {
  retryHomeConnection({ automatic: true, refresh: true }).catch(() => {});
});
window.setInterval(() => {
  if (!state.connected) retryHomeConnection({ automatic: true }).catch(() => {});
}, STATUS_REFRESH_INTERVAL_MS);
window.setInterval(() => {
  if (!document.hidden && state.connected && state.roomId && state.mode === "home" && !state.sending && !state.syncing
    && !state.homeConnectPromise && !state.homeReconnectPromise) {
    refreshStatus().catch((error) => showConnectionError(error));
  }
}, STATUS_REFRESH_INTERVAL_MS);
window.setInterval(() => {
  renderSnapshotFreshness();
  maybeAutoRefreshStandby().catch((error) => {
    console.warn("Lite standby auto refresh failed:", error);
  });
}, STANDBY_AUTO_CHECK_INTERVAL_MS);
window.setInterval(renderSnapshotFreshness, 60_000);

els.homeModeButton.addEventListener("click", () => handleHomeModeAction().catch((error) => {
  setTravelReturnStatus(error.message, "warn");
  setSyncStatus(error.message, "warn");
}));
els.travelModeButton.addEventListener("click", () => enterTravelMode().catch((error) => {
  els.travelReadiness.textContent = error.message;
  openConnectionSettings(els.travelWorkerUrl);
}));
els.returnHomeButton.addEventListener("click", () => returnTravelToHome().catch((error) => {
  setTravelReturnStatus(error.message, "warn");
  setSyncStatus(error.message, "warn");
}));
els.standbyShortcutButton.addEventListener("click", openStandbySettings);
els.travelPairButton.addEventListener("click", () => pairTravelDevice().catch((error) => setSyncStatus(error.message, "warn")));
window.addEventListener("storage", (event) => {
  if (event.key === null || [travelAdapter.keys.pending, travelAdapter.keys.pendingArchive, travelAdapter.keys.base].includes(event.key)) {
    els.travelPendingUnknownConfirm.checked = false;
    els.travelPendingHomeConfirm.checked = false;
    if (event.key === travelAdapter.keys.base || event.key === null) {
      resetTravelContextAfterWorkerChange(event.oldValue);
    } else {
      invalidateTravelPendingView();
    }
    refreshTravelPendingRecovery().catch(() => {});
  }
});
els.travelPendingCheck.addEventListener("click", () => refreshTravelPendingRecovery().catch((error) => setSyncStatus(error.message, "warn")));
els.travelPendingUnknownConfirm.addEventListener("change", renderTravelPendingRecovery);
els.travelPendingHomeConfirm.addEventListener("change", renderTravelPendingRecovery);
els.travelPendingAcknowledge.addEventListener("click", () => archiveTravelPending("unknown").catch((error) => setSyncStatus(error.message, "warn")));
els.travelPendingHomeRecovered.addEventListener("click", () => archiveTravelPending("home_recovered").catch((error) => setSyncStatus(error.message, "warn")));
els.travelPendingRestore.addEventListener("click", () => restoreTravelPendingOrigin().catch((error) => setSyncStatus(error.message, "warn")));
els.travelPendingSettings.addEventListener("click", () => {
  openConnectionSettings(els.travelWorkerUrl);
});
els.travelPendingExport.addEventListener("click", () => downloadTravelPendingDiagnostic().catch(() => setSyncStatus("確認用の記録を保存できませんでした。", "warn")));

els.installAppButton.addEventListener("click", () => installLiteApp().catch((error) => {
  els.installStatus.textContent = `インストール案内を開けませんでした: ${error.message}`;
}));
els.standbyRefreshButton.addEventListener("click", () => prepareStandbyFromLite().catch((error) => {
  els.standbyStatus.textContent = `お出かけ前のデータ: ${error.message}`;
  setConnectivityStep("standby", "prepare_failed", "error", "準備に失敗", error.message);
}));
els.externalAiExportDetails.addEventListener("toggle", () => {
  if (!els.externalAiExportDetails.open) clearExternalAiExport();
});
els.externalAiShowButton.addEventListener("click", () => showExternalAiExport().catch((error) => {
  els.externalAiExportStatus.textContent = error.message;
}));
els.externalAiCopyButton.addEventListener("click", () => copyExternalAiExport().catch((error) => {
  els.externalAiExportStatus.textContent = error.message;
}));
els.externalAiClearButton.addEventListener("click", clearExternalAiExport);
els.connectionNextButton.addEventListener("click", runConnectivityNextAction);
els.liteUpdateButton.addEventListener("click", () => updatePwaShell().catch((error) => {
  setSyncStatus(`画面の更新を保留しました: ${error.message}`, "warn");
}));
els.connectionCompactButton.addEventListener("click", () => openConnectionSettings());
els.chatConnectionButton.addEventListener("click", () => {
  els.chatConnectionPanel.hidden = !els.chatConnectionPanel.hidden;
  renderChatConnectivity();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !els.chatConnectionPanel.hidden) {
    els.chatConnectionPanel.hidden = true;
    renderChatConnectivity();
    els.chatConnectionButton.focus();
  }
});
els.chatRetryButton.addEventListener("click", () => retryHomeConnection());
els.chatTravelButton.addEventListener("click", () => enterTravelMode().catch((error) => setSyncStatus(error.message, "warn")));
els.chatSettingsButton.addEventListener("click", () => openConnectionSettings(
  els.chatSettingsButton.dataset.action === "home" ? els.apiBaseInput : null,
));
try {
  els.chatContextDetails.open = localStorage.getItem("nexusLite.chatContextOpen") === "true";
} catch {
  els.chatContextDetails.open = false;
}
els.chatContextDetails.addEventListener("toggle", () => {
  try {
    localStorage.setItem("nexusLite.chatContextOpen", String(els.chatContextDetails.open));
  } catch {
    // 表示の好みを保存できなくても、開閉と会話を継続する。
  }
});
els.connectionCheckButton.addEventListener("click", async () => {
  els.connectionCheckButton.disabled = true;
  if (state.connectivity.home.code !== "auth_error") {
    setConnectivityStep("home", "checking", "checking", "確認中", "本体への到達性を確認しています。");
  }
  setConnectivityStep("worker", "checking", "checking", "確認中", "Lite用クラウドへの接続を確認しています。");
  setConnectivityStep("device", "checking", "checking", "確認中", "端末認証を確認しています。");
  setConnectivityStep("standby", "checking", "checking", "確認中", "お出かけ前のデータを確認しています。");
  try {
    await Promise.all([probeHome(), refreshTravelReadiness()]);
  } finally {
    els.connectionCheckButton.disabled = false;
  }
  checkLiteBuildManifest({ force: true }).catch(() => {});
});
const storedStandbyAutoRefresh = localStorage.getItem("nexusLite.standbyAutoRefresh");
els.standbyAutoRefreshCheckbox.checked = storedStandbyAutoRefresh === null || storedStandbyAutoRefresh === "true";
if (storedStandbyAutoRefresh === null) {
  localStorage.setItem("nexusLite.standbyAutoRefresh", "true");
}
els.standbyIncludeCoreMemoryCheckbox.checked = localStorage.getItem("nexusLite.standbyIncludeCoreMemory") !== "false";
els.standbyIncludeEpisodicMemoryCheckbox.checked = localStorage.getItem("nexusLite.standbyIncludeEpisodicMemory") !== "false";
els.standbyEpisodicMemoryDays.value = localStorage.getItem("nexusLite.standbyEpisodicMemoryDays") || "2";
els.standbyRecentMessageLimit.value = localStorage.getItem("nexusLite.standbyRecentMessageLimit") || "40";
applyStandbyDataPreset(localStorage.getItem("nexusLite.standbyDataPreset") || "recommended", { persist: false });
els.standbyDataPreset.addEventListener("change", () => {
  applyStandbyDataPreset(els.standbyDataPreset.value);
});
els.standbyIncludeCoreMemoryCheckbox.addEventListener("change", () => {
  localStorage.setItem("nexusLite.standbyIncludeCoreMemory", String(els.standbyIncludeCoreMemoryCheckbox.checked));
});
els.standbyIncludeEpisodicMemoryCheckbox.addEventListener("change", () => {
  localStorage.setItem("nexusLite.standbyIncludeEpisodicMemory", String(els.standbyIncludeEpisodicMemoryCheckbox.checked));
  els.standbyEpisodicMemoryDays.disabled = !els.standbyIncludeEpisodicMemoryCheckbox.checked;
});
els.standbyEpisodicMemoryDays.addEventListener("change", () => {
  const value = Math.max(0, Math.min(30, Number(els.standbyEpisodicMemoryDays.value) || 0));
  els.standbyEpisodicMemoryDays.value = String(value);
  localStorage.setItem("nexusLite.standbyEpisodicMemoryDays", String(value));
});
els.standbyRecentMessageLimit.addEventListener("change", () => {
  const value = Math.max(0, Math.min(40, Number(els.standbyRecentMessageLimit.value) || 0));
  els.standbyRecentMessageLimit.value = String(value);
  localStorage.setItem("nexusLite.standbyRecentMessageLimit", String(value));
});
els.standbyAutoRefreshCheckbox.addEventListener("change", () => {
  localStorage.setItem("nexusLite.standbyAutoRefresh", String(els.standbyAutoRefreshCheckbox.checked));
  if (els.standbyAutoRefreshCheckbox.checked) {
    maybeAutoRefreshStandby({ forceCheck: true }).catch((error) => {
      els.standbyStatus.textContent = `お出かけ前データの自動更新: ${error.message}`;
    });
  }
});

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  state.deferredInstallPrompt = event;
  renderInstallState();
});
window.addEventListener("appinstalled", () => {
  state.deferredInstallPrompt = null;
  renderInstallState();
  renderStorageContext();
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/lite/service-worker.js", { scope: "/lite/" }).catch(() => {});
}

updateNotificationStatus();
applyThemeSettings();
applyRedactionSettings();
renderStorageContext();
try {
  state.latestStandbySnapshot = JSON.parse(localStorage.getItem("nexusLite.latestStandbySnapshot") || "null");
} catch {
  state.latestStandbySnapshot = null;
}
renderSnapshotFreshness();
renderInstallState();
setLiteMode("home");
renderLiteUpdateBanner();
if (isCloudHostedLite() && !travelAdapter.configuredBase()) {
  travelAdapter.configure(window.location.origin);
}
const pairingHandoffConsumed = consumePairingHandoff();
setActivePage(pairingHandoffConsumed ? "settings" : "chat");

async function initializeLiteConnections() {
  // 軽い到達性確認だけを並行実行し、本体の履歴・補助情報は後段へ送る。
  // 本体停止やTailscale障害で、クラウド診断と独立モードの復元を待たせない。
  const homeProbe = probeHome();
  const readiness = await refreshTravelReadiness().catch((error) => {
    els.travelReadiness.textContent = `Lite用クラウドの確認に失敗しました: ${error.message}`;
    return null;
  });
  const currentTravelSession = readiness?.currentSession || state.currentTravelSession;
  if (currentTravelSession || state.mode === "travel") {
    if (state.mode !== "travel") {
      await enterTravelMode().catch((error) => setSyncStatus(error.message, "warn"));
    }
    await reconcileTravelPendingOnStartup();
    return;
  }
  const homeReachable = await homeProbe;
  if (state.mode === "travel" || state.currentTravelSession) return;
  let connected = false;
  if (homeReachable) {
    try {
      await connect({ collapse: true, useInputs: false });
      connected = state.connected;
    } catch (error) {
      connected = false;
      showConnectionError(error);
    }
  }
  if (state.mode === "travel" || state.currentTravelSession) return;
  if (!connected) {
    if (!state.homeConnectionError) showConnectionError(new Error("本体からの応答を確認できませんでした。"));
  } else if (els.standbyAutoRefreshCheckbox.checked) {
    maybeAutoRefreshStandby({ forceCheck: true }).catch((error) => {
      els.standbyStatus.textContent = `お出かけ前データの自動更新: ${error.message}`;
    });
  }
  await reconcileTravelPendingOnStartup();
}

// ネットワーク確認前に、このoriginに保存済みの入力を戻す。
restoreComposerDraft();
initializeLiteConnections().catch((error) => setSyncStatus(error.message, "warn"))
  .finally(() => { state.connectionsInitialized = true; });
checkLiteBuildManifest().catch(() => {});
