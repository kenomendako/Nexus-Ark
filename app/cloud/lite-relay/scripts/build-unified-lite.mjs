import { createHash } from "node:crypto";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const relayRoot = path.resolve(here, "..");
const repositoryRoot = path.resolve(relayRoot, "../..");
const sourceRoot = path.join(repositoryRoot, "mobile_app");
const outputOption = process.argv.indexOf("--output-root");
if (outputOption !== -1 && (!process.argv[outputOption + 1] || process.argv[outputOption + 1].startsWith("--"))) {
  throw new Error("--output-rootには出力先が必要です。");
}
const publicRoot = outputOption === -1
  ? path.join(relayRoot, "public")
  : path.resolve(process.argv[outputOption + 1]);
const staticRoot = path.join(publicRoot, "static");
const writeSourceMetadata = process.argv.includes("--write-source-metadata");

const UI_ASSET_NAMES = [
  "index.html",
  "static/app.js",
  "static/styles.css",
  "static/pairing-handoff.js",
  "static/lite-continuity-state.js",
  "static/travel-adapter.js",
  "static/lite-update-state.js",
  "service-worker.js",
  "manifest.webmanifest",
  "badge.png",
].sort();

const sha256 = (value) => createHash("sha256").update(value).digest("hex");

async function readUiMetadata() {
  const appSource = await readFile(path.join(sourceRoot, "static/app.js"), "utf8");
  const buildMatch = appSource.match(/const LITE_UI_BUILD = "(v\d+)";/);
  if (!buildMatch) {
    throw new Error("Lite画面の版を確認できません。");
  }
  const rows = [];
  for (const name of UI_ASSET_NAMES) {
    const raw = await readFile(path.join(sourceRoot, name));
    rows.push(`${name}\0${sha256(raw)}\n`);
  }
  return {
    format_version: 1,
    ui_build: buildMatch[1],
    asset_digest: sha256(Buffer.from(rows.join(""), "utf8")),
  };
}

await mkdir(staticRoot, { recursive: true });

const replaceLiteScope = (value) => value
  .replace(
    'function isCloudHostedLite() {\n  return !window.location.pathname.startsWith("/lite");\n}',
    'function isCloudHostedLite() {\n  return true;\n}',
  )
  .replace(
    'if (!window.location.pathname.startsWith("/lite")) {\n    window.location.href = "/lite/";\n  }',
    'if (window.location.pathname !== "/") {\n    window.location.href = "/";\n  }',
  )
  .replaceAll("/lite/", "/")
  .replaceAll('"/lite"', '"/"');

await writeFile(
  path.join(publicRoot, "index.html"),
  replaceLiteScope(await readFile(path.join(sourceRoot, "index.html"), "utf8")),
  "utf8",
);
await writeFile(
  path.join(staticRoot, "app.js"),
  replaceLiteScope(await readFile(path.join(sourceRoot, "static/app.js"), "utf8")),
  "utf8",
);
await copyFile(path.join(sourceRoot, "static/styles.css"), path.join(staticRoot, "styles.css"));
await copyFile(path.join(sourceRoot, "static/pairing-handoff.js"), path.join(staticRoot, "pairing-handoff.js"));
await copyFile(path.join(sourceRoot, "static/lite-continuity-state.js"), path.join(staticRoot, "lite-continuity-state.js"));
await copyFile(path.join(sourceRoot, "static/travel-adapter.js"), path.join(staticRoot, "travel-adapter.js"));
await copyFile(path.join(sourceRoot, "static/lite-update-state.js"), path.join(staticRoot, "lite-update-state.js"));
await writeFile(
  path.join(publicRoot, "service-worker.js"),
  replaceLiteScope(await readFile(path.join(sourceRoot, "service-worker.js"), "utf8"))
    .replace(/nexus-ark-lite-v\d+/, "nexus-ark-lite-travel-phase5-v51"),
  "utf8",
);
await writeFile(
  path.join(publicRoot, "manifest.webmanifest"),
  replaceLiteScope(await readFile(path.join(sourceRoot, "manifest.webmanifest"), "utf8")),
  "utf8",
);
for (const name of ["badge.png", "icon.png", "icon-maskable.png"]) {
  await copyFile(path.join(sourceRoot, "badge.png"), path.join(publicRoot, name));
}

const metadata = JSON.stringify(await readUiMetadata(), null, 2) + "\n";
await writeFile(path.join(staticRoot, "lite-build.json"), metadata, "utf8");
if (writeSourceMetadata) {
  await writeFile(path.join(sourceRoot, "static/lite-build.json"), metadata, "utf8");
}

process.stdout.write("mobile_app正本からWorker Static Assetsを生成しました。\n");
