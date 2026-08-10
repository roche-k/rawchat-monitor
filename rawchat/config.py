"""Configuration loading and shared runtime constants."""

import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


BASE_URL = "https://rawchat.cn"
API_BASE_URL = "https://api.rawchat.cn"
LOGIN_URL = f"{BASE_URL}/frontend-api/login"
QUOTA_URL = f"{BASE_URL}/frontend-api/vibe-code/quota"
RECORDS_URL = f"{BASE_URL}/frontend-api/vibe-code/records"
GETME_URL = f"{BASE_URL}/frontend-api/getme"
ROLLING_LIMIT_URL = (
    f"{API_BASE_URL}/frontend-api/vibe-code/codex/rolling-limit"
)
BALANCE_URL = (
    f"{BASE_URL}/frontend-api/vibe-code/codex/billing-profile"
)
REFRESH_INTERVAL = 60
RECORD_LIMIT = 20
REQUEST_TIMEOUT = 15
UPSTREAM_CONNECT_TIMEOUT = 15
UPSTREAM_READ_TIMEOUT = 180
WORKER_STOP_TIMEOUT = 0.05
MIN_ROWS = 24
ACCOUNT_REQUEST_GAP = 2
MIN_COLS = 50
LOG_DIR = os.environ.get("RAWCHAT_LOG_DIR", "logs")
DEFAULT_ACCOUNTS_FILE = Path(__file__).resolve().parent.parent / "accounts.toml"
STATS_RESERVED = 6
CHART_RESERVED = 9
MAX_CHART_WIDTH = 200
CHART_BUCKET_MINUTES = 5

COLOR_ERROR = 1
COLOR_SUCCESS = 2
COLOR_WARNING = 3
COLOR_HEADER = 4


XRAY_START_TIMEOUT = 5.0
XRAY_STOP_TIMEOUT = 1.0
PROXY_HEALTHCHECK_URL = "https://www.google.com/generate_204"
PROXY_HEALTHCHECK_TIMEOUT = (5, 10)


def _query_value(query: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = query.get(name)
        if value is not None:
            return value
    return default


def _query_bool(query: dict[str, str], *names: str, default: bool = False) -> bool:
    value = _query_value(query, *names)
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _query_int(query: dict[str, str], name: str) -> int | None:
    value = query.get(name)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"VLESS 参数 {name} 必须是整数") from exc


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("|", ",").split(",") if item.strip()]


def _parse_vless_query(url: str) -> tuple[str, int, str, dict[str, str]]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "vless":
            raise ValueError("VLESS 链接必须使用 vless:// scheme")
        host = parsed.hostname
        port = parsed.port
        uuid = unquote(parsed.username or "")
    except ValueError as exc:
        raise ValueError(f"VLESS 链接格式无效: {exc}") from exc
    if not host or port is None or not uuid:
        raise ValueError("VLESS 链接缺少 UUID、地址或端口")
    values = parse_qs(parsed.query, keep_blank_values=True)
    query = {key: unquote(items[-1]) for key, items in values.items()}
    return host, port, uuid, query


def build_xray_config(url: str, socks_port: int) -> dict[str, Any]:
    """Convert a VLESS share link into an Xray SOCKS-inbound config."""
    host, port, uuid, query = _parse_vless_query(url)
    security = _query_value(query, "security", default="none").lower()
    network = _query_value(query, "type", "network", default="tcp").lower()
    network = {"splithttp": "xhttp", "httpupgrade": "httpupgrade"}.get(
        network, network
    )
    if security not in {"none", "tls", "reality"}:
        raise ValueError(f"VLESS security 不支持: {security}")
    if network not in {
        "tcp",
        "raw",
        "ws",
        "grpc",
        "http",
        "h2",
        "httpupgrade",
        "xhttp",
        "kcp",
        "mkcp",
        "quic",
    }:
        raise ValueError(f"VLESS 传输类型不支持: {network}")

    user: dict[str, Any] = {
        "id": uuid,
        "encryption": _query_value(query, "encryption", default="none"),
    }
    if query.get("flow"):
        user["flow"] = query["flow"]
    level = _query_int(query, "level")
    if level is not None:
        user["level"] = level

    stream: dict[str, Any] = {
        "network": "tcp" if network == "raw" else network,
        "security": security,
    }
    if security == "tls":
        tls: dict[str, Any] = {
            "serverName": _query_value(query, "sni", "serverName", default=host),
            "allowInsecure": _query_bool(
                query, "allowInsecure", "insecure", default=False
            ),
        }
        if query.get("alpn"):
            tls["alpn"] = _split_list(query["alpn"])
        fingerprint = _query_value(query, "fp", "fingerprint")
        if fingerprint:
            tls["fingerprint"] = fingerprint
        for source, target in (
            ("echConfigList", "echConfigList"),
            ("echForceQuery", "echForceQuery"),
            ("pinnedPeerCertSha256", "pinnedPeerCertSha256"),
        ):
            if query.get(source):
                tls[target] = query[source]
        stream["tlsSettings"] = tls
    elif security == "reality":
        reality: dict[str, Any] = {
            "serverName": _query_value(query, "sni", "serverName", default=host),
            "fingerprint": _query_value(query, "fp", "fingerprint", default="chrome"),
            "publicKey": _query_value(query, "pbk", "publicKey"),
            "shortId": _query_value(query, "sid", "shortId"),
        }
        spider = _query_value(query, "spx", "spiderX")
        if spider:
            reality["spiderX"] = spider
        if query.get("mldsa65Verify"):
            reality["mldsa65Verify"] = query["mldsa65Verify"]
        stream["realitySettings"] = reality

    if stream["network"] == "tcp":
        header_type = _query_value(query, "headerType", default="none")
        if header_type != "none":
            header: dict[str, Any] = {"type": header_type}
            if query.get("host"):
                header["request"] = {"headers": {"Host": _split_list(query["host"])}}
            if query.get("path"):
                header.setdefault("request", {})["path"] = _split_list(query["path"])
            stream["tcpSettings"] = {"header": header}
    elif stream["network"] == "ws":
        ws: dict[str, Any] = {"path": _query_value(query, "path", default="/")}
        ws_host = _query_value(query, "host", "authority")
        if ws_host:
            ws["headers"] = {"Host": ws_host}
        early_data = _query_int(query, "ed")
        if early_data is not None:
            ws["maxEarlyData"] = early_data
        early_header = _query_value(query, "eh")
        if early_header:
            ws["earlyDataHeaderName"] = early_header
        stream["wsSettings"] = ws
    elif stream["network"] == "grpc":
        grpc: dict[str, Any] = {
            "serviceName": _query_value(query, "serviceName", default=""),
            "authority": _query_value(query, "authority", "host"),
        }
        grpc["multiMode"] = _query_value(query, "mode").lower() == "multi"
        stream["grpcSettings"] = grpc
    elif stream["network"] in {"http", "h2"}:
        http_settings: dict[str, Any] = {
            "path": _query_value(query, "path", default="/"),
            "host": _split_list(_query_value(query, "host", "authority")),
        }
        stream["httpSettings"] = http_settings
    elif stream["network"] == "httpupgrade":
        stream["httpupgradeSettings"] = {
            "path": _query_value(query, "path", default="/"),
            "host": _query_value(query, "host", "authority"),
        }
    elif stream["network"] == "xhttp":
        xhttp: dict[str, Any] = {
            "path": _query_value(query, "path", default="/"),
            "host": _query_value(query, "host", "authority"),
            "mode": _query_value(query, "mode", default="auto"),
        }
        if query.get("extra"):
            try:
                extra = json.loads(query["extra"])
            except (TypeError, ValueError) as exc:
                raise ValueError("VLESS xhttp extra 参数不是有效 JSON") from exc
            if not isinstance(extra, dict):
                raise ValueError("VLESS xhttp extra 参数必须是 JSON 对象")
            xhttp.update(extra)
        stream["xhttpSettings"] = xhttp
    elif stream["network"] in {"kcp", "mkcp"}:
        kcp: dict[str, Any] = {}
        for name in (
            "mtu",
            "tti",
            "uplinkCapacity",
            "downlinkCapacity",
            "congestion",
            "readBufferSize",
            "writeBufferSize",
        ):
            value = _query_int(query, name)
            if value is not None:
                kcp[name] = value
        header_type = _query_value(query, "headerType", default="none")
        kcp["header"] = {"type": header_type}
        if query.get("seed"):
            kcp["seed"] = query["seed"]
        stream["kcpSettings"] = kcp
    elif stream["network"] == "quic":
        stream["quicSettings"] = {
            "security": _query_value(query, "quicSecurity", "security", default="none"),
            "key": _query_value(query, "key"),
            "header": {"type": _query_value(query, "headerType", default="none")},
        }

    settings: dict[str, Any] = {
        "vnext": [
            {
                "address": host,
                "port": port,
                "users": [user],
            }
        ]
    }
    packet_encoding = _query_value(query, "packetEncoding")
    if packet_encoding:
        settings["packetEncoding"] = packet_encoding
    outbound: dict[str, Any] = {
        "protocol": "vless",
        "settings": settings,
        "streamSettings": stream,
    }
    if _query_bool(query, "mux", default=False):
        mux: dict[str, Any] = {"enabled": True}
        concurrency = _query_int(query, "muxConcurrency")
        if concurrency is not None:
            mux["concurrency"] = concurrency
        outbound["mux"] = mux

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": socks_port,
                "protocol": "http",
                "settings": {},
            }
        ],
        "outbounds": [outbound, {"protocol": "freedom", "tag": "direct"}],
    }


@dataclass
class ProxyConfig:
    socks: str = ""
    username: str = ""
    password: str = ""
    url: str = ""
    xray: str = ""
    config_error: str = ""
    _active_socks: str = field(default="", init=False, repr=False)
    _state: str = field(default="", init=False, repr=False)
    _reason: str = field(default="", init=False, repr=False)
    _process: Any = field(default=None, init=False, repr=False)
    _config_path: Path | None = field(default=None, init=False, repr=False)
    _process_log_thread: threading.Thread | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _event_logger: Callable[..., Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _failure_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.socks and self.url:
            self.config_error = "代理配置同时包含 socks 和 url"
        if self.config_error:
            self._state = "failed"
            self._reason = self.config_error
        elif self.socks:
            self._active_socks = self.socks
            self._state = "active"
        elif self.url:
            self._state = "inactive"
        else:
            self._state = "inactive"

    @property
    def configured(self) -> bool:
        return bool(self.socks or self.url or self.config_error)

    @property
    def using(self) -> bool:
        with self._lock:
            self._poll_process_unlocked()
            return self._state == "active" and bool(self._active_socks)

    def _poll_process_unlocked(self) -> None:
        process = self._process
        return_code = process.poll() if process is not None else None
        if process is not None and return_code is not None and self._state == "active":
            self._emit_event(
                "proxy_process_exit",
                return_code=return_code,
                proxy_kind=self._proxy_kind_unlocked(),
            )
            self._fail_unlocked("Xray 进程已退出")

    def set_event_logger(self, logger: Callable[..., Any] | None) -> None:
        """Attach the structured event sink used by the local proxy server."""
        with self._lock:
            self._event_logger = logger

    @staticmethod
    def _error_message(error: BaseException | None) -> str:
        if error is None:
            return ""
        return str(error)

    def _emit_event(self, event: str, **fields: Any) -> None:
        logger = self._event_logger
        if not callable(logger):
            return
        try:
            logger(event, **fields)
        except Exception:
            # Proxy diagnostics must never break the request path.
            return

    def _proxy_kind_unlocked(self) -> str:
        return "managed_xray" if self.url else "socks5"

    def _read_process_output(self, stream: io.IOBase) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                if isinstance(line, bytes):
                    message = line.decode("utf-8", "replace")
                else:
                    message = str(line)
                message = message.rstrip("\r\n")
                if message:
                    self._emit_event(
                        "proxy_process_log",
                        stream="xray",
                        proxy_kind=self._proxy_kind_unlocked(),
                        message=message,
                    )
        except Exception as exc:
            self._emit_event(
                "proxy_process_log_error",
                stream="xray",
                proxy_kind=self._proxy_kind_unlocked(),
                error_type=type(exc).__name__,
                error_message=self._error_message(exc),
            )

    def _start_process_log_reader_unlocked(self) -> None:
        stream = getattr(self._process, "stdout", None)
        if not isinstance(stream, io.IOBase):
            return
        thread = threading.Thread(
            target=self._read_process_output,
            args=(stream,),
            name="rawchat-xray-log",
            daemon=True,
        )
        self._process_log_thread = thread
        thread.start()

    def requests_proxies(self) -> dict[str, str]:
        with self._lock:
            self._poll_process_unlocked()
            if self._state != "active" or not self._active_socks:
                return {}
            auth = (
                f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
                if self.username
                else ""
            )
            scheme = "http" if self.url else "socks5"
            url = f"{scheme}://{auth}{self._active_socks}"
            return {"http": url, "https": url}

    def _fail_unlocked(
        self,
        reason: str,
        error: BaseException | None = None,
    ) -> None:
        self._state = "failed"
        self._reason = reason
        self._active_socks = ""
        self._emit_event(
            "proxy_fallback",
            reason=reason,
            error_type=type(error).__name__ if error else None,
            error_message=self._error_message(error),
            proxy_kind=self._proxy_kind_unlocked(),
        )
        self._stop_process_unlocked()

    def mark_failed(
        self,
        _error: BaseException | None = None,
        reason: str = "代理连接失败",
    ) -> None:
        """Force the proxy into direct mode for unrecoverable configuration errors."""
        with self._lock:
            if self._state == "active":
                self._emit_event(
                    "proxy_failure_detected",
                    reason=reason,
                    error_type=type(_error).__name__ if _error else None,
                    error_message=self._error_message(_error),
                    proxy_kind=self._proxy_kind_unlocked(),
                )
                self._fail_unlocked(reason, error=_error)

    def _health_check_proxies(self) -> dict[str, str]:
        with self._lock:
            self._poll_process_unlocked()
            if self._state != "active" or not self._active_socks:
                return {}
            return self.requests_proxies()

    def check_health(self) -> bool:
        """Check a proxy-independent endpoint through the configured proxy."""
        proxy_urls = self._health_check_proxies()
        if not proxy_urls:
            return False
        response = None
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(
                    PROXY_HEALTHCHECK_URL,
                    proxies=proxy_urls,
                    timeout=PROXY_HEALTHCHECK_TIMEOUT,
                    allow_redirects=False,
                )
                status = response.status_code
                healthy = status == 204
                self._emit_event(
                    "proxy_health_check",
                    url=PROXY_HEALTHCHECK_URL,
                    status=status,
                    expected_status=204,
                    healthy=healthy,
                    proxy_kind=self._proxy_kind_unlocked(),
                )
                return healthy
        except (OSError, requests.RequestException) as exc:
            self._emit_event(
                "proxy_health_check",
                url=PROXY_HEALTHCHECK_URL,
                status=None,
                expected_status=204,
                healthy=False,
                error_type=type(exc).__name__,
                error_message=self._error_message(exc),
                proxy_kind=self._proxy_kind_unlocked(),
            )
            return False
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def handle_failure(
        self,
        error: BaseException | None = None,
        *,
        reason: str = "代理连接失败",
        target: str | None = None,
    ) -> bool:
        """Return whether the caller should retry directly after a proxy error."""
        with self._failure_lock:
            with self._lock:
                self._poll_process_unlocked()
                if self._state != "active" or not self._active_socks:
                    return True
                proxy_kind = self._proxy_kind_unlocked()
            self._emit_event(
                "proxy_failure_detected",
                reason=reason,
                error_type=type(error).__name__ if error else None,
                error_message=self._error_message(error),
                proxy_kind=proxy_kind,
                target=target,
            )
            if self.check_health():
                self._emit_event(
                    "proxy_failure_recovered",
                    reason=reason,
                    proxy_kind=proxy_kind,
                    target=target,
                )
                return False
            with self._lock:
                if self._state == "active":
                    self._fail_unlocked(reason, error=error)
            return True

    def _stop_process_unlocked(self) -> None:
        process = self._process
        self._process = None
        log_thread = self._process_log_thread
        self._process_log_thread = None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=XRAY_STOP_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=XRAY_STOP_TIMEOUT)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if log_thread is not None and log_thread is not threading.current_thread():
            log_thread.join(timeout=XRAY_STOP_TIMEOUT)
        if self._config_path is not None:
            try:
                self._config_path.unlink()
            except OSError:
                pass
            self._config_path = None

    def start(self) -> None:
        with self._lock:
            if not self.configured or self._state in {"active", "failed"}:
                return
            if self.config_error:
                return
            if self.socks:
                self._active_socks = self.socks
                self._state = "active"
                return
            executable = shutil.which(self.xray or "xray")
            if not executable:
                self._fail_unlocked("Xray 未找到")
                return
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = int(probe.getsockname()[1])
                config_fd, config_name = tempfile.mkstemp(
                    prefix="rawchat-xray-", suffix=".json"
                )
                os.close(config_fd)
                config_path = Path(config_name)
                self._config_path = config_path
                config_path.write_text(
                    json.dumps(build_xray_config(self.url, port), ensure_ascii=True),
                    encoding="utf-8",
                )
                os.chmod(config_path, 0o600)
                self._process = subprocess.Popen(
                    [executable, "run", "-config", str(config_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                self._start_process_log_reader_unlocked()
                process_id = getattr(self._process, "pid", None)
                self._emit_event(
                    "proxy_process_started",
                    pid=process_id if isinstance(process_id, int) else None,
                    proxy_kind=self._proxy_kind_unlocked(),
                )
                deadline = time.monotonic() + XRAY_START_TIMEOUT
                while time.monotonic() < deadline:
                    if self._process.poll() is not None:
                        self._fail_unlocked("Xray 启动失败")
                        return
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            self._active_socks = f"127.0.0.1:{port}"
                            self._state = "active"
                            return
                    except OSError:
                        time.sleep(0.05)
                self._fail_unlocked("Xray 启动超时")
            except ValueError as exc:
                self._fail_unlocked("VLESS 配置无效", error=exc)
            except (OSError, subprocess.SubprocessError) as exc:
                self._fail_unlocked("Xray 启动失败", error=exc)

    def stop(self) -> None:
        with self._lock:
            self._active_socks = ""
            self._state = "inactive"
            self._stop_process_unlocked()

    def status_text(self) -> str:
        with self._lock:
            self._poll_process_unlocked()
            if not self.configured:
                return "代理未配置 | 当前直连"
            if self._state == "active":
                return "代理使用中 | 当前代理"
            if self._reason:
                return f"代理失效 | 当前直连 | {self._reason}"
            return "代理未启用 | 当前直连"


def load_accounts(path: str | os.PathLike[str]) -> list[dict[str, str]]:
    """Load and validate account credentials from an external TOML file."""
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ValueError(f"账号配置文件不存在: {config_path}")
    if os.name == "posix" and config_path.stat().st_mode & 0o077:
        raise ValueError(f"账号配置文件权限过宽，请设置为 600: {config_path}")
    if tomllib is None:
        raise RuntimeError("TOML parser unavailable")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"账号配置文件无法解析: {config_path}") from exc

    raw_accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("账号配置必须包含至少一个 [[accounts]]")

    accounts: list[dict[str, str]] = []
    for index, raw_account in enumerate(raw_accounts, start=1):
        if not isinstance(raw_account, dict):
            raise ValueError(f"第 {index} 个账号配置格式无效")
        email = raw_account.get("email")
        password = raw_account.get("password")
        if not isinstance(email, str) or not email.strip():
            raise ValueError(f"第 {index} 个账号缺少有效 email")
        if not isinstance(password, str) or not password:
            raise ValueError(f"第 {index} 个账号缺少有效 password")
        accounts.append({"email": email.strip(), "password": password})
    return accounts


def load_proxy_config(path: str | os.PathLike[str]) -> ProxyConfig | None:
    """Load the optional external SOCKS5 or managed VLESS configuration."""
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ValueError(f"账号配置文件不存在: {config_path}")
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"账号配置文件无法解析: {config_path}") from exc
    raw = data.get("proxy") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    socks_value = raw.get("socks")
    url_value = raw.get("url")
    xray_value = raw.get("xray")
    socks = socks_value.strip() if isinstance(socks_value, str) else ""
    url = url_value.strip() if isinstance(url_value, str) else ""
    xray = xray_value.strip() if isinstance(xray_value, str) else ""
    if not socks and not url and not xray:
        return None
    if socks and url:
        return ProxyConfig(config_error="代理配置同时包含 socks 和 url")
    if xray and not url:
        return ProxyConfig(config_error="xray 配置必须与 url 一起使用")
    username = raw.get("username") or ""
    password = raw.get("password") or ""
    return ProxyConfig(
        socks=socks,
        username=str(username).strip(),
        password=str(password),
        url=url,
        xray=xray,
    )


def _require_socks(proxy: ProxyConfig | None) -> None:
    """Fail fast when an external SOCKS5 proxy lacks PySocks."""
    if proxy is None or not proxy.socks:
        return
    try:
        import socks  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "已配置 SOCKS5 代理，但未安装 PySocks。请运行: pip install PySocks"
        ) from exc


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://rawchat.cn/pastel/",
    "Origin": "https://rawchat.cn",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}
