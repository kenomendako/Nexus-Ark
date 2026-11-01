const PAIRING_PARAM = "nexus-lite-pairing";
const EXPIRY_PARAM = "expires";
const PAIRING_CODE_PATTERN = /^[A-Za-z0-9_-]{8,200}$/;
const MAX_HANDOFF_LIFETIME_MS = 30 * 60_000;

export function parsePairingHandoff(href, nowMs = Date.now()) {
  let url;
  try {
    url = new URL(href);
  } catch {
    return { present: false };
  }
  const fragment = new URLSearchParams(url.hash.replace(/^#/, ""));
  if (!fragment.has(PAIRING_PARAM)) return { present: false };

  const cleanUrl = `${url.pathname}${url.search}`;
  const code = fragment.get(PAIRING_PARAM) || "";
  const expiresAt = fragment.get(EXPIRY_PARAM) || "";
  const expiryMs = Date.parse(expiresAt);
  const localDevelopment = ["localhost", "127.0.0.1"].includes(url.hostname);
  if (url.protocol !== "https:" && !localDevelopment) {
    return { present: true, valid: false, reason: "insecure_origin", cleanUrl };
  }
  if (!PAIRING_CODE_PATTERN.test(code)) {
    return { present: true, valid: false, reason: "invalid_code", cleanUrl };
  }
  if (!Number.isFinite(expiryMs) || expiryMs <= nowMs || expiryMs - nowMs > MAX_HANDOFF_LIFETIME_MS) {
    return { present: true, valid: false, reason: "expired", cleanUrl };
  }
  return {
    present: true,
    valid: true,
    workerUrl: url.origin,
    code,
    expiresAt,
    cleanUrl,
  };
}
