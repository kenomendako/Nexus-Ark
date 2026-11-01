export type LiteMode = "home" | "travel";

export interface LiteBuildDescriptor {
  mode: LiteMode;
  build_id: string;
  api_schema_version: number;
  cache_name: string;
  storage_namespace: string;
  origin: string;
}

export function createLiteBuildDescriptor(input: {
  mode: LiteMode;
  buildId: string;
  apiSchemaVersion: number;
  origin: string;
}): LiteBuildDescriptor {
  const origin = new URL(input.origin).origin;
  if (!input.buildId || !Number.isInteger(input.apiSchemaVersion) || input.apiSchemaVersion < 1) {
    throw new Error("invalid_lite_build_descriptor");
  }
  return {
    mode: input.mode,
    build_id: input.buildId,
    api_schema_version: input.apiSchemaVersion,
    cache_name: `nexus-ark-lite-${input.mode}-${input.buildId}`,
    storage_namespace: `nexusLite.${input.mode}`,
    origin,
  };
}

export function canSendWithSchema(clientVersion: number, serverVersion: number): boolean {
  return Number.isInteger(clientVersion) && clientVersion === serverVersion;
}
