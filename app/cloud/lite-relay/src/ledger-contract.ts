export interface LedgerContractEntry {
  source: string;
  occurred_at: string;
  provider: string;
  model: string;
  known_cost_usd: number | null;
  unknown_price_count: 0 | 1;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function normalizeLedgerContractEntry(value: unknown): LedgerContractEntry | null {
  const item = record(value);
  const provider = typeof item.provider === "string" ? item.provider : "";
  const model = typeof item.model === "string" ? item.model : typeof item.model_resolved === "string" ? item.model_resolved : "";
  const occurredAt =
    typeof item.occurred_at === "string" ? item.occurred_at : typeof item.ts === "string" ? item.ts : "";
  if (!provider || !model || !occurredAt) {
    return null;
  }

  const source = typeof item.source === "string" ? item.source : item.receipt_id ? "travel" : "chat";
  const estimateStatus = typeof item.estimate_status === "string" ? item.estimate_status : "estimated";
  const legacyCost = typeof item.cost === "number" && Number.isFinite(item.cost) ? item.cost : null;
  const receiptCost =
    typeof item.estimated_cost_usd === "number" && Number.isFinite(item.estimated_cost_usd)
      ? item.estimated_cost_usd
      : null;
  const unknown = estimateStatus === "unknown_price" || estimateStatus === "missing_usage";
  return {
    source,
    occurred_at: occurredAt,
    provider,
    model,
    known_cost_usd: unknown ? null : receiptCost ?? legacyCost,
    unknown_price_count: unknown ? 1 : 0,
  };
}
