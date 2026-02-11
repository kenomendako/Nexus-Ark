module.exports = {
    run: [
        // 1. Install uv (if not present)
        {
            method: "shell.run",
            params: {
                message: "curl -LsSF https://astral.sh/uv/install.sh | sh",
            }
        },
        // 2. Sync dependencies
        {
            method: "shell.run",
            params: {
                message: "uv sync",
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
