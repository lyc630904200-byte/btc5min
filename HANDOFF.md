# polybtc 交接文档

## 2026-07-27 真实首单交易接入

项目已增加第五个 Dashboard 页签“真实交易”，真实执行只跟随 `BTC 70/40策略` 的首单买入信号。模拟策略与真实账本并行运行；恢复单、盘中止盈、盘中止损和主动卖出均不会进入真实账户，真实首单持有到官方结算，赢家可自动兑付。

安全约定：

- 普通 `scripts/run-dashboard.cmd` 不加载私钥，只能运行模拟和无凭据观察。
- 真实交易使用 `scripts/run-dashboard-live.cmd`。每次进程启动都在可见终端中遮罩输入私钥，可选输入钱包地址；私钥只保存在本进程内存，不写配置、日志、Dashboard 状态或 SQLite。
- 加载凭据时 Dashboard 只允许绑定环回地址；真实控制接口要求本次进程生成的控制令牌，连接、授权和实盘武装还要求独立确认码。
- 每次进程启动都回到影子模式且不武装。紧急停止会写入独立账本并跨重启保留，实盘武装状态不会持久化。
- 地区限制、账户未连接、用户 WebSocket 未就绪、余额或授权不足、盘口过期/不可信、深度不足、最高限价、官方最小数量和任一风险上限都会阻止真实提交。
- 正式武装前必须累计至少 20 个唯一市场的有效影子信号且严重错误为 0。默认真实数量为 1 份，因此在官方最小量通常为 5 份的市场只能观察，不会提交。

真实配置位于 `real_trading`：

```yaml
enabled: false
order_quantity: 1
max_order_notional_usd: 5
daily_loss_limit_usd: 10
max_orders_per_day: 10
auto_redeem: true
shadow_required_signals: 20
```

保存真实配置后在下一场 BTC 市场生效。真实买单使用官方 Python SDK、FOK 和 `btc_recovery.max_entry_price_cents` 最高价格保护；BUY 的 SDK 金额使用当前可信多档盘口为配置份数模拟出的美元支出。提交意图先写入 `data/real-trading-ledger.sqlite3`，再发送交易所请求。提交结果不确定时不会自动重试同一市场，而是解除武装并通过账户成交记录对账。

真实接口：

```text
POST /api/real-trading/connect
POST /api/real-trading/prepare-allowance
POST /api/real-trading/arm
POST /api/real-trading/disarm
POST /api/real-trading/emergency-stop
POST /api/real-trading/resume
POST /api/real-trading/shadow-reset
```

当前实现测试使用假适配器，不连接真实账户、不发送真实订单。截至本节更新时没有加载用户私钥，也没有产生任何真实订单。

更新时间：2026-07-28 12:30（Asia/Shanghai）

## 项目目标

`polybtc` 是一个 Polymarket 5 分钟 BTC/ETH Up/Down 市场的模拟交易研究工具，并包含 BTC/ETH 跨市场自动匹配、BTC 70/40、BTC 动态胜率和受保护的真实首单执行模块。

程序并行监听 Binance `BTCUSDT`/`ETHUSDT`、Polymarket RTDS `BTC/USD`/`ETH/USD` 和两套 Polymarket CLOB 盘口，以各资产官网当前 5 分钟市场的开盘价作为阈值。BTC 与 ETH 使用互相隔离的市场、行情、盘口、仓位和统计状态。普通启动方式不加载私钥，所有策略只做本地模拟；只有使用独立 live 启动脚本、加载凭据并通过影子验证和手工武装后，真实首单模块才可能提交订单。

## 当前状态

- 工作目录：`D:\Users\Administrator\Documents\btc5fenzhong`
- 当前分支：`jiaoyi02`
- HEAD：`0e868b6 实体交易前`
- 跟踪分支：`origin/jiaoyi02`
- `jiaoyi02` 与 `origin/jiaoyi02` 当前指向同一提交。
- `0e868b6` 已提交并推送；其后仍有 BTC 动态胜率、真实交易、系统代理/前端重连、70/40 与动态策略并行等未提交修改，交接后不要 reset 或覆盖。
- Dashboard 正在运行：`http://127.0.0.1:8765/`
- WebSocket：`ws://127.0.0.1:8766/ws`
- 更新文档时进程 PID：`32784`
- 当前运行输出目录：`data\20260728T015713Z`
- 更新文档时 BTC 市场为 `btc-updown-5m-1785213000`，Chainlink tick 与当前秒同步，配置状态为 `active`，无待生效配置。
- BTC 动态胜率和 BTC 70/40 当前同时启用，共享可信 BTC 行情/盘口，但使用独立账本、持仓与统计。旧 BTC 单币入场和 BTC/ETH 新配对暂停，ETH 与已有仓位管理不受影响。
- 70/40 当前参数为：首单触发 92¢、首单最高限价 95¢、首单止盈 100¢、恢复单止盈 90¢、恢复触发/首单止损 0¢、恢复止损 20¢、首单 10 份、恢复单 400 份、窗口 `[240, 290)` 秒。
- 恢复单下单控制仍为“已停止”，状态已从 SQLite 恢复；首单继续运行，但不会新买恢复单。统计重置后累计观察 431 场、首单 247 笔、完成 246 场、胜率 95.53%、已实现净盈亏 `35.15786574 USD`，另有 1 场待结算。
- 动态胜率当前参数为：10 份、窗口 `[240, 290)` 秒、最低净优势 3¢/份、滑点预留 1.35¢/份、确认 2 秒且至少 2 次更新、最大概率修正 10 个百分点；训练快照为 240/252.5/265/277.5 秒。
- 动态模型已训练 149 个市场。统计重置后在线主单 23 笔、已结算 22 笔、净盈亏 `17.26711 USD`；公式对照 27 笔、已结算 26 笔、净盈亏 `9.63297 USD`。在线/公式 Brier 分别为 `0.094204/0.095081`。样本仍少，不应据此接入真实交易或频繁改参数。
- 真实交易配置当前关闭，未加载凭据、未武装、真实订单为 0。
- 最近完整测试：`260 passed`

当前工作区主要变更：

```text
 M HANDOFF.md
 M config.example.yaml
 M polybtc/btc_recovery.py
 M polybtc/cli.py
 M polybtc/clients.py
 M polybtc/config.py
 M polybtc/dashboard.py
 M polybtc/runner.py
 M pyproject.toml
 M tests/test_clob_ws.py
 M tests/test_dashboard.py
 M web/index.html
?? polybtc/btc_dynamic.py
?? polybtc/real_trading.py
?? scripts/run-dashboard-live.cmd
?? tests/test_btc_dynamic.py
?? tests/test_real_trading.py
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

当前开发分支 `jiaoyi02` 位于 `0e868b6`，并与 `origin/jiaoyi02` 一致。

程序持久配置仍为 `http://127.0.0.1:10808`，但该端口没有监听。`polybtc.clients.system_proxy_url()` 会显式读取 Windows 系统代理，HTTP 和外部 WebSocket 均按“Windows 系统代理 → 手工配置代理 → 直连”的顺序尝试；当前系统代理为 Clash Verge mixed 入口 `127.0.0.1:7897`。仓库 Git 代理仍是 10808；最近一次推送使用单次命令参数走 Clash，未修改 Git 持久配置：

```powershell
git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin jiaoyi02
```

用户所说的 `21079` 是旧 v2rayN 节点的远端服务器端口，不是本机监听端口，不要把程序配置直接改成 `127.0.0.1:21079`。

Dashboard 浏览器端增加了实时连接看门狗：连续 5 秒没有收到本地 WebSocket 消息时主动关闭并重连，标签页从后台恢复到前台时也会检查连接。倒计时每秒在浏览器本地刷新；服务端重启后旧页面通常会自动恢复，首次加载新代码时仍应刷新一次页面。

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
- `polybtc/btc_recovery.py`：BTC 70/40 本地模拟策略、高于首单价入场、恢复单、原子清仓、官方结算、独立 SQLite 账本与连续订单号。
- `polybtc/btc_dynamic.py`：BTC 动态胜率公式概率、在线逻辑回归、训练快照、动态限价、模拟订单和独立账本。
- `polybtc/real_trading.py`：真实首单影子验证、FOK 适配、账户/授权、风控、成交对账和独立账本。
- `polybtc/dashboard.py`：HTTP/WebSocket 服务和“保存后下一把生效”的运行时参数管理。
- `web/index.html`：Dashboard 前端。
- `tests/`：当前完整测试集，共 260 项。

## BTC 70/40 恢复策略

Dashboard 第四页“BTC 70/40策略”仅使用 BTC 的 UP/DOWN 实时多档盘口进行本地模拟，不发送真实订单。配置字段位于 `btc_recovery`：

```text
enabled
entry_price_cents              默认 70
max_entry_price_cents          默认 100（不限制）
target_price_cents             默认 80
recovery_target_price_cents    默认 80
recovery_trigger_cents         默认 40
stop_price_cents               默认 30
initial_quantity               默认 5
recovery_quantity              默认 15
entry_seconds_after_open       默认 0
exit_seconds_after_open        默认 300
```

首单只在 `[entry_seconds_after_open, exit_seconds_after_open)` 内判断当前盘口。任一方向可信卖一价严格大于 `entry_price_cents` 且不高于 `max_entry_price_cents` 时锁定该方向；完整数量的所有卖盘档位均不高于最高限价才成交，深度不足或价格超过限价时等待，不追高、不部分成交。等于首单价时不买；最高限价默认 100，旧配置自动兼容。若价格跌回首单价或以下则解除方向锁定。恢复触发后，反向数量仍按实时完整卖盘模拟买入。

首单完整可卖均价达到 `target_price_cents` 且含费净盈利大于 0 时直接退出；`target_price_cents=100` 是明确的关闭首单盘口止盈哨兵，即使盘口达到 100¢ 或提前到达配置出场时间也不产生首单卖出，未触发首单止损或恢复仓规则时会持有至官方结算。首单可卖均价跌到恢复触发价时完整买入反方向。恢复仓达到独立的 `recovery_target_price_cents` 且两边可原子清仓、合计含费盈利时退出；恢复仓跌到止损价时无视盈亏原子清仓。其他模式到达配置出场秒数后无视目标持续尝试完整清仓；默认 300 秒没有提前缓冲，到期未卖持仓转为官方结算。

启用恢复策略时，新的 BTC/ETH 配对和旧单币入场暂停，既有仓位仍按原规则管理；停用后配对自动恢复。BTC 动态胜率不会再暂停 70/40 首单，两套策略可以同时模拟。配置通过 `GET/POST /api/config` 和实时 `btc_recovery.config` 暴露，仅在下一场 BTC 市场原子生效。重启会恢复已成交持仓；未成交场次可直接根据重启后的当前盘口继续判断，不再因无法还原历史触发顺序而跳过。

持久账本为 `data/btc-recovery-ledger.sqlite3`。每次运行目录同步写入：

```text
btc_recovery_orders.csv
btc_recovery_rounds.csv
btc_recovery_results.csv
```

统计包含观察/无交易场次、首单/恢复单、直接盈利/恢复成功、30 止损、定时退出、官方/待结算、成交额、手续费、胜率和净盈亏。

恢复策略每条底层成交保留唯一 `order_number`，页面使用 `trade_order_number` 作为交易订单号。同一方向的买入和后续卖出共用一个展示号；若一场出现恢复单，则首单与恢复单各有一个展示号。“最近市场”显示该场的首单/恢复单编号。旧账本缺少新字段时，页面按对应方向的买入成交号回填。

页面提供“停止恢复单”即时控制按钮。停止后当前场和后续场次都不会新增 `recovery_entry`；`recovery_trigger_cents` 立即改作首单止损价，首单完整可卖均价小于或等于该值时以 `initial_stop` 退出。该值允许设为 `0`，此时明确关闭首单止损，即使盘口达到 0¢ 也不产生止损请求。深度不足时持久记录 `initial_stop_requested` 并持续重试，不允许部分卖出；已经成交的恢复仓继续按原规则退出。停止状态保存在 `btc_recovery_controls` 表并跨重启生效；同一按钮变为“恢复下单”，恢复后会取消尚未成交的首单止损请求，若恢复触发条件仍满足则重新执行恢复单逻辑。

官方结果轮询覆盖所有已结束且 `official_outcome` 为空的恢复策略场次，不再只处理 `PENDING_SETTLEMENT`。直接止盈、首单/恢复止损、定时退出、无交易和安全跳过场次在 Gamma 严格标记 resolved 后都会补记 UP/DOWN；这些已完成场次只更新官方方向，不修改原退出原因、兑付或已实现盈亏。仍有持仓的待结算场次继续按官方结果计算兑付并关闭。启动时每轮优先补记最近 50 场，历史空白会逐批完成。

实时 `btc_recovery.recent_orders` 返回最近 300 个逻辑订单，而不是最近 300 条底层成交。同一 `trade_order_number` 的买入和卖出合并为一条，分别返回买入/卖出时间、加权均价和金额，手续费为该订单全部成交手续费之和；多笔卖出按数量加权。Dashboard“最近订单”删除“买卖”和“原因”，一行展示完整买卖过程、官方结果和按方向计算的净盈亏。尚未卖出或仅等待官方结算时卖出字段显示 `--`。更新文档时接口返回最近 300 个历史逻辑订单。

Dashboard 卡顿原因已经定位并修复：此前每次盘口更新都会在顶层、BTC/ETH 资产快照和事件载荷中重复携带配对及恢复策略的完整历史，同时反复序列化最近订单。现在历史只保留在顶层状态，重型事件改为轻量通知，配对/恢复历史使用刷新缓存，高频 WebSocket 快照限制为每 250ms 一次。实测 `/api/state` 从约 1.43MB 降至 366KB，响应约从 500ms 降至 199ms，单核运行 CPU 从约 62% 降至 23%；在线 WebSocket 客户端、盘口更新和 stderr 均正常。

截至 `0e868b6` 已提交的恢复策略行为包括：

- 新增独立的“恢复单止盈”参数 `recovery_target_price_cents`，首单和恢复单可使用不同止盈价。
- 首单止盈允许设置为 100，表示不按盘口止盈；停止恢复单时首单止损允许设置为 0，表示不止损。
- 首单改为当前可信卖一价严格大于首单价时立即按实时多档盘口完整买入；等于时不买，且不要求此前价格低于首单价。深度不足且价格回到首单价或以下时解除锁定。
- 恢复触发条件已经满足时立即按反方向实时盘口买入，不再使用 `100 - recovery_trigger_cents` 作为限价。
- 同一方向的买入和卖出共用页面交易订单号，底层成交编号仍保持唯一；旧账本会自动回填展示号。
- “最近订单”已按交易订单号合并为一行，并保留最近 300 个逻辑订单的完整成交。
- 页面退出原因已汉化。

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

`web/index.html` 已删除价格偏离折线图及其前端历史数组、绘图函数和定时绘制调用；“价格偏离”和“有效偏离”两个实时数值仍保留。

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
最低配对盈利价差              10 美分
第二单最低盈利价差            1 美分
UP/DOWN 最小价格差            60 美分
配对入场窗口                  开盘后 [150, 300) 秒
每场配对上限                  1 组
严格方向控制                  开启
方向模式                      per_market

BTC 70/40策略                 开启
首单触发/最高限价/首单止盈     92/95/100 美分
恢复触发/恢复止盈/恢复止损     0/90/20 美分
首单/恢复单数量               10/400 份
恢复策略入场窗口              开盘后 [240, 290) 秒
恢复单下单                    已停止

BTC动态胜率                   开启
动态数量                      10 份
动态入场窗口                  开盘后 [240, 290) 秒
最低净优势/滑点预留            3/1.35 美分/份
连续确认                      2 秒且至少 2 次更新
最大概率修正                  10 个百分点

真实交易                      关闭
真实数量/单笔上限              1 份 / 5 USD
```

BTC 70/40 与动态胜率当前允许同时运行。任一策略启用时旧 BTC 单币入场暂停；动态胜率启用时 BTC/ETH 新配对暂停。两套 BTC 模拟策略不会共享订单、持仓或盈亏。

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

通常相同四腿卖盘组合快照最多开一组。快照指纹包含 BTC/ETH 的 UP/DOWN 四套完整卖盘价格和数量；数据库还有 `UNIQUE(interval_key, fingerprint)`，所以重启也不能在同一场重复记录完全相同的快照。“每场两阶段”模式例外：原始盘口指纹会分别加入首单/第二单阶段盐，因此同一快照可在两个阶段各成交一次，但同一阶段仍不能重复。无需修改 SQLite 表结构。等份数使“仅 BTC 腿赢”和“仅 ETH 腿赢”的兑付相同，单边净盈亏等于相同份数乘以入场价差。

严格方向控制支持七种模式：

- `per_market`：本场没有历史订单时选择当前达标方向中价差较高者，之后只等待相反方向；每个新对齐场次重新开始。
- `per_market_two_stage`：首单与 `per_market` 相同；首单成交后只等待反方向，忽略首单盈利价差和 UP/DOWN 价格差，仅使用 `second_order_min_spread_cents`。该模式强制严格交替和每场上限 2，并允许第二单复用首单原始盘口快照。
- `per_market_ab`：每个新场次固定从 A 开始，之后按 A→B→A→B 循环。
- `per_market_ba`：每个新场次固定从 B 开始，之后按 B→A→B→A 循环。
- `continuous_abab`：账本首次启用时随机确定 A/B 首单，之后跨场、跨重启持续 ABAB，只等待指定方向。
- `always_a`：始终只等待 A，即 `BTC UP + ETH DOWN`；B 即使价差更高也不成交。
- `always_b`：始终只等待 B，即 `BTC DOWN + ETH UP`；A 即使价差更高也不成交。

固定 A/B 模式允许同一市场重复相同方向，直到 `max_pairs_per_market`。第二组不要求价差先跌破门槛再重新穿越，也没有冷却时间；四套卖盘中任意一套发生变化形成新指纹后，只要目标方向仍达标，就可能在下一次约 20ms 合并批次中再次成交。固定模式不会修改 SQLite 中 `continuous_abab` 的持久方向。关闭严格方向控制时，固定模式值被忽略，每次仍选择当前达标方向中价差较高者。

`per_market_ab` 和 `per_market_ba` 在省略 `max_pairs_per_market` 时条件默认上限为 2；其他模式及全局默认仍为 1。Dashboard 手动选择这两个模式时会把上限预填为 2，之后仍可修改；加载已保存配置不会覆盖其显式上限。

`per_market_two_stage` 的第二单最低盈利价差默认 0 美分，范围 `-100..100`。Dashboard 仅在该模式允许编辑该输入框，同时锁定严格交替和每场上限 2；API 或配置文件显式传入其他上限或关闭严格交替也会被规范为 2/开启。首单后门槛立即切换；进程重启根据本场账本数量和最后方向恢复第二阶段，新市场重新进入首单阶段。

无论选择哪种模式，引擎仍要求四个盘口都可信、未过期且可执行。模块启用后，BTC/ETH 单币引擎停止新入场，但已经存在的单币仓位仍按原规则退出和结算；配对条件不足时不会恢复单币入场。

配对订单不做中途止盈止损，市场到期后每 2 秒查询 Gamma。只有 `closed=true`、`umaResolutionStatus=resolved` 且一一对应的 `outcomes`/`outcomePrices` 严格出现一个 `1`、其余为 `0` 时才结算；否则保持待结算并重试。净盈亏为中奖腿份数之和减去两腿成交额和两腿买入手续费。

完整配对订单保存在 `data/pair-match-ledger.sqlite3`，重启后会恢复待结算订单、场次计数和交替方向，重复结算不会重复记账。每组订单同时保存内部 UUID `order_id` 和面向用户的连续整数 `order_number`；旧账本启动时按 `opened_at, order_id` 自动补号，无需手工迁移。API、WebSocket、CSV 和 Dashboard 均返回或显示订单号，前端格式为 `#000001`，缺失时才回退到 UUID 片段。

截至 2026-07-23 本次更新，已结算账本结果为：

```text
BTC UP   + ETH UP      211 单 / 166 场
BTC UP   + ETH DOWN     45 单 /  39 场
BTC DOWN + ETH UP       49 单 /  38 场
BTC DOWN + ETH DOWN    184 单 / 145 场
合计                    489 单 / 388 场，全部已结算
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
- `GET /api/config`：当前实际生效参数以及待下一期生效参数；`pair_match` 包含 `min_leg_price_gap_cents` 和 `second_order_min_spread_cents`，`alternation_mode` 可返回七种配对模式。
- `POST /api/config`：保存前端允许修改的策略和风控参数；配对配置接受两个价差门槛及七种方向模式。
- `ws://127.0.0.1:8766/ws`：实时状态推送。

可在前端修改：

- 入场偏离、优势消失/止损偏离；
- 买入最低/最高价、止盈价差；
- 入场时间窗；
- 单笔模拟金额、最大净亏损、每市场最多交易数；
- 入场持续确认开关、反买开关。
- 配对开关、每腿金额、最低盈利价差、第二单最低盈利价差、UP/DOWN 最小价格差、开盘后运行区间、每场组数上限、严格方向控制及七种方向模式。

保存后不会改变当前市场使用中的参数，而是在检测到下一个市场后原子切换；保存值会持久化，重启后不会恢复成代码默认值。

Dashboard 已删除价格偏离折线图，并增加 BTC/ETH 页签。切换页签会显示对应资产的市场、阈值、Binance/RTDS 行情、持仓、统计和成交事件；盘口区域则在两个页面都固定同时显示两套盘口，BTC 在上、ETH 在下，各自包含 UP/DOWN 买入或卖出报价及更新时间。两个页签都显示同一个“BTC/ETH 自动匹配”面板，包括 A/B 实时成交均价、份数、金额、手续费、盈利价差、UP/DOWN 价格差、拒绝原因、四种结果预估 PnL、最近 20 场、最近 100 单和累计统计。模式下拉框包含每场择优、每场两阶段、每场先 A 后 B、每场先 B 后 A、连续 ABAB、一直选 A 和一直选 B；实时状态显示当前阶段和下一方向，订单列表优先显示连续订单号。两套单币状态不会互相覆盖。

## BTC 动态胜率

第六个页签“BTC动态胜率”是独立的本地模拟策略：

- 基础概率仅由 Chainlink 开盘价、当前价和 10/60 秒波动率计算。Binance 绝不与 Chainlink 比较绝对美元价格，只提供 1/3/5 秒标准化动量特征；Polymarket 价格只作为概率和成交成本。
- 在线逻辑回归初始系数为零，第一场在线概率等于公式概率。每场在当前入场窗口的起点及 1/4、2/4、3/4 位置保存四个等权快照；默认 270–290 秒窗口对应 270、275、280、285 秒。官方结果公布后才更新一次，重启不会重复训练。
- 在线概率相对公式概率最多修正 10 个百分点，最终概率限制在 1% 至 99%。模型概率同时决定方向和动态最高限价，不存在固定 91 美分门槛。
- 默认仅在开盘后 `[270, 290)` 秒内观察，完整买入 10 份。动态限价内深度必须完整，按实际多档均价和逐档手续费复核每份净优势，默认还需预留 1.35 美分滑点并保留 3 美分净优势。
- 同方向必须持续 2 秒且至少包含 2 个不同更新。每场主模型最多一单，成交后不止损、不恢复、不提前止盈，持有到官方结果。
- 每场同时保存一笔遵循相同风控的纯公式反事实订单，用于比较在线修正是否改善 Brier 分数、准确率和净盈亏。
- 启用后暂停旧 BTC 单币新入场和 BTC/ETH 新配对；BTC 70/40 可与动态胜率同时运行，两者共享可信 BTC 盘口但使用独立账本、独立持仓和独立统计。ETH 与已有持仓管理继续运行，动态订单不会进入真实交易接口。
- 独立账本为 `data/btc-dynamic-ledger.sqlite3`，保存市场、90 天训练快照与一秒价格缓冲、模型权重、在线/公式订单、官方结果和盈亏。
- “重置统计”只改变统计起点；“重置在线模型”要求确认码 `RESET_DYNAMIC_MODEL`，从下一场恢复零系数，不删除历史样本和订单。

`GET/POST /api/config` 和实时状态新增 `btc_dynamic`。保存动态配置后在下一场 BTC 市场原子生效；实时状态包含公式/在线概率、修正量、特征、动态限价、实际成交均价、手续费、净优势、确认进度和拒绝原因。

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
- `data/btc-recovery-ledger.sqlite3`：BTC 70/40 场次、连续订单号、成交、持仓、官方结果和统计控制。
- `data/btc-dynamic-ledger.sqlite3`：动态胜率市场、训练快照、模型权重、在线/公式模拟订单和官方结算。
- `data/real-trading-ledger.sqlite3`：真实交易意图、影子验证、风控状态、成交与兑付记录；当前没有真实订单。

## 测试覆盖

最近完整结果：

```text
260 passed
```

重点覆盖：

- BTC/ETH 标准 5 分钟 slug、时间边界、同资产当前/相邻市场选择。
- Next.js Flight/React Query 页面结构解析和错误数据拒绝。
- 当前官网 `openPrice`、上一期独立页面 `closePrice`、RTDS 精确开盘 tick 的严格匹配。
- RTDS tick 提前缓存、重复时间戳冲突、错误 symbol、超出接收窗口和后到冲突。
- 阈值验证失败、重试、过期和禁止交易。
- CLOB WebSocket、REST 兜底、旧响应防覆盖和多档盘口模拟。
- RTDS 静默超时主动重连与 RTDS 价格过期禁止入场。
- Windows 系统代理优先、配置代理回退和直连回退；Dashboard WebSocket 失活自动重连。
- 入场边界、盘口方向冲突、1 秒/3 次持续确认及关闭开关。
- 买卖手续费、滑点、完整深度硬止损。
- 盘口冲突 10 秒延迟、优势消失、止盈、最大持仓和结算。
- SQLite 每市场最多一笔及历史成交补种。
- Dashboard 保存后下一期生效和旧设置迁移。
- Dashboard BTC/ETH 状态隔离、ETH 官网价格结构、ETH Binance/RTDS symbol 路由。
- 配对多档等额成交、逐档手续费、价差边界、时间窗、四盘口完整性、相同快照防重、每场严格交替、固定 A/B 起始顺序、连续 ABAB 跨场/跨重启、固定 A/B 重复同方向、关闭交替择优和跨重启场次上限。
- UP/DOWN 价格差使用多档模拟成交均价、绝对差边界、A/B 对称性、严格方向不回退、关闭严格方向后的合格方向择优，以及 Dashboard 配置持久化。
- 每场两阶段首单择优、第二单独立盈利门槛、首单门槛失效、同快照分阶段成交、固定两单上限、新市场重置和跨重启恢复。
- 配对订单号自动迁移、连续分配、API/事件/CSV 序列化和 Dashboard 显示。
- BTC/ETH 四种官方结果、非严格 0/1 拒绝、幂等结算、配对账本恢复和 Dashboard 下一组对齐市场原子配置。
- BTC 70/40 突破入场、最高限价、恢复单控制、首单止损、订单合并、官方结算和跨重启恢复。
- 动态公式概率、在线逻辑回归、四个随入场窗口移动的训练快照、动态限价、净优势、确认、公式对照、Brier/PnL 与训练幂等。
- 真实交易影子门槛、FOK 适配、控制令牌、紧急停止、风险限制和账本幂等；测试使用假适配器，不发送真实订单。
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
- 普通启动仍只做模拟。真实执行基础设施虽然已接入，但当前 `real_trading.enabled=false`、无凭据、未武装且真实订单为 0；不得把模拟统计当作实盘验证结果。

## 建议后续顺序

1. 提交前完整复核当前未提交的动态胜率、真实交易、系统代理/页面重连和双策略并行修改；不要 reset 或覆盖用户已有改动。
2. 动态胜率保持固定参数继续模拟，至少累计 200 笔已结算在线主单后再判断在线修正是否稳定优于公式基线；当前只有 22 笔已结算。
3. 真实交易继续保持关闭。正式启用前按官方最小数量、真实余额/授权、地区限制和至少 20 个影子信号重新验收。
4. 增加独立阈值审计事件，按资产记录页面 open、上一期 close、RTDS candidate、差值和验证耗时。
