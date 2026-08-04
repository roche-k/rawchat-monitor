"""Configuration loading and shared runtime constants."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

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


@dataclass(frozen=True)
class ProxyConfig:
    socks: str
    username: str = ""
    password: str = ""

    def requests_proxies(self) -> dict[str, str]:
        auth = (
            f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
            if self.username
            else ""
        )
        url = f"socks5://{auth}{self.socks}"
        return {"http": url, "https": url}


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
    """Load the optional [proxy] SOCKS5 configuration from the TOML file.

    Returns None when the table is absent or socks is empty (direct connection).
    """
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
    socks = raw.get("socks")
    if not isinstance(socks, str) or not socks.strip():
        return None
    username = raw.get("username") or ""
    password = raw.get("password") or ""
    return ProxyConfig(
        socks=socks.strip(),
        username=str(username).strip(),
        password=str(password),
    )


def _require_socks(proxy: ProxyConfig | None) -> None:
    """Fail fast when a proxy is configured but PySocks is unavailable."""
    if proxy is None:
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
