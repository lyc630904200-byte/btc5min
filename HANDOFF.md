# polybtc 交接文档

更新时间：2026-07-22（Asia/Shanghai）

## 项目目标

`polybtc` 是一个 Polymarket 5 分钟 BTC/ETH Up/Down 市场的模拟交易研究工具，并包含 BTC/ETH 跨市场自动匹配模块。

程序并行监听 Binance `BTCUSDT`/`ETHUSDT`、Polymarket RTDS `BTC/USD`/`ETH/USD` 和两套 Polymarket CLOB 盘口，以各资产官网当前 5 分钟市场的开盘价作为阈值。BTC 与 ETH 使用互相隔离的市场、行情、盘口、仓位和统计状态。所有交易均为本地模拟成交，不连接私钥，也不发送真实订单。

## 当前状态

- 工作目录：`D:\Users\Administrator\Documents\btc5fenzhong`
- 当前分支：`jiaoyi02`
- HEAD：`84e2f62 goodv1`
- 跟踪分支：`origin/jiaoyi02`
- `jiaoyi02` 与 `origin/jiaoyi02` 位于同一提交。
- BTC/ETH 双资产、配对模块和 Dashboard 主体已包含在 `84e2f62`。当前工作区另有固定 A/B 模式、订单序号、UP/DOWN 最小价格差及对应前端/测试修改，交接后不要 reset 或覆盖。
- Dashboard 正在运行：`http://127.0.0.1:8765/`
- WebSocket：`ws://127.0.0.1:8766/ws`
- 更新文档时进程 PID：`20376`
- 当前运行输出目录：`data\20260722T111509Z`
- 更新文档时 BTC 市场为 `btc-updown-5m-1784718900`，ETH 市场为 `eth-updown-5m-1784718900`，两者起止时间完全一致。
- 配对账本共有 435 组订单、338 个去重市场，订单号连续为 `1..435`；更新时 431 组已结算、4 组待结算。
- 实际联网验证已确认：BTC/ETH 当前市场、各自 UP/DOWN token、两套 CLOB 盘口、`BTCUSDT`/`ETHUSDT` 和 `BTC/USD`/`ETH/USD` 实时 tick 均正常。
- 配对模块当前已开启；`leg_quote_usd=20`（单组报价预算 40 USD，手续费另计）、最低盈利价差 5 美分、UP/DOWN 最小价格差 0 美分、开盘后 `[10, 290)` 秒、每场上限 2、严格方向控制开启，模式为每场重新开始（`per_market`）。当前无待生效配置。
- 最近完整测试：`198 passed`

当前工作区主要变更：

```text
M  HANDOFF.md
M  config.example.yaml
M  polybtc/config.py
M  polybtc/dashboard.py
M  polybtc/journal.py
M  polybtc/pair_match.py
M  tests/test_dashboard.py
M  tests/test_pair_match.py
M  web/index.html
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

当前主线 `master` 位于 `03e1c80`，该提交为 `Merge branch 'codex/jiaoyi' into master`。当前开发分支 `jiaoyi02` 位于 `84e2f62`，并与 `origin/jiaoyi02` 一致。

程序持久配置仍为 `http://127.0.0.1:10808`，但该端口没有监听；运行时网络现按“Windows 系统代理 → 手工配置代理 → 直连”的顺序尝试，并记住成功路径、复用 CLOB REST 连接。Windows 系统代理当前指向 Clash Verge mixed 入口 `127.0.0.1:7897`。仓库 Git 代理仍是 10808；最近一次推送使用单次命令参数走 Clash，未修改 Git 持久配置：

```powershell
git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin jiaoyi02
```

用户所说的 `21079` 是旧 v2rayN 节点的远端服务器端口，不是本机监听端口，不要把程序配置直接改成 `127.0.0.1:21079`。

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
$commandLine = "set SystemRoot=C:\WINDOWS&& set USERPROFILE=C:\Users\Administrator&& set APPDATA=C:\Users\Administrator\AppData\Roaming&& set LOCALAPPDATA=C:\Users\Administrator\AppData\Local&& $runtimePython -m polybtc dashboard --host 127.0.0.1 --port 8765 --ws-port 8766"
Start-Process -FilePath 'C:\Windows\System32\cmd.exe' `
  -ArgumentList @('/d', '/c', $commandLine) `
  -WorkingDirectory (Get-Location) `
  -RedirectStandardOutput $stdoutPath `
  -RedirectStandardError $stderrPath `
  -WindowStyle Hidden `
  -UseNewEnvironment
```

这里使用 `cmd.exe` 并显式补齐 Windows 用户环境，是为了规避当前 Codex 会话中重复 `Path/PATH` 导致 `Start-Process` 启动失败的问题。

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
- `polybtc/runner.py`：BTC/ETH 双资产实时异步任务、市场切换、阈值验证、盘口更新与日志写入。
- `polybtc/engine.py`：按资产隔离的模拟交易状态机、RTDS 开盘 tick 缓存、入场持续确认。
- `polybtc/strategy.py`：入场、退出、手续费、滑点和结算规则。
- `polybtc/orderbook.py`：按多档盘口模拟买卖成交。
- `polybtc/entry_registry.py`：每市场入场次数的 SQLite 持久化注册表。
- `polybtc/pair_match.py`：跨市场组合评估、每场交替、跨场 ABAB、固定 A/B、SQLite 配对账本、顺序订单号、结算与汇总。
- `polybtc/dashboard.py`：HTTP/WebSocket 服务和“保存后下一把生效”的运行时参数管理。
- `web/index.html`：Dashboard 前端。
- `tests/`：当前完整测试集，共 189 项。

## 市场与阈值机制

只接受标准 slug：

```text
btc-updown-5m-<Unix开始时间>
bitcoin-updown-5m-<Unix开始时间>
eth-updown-5m-<Unix开始时间>
ethereum-updown-5m-<Unix开始时间>
```

开始时间必须是 300 秒整点，程序严格使用 slug 推导 `[start, start+300s)`，不会信任可能错位的 Gamma `startDate`。只允许当前已开始且未结束的市场；预取仅保留同资产下一期相邻市场元数据，不携带预取阈值。

动态阈值必须经过验证，未验证时 `threshold_price = null`，策略禁止下单。当前验证路径：

1. 分别缓存 Polymarket RTDS `BTC/USD` 或 `ETH/USD` 在对应市场开始时间的精确 tick。
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

每个资产的 RTDS 行情连接都有 10 秒有效数据看门狗：连续 10 秒未收到对应 `BTC/USD` 或 `ETH/USD` tick 时，会主动关闭连接、记录错误并在 1 秒后重连。WebSocket 保持 ping/pong 但不推行情的静默失活，不能再无限期保留旧数据。

## 价格偏离

当前程序定义：

```text
原始偏离 = Binance 对应资产价格 - Polymarket阈值
偏离修正 = Binance 对应资产价格 - Polymarket RTDS 对应资产价格
价格偏离 = 原始偏离 - 偏离修正
```

代数化简后，正常有 RTDS 数据时：

```text
价格偏离 = Polymarket RTDS 对应资产价格 - Polymarket阈值
```

前端策略参数中已经删除“偏离修正（USD）”输入框。修正值完全动态计算；RTDS 暂不可用时修正为 0，不使用旧的固定 `-47.75 USD` 或其他备用动态修正。

反买持仓建立后另行计算：

```text
有效偏离 = -价格偏离
```

前端“价格偏离”始终保留原值，并在其下方单独显示“有效偏离”；普通模式或空仓时两者相同，反买持仓期间只有有效偏离取反。偏离优势消失和盘口冲突使用有效偏离。

当前未提交的 `web/index.html` 已删除价格偏离折线图及其前端历史数组、绘图函数和定时绘制调用；“价格偏离”和“有效偏离”两个实时数值仍保留。

## 当前实际生效参数

以下来自 `GET /api/config`，是前端已保存并实际生效的值，不等同于 `config.example.yaml` 默认值：

```text
入场偏离                      5 USD
优势消失/止损偏离             5 USD
买入最低价                    0.20
买入最高价                    0.75
止盈价差                      0.10
距到期入场范围                20–280 秒
单笔模拟金额                  10 USD
最大净亏损                    10 USD
每市场最多交易                1 笔
taker 费率参数                0.07
入场持续确认开关              关闭
持续确认参数                  1 秒且连续 3 次
盘口冲突退出延迟              10 秒
最大持仓时间                  120 秒
临近到期强制退出              5 秒

配对开关                      开启
配对基准金额                  20 USD
单组报价预算                  40 USD（手续费另计）
最低配对盈利价差              5 美分
UP/DOWN 最小价格差            0 美分（旧配置兼容默认）
配对入场窗口                  开盘后 [10, 290) 秒
每场配对上限                  2 组
严格方向控制                  开启
方向模式                      per_market
```

入场持续确认已做成前端按钮：

- 开启：方向一致的有效入场信号必须持续至少 1 秒，并由 3 个不同的行情更新时间连续确认。
- 重复盘口事件不计为新确认；方向中断或更新间隔过大会重新计数。
- 关闭：满足其余入场条件即可入场。
- 代码及 `config.example.yaml` 默认开启，但当前保存的运行时值为关闭。
- 和其他策略参数一样，保存后下一期市场生效并写入 `data/dashboard-settings.json`。

## BTC/ETH 跨市场自动匹配

配对模块评估两种两腿组合：A 为 `BTC UP + ETH DOWN`，B 为 `BTC DOWN + ETH UP`。开关、每腿金额、最低价差（前端单位为美分）、开盘后开始/结束秒数、每场配对上限、严格交替和交替模式均可在 Dashboard 修改。保存后必须等待下一组开始、结束时间完全一致的新 BTC/ETH 市场，所有参数才会原子生效。

两腿按完全相同的份数扫多档卖盘，单组目标成交额为 `2 × leg_quote_usd`，不同价格会令两腿实际成交金额不同；买入手续费额外计入成本。只有四个盘口都可信、未过期、深度足够且达到最小订单量时才允许入场；配对入场不依赖单币阈值核验。价差为：

```text
价差（美分） = 100 × [1 - BTC均价 - ETH均价 - BTC手续费/份数 - ETH手续费/份数]
```

配对还支持独立的 `min_leg_price_gap_cents` 门槛，按两腿多档等份数模拟成交均价计算：

```text
UP/DOWN 价格差（美分） = 100 × |BTC 腿均价 - ETH 腿均价|
```

A 比较 `BTC UP` 与 `ETH DOWN`，B 比较 `BTC DOWN` 与 `ETH UP`。候选必须同时满足最低盈利价差和最低 UP/DOWN 价格差；盈利价差先失败时原因为 `spread_below_threshold`，仅价格差失败时为 `leg_price_gap_below_threshold`。价格差不含手续费，手续费仍由盈利价差公式处理。新字段范围为 `0..100`，旧配置缺失时默认 `0`，因此升级本身不改变交易行为。

相同四腿卖盘组合快照最多开一组。快照指纹包含 BTC/ETH 的 UP/DOWN 四套完整卖盘价格和数量；数据库还有 `UNIQUE(interval_key, fingerprint)`，所以重启也不能在同一场重复记录完全相同的快照。等份数使“仅 BTC 腿赢”和“仅 ETH 腿赢”的兑付相同，单边净盈亏等于相同份数乘以入场价差。

严格方向控制支持六种模式：

- `per_market`：本场没有历史订单时选择当前达标方向中价差较高者，之后只等待相反方向；每个新对齐场次重新开始。
- `per_market_ab`：每个新场次固定从 A 开始，之后按 A→B→A→B 循环。
- `per_market_ba`：每个新场次固定从 B 开始，之后按 B→A→B→A 循环。
- `continuous_abab`：账本首次启用时随机确定 A/B 首单，之后跨场、跨重启持续 ABAB，只等待指定方向。
- `always_a`：始终只等待 A，即 `BTC UP + ETH DOWN`；B 即使价差更高也不成交。
- `always_b`：始终只等待 B，即 `BTC DOWN + ETH UP`；A 即使价差更高也不成交。

固定 A/B 模式允许同一市场重复相同方向，直到 `max_pairs_per_market`。第二组不要求价差先跌破门槛再重新穿越，也没有冷却时间；四套卖盘中任意一套发生变化形成新指纹后，只要目标方向仍达标，就可能在下一次约 20ms 合并批次中再次成交。固定模式不会修改 SQLite 中 `continuous_abab` 的持久方向。关闭严格方向控制时，固定模式值被忽略，每次仍选择当前达标方向中价差较高者。

`per_market_ab` 和 `per_market_ba` 在省略 `max_pairs_per_market` 时条件默认上限为 2；其他模式及全局默认仍为 1。Dashboard 手动选择这两个模式时会把上限预填为 2，之后仍可修改；加载已保存配置不会覆盖其显式上限。

无论选择哪种模式，引擎仍要求四个盘口都可信、未过期且可执行。模块启用后，BTC/ETH 单币引擎停止新入场，但已经存在的单币仓位仍按原规则退出和结算；配对条件不足时不会恢复单币入场。

配对订单不做中途止盈止损，市场到期后每 2 秒查询 Gamma。只有 `closed=true`、`umaResolutionStatus=resolved` 且一一对应的 `outcomes`/`outcomePrices` 严格出现一个 `1`、其余为 `0` 时才结算；否则保持待结算并重试。净盈亏为中奖腿份数之和减去两腿成交额和两腿买入手续费。

完整配对订单保存在 `data/pair-match-ledger.sqlite3`，重启后会恢复待结算订单、场次计数和交替方向，重复结算不会重复记账。每组订单同时保存内部 UUID `order_id` 和面向用户的连续整数 `order_number`；旧账本启动时按 `opened_at, order_id` 自动补号，无需手工迁移。API、WebSocket、CSV 和 Dashboard 均返回或显示订单号，前端格式为 `#000001`，缺失时才回退到 UUID 片段。

截至 2026-07-22 本次文档更新，已结算账本结果为：

```text
BTC UP   + ETH UP      186 单 / 142 场
BTC UP   + ETH DOWN     43 单 /  37 场
BTC DOWN + ETH UP       46 单 /  36 场
BTC DOWN + ETH DOWN    156 单 / 121 场
合计                    431 单 / 336 场，另有 4 单待结算
```

另一次官方 Gamma 全市场严格核验覆盖北京时间 `2026-07-14 20:10` 至 `2026-07-21 20:10` 的 2016 组完整市场：`UP+DOWN=186`、`DOWN+UP=186`，反向结果合计 372 组（18.45%）。这组数据是固定历史窗口，不应在后续文档中误写成滚动“当前过去一周”。

## 入场规则

必须同时满足：

- 市场处于活动期，结算逻辑已确认，阈值已经严格核验。
- 距到期时间位于配置的入场范围内。
- Binance tick、UP 盘口、DOWN 盘口数据年龄均不超过 `1000 ms`。
- 当前没有未平仓持仓。
- 本市场历史入场数小于 1。
- 当前价格偏离 `> +5` 时考虑 UP；`< -5` 时考虑 DOWN，等于边界不买。
- 原信号方向 best ask 必须在 `[0.20, 0.75]` 内。
- 盘口方向必须与价格偏离方向一致；冲突时不入场。
- 多档盘口深度足以完成整笔 10 USD 模拟买入。
- 成交数量达到市场最小数量。
- 扣除买入手续费和滑点后，理论剩余空间至少为 `0.04`。
- 若持续确认开关开启，还必须通过 1 秒/3 次确认。
- 已经收到过 RTDS 行情后，RTDS 价格过期会以 `polymarket_price_stale` 拒绝新的入场，不会把旧的偏离修正继续用于开仓。

反买开关当前关闭。开启后，入场信号和全部入场检查仍按原始方向执行，但真正模拟买入相反 token：原信号 UP 实际买 DOWN，原信号 DOWN 实际买 UP。反方向盘口必须有有效卖价、足够深度并满足最小订单量；买入价格上下限仍检查原信号盘口。持仓同时保存原信号模拟成交数据和实际反向成交数据。

每市场最多 1 笔不是只存在内存中。`data/market-entry-ledger.sqlite3` 会持久化计数，程序启动时还会扫描历史 `fills.csv` 补种已有买入记录，重启程序不能绕过限制。

## 模拟手续费

买入和卖出都会按每档成交价格计算 Polymarket crypto taker fee：

```text
每档手续费 = 数量 × taker_fee_rate × 价格 × (1 - 价格)
```

当前 `taker_fee_rate = 0.07`。仓位买入成本 `entry_quote` 已包含买入手续费；卖出 PnL 还会扣除卖出手续费。

## 出场规则与优先级

1. 盘口方向冲突：反买持仓先将实时价格偏离取反；持仓满 10 秒后，如果盘口方向与有效偏离方向相反，立即按实际持仓盘口全部卖出。
2. 最大净亏损：当前版本按实际持仓和实际完整多档买盘模拟全部清仓；反买时看反买后的实际仓位。只有实际盘口深度可信且足够卖完时才判断。公式：

   ```text
   预计清仓净损益 = 卖出成交额 - 卖出手续费 -（买入成交额 + 买入手续费）
   ```

   当结果 `<= -10 USD` 时触发。
3. 优势消失：按有效偏离判断；UP 持仓在有效偏离 `<= +5` 时退出，DOWN 持仓在有效偏离 `>= -5` 时退出。
4. 止盈：按实际持仓判断；反买时看反买后的实际仓位，实际持仓 best bid `>= 实际入场均价 + 0.10`。
5. 最大持仓：持仓达到 120 秒退出。
6. 临近到期：距到期不超过 5 秒强制退出。
7. 若仍未退出，到期后按对应资产的最终 Binance tick 与已核验阈值模拟结算。

所有主动退出都要求当前多档盘口足以卖完整个仓位，不做静默的部分清仓。

特别注意：用户曾要求把“最大净亏损、止盈价差”改为看反买前模拟仓位，代码一度修改但未提交，随后明确回退。当前运行版本和 Git 工作区都是“这两个规则看反买后的实际仓位”，不要按旧对话误判。

## 盘口更新机制

- 优先使用官方 CLOB WebSocket。
- 开启 `custom_feature_enabled`，接收 `best_bid_ask` 顶级盘口更新。
- WebSocket book、price change、best bid/ask 都会更新本地盘口。
- 时间戳更旧的消息会丢弃，避免慢响应覆盖新数据。
- REST `/book` 在盘口缺失、不可信或超过 500ms 无有效更新时立即兜底；WebSocket 健康时每 2 秒做一次完整深度校准。
- CLOB WebSocket 使用系统代理优先、关闭压缩、扩大接收队列；已成功的代理路径断线后在流内部快速重连，不中断盘口消费循环。
- 已规避 `websockets 15.0.1` 在系统代理握手前被重置时的 `recv_messages` 初始化竞态；Dashboard 推送、后台任务清理和服务关闭均有超时保护，实时引擎异常退出时 Dashboard 会保留端口并自动重启引擎，避免进程假存活。
- REST 请求发出后若已经收到更新的 WebSocket 数据，不允许旧 REST 结果覆盖。
- 策略保留多档深度用于成交和硬止损；Dashboard 快照只推送 top bid/ask，降低渲染和网络压力。
- 高频相同盘口事件会合并，价格改变时立即发布，未改变时仅发送心跳。

## Dashboard 与运行时参数

接口：

- `GET /api/state`：最新市场、行情、盘口、持仓、统计和配置状态。
- `GET /api/config`：当前实际生效参数以及待下一期生效参数；`pair_match` 包含 `min_leg_price_gap_cents`，`alternation_mode` 可返回六种配对模式。
- `POST /api/config`：保存前端允许修改的策略和风控参数；配对配置接受 `min_leg_price_gap_cents` 及六种方向模式。
- `ws://127.0.0.1:8766/ws`：实时状态推送。

可在前端修改：

- 入场偏离、优势消失/止损偏离；
- 买入最低/最高价、止盈价差；
- 入场时间窗；
- 单笔模拟金额、最大净亏损、每市场最多交易数；
- 入场持续确认开关、反买开关。
- 配对开关、每腿金额、最低盈利价差、UP/DOWN 最小价格差、开盘后运行区间、每场组数上限、严格方向控制及六种方向模式。

保存后不会改变当前市场使用中的参数，而是在检测到下一个市场后原子切换；保存值会持久化，重启后不会恢复成代码默认值。

Dashboard 已删除价格偏离折线图，并增加 BTC/ETH 页签。切换页签会显示对应资产的市场、阈值、Binance/RTDS 行情、持仓、统计和成交事件；盘口区域则在两个页面都固定同时显示两套盘口，BTC 在上、ETH 在下，各自包含 UP/DOWN 买入或卖出报价及更新时间。两个页签都显示同一个“BTC/ETH 自动匹配”面板，包括 A/B 实时成交均价、份数、金额、手续费、盈利价差、UP/DOWN 价格差、拒绝原因、四种结果预估 PnL、最近 20 场、最近 100 单和累计统计。模式下拉框包含每场择优、每场先 A 后 B、每场先 B 后 A、连续 ABAB、一直选 A 和一直选 B；实时状态显示下一方向，订单列表优先显示连续订单号。两套单币状态不会互相覆盖。

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
- `ticks.jsonl`：BTC/ETH tick 和压缩盘口，可由 symbol/token 区分资产。
- `fills.csv`：模拟买卖成交与手续费。
- `positions.csv`：仓位生命周期和 PnL。
- `latency.csv`：连接、超时和异常。
- `summary.json`：运行汇总。
- `data/market-entry-ledger.sqlite3`：跨进程每市场入场次数。
- `pair_orders.csv`：本次进程中新开的配对订单，包含 `order_number` 和内部 `order_id`。
- `pair_results.csv`：本次进程中完成的配对结算。
- `pair_markets.jsonl`：配对场次结算汇总。
- `data/pair-match-ledger.sqlite3`：配对订单、连续订单号、场次上限、交替方向和官方结算的完整持久账本。

## 测试覆盖

最近完整结果：

```text
198 passed
```

重点覆盖：

- BTC/ETH 标准 5 分钟 slug、时间边界、同资产当前/相邻市场选择。
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
- Dashboard BTC/ETH 状态隔离、ETH 官网价格结构、ETH Binance/RTDS symbol 路由。
- 配对多档等额成交、逐档手续费、价差边界、时间窗、四盘口完整性、相同快照防重、每场严格交替、固定 A/B 起始顺序、连续 ABAB 跨场/跨重启、固定 A/B 重复同方向、关闭交替择优和跨重启场次上限。
- UP/DOWN 价格差使用多档模拟成交均价、绝对差边界、A/B 对称性、严格方向不回退、关闭严格方向后的合格方向择优，以及 Dashboard 配置持久化。
- 配对订单号自动迁移、连续分配、API/事件/CSV 序列化和 Dashboard 显示。
- BTC/ETH 四种官方结果、非严格 0/1 拒绝、幂等结算、配对账本恢复和 Dashboard 下一组对齐市场原子配置。
- replay、日志、自动清理启用/禁用与过期目录保护。

## 已知注意点

- 官网阈值页面是当前主要延迟来源。不要为了更快而取消交叉核验，否则错误阈值会直接污染入场方向。
- 阈值获取近期有整期失败和分钟级延迟；看到 `threshold_verification_failed` 时先检查页面/RTDS候选和重试日志，未核验前不得下单。
- 前端若仍显示旧页面或价格偏离折线图，强制刷新浏览器缓存；服务端当前会返回 `entry_confirmation_enabled`。
- Dashboard 默认显示 BTC，可用顶部 BTC/ETH 页签切换；`GET /api/state` 的 `assets.BTC` 和 `assets.ETH` 保存完整的独立快照。
- 根级 `pair_match` 是两个资产和独立“BTC/ETH 匹配”页签共享的配对状态；`GET /api/config` 同时返回 active `pair_match` 和 `pending_pair_match`。独立匹配页集中显示 BTC/ETH 两个市场、双盘口和完整配对模块。
- Dashboard 的“每腿金额”目前在代码中实际作为 `leg_quote_usd` 基准，并以 `2 × leg_quote_usd` 形成两腿合计报价预算；两腿为保持份数相同，实际各自成交额通常不相等，手续费在预算之外。
- 固定 A/B 的“每场配对上限 2”表示同场最多两组相同方向配对，即四条腿；它不是 A、B 各一组。盘口持续变化且目标价差持续达标时，两组可能几乎连续产生。
- `config.example.yaml` 是默认示例；真实运行优先加载 `data/dashboard-settings.json` 保存的 active/pending 参数。
- 当前程序仅模拟交易。接入真实订单前必须单独设计私钥隔离、订单状态同步、限价与撤单、异常恢复及真实仓位风控。

## 建议后续顺序

1. 用户要求时提交当前固定 A/B、订单编号及交接文档修改；提交前不要 reset。当前涉及 `config.example.yaml`、`polybtc/config.py`、`polybtc/journal.py`、`polybtc/pair_match.py`、`tests/test_dashboard.py`、`tests/test_pair_match.py`、`web/index.html` 和 `HANDOFF.md`。
2. 增加独立阈值审计事件，按资产记录页面 open、上一期 close、RTDS candidate、差值和验证耗时。
3. 使用现有日志做按资产只读回放分析，分别统计 BTC/ETH、普通/反买、手续费占比和各退出原因 PnL。
4. 连续运行多个完整窗口后，再分别评估 BTC 与 ETH 是否需要独立的入场偏离、价格范围和止损参数。
