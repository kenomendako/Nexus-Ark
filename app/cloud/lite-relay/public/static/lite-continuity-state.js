export function normalizeHomeApiBase(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";

  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    return raw;
  }
  if ((parsed.protocol !== "http:" && parsed.protocol !== "https:")
    || parsed.username || parsed.password || parsed.search || parsed.hash) {
    return raw;
  }

  const path = parsed.pathname.replace(/\/+$/, "");
  if (!path || path === "/lite") return parsed.origin;
  return `${parsed.origin}${path}`;
}

export function liteContinuityState(status, mode = "home", returningHome = false) {
  const normalized = ["active", "returning"].includes(String(status)) ? String(status) : "";
  const inUse = normalized === "active";
  const returning = normalized === "returning";
  const blocksStandby = inUse || returning;
  return {
    status: normalized,
    blocksStandby,
    homeLabel: returning ? "帰宅を再開" : inUse ? "署名付き帰宅" : "本体",
    homeDisabled: returningHome || (!blocksStandby && mode === "home"),
    travelDisabled: returningHome || returning || mode === "travel",
    freshnessText: returning
      ? "お出かけ前データ 帰宅処理中・完了後に更新"
      : inUse ? "お出かけ前データ 使用中・帰宅後に更新" : "",
    readinessText: returning ? "署名付き帰宅の途中" : inUse ? "独立モード使用中" : "",
    standbyStatusText: returning
      ? "お出かけ前のデータ: 帰宅処理中です。帰宅完了後に更新できます。"
      : inUse ? "お出かけ前のデータ: 独立モードで使用中です。署名付き帰宅後に更新できます。" : "",
    standbyCode: returning ? "returning" : inUse ? "in_use" : "",
    standbyLabel: returning ? "帰宅処理中" : inUse ? "使用中" : "",
    standbyNext: returning
      ? "「帰宅を再開」で処理を完了してください。"
      : inUse ? "署名付き帰宅後に新しいデータを保存できます。" : "",
  };
}

export function readableApiError(status, statusText, contentType, bodyText) {
  const body = String(bodyText || "");
  if (String(contentType || "").toLowerCase().includes("json")) {
    try {
      const detail = JSON.parse(body)?.detail;
      if (typeof detail === "string" && detail.trim()) {
        return detail.trim().replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "").slice(0, 1000);
      }
    } catch {
      // JSONを安全に解釈できない場合は、従来のHTTP表示へ戻す。
    }
  }
  return `${status} ${body || statusText}`.trim();
}
