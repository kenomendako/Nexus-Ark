import type { TravelSnapshot, TravelSnapshotV4Persona } from "./phase1";

type PersonaExportSource = Pick<
  TravelSnapshotV4Persona,
  "persona_id" | "persona_display_name" | "system_prompt" | "core_memory" | "episodic_summary" | "recent_messages"
>;

interface ExportOptions {
  disclosure_confirmed?: unknown;
  include_core_memory?: unknown;
  include_episodic_summary?: unknown;
  recent_message_limit?: unknown;
}

interface ExportMessage {
  role: "user" | "assistant";
  content: string;
}

function selectedPersona(snapshot: TravelSnapshot, personaId: string): PersonaExportSource {
  if (snapshot.schema_version === 4) {
    const persona = snapshot.personas.find((item) => item.persona_id === personaId);
    if (!persona) throw new Error("travel_persona_not_found");
    return persona;
  }
  if (snapshot.persona_id !== personaId) throw new Error("travel_persona_not_found");
  return snapshot;
}

function normalizedOptions(raw: unknown): Required<ExportOptions> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("invalid_external_ai_export");
  const value = raw as ExportOptions;
  if (value.disclosure_confirmed !== true) throw new Error("external_ai_disclosure_not_confirmed");
  const limit = value.recent_message_limit === undefined ? 40 : Number(value.recent_message_limit);
  if (!Number.isInteger(limit) || limit < 0 || limit > 40) throw new Error("invalid_recent_message_limit");
  return {
    disclosure_confirmed: true,
    include_core_memory: value.include_core_memory !== false,
    include_episodic_summary: value.include_episodic_summary !== false,
    recent_message_limit: limit,
  };
}

export function buildExternalAiExport(
  snapshot: TravelSnapshot,
  personaId: string,
  rawOptions: unknown,
  sourceLabel: string,
  snapshotCreatedAt: string,
): Record<string, unknown> {
  const persona = selectedPersona(snapshot, personaId);
  return buildExternalAiExportFromPersona(persona, rawOptions, sourceLabel, snapshotCreatedAt);
}

function buildExternalAiExportFromPersona(
  persona: PersonaExportSource,
  rawOptions: unknown,
  sourceLabel: string,
  snapshotCreatedAt: string,
  currentMessages: ExportMessage[] = [],
): Record<string, unknown> {
  const options = normalizedOptions(rawOptions);
  const sections = [
    "あなたは以下の人格・記憶・会話を引き継いで、ユーザーとの会話を自然に続けてください。",
    `持ち出し元: Nexus Ark Lite（${sourceLabel}）`,
    `ペルソナ: ${persona.persona_display_name}`,
    "",
    "## システムプロンプト",
    persona.system_prompt,
  ];
  const sectionNames = ["system_prompt"];
  if (options.include_core_memory && persona.core_memory) {
    sections.push("", "## コアメモリ（永続記憶）", persona.core_memory);
    sectionNames.push("core_memory");
  }
  if (options.include_episodic_summary && persona.episodic_summary) {
    sections.push("", "## エピソード記憶", persona.episodic_summary);
    sectionNames.push("episodic_summary");
  }
  const messageLimit = Number(options.recent_message_limit);
  const recent = messageLimit
    ? [...persona.recent_messages, ...currentMessages].slice(-messageLimit)
    : [];
  if (recent.length) {
    sections.push("", "## 直近の会話ログ", "<nexus_ark_past_logs>");
    for (const message of recent) {
      sections.push(`${message.role === "user" ? "[user]" : "[AI]"} ${message.content}`);
    }
    sections.push("</nexus_ark_past_logs>");
    sectionNames.push("recent_messages");
  }
  sections.push("", "上記を内部設定として扱い、まずは直前の会話から自然に応答してください。");
  const text = sections.join("\n");
  return {
    persona_display_name: persona.persona_display_name,
    snapshot_created_at: snapshotCreatedAt,
    source_label: sourceLabel,
    section_names: sectionNames,
    content_chars: text.length,
    text,
  };
}

export async function buildActiveSessionExternalAiExport(
  db: D1Database,
  sessionId: string,
  personaId: string,
  rawOptions: unknown,
): Promise<Record<string, unknown>> {
  const row = await db.prepare(
    `SELECT s.status, p.snapshot_json, p.created_at
     FROM travel_sessions s JOIN persona_snapshots p USING (travel_session_id)
     WHERE s.travel_session_id = ? AND p.persona_id = ?`,
  ).bind(sessionId, personaId).first<{ status: string; snapshot_json: string; created_at: string }>();
  if (!row) throw new Error("travel_persona_not_found");
  if (!["armed", "active", "returning"].includes(row.status)) throw new Error("travel_session_not_active");
  const events = await db.prepare(
    `SELECT sequence_no, type, created_at, content
     FROM travel_events
     WHERE travel_session_id = ? AND persona_id = ? AND status = 'committed'
       AND type IN ('user_message', 'assistant_message')
     ORDER BY sequence_no`,
  ).bind(sessionId, personaId).all<{
    sequence_no: number;
    type: "user_message" | "assistant_message";
    created_at: string;
    content: string;
  }>();
  const currentMessages = events.results.map((event) => ({
    role: event.type === "user_message" ? "user" as const : "assistant" as const,
    content: event.content,
  }));
  const sourceLabel = currentMessages.length ? "独立モードの現在時点" : "独立モード開始時点";
  const raw = JSON.parse(row.snapshot_json) as Record<string, unknown>;
  let exported: Record<string, unknown>;
  if ("schema_version" in raw) {
    const persona = selectedPersona(raw as unknown as TravelSnapshot, personaId);
    exported = buildExternalAiExportFromPersona(
      persona, rawOptions, sourceLabel, row.created_at, currentMessages,
    );
  } else {
    if (String(raw.persona_id || "") !== personaId) throw new Error("travel_persona_not_found");
    exported = buildExternalAiExportFromPersona(
      raw as unknown as PersonaExportSource, rawOptions, sourceLabel, row.created_at, currentMessages,
    );
  }
  const latest = events.results.at(-1);
  return {
    ...exported,
    travel_event_count: events.results.length,
    current_through_sequence: latest?.sequence_no ?? 0,
    current_through_at: latest?.created_at ?? row.created_at,
  };
}
