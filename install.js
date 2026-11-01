const path = require('node:path');
const fs = require('node:fs');
const securityCheck = path.join(__dirname, fs.existsSync(path.join(__dirname, 'app', 'release', 'check_uv_security.cjs'))
    ? 'app/release/check_uv_security.cjs' : 'release/check_uv_security.cjs');
const secureUv = 'node "' + securityCheck + '" && ';
module.exports = {
    run: [
        // 2. Sync dependencies
        {
            method: "shell.run",
            params: {
                message: secureUv + "uv sync",
            }
        },
        // 3. Notify success
        {
            method: "notify",
            params: {
                html: "Installation Complete! Click 'Start Application' to begin."
            }
        }
    ]
}
