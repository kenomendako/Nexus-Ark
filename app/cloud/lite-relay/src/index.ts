import { REQUEST_EXPIRY_CRON } from "./request-deadline";
import { API_SCHEMA_VERSION, RELAY_RELEASE, WORKER_API_CATALOG } from "./api-catalog";
import {
  cleanupExpiredDevices,
  listDevices,
  ownerDiagnostics,
  retentionPreview,
  revokeAllDevices,
  runRetention,
} from "./admin";
import { authenticateDevice, hasOwnerToken } from "./auth";
import { validateLiveProvider } from "./live-validation";
import { listModelsForProfile } from "./model-catalog";
import {
  buildSignedBundle,
  closeSession,
  consumePairingCode,
  createPairingCode,
  deleteExpiredContent,
  getCurrentSession,
  refreshDevice,
  registerSnapshot,
  revokeDevice,
} from "./phase1";
import {
  changePersonaRoute,
  disableProviderProfile,
  listProviderRegistrationOptions,
  listSafeProviderProfiles,
  upsertProviderProfile,
} from "./phase2-routing";
import {
  cleanupRemoteValidation,
  hasValidationToken,
  parseRunId,
  syntheticSseResponse,
  validateRemoteD1,
} from "./remote-validation";
import { deleteSessionContent, getEventsAfterCursor, recoverStaleRequests } from "./storage";
import { messageRequestStatus, streamTravelMessage, type StreamTravelLifecycle } from "./travel-chat";
import { getUsageSummary } from "./budget";
import { changeTravelBudget } from "./budget-recovery";
import { deleteProviderCaches } from "./provider-cache";
import {
  acknowledgeReturn, buildReturnManifest, buildSignedReturnChunk, emergencyReclaimPersona, startReturn,
} from "./phase4-return";
import {
  activateStandbySnapshot,
  buildStandbyExternalAiExport,
  deleteStandbySnapshot,
  listStandbySnapshots,
  registerStandbySnapshot,
} from "./standby";
import { buildActiveSessionExternalAiExport } from "./external-ai-export";

function json(value: unknown, status = 200): Response {
  return Response.json(value, { status, headers: { "Cache-Control": "no-store" } });
}

function errorCode(error: unknown): string {
  return error instanceof Error ? error.message : "request_failed";
}

function pathId(pathname: string, prefix: string, suffix = ""): string | null {
  if (!pathname.startsWith(prefix) || (suffix && !pathname.endsWith(suffix))) {
    return null;
  }
  const end = suffix ? pathname.length - suffix.length : pathname.length;
  const value = pathname.slice(prefix.length, end);
  return value && !value.includes("/") ? decodeURIComponent(value) : null;
}

function personaPath(pathname: string, suffix: "route" | "events" | "external-ai-export"): { sessionId: string; personaId: string } | null {
  const match = pathname.match(new RegExp(`^/v1/travel-sessions/([^/]+)/personas/([^/]+)/${suffix}$`));
  return match ? { sessionId: decodeURIComponent(match[1]!), personaId: decodeURIComponent(match[2]!) } : null;
}

const EXPECTED_D1_SCHEMA_VERSION = 10;

export async function storageCompatibilityState(db: D1Database): Promise<{
  d1_schema_version: number | null;
  storage_schema_ready: boolean;
}> {
  try {
    const row = await db.prepare(
      "SELECT d1_schema_version FROM relay_schema_state WHERE singleton_id = 1",
    ).first<{ d1_schema_version: number }>();
    const version = Number(row?.d1_schema_version);
    return {
      d1_schema_version: Number.isInteger(version) ? version : null,
      storage_schema_ready: Number.isInteger(version) && version === EXPECTED_D1_SCHEMA_VERSION,
    };
  } catch {
    // 初期migration前や旧D1でもhealth自体は壊さず、更新が必要とのみ公開する。
    return { d1_schema_version: null, storage_schema_ready: false };
  }
}

const coreHandler = {
  async fetch(request: Request, env: Env, context?: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/v1/health") {
      const storage = await storageCompatibilityState(env.DB);
      return json({
        ok: true,
        mode: env.APP_MODE,
        build_id: env.BUILD_ID,
        relay_release: RELAY_RELEASE,
        api_schema_version: API_SCHEMA_VERSION,
        snapshot_schema_range: { min: 1, max: 4 },
        bundle_schema_range: { min: 1, max: 4 },
        pwa_schema_version: 10,
        ...storage,
        capabilities: WORKER_API_CATALOG,
      });
    }

    if (url.pathname.startsWith("/v1/phase0/")) {
      if (!(await hasValidationToken(request, env.PHASE0_VALIDATION_TOKEN))) {
        return json({ error: "unauthorized" }, 401);
      }
      if (request.method === "GET" && url.pathname === "/v1/phase0/stream") {
        return syntheticSseResponse();
      }
      if (request.method === "POST" && url.pathname === "/v1/phase0/live/validate") {
        try {
          return json(await validateLiveProvider(await request.json(), env));
        } catch (error) {
          if (error instanceof Error && error.message === "invalid_live_validation_request") {
            return json({ error: error.message }, 400);
          }
          if (error instanceof Error && error.message === "live_validation_already_attempted") {
            return json({ error: error.message }, 409);
          }
          return json({ error: "phase0_live_validation_failed" }, 500);
        }
      }
      if (url.pathname === "/v1/phase0/d1/validate" && ["POST", "DELETE"].includes(request.method)) {
        try {
          const runId = parseRunId(await request.json());
          if (request.method === "POST") {
            const result = await validateRemoteD1(env.DB, runId);
            return json(result, result.ok ? 200 : 500);
          }
          await cleanupRemoteValidation(env.DB, runId);
          return json({ ok: true });
        } catch (error) {
          if (error instanceof Error && error.message === "invalid_run_id") {
            return json({ error: "invalid_run_id" }, 400);
          }
          return json({ error: "phase0_validation_failed" }, 500);
        }
      }
      return json({ error: "not_found" }, 404);
    }

    const now = new Date();
    const nowIso = now.toISOString();
    if (request.method === "POST" && url.pathname === "/v1/devices/pair") {
      try {
        const existingDevice = await authenticateDevice(env.DB, request, nowIso);
        const body = (await request.json()) as Record<string, unknown>;
        return json(await consumePairingCode(
          env.DB,
          String(body.code ?? ""),
          String(body.display_name ?? ""),
          now,
          existingDevice?.device_id,
        ), 201);
      } catch (error) {
        return json({ error: errorCode(error) }, 400);
      }
    }
    if (request.method === "POST" && url.pathname === "/v1/devices/refresh") {
      try {
        const body = (await request.json()) as Record<string, unknown>;
        return json(await refreshDevice(env.DB, String(body.refresh_token ?? ""), now));
      } catch (error) {
        return json({ error: errorCode(error) }, 401);
      }
    }

    const owner = hasOwnerToken(request, env.OWNER_AUTH_TOKEN, env.OWNER_AUTH_TOKEN_NEXT);
    if (request.method === "GET" && url.pathname === "/v1/provider-profile-registration-options") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try {
        return json(await listProviderRegistrationOptions(env.DB));
      } catch {
        return json({ error: "provider_registration_options_unavailable" }, 503);
      }
    }
    if (request.method === "POST" && url.pathname === "/v1/pairing-codes") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      let body: unknown = {};
      const rawBody = await request.text();
      if (rawBody.trim()) {
        try {
          body = JSON.parse(rawBody);
        } catch {
          return json({ error: "invalid_pairing_ttl" }, 400);
        }
      }
      if (!body || typeof body !== "object" || Array.isArray(body)) {
        return json({ error: "invalid_pairing_ttl" }, 400);
      }
      const ttlMinutes = (body as Record<string, unknown>).ttl_minutes;
      try {
        return json(await createPairingCode(env.DB, now, ttlMinutes), 201);
      } catch (error) {
        if (errorCode(error) === "invalid_pairing_ttl") {
          return json({ error: "invalid_pairing_ttl" }, 400);
        }
        throw error;
      }
    }
    if (request.method === "POST" && url.pathname === "/v1/travel-sessions") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try {
        return json(await registerSnapshot(env.DB, await request.json()), 201);
      } catch (error) {
        return json({ error: errorCode(error) }, 400);
      }
    }
    if (request.method === "POST" && url.pathname === "/v1/standby-snapshots") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try {
        return json(await registerStandbySnapshot(
          env.DB,
          await request.json(),
          env.STANDBY_ENCRYPTION_KEY,
          env.STANDBY_ENCRYPTION_KEY_ID || "standby-key-1",
          now,
        ), 201);
      } catch (error) {
        const code = errorCode(error);
        return json({ error: code }, code === "standby_persona_active" ? 409 : 400);
      }
    }
    if (request.method === "POST" && url.pathname === "/v1/maintenance/retention") {
      return owner
        ? json({ deleted_sessions: await deleteExpiredContent(env.DB, nowIso) })
        : json({ error: "unauthorized" }, 401);
    }
    if (request.method === "GET" && url.pathname === "/v1/admin/diagnostics") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      return json(await ownerDiagnostics(env.DB, nowIso, [
        { name: "DB", configured: Boolean(env.DB), required: true },
        { name: "OWNER_AUTH_TOKEN", configured: Boolean(env.OWNER_AUTH_TOKEN), required: true },
        { name: "BUNDLE_SIGNING_KEY", configured: Boolean(env.BUNDLE_SIGNING_KEY), required: true },
        { name: "STANDBY_ENCRYPTION_KEY", configured: Boolean(env.STANDBY_ENCRYPTION_KEY), required: true },
      ]));
    }
    if (request.method === "POST" && url.pathname === "/v1/admin/maintenance/retention/preview") {
      return owner ? json(await retentionPreview(env.DB, nowIso)) : json({ error: "unauthorized" }, 401);
    }
    if (request.method === "POST" && url.pathname === "/v1/admin/maintenance/retention/run") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try { return json(await runRetention(env.DB, nowIso, "manual")); }
      catch { return json({ error: "retention_failed" }, 500); }
    }
    if (request.method === "GET" && url.pathname === "/v1/admin/devices") {
      return owner ? json({ devices: await listDevices(env.DB, nowIso) }) : json({ error: "unauthorized" }, 401);
    }
    if (request.method === "POST" && url.pathname === "/v1/admin/devices/revoke-all") {
      return owner ? json({ revoked_count: await revokeAllDevices(env.DB, nowIso) }) : json({ error: "unauthorized" }, 401);
    }
    if (request.method === "POST" && url.pathname === "/v1/admin/devices/cleanup-expired") {
      return owner ? json({ revoked_count: await cleanupExpiredDevices(env.DB, nowIso) }) : json({ error: "unauthorized" }, 401);
    }

    const profileId = pathId(url.pathname, "/v1/provider-profiles/");
    if (profileId && request.method === "PUT") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try {
        return json(await upsertProviderProfile(env.DB, profileId, await request.json(), nowIso));
      } catch (error) {
        return json({ error: errorCode(error) }, 400);
      }
    }
    if (profileId && request.method === "DELETE") {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try {
        return json({ disabled: await disableProviderProfile(env.DB, profileId) });
      } catch (error) {
        return json({ error: errorCode(error) }, 400);
      }
    }

    const revokeId = pathId(url.pathname, "/v1/devices/", "/revoke");
    if (request.method === "POST" && revokeId && revokeId !== "self") {
      return owner
        ? json({ revoked: await revokeDevice(env.DB, revokeId, nowIso) })
        : json({ error: "unauthorized" }, 401);
    }
    const standbyDeleteId = pathId(url.pathname, "/v1/standby-snapshots/");
    if (request.method === "DELETE" && standbyDeleteId) {
      return owner
        ? json({ deleted: await deleteStandbySnapshot(env.DB, standbyDeleteId, nowIso) })
        : json({ error: "unauthorized" }, 401);
    }
    const exportId = pathId(url.pathname, "/v1/travel-sessions/", "/export");
    if (request.method === "POST" && exportId) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      if (!env.BUNDLE_SIGNING_KEY) return json({ error: "signing_key_unavailable" }, 503);
      try {
        const version = await env.DB.prepare(
          "SELECT snapshot_schema_version, status FROM travel_sessions WHERE travel_session_id = ?",
        ).bind(exportId).first<{ snapshot_schema_version: number; status: string }>();
        if (version?.snapshot_schema_version === 4 && version.status === "active") {
          await startReturn(env.DB, exportId, nowIso);
        }
        return json(await buildSignedBundle(env.DB, exportId, env.BUNDLE_SIGNING_KEY, nowIso));
      } catch (error) {
        return json({ error: errorCode(error) }, 404);
      }
    }
    const closeId = pathId(url.pathname, "/v1/travel-sessions/", "/close");
    if (request.method === "POST" && closeId) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try {
        await deleteProviderCaches(env.DB, env, closeId);
        await closeSession(env.DB, closeId, now);
        if (
          (await env.DB.prepare("SELECT retention_days FROM travel_sessions WHERE travel_session_id = ?").bind(closeId).first<{ retention_days: number }>())
            ?.retention_days === 0
        ) {
          await deleteExpiredContent(env.DB, nowIso);
        }
        return json({ closed: true });
      } catch (error) {
        return json({ error: errorCode(error) }, 404);
      }
    }
    const returnStartId = pathId(url.pathname, "/v1/travel-sessions/", "/return/start");
    if (request.method === "POST" && returnStartId) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try { return json(await startReturn(env.DB, returnStartId, nowIso)); }
      catch (error) { return json({ error: errorCode(error) }, 409); }
    }
    const returnManifestId = pathId(url.pathname, "/v1/travel-sessions/", "/return/manifest");
    if (request.method === "GET" && returnManifestId) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try { return json(await buildReturnManifest(env.DB, returnManifestId, nowIso)); }
      catch (error) { return json({ error: errorCode(error) }, 404); }
    }
    const returnChunksId = pathId(url.pathname, "/v1/travel-sessions/", "/return/chunks");
    if (request.method === "GET" && returnChunksId) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      if (!env.BUNDLE_SIGNING_KEY) return json({ error: "signing_key_unavailable" }, 503);
      try {
        return json(await buildSignedReturnChunk(
          env.DB, returnChunksId, String(url.searchParams.get("persona_id") || ""),
          Number(url.searchParams.get("after_sequence") || 0), env.BUNDLE_SIGNING_KEY, nowIso,
        ));
      } catch (error) { return json({ error: errorCode(error) }, 400); }
    }
    const returnAckId = pathId(url.pathname, "/v1/travel-sessions/", "/return/ack");
    if (request.method === "POST" && returnAckId) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try { return json(await acknowledgeReturn(env.DB, returnAckId, await request.json(), nowIso)); }
      catch (error) { return json({ error: errorCode(error) }, 409); }
    }
    const emergencyPath = url.pathname.match(/^\/v1\/travel-sessions\/([^/]+)\/personas\/([^/]+)\/emergency-reclaim$/);
    if (request.method === "POST" && emergencyPath) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      try {
        const body = (await request.json()) as Record<string, unknown>;
        return json(await emergencyReclaimPersona(
          env.DB, decodeURIComponent(emergencyPath[1]!), decodeURIComponent(emergencyPath[2]!),
          String(body.reason || ""), nowIso,
        ));
      } catch (error) { return json({ error: errorCode(error) }, 409); }
    }
    const contentId = pathId(url.pathname, "/v1/travel-sessions/", "/content");
    if (request.method === "DELETE" && contentId) {
      if (!owner) return json({ error: "unauthorized" }, 401);
      const session = await env.DB.prepare(
        "SELECT snapshot_schema_version, status FROM travel_sessions WHERE travel_session_id = ?",
      ).bind(contentId).first<{ snapshot_schema_version: number; status: string }>();
      if (session?.snapshot_schema_version === 4 && session.status !== "closed") {
        return json({ error: "return_ack_incomplete" }, 409);
      }
      await deleteProviderCaches(env.DB, env, contentId);
      await deleteSessionContent(env.DB, contentId, nowIso);
      await env.DB.prepare("DELETE FROM persona_snapshots WHERE travel_session_id = ?").bind(contentId).run();
      return json({ deleted: true });
    }

    const device = await authenticateDevice(env.DB, request, nowIso);
    if (request.method === "POST" && url.pathname === "/v1/devices/self/revoke") {
      return device
        ? json({ revoked: await revokeDevice(env.DB, device.device_id, nowIso) })
        : json({ error: "unauthorized" }, 401);
    }
    if (request.method === "GET" && url.pathname === "/v1/travel-sessions/current") {
      return device ? json({ session: await getCurrentSession(env.DB) }) : json({ error: "unauthorized" }, 401);
    }
    if (request.method === "GET" && url.pathname === "/v1/standby-snapshots") {
      return device ? json({ snapshots: await listStandbySnapshots(env.DB, nowIso) }) : json({ error: "unauthorized" }, 401);
    }
    const standbyExternalExportId = pathId(url.pathname, "/v1/standby-snapshots/", "/external-ai-export");
    if (request.method === "POST" && standbyExternalExportId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        const body = await request.json() as Record<string, unknown>;
        return json(await buildStandbyExternalAiExport(
          env.DB,
          standbyExternalExportId,
          String(body.persona_id || ""),
          body,
          env.STANDBY_ENCRYPTION_KEY,
          now,
        ));
      } catch (error) {
        const code = errorCode(error);
        return json({ error: code }, code === "standby_not_found" ? 404 : ["standby_not_ready", "standby_expired"].includes(code) ? 409 : 400);
      }
    }
    const standbyActivateId = pathId(url.pathname, "/v1/standby-snapshots/", "/activate");
    if (request.method === "POST" && standbyActivateId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        return json(await activateStandbySnapshot(
          env.DB, standbyActivateId, await request.json(), env.STANDBY_ENCRYPTION_KEY, now,
        ));
      } catch (error) {
        const code = errorCode(error);
        return json({ error: code }, ["standby_not_ready", "standby_activation_conflict"].includes(code) ? 409 : 400);
      }
    }
    if (request.method === "GET" && url.pathname === "/v1/provider-profiles") {
      return device || owner
        ? json({ profiles: await listSafeProviderProfiles(env.DB) })
        : json({ error: "unauthorized" }, 401);
    }
    const modelProfileId = pathId(url.pathname, "/v1/provider-profiles/", "/models");
    if (request.method === "GET" && modelProfileId) {
      if (!device && !owner) return json({ error: "unauthorized" }, 401);
      try {
        return json(
          await listModelsForProfile(
            env.DB,
            env.MODEL_CATALOG_CACHE,
            env,
            modelProfileId,
            url.searchParams.get("refresh") === "1",
          ),
        );
      } catch (error) {
        const code = errorCode(error);
        return json({ error: code }, code === "model_catalog_unavailable" ? 503 : 409);
      }
    }
    const routeId = pathId(url.pathname, "/v1/travel-sessions/", "/route");
    if (request.method === "PUT" && routeId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        return json(await changePersonaRoute(env.DB, routeId, await request.json(), nowIso));
      } catch (error) {
        const code = errorCode(error);
        const status = code === "travel_session_not_found" ? 404 :
          ["travel_session_not_active", "session_message_in_progress", "route_change_id_conflict"].includes(code) ? 409 : 400;
        return json({ error: code }, status);
      }
    }
    const personaRoute = personaPath(url.pathname, "route");
    if (request.method === "PUT" && personaRoute) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        return json(await changePersonaRoute(env.DB, personaRoute.sessionId, await request.json(), nowIso, personaRoute.personaId));
      } catch (error) {
        const code = errorCode(error);
        return json({ error: code }, ["travel_session_not_active", "travel_persona_not_active", "session_message_in_progress"].includes(code) ? 409 : 400);
      }
    }
    const eventsId = pathId(url.pathname, "/v1/travel-sessions/", "/events");
    if (request.method === "GET" && eventsId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      const session = await env.DB
        .prepare("SELECT persona_id FROM travel_sessions WHERE travel_session_id = ?")
        .bind(eventsId)
        .first<{ persona_id: string }>();
      if (!session) return json({ error: "travel_session_not_found" }, 404);
      return json(
        await getEventsAfterCursor(env.DB, eventsId, session.persona_id, Number(url.searchParams.get("after_sequence") ?? 0)),
      );
    }
    const personaEvents = personaPath(url.pathname, "events");
    if (request.method === "GET" && personaEvents) {
      if (!device) return json({ error: "unauthorized" }, 401);
      const persona = await env.DB.prepare(
        "SELECT persona_id FROM travel_personas WHERE travel_session_id = ? AND persona_id = ?",
      ).bind(personaEvents.sessionId, personaEvents.personaId).first();
      if (!persona) return json({ error: "travel_persona_not_found" }, 404);
      return json(await getEventsAfterCursor(
        env.DB, personaEvents.sessionId, personaEvents.personaId, Number(url.searchParams.get("after_sequence") ?? 0),
      ));
    }
    const personaExternalExport = personaPath(url.pathname, "external-ai-export");
    if (request.method === "POST" && personaExternalExport) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        return json(await buildActiveSessionExternalAiExport(
          env.DB, personaExternalExport.sessionId, personaExternalExport.personaId, await request.json(),
        ));
      } catch (error) {
        const code = errorCode(error);
        return json({ error: code }, code === "travel_persona_not_found" ? 404 : code === "travel_session_not_active" ? 409 : 400);
      }
    }
    const budgetId = pathId(url.pathname, "/v1/travel-sessions/", "/budget");
    if (request.method === "PUT" && budgetId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        return json(await changeTravelBudget(env.DB, budgetId, await request.json(), nowIso));
      } catch (error) {
        const code = errorCode(error);
        return json({ error: code }, code.endsWith("_not_found") ? 404 :
          ["travel_session_not_active", "travel_persona_not_active", "session_message_in_progress", "budget_change_conflict"].includes(code) ? 409 : 400);
      }
    }
    const usageSummaryId = pathId(url.pathname, "/v1/travel-sessions/", "/usage-summary");
    if (request.method === "GET" && usageSummaryId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        const personaId = url.searchParams.get("persona_id") || undefined;
        const summary = await getUsageSummary(env.DB, usageSummaryId, nowIso, personaId);
        if (personaId) return json(summary);
        if (!personaId) {
          const personas = await env.DB.prepare(
            "SELECT persona_id FROM travel_personas WHERE travel_session_id = ? ORDER BY persona_id",
          ).bind(usageSummaryId).all<{ persona_id: string }>();
          return json({ ...summary, personas: await Promise.all(
            personas.results.map((row) => getUsageSummary(env.DB, usageSummaryId, nowIso, row.persona_id)),
          ) });
        }
        return json(summary);
      } catch (error) {
        return json({ error: errorCode(error) }, 404);
      }
    }
    const messagesId = pathId(url.pathname, "/v1/travel-sessions/", "/messages");
    if (request.method === "POST" && messagesId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      try {
        const lifecycle: StreamTravelLifecycle = { signal: request.signal };
        if (context) lifecycle.waitUntil = (promise) => context.waitUntil(promise);
        return await streamTravelMessage(
          env.DB,
          env,
          messagesId,
          await request.json(),
          undefined,
          undefined,
          undefined,
          lifecycle,
        );
      } catch (error) {
        return json({ error: errorCode(error) }, 400);
      }
    }
    const requestId = pathId(url.pathname, "/v1/message-requests/");
    if (request.method === "GET" && requestId) {
      if (!device) return json({ error: "unauthorized" }, 401);
      const status = await messageRequestStatus(env.DB, requestId);
      return status ? json(status) : json({ error: "message_request_not_found" }, 404);
    }

    return json({ error: "not_found" }, 404);
  },
  async scheduled(_controller: ScheduledController, env: Env, context: ExecutionContext): Promise<void> {
    const now = new Date().toISOString();
    context.waitUntil((async () => {
      await recoverStaleRequests(env.DB, now);
      if (_controller.cron !== REQUEST_EXPIRY_CRON) await runRetention(env.DB, now, "cron");
    })());
  },
} satisfies ExportedHandler<Env>;

function withCors(response: Response, request: Request, env: Env): Response {
  const origin = request.headers.get("Origin");
  if (!origin || !env.LITE_ALLOWED_ORIGIN || origin !== env.LITE_ALLOWED_ORIGIN) return response;
  const headers = new Headers(response.headers);
  headers.set("Access-Control-Allow-Origin", origin);
  headers.set("Access-Control-Allow-Headers", "Authorization, Content-Type");
  headers.set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS");
  headers.set("Access-Control-Max-Age", "600");
  headers.append("Vary", "Origin");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

export default {
  async fetch(request: Request, env: Env, context?: ExecutionContext): Promise<Response> {
    if (request.method === "OPTIONS") {
      const allowed = request.headers.get("Origin") === env.LITE_ALLOWED_ORIGIN;
      return withCors(new Response(null, { status: allowed ? 204 : 403 }), request, env);
    }
    return withCors(await coreHandler.fetch(request, env, context), request, env);
  },
  scheduled(controller: ScheduledController, env: Env, context: ExecutionContext): Promise<void> {
    return coreHandler.scheduled(controller, env, context);
  },
} satisfies ExportedHandler<Env>;
