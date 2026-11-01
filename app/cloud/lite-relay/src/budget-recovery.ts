import { getUsageSummary } from "./budget";

interface Limits {
  daily_limit_usd: number | null;
  session_limit_usd: number | null;
}

interface BudgetChange {
  scope: "session" | "persona";
  persona_id?: string;
  expected: Limits;
  limits: Limits;
}

function object(value: unknown, allowed: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_budget_change");
  const result = value as Record<string, unknown>;
  if (Object.keys(result).some((key) => !allowed.includes(key))) throw new Error("invalid_budget_change");
  return result;
}

function limit(value: unknown, nullable: boolean): number | null {
  if (nullable && value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new Error("invalid_budget_change");
  return value;
}

function parseChange(value: unknown): BudgetChange {
  const raw = object(value, ["scope", "persona_id", "expected", "limits", "confirmed"]);
  if (raw.confirmed !== true || (raw.scope !== "session" && raw.scope !== "persona")) {
    throw new Error("invalid_budget_change");
  }
  if (raw.persona_id !== undefined && (typeof raw.persona_id !== "string" || !raw.persona_id.trim())) {
    throw new Error("invalid_budget_change");
  }
  if (raw.scope === "persona" && typeof raw.persona_id !== "string") throw new Error("invalid_budget_change");
  const expectedRaw = object(raw.expected, ["daily_limit_usd", "session_limit_usd"]);
  const limitsRaw = object(raw.limits, ["daily_limit_usd", "session_limit_usd"]);
  const expected = {
    daily_limit_usd: limit(expectedRaw.daily_limit_usd, true),
    session_limit_usd: limit(expectedRaw.session_limit_usd, true),
  };
  const limits = {
    daily_limit_usd: limit(limitsRaw.daily_limit_usd, true),
    session_limit_usd: limit(limitsRaw.session_limit_usd, true),
  };
  // 無制限の項目はそのまま維持できる。有限値の増額だけを明示確認で許可する。
  let increased = false;
  for (const key of ["daily_limit_usd", "session_limit_usd"] as const) {
    const before = expected[key];
    const after = limits[key];
    if (before === null) {
      if (after !== null) throw new Error("invalid_budget_change");
    } else {
      if (after === null || after < before) throw new Error("invalid_budget_change");
      if (after > before) increased = true;
    }
  }
  if (!increased) throw new Error("invalid_budget_change");
  return {
    scope: raw.scope,
    ...(typeof raw.persona_id === "string" ? { persona_id: raw.persona_id } : {}),
    expected,
    limits,
  };
}

/** 認証済み端末が明示確認した、今回のお出かけの上限だけを引き上げる。 */
export async function changeTravelBudget(db: D1Database, sessionId: string, raw: unknown, nowIso: string) {
  const change = parseChange(raw);
  const session = await db.prepare("SELECT status FROM travel_sessions WHERE travel_session_id = ?")
    .bind(sessionId).first<{ status: string }>();
  if (!session) throw new Error("travel_session_not_found");
  if (session.status !== "active") throw new Error("travel_session_not_active");
  if (change.scope === "persona") {
    const persona = await db.prepare(
      "SELECT status FROM travel_personas WHERE travel_session_id = ? AND persona_id = ?",
    ).bind(sessionId, change.persona_id!).first<{ status: string }>();
    if (!persona) throw new Error("travel_persona_not_found");
    if (persona.status !== "active") throw new Error("travel_persona_not_active");
  }
  const activeRequest = await db.prepare(
    "SELECT client_message_id FROM message_requests WHERE travel_session_id = ? AND status IN ('reserved', 'provider_started') LIMIT 1",
  ).bind(sessionId).first();
  if (activeRequest) throw new Error("session_message_in_progress");

  // 存在確認後にも返却や送信が始まり得るため、状態と旧上限を同じUPDATE内で再照合する。
  const statement = change.scope === "session"
    ? db.prepare(
      `UPDATE travel_sessions SET budget_daily_limit_usd = ?, budget_session_limit_usd = ?
       WHERE travel_session_id = ? AND status = 'active'
         AND budget_daily_limit_usd IS ? AND budget_session_limit_usd IS ?
         AND NOT EXISTS (SELECT 1 FROM message_requests WHERE travel_session_id = ?
           AND status IN ('reserved', 'provider_started'))`,
    ).bind(change.limits.daily_limit_usd, change.limits.session_limit_usd, sessionId,
      change.expected.daily_limit_usd, change.expected.session_limit_usd, sessionId)
    : db.prepare(
      `UPDATE travel_personas SET budget_daily_limit_usd = ?, budget_session_limit_usd = ?
       WHERE travel_session_id = ? AND persona_id = ? AND status = 'active'
         AND budget_daily_limit_usd IS ? AND budget_session_limit_usd IS ?
         AND EXISTS (SELECT 1 FROM travel_sessions WHERE travel_session_id = ? AND status = 'active')
         AND NOT EXISTS (SELECT 1 FROM message_requests WHERE travel_session_id = ?
           AND status IN ('reserved', 'provider_started'))`,
    ).bind(change.limits.daily_limit_usd, change.limits.session_limit_usd, sessionId, change.persona_id!,
      change.expected.daily_limit_usd, change.expected.session_limit_usd, sessionId, sessionId);
  const result = await statement.run();
  if (Number(result.meta.changes ?? 0) !== 1) throw new Error("budget_change_conflict");
  return {
    updated: true,
    scope: change.scope,
    usage_summary: await getUsageSummary(db, sessionId, nowIso, change.scope === "persona" ? change.persona_id : undefined),
  };
}
