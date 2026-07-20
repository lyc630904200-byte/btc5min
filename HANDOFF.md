# polybtc 交接文档

更新时间：2026-07-18（Asia/Shanghai）

## 项目目标

`polybtc` 是一个 Polymarket 5 分钟 BTC Up/Down 市场的模拟交易研究工具。

程序监听 Binance `BTCUSDT`、Polymarket RTDS BTC/USD 和 Polymarket CLOB 盘口，以 Polymarket 官网当前 5 分钟市场的开盘价作为阈值。所有交易均为本地模拟成交，不连接私钥，也不发送真实订单。

## 当前状态

- 工作目录：`D:\Users\Administrator\Documents\btc5fenzhong`
- 当前分支：`jiaoyi02`
- HEAD：`47337be 最大净亏损止盈价差看的是反买后  要改成前`
- 跟踪分支：`origin/jiaoyi02`
- `jiaoyi02` 与 `origin/jiaoyi02` 位于同一提交。
- 工作区有自动清理开关相关的 3 个未提交修改以及本交接文档更新，交接后不要 reset 或覆盖。
- Dashboard 正在运行：`http://127.0.0.1:8765/`
- WebSocket：`ws://127.0.0.1:8766/ws`
- 更新文档时进程 PID：`12500`
- 当前运行输出目录：`data\20260718T032428Z`
- 更新文档时无持仓。
- 更新文档过程中当前市场曾短暂处于 `threshold_verification_failed`，随后重试成功；最终检查阈值为 `63916.878165`，来源 `polymarket_page_rtds_verified_open_price`。失败期间程序正确禁止下单，不得手工填入备用阈值。
- 最近完整测试：`144 passed`

当前工作区主要变更：

```text
M  HANDOFF.md
M  config.example.yaml
M  polybtc/config.py
M  tests/test_cleanup.py
```

## Git 与代理

当前远端：

```text
origin https://github.com/lyc630904200-byte/btc5min.git
```

仓库本地 Git 代理：

```text
http.proxy  = http://127.0.0.1:10808
https.proxy = http://127.0.0.1:10808
```

当前主线 `master` 位于 `03e1c80`，该提交为 `Merge branch 'codex/jiaoyi' into master`。当前开发分支 `jiaoyi02` 位于 `47337be` 并已推送到 `origin/jiaoyi02`。自动清理默认开启、对应测试和本交接文档仍未提交。

程序代理配置为 `http://127.0.0.1:10808`。这是 v2rayN/sing-box 的本地 mixed 入口；程序当前存在到 `127.0.0.1:10808` 的实际连接。用户所说的 `21079` 是 v2rayN 节点的远端服务器端口，不是本机监听端口，不要把程序配置直接改成 `127.0.0.1:21079`。客户端代理失败时仍保留快速直连回退。

常用检查：

```powershell
git status --short --branch
git log --oneline --decorate --graph --all -15
git config --local --get-regexp "^(http|https)\.proxy$"
```

## 运行方式

Windows 商店的 `python.exe` 可能只是占位程序。使用 Codex bundled Python：

```powershell
$runtimePython = 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
```

前台启动 Dashboard：

```powershell
& $runtimePython -m polybtc dashboard --host 127.0.0.1 --port 8765 --ws-port 8766
```

隐藏窗口启动：

```powershell
$stdoutPath = Join-Path (Get-Location) 'data\dashboard-live.stdout.log'
$stderrPath = Join-Path (Get-Location) 'data\dashboard-live.stderr.log'
Start-Process -FilePath $runtimePython `
  -ArgumentList @('-m', 'polybtc', 'dashboard') `
  -WorkingDirectory (Get-Location) `
  -RedirectStandardOutput $stdoutPath `
  -RedirectStandardError $stderrPath `
  -WindowStyle Hidden
```

停止程序：

```powershell
$dashboardListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
  Select-Object -First 1
if ($dashboardListener) {
  Stop-Process -Id $dashboardListener.OwningProcess -Force
}
```

检查状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/state
Invoke-RestMethod http://127.0.0.1:8765/api/config
```

运行测试：

```powershell
& $runtimePython -m pytest -q
```

## 核心文件

- `polybtc/config.py`：数据源、策略和风控配置模型。
- `polybtc/clients.py`：Binance、Gamma、CLOB、RTDS 和 Polymarket 官网页面客户端与解析器。
- `polybtc/market.py`：5 分钟市场识别、时间边界、阈值可交易性校验。
- `polybtc/runner.py`：实时异步任务、市场切换、阈值验证、盘口更新与日志写入。
- `polybtc/engine.py`：模拟交易状态机、RTDS 开盘 tick 缓存、入场持续确认。
- `polybtc/strategy.py`：入场、退出、手续费、滑点和结算规则。
- `polybtc/orderbook.py`：按多档盘口模拟买卖成交。
- `polybtc/entry_registry.py`：每市场入场次数的 SQLite 持久化注册表。
- `polybtc/dashboard.py`：HTTP/WebSocket 服务和“保存后下一把生效”的运行时参数管理。
- `web/index.html`：Dashboard 前端。
- `tests/`：当前完整测试集，共 144 项。

## 市场与阈值机制

只接受标准 slug：

```text
btc-updown-5m-<Unix开始时间>
bitcoin-updown-5m-<Unix开始时间>
```

开始时间必须是 300 秒整点，程序严格使用 slug 推导 `[start, start+300s)`，不会信任可能错位的 Gamma `startDate`。只允许当前已开始且未结束的市场；预取仅保留下一期相邻市场元数据，不携带预取阈值。

动态阈值必须经过验证，未验证时 `threshold_price = null`，策略禁止下单。当前验证路径：

1. 缓存 Polymarket RTDS `BTC/USD` 在市场开始时间的精确 tick。
2. tick 的 `exchange_timestamp` 必须精确等于本期开始时间，接收时间必须位于开始前 1 秒至开始后 2 秒。
3. 读取当前市场自己的 Polymarket 官网页面，解析与 slug、开始时间、结束时间完全匹配的 `openPrice`。
4. 官网 `openPrice` 与 RTDS 开盘 tick 差值必须不超过 `0.01 USD`。
5. 验证成功来源为 `polymarket_page_rtds_verified_open_price`。

如果没有有效 RTDS 精确开盘 tick，则使用严格回退：

1. 当前市场自己页面的精确 `openPrice`；
2. 上一期市场自己页面的精确 `closePrice`；
3. 两者差值不超过 `0.01 USD` 才通过。

任一来源冲突、重复时间戳价格冲突、页面区间错位、市场已结束或 Gamma 提供了不同阈值时，验证失败并继续禁止下单。Binance 开盘首 tick 只能作为未验证候选，不能单独解锁交易。

历史抽样结果显示 RTDS 精确开盘 tick 通常在开盘后 2 秒内到达；实际剩余延迟主要来自 Polymarket 官网页面更新。2026-07-18 实测既有约 5–34 秒内成功，也出现整期失败和约 3 分钟后才成功的情况，阈值链路存在间歇性延迟。验证失败时必须继续保持 `threshold_price = null` 并禁止交易，不能为了提速使用未经交叉核验的单一来源。

RTDS 行情连接有 10 秒有效数据看门狗：连续 10 秒未收到有效 `BTC/USD` tick 时，会主动关闭连接、记录 `polymarket_rtds` 错误并在 1 秒后重连。WebSocket 保持 ping/pong 但不推行情的静默失活，不能再无限期保留旧数据。

## 价格偏离

当前程序定义：

```text
原始偏离 = Binance BTC - Polymarket阈值
偏离修正 = Binance BTC - Polymarket RTDS BTC
价格偏离 = 原始偏离 - 偏离修正
```

代数化简后，正常有 RTDS 数据时：

```text
价格偏离 = Polymarket RTDS BTC - Polymarket阈值
```

前端策略参数中已经删除“偏离修正（USD）”输入框。修正值完全动态计算；RTDS 暂不可用时修正为 0，不使用旧的固定 `-47.75 USD` 或其他备用动态修正。

反买持仓建立后另行计算：

```text
有效偏离 = -价格偏离
```

前端“价格偏离”始终保留原值，并在其下方单独显示“有效偏离”；普通模式或空仓时两者相同，反买持仓期间只有有效偏离取反。偏离优势消失和盘口冲突使用有效偏离。

## 当前实际生效参数

以下来自 `GET /api/config`，是前端已保存并实际生效的值，不等同于 `config.example.yaml` 默认值：

```text
入场偏离                     10 USD
优势消失/止损偏离            10 USD
买入最低价                    0.20
买入最高价                    0.75
止盈价差                      0.11
距到期入场范围                20–280 秒
单笔模拟金额                  10 USD
最大净亏损                    2 USD
每市场最多交易                1 笔
taker 费率参数                0.07
入场持续确认开关              关闭
持续确认参数                  1 秒且连续 3 次
盘口冲突退出延迟              10 秒
最大持仓时间                  120 秒
临近到期强制退出              5 秒
```

入场持续确认已做成前端按钮：

- 开启：方向一致的有效入场信号必须持续至少 1 秒，并由 3 个不同的行情更新时间连续确认。
- 重复盘口事件不计为新确认；方向中断或更新间隔过大会重新计数。
- 关闭：满足其余入场条件即可入场。
- 代码及 `config.example.yaml` 默认开启，但当前保存的运行时值为关闭。
- 和其他策略参数一样，保存后下一期市场生效并写入 `data/dashboard-settings.json`。

## 入场规则

必须同时满足：

- 市场处于活动期，结算逻辑已确认，阈值已经严格核验。
- 距到期时间位于配置的入场范围内。
- Binance tick、UP 盘口、DOWN 盘口数据年龄均不超过 `1000 ms`。
- 当前没有未平仓持仓。
- 本市场历史入场数小于 1。
- 当前价格偏离 `> +10` 时考虑 UP；`< -10` 时考虑 DOWN，等于边界不买。
- 原信号方向 best ask 必须在 `[0.20, 0.75]` 内。
- 盘口方向必须与价格偏离方向一致；冲突时不入场。
- 多档盘口深度足以完成整笔 10 USD 模拟买入。
- 成交数量达到市场最小数量。
- 扣除买入手续费和滑点后，理论剩余空间至少为 `0.04`。
- 若持续确认开关开启，还必须通过 1 秒/3 次确认。
- 已经收到过 RTDS 行情后，RTDS 价格过期会以 `polymarket_price_stale` 拒绝新的入场，不会把旧的偏离修正继续用于开仓。

反买开关当前开启。入场信号和全部入场检查仍按原始方向执行，但真正模拟买入相反 token：原信号 UP 实际买 DOWN，原信号 DOWN 实际买 UP。反方向盘口必须有有效卖价、足够深度并满足最小订单量；买入价格上下限仍检查原信号盘口。持仓同时保存原信号模拟成交数据和实际反向成交数据。

每市场最多 1 笔不是只存在内存中。`data/market-entry-ledger.sqlite3` 会持久化计数，程序启动时还会扫描历史 `fills.csv` 补种已有买入记录，重启程序不能绕过限制。

## 模拟手续费

买入和卖出都会按每档成交价格计算 Polymarket crypto taker fee：

```text
每档手续费 = 数量 × taker_fee_rate × 价格 × (1 - 价格)
```

当前 `taker_fee_rate = 0.07`。仓位买入成本 `entry_quote` 已包含买入手续费；卖出 PnL 还会扣除卖出手续费。

## 出场规则与优先级

1. 盘口方向冲突：反买持仓先将实时价格偏离取反；持仓满 10 秒后，如果盘口方向与有效偏离方向相反，立即按实际持仓盘口全部卖出。
2. 最大净亏损：当前 `47337be` 版本按反买后的实际持仓和实际完整多档买盘模拟全部清仓；只有实际盘口深度可信且足够卖完时才判断。公式：

   ```text
   预计清仓净损益 = 卖出成交额 - 卖出手续费 -（买入成交额 + 买入手续费）
   ```

   当结果 `<= -2 USD` 时触发。
3. 优势消失：按有效偏离判断；UP 持仓在有效偏离 `<= +10` 时退出，DOWN 持仓在有效偏离 `>= -10` 时退出。
4. 止盈：当前 `47337be` 版本按反买后的实际持仓判断，实际持仓 best bid `>= 实际入场均价 + 0.11`。
5. 最大持仓：持仓达到 120 秒退出。
6. 临近到期：距到期不超过 5 秒强制退出。
7. 若仍未退出，到期后按最终 BTC tick 与已核验阈值模拟结算。

所有主动退出都要求当前多档盘口足以卖完整个仓位，不做静默的部分清仓。

特别注意：用户曾要求把“最大净亏损、止盈价差”改为看反买前模拟仓位，代码一度修改但未提交，随后明确回退到 `47337be`。当前运行版本和 Git 工作区都已恢复为“这两个规则看反买后的实际仓位”，不要按旧对话误判。

## 盘口更新机制

- 优先使用官方 CLOB WebSocket。
- 开启 `custom_feature_enabled`，接收 `best_bid_ask` 顶级盘口更新。
- WebSocket book、price change、best bid/ask 都会更新本地盘口。
- 时间戳更旧的消息会丢弃，避免慢响应覆盖新数据。
- REST `/book` 仅在盘口缺失或长时间无有效更新时兜底。
- REST 请求发出后若已经收到更新的 WebSocket 数据，不允许旧 REST 结果覆盖。
- 策略保留多档深度用于成交和硬止损；Dashboard 快照只推送 top bid/ask，降低渲染和网络压力。
- 高频相同盘口事件会合并，价格改变时立即发布，未改变时仅发送心跳。

## Dashboard 与运行时参数

接口：

- `GET /api/state`：最新市场、行情、盘口、持仓、统计和配置状态。
- `GET /api/config`：当前实际生效参数以及待下一期生效参数。
- `POST /api/config`：保存前端允许修改的策略和风控参数。
- `ws://127.0.0.1:8766/ws`：实时状态推送。

可在前端修改：

- 入场偏离、优势消失/止损偏离；
- 买入最低/最高价、止盈价差；
- 入场时间窗；
- 单笔模拟金额、最大净亏损、每市场最多交易数；
- 入场持续确认开关、反买开关。

保存后不会改变当前市场使用中的参数，而是在检测到下一个市场后原子切换；保存值会持久化，重启后不会恢复成代码默认值。

## 日志与数据

数据位于工作目录所在的 D 盘：

```text
D:\Users\Administrator\Documents\btc5fenzhong\data
```

自动清理已开启：

```yaml
data_cleanup_enabled: true
data_retention_hours: 24
data_cleanup_interval_seconds: 300
```

只删除 `data` 目录下超过 24 小时、带运行标记文件的已完成运行目录；当前活动目录和普通无关目录不会删除。开启时首次清理了 30 个过期运行目录，最近 24 小时数据保留。每次启动生成：

```text
data/YYYYMMDDTHHMMSSZ/
```

主要文件：

- `events.jsonl`：事件流，可用于 replay。
- `markets.jsonl`：市场发现、切换和阈值状态。
- `ticks.jsonl`：BTC tick 和压缩盘口。
- `fills.csv`：模拟买卖成交与手续费。
- `positions.csv`：仓位生命周期和 PnL。
- `latency.csv`：连接、超时和异常。
- `summary.json`：运行汇总。
- `data/market-entry-ledger.sqlite3`：跨进程每市场入场次数。

## 测试覆盖

最近完整结果：

```text
144 passed
```

重点覆盖：

- 标准 5 分钟 slug、时间边界、当前/相邻市场选择。
- Next.js Flight/React Query 页面结构解析和错误数据拒绝。
- 当前官网 `openPrice`、上一期独立页面 `closePrice`、RTDS 精确开盘 tick 的严格匹配。
- RTDS tick 提前缓存、重复时间戳冲突、错误 symbol、超出接收窗口和后到冲突。
- 阈值验证失败、重试、过期和禁止交易。
- CLOB WebSocket、REST 兜底、旧响应防覆盖和多档盘口模拟。
- RTDS 静默超时主动重连与 RTDS 价格过期禁止入场。
- 入场边界、盘口方向冲突、1 秒/3 次持续确认及关闭开关。
- 买卖手续费、滑点、完整深度硬止损。
- 盘口冲突 10 秒延迟、优势消失、止盈、最大持仓和结算。
- SQLite 每市场最多一笔及历史成交补种。
- Dashboard 保存后下一期生效和旧设置迁移。
- replay、日志、自动清理启用/禁用与过期目录保护。

## 已知注意点

- 官网阈值页面是当前主要延迟来源。不要为了更快而取消交叉核验，否则错误阈值会直接污染入场方向。
- 阈值获取近期有整期失败和分钟级延迟；看到 `threshold_verification_failed` 时先检查页面/RTDS候选和重试日志，未核验前不得下单。
- 前端若仍显示旧页面，强制刷新浏览器缓存；服务端当前会返回 `entry_confirmation_enabled`。
- Dashboard 里的阈值核验文案有时仍写“双页面一致”，而快速路径实际是“官网页面 + RTDS”双通道一致；这是显示文案问题，不影响风控。
- `config.example.yaml` 是默认示例；真实运行优先加载 `data/dashboard-settings.json` 保存的 active/pending 参数。
- 当前程序仅模拟交易。接入真实订单前必须单独设计私钥隔离、订单状态同步、限价与撤单、异常恢复及真实仓位风控。

## 建议后续顺序

1. 提交当前自动清理的 3 个文件及本交接文档，避免后续 reset 再次关闭清理。
2. 在 Dashboard 把“阈值双页面一致”改成更准确的“阈值双通道一致”。
3. 增加独立阈值审计事件，记录页面 open、上一期 close、RTDS candidate、差值和验证耗时。
4. 使用现有日志做只读回放分析，统计普通/反买、手续费占比和各退出原因 PnL。
5. 连续运行多个完整窗口后再调整入场偏离、价格范围和止损参数。
