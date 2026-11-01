// Local-only check: never let an old downloader perform dependency requests.
const { spawnSync } = require('node:child_process');
const result = spawnSync('uv', ['--version'], { encoding: 'utf8', timeout: 10000 });
const match = /^uv (\d+)\.(\d+)\.(\d+)(?:\s|$)/.exec(result.stdout || '');
const safe = match && (Number(match[1]) > 0 || Number(match[2]) > 13 ||
  (Number(match[2]) === 13 && Number(match[3]) >= 4));
if (result.status !== 0 || !safe) {
  console.error('uv 0.13.4 or newer is required. On Windows, run PrepareSecureUpdate.bat from the new ZIP first.');
  process.exit(1);
}
