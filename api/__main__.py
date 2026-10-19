"""Run the API with Uvicorn.

    python -m api --local                   this machine: 127.0.0.1:8000, models from data/, no auth,
                                            /docs on, auto-reload, readable logs
    python -m api --local --auth api_key    the same with the API key from .env (API_KEYS=...)
    python -m api                           the container: settings from the environment only

The local profile refuses to bind to anything but loopback without auth. On first start it builds a
directory snapshot in data/directory from data/instances.csv and the model's alert names.
"""

from __future__ import annotations

import argparse
import os
import secrets

from ticket_analysis.paths import REPO_ROOT

from .settings import ConfigError, is_loopback, load_settings, local_profile


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local", action="store_true", help="the local profile (see above)")
    p.add_argument("--auth", choices=("none", "api_key"), default="none",
                   help="with --local: none (default) or api_key, with the key from .env")
    p.add_argument("--host", help="address to bind (default API_HOST: 0.0.0.0, or 127.0.0.1 with --local)")
    p.add_argument("--port", type=int, help="port (default API_PORT, 8000)")
    p.add_argument("--debug-responses", action="store_true",
                   help="with --local and no auth: add scored candidates, cleaned text and rule checks to responses")
    p.add_argument("--no-reload", action="store_true", help="with --local: no auto-reload (for the debugger)")
    return p.parse_args(argv)


def _api_key_from_env_file() -> str:
    from dotenv import dotenv_values  # installed with pydantic-settings
    path = REPO_ROOT / ".env"
    keys = (dotenv_values(path) if path.exists() else {}).get("API_KEYS") or ""
    if not keys.strip():
        raise SystemExit(f"--auth api_key reads API_KEYS from {path} (gitignored). Add a line such as\n"
                         f"API_KEYS={secrets.token_urlsafe(32)}\nand send that key in the X-API-Key header.")
    return keys


def main(argv=None) -> None:
    import uvicorn

    from . import logs
    args = parse_args(argv)
    if args.local:
        os.environ.update(local_profile(args.auth))
        os.environ["AUTH_MODE"] = args.auth  # the flag wins over an inherited AUTH_MODE
        if args.auth == "api_key" and "API_KEYS" not in os.environ:
            os.environ["API_KEYS"] = _api_key_from_env_file()
        if args.debug_responses:
            os.environ["DEBUG_RESPONSES"] = "true"
    if args.host:
        os.environ["API_HOST"] = args.host
    if args.port:
        os.environ["API_PORT"] = str(args.port)
    try:
        settings = load_settings()
        settings.check_serving()
    except ConfigError as e:
        raise SystemExit(str(e)) from None
    if args.local and settings.auth_mode == "none" and not is_loopback(settings.api_host):
        raise SystemExit("The local profile without auth binds only to loopback; use --auth api_key to serve others")
    logs.configure(settings.log_level, settings.log_format)
    if args.local:
        from .refresh_directory import bootstrap_local_snapshot
        bootstrap_local_snapshot(settings)
    reload = args.local and not args.no_reload
    # One process: the models take memory per process, and Container Apps scales with replicas.
    # Inference runs on WORKERS threads inside it.
    uvicorn.run("api.app:create_app", factory=True, host=settings.api_host, port=settings.api_port, reload=reload,
                reload_dirs=[str(REPO_ROOT / "api"), str(REPO_ROOT / "ticket_analysis")] if reload else None,
                log_config=None, access_log=False, proxy_headers=True, server_header=False,
                limit_concurrency=settings.max_concurrent_requests, timeout_graceful_shutdown=20)


if __name__ == "__main__":
    main()
