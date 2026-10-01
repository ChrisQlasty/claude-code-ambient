"""The optional long-running daemon: hook events over a unix socket, plus the web UI and API.

``ambient fire`` hands events to it when it's running and falls back to direct mode when not.
Effects still go through :mod:`ambient.engine`'s per-device lock and baseline, so the daemon,
direct-mode workers and ``ambient test`` never step on each other.
"""
