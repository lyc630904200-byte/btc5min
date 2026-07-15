# polybtc 交接文档

## 项目目标

`polybtc` 是一个 Polymarket 5 分钟 BTC Up/Down 市场的 paper trading 研究工具。

它监听 Binance `BTCUSDT` 实时成交价和 Polymarket CLOB 盘口，在外部 BTC 价格相对 Polymarket 本期阀值出现偏离、且对应方向盘口价格足够便宜时，模拟买入 UP/YES 或 DOWN/NO，并根据盘口止盈、偏离回落、反向突破、持仓时长或临近到期模拟退出。

当前版本只做模拟交易，不接私钥，不真实下单。

## 当前状态

- 当前工作目录：`D:\Users\Administrator\Documents\btc5fenzhong`
- 当前分支：`codex/jiaoyi`
- 当前提交：`064aa98 价格偏离改进`
- 本地与远端：`codex/jiaoyi...origin/codex/jiaoyi`，已同步
- Dashboard 正在运行：`http://127.0.0.1:8765/`
- 当前后台进程 PID：`11976`
- 最近测试结果：`48 passed`

## Git 与代理

本仓库已配置 Git 本地代理，后续 `fetch` / `pull` / `push` 默认走代理：

```text
http.proxy  = http://127.0.0.1:19617
https.proxy = http://127.0.0.1:19617
```

检查命令：

```powershell
git config --local --get-regexp "^(http|https)\.proxy$"
```

当前远端：

```text
origin https://github.com/lyc630904200-byte/btc5min.git
```

常用 Git 命令：

```powershell
git status --branch --short
git log --oneline --decorate -10
git push origin codex/jiaoyi
```

## 运行方式

建议使用 Codex bundled Python：

```powershell
$python = 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
```

启动 dashboard：

```powershell
& $python -m polybtc dashboard --host 127.0.0.1 --port 8765 --ws-port 8766
```

停止 dashboard：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*polybtc dashboard*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

运行测试：

```powershell
& $python -m pytest
```

连通性检查：

```powershell
& $python -m polybtc check --config config.example.yaml
```

运行无网页 paper trading：

```powershell
& $python -m polybtc run --config config.example.yaml
```

回放历史事件：

```powershell
& $python -m polybtc replay --input data\<run>\events.jsonl --config config.example.yaml
```

## 核心文件

- `polybtc/config.py`：配置模型，包含数据源、策略和风控默认值。
- `polybtc/clients.py`：Binance、Polymarket Gamma/CLOB、Polymarket RTDS、Polymarket 页面数据解析。
- `polybtc/market.py`：Polymarket BTC 5 分钟市场识别和时间窗口解析。
- `polybtc/strategy.py`：入场、出场、结算规则。
- `polybtc/engine.py`：paper trading 状态机，维护市场、tick、盘口、持仓、成交和拒绝原因。
- `polybtc/runner.py`：实时任务循环，处理市场刷新、BTC tick、CLOB 盘口、日志和推送。
- `polybtc/dashboard.py`：本地 HTTP/WebSocket 服务和配置接口。
- `web/index.html`：实时 dashboard 前端。
- `tests/`：市场解析、盘口、策略、runner、回放等测试。

## 数据源与阀值

当前 Polymarket 5 分钟 BTC Up/Down 市场的胜负逻辑是“结束价是否高于/低于本期开始价”。Gamma 的 `groupItemThreshold` 当前不能直接当 BTC 阀值使用。

当前阀值逻辑：

1. 优先解析 Polymarket 事件页面数据。
2. 如果页面有当前 slug 的 `openPrice`，使用 `polymarket_page_open_price`。
3. 否则使用上一期 `closePrice` 作为当前期阀值，来源 `polymarket_page_previous_close`。
4. 如果 Polymarket 页面阀值暂不可用，使用开盘后第一条 Binance tick 兜底，来源 `binance_first_tick_after_start`。
5. 若仍无阀值，策略不进场。

行情展示：

- 策略基础实时价仍使用 Binance `BTCUSDT` tick。
- Dashboard 额外订阅 Polymarket RTDS `wss://ws-live-data.polymarket.com` 的 `btc/usd`，作为页面里的 Polymarket BTC 实时价展示。
- 偏离修正动态等于 `Polymarket BTC - Binance BTC`，不再手动输入。

速度优化状态：

- `market_refresh_seconds = 0.5`
- `poly_book_poll_ms = 200`，用于 WebSocket 等待与 REST 兜底状态检查，不再每 200ms 强制全量拉盘。
- 启动和市场切换时会先尝试获取 Polymarket 页面阀值，再推送市场状态。
- Polymarket 页面现在只请求一次，同时解析 `openPrice` 和历史结果，避免重复请求拖慢阀值获取。
- Dashboard 推送给前端的盘口只保留 YES/NO 买入价和卖出价，避免全量深度数组拖慢渲染。
- CLOB WebSocket 订阅已启用官方 `custom_feature_enabled`，优先接收 `best_bid_ask` 顶级盘口推送。
- `best_bid_ask` 可直接建立本地盘口；时间戳相同或更旧的 book、price change、best bid/ask 更新都会丢弃，避免慢响应覆盖新价格。
- REST `/book` 只在任一方向盘口缺失或超过 2 秒没有有效更新时兜底；WebSocket 正常时不会持续轮询抢占更新。
- 高频盘口日志只写 token、市场、时间和 best bid/ask，不再写全量深度和原始 payload，降低文件写入造成的事件积压。

## 当前策略规则

核心价格偏离：

```text
原始偏离 = BTC实时价 - Polymarket阀值
偏离修正 = Polymarket BTC - Binance BTC
修正后偏离 = 原始偏离 + 偏离修正
```

当前配置：

```text
min_entry_edge_usd = 10
max_buy_price = 0.75
stop_edge_usd = 15
```

如果 Polymarket RTDS 暂时还没给出 BTC 价，程序会临时使用配置里的 `edge_correction_usd` 作为兜底值；正常运行后以 `Polymarket BTC - Binance BTC` 为准。

入场规则是严格条件：

```text
修正后偏离 > 10 且 UP best ask < 0.75  -> 买 UP / YES
修正后偏离 < -10 且 DOWN best ask < 0.75 -> 买 DOWN / NO
```

等于边界不买：

```text
修正后偏离 = 10 不买
修正后偏离 = -10 不买
best ask = 0.75 不买
```

其它入场约束：

- 当前市场必须已开始且未接近结束。
- 距离到期必须大于 `min_seconds_to_entry = 12` 秒。
- BTC tick、UP 盘口、DOWN 盘口都不能超过 `max_data_age_ms = 1000` ms。
- 没有未平 open position。
- 单笔模拟买入金额 `max_order_usd = 10`。
- 单市场累计投入不超过 `max_market_usd = 30`。
- 盘口深度必须足够，成交数量不能低于市场最小下单量。
- 滑点后至少保留 `min_profit_after_slippage = 0.04` 的理论空间。

## 当前出场规则

出场规则已修正，避免“买 NO 后立刻卖”的冲突。

NO：

```text
买 NO：修正后偏离 < -10
NO 优势衰减卖出：修正后偏离 >= -10
NO 反向止损：修正后偏离 >= 15
```

UP：

```text
买 UP：修正后偏离 > 10
UP 优势衰减卖出：修正后偏离 <= 10
UP 反向止损：修正后偏离 <= -15
```

其它退出：

- 止盈：`best bid >= entry_price + take_profit_ticks`，当前 `take_profit_ticks = 0.10`
- 最大持仓时间：`max_hold_seconds = 90`
- 临近到期强制退出：`force_exit_seconds = 5`
- 到期未退出则按当前 tick 和阀值模拟结算。

## 默认配置摘录

见 `config.example.yaml`。

```yaml
sources:
  poly_book_poll_ms: 200 # WebSocket 等待与 REST 兜底检查间隔
  market_refresh_seconds: 0.5
  max_start_price_lag_ms: 2000

strategy:
  min_entry_edge_usd: 10
  stop_edge_usd: 15
  edge_correction_usd: -47.75
  max_buy_price: 0.75
  take_profit_ticks: 0.10
  min_profit_after_slippage: 0.04
  min_seconds_to_entry: 12
  force_exit_seconds: 5

risk:
  max_order_usd: 10
  max_market_usd: 30
  max_data_age_ms: 1000
  max_hold_seconds: 90
```

## Dashboard

地址：

```text
http://127.0.0.1:8765/
```

主要接口：

- `GET /api/state`：返回最新 snapshot。
- `GET /api/config`：返回兜底策略配置。
- `POST /api/config`：更新兜底 `edge_correction_usd`，正常运行时动态修正优先。
- `ws://127.0.0.1:8766/ws`：实时推送 snapshot。

前端显示：

- 当前市场、slug、窗口时间、阀值和阀值来源。
- Binance BTC 实时价、Polymarket BTC 实时价、动态偏离修正、修正后偏离、YES/NO 买入价和卖出价。
- 当前持仓、成交、拒绝原因、统计数据。
- 偏离修正是只读动态值，等于 `Polymarket BTC - Binance BTC`。

## 日志输出

每次运行生成目录：

```text
data/YYYYMMDDTHHMMSSZ/
```

常见文件：

- `events.jsonl`：完整事件流，可 replay。
- `markets.jsonl`：市场发现、切换和阀值记录。
- `ticks.jsonl`：BTC tick 和压缩后的盘口快照（仅 best bid/ask）。
- `fills.csv`：模拟成交。
- `positions.csv`：持仓生命周期。
- `latency.csv`：连接和错误记录。
- `summary.json`：运行汇总。

## 测试覆盖

当前测试包括：

- Polymarket 市场解析。
- Polymarket 页面 `openPrice` / `closePrice` 解析。
- 当前市场选择和阀值回填。
- CLOB 盘口解析、WebSocket 更新、`best_bid_ask` 首次建盘口、旧时间戳丢弃、top bid/ask 前端压缩、买卖深度模拟。
- REST 盘口仅在缺失或超过 2 秒未更新时触发兜底。
- 动态价格偏离修正。
- 严格入场边界：偏离等于 10 不买，ask 等于 0.75 不买。
- NO 持仓在偏离仍小于 -10 时不会瞬间卖出。
- 偏离回到入场阀值时按 `edge_faded` 退出。
- 止盈、动态阀值捕获、stale book/tick 拒绝、replay。

最近结果：

```text
48 passed
```

## 已知注意点

- Polymarket 结算源是 Chainlink BTC/USD；当前程序使用 Polymarket 页面数据作为阀值来源，Binance tick 做基础实时偏离，Polymarket RTDS BTC 价用于动态偏离修正和 dashboard 展示。
- Binance 价格和 Polymarket/Chainlink 结算源可能天然有价差，所以修正值实时取 `Polymarket BTC - Binance BTC`。
- 盘口优先使用官方 CLOB WebSocket 的 `best_bid_ask` 推送；REST 仅在盘口缺失或 2 秒未更新时兜底。前端和高频日志都只保留 top bid/ask，避免全量深度数据造成卡顿。
- WebSocket 或 REST 本身发生网络超时仍可能导致短暂滞后；可通过对应运行目录的 `latency.csv` 判断是链路异常还是前端显示问题。
- 当前是 paper engine，不能直接复用为真实下单逻辑。真实下单需要私钥隔离、限价单、撤单、订单状态同步、异常风控和小额灰度。
- `config.example.yaml` 里有固定 `market_slug` 示例；如果要自动跟随当前市场，可能需要移除或更新这个字段，取决于 CLI 加载逻辑。

## 建议下一步

1. 连续运行 30-60 分钟，收集多个完整 5 分钟窗口。
2. 用 `replay` 验证信号、成交和退出是否 deterministic。
3. 增加报表脚本，统计阀值获取延迟、盘口更新延迟、入场后最大有利/不利偏移、模拟 PnL 分布。
4. 如需更贴近结算源，研究接入 Chainlink Data Streams 或更官方的结算价格数据源。
5. 如果准备真实下单，先单独设计交易执行层，不要直接把 paper engine 改成实盘。
