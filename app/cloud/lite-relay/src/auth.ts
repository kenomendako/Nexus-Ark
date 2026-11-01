const encoder = new TextEncoder();

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function equalText(left: string, right: string): boolean {
  const length = Math.max(left.length, right.length);
  let mismatch = left.length ^ right.length;
  for (let index = 0; index < length; index += 1) {
    mismatch |= (left.charCodeAt(index) || 0) ^ (right.charCodeAt(index) || 0);
  }
  return mismatch === 0;
}

export function randomToken(bytes = 32): string {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return base64Url(value);
}

export async function sha256(value: string): Promise<string> {
  return base64Url(new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value))));
}

export async function hmacSha256(value: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return base64Url(new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(value))));
}

function bearer(request: Request): string | null {
  const authorization = request.headers.get("Authorization") ?? "";
  return authorization.startsWith("Bearer ") ? authorization.slice(7).trim() || null : null;
}

export function hasOwnerToken(
  request: Request,
  expected: string | undefined,
  nextExpected?: string,
): boolean {
  const supplied = bearer(request);
  return Boolean(
    supplied && (
      (expected && equalText(supplied, expected))
      || (nextExpected && equalText(supplied, nextExpected))
    )
  );
}

export interface DevicePrincipal {
  device_id: string;
  display_name: string;
}

export async function authenticateDevice(
  db: D1Database,
  request: Request,
  now: string,
): Promise<DevicePrincipal | null> {
  const supplied = bearer(request);
  if (!supplied) {
    return null;
  }
  const tokenHash = await sha256(supplied);
  const principal = await db
    .prepare(
      `SELECT device_id, display_name FROM travel_devices
       WHERE access_token_hash = ? AND access_expires_at > ? AND revoked_at IS NULL`,
    )
    .bind(tokenHash, now)
    .first<DevicePrincipal>();
  if (principal) {
    await db.prepare("UPDATE travel_devices SET last_used_at = ? WHERE device_id = ?")
      .bind(now, principal.device_id).run();
  }
  return principal;
}

export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
    .join(",")}}`;
}
