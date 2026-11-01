declare namespace Cloudflare {
  interface Env {
    DB: D1Database;
    MODEL_CATALOG_CACHE?: KVNamespace;
    APP_MODE: string;
    BUILD_ID: string;
    PHASE0_GEMINI_MODEL: string;
    PHASE0_OPENAI_MODEL: string;
    PHASE0_ANTHROPIC_MODEL: string;
    PHASE0_XAI_MODEL: string;
    PHASE0_OPENROUTER_MODEL: string;
    PHASE0_VALIDATION_TOKEN: string;
    OWNER_AUTH_TOKEN?: string;
    OWNER_AUTH_TOKEN_NEXT?: string;
    BUNDLE_SIGNING_KEY?: string;
    STANDBY_ENCRYPTION_KEY?: string;
    STANDBY_ENCRYPTION_KEY_ID?: string;
    LITE_ALLOWED_ORIGIN?: string;
    TRAVEL_GEMINI_MODEL?: string;
    GEMINI_PERSONAL_1?: string;
    GEMINI_PERSONAL_2?: string;
    GEMINI_PERSONAL_3?: string;
    OPENAI_PERSONAL_1?: string;
    OPENAI_PERSONAL_2?: string;
    ANTHROPIC_PERSONAL_1?: string;
    XAI_PERSONAL_1?: string;
    OPENROUTER_PERSONAL_1?: string;
    ASSETS?: Fetcher;
  }
}

interface Env extends Cloudflare.Env {}
