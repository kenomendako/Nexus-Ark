import { deleteExpiredContent } from "./phase1";
import { deleteExpiredStandbyContent } from "./standby";

export interface BindingState {
  name: string;
  configured: boolean;
  required: boolean;
}

export async function listDevices(db: D1Database, now: string): Promise<Record<string, unknown>[]> {
  const devices = await db.prepare(
    `SELECT device_id, display_name, access_expires_at, refresh_expires_at, created_at,
            last_refreshed_at, last_used_at, revoked_at
     FROM travel_devices ORDER BY created_at DESC`,
  ).all<Record<string, unknown>>();
  return devices.results.map((device) => ({
    ...device,
    access_expired: String(device.access_expires_at) <= now,
    refresh_expired: String(device.refresh_expires_at) <= now,
  }));
}

export async function revokeAllDevices(db: D1Database, now: string): Promise<number> {
  const result = await db.prepare(
    "UPDATE travel_devices SET revoked_at = ? WHERE revoked_at IS NULL",
  ).bind(now).run();
  return Number(result.meta.changes ?? 0);
}

export async function cleanupExpiredDevices(db: D1Database, now: string): Promise<number> {
  const result = await db.prepare(
    `UPDATE travel_devices SET revoked_at = COALESCE(revoked_at, ?)
     WHERE refresh_expires_at <= ?`,
  ).bind(now, now).run();
  return Number(result.meta.changes ?? 0);
}

export async function retentionPreview(db: D1Database, now: string): Promise<Record<string, unknown>> {
  const sessions = await db.prepare(
    `SELECT travel_session_id AS id, content_delete_after AS expires_at
     FROM travel_sessions
     WHERE status = 'closed' AND content_deleted_at IS NULL AND content_delete_after <= ?
     ORDER BY content_delete_after`,
  ).bind(now).all();
  const standby = await db.prepare(
    `SELECT standby_snapshot_id AS id,
            CASE WHEN status = 'superseded' THEN content_delete_after ELSE expires_at END AS expires_at,
            status
     FROM standby_snapshots
     WHERE content_deleted_at IS NULL AND ciphertext IS NOT NULL
       AND ((status = 'ready' AND expires_at <= ?)
         OR (status = 'expired' AND expires_at <= ?)
         OR (status = 'superseded' AND content_delete_after <= ?))
     ORDER BY expires_at`,
  ).bind(now, now, now).all();
  return {
    generated_at: now,
    sessions: sessions.results,
    standby_snapshots: standby.results,
    deleted_session_count: sessions.results.length,
    deleted_standby_count: standby.results.length,
  };
}

export async function runRetention(
  db: D1Database,
  now: string,
  triggerKind: "cron" | "manual",
): Promise<Record<string, unknown>> {
  const runId = crypto.randomUUID();
  await db.prepare(
    `INSERT INTO maintenance_runs
     (maintenance_run_id, trigger_kind, status, started_at) VALUES (?, ?, 'running', ?)`,
  ).bind(runId, triggerKind, now).run();
  try {
    const deletedSessions = await deleteExpiredContent(db, now);
    const deletedStandby = await deleteExpiredStandbyContent(db, now);
    await db.prepare(
      `UPDATE maintenance_runs SET status = 'completed', completed_at = ?,
       deleted_session_count = ?, deleted_standby_count = ? WHERE maintenance_run_id = ?`,
    ).bind(now, deletedSessions, deletedStandby, runId).run();
    return {
      maintenance_run_id: runId,
      status: "completed",
      deleted_session_count: deletedSessions,
      deleted_standby_count: deletedStandby,
    };
  } catch (error) {
    await db.prepare(
      `UPDATE maintenance_runs SET status = 'failed', completed_at = ?, failure_code = ?
       WHERE maintenance_run_id = ?`,
    ).bind(now, "retention_failed", runId).run();
    throw error;
  }
}

export async function ownerDiagnostics(
  db: D1Database,
  now: string,
  bindings: BindingState[],
): Promise<Record<string, unknown>> {
  const schema = await db.prepare(
    "SELECT d1_schema_version, latest_migration, applied_at FROM relay_schema_state WHERE singleton_id = 1",
  ).first<Record<string, unknown>>();
  const counts = await db.prepare(
    `SELECT
       COALESCE(SUM(CASE WHEN status IN ('armed', 'active') THEN 1 ELSE 0 END), 0) AS active_sessions,
       COALESCE(SUM(CASE WHEN status = 'returning' THEN 1 ELSE 0 END), 0) AS returning_sessions,
       COALESCE(SUM(CASE WHEN status = 'closed' AND content_deleted_at IS NULL AND content_delete_after <= ? THEN 1 ELSE 0 END), 0)
         AS overdue_sessions
     FROM travel_sessions`,
  ).bind(now).first<Record<string, number>>();
  const standby = await db.prepare(
    `SELECT
       COALESCE(SUM(CASE WHEN status = 'ready' AND expires_at > ? THEN 1 ELSE 0 END), 0) AS ready_standby,
       COALESCE(SUM(CASE WHEN content_deleted_at IS NULL AND ciphertext IS NOT NULL
         AND ((status IN ('ready', 'expired') AND expires_at <= ?)
           OR (status = 'superseded' AND content_delete_after <= ?)) THEN 1 ELSE 0 END), 0) AS overdue_standby
     FROM standby_snapshots`,
  ).bind(now, now, now).first<Record<string, number>>();
  const devices = await db.prepare(
    `SELECT
       COALESCE(SUM(CASE WHEN revoked_at IS NULL AND refresh_expires_at > ? THEN 1 ELSE 0 END), 0) AS active_devices,
       COALESCE(SUM(CASE WHEN revoked_at IS NULL AND refresh_expires_at <= ? THEN 1 ELSE 0 END), 0) AS expired_devices
     FROM travel_devices`,
  ).bind(now, now).first<Record<string, number>>();
  const maintenance = await db.prepare(
    `SELECT maintenance_run_id, trigger_kind, status, started_at, completed_at,
            deleted_session_count, deleted_standby_count, failure_code
     FROM maintenance_runs ORDER BY started_at DESC LIMIT 1`,
  ).first<Record<string, unknown>>();
  const bindingMissing = bindings.some((binding) => binding.required && !binding.configured);
  const maintenanceOverdue = Number(counts?.overdue_sessions ?? 0) + Number(standby?.overdue_standby ?? 0) > 0;
  return {
    generated_at: now,
    d1: schema ?? { d1_schema_version: null, latest_migration: null, applied_at: null },
    bindings,
    resources: { ...counts, ...standby, ...devices },
    maintenance: maintenance ?? null,
    state: bindingMissing ? "secret_action_required" : maintenanceOverdue ? "maintenance_overdue" : "ready",
  };
}
