# Polymarket 5 分钟盘口实时刷新修复交接文档

更新时间：2026-08-15（Asia/Shanghai）

## 1. 本次目标与结果

目标页面：

- `https://polymarket.com/zh/event/btc-updown-5m-1786755600`

目标是让本项目的盘口接收与前端价格跳动采用和 Polymarket 官网相同的实时事件源与显示节奏，消除原先约 250ms 一次、最高约 4 FPS 的卡顿。

本次已经完成代码修改：

- 盘口使用 Polymarket CLOB Market WebSocket 逐事件消费。
- 后端增加轻量 `market_data` 快通道，不再让盘口价格等待 250ms 全量状态节流。
- 前端收到快通道消息后使用 `requestAnimationFrame` 合帧，最多等待一个浏览器绘制帧。
- 每个浏览器客户端使用独立、latest-only、有界发送槽；慢客户端不会再阻塞行情主循环。
- 后续状态更新改为增量 `state_data` 慢通道，不再反复发送约 4.37MB 的完整 Dashboard 快照。
- RTDS 增加官方要求的每 5 秒纯文本 `PING`。
- 支持当前 BTC 5 分钟市场使用的 60 秒 Chainlink TWAP，并继续兼容 30 秒市场。
- 支持 CLOB `tick_size_change` 实时更新。
- REST 盘口降级为低频安全网，不再把正常的短暂无事件误判为 WebSocket 故障。

## 2. 修复前的实测根因

### 2.1 上游 CLOB 并不慢

对活动 BTC 5 分钟市场进行约 12.3 秒采样：

- 解析到 6,655 个盘口快照，约 542 个/秒。
- 源时间到本机接收时间：P50 约 54.5ms，P90 约 133.6ms。
- 消息包括 `book`、`price_change`、`best_bid_ask` 和 `last_trade_price`。

结论：主要瓶颈不在 Polymarket 上游，而在本地 Dashboard 推送和浏览器处理链路。

### 2.2 原 Dashboard 把盘口限制到约 4 FPS

原 `DashboardHub.publish()` 将以下事件共用一个 250ms 领先沿节流槽：

- `tick`
- `polymarket_tick`
- `polymarket_twap_tick`
- `book`
- `pair_state`

250ms 内到达的新状态只更新 `latest`，没有尾随补发，因此多个资产、现货、TWAP 和盘口事件会互相抢槽。

### 2.3 网络发送反向阻塞行情消费

原实现会在行情消费回调中等待所有浏览器的 `client.send()`。单个慢客户端最多可阻塞约 1 秒，导致 runner 行情队列积压。

### 2.4 全量消息过大

旧运行实例采样 5 秒：

- 只收到 16 个 WebSocket 消息。
- 消息间隔 P50 约 315ms。
- 每个消息平均约 4.35MB，最大约 4.35MB。

主要体积来自各策略的历史数组，例如 `recent_attempts`、`recent_positions`、`prediction_history` 和 `recent_trades`。即使浏览器用 rAF 合并渲染，也仍需对每个 4MB 消息执行 `JSON.parse`。

## 3. 官方协议基线

### 3.1 CLOB Market WebSocket

官方地址：

- `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- 文档：`https://docs.polymarket.com/api-reference/wss/market`
- 实时数据总览：`https://docs.polymarket.com/market-data/realtime-data`

订阅格式：

```json
{
  "assets_ids": ["UP_TOKEN_ID", "DOWN_TOKEN_ID"],
  "type": "market",
  "custom_feature_enabled": true
}
```

必须处理：

- `book`：完整聚合盘口，收到后替换整本。
- `price_change`：当前格式是 `price_changes[]` 数组，必须遍历数组中的所有 token 更新；`size = "0"` 表示删除价位。
- `best_bid_ask`：最优买卖价变化的低延迟事件，需要 `custom_feature_enabled=true`。
- `last_trade_price`：成交打印，不应被同 token 的后续事件覆盖或乱序合并。
- `tick_size_change`：极端价格区域可能从 0.01 改成 0.001，必须立即更新。

连接建立后每 10 秒发送一次应用层纯文本 `PING`，不能只依赖 WebSocket protocol ping。

### 3.2 RTDS / Chainlink TWAP

官方地址：

- `wss://ws-live-data.polymarket.com`
- 文档：`https://docs.polymarket.com/market-data/chainlink-twap`

60 秒 TWAP 订阅示例：

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices_twap_sixty",
      "type": "update",
      "filters": "{\"symbol\":\"btc/usd\"}"
    }
  ]
}
```

RTDS raw client 每 5 秒发送纯文本 `PING`。RTDS 没有初始 snapshot 或重放，断线后必须重连并重新订阅。

目标页面对应的 `cryptoMarketConfig` 为：

```json
{
  "id": "btc-5m-twap-60",
  "twapEnabled": true,
  "twapLookbackSeconds": 60
}
```

代码必须从当前市场的 `cryptoMarketConfig.twapLookbackSeconds` 动态选择 30/60 秒流，不能硬编码 30 秒。

## 4. 具体代码修改

### 4.1 `polybtc/dashboard.py`

新增两种后续推送协议。

#### `market_data` 快通道

用途：价格、TWAP 和盘口 top-of-book。

主要字段：

```json
{
  "type": "market_data",
  "sequence": 123,
  "asset": "BTC",
  "server_enqueued_at": "...",
  "data": {
    "market": {},
    "tick": {},
    "polymarket_tick": {},
    "polymarket_twap_tick": {},
    "settlement_tick": {},
    "books": {}
  }
}
```

行为：

- `book/tick/polymarket_tick/polymarket_twap_tick` 每次发布都会创建快通道消息。
- 不受原 250ms 全量状态节流限制。
- 单个客户端只保留尚未发送的最新一条行情，避免堆积旧价。

#### `state_data` 增量慢通道

用途：策略状态、历史、配置和普通 Dashboard 状态。

主要字段：

```json
{
  "type": "state_data",
  "sequence": 45,
  "asset": "BTC",
  "event_type": "btc_maker_group",
  "data": {},
  "globals": {
    "btc_maker_arbitrage": {}
  }
}
```

行为：

- 首次 WebSocket 连接仍发送完整 `self.latest`，保证新页面可以一次初始化。
- 后续事件不再反复发送完整聚合对象。
- 按事件前缀只附带相关的全局策略状态，例如 `btc_maker_*` 只发送 `btc_maker_arbitrage`。
- 频繁状态事件仍保留 250ms 慢通道节奏，但盘口快通道不受影响。
- 每个客户端、每个策略模块只保留一条最新状态。

#### 客户端背压隔离

- `publish()` 只写入客户端待发送槽，不等待网络发送。
- 每个客户端有独立 sender task。
- 慢客户端使用有界 latest-only 状态，不会无限增长内存，也不会拖慢 runner。

### 4.2 `web/index.html`

新增：

- `applyMarketDataMessage()`：按 `market_data.sequence` 拒绝旧消息并合并到对应资产。
- `applyStateDataMessage()`：合并资产增量和对应 `globals` 策略状态。
- `scheduleMarketRender()`：使用 `requestAnimationFrame` 合并同一帧内的多次价格变化。
- `renderFastMarketData()`：只改动价格、UP/DOWN 买卖价和盘口更新时间等少量 DOM。

额外修正：

- 盘口时间取 UP/DOWN 两腿中较新的时间。
- 相同文本使用 `setText()`，避免无意义 DOM 写入。
- 完整策略页面仍走慢速 `render()`，不会阻塞每次盘口跳动。

### 4.3 `polybtc/clients.py`

- RTDS 连接新增 `send_rtds_heartbeats()`，每 5 秒发送纯文本 `PING`。
- async generator 关闭时立即取消并等待心跳 task，避免连接或 task 泄漏。
- CLOB 新增 `tick_size_change` 解析和缓存更新。
- 连续 `last_trade_price` 按 wire order 转发，不按 token 合并丢失成交。
- 页面 React Query 价格解析同时接受 30 秒和 60 秒 TWAP key。

### 4.4 `polybtc/market.py`、`polybtc/engine.py`、`polybtc/runner.py`

- 增加 `SUPPORTED_TWAP_LOOKBACK_SECONDS = {30, 60}`。
- TWAP source、候选 source 和 verified threshold source 按窗口动态生成。
- engine 按 `(window_seconds, exchange_timestamp)` 隔离 TWAP 缓存。
- engine 拒绝与当前市场窗口不匹配的 tick，防止 30 秒和 60 秒串流。
- 60 秒流已接入开盘阈值候选、页面确认、结算参考价和 edge correction。
- 同一 TWAP 窗口的相邻市场继续复用当前 WebSocket，避免开盘边界断流。
- 仅当市场窗口在 30 和 60 之间变化，或进入/离开支持的 TWAP 市场时，才关闭旧流并重订阅。
- Maker 合成状态改为最多每 250ms 一次；真实 Maker 事件仍立即处理。
- REST 盘口 fallback/reconcile 从原来的约 100ms/500ms 调整为 1s/5s，WebSocket 保持主数据源。

## 5. 消息尺寸与性能验收

使用运行实例的真实状态作为样本，新序列化结果：

| 消息 | 大小 |
| --- | ---: |
| 旧完整 Dashboard 快照 | 约 4,374,843 bytes |
| 新 `market_data` | 约 2,988 bytes |
| 新普通 `book` 状态增量 | 约 4,234 bytes |
| 新 `btc_maker_group` 增量 | 约 142,758 bytes |

盘口相关消息从约 4.37MB 降至约 3–4KB，缩小约一千倍。大策略历史不会再跟随每次盘口变化重复传输和解析。

## 6. 测试结果

最终验证：

- 全量 pytest：`451 passed in 15.44s`
- Python `py_compile`：通过
- 前端内联 JavaScript `node --check`：通过
- `git diff --check`：通过；只有工作区既有 LF/CRLF 提示

覆盖的关键测试包括：

- `price_changes[]`、两 token、最优买卖价与连续成交顺序。
- `tick_size_change`。
- CLOB/RTDS 应用层心跳。
- 关闭 RTDS 流时取消心跳。
- 30/60 秒动态 TWAP 切换及错误窗口拒绝。
- 盘口快通道绕过 250ms 慢状态节流。
- 慢客户端不阻塞 `publish()`。
- 客户端行情队列 latest-only。

## 7. 当前部署注意事项

文档写入时，旧 Dashboard 仍运行在：

- HTTP：`http://127.0.0.1:8767`
- WebSocket：`ws://127.0.0.1:8768/ws`

2026-08-15 09:49 +08:00 检查时：

- Maker 为 `SHADOW_ONLY`。
- `current_group.status = UNHEDGED`。
- 真实交易未武装。

因此本次没有强制重启旧进程。不要在 Maker 仍有 `HEDGING/UNHEDGED` 组、开放 quote 或任一策略开放仓位时重启。

安全加载新代码前应检查：

1. `/api/state` 中 `btc_maker_arbitrage.current_group` 已为空或已完成。
2. `btc_maker_arbitrage.current_quotes` 为空。
3. `open_position`、`btc_v8.position`、`orderbook_chase.positions` 和其它启用策略仓位均为空。
4. `real_trading.current_position` 为空且 `real_trading.armed=false`。
5. 确认安全后，使用当前实例相同的 `8767/8768` 参数重启，不要另外启动第二个 Dashboard。

重启后建议做 5 秒 WebSocket 验收：

- 消息中应出现 `market_data` 和 `state_data`。
- `market_data` 应为几 KB，而不是几 MB。
- 盘口活跃时价格应逐事件变化，浏览器最多按一帧合并。
- 消息 sequence 应单调增加，旧值不能覆盖新值。

## 8. 下次调用 Codex 可直接复制的提示词

```text
请先完整阅读项目根目录 POLYMARKET_REALTIME_HANDOFF.md，然后继续处理 Polymarket 5 分钟盘口实时刷新。

先执行只读检查：
1. 查看 git status，保留现有未提交改动，不要 reset 或覆盖用户文件。
2. 检查 127.0.0.1:8767/api/state，确认所有策略无开放仓位、Maker 无 HEDGING/UNHEDGED current_group、无开放 quotes、真实交易未武装。
3. 如果安全，按原 8767/8768 参数重启唯一 Dashboard；如果不安全，不要重启，只报告阻塞状态。
4. 重启后连接 ws://127.0.0.1:8768/ws 采样至少 10 秒，统计 market_data/state_data 数量、消息大小、消息间隔、source timestamp 到本机接收延迟。
5. 确认盘口更新不再受 250ms/4FPS 限制，market_data 为几 KB，慢状态不会阻塞价格跳动。
6. 运行全量 pytest、py_compile、前端 node --check 和 git diff --check。

不要改交易参数、不要启用真实交易、不要提交真实订单。
```

## 9. 相关文件

- `polybtc/dashboard.py`
- `polybtc/clients.py`
- `polybtc/market.py`
- `polybtc/engine.py`
- `polybtc/runner.py`
- `web/index.html`
- `tests/test_dashboard.py`
- `tests/test_clob_ws.py`
- `tests/test_rtds.py`
- `tests/test_market.py`
- `tests/test_polymarket_page.py`
- `tests/test_runner.py`

## 10. 官方参考资料

- Market WebSocket：`https://docs.polymarket.com/api-reference/wss/market`
- 实时数据总览：`https://docs.polymarket.com/market-data/realtime-data`
- 价格与盘口：`https://docs.polymarket.com/market-data/prices-order-books`
- Chainlink TWAP：`https://docs.polymarket.com/market-data/chainlink-twap`
- Event by slug：`https://docs.polymarket.com/api-reference/events/get-event-by-slug`
- WebSocket changelog：`https://docs.polymarket.com/changelog`
- 官网价格显示规则：`https://help.polymarket.com/en/articles/13364488-how-are-prices-calculated`

说明：目标页面的 condition/token ID 只适合诊断该历史场次，生产代码必须通过 Gamma API 按当前 slug 动态发现市场和两个 token，不能硬编码历史 ID。
