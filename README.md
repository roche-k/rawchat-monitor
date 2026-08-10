# RawChat Codex Monitor

This repository contains the monitor and local proxy code. Account credentials
are intentionally kept outside the repository.

## Account configuration

Copy `accounts.example.toml` next to `rawchat_monitor.py` as `accounts.toml`
and edit the copy with real values:

```bash
cp accounts.example.toml accounts.toml
chmod 600 accounts.toml
```

The file uses one `[[accounts]]` table per account:

```toml
[[accounts]]
email = "account@example.com"
password = "your-password"
```

Start the monitor without arguments to use the same-directory configuration.
The password itself is never a command-line argument or an environment
variable:

```bash
python rawchat_monitor.py
```

To use a configuration file elsewhere, pass its path as the single positional
argument:

```bash
python rawchat_monitor.py /path/to/accounts.toml
```

The program rejects a missing file, invalid TOML, empty account lists, and
group/other-readable files on POSIX systems.

## Optional proxy

All upstream HTTPS connections (quota refresh and the local proxy) can be routed
through an external SOCKS5 proxy by adding an optional `[proxy]` table to the
same configuration file:

```toml
[proxy]
socks = "127.0.0.1:1080"
# username = ""
# password = ""
```

When the `[proxy]` table is absent or `socks` is empty, the program connects
directly (default). For an external SOCKS5 proxy, the `PySocks` package must be
installed (`pip install PySocks` or `pip install "requests[socks]"`); otherwise
the program falls back to a direct connection and shows that state in the
dashboard.

### VLESS through Xray

The same table can start a local Xray HTTP CONNECT listener from a VLESS share
link:

```toml
[proxy]
url = "vless://UUID@example.com:443?security=tls&type=tcp"
# Optional; defaults to an executable named xray found in PATH.
xray = "/usr/local/bin/xray"
```

The monitor maps common VLESS TLS/REALITY parameters and transports (TCP,
WebSocket, gRPC, HTTP Upgrade and XHTTP, plus legacy link forms where the
selected Xray version still supports them). Xray itself validates the generated
configuration, so links using a transport removed by the installed Xray
version fail safely to direct connection instead of being silently rewritten.
It only starts an existing Xray executable; it never downloads one. The local
HTTP CONNECT listener does not require PySocks. For a request failure while a
proxy is active, the monitor first requests
`https://www.google.com/generate_204` through that same proxy and requires HTTP
204. A healthy proxy is kept active and the request is retried through it when
the response has not started; only a failed health check switches subsequent
requests to direct connection and stops managed Xray. Startup and configuration
errors still fall back directly. The dashboard always shows whether a proxy is
configured and whether requests are currently using it. Do not set `socks` and
`url` together.

### Proxy diagnostics

Proxy events are written to `logs/rawchat_proxy_YYYY-MM-DD.jsonl`. The records
include the original request or stream error, the Google health-check result,
the recovery/fallback decision, and captured Xray output. Request bodies are not
written to this log; error text is kept as returned for diagnosis.

## Log repair utility

`fix_logs.py` requires the email to be supplied explicitly:

```bash
python fix_logs.py --log-dir logs --email account@example.com
```

Keep `logs/`, runtime caches, and generated test output local. Do not commit
real configuration files or request logs.
