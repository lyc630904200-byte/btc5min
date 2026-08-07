# BTC V8 盘口追赶逻辑复刻规范

本文档记录 2026-08-04 当前工作区实际运行的 BTC 5 分钟 Polymarket 盘口追赶逻辑。目标是让另一套程序可以按相同的数据源、时间语义、参数和买卖生命周期复刻，而不是描述旧版 V8 Logistic 模型。

## 1. 当前边界

- 当前模式是本地模拟交易，固定使用实时公开行情，不发送真实订单。
- 当前 V8 Logistic 残差模型在盘口追赶模式下被绕过：`model_probability == formula_probability`。
- 盘口追赶模式不保存每秒训练快照，场次结算后不更新 V8 模型权重。
- 追赶方向只由三所现货相对 Chainlink 的 1 秒领先信号决定。
- Polymarket 完整盘口只负责判断是否可成交、成交均价、手续费、滑点、退出价值和盈亏。
- Binance 永续仍在采集，但当前不影响追赶方向、买入或卖出。
- 现有真实交易适配器没有接入 V8 完整买卖生命周期，不能通过打开开关直接变成实盘。

## 2. 模块对应关系

| 模块 | 职责 |
|---|---|
| `polybtc/config.py` | 数据接口、V8、风控和通用策略配置 |
| `polybtc/signal_sources.py` | Binance、Coinbase、Kraken 和 Binance 永续采集器 |
| `polybtc/clients.py` | Polymarket Gamma、CLOB、RTDS、市场发现和开盘价验证 |
| `polybtc/btc_v8.py` | 领先信号、概率、买卖条件、生命周期和 SQLite 持久化 |
| `polybtc/orderbook.py` | 完整盘口买入/卖出模拟及手续费 |
| `polybtc/runner.py` | 采集器编排、有界队列、批处理和 250ms 评估 |
| `polybtc/dashboard.py` | 本地状态和运行参数 API |
| `web/index.html` | Dashboard 控制和展示 |
| `tests/test_btc_v8.py` | 盘口追赶核心测试 |

最小复刻应保留 `config.py`、`signal_sources.py`、`clients.py`、`btc_v8.py`、`orderbook.py` 中对应逻辑，不能只复制一个信号函数。

## 3. 总体数据流

```text
Binance/Coinbase/Kraken 成交与盘口
                    |
                    v
          来源校验、时效、异常剔除
                    |
                    v
     三所现货 1s 收益 - Chainlink 1s 收益
                    |
                    v
             中位领先值 / sigma
                    |
                    v
        推算 Chainlink 价格和目标概率
                    |
                    v
      Polymarket UP/DOWN 完整盘口检查
                    |
                    v
       固定 $5 买入 -> 最多持有 5 秒
                    |
                    v
  追上盈利 / 5秒退出 / 反向 / 回撤止盈
```

所有窗口计算使用程序 `received_at`，不能使用决策时尚未接收的交易所时间数据。

## 4. 外部接口

### 4.1 Binance 现货

- 符号：`BTCUSDT`
- REST：`https://api.binance.com/api/v3/depth`
- REST 参数：`symbol=BTCUSDT&limit=100`
- WebSocket：`wss://stream.binance.com:9443/stream`
- 组合流：`btcusdt@aggTrade/btcusdt@depth@100ms`
- 本地盘口输出：前 20 档买卖盘

恢复要求：

1. 连接后先取 REST 深度快照。
2. 保存 `lastUpdateId`。
3. 按 `U/u` 顺序应用增量。
4. `U > lastUpdateId + 1` 视为 `sequence_gap`，盘口失效并重新拉取快照。
5. `aggTrade.m == true` 表示主动卖，否则表示主动买。

### 4.2 Coinbase 现货

- 符号：`BTC-USD`
- WebSocket：`wss://ws-feed.exchange.coinbase.com`
- 频道：`matches`、`heartbeat`、`level2_batch`
- REST 补漏：`https://api.exchange.coinbase.com/products/BTC-USD/trades`
- REST 参数：`limit=100`，分页使用响应头 `cb-after`
- 本地盘口输出：前 20 档买卖盘

恢复要求：

1. `snapshot` 初始化盘口，`l2update` 更新盘口。
2. 使用 `trade_id` 检测成交缺口。
3. 缺口不超过 2000 个 ID 时，最多分页 20 次通过 REST 补齐。
4. Coinbase `match.side` 是 maker 方向，程序会反转得到 taker 方向。

### 4.3 Kraken 现货

- 符号：`BTC/USD`
- WebSocket：`wss://ws.kraken.com/v2`
- 盘口订阅：`book`，`depth=25`，`snapshot=true`
- 成交订阅：`trade`，`snapshot=false`
- 本地决策使用前 20 档，CRC32 校验使用前 25 档

恢复要求：

1. 按消息顺序更新 Decimal 价格和数量。
2. 每次盘口更新后校验 Kraken checksum。
3. checksum 不一致时标记 `checksum_mismatch`，清空本地盘口并重新订阅快照。

### 4.4 Chainlink 结算价格流

- WebSocket：`wss://ws-live-data.polymarket.com`
- Topic：`crypto_prices_chainlink`
- Filter：`{"symbol":"btc/usd"}`
- 当前时效阈值：10 秒
- 用途：开盘价、当前价、1 秒收益、10/60 秒波动率和结算公式概率

订阅负载：

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices_chainlink",
      "type": "*",
      "filters": "{\"symbol\":\"btc/usd\"}"
    }
  ]
}
```

### 4.5 Polymarket 市场和盘口

- Gamma：`https://gamma-api.polymarket.com`
- CLOB REST：`https://clob.polymarket.com`
- CLOB WebSocket：`wss://ws-subscriptions-clob.polymarket.com/ws/market`
- 市场页面：`https://polymarket.com/event/{market_slug}`

使用接口：

| 接口 | 用途 |
|---|---|
| `GET /markets?slug=...` | 发现当前 5 分钟市场及结算结果 |
| `GET /events?slug=...` | 读取 `priceToBeat` |
| `GET /book?token_id=...` | 初始化/恢复完整盘口 |
| `GET /price?token_id=...&side=BUY` | 连接探测 |
| CLOB market WebSocket | `book`、`price_change`、`best_bid_ask`、`last_trade_price` |

CLOB 订阅负载：

```json
{
  "type": "market",
  "assets_ids": ["UP_TOKEN_ID", "DOWN_TOKEN_ID"],
  "custom_feature_enabled": true
}
```

开盘价只有经过页面/Gamma/RTDS 交叉验证后才允许追赶。未验证时状态为 `chainlink_open_unverified`。

### 4.6 Binance 永续

当前仍采集，但盘口追赶不使用这些数据做决策。

- WebSocket：`wss://fstream.binance.com/stream`
- Streams：`btcusdt@aggTrade`、`btcusdt@depth20@100ms`、`btcusdt@markPrice@1s`、`btcusdt@forceOrder`
- REST：`/fapi/v1/openInterest` 每 5 秒
- REST 回退：`/fapi/v1/aggTrades` 每 0.5 秒
- REST 回退：`/fapi/v1/premiumIndex` 每 1 秒

复制纯追赶程序时可以不启动该采集器。

## 5. 采集并发和代理

- 每个来源单独运行采集任务，单独重连。
- 每个来源使用 `asyncio.Queue(maxsize=512)`。
- 队列满时丢弃最旧事件，避免慢来源阻塞决策。
- 批处理循环每 50ms 排空各来源队列；没有事件时每 250ms 发送一次心跳评估。
- V8 引擎最终最多每 250ms 评估一次。
- WebSocket/HTTP 优先尝试 Windows 系统代理，失败后尝试显式代理和直连。
- 复制时应保留每个来源独立重连和有界队列，不能让一个交易所等待拖住其他来源。

## 6. 当前运行参数

以下是当前 Dashboard 中已生效的 V8 配置：

```json
{
  "enabled": true,
  "auto_decision_mode": false,
  "orderbook_chase_mode": true,
  "auto_emergency_loss_enabled": false,
  "quote_amount_usd": 5.0,
  "buy_edge_cents": 5.0,
  "sell_edge_cents": 2.0,
  "slippage_reserve_cents": 1.0,
  "entry_end_seconds": 285.0,
  "sell_end_seconds": 298.0,
  "buy_confirmation_seconds": 2.0,
  "buy_confirmation_updates": 2,
  "sell_confirmation_seconds": 0.1,
  "sell_confirmation_updates": 1,
  "reentry_cooldown_seconds": 3.0,
  "max_entries_per_market": 5,
  "max_loss_usd": 5.0,
  "chase_take_profit_arm_usd": 0.25,
  "chase_take_profit_drawdown_usd": 0.15,
  "chase_take_profit_drawdown_fraction": 0.35,
  "evaluation_interval_ms": 250,
  "snapshot_interval_seconds": 1,
  "spot_exchanges": ["binance", "coinbase", "kraken"],
  "min_fresh_spot_exchanges": 2,
  "spot_stale_seconds": 2.0,
  "chainlink_stale_seconds": 10.0,
  "raw_retention_hours": 24.0,
  "short_volatility_window_seconds": 10,
  "long_volatility_window_seconds": 60,
  "volatility_floor_bps": 0.5,
  "max_probability_correction_points": 10.0
}
```

通用配置中还实际使用：

```json
{
  "risk.max_data_age_ms": 1000,
  "strategy.taker_fee_rate": 0.07
}
```

### 6.1 追赶模式内部覆盖

配置界面中的部分字段不会直接用于追赶模式：

| 配置字段 | 当前显示值 | 追赶模式实际值/行为 |
|---|---:|---|
| `buy_edge_cents` | 5.0 | 固定覆盖为 1.0 美分 |
| `sell_edge_cents` | 2.0 | 不用于追赶退出 |
| `buy_confirmation_seconds` | 2.0 | 固定覆盖为 0.25 秒 |
| `buy_confirmation_updates` | 2 | 固定为 2 次独立更新 |
| `sell_confirmation_seconds` | 0.1 | 固定覆盖为 0.25 秒 |
| `sell_confirmation_updates` | 1 | 固定覆盖为 2 次独立更新 |
| `max_loss_usd` | 5.0 | 不作为持仓退出条件 |
| `auto_emergency_loss_enabled` | false | 追赶模式固定关闭紧急亏损退出 |
| `snapshot_interval_seconds` | 1 | 追赶模式不保存训练快照 |
| `max_probability_correction_points` | 10 | 追赶模式不启用残差模型 |

追赶模式代码常量：

```text
CHASE_MIN_SIGNAL_SIGMA          = 1.0
CHASE_MIN_PROBABILITY_MOVE      = 0.03
CHASE_MIN_NET_EDGE              = 0.01
CHASE_MIN_PROFIT_USD            = 0.02
CHASE_MAX_HOLD_SECONDS          = 5.0
CHASE_BUY_CONFIRMATION_SECONDS  = 0.25
CHASE_BUY_CONFIRMATION_UPDATES  = 2
CHASE_SELL_CONFIRMATION_SECONDS = 0.25
CHASE_SELL_CONFIRMATION_UPDATES = 2
```

## 7. 时间和来源健康判定

一个现货来源只有同时满足下列条件才进入共识：

1. 最近有效成交存在。
2. 最近盘口存在且 `book.valid == true`。
3. 来源健康状态有效。
4. `0 <= now - trade.received_at <= 2s`。
5. `0 <= now - book.received_at <= 2s`。

异常源剔除：

```text
median_mid = median(各新鲜来源盘口中间价)
abs(log(source_mid / median_mid)) > 0.005 -> price_outlier
```

被剔除来源不计入领先中位数和新鲜来源数量。至少需要 2 个新鲜现货来源。

## 8. Chainlink 公式概率

### 8.1 波动率

对相邻 Chainlink tick：

```text
instant_sigma_i = log(price_i / price_(i-1)) / sqrt(delta_seconds)
window_sigma = sqrt(mean(instant_sigma_i ^ 2))
sigma = max(sigma_10s, sigma_60s, 0.5 / 10000)
```

### 8.2 基础 UP 概率

```text
remaining = max(market_end - now, 0.001s)
log_return = log(chainlink_current / chainlink_open)
z = log_return / (sigma * sqrt(remaining))
formula_up = clamp(NormalCDF(z), 0.01, 0.99)
```

DOWN 概率为 `1 - formula_up`。

## 9. 1 秒领先信号

每个来源的窗口收益使用最后一个 `received_at <= now` 的价格，以及最后一个 `received_at <= now - 1s` 的价格：

```text
spot_return_i_1s = log(spot_current_i / spot_prior_i)
chainlink_return_1s = log(chainlink_current / chainlink_prior)
lead_i = spot_return_i_1s - chainlink_return_1s
lead_return = median(lead_i)
```

方向：

```text
lead_return > 0 -> UP
lead_return <= 0 -> DOWN
```

注意：同向来源数量按各现货来源自身 1 秒收益的正负统计，不按 `lead_i` 的正负统计。

信号强度和目标概率：

```text
signal_strength = abs(lead_return) / sigma
projected_return = clamp(lead_return, -3 * sigma, 3 * sigma)
projected_price = chainlink_current * exp(projected_return)
projected_z = log(projected_price / chainlink_open) / (sigma * sqrt(remaining))
target_up = clamp(NormalCDF(projected_z), 0.01, 0.99)
probability_move = abs(target_up - formula_up)
target_probability = target_up              # UP
target_probability = 1 - target_up          # DOWN
```

信号合格必须同时满足：

- 同方向现货来源数至少 2。
- `signal_strength >= 1.0`。
- `probability_move >= 0.03`。

## 10. 买入条件

按以下顺序检查，任一失败都不买：

1. V8 已启用且当前为 BTC 市场。
2. Chainlink 开盘价已验证且当前 tick 不超过 10 秒。
3. 当前无持仓。
4. `elapsed < 285s`。
5. 本场成功买入次数 `< 5`。
6. 距上次退出至少 3 秒。
7. 至少 2 个新鲜现货来源。
8. 追赶信号满足共识、1 sigma 和 3% 概率变化。
9. 对应 Polymarket Token/market ID 正确。
10. 对应盘口 `depth_trusted == true` 且接收时间不超过 1000ms。
11. 价格限制内的完整卖盘能买完 $5。
12. 买入份数不少于市场和盘口声明的最小份数。
13. 扣除双边手续费、1 美分滑点预留后，净优势至少 1 美分/份。
14. 当前完整买盘能够立即卖完全部份数。
15. 立即清仓预计亏损必须小于买入本金的 50%。
16. 同方向持续至少 0.25 秒，并出现至少 2 个独立更新键。

独立更新键由 Chainlink tick、UP/DOWN Polymarket 盘口接收时间和三所最新成交接收时间组成。

## 11. 买入价格、手续费和深度

手续费公式：

```text
fee_usd = quantity * fee_rate * price * (1 - price)
fee_rate = 0.07
```

追赶买入先估算目标概率处的退出手续费，再求允许的最高买价：

```text
estimated_exit_fee_per_share = 0.07 * target * (1 - target)

target
- estimated_exit_fee_per_share
- buy_price
- 0.07 * buy_price * (1 - buy_price)
- 0.01 slippage reserve
- 0.01 minimum edge
>= 0
```

解出的最高买价向下取整到市场 tick，最高不超过 0.99。只保留 `ask.price <= limit` 的卖盘，再逐档吃完固定 $5。

实际净优势再次按成交均价校验：

```text
edge_per_share =
    target_probability
    - avg_buy_price
    - buy_fee / quantity
    - estimated_exit_fee_per_share
    - 0.01

edge_per_share >= 0.01
```

模拟买入按每档 `take_quote / price` 计算份数；任一档手续费单独计算后累加。

## 12. 持仓和卖出

有持仓时只读取同方向 Polymarket 完整买盘。必须能够整仓卖完，否则继续持有，包括已经超过 5 秒的情况。

```text
net_value_per_share = avg_sell_price - sell_fee / quantity - 0.01
pnl = sell_quote - sell_fee - entry_quote - entry_fee
```

当前退出优先级：

1. **盘口已追上并盈利**
   - `pnl >= $0.02`
   - `net_value_per_share + 0.005 >= entry_target_probability`
2. **5 秒强制退出**
   - 持仓时间 `>= 5s`
3. **严格反向信号**
   - 当前追赶信号完整满足共识、1 sigma、3% 门槛
   - 当前方向与持仓方向相反
4. **回撤止盈**
   - 历史最高可执行利润 `>= $0.25`
   - 当前回撤 `>= max($0.15, peak_pnl * 35%)`

确认规则：

- `chase_timeout`：0 秒、1 次更新，立即执行。
- 追上盈利、反向和回撤止盈：0.25 秒、2 次独立更新。
- `signal_decayed` 只记录诊断，不触发卖出。
- `elapsed >= 298s` 后停止主动卖出，持仓等待官方结算。

官方结算：方向正确按每份 $1 支付，错误按 $0 支付；结算交易不再收取模拟退出费。

## 13. 场次生命周期

- 新市场创建独立 `V8Round`，并冻结当场 `settings`。
- Dashboard 修改 V8 参数后保存为 pending，只在下一 BTC 市场启用，当前场不变。
- 每场同时只允许一个方向持仓。
- 每次成功买入才增加 `entry_count`。
- 卖出后保存 `last_exit_at`，3 秒内禁止重入。
- 每场最多成功买入 5 次。
- 程序重启后从数据库恢复当前场次、开放持仓、入场次数和最后退出时间。
- 盘口追赶场次结算后只标记 `trained=true`，不更新模型。

## 14. SQLite 持久化

数据库：`data/btc-v8-ledger.sqlite3`

启用 `PRAGMA journal_mode=WAL`，主要表：

| 表 | 内容 |
|---|---|
| `btc_v8_rounds` | 市场、冻结设置、入场次数、结算结果 |
| `btc_v8_positions` | 开仓、峰值利润、退出和结算状态 |
| `btc_v8_trades` | BUY/SELL/SETTLE 交易明细 |
| `btc_v8_models` | 历史 V8 模型版本，追赶时不更新 |
| `btc_v8_snapshots` | 旧模型训练快照，追赶时不新增 |
| `btc_v8_raw_events` | 压缩的原始事件，保留 24 小时 |
| `btc_v8_controls` | 模型控制项 |

原始事件记录：`source`、`kind`、`symbol`、价格、数量、方向、序列号、交易所时间、接收时间、处理时间、有效状态和原始负载。

- 普通原始事件累计 100 条或 1 秒批量写入。
- 盘口原始事件按来源、符号和 250ms 时间桶合并。
- JSON 使用 zlib level 3 压缩。
- 每次新场开始清理超过 24 小时的原始事件。

## 15. 本地控制接口

Dashboard：`http://127.0.0.1:8765`

| 方法 | 地址 | 用途 |
|---|---|---|
| GET | `/api/state` | 当前市场、盘口、V8状态、候选、持仓和最近成交 |
| GET | `/api/config` | 当前配置、pending 配置和生效状态 |
| POST | `/api/config` | 保存下一 BTC 市场生效的参数 |
| WebSocket | `ws://127.0.0.1:8766/ws` | Dashboard 实时状态推送 |

V8 参数更新示例：

```http
POST /api/config
Content-Type: application/json

{
  "btc_v8": {
    "enabled": true,
    "orderbook_chase_mode": true,
    "quote_amount_usd": 5.0,
    "entry_end_seconds": 285.0,
    "sell_end_seconds": 298.0,
    "reentry_cooldown_seconds": 3.0,
    "max_entries_per_market": 5,
    "min_fresh_spot_exchanges": 2,
    "spot_stale_seconds": 2.0,
    "chainlink_stale_seconds": 10.0,
    "chase_take_profit_arm_usd": 0.25,
    "chase_take_profit_drawdown_usd": 0.15,
    "chase_take_profit_drawdown_fraction": 0.35
  }
}
```

运行参数持久化文件：`data/dashboard-settings.json`。

## 16. 常见拒绝原因

| 状态 | 含义 |
|---|---|
| `chainlink_open_unverified` | 开盘价尚未验证 |
| `chainlink_current_unavailable` | 没有当前 Chainlink tick |
| `chainlink_stale` | Chainlink 超时 |
| `insufficient_fresh_spot_exchanges` | 新鲜现货来源少于 2 |
| `chase_waiting_history` | 1 秒历史不足 |
| `chase_consensus_insufficient` | 同方向来源少于 2 |
| `chase_signal_weak` | 信号低于 1 sigma |
| `chase_probability_move_small` | 概率变化低于 3% |
| `book_missing` | Polymarket 盘口缺失 |
| `book_market_mismatch` | Token 或 market ID 不匹配 |
| `book_depth_untrusted` | 完整深度尚未可信 |
| `book_stale` | Polymarket 盘口超过 1000ms |
| `depth_below_limit` | 限价内不能买完 $5 |
| `quantity_below_market_minimum` | 成交份数低于市场最低数量 |
| `actual_edge_below_threshold` | 实际净优势低于 1 美分 |
| `auto_exit_depth_unavailable` | 当前买盘不能立即整仓退出 |
| `auto_liquidation_risk` | 立即清仓亏损达到本金 50% |
| `confirming_buy` | 等待 0.25 秒/2 次更新确认 |
| `entry_window_closed` | 已到 285 秒 |
| `market_entry_limit` | 本场已经买入 5 次 |
| `reentry_cooldown` | 卖出后尚未满 3 秒 |
| `sell_depth_unavailable` | 当前买盘不能整仓卖完 |
| `confirming_sell` | 等待卖出确认 |
| `holding_for_settlement` | 已到 298 秒，等待官方结算 |

## 17. 最小复刻伪代码

```python
every_250ms:
    require_verified_chainlink_open()
    require_fresh_chainlink(max_age=10.0)

    fresh = filter_spot_sources(
        exchanges=[binance, coinbase, kraken],
        trade_max_age=2.0,
        book_max_age=2.0,
        require_valid_book=True,
        midpoint_log_outlier=0.005,
    )

    sigma = max(chainlink_volatility(10), chainlink_volatility(60), 0.00005)
    formula_up = settlement_probability(chainlink_open, chainlink_current, sigma, remaining)

    lead = median(
        spot_log_return(source, 1) - chainlink_log_return(1)
        for source in fresh
    )
    chase = project_lead_to_probability(lead, sigma, remaining)

    if position:
        full_sell = walk_full_bid_book(position.quantity)
        apply_exit_priority(full_sell, chase)
        return

    require(elapsed < 285)
    require(entry_count < 5)
    require(reentry_cooldown_complete())
    require(chase.same_direction_sources >= 2)
    require(chase.signal_strength >= 1.0)
    require(chase.probability_move >= 0.03)

    limit = solve_dynamic_max_price(
        target=chase.target_probability,
        fee_rate=0.07,
        exit_fee=True,
        slippage_reserve=0.01,
        min_edge=0.01,
    )
    buy = walk_ask_book(quote=5.0, max_price=limit)
    require(buy.complete)
    require(minimum_quantity_met(buy.quantity))
    require(actual_edge(buy) >= 0.01)
    require(immediate_full_liquidation_available(buy.quantity))
    require(immediate_liquidation_pnl() > -2.50)
    require(persistent_for(0.25, independent_updates=2))
    open_paper_position(buy)
```

## 18. 复刻验收清单

- 三所现货在同一组录制事件上产生相同的 1 秒收益。
- 所有窗口严格使用 `received_at`，没有未来数据。
- 异常中间价来源被排除，单一来源不能主导方向。
- 少于 2 个新鲜来源时绝不买入。
- 1 sigma 和 3% 边界测试包含等号、略低于和略高于。
- UP/DOWN 投影概率与当前实现数值一致。
- 完整盘口逐档成交、手续费、滑点和最小份数一致。
- 同一个更新键不重复累计确认次数。
- 追上盈利优先于 5 秒退出分类。
- 5 秒时盘口深度不足不会虚构卖出，恢复后再卖。
- 反向信号必须重新满足完整严格门槛。
- 单纯信号衰减不会卖出。
- 回撤止盈使用 `max($0.15, peak * 35%)`。
- 285 秒停止买入，298 秒后持仓等待结算。
- 重启可恢复持仓、入场次数和冷却时间。
- 追赶场次不新增训练快照、不更新模型版本。
- 模拟路径不调用任何真实订单接口。

## 19. 接入真实 FOK 时的扩展点

以下不属于当前追赶程序，只是复制后接实盘时必须新增：

1. 开盘前完成账户连接、地区检查、余额和 allowance。
2. 保持 CLOB HTTP/TLS 和用户 WebSocket 长连接。
3. 使用最新盘口计算 $5 所需最差价格和策略最高价。
4. 动态签名并发送 FOK，不能把本地模拟成交直接当成真实成交。
5. 通过用户 WebSocket 和订单查询确认 MATCHED/REJECTED/UNKNOWN。
6. 对提交结果不确定的订单先 reconcile，禁止盲目重发。
7. 实现真实整仓 SELL FOK、部分/失败状态和重启恢复。
8. 保存模拟预期价格与真实成交价格，用于测量延迟和滑点偏差。

在完成这些扩展前，当前收益只能视为本地模拟结果。
