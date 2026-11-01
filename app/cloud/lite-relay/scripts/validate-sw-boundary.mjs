import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(here, "../../..");
const homeIndex = await readFile(path.join(repositoryRoot, "mobile_app/index.html"), "utf8");
const serviceWorker = await readFile(path.join(repositoryRoot, "mobile_app/service-worker.js"), "utf8");
const app = await readFile(path.join(repositoryRoot, "mobile_app/static/app.js"), "utf8");
const buildContract = await readFile(path.join(here, "../src/lite-build-contract.ts"), "utf8");
const apiCatalog = await readFile(path.join(here, "../src/api-catalog.ts"), "utf8");
const travelServiceWorker = await readFile(path.join(here, "../public/service-worker.js"), "utf8");
const travelApp = await readFile(path.join(here, "../public/static/app.js"), "utf8");
const travelAdapter = await readFile(path.join(repositoryRoot, "mobile_app/static/travel-adapter.js"), "utf8");
const generatedTravelAdapter = await readFile(path.join(here, "../public/static/travel-adapter.js"), "utf8");
const pairingHandoff = await readFile(path.join(repositoryRoot, "mobile_app/static/pairing-handoff.js"), "utf8");
const generatedPairingHandoff = await readFile(path.join(here, "../public/static/pairing-handoff.js"), "utf8");
const continuityState = await readFile(path.join(repositoryRoot, "mobile_app/static/lite-continuity-state.js"), "utf8");
const generatedContinuityState = await readFile(path.join(here, "../public/static/lite-continuity-state.js"), "utf8");
const travelManifest = await readFile(path.join(here, "../public/manifest.webmanifest"), "utf8");
const travelIndex = await readFile(path.join(here, "../public/index.html"), "utf8");

const homeCacheVersion = serviceWorker.match(/nexus-ark-lite-v(\d+)/)?.[1] || "";
const homeIndexVersion = homeIndex.match(/\/lite\/static\/app\.js\?v=(\d+)/)?.[1] || "";
const travelIndexVersion = travelIndex.match(/\/static\/app\.js\?v=(\d+)/)?.[1] || "";
const moduleVersions = [...app.matchAll(/\.\/(?:travel-adapter|pairing-handoff)\.js\?v=(\d+)/g)]
  .map((match) => match[1]);
const precacheVersions = [...serviceWorker.matchAll(/\/lite\/static\/[^"?]+\?v=(\d+)/g)]
  .map((match) => match[1]);

const checks = {
  home_sw_scope_is_lite: app.includes('register("/lite/service-worker.js", { scope: "/lite/" })'),
  api_is_not_cached: serviceWorker.includes('url.pathname.startsWith("/api/")')
    && serviceWorker.includes('url.pathname.startsWith("/v1/")'),
  cache_version_is_explicit: /const CACHE_NAME = "nexus-ark-lite-v\d+";/.test(serviceWorker),
  home_asset_versions_match_cache: Boolean(homeCacheVersion)
    && homeIndexVersion === homeCacheVersion
    && moduleVersions.length === 2
    && moduleVersions.every((version) => version === homeCacheVersion)
    && precacheVersions.length >= 3
    && precacheVersions.every((version) => version === homeCacheVersion),
  travel_cache_is_mode_scoped: buildContract.includes("nexus-ark-lite-${input.mode}-${input.buildId}"),
  travel_storage_is_mode_scoped: buildContract.includes("nexusLite.${input.mode}"),
  schema_mismatch_blocks_send: buildContract.includes("clientVersion === serverVersion"),
  travel_sw_does_not_cache_api: travelServiceWorker.includes('url.pathname.startsWith("/api/")')
    && travelServiceWorker.includes('url.pathname.startsWith("/v1/")'),
  travel_sw_uses_distinct_cache: /const CACHE_NAME = "nexus-ark-lite-travel-phase5-v\d+";/.test(travelServiceWorker),
  travel_manifest_is_standalone: JSON.parse(travelManifest).display === "standalone",
  travel_app_uses_common_source: travelIndexVersion === homeCacheVersion
    && travelApp.includes(`import { travelAdapter, withConnectionTimeout, LITE_CONNECTION_TIMEOUT_MS } from "./travel-adapter.js?v=${homeCacheVersion}"`)
    && travelApp.includes(`import { parsePairingHandoff } from "./pairing-handoff.js?v=${homeCacheVersion}"`)
    && travelApp.includes(`import { liteContinuityState, readableApiError, normalizeHomeApiBase } from "./lite-continuity-state.js?v=${homeCacheVersion}"`),
  travel_app_uses_cloud_root: travelApp.includes('function isCloudHostedLite() {\n  return true;\n}')
    && travelApp.includes('register("/service-worker.js", { scope: "/" })')
    && travelApp.includes('data: { url: `${window.location.origin}/` }')
    && travelApp.includes('if (window.location.pathname !== "/")'),
  travel_adapter_is_generated_identically: travelAdapter === generatedTravelAdapter,
  pairing_handoff_is_generated_identically: pairingHandoff === generatedPairingHandoff,
  continuity_state_is_generated_identically: continuityState === generatedContinuityState,
  travel_app_uses_distinct_storage: travelAdapter.includes("nexusLite.travel.device.accessToken") && travelApp.includes("nexusLite.token"),
  travel_app_keeps_owner_secret_out: !travelApp.includes("OWNER_AUTH_TOKEN") && !travelApp.includes("BUNDLE_SIGNING_KEY"),
  travel_app_reconciles_pending_send: travelAdapter.includes("nexusLite.travel.pendingMessage") && travelAdapter.includes("pendingStatus"),
  travel_app_does_not_auto_fallback: travelApp.includes("別モードへ自動再送しません") && travelApp.includes("本体へ自動切替しません"),
  travel_app_has_explicit_activation: travelApp.includes("window.confirm(warning)") && travelAdapter.includes("activation_id"),
  travel_app_keeps_binding_ids_out: !/(GEMINI|OPENAI|ANTHROPIC|XAI|OPENROUTER)_PERSONAL_/.test(travelApp + travelAdapter),
  travel_schema_versions_match: apiCatalog.includes("API_SCHEMA_VERSION = 10")
    && travelAdapter.includes("SUPPORTED_API_SCHEMA_VERSION = 10")
    && travelAdapter.includes("api_schema_version > SUPPORTED_API_SCHEMA_VERSION")
    && travelAdapter.includes("api_schema_version < SUPPORTED_API_SCHEMA_VERSION"),
  standby_body_not_in_browser_storage: !travelAdapter.includes("snapshot_json") && !travelAdapter.includes("ciphertext"),
  external_ai_export_is_explicit_and_transient: travelAdapter.includes("external-ai-export")
    && travelApp.includes("externalAiDisclosureCheckbox.checked")
    && !travelApp.includes("localStorage.setItem(\"nexusLite.externalAi"),
  revoked_device_requires_re_pairing: travelAdapter.includes('"re_pair_required"') && travelApp.includes("再ペアリングが必要"),
  connectivity_wizard_has_four_states: ["home", "worker", "device", "standby"].every((name) => travelApp.includes(`setConnectivityStep("${name}"`)),
  pairing_handoff_uses_fragment_and_manual_confirm: pairingHandoff.includes("nexus-lite-pairing")
    && travelApp.includes("window.history.replaceState")
    && travelApp.includes("自動では実行しません"),
};

const failed = Object.entries(checks).filter(([, ok]) => !ok).map(([name]) => name);
const knownConstraints = {
  immediate_activation: serviceWorker.includes("self.skipWaiting()") && serviceWorker.includes("self.clients.claim()"),
  home_token_in_browser_storage: app.includes("[localStorage, sessionStorage]") && app.includes('readConnectionValue("nexusLite.token")'),
  network_first_assets: serviceWorker.includes('fetch(event.request, { cache: "no-store" })')
    && serviceWorker.includes(".catch(() => caches.match(event.request))"),
};

process.stdout.write(`${JSON.stringify({ checks, known_constraints: knownConstraints }, null, 2)}\n`);
if (failed.length > 0) {
  throw new Error(`Lite Service Worker boundary validation failed: ${failed.join(", ")}`);
}
