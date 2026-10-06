# KeepFlame

KeepFlame 维护长期存在的外部状态。当前首个 Flame 是聚宽每日签到：服务检查账号状态、记录历史，并在聚宽要求验证码或登录更新时明确进入 `WAITING_HUMAN`。它不会自动求解滑块验证码。

## 本地运行

要求 Python 3.13 和 `uv`：

```bash
uv sync --dev
cp config/keepflame.local.example.toml config/keepflame.local.toml
# 编辑 cookie_file，使其指向仓库外的绝对路径。
uv run keepflame status --config config/keepflame.local.toml
uv run keepflame run-once --config config/keepflame.local.toml --flame joinquant-primary
```

实际运行配置不得提交。聚宽 `cookie_file` 必须指向仓库外的 UTF-8 文件，内容是单行原始 Cookie 请求头值，不包含 `Cookie:` 前缀；建议把宿主机文件权限设为仅服务账号可读。服务每次检查都会重新读取该文件，因此轮换 Cookie 不需要重启。

`run-once` 的退出码为：成功 `0`、等待人工 `2`、失败 `1`。`status` 默认显示表格，`status --json` 提供机器可读输出；`health` 只检查配置与 SQLite，不访问聚宽，也不会因为 Flame 正在等待人工而把服务判为不健康。

## Docker Compose

先准备真实运行配置和仓库外 Cookie 文件，然后设置宿主机路径：

```bash
export KEEPFLAME_CONFIG_FILE=/absolute/path/to/keepflame.local.toml
export JOINQUANT_COOKIE_FILE=/absolute/path/to/joinquant.cookie
docker compose up --build -d
docker compose logs -f keepflame
```

容器以非 root 用户运行，根文件系统只读，不开放端口；配置和 Cookie 只读挂载，SQLite 使用独立数据卷。JSONL 日志只输出首次人工操作、20:00 提醒、失败变化与恢复事件。

## 验证边界

- `uv run pytest` 是模拟/本地测试，不访问真实聚宽账号。
- Docker 构建和健康检查只证明应用与部署外壳可运行。
- 真实验证需要用户另行提供仓库外 Cookie：先确认未签到映射为 `WAITING_HUMAN`，用户在官方页面完成签到后，再确认复检转为 `SUCCESS`。
- 真实账号的一次成功不能替代模拟测试，也不得被描述为验证码自动化。
