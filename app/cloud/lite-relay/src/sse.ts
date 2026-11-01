export interface SseRecord {
  event: string | null;
  data: string;
}

export function parseSseText(input: string): SseRecord[] {
  const normalized = input.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const records: SseRecord[] = [];

  for (const block of normalized.split("\n\n")) {
    let event: string | null = null;
    const dataLines: string[] = [];
    for (const rawLine of block.split("\n")) {
      const line = rawLine.trimEnd();
      if (!line || line.startsWith(":")) {
        continue;
      }
      if (line.startsWith("event:")) {
        event = line.slice(6).trim() || null;
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (dataLines.length > 0) {
      records.push({ event, data: dataLines.join("\n") });
    }
  }
  return records;
}

export function parseJsonRecord(record: SseRecord): unknown | null {
  if (record.data === "[DONE]") {
    return null;
  }
  try {
    return JSON.parse(record.data) as unknown;
  } catch {
    return null;
  }
}
