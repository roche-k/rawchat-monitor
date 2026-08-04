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

## Optional SOCKS5 proxy

All upstream HTTPS connections (quota refresh and the local proxy) can be routed
through a SOCKS5 proxy by adding an optional `[proxy]` table to the same
configuration file:

```toml
[proxy]
socks = "127.0.0.1:1080"
# username = ""
# password = ""
```

When the `[proxy]` table is absent or `socks` is empty, the program connects
directly (default). If a proxy is configured, the `PySocks` package must be
installed (`pip install PySocks` or `pip install "requests[socks]"`); otherwise
the program exits with a clear error instead of silently falling back.

## Log repair utility

`fix_logs.py` requires the email to be supplied explicitly:

```bash
python fix_logs.py --log-dir logs --email account@example.com
```

Keep `logs/`, runtime caches, and generated test output local. Do not commit
real configuration files or request logs.
