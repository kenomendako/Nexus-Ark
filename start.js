module.exports = {
    run: [
        {
            method: "shell.run",
            params: {
                path: "app",
                message: "node release/check_uv_security.cjs && uv run nexus_ark.py",
            }
        }
    ]
}
