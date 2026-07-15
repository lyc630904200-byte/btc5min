# polybtc 交接文档

## 项目范围

`polybtc` 是 Polymarket BTC 5 分钟 Up/Down 市场的 **paper trading** 研究工具。它读取 Binance BTCUSDT 行情、Polymarket 市场阈值和 CLOB 盘口，按策略模拟开平仓；不保存私钥，也不会发送真实订单。

工作目录：`D:\Users\Administrator\Documents\btc5fenzhong`

分支：`codex/jiaoyi`
远端：`origin https://github.com/lyc630904200-byte/btc5min.git`

## 当前交接状态（2026-07-15）

- 已提交的最新版本：`e4ad1f0 修正回放测试时间`。
- 本地与 `origin/codex/jiaoyi` 在该提交处一致；但以下 3 个文件有**未提交的本地修复**：
  - `config.example.yaml`
  - `polybtc/config.py`
  - `polybtc/clients.py`
- 未提交修复将飞鸟代理默认端口由 `23479` 改为当前可用的 `23274`，并将经代理请求的单次尝试限制为 1.5 秒；代理卡住时会快速改走直连，不再占满阈值请求的完整超时。
- 最近测试结果：`56 passed`。
- Dashboard 当前已停止；地址固定为 `http://127.0.0.1:8765/`，WebSocket 为 `ws://127.0.0.1:8766/ws`。

> 不要在未确认前丢弃上述 3 个本地改动；它们尚未提交或推送。

## 代理与联网

飞鸟代理重启后端口会变化。最近检测到的主混合代理端口为：

```text
http://127.0.0.1:23274
```

当前 Git 本地代理仍是旧端口 `http://127.0.0.1:23479`。下一次需要 `fetch`、`pull` 或 `push` 前，应先确认飞鸟端口；若仍为 `23274`，执行：

```powershell
git config --local http.proxy http://127.0.0.1:23274
git config --local https.proxy http://127.0.0.1:23274
```

项目网络请求优先走配置中的 `sources.proxy_url`，失败后回退直连。Gamma 阈值接口在代理路径偶尔会发生 TLS 握手慢，当前代码通过 1.5 秒代理尝试上限避免阻塞；直连可作为回退。

## 运行与检查

统一使用 bundled Python（系统 `python` 可能是 Windows Store 别名）：

```powershell
$python = 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
```

启动 Dashboard：

```powershell
Start-Process -FilePath $python `
  -ArgumentList '-m polybtc dashboard --host 127.0.0.1 --port 8765 --ws-port 8766' `
  -WorkingDirectory 'D:\Users\Administrator\Documents\btc5fenzhong' `
  -WindowStyle Hidden
```

停止 Dashboard：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*polybtc dashboard*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

运行测试和连通性检查：

```powershell
& $python -m pytest -q
& $python -m polybtc check --config config.example.yaml
```

常用 Git 检查：

```powershell
git status --short --branch
git log --oneline --decorate -10
git fetch origin
git status --short --branch
```

## 当前策略参数与下单规则

配置位于 `config.example.yaml` 和 `polybtc/config.py`。关键参数：

| 项目 | 当前值 |
| --- | ---: |
| 入场偏离阈值 | `15 USD` |
| 反向止损阈值 | `15 USD` |
| 最大买入价 | `0.75` |
| 止盈价差 | `0.10` |
| 入场时间窗 | 距到期 `30–240 秒` |
| 单笔模拟金额 | `10 USD` |
| 单市场累计金额 | `30 USD` |
| 最低滑点后理论利润 | `0.04` |
| 最大行情/盘口数据年龄 | `1000 ms` |
| 最大持仓时间 | `90 秒` |
| 到期前强制退出 | `5 秒` |

计算公式：

```text
修正后偏离 = Binance BTC 实时价 - Polymarket 本期阈值 + 动态修正
动态修正 = Polymarket BTC - Binance BTC
```

入场必须同时满足所有条件：

| 方向 | 信号 | 价格条件 |
| --- | --- | --- |
| 买入 UP / YES | 修正后偏离 `> +15` | UP 最优卖价 `< 0.75` |
| 买入 DOWN / NO | 修正后偏离 `< -15` | DOWN 最优卖价 `< 0.75` |

补充约束：只在 30–240 秒窗口内交易；阈值、行情、UP/Down 盘口均需有效且新鲜；不得已有持仓；盘口数量需足够；滑点后仍须保留 0.04 的理论利润空间。边界值不交易（偏离正负 15、卖价 0.75 均拒绝）。

退出规则：

| 持仓 | 优势衰减 | 反向止损 |
| --- | --- | --- |
| UP | 修正后偏离 `<= +15` | 修正后偏离 `<= -15` |
| DOWN | 修正后偏离 `>= -15` | 修正后偏离 `>= +15` |

此外，当对应最优买价 `>= 入场均价 + 0.10` 时止盈；持仓满 90 秒或到期前 5 秒也会退出；到期仍未退出则按阈值与最终 tick 模拟结算。

## 数据、阈值与盘口实现

- 阈值优先来自 Polymarket 事件/页面数据：当前期 `openPrice`，不可用时使用上一期 `closePrice`，再不可用才使用开盘后的首个 Binance tick 兜底；没有阈值则拒绝入场。
- Gamma `eventMetadata.priceToBeat` 可能为空，代码会回退到页面解析。
- 市场刷新间隔为 `0.5 秒`；盘口 WebSocket 等待/兜底检查为 `200 ms`。
- CLOB WebSocket 优先接收 `best_bid_ask`；只有盘口缺失或超过 `500 ms` 没有有效更新时才 REST 兜底。
- 盘口保留源时间戳用于排序，另记录本地 `received_at` 用于新鲜度判断，避免远端时间戳和本地时钟差导致误判。
- Binance、Gamma、CLOB 的 HTTP 与 WebSocket 均采用“代理优先、直连回退”。
- Dashboard 前端的盘口为紧凑样式：买入/卖出标签切换，分别展示 UP / DOWN 的可成交价格；仅展示，不提供手动交易按钮。

## 数据清理与拒绝信息

- `data_retention_hours = 24`：保留最近 24 小时的运行数据。
- `data_cleanup_interval_seconds = 300`：每 5 分钟自动清理一次过期运行目录。
- 清理只处理带运行标记的历史目录，不会删除当前运行目录。
- 前端最近拒绝记录最多保留 `500` 条；总拒绝数单独累计；同原因拒绝按 1 秒限频，陈旧时间戳的预期乱序拒绝会静默丢弃，避免页面快速刷屏和日志膨胀。

## 核心文件

- `polybtc/config.py`：配置模型和默认值。
- `polybtc/clients.py`：Binance、Polymarket Gamma/CLOB/RTDS、页面阈值解析及网络回退。
- `polybtc/market.py`：5 分钟市场识别与时间窗处理。
- `polybtc/strategy.py`：入场、止盈、止损、到期退出。
- `polybtc/engine.py`：paper 仓位、成交、拒绝与状态机。
- `polybtc/runner.py`：实时任务循环、行情与盘口更新。
- `polybtc/dashboard.py`：本地 HTTP/WebSocket 服务和配置接口。
- `web/index.html`：Dashboard 前端。
- `tests/`：市场、盘口、阈值、策略、回放和 runner 测试。

## 接手建议

1. 先确认飞鸟当前端口，再做任何远端 Git 操作。
2. 检查并决定是否提交当前 3 个代理修复文件；提交前运行 `& $python -m pytest -q`。
3. 启动 Dashboard 后确认阈值来源、Binance tick 与 UP/DOWN 盘口的更新时间均正常，再观察至少一个完整 5 分钟窗口。
4. 本项目仍是 paper engine；若未来接入真实下单，必须单独设计订单执行、私钥隔离、撤单、订单状态同步和风控层，不能直接将现有模拟引擎改为实盘。
