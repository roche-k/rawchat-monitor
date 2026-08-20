# RawChat Codex Monitor

本项目提供 RawChat Codex 用量监控和本地代理。

## 账号配置

复制示例配置并填写真实账号：

```bash
cp accounts.example.toml accounts.toml
chmod 600 accounts.toml
```

每个账号使用一个 `[[accounts]]` 表；多个账号的排列顺序也是初始路由
优先级：

```toml
[[accounts]]
email = "account-1@example.com"
password = "your-password-1"

[[accounts]]
email = "account-2@example.com"
password = "your-password-2"
```

不带参数启动时，程序读取 `rawchat_monitor.py` 同目录下的
`accounts.toml`。密码不会作为命令行参数或环境变量传入：

```bash
python rawchat_monitor.py
```

也可以显式指定其他配置文件：

```bash
python rawchat_monitor.py /path/to/accounts.toml
```

程序会拒绝不存在的文件、无效 TOML、空账号列表，以及 POSIX 系统中对组
用户或其他用户可读的账号文件。

## 切号功能原理

请求先使用当前账号；当前账号的额度不足，或者
3 小时滚动窗口达到限制，就切换到下一个可用账号。账号恢复额度或窗口
解除限制后，才会重新成为可用账号。这种策略每天切号次数非常少，每天仅仅
会切换几次账号。

例如，假设账号 1 和账号 2 各自都有每天 100 美元额度、3 小时窗口 30 美元：

| 总切换次数 | 原账号 | 触发条件 | 切换到 | 账号 1 每日剩余 | 账号 2 每日剩余 |
| ---: | --- | --- | --- | ---: | ---: |
| 1 | 账号 1 | 使用 30 美元 | 账号 2 | 70 | 100 |
| 2 | 账号 2 | 使用 30 美元 | 账号 1 | 70 | 70 |
| 3 | 账号 1 | 使用 30 美元 | 账号 2 | 40 | 70 |
| 4 | 账号 2 | 使用 30 美元 | 账号 1 | 40 | 40 |
| 5 | 账号 1 | 使用 30 美元 | 账号 2 | 10 | 40 |
| 6 | 账号 2 | 使用 30 美元 | 账号 1 | 10 | 10 |
| 7 | 账号 1 | 使用最后 10 美元，每日额度耗尽 | 账号 2 | 0 | 10 |
| 8 | 账号 2 | 使用最后 10 美元，每日额度耗尽 | 无可用账号 | 0 | 0 |

## Codex 接入

监控程序启动本地 HTTP 代理。默认端口为 `15722`；第一次刷新成功后，程序
会将 Codex 配置中的 provider 指向：

```text
http://127.0.0.1:15722/v1
```

如果不希望程序接管 Codex 配置，可使用：

```bash
python rawchat_monitor.py --no-apply-codex-config
```

程序退出时不会自动把 `model_provider` 改回原值；需要切回其他 provider
时手动修改 Codex 配置，或使用另一份配置文件。

## 可选代理

可以在同一个账号配置文件中增加 `[proxy]`，让额度刷新和本地代理的上游
HTTPS 请求通过外部 SOCKS5 代理：

```toml
[proxy]
socks = "127.0.0.1:1080"
# username = ""
# password = ""
```

没有 `[proxy]`，或 `socks` 为空时默认直连。使用外部 SOCKS5 代理需要安装
`PySocks`：

```bash
pip install PySocks
# 或
pip install "requests[socks]"
```

如果未安装，程序会回退到直连，并在监控界面显示当前状态。

### 通过 Xray 使用 VLESS

同一个 `[proxy]` 表也支持从 VLESS 分享链接启动本地 Xray HTTP CONNECT
监听器：

```toml
[proxy]
url = "vless://UUID@example.com:443?security=tls&type=tcp"
# 可选；默认使用 PATH 中名为 xray 的可执行文件
xray = "/usr/local/bin/xray"
```

程序会映射常见的 VLESS TLS/REALITY 参数和 TCP、WebSocket、gRPC、HTTP
Upgrade、XHTTP 等传输方式，也兼容已安装 Xray 版本仍支持的旧链接格式。
Xray 会自行校验生成的配置；如果传输方式已被当前版本移除，程序会安全地
回退直连，不会静默改写链接。程序只启动已经存在的 Xray 可执行文件，不会
自动下载。

本地 HTTP CONNECT 监听器不需要 `PySocks`。代理正在使用时，如果请求失败，
程序会先通过同一个代理访问 `https://www.google.com/generate_204`，并要求
返回 HTTP 204。健康检查成功则继续使用代理并重试请求；只有健康检查失败
才会让后续请求改用直连并停止程序管理的 Xray。启动或配置错误仍直接回退。
监控界面会显示代理是否配置以及当前请求是否正在使用代理。`socks` 和
`url` 不能同时配置。

### 代理诊断

代理事件写入 `logs/rawchat_proxy_YYYY-MM-DD.jsonl`，包括原始请求或流错误、
Google 健康检查结果、恢复或回退决定，以及 Xray 输出。日志不会写入请求体；
错误文本按上游返回内容保留，方便诊断。

## 日志修复工具

`fix_logs.py` 要求显式指定账号邮箱：

```bash
python fix_logs.py --log-dir logs --email account@example.com
```

请将 `logs/`、运行时缓存和生成的测试输出保留在本地，不要提交真实配置文件
或请求日志。
