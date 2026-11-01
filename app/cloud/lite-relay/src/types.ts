export type Provider = "gemini" | "openai" | "anthropic" | "xai" | "openrouter";

export type RequestStatus =
  | "reserved"
  | "provider_started"
  | "completed"
  | "partial"
  | "failed_known"
  | "outcome_unknown";

export interface CanonicalUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_creation_tokens: number | null;
  reasoning_tokens: number | null;
  provider_reported_cost_usd: number | null;
  cache_creation_5m_tokens?: number | null;
  cache_creation_1h_tokens?: number | null;
  cache_ttl_seconds?: number | null;
  cache_status?: "hit" | "miss" | "created" | "unavailable" | "unreported";
  usage_status: "reported" | "missing" | "partial";
}

export type CanonicalStreamEvent =
  | { schema_version: 1; type: "response.started"; model_requested: string; model_resolved: string | null }
  | { schema_version: 1; type: "response.text.delta"; text: string }
  | { schema_version: 1; type: "response.usage"; usage: CanonicalUsage }
  | {
      schema_version: 1;
      type: "response.committed";
      model_resolved: string | null;
      upstream_provider: string | null;
    }
  | { schema_version: 1; type: "response.partial"; reason: "stream_interrupted" | "max_output_tokens" }
  | { schema_version: 1; type: "response.error"; error: CanonicalProviderError };

export type ErrorCategory =
  | "auth"
  | "rate_limit"
  | "invalid_request"
  | "model_unavailable"
  | "provider_error"
  | "cloudflare_limit"
  | "timeout_before_response"
  | "stream_interrupted"
  | "output_limit"
  | "persistence_failed"
  | "unknown";

export interface CanonicalProviderError {
  category: ErrorCategory;
  http_status: number | null;
  provider_code: string | null;
  retryable: boolean;
  retry_after_seconds: number | null;
  request_may_be_billed: boolean;
  safe_message_ja: string;
}

export interface NormalizedStream {
  provider: Provider;
  events: CanonicalStreamEvent[];
  terminal_status: "completed" | "partial" | "failed_known";
  text: string;
  usage: CanonicalUsage;
  model_requested: string;
  model_resolved: string | null;
  upstream_provider: string | null;
  routing_violation: "unexpected_fallback" | null;
  unknown_event_count: number;
}

export interface ProviderModel {
  provider: Provider;
  model_id: string;
  display_name: string;
  text_chat: boolean | null;
}

export interface MessageRequestRow {
  client_message_id: string;
  travel_session_id: string;
  persona_id: string;
  status: RequestStatus;
  provider: Provider;
  credential_profile_id: string | null;
  model_requested: string;
  route_epoch: number;
  event_id: string | null;
  receipt_id: string | null;
  reserved_at: string;
  provider_started_at: string | null;
  finalized_at: string | null;
  budget_reserved_usd?: number | null;
  budget_settled_usd?: number | null;
  budget_state?: "reserved" | "unknown_allowed" | "settled" | "released" | "held" | null;
  budget_settled_at?: string | null;
}
