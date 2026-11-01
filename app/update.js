const path = require('node:path');
const fs = require('node:fs');
const securityCheck = path.join(__dirname, fs.existsSync(path.join(__dirname, 'app', 'release', 'check_uv_security.cjs'))
    ? 'app/release/check_uv_security.cjs' : 'release/check_uv_security.cjs');
const secureUv = 'node "' + securityCheck + '" && ';
module.exports = {
    run: [
        {
            method: "shell.run",
            params: {
                message: "git pull",
            }
        },
        {
            method: "shell.run",
            params: {
                message: secureUv + "uv sync",
            }
        },
        {
            method: "shell.run",
            params: {
                message: secureUv + "uv run tools/update_knowledge.py",  // Placeholder for future update logic
                on: [{ "event": null, "return": true }] // Continue even if this fails or doesn't exist yet
            }
        },
        {
            method: "notify",
            params: {
                html: "Update Complete!"
            }
        }
    ]
}
