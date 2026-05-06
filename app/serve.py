from __future__ import annotations

import os

import uvicorn


def _resolve_app_import() -> str:
    app_mode = os.environ.get("APP_MODE", "agent").strip().lower()
    if app_mode == "mcp":
        return "app.mcp_app:app"
    return "app.fast_api_app:app"


def main() -> None:
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        _resolve_app_import(),
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
