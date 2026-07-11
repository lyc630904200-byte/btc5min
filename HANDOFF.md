# polybtc 交接文档

## 项目目标

`polybtc` 是一个 Polymarket 5 分钟 BTC Up/Down 市场的 paper trading 研究工具。它监听 Binance `BTCUSDT` 实时成交价和 Polymarket CLOB 盘口，在外部价格相对市场起始价出现明显偏离、而 Polymarket 盘口尚未完全修正时模拟买入，并优先在盘口修正后提前卖出。

当前版本只做模拟交易，不接私钥、不真实下单。目标是验证信号是否真实存在、盘口修正速度、可成交深度、滑点后收益和提前卖出效果。

## 当前状态

- Python 包已实现：CLI、配置、市场发现、Binance/Polymarket 客户端、策略、paper engine、日志、回放、实时 dashboard。
- 本地实时面板已启动：`http://127.0.0.1:8765`
- 当前后台进程：`PID 44120`
- 启动命令：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m polybtc dashboard --config config.example.yaml --port 8765 --ws-port 8766
```

如果需要停止当前面板：

```powershell
Get-Process -Id 44120 | Stop-Process
```

## 运行方式

本机 PATH 里的 `python` 目前不可用，建议使用 Codex bundled Python：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' --version
```

连通性检查：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m polybtc check --config config.example.yaml
```

仅运行 paper trading，无网页：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m polybtc run --config config.example.yaml
```

运行实时 dashboard：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m polybtc dashboard --config config.example.yaml --port 8765 --ws-port 8766
```

回放历史事件：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m polybtc replay --input data\<run>\events.jsonl --config config.example.yaml
```

运行测试：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q
```

最近验证结果：`19 passed in 0.17s`。

## 核心文件

- `polybtc/cli.py`：Typer CLI 入口，命令包括 `check`、`run`、`replay`、`dashboard`。
- `polybtc/config.py`：YAML 配置模型，默认数据源、策略参数、风险参数。
- `polybtc/clients.py`：Binance REST/WebSocket、Polymarket Gamma/CLOB 客户端、Polymarket 页面 past-results 解析。
- `polybtc/market.py`：Polymarket 5 分钟 BTC 市场识别、起止时间、动态阈值解析。
- `polybtc/strategy.py`：进场、退出、结算逻辑。
- `polybtc/engine.py`：paper trading 状态机，维护市场、tick、盘口、持仓、信号、成交和退出事件。
- `polybtc/runner.py`：实时任务循环，处理市场刷新、Binance tick、CLOB 盘口轮询和日志落盘。
- `polybtc/journal.py`：输出 `events.jsonl`、`markets.jsonl`、`ticks.jsonl`、`fills.csv`、`positions.csv`、`latency.csv`、`summary.json`。
- `polybtc/replay.py`：从 `events.jsonl` 确定性重放。
- `polybtc/dashboard.py`：本地 HTTP 服务、`/api/state`、WebSocket 实时推送。
- `web/index.html`：实时 dashboard 前端。
- `tests/`：市场解析、策略、盘口模拟、回放测试。

## 策略逻辑

市场规则：Polymarket 5 分钟 BTC Up/Down 现在不是静态 `above $xxx` 题型，而是：结束价是否大于等于该窗口开始价。Gamma 返回的 `groupItemThreshold` 为 `"0"`，所以不能直接当作阈值。

当前实现：

- `market.py` 从 `eventStartTime` 解析真实 5 分钟窗口开始时间。
- 如果题面描述包含 beginning/start price 逻辑，则标记 `threshold_source = "dynamic_start_price"`。
- `clients.py` 会从 Polymarket 具体事件页（如 `/event/btc-updown-5m-...`）解析前端 hydration 里的 `past-results`，提取历史 `openPrice` / `closePrice`。
- `runner.py` 优先使用“上一期 `closePrice` == 当前期阈值”的规则回填当前市场阈值，来源标记为 `polymarket_page_previous_close`。
- 如果页面数据暂时未更新，`engine.py` 仍保留 Binance 开盘后首 tick 作为兜底阈值，来源标记为 `binance_first_tick_after_start`。
- 页面阈值后到时可以覆盖 Binance 兜底阈值。
- 捕获窗口由 `sources.max_start_price_lag_ms` 控制，默认 `2000` ms，仅用于 Binance 兜底。
- 当前市场一旦有阈值且未到期，`runner.py` 会锁定该市场直到结束，不会被 Gamma 刷新提前切到下一期。
- 如果程序中途启动，会先尝试用 Polymarket 页面 previous close 回填当前市场；只有页面阈值和 Binance 兜底都不可用时，才继续等待或选择下一期。

进场逻辑：

- `edge = Binance BTC - threshold_price`
- `edge >= min_entry_edge_usd` 时尝试买 `UP/YES`
- `edge <= -min_entry_edge_usd` 时尝试买 `DOWN/NO`
- ask 必须不高于 `max_buy_price`
- 模拟买入必须满足盘口深度、最小订单量、滑点后收益约束
- 已有 open position 时不重复开仓

退出逻辑：

- 止盈：best bid >= entry price + `take_profit_ticks`
- 优势回落：edge 回到 `stop_edge_usd` 范围内
- 反向突破：价格跨过阈值并反向达到 stop buffer
- 最大持仓时间：`risk.max_hold_seconds`
- 临近到期：`strategy.force_exit_seconds`
- 到期未退出则模拟结算

阈值优先级：

1. `polymarket_page_previous_close`：从 Polymarket 页面 `past-results` 中取上一期 `closePrice`，作为当前期阈值。
2. `binance_first_tick_after_start`：页面结果暂不可用时，用开盘后第一条 Binance tick 兜底。
3. `dynamic_start_price`：尚未捕获阈值，策略不进场。

注意：`polymarket_page_previous_close` 来自 Polymarket 前端页面的 hydration 数据，不是 Gamma/CLOB 正式市场字段；稳定性优于纯网页人工读取，但仍不等同于 Chainlink Data Streams 官方接口。

## 默认配置

见 `config.example.yaml`。关键参数：

```yaml
sources:
  poly_book_poll_ms: 500
  market_refresh_seconds: 20
  max_start_price_lag_ms: 2000

strategy:
  min_entry_edge_usd: 50
  stop_edge_usd: 15
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

实时面板地址：

```text
http://127.0.0.1:8765
```

后端接口：

- `GET /api/state`：返回最新 snapshot，包含 market、tick、books、open_position、summary、events、ws_url。
- `ws://127.0.0.1:8766/ws`：实时推送 snapshot。

前端显示：

- 当前市场窗口、slug、动态阈值和阈值来源
- Binance BTC 最新价、价格偏离、UP/DOWN 卖一
- UP/DOWN order book
- 当前决策状态、最近拒绝原因
- 持仓、浮动 PnL、累计统计
- 最近事件时间线

前端会在 WebSocket 断线后自动重连。`file:///D:/Users/Administrator/Documents/btc5fenzhong/web/index.html` 只是静态打开方式，不用于稳定实时运行。

## 日志输出

每次运行都会生成目录：

```text
data/YYYYMMDDTHHMMSSZ/
```

常见文件：

- `events.jsonl`：完整事件流，可用于 replay
- `markets.jsonl`：市场发现、市场切换、动态阈值捕获
- `ticks.jsonl`：Binance tick 和盘口快照
- `fills.csv`：模拟成交
- `positions.csv`：持仓生命周期
- `latency.csv`：连接/错误记录
- `summary.json`：运行汇总

## 测试覆盖

当前测试点：

- 解析静态阈值市场
- 解析动态起始价市场
- 解析 Polymarket 页面 `past-results` 中的 `openPrice` / `closePrice`
- 跳过已错过动态阈值窗口的市场
- 用上一期 `closePrice` 回填当前期阈值
- 已捕获阈值的当前市场锁定到到期，不提前切换下一期
- 买入盘口深度和均价模拟
- 卖出部分深度模拟
- UP 进场信号
- ask 过贵拒绝
- 未开始市场拒绝
- 止盈退出
- 动态阈值捕获窗口
- stale 动态阈值拒绝
- `events.jsonl` 确定性回放

## 已知注意点

- Polymarket 结算源是 Chainlink BTC/USD；当前优先使用 Polymarket 页面 previous close 作为阈值，Binance 首 tick 仅作为兜底。真实研究结论仍应记录页面阈值、Binance 价格和 Chainlink 结算源之间的差异，不能把它当作无风险套利。
- 本机时钟相对 Binance server time 曾显示约 2.8 秒偏移；动态阈值日志记录的是交易所 tick 时间，减少本机时钟偏移影响。
- Polymarket 页面 `past-results` 可能比市场开盘滞后几分钟更新；程序会周期性重试，阈值到达后回填并锁定市场。
- Gamma API 的 `groupItemThreshold` 当前为 `"0"`，不能作为 BTC 开盘阈值。
- `market_refresh_seconds=20` 对发现下一期市场够用；若要更快获取页面阈值更新，可把该值降到 2-5 秒。
- `ticks.jsonl` 目前也写盘口快照，因此文件可能增长较快。
- 当前所有文件在 git 状态中仍是 untracked，尚未提交。

## 建议下一步

1. 连续跑 30-60 分钟，收集多个完整 5 分钟窗口。
2. 用 `replay` 重放事件，确认信号和 PnL deterministic。
3. 增加报告脚本，统计信号持续时间、盘口修正时间、滑点后 PnL 分布。
4. 如需更贴近结算，接入 Chainlink Data Streams，替代页面 hydration 解析和 Binance 兜底。
5. 若准备真实下单，必须另做私钥隔离、限价单、撤单、订单状态同步、异常风控和小额灰度，不要直接复用 paper engine 下单。
