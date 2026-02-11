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
                message: "uv sync",
            }
        },
        {
            method: "shell.run",
            params: {
                message: "uv run tools/update_knowledge.py",  // Placeholder for future update logic
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
