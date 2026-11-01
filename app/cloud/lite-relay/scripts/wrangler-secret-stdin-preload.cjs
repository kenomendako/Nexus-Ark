'use strict';

// Wrangler 4.149.0 requires a --secrets-file path for a new Worker whose
// configuration declares secrets.required. This preload supplies that one
// read from stdin-backed memory without creating a plaintext file.

const fs = require('node:fs');
const path = require('node:path');

const EXPECTED_WRANGLER_VERSION = '4.149.0';
const VIRTUAL_SECRET_BASENAME = '.nexus-ark-virtual-secrets.json';
const REQUIRED_SECRET_NAMES = Object.freeze([
  'OWNER_AUTH_TOKEN',
  'BUNDLE_SIGNING_KEY',
  'STANDBY_ENCRYPTION_KEY',
]);
const MAX_SECRET_INPUT_BYTES = 64 * 1024;

function fail(message) {
  throw new Error(`Nexus Ark Secret bootstrap gate: ${message}`);
}

const secretsFileIndex = process.argv.indexOf('--secrets-file');
if (secretsFileIndex < 0 || !process.argv[secretsFileIndex + 1]) {
  fail('--secrets-fileの仮想パスがありません。');
}
if (process.argv.indexOf('--secrets-file', secretsFileIndex + 1) >= 0) {
  fail('--secrets-fileは1件だけ指定できます。');
}

const virtualSecretPath = path.resolve(process.argv[secretsFileIndex + 1]);
if (path.basename(virtualSecretPath) !== VIRTUAL_SECRET_BASENAME) {
  fail('許可されていない仮想パスです。');
}
if (fs.existsSync(virtualSecretPath)) {
  fail('仮想パスに実ファイルが存在します。');
}

const wranglerCliPath = path.resolve(process.argv[1] || '');
const wranglerPackagePath = path.resolve(
  path.dirname(wranglerCliPath),
  '..',
  'package.json',
);
let wranglerPackage;
let wranglerSource;
try {
  wranglerPackage = JSON.parse(fs.readFileSync(wranglerPackagePath, 'utf8'));
  wranglerSource = fs.readFileSync(wranglerCliPath, 'utf8');
} catch {
  fail('固定Wranglerの契約を確認できません。');
}
if (wranglerPackage.version !== EXPECTED_WRANGLER_VERSION) {
  fail('固定Wranglerのversionが一致しません。');
}
const requiredSourceFragments = [
  'async function parseBulkInputToObject(input2, includeNull = false)',
  'const jsonFilePath = path29__namespace.default.resolve(input2);',
  'const fileContent = readFileSync(jsonFilePath);',
  'const secretsResult = await parseBulkInputToObject(props.secretsFile);',
];
if (!requiredSourceFragments.every((fragment) => wranglerSource.includes(fragment))) {
  fail('固定WranglerのSecret読込契約が変わっています。');
}
wranglerSource = '';

const secretBuffer = fs.readFileSync(0);
if (secretBuffer.length === 0 || secretBuffer.length > MAX_SECRET_INPUT_BYTES) {
  secretBuffer.fill(0);
  fail('Secret入力のサイズが許可範囲外です。');
}
let parsedSecrets;
try {
  parsedSecrets = JSON.parse(secretBuffer.toString('utf8'));
} catch {
  secretBuffer.fill(0);
  fail('Secret入力がJSON objectではありません。');
}
const receivedNames = Object.keys(parsedSecrets).sort();
const expectedNames = [...REQUIRED_SECRET_NAMES].sort();
if (
  receivedNames.length !== expectedNames.length ||
  receivedNames.some((name, index) => name !== expectedNames[index]) ||
  expectedNames.some(
    (name) => typeof parsedSecrets[name] !== 'string' || parsedSecrets[name].length === 0,
  )
) {
  secretBuffer.fill(0);
  fail('Secret名または値がbootstrap契約と一致しません。');
}
for (const name of expectedNames) {
  parsedSecrets[name] = '';
}
parsedSecrets = undefined;

const originalReadFileSync = fs.readFileSync;
let virtualReadCount = 0;
fs.readFileSync = function nexusArkVirtualSecretRead(target, ...args) {
  const targetPath =
    typeof target === 'string' || Buffer.isBuffer(target) || target instanceof URL
      ? path.resolve(target.toString())
      : '';
  if (targetPath !== virtualSecretPath) {
    return originalReadFileSync.call(fs, target, ...args);
  }
  virtualReadCount += 1;
  if (virtualReadCount !== 1 || fs.existsSync(virtualSecretPath)) {
    secretBuffer.fill(0);
    fail('仮想Secretの読込回数または実体検査に失敗しました。');
  }
  const encoding =
    typeof args[0] === 'string'
      ? args[0]
      : args[0] && typeof args[0] === 'object'
        ? args[0].encoding
        : null;
  return encoding ? secretBuffer.toString(encoding) : secretBuffer;
};

process.once('exit', () => {
  secretBuffer.fill(0);
  fs.readFileSync = originalReadFileSync;
  if (virtualReadCount !== 1) {
    process.exitCode = 86;
  }
});
