import os

import config_manager
from api.server import start_server


if __name__ == "__main__":
    config_manager.load_config()
    port = int(os.getenv("NEXUS_ARK_API_PORT") or config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}).get("port", 8000))
    host = os.getenv("NEXUS_ARK_API_HOST") or config_manager.CONFIG_GLOBAL.get("api_gateway_settings", {}).get("host", "127.0.0.1")
    start_server(port=port, host=host, daemon=False)
    try:
        while True:
            import time

            time.sleep(1)
    except KeyboardInterrupt:
        pass
