import { conservativeRequestCost } from "./pricing-catalog";
import type { Provider } from "./types";

export interface BudgetSettings {
  daily_limit_usd: number | null;
  session_limit_usd: number | null;
  warning_ratio: number;
  allow_unknown_price: boolean;
  max_output_tokens: number | null;
  timezone: string;
  cache_policy: "off" | "auto" | "gemini_explicit";
}

export interface UsageSummary {
  known_cost_usd: number;
  daily_known_cost_usd: number;
  pending_reserved_usd: number;
  daily_pending_reserved_usd: number;
  unknown_price_count: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  cache_unreported_count: number;
  budget: BudgetSettings;
  persona_budget?: {
    daily_limit_usd: number | null;
    session_limit_usd: number | null;
    max_output_tokens: number | null;
  };
  warning: boolean;
  stopped: boolean;
  persona_id?: string;
  session_summary?: UsageSummary;
}

interface SessionBudgetRow {
  budget_daily_limit_usd: number | null;
  budget_session_limit_usd: number | null;
  budget_warning_ratio: number;
  budget_allow_unknown_price: number;
  budget_max_output_tokens: number;
  budget_timezone: string;
  cache_policy: string;
}

interface ReceiptSummaryRow {
  travel_session_id: string;
  persona_id: string;
  occurred_at: string;
  known_cost_usd: number | null;
  estimate_status: string | null;
  cache_read_tokens: number | null;
  cache_creation_tokens: number | null;
  cache_status: string | null;
}

function finiteLimit(value: number | null): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function outputTokenLimit(value: number): number | null {
  const result = Number(value);
  if (!Number.isFinite(result) || result <= 0) return null;
  return Math.min(65_536, Math.max(1, Math.trunc(result)));
}

function settings(row: SessionBudgetRow): BudgetSettings {
  return {
    daily_limit_usd: finiteLimit(row.budget_daily_limit_usd),
    session_limit_usd: finiteLimit(row.budget_session_limit_usd),
    warning_ratio: Math.min(1, Math.max(0.01, Number(row.budget_warning_ratio || 0.8))),
    allow_unknown_price: row.budget_allow_unknown_price === 1,
    max_output_tokens: outputTokenLimit(row.budget_max_output_tokens),
    timezone: row.budget_timezone || "UTC",
    cache_policy: ["off", "auto", "gemini_explicit"].includes(row.cache_policy)
      ? row.cache_policy as BudgetSettings["cache_policy"]
      : "auto",
  };
}

function localDateKey(iso: string, timeZone: string): string {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date(iso));
    const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${value.year}-${value.month}-${value.day}`;
  } catch {
    return iso.slice(0, 10);
  }
}

async function sessionBudget(db: D1Database, sessionId: string): Promise<BudgetSettings> {
  const row = await db
    .prepare(
      `SELECT budget_daily_limit_usd, budget_session_limit_usd, budget_warning_ratio,
              budget_allow_unknown_price, budget_max_output_tokens, budget_timezone, cache_policy
       FROM travel_sessions WHERE travel_session_id = ?`,
    )
    .bind(sessionId)
    .first<SessionBudgetRow>();
  if (!row) throw new Error("travel_session_not_found");
  return settings(row);
}

export async function getUsageSummary(
  db: D1Database,
  sessionId: string,
  nowIso = new Date().toISOString(),
  personaId?: string,
): Promise<UsageSummary> {
  const budget = await sessionBudget(db, sessionId);
  const receipts = await db
    .prepare(
      `SELECT travel_session_id, persona_id, occurred_at,
              COALESCE(provider_reported_cost_usd, estimated_cost_usd) AS known_cost_usd,
              estimate_status, cache_read_tokens, cache_creation_tokens, cache_status
       FROM usage_receipts`,
    )
    .all<ReceiptSummaryRow>();
  const today = localDateKey(nowIso, budget.timezone);
  const personaBudget = personaId
    ? await db.prepare(
      `SELECT budget_daily_limit_usd, budget_session_limit_usd, budget_max_output_tokens
       FROM travel_personas WHERE travel_session_id = ? AND persona_id = ?`,
    ).bind(sessionId, personaId).first<{
      budget_daily_limit_usd: number | null;
      budget_session_limit_usd: number | null;
      budget_max_output_tokens: number;
    }>()
    : null;
  if (personaId && !personaBudget) throw new Error("travel_persona_not_found");
  let known = 0;
  let dailyKnown = 0;
  let unknown = 0;
  let cacheRead = 0;
  let cacheCreation = 0;
  let cacheUnreported = 0;
  for (const receipt of receipts.results) {
    const belongsToSession = receipt.travel_session_id === sessionId && (!personaId || receipt.persona_id === personaId);
    if (typeof receipt.known_cost_usd === "number" && Number.isFinite(receipt.known_cost_usd)) {
      if (belongsToSession) known += receipt.known_cost_usd;
      if (
        localDateKey(receipt.occurred_at, budget.timezone) === today &&
        (!personaId || receipt.persona_id === personaId)
      ) dailyKnown += receipt.known_cost_usd;
    } else if (belongsToSession && ["unknown_price", "missing_usage"].includes(receipt.estimate_status || "")) {
      unknown += 1;
    }
    if (belongsToSession) {
      cacheRead += Math.max(0, Number(receipt.cache_read_tokens || 0));
      cacheCreation += Math.max(0, Number(receipt.cache_creation_tokens || 0));
      if (!receipt.cache_status || receipt.cache_status === "unreported") cacheUnreported += 1;
    }
  }
  const pendingRow = await db
    .prepare(
      `SELECT COALESCE(SUM(budget_reserved_usd), 0) AS pending
       FROM message_requests
       WHERE travel_session_id = ? AND (? IS NULL OR persona_id = ?) AND budget_state IN ('reserved', 'held')`,
    )
    .bind(sessionId, personaId ?? null, personaId ?? null)
    .first<{ pending: number }>();
  const pending = Math.max(0, Number(pendingRow?.pending || 0));
  const dailyPendingRows = await db
    .prepare(
      `SELECT reserved_at, budget_reserved_usd
       FROM message_requests
       WHERE (? IS NULL OR persona_id = ?) AND budget_state IN ('reserved', 'held')`,
    )
    .bind(personaId ?? null, personaId ?? null)
    .all<{ reserved_at: string; budget_reserved_usd: number | null }>();
  const dailyPending = dailyPendingRows.results.reduce(
    (total, row) =>
      localDateKey(row.reserved_at, budget.timezone) === today
        ? total + Math.max(0, Number(row.budget_reserved_usd || 0))
        : total,
    0,
  );
  const sessionRatio = budget.session_limit_usd === null ? 0 : (known + pending) / Math.max(budget.session_limit_usd, Number.EPSILON);
  const dailyRatio = budget.daily_limit_usd === null ? 0 : (dailyKnown + dailyPending) / Math.max(budget.daily_limit_usd, Number.EPSILON);
  const result: UsageSummary = {
    known_cost_usd: known,
    daily_known_cost_usd: dailyKnown,
    pending_reserved_usd: pending,
    daily_pending_reserved_usd: dailyPending,
    unknown_price_count: unknown,
    cache_read_tokens: cacheRead,
    cache_creation_tokens: cacheCreation,
    cache_unreported_count: cacheUnreported,
    budget,
    warning: Math.max(sessionRatio, dailyRatio) >= budget.warning_ratio,
    stopped: Math.max(sessionRatio, dailyRatio) >= 1,
  };
  if (personaId && personaBudget) {
    result.persona_id = personaId;
    result.persona_budget = {
      daily_limit_usd: finiteLimit(personaBudget.budget_daily_limit_usd),
      session_limit_usd: finiteLimit(personaBudget.budget_session_limit_usd),
      max_output_tokens: outputTokenLimit(personaBudget.budget_max_output_tokens),
    };
    const personaSessionRatio = result.persona_budget.session_limit_usd === null
      ? 0 : (known + pending) / Math.max(result.persona_budget.session_limit_usd, Number.EPSILON);
    const personaDailyRatio = result.persona_budget.daily_limit_usd === null
      ? 0 : (dailyKnown + dailyPending) / Math.max(result.persona_budget.daily_limit_usd, Number.EPSILON);
    result.warning ||= Math.max(personaSessionRatio, personaDailyRatio) >= budget.warning_ratio;
    result.stopped ||= Math.max(personaSessionRatio, personaDailyRatio) >= 1;
    const whole = await getUsageSummary(db, sessionId, nowIso);
    result.session_summary = whole;
    result.warning ||= whole.warning;
    result.stopped ||= whole.stopped;
  }
  return result;
}

export class BudgetLimitError extends Error {
  constructor(code: string, public readonly nextRequestReservedUsd: number) { super(code); }
}

export async function reserveBudget(
  db: D1Database,
  input: {
    clientMessageId: string;
    travelSessionId: string;
    provider: Provider;
    model: string;
    inputTokenUpperBound: number;
    nowIso: string;
    personaId?: string;
  },
): Promise<number | null> {
  const summary = await getUsageSummary(db, input.travelSessionId, input.nowIso);
  const personaSummary = input.personaId
    ? await getUsageSummary(db, input.travelSessionId, input.nowIso, input.personaId)
    : null;
  const reserved = conservativeRequestCost(
    input.provider,
    input.model,
    Math.max(0, Math.ceil(input.inputTokenUpperBound)),
    summary.budget.max_output_tokens ?? 16_384,
    {
      explicitCacheTtlSeconds:
        input.provider === "gemini" && summary.budget.cache_policy === "gemini_explicit" ? 3600 : null,
    },
  );
  const amount = reserved ?? 0;
  if (
    (summary.budget.session_limit_usd !== null && summary.known_cost_usd + summary.pending_reserved_usd + amount > summary.budget.session_limit_usd) ||
    (summary.budget.daily_limit_usd !== null && summary.daily_known_cost_usd + summary.daily_pending_reserved_usd + amount > summary.budget.daily_limit_usd)
  ) {
    throw new BudgetLimitError("budget_limit_exceeded", amount);
  }
  if (input.personaId) {
    const row = await db.prepare(
      `SELECT budget_daily_limit_usd, budget_session_limit_usd
       FROM travel_personas WHERE travel_session_id = ? AND persona_id = ?`,
    ).bind(input.travelSessionId, input.personaId).first<{
      budget_daily_limit_usd: number | null; budget_session_limit_usd: number | null;
    }>();
    if (!row || !personaSummary) throw new Error("travel_persona_not_found");
    if (
      (row.budget_session_limit_usd !== null && personaSummary.known_cost_usd + personaSummary.pending_reserved_usd + amount > row.budget_session_limit_usd) ||
      (row.budget_daily_limit_usd !== null && personaSummary.daily_known_cost_usd + personaSummary.daily_pending_reserved_usd + amount > row.budget_daily_limit_usd)
    ) throw new BudgetLimitError("persona_budget_limit_exceeded", amount);
  }
  const result = await db
    .prepare(
      `UPDATE message_requests
       SET budget_reserved_usd = ?, budget_state = ?
       WHERE client_message_id = ? AND travel_session_id = ? AND status = 'reserved' AND budget_state IS NULL`,
    )
    .bind(amount, reserved === null ? "unknown_allowed" : "reserved", input.clientMessageId, input.travelSessionId)
    .run();
  if (Number(result.meta.changes ?? 0) !== 1) throw new Error("budget_reservation_conflict");
  return reserved;
}

export async function releaseBudget(db: D1Database, clientMessageId: string, settledAt: string): Promise<void> {
  await db
    .prepare(
      `UPDATE message_requests SET budget_state = 'released', budget_settled_usd = 0, budget_settled_at = ?
       WHERE client_message_id = ? AND status IN ('reserved', 'provider_started')
         AND budget_state IN ('reserved', 'unknown_allowed')`,
    )
    .bind(settledAt, clientMessageId)
    .run();
}

export async function holdBudget(db: D1Database, clientMessageId: string): Promise<void> {
  await db
    .prepare(
      `UPDATE message_requests SET budget_state = 'held'
       WHERE client_message_id = ? AND budget_state = 'reserved'`,
    )
    .bind(clientMessageId)
    .run();
}
