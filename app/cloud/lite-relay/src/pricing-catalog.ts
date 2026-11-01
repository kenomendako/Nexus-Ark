import type { CanonicalUsage, Provider } from "./types";

export const PRICING_VERSION = "2026-10-19.1";

export type EstimateStatus = "estimated" | "free_key_reference" | "unknown_price" | "missing_usage";
export type CostBasis = "provider_reported" | "catalog" | "none";

export interface PriceRates {
  input: number;
  output: number;
  cacheRead?: number;
  cacheWrite?: number;
  cacheWrite5m?: number;
  cacheWrite1h?: number;
  cacheStoragePerMillionTokenHour?: number;
}

interface PricingPeriod {
  validFrom?: string;
  validUntil?: string;
  rates: PriceRates;
}

interface PriceEntry {
  provider: Provider;
  model: RegExp;
  rates: PriceRates;
  sourceUrl: string;
  longContext?: { minimumInputTokens: number; rates: PriceRates };
  pricingPeriods?: readonly PricingPeriod[];
  explicitCacheSupported?: boolean;
}

// 運用モデルの選択肢ではなく、公式料金を確認済みのモデルIDだけを版付きで照合するカタログ。
const PRICE_CATALOG: PriceEntry[] = [
  {
    provider: "gemini",
    model: /^gemini-3\.8-flash$/,
    rates: { input: 0.75, output: 3.75, cacheRead: 0.075, cacheWrite: 0.75, cacheStoragePerMillionTokenHour: 0.5 },
    pricingPeriods: [
      {
        validFrom: "2026-09-02T00:00:00.000Z",
        validUntil: "2027-01-01T00:00:00.000Z",
        rates: { input: 0.75, output: 3.75, cacheRead: 0.075, cacheWrite: 0.75, cacheStoragePerMillionTokenHour: 0.5 },
      },
      {
        validFrom: "2027-01-01T00:00:00.000Z",
        rates: { input: 1.5, output: 7.5, cacheRead: 0.15, cacheWrite: 1.5, cacheStoragePerMillionTokenHour: 1 },
      },
    ],
    explicitCacheSupported: true,
    sourceUrl: "https://ai.google.dev/gemini-api/docs/pricing",
  },
  {
    provider: "gemini",
    model: /^gemini-4-argon$/,
    rates: { input: 2, output: 10, cacheRead: 0.1 },
    pricingPeriods: [
      {
        validFrom: "2026-10-13T00:00:00.000Z",
        validUntil: "2026-12-01T00:00:00.000Z",
        rates: { input: 2, output: 10, cacheRead: 0.1 },
      },
      {
        validFrom: "2026-12-01T00:00:00.000Z",
        rates: { input: 4, output: 20, cacheRead: 0.2 },
      },
    ],
    explicitCacheSupported: false,
    sourceUrl: "https://ai.google.dev/gemini-api/docs/pricing",
  },
  {
    provider: "gemini",
    model: /^gemini-3-flash-preview(?:-[0-9-]+)?$/,
    rates: { input: 0.5, output: 3 },
    sourceUrl: "https://ai.google.dev/gemini-api/docs/pricing",
  },
  {
    provider: "gemini",
    model: /^gemini-3\.5-flash-lite(?:-[0-9-]+)?$/,
    rates: { input: 0.3, output: 2.5, cacheRead: 0.03, cacheWrite: 0.3, cacheStoragePerMillionTokenHour: 1 },
    sourceUrl: "https://ai.google.dev/gemini-api/docs/pricing",
  },
  {
    provider: "gemini",
    model: /^gemini-2\.5-flash-lite(?:-[0-9-]+)?$/,
    rates: { input: 0.1, output: 0.4, cacheRead: 0.025, cacheWrite: 0.1, cacheStoragePerMillionTokenHour: 1 },
    sourceUrl: "https://ai.google.dev/gemini-api/docs/pricing",
  },
  {
    provider: "openai",
    model: /^gpt-5\.4-nano(?:-[0-9-]+)?$/,
    rates: { input: 0.2, output: 1.25, cacheRead: 0.02 },
    sourceUrl: "https://developers.openai.com/api/docs/pricing",
  },
  {
    provider: "anthropic",
    model: /^claude-haiku-4-5(?:-[0-9]+)?$/,
    rates: { input: 1, output: 5, cacheRead: 0.1, cacheWrite5m: 1.25, cacheWrite1h: 2 },
    sourceUrl: "https://platform.claude.com/docs/en/about-claude/pricing",
  },
  {
    provider: "xai",
    model: /^grok-4\.3(?:-[0-9-]+)?$/,
    rates: { input: 1.25, output: 2.5, cacheRead: 0.2 },
    longContext: {
      minimumInputTokens: 131_072,
      rates: { input: 2.5, output: 5, cacheRead: 0.4 },
    },
    sourceUrl: "https://docs.x.ai/developers/pricing",
  },
  {
    provider: "openrouter",
    model: /^openai\/gpt-5\.4-nano$/,
    rates: { input: 0.2, output: 1.25, cacheRead: 0.02 },
    sourceUrl: "https://openrouter.ai/docs/guides/overview/models",
  },
  {
    provider: "openrouter",
    model: /^google\/gemini-3\.8-flash$/,
    rates: { input: 0.75, output: 3.75, cacheRead: 0.075 },
    pricingPeriods: [
      {
        validFrom: "2026-09-02T00:00:00.000Z",
        validUntil: "2027-01-01T00:00:00.000Z",
        rates: { input: 0.75, output: 3.75, cacheRead: 0.075 },
      },
    ],
    explicitCacheSupported: false,
    sourceUrl: "https://openrouter.ai/google/gemini-3.8-flash",
  },
  {
    provider: "openrouter",
    model: /^google\/gemini-4-argon$/,
    rates: { input: 2, output: 10, cacheRead: 0.1 },
    pricingPeriods: [
      {
        validFrom: "2026-10-13T00:00:00.000Z",
        validUntil: "2026-12-01T00:00:00.000Z",
        rates: { input: 2, output: 10, cacheRead: 0.1 },
      },
    ],
    explicitCacheSupported: false,
    sourceUrl: "https://openrouter.ai/google/gemini-4-argon",
  },
];

export interface PricingMetadata {
  pricingVersion: string;
  provider: Provider;
  model: string;
  rates: PriceRates;
  sourceUrl: string;
  longContextApplied: boolean;
  explicitCacheSupported: boolean;
}

export interface CostBreakdown {
  pricing_version: string | null;
  cost_basis: CostBasis;
  estimate_status: EstimateStatus;
  input_cost_usd: number | null;
  output_cost_usd: number | null;
  cache_read_cost_usd: number | null;
  cache_creation_cost_usd: number | null;
  cache_storage_cost_usd: number | null;
  estimated_cost_usd: number | null;
  estimated_savings_usd: number | null;
  unknown_reason: "model_price_unavailable" | "usage_missing" | null;
}

function finiteNonNegative(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function hasExplicitCacheRates(rates: PriceRates): boolean {
  return rates.cacheWrite !== undefined || rates.cacheWrite5m !== undefined || rates.cacheWrite1h !== undefined;
}

function explicitCacheUnsupported(provider: Provider, model: string, price: PricingMetadata): boolean {
  // Argonはimplicit cache専用で、Lite側からexplicit cache APIを呼ばない。
  if (provider === "gemini" && model === "gemini-4-argon") return true;
  // OpenRouterの新Gemini価格はread単価だけ確認済みで、write/storage契約は未確認。
  return provider === "openrouter" && price.explicitCacheSupported === false;
}

function ratesFor(entry: PriceEntry, asOf: Date): PriceRates | null {
  if (!entry.pricingPeriods) return entry.rates;
  const timestamp = asOf.getTime();
  if (!Number.isFinite(timestamp)) return null;
  const period = entry.pricingPeriods.find((candidate) => {
    const from = candidate.validFrom === undefined ? Number.NEGATIVE_INFINITY : Date.parse(candidate.validFrom);
    const until = candidate.validUntil === undefined ? Number.POSITIVE_INFINITY : Date.parse(candidate.validUntil);
    return !Number.isNaN(from) && !Number.isNaN(until) && timestamp >= from && timestamp < until;
  });
  return period?.rates ?? null;
}

export function lookupPrice(
  provider: Provider,
  model: string,
  inputTokens = 0,
  asOf: Date = new Date(),
): PricingMetadata | null {
  const entry = PRICE_CATALOG.find((item) => item.provider === provider && item.model.test(model));
  if (!entry) return null;
  const rates = ratesFor(entry, asOf);
  if (!rates) return null;
  const longContextApplied = Boolean(entry.longContext && inputTokens >= entry.longContext.minimumInputTokens);
  return {
    pricingVersion: PRICING_VERSION,
    provider,
    model,
    rates: longContextApplied ? entry.longContext!.rates : rates,
    sourceUrl: entry.sourceUrl,
    longContextApplied,
    explicitCacheSupported: entry.explicitCacheSupported ?? hasExplicitCacheRates(rates),
  };
}

export interface CostEstimateOptions {
  cacheTtlSeconds?: number | null;
  cacheStorageTokenHours?: number | null;
  asOf?: Date;
}

export function estimateUsageCost(
  provider: Provider,
  model: string,
  usage: CanonicalUsage,
  options: CostEstimateOptions = {},
): CostBreakdown {
  const providerCost = finiteNonNegative(usage.provider_reported_cost_usd);
  const input = finiteNonNegative(usage.input_tokens);
  const output = finiteNonNegative(usage.output_tokens);
  if (providerCost !== null) {
    return {
      pricing_version: PRICING_VERSION,
      cost_basis: "provider_reported",
      estimate_status: "estimated",
      input_cost_usd: null,
      output_cost_usd: null,
      cache_read_cost_usd: null,
      cache_creation_cost_usd: null,
      cache_storage_cost_usd: null,
      estimated_cost_usd: providerCost,
      estimated_savings_usd: null,
      unknown_reason: null,
    };
  }
  if (usage.usage_status === "missing" || (input === null && output === null)) {
    return {
      pricing_version: null,
      cost_basis: "none",
      estimate_status: "missing_usage",
      input_cost_usd: null,
      output_cost_usd: null,
      cache_read_cost_usd: null,
      cache_creation_cost_usd: null,
      cache_storage_cost_usd: null,
      estimated_cost_usd: null,
      estimated_savings_usd: null,
      unknown_reason: "usage_missing",
    };
  }

  const cacheRead = finiteNonNegative(usage.cache_read_tokens) ?? 0;
  const cacheCreation = finiteNonNegative(usage.cache_creation_tokens) ?? 0;
  const totalInput = (input ?? 0) + (provider === "anthropic" ? cacheRead + cacheCreation : 0);
  const price = lookupPrice(provider, model, totalInput, options.asOf);
  if (!price) {
    return {
      pricing_version: null,
      cost_basis: "none",
      estimate_status: "unknown_price",
      input_cost_usd: null,
      output_cost_usd: null,
      cache_read_cost_usd: null,
      cache_creation_cost_usd: null,
      cache_storage_cost_usd: null,
      estimated_cost_usd: null,
      estimated_savings_usd: null,
      unknown_reason: "model_price_unavailable",
    };
  }

  // Argonのようにimplicit cacheだけを許すモデルでcreation usageが現れた場合は、
  // 入力単価へフォールバックして課金額を捏造せず、不明として台帳へ渡す。
  if (cacheCreation > 0 && explicitCacheUnsupported(provider, model, price)) {
    return {
      pricing_version: null,
      cost_basis: "none",
      estimate_status: "unknown_price",
      input_cost_usd: null,
      output_cost_usd: null,
      cache_read_cost_usd: null,
      cache_creation_cost_usd: null,
      cache_storage_cost_usd: null,
      estimated_cost_usd: null,
      estimated_savings_usd: null,
      unknown_reason: "model_price_unavailable",
    };
  }

  const rates = price.rates;
  const divisor = 1_000_000;
  // GoogleのpromptTokenCountはcachedContentTokenCountを内包する。明示cacheを
  // 新規作成した同一ターンでも、creation tokenは別の入力課金ではなくstorageへ分離する。
  const nonCachedInput = provider === "anthropic"
    ? input ?? 0
    : provider === "gemini"
      ? Math.max(0, (input ?? 0) - cacheRead)
      : Math.max(0, (input ?? 0) - cacheRead - cacheCreation);
  const inputCost = nonCachedInput * rates.input / divisor;
  const outputCost = (output ?? 0) * rates.output / divisor;
  const cacheReadCost = cacheRead * (rates.cacheRead ?? rates.input) / divisor;
  let cacheWriteRate = rates.cacheWrite ?? rates.input;
  if ((options.cacheTtlSeconds ?? 0) >= 3600 && rates.cacheWrite1h !== undefined) cacheWriteRate = rates.cacheWrite1h;
  else if (rates.cacheWrite5m !== undefined) cacheWriteRate = rates.cacheWrite5m;
  const cacheCreationCost = provider === "gemini" ? 0 : cacheCreation * cacheWriteRate / divisor;
  const storageTokenHours = finiteNonNegative(options.cacheStorageTokenHours) ?? 0;
  const cacheStorageCost = storageTokenHours * (rates.cacheStoragePerMillionTokenHour ?? 0) / divisor;
  const baselineInputTokens = nonCachedInput + cacheRead + (provider === "gemini" ? 0 : cacheCreation);
  const baseline = baselineInputTokens * rates.input / divisor;
  const actualInput = inputCost + cacheReadCost + cacheCreationCost;
  return {
    pricing_version: price.pricingVersion,
    cost_basis: "catalog",
    estimate_status: "estimated",
    input_cost_usd: inputCost,
    output_cost_usd: outputCost,
    cache_read_cost_usd: cacheReadCost,
    cache_creation_cost_usd: cacheCreationCost,
    cache_storage_cost_usd: cacheStorageCost,
    estimated_cost_usd: actualInput + outputCost + cacheStorageCost,
    estimated_savings_usd: usage.cache_read_tokens === null && usage.cache_creation_tokens === null
      ? null
      : baseline - actualInput,
    unknown_reason: null,
  };
}

export function conservativeRequestCost(
  provider: Provider,
  model: string,
  inputTokenUpperBound: number,
  maxOutputTokens: number,
  options: { explicitCacheTtlSeconds?: number | null; asOf?: Date } = {},
): number | null {
  const price = lookupPrice(provider, model, inputTokenUpperBound, options.asOf);
  if (!price) return null;
  const explicitCacheHours = !explicitCacheUnsupported(provider, model, price)
    ? Math.max(0, Number(options.explicitCacheTtlSeconds || 0)) / 3600
    : 0;
  const explicitCacheRate = explicitCacheHours > 0
    ? Math.max(price.rates.input, price.rates.cacheWrite ?? price.rates.input)
      + (price.rates.cacheRead ?? price.rates.input)
      + (price.rates.cacheStoragePerMillionTokenHour ?? 0) * explicitCacheHours
    : price.rates.input;
  return (inputTokenUpperBound * explicitCacheRate + maxOutputTokens * price.rates.output) / 1_000_000;
}
