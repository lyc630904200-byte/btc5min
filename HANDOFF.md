# polybtc 交接文档

## 当前运行状态（2026-08-12，BTC领先预测已独占运行）

- 当前唯一 Dashboard 为 `http://127.0.0.1:8767`，WebSocket 为 `8768`，Python PID `28312`。旧实例 `8765/8766` 已停止，不要再按下方历史记录中的旧端口启动第二份服务。
- 当前生效策略为 `btc_lead_prediction.enabled=true`，固定 `SHADOW_ONLY`。`btc_weighted`、`btc_v8`、`btc_recovery`、`btc_dynamic`、`pair_match`、`orderbook_chase` 和 `real_trading` 均已关闭，不会发送真实订单。
- `standard_entry_enabled()` 已增加互斥保护：只要 BTC领先预测启用，BTC/ETH 通用纸面策略也暂停，避免专用策略关闭后仍出现无关模拟入场。对应回归测试已加入 `tests/test_runner.py`。
- 当前无单不是成交失败。策略尚未创建 BUY 尝试，主要在 `waiting_for_sources` 和 `prediction_warmup` 之间切换：至少需要两个交易所同时满足来源年龄和交易所时间年龄均不超过 `0.5秒`；Binance、Coinbase、Kraken 的网络延迟接近该边界，健康源数量会瞬时波动。
- 盘口响应预热要求 UP、DOWN 各有至少 `30` 个主周期已到期有效样本。2026-08-12 23:20 左右抽样为 UP `23/30`、DOWN `24/30`；样本用于学习“预测概率变化 -> 对应方向买一变化”，从而计算预计重定价、P95净收益和目标卖价。预热数据写入 `data/btc-lead-prediction-ledger.sqlite3`，正常重启不会清零。
- 预热完成后仍须同时满足：500毫秒综合价格变化至少 `1.5bps`、至少两个健康现货源同向、持续 `0.8秒/3次更新`、来源离散度不超过 `2bps`、3秒/5秒预测方向一致、预计买一上涨至少 `6美分`、P95净收益至少 `2美分/份`，以及价格、价差、深度和剩余时间门槛。
- 全量回归最新结果为 `392 passed`。当前代码和运行配置均已加载；若只是暂时没有影子单，应先看 `/api/state` 下 `btc_lead_prediction.status`、`last_reason`、三家 `sources.*.reason` 和 `book_projection.UP/DOWN.response_samples`，不要直接判断服务卡死。

## 最新状态（2026-08-11，BTC动态加权三档反转递进）

- `btc_weighted` 新增 `reversal_sequence_enabled`，默认关闭。关闭时原有按前/中/后时间段入场完全不变；开启时忽略三段时长，按第一个尚未使用且已开启的档位依次使用金额，仍最多提交三次Observed/P95影子入场决策。
- 首笔允许当前严格领先方向正常确认入场；之后只有评分领先方向从上一笔BUY方向切换到对侧才进入反向识别。反转方向仍需通过总分、领先分、确认时间/更新数、预热、剩余时间、风险暂停和全部盘口门槛。识别期间若方向切回上一笔方向，立即清除确认且不消耗档位；分数相等或缺失不判定方向。
- 轮次新增并持久化 `last_entry_direction`；重启遇到旧轮次时会从最近一次BUY尝试恢复该方向。状态新增 `reversal_sequence`，包含当前状态、领先方向、上一笔方向、识别方向、下一档及金额、已完成数和剩余可用档位。BUY决策详情同时记录入场模式、前一方向、反转方向和顺序档位。
- 页面新增“反转递进模式”开关和运行状态。开启后前/中/后三个时长输入立即禁用并保留原值；已开启档位的金额仍可编辑，某档自身关闭时该档时长和金额都禁用。关闭反转模式后，已开启档位的时长恢复编辑；保存仍校验三段时长合计300秒，并在下一场BTC生效。
- 当前仍为 `SHADOW_ONLY`，未修改真实下单、BTC V8、反向评分退出或官方结算。Dashboard 已重启在 `http://127.0.0.1:8765/`，进程PID `8860`，当前配置中的反转递进开关为关闭。
- 验证：新增首笔/两次反转、同方向不追加、反转取消重认、关闭档位跳过、无可用档位、严格方向及重启恢复测试；全量回归 `381 passed`。Python编译、前端JavaScript语法及 `git diff --check` 通过；Chrome桌面 `1440x1000` 验证开关联动、状态字段、无文本裁切和无横向溢出。

## 最新状态（2026-08-11，BTC动态加权三时段影子入场）

- BTC动态加权入场改为固定三段配置 `entry_segments`，默认前段 `0-80秒/$1`、中段 `80-220秒/$3`、后段 `220-300秒/$5`；时长必须为正整数且合计300秒，金额大于0且最多两位小数。旧的全局 `quote_amount_usd` 和 `max_entries_per_market` 已从动态加权配置移除。
- 每段最多提交一次Observed/P95双轨影子决策，未触发不补单，跨段清除未完成确认；时段在提交决策时即记为已使用。每段独立选择当前领先方向，允许后续时段买入对侧。轮次持久化 `used_entry_segments`，订单和仓位记录 `entry_segment`、`segment_quote_amount_usd`，重启时可从旧订单时间推断已使用时段。
- 动态加权BUY现在严格按该段美元金额模拟FOK。盘口 `min_order_size` 只保留 `below_book_min_order_size` 诊断，不再拦截少于5份的影子市价买入；决策盘口和P95延迟盘口仍要求在最高限价内完整成交全部美元金额。SELL、真实交易适配器和BTC V8未改。
- 页面已增加前/中/后三段独立启用开关、时长和金额输入、当前时段、三段状态、订单/仓位时段及金额；关闭某段后对应时长和金额立即禁用，该段不确认也不下单。最小份数在门槛表显示为“诊断”。保存前前端校验三段合计300秒，保存反馈沿用“已保存，下一场BTC生效”。仅检查桌面布局。
- 运行验证：Dashboard `http://127.0.0.1:8765/` 已重启并运行新代码。下一场前段成功提交 `$1.00`，P95影子成交 `1.470588` 份，盘口最小量为 `5` 份，诊断 `below_minimum=true`，订单状态 `MATCHED`，证明小额美元路径未被份数门槛拦截；策略仍为 `SHADOW_ONLY`。
- 验证：动态加权专项 `24 passed`，全量回归 `377 passed`；Python编译、前端JavaScript语法和 `git diff --check` 通过。Chrome桌面 `1440x1000` 检查三个开关、六个输入值、禁用联动、当前时段、三段状态和保存反馈均正确，无必填错误、页面错误或横向溢出。

## 最新状态（2026-08-11，BTC动态加权风控与页面修订）

- BTC动态加权仍是独立 `SHADOW_ONLY` 策略，不接入真实下单，也不改变 BTC V8。退出现在使用可撤销的短时意图：反向评分、价格和时间条件任一消失就取消；卖盘深度不足只在意图有效期内等待新盘口，默认 TTL 为 `3秒`。
- 新增入场保护：开场后 `10秒` 才允许入场，盘口/Chainlink 指标至少预热 `10秒`，且至少 `8` 个样本；新增评分退出窗口（开场后默认 `60秒`）和评分退出最低价 `25美分`，低于该价不触发评分卖出。退出决策快照保存对侧分数、持仓侧分数、领先分、可卖价、时间、组件和权重。
- 每个卖出意图和延迟卖出尝试都绑定精确 `position_id`，避免同市场同方向多仓时错配仓位；旧账本缺少该字段时仍保留兼容回退。
- 每份动态加权配置生成短版本号。统计区同时展示累计结果和当前参数版本结果；当前版本增加完成数、近10单盈亏、P95连亏、最大回撤。新增 P95 影子保护诊断：滚动亏损和连亏暂停只拦截新入场，不影响已有仓位和官方结算。
- Dashboard 参数页已增加开场等待、预热、退出窗口、最低退出价、意图 TTL、P95保护及其阈值；门槛表和订单/仓位历史显示具体参数。保存反馈会完整显示“保存中/保存成功/保存失败”，避免实时刷新立即覆盖成功提示。
- 当前运行核对：Dashboard `http://127.0.0.1:8765/` 正常，BTC动态加权运行中；当前场 `btc-updown-5m-1786407300`，配置版本 `14f4944b91ec`，开场等待 `10秒`、预热 `10秒`、评分退出截止 `60秒`、最低评分卖价 `25美分`、意图 TTL `3秒`，当前有2条影子仓位。当前保存配置中的 P95保护开关为关闭，代码和示例默认值仍为开启，可在页面按需打开。
- 验证：全量回归 `371 passed`；Python 编译、前端 JavaScript 语法、桌面浏览器 `1440x1000` 检查通过，表单无必填缺失、无可见文字溢出、评分曲线画布有有效像素；桌面控制脚本重启与状态检查通过。

## 最新状态（2026-08-09，BTC动态加权独立影子策略）

- 顶部新增独立分页“BTC动态加权”，不修改、不替代 BTC V8。策略固定为 `SHADOW_ONLY`，默认 `enabled=false`，不会连接签名器或发送真实订单；运行中即使关闭自动入场也持续展示评分，方便先观察再决定参数。
- UP、DOWN 分别计算 0-100 分：时间、目标方向合约价格、Chainlink TWAP/官方目标价距离、盘口3秒速度、价差3秒速度、盘口加速度、价差加速度。时间和价格按方案锚点线性插值；速度及加速度使用 `t-3/t-6`（允许 `+-1秒`）和最近60秒标准差转正态 CDF 分数，缺少历史时不允许入场。
- 权重按剩余 `300/180/60秒` 三组基础值线性插值，并根据 Chainlink `10秒/60秒` 波动率比动态偏移，最终使用3秒 EMA 平滑并归一化到100%。页面逐项展示原值、标准分、权重和贡献，并绘制最近5分钟 UP/DOWN 总分及入场阈值曲线。
- 入场规则为总分 `>=70`、领先 `>=8分`、持续 `>=2秒/3次更新`、每场最多一次。硬保护包括 Chainlink/盘口年龄、深度可信、价差、90美分内完整成交、市场最小数量、固定 `$5`、每跳风险 `$0.50`、买价 `15-90美分` 和剩余10秒停止新买入。手续费、滑点、距离概率和诊断净优势只展示，不参与拦截。
- 每个信号同时创建 Observed、P95 两条延迟影子 FOK；两条轨道共用现有 CLOB 延迟探针但账本完全独立。退出只使用反向评分：至少持有15秒，对侧 `>=70` 且领先10分，持续 `3秒/3次更新`；卖盘不足等待新盘口重试，越过市场结束时间后停止评分和卖出，等待官方结算。
- SQLite 独立账本为 `data/btc-weighted-ledger.sqlite3`，记录场次、每秒评分、尝试和仓位。切场、到期、官方结算或进程重启时，未完成延迟尝试会标记为明确的 `UNMEASURABLE` 原因，不会误用下一场盘口；设置通过 `/api/config` 保存，状态通过 `/api/state` 和 WebSocket 顶层 `btc_weighted` 发布，并从下一场 BTC 生效。
- Dashboard 包含市场/目标/TWAP/剩余时间/波动率/数据年龄、双向总分、七项贡献、确认进度、全部门槛、Observed/P95 统计、持仓/尝试历史和完整参数表单。保存按钮会显示“保存中.../已保存/保存失败”，同时提示下一场 BTC 生效。
- 验证：完整回归 `366 passed`；Python 编译、前端 JavaScript 语法、509个 HTML ID/脚本引用检查均通过。真实行情浏览器验收覆盖 `1440x1000` 与 `390x844`，无页面横向溢出或脚本错误，曲线画布有有效像素，七项组件和30个双向门槛均已渲染。Dashboard 运行于 `http://127.0.0.1:8765/`，API 确认 `mode=SHADOW_ONLY`、`enabled=false`、零仓位零尝试。
- 运行切换（2026-08-09 23:50）：用户已正式启用 BTC动态加权，并关闭 `btc_v8`、`orderbook_chase`、`btc_recovery`、`btc_dynamic`、`pair_match` 和 `real_trading`。配置在新场边界生效后已重启，Dashboard Python PID `81612`；共享 CLOB 延迟探针仍由动态加权独立采样。当前场 `btc-updown-5m-1786290300` 已完成一次入场，Observed/P95 各有一条影子仓；上一场两条仓位正在 `HOLD_TO_SETTLEMENT`，均由独立账本继续管理。
- 桌面 `C:\Users\Administrator\Desktop\BTC程序控制.cmd` 已同步为“BTC Dynamic Weighted Control”：菜单只保留启动、停止、重启、动态加权状态和打开页面；启动会验证 weighted-only 配置，停止会检查 `OPEN/EXIT_PENDING/HOLD_TO_SETTLEMENT` 仓位及未完成延迟尝试并先警告，状态会显示双向分数、领先方向、入场计数、仓位和延迟探针。旧脚本备份为 `BTC程序控制.cmd.bak-20260809-weighted`；`status` 与 `start` 实际执行验证通过。

## 最新状态（2026-08-09，BTC V8 再放宽）

- “停止新买入（剩余秒）”不再要求覆盖最短或最长持有时间，只保留 `最短持有 <= 最长持有`。例如停止买入 `10秒`、最短持有 `30秒`、最长持有 `90秒` 现可正常保存；晚入仓跨过5分钟市场结束线后，主 V8 停止主动卖出，Observed/P95 影子轨道转为 `HOLD_TO_SETTLEMENT`，不发送卖单并等待官方结果，最终以 `official_settlement` 结算。当前活动参数仍保持用户原值 `60/30/90`。
- 订单稀少的 60 次实时采样中，方向信号通过 `59/60`；主要瓶颈是盘口经济性：旧诊断 `depth_below_limit` 为 `45/60`，`book_depth_untrusted` 为 `14/60`。本次把滑点预留 `1.0 -> 0.5美分`、有效净优势 `1.0 -> 0.5美分`、离场手续费预留比例改为 `0`，动态最高买价约可提高 `3美分`；完整离场手续费估算仍保留在 diagnostics 中。
- 最晚入场从剩余 `>94秒` 放宽为 `>60秒`，每场增加 `34秒` 可入场时间。价格带仍为 `15-90美分`、每场最多买入2次、每跳风险预算 `$0.50`，盘口深度可信检查和最近3秒同方向盘口趋势保护均未关闭。
- 买价不足的诊断已细分：当卖一价本身高于动态最高限价时返回 `best_ask_above_limit`，并携带 `book_best_ask`、`limit`、`limit_gap_cents`；其余限价内深度不足继续返回 `depth_below_limit`。Dashboard 已增加对应中文参数展示。
- `BtcV8Config`、`config.example.yaml`、`data/dashboard-settings.json`、Dashboard 表单和保存接口现严格对应同一组 `59` 个字段。运行时覆盖层曾保留旧的 `1.0/1.0/94`，现已通过下一 BTC 场生效机制修正并清空 pending。
- 运行核对：Dashboard `http://127.0.0.1:8765/`，Python PID `74328`，启动器 PID `67540`。新场 `btc-updown-5m-1786270500` 的执行引擎和 `/api/config` 均返回 `slippage_reserve_cents=0.5`、`exit_fee_reserve_fraction=0`、`min_effective_edge_cents=0.5`、`min_entry_remaining_seconds=60`，配置状态为 `active`；真实交易关闭。该场直到60秒保护线前未形成合格方向，所以没有订单，这不是新价格门槛未加载。
- 后续实盘行情验证：下一场 `btc-updown-5m-1786270800` 已按新参数触发1次 DOWN 影子买入，动态限价 `48美分`，Observed 轨成交 `47美分`，P95 延迟轨成交 `45美分`。两条轨道是同一信号的延迟对照，不是主 V8 重复入场。随后盘口转向 UP 时被 `book_depth_untrusted` 拦截，表明深度保护仍在生效。
- 验证：新增极晚入场跨结束线、禁止卖出和官方结算测试；V8/盘口目标测试 `49 passed`，完整回归 `351 passed`，Python 编译、前端 JavaScript 语法及59字段一致性检查均通过。V8 保存按钮已有“保存中/已保存/保存失败”反馈。

## 当前生效状态（2026-08-09，BTC V8 全量大方向参数版）

BTC V8 已删除旧的自动模型、手动买卖边、训练快照、概率修正和旧时间窗口配置，重新定义为仅使用30秒大方向的一套参数。`BtcV8Config`、`config.example.yaml`、`data/dashboard-settings.json`、Dashboard 表单和保存接口均严格对应同一组58个字段；旧字段提交会被 Dashboard 拒绝，旧配置字段不会再序列化。

- 领先信号公式：`time_weighted_average_30s(spot_momentum_10s + 0.5*spot_momentum_30s + 0.35*calibrated_spot_twap_gap - twap_drift_10s - 0.5*twap_drift_30s)`。
- 入场门槛：方向幅度 `>=0.03bps`、强度 `>=0.05 sigma`、目标概率变化 `>=0.2pp`、目标方向概率 `>=40%`、至少1所方向支持、至少1所现货新鲜、有效净优势至少1美分、买入确认 `1.0s/2次更新`。
- 买入限制：价格 `15-90美分`；低价惩罚 `max(0, 0.45-avg_price)*1.5`；高价优先评分 `avg_price*1.0 + effective_edge*0.30`；每跳最大风险预算 `$0.50`；每个5分钟市场最多买入2次；剩余时间必须 `>94s`。
- 方向与数据窗口：短/长方向窗口 `10s/30s`；方向平均 `30s`、最小跨度 `20s`、最少15样本、最大样本间隔 `2.5s`；盘口趋势 `3s`、最小跨度 `1s`；Chainlink 最大年龄 `5s`；现货过期 `2s`。
- 持仓退出：卖出确认 `0.5s/1次`，反向确认 `10s/2次`；最短/最长持仓 `30s/90s`；最低止盈 `$0.05`；硬止损 `-$1.50`；任何时刻紧急止损 `-$2.00`；利润回撤参数 `$0.25/$0.15/35%`。
- 运行状态：Dashboard `http://127.0.0.1:8765/`，Python PID `69988`，启动器 PID `74932`。真实交易关闭，主 V8 与影子跟单核对时均无开放仓位；运行 API 返回58个新字段及 `min_buy_price_cents=15`、`max_buy_price_cents=90`。
- 验证结果：Python 编译与页面 JavaScript 语法检查通过；V8/Dashboard/runner/跟单定向测试 `114 passed`；完整回归 `349 passed`。

本次全量替换还移除了 V8 页面中的“模型版本、训练市场、Brier、准确率、评估快照”等旧模型展示，运行模式固定显示“30秒大方向”。Chainlink 过期拦截会立即清除未完成买入确认，并携带实际年龄、最大允许年龄和行情时间戳。

Dashboard 的 V8 保存按钮已增加明确反馈：点击后显示“保存中...”，成功时绿色显示“已保存”并在状态栏提示生效时机，失败时红色显示“保存失败”并展示错误；约2.2秒后按钮恢复。状态栏带 `role=status` 与 `aria-live=polite`。前端语法检查、Dashboard `26 passed`、完整回归 `349 passed`。

续接复核（2026-08-09）：已重新读取本文件并核对当前运行 API。Dashboard 仍在 `http://127.0.0.1:8765/`，PID `41676` 存活；`/api/config` 确认 `btc_v8.enabled=true`、`btc_v8.orderbook_chase_mode=true`、`orderbook_chase.enabled=true`、`real_trading.enabled=false`、`pair_match/btc_recovery/btc_dynamic=false`，`max_entries_per_market=2`，`min_fresh_spot_exchanges=2`。`/api/state` 核对时无 open position，追赶状态为 `v8_signal_following`，当前场因剩余时间不足处于 `chase_entry_too_late` 属正常保护。`data/dashboard-live.stderr.log` 尾部仍有旧的 `websockets 15.0.1 recv_messages` 异常记录，但该日志最后写入早于后续 dashboard stdout 重启/输出；代码中主要 WebSocket 源均已走 `connect_websocket()` 的 `ProxySafeClientConnection`。

本次修正：`web/index.html` 的 Chase 页旧文案“0.25秒 / 2次更新”已改为当时规则“买入 2.0秒/2次 · 反向退出 10秒/2次”，并将 V8 每场最多买入输入框初始值从 3 同步为 2。静态前端 ID 检查 `missing_refs=[]`。验证命令均使用 Codex bundled Python：`py_compile` 通过；`tests/test_orderbook_chase.py` 为 `8 passed`；`tests/test_btc_v8.py tests/test_dashboard.py tests/test_runner.py tests/test_real_trading.py` 为 `112 passed`；完整回归 `346 passed`；文案修正后 `tests/test_dashboard.py tests/test_orderbook_chase.py` 为 `34 passed`。

2026-08-09 用户要求买入确认改为1秒：`CHASE_BUY_CONFIRMATION_SECONDS=1.0`，仍要求 `2次更新`；`config.example.yaml`、`data/dashboard-settings.json` 和 Dashboard Chase/V8 前端默认显示已同步。反向退出确认仍为 `10s/2次更新`。验证：`py_compile` 通过，`tests/test_btc_v8.py tests/test_orderbook_chase.py tests/test_dashboard.py` 为 `70 passed`，完整回归 `346 passed`，静态前端 ID 检查 `missing_refs=[]`。空仓重启后监听 PID 为 `48716`，`/api/config` 返回 `buy_confirmation_seconds=1.0`、`buy_confirmation_updates=2`、`real_trading=false`。

2026-08-09 用户要求按 A 方案把买入核心排序改为高价优先：合格门槛仍保留净优势 `>=3¢`，但最终候选选择从单纯 `edge_per_share` 最大改为 `buy_score = avg_price*1.0 + edge_per_share*0.30` 最大；候选诊断新增 `buy_score` 和 `buy_score_formula`。这会让仍有正净优势的高价合约优先于低价高毛优势合约。验证：`py_compile` 通过，`tests/test_btc_v8.py` 为 `38 passed`，完整回归 `348 passed`，静态前端 ID 检查 `missing_refs=[]`。空仓重启后监听 PID 为 `57288`，`/api/config` 仍返回 `buy_confirmation_seconds=1.0`、`buy_confirmation_updates=2`、`real_trading=false`。

2026-08-09 用户要求继续改公式，解决低价票看起来 edge 很大导致“爱买小”的问题：orderbook chase 买入候选现在保留旧公式为 `unpenalized_edge_per_share`，再扣 `low_price_penalty_per_share = max(0, 0.45 - avg_price) * 1.5`，最终 `edge_per_share/effective_edge_per_share` 用于 `>=3¢` 入场判断和 `buy_score` 排序。#002241 类型的 `26¢` 票即使旧净优势约 `19¢`，也会因约 `28.5¢` 低价惩罚被 `actual_edge_below_threshold` 拦截。Dashboard 候选文案会显示“低价惩罚 x¢”。验证：`py_compile` 通过，`tests/test_btc_v8.py` 为 `39 passed`，完整回归 `349 passed`。空仓重启后监听 Python PID 为 `54496`，运行 API 已就绪且无主仓/影子开放仓位。

2026-08-09 用户要求降低“组合方向幅度不足”条件：`CHASE_MIN_DIRECTION_SIGNAL_BPS` 从 `0.10bps` 降为 `0.05bps`，其它信号质量、盘口和经济性门槛保持不变；方向诊断新增 `min_direction_signal_bps`，便于直接看到当前阈值。实时核对时曾出现方向幅度约 `0.409bps`、强度 `0.819 sigma`、概率变化 `1.91pp` 已全部通过，但最终被 `actual_edge_below_threshold` 拦截，说明少下单还会受有效净优势（含低价惩罚）影响。验证：`py_compile` 通过，`tests/test_btc_v8.py` 为 `39 passed`，完整回归 `349 passed`。空仓重启后监听 Python PID 为 `68684`，运行 API 返回 `min_direction_signal_bps=0.05`。

2026-08-09 用户要求将有效净优势门槛改为1美分：orderbook chase 的 `CHASE_MIN_NET_EDGE` 从 `0.03` 降为 `0.01`，即 `effective_edge_per_share >= 0.01` 即可通过该项。低价惩罚公式、高价优先评分和其它入场保护保持不变。验证：`py_compile` 通过，`tests/test_btc_v8.py` 为 `39 passed`，完整回归 `349 passed`。空仓重启后监听 Python PID 为 `73016`，运行 API 返回 `buy_edge_cents=1.0`。

2026-08-09 根据45秒实时拦截采样降低主要频率瓶颈：最小信号强度 `0.20 -> 0.10 sigma`，目标概率变化 `1pp -> 0.3pp`，最少新鲜现货源 `2 -> 1`；方向幅度 `0.05bps`、有效净优势 `1美分`、低价惩罚及其它保护不变。诊断新增 `min_signal_sigma` 和 `min_probability_move`。一路新鲜现货可继续决策、零路仍拦截的测试已覆盖；`py_compile` 通过，V8/Dashboard 目标测试 `65 passed`，完整回归 `349 passed`。空仓重启后监听 Python PID 为 `71128`，新一轮运行 API 已确认 `min_signal_sigma=0.1`、`min_probability_move=0.003`、`required_fresh_spot_count=1`、`buy_edge_cents=1.0`，真实交易保持关闭。

2026-08-09 用户要求再次放宽信号触发：方向幅度 `0.05 -> 0.03bps`、最小信号强度 `0.10 -> 0.05 sigma`、目标概率变化 `0.3pp -> 0.2pp`。最少新鲜现货源继续为1，有效净优势继续为1美分，其余盘口和风险保护不变。`py_compile` 通过，V8目标测试 `39 passed`，完整回归 `349 passed`。空仓重启后监听 Python PID 为 `71772`，运行 API 已确认 `min_direction_signal_bps=0.03`、`min_signal_sigma=0.05`、`min_probability_move=0.002`。

以下章节保留为变更历史。若旧章节中的“两所方向支持”“每场最多1次”或旧 PID 与本节冲突，以本节和最上方最新变更记录为准。

## 2026-08-09 BTC V8 TWAP方向支持改为一所

按用户要求，30秒TWAP组合方向信号的同向支持门槛从至少两所改为至少一所，即 `CHASE_MIN_SUPPORTING_SOURCES=1`。只要任一交易所完成连续30秒方向平均并通过其余幅度、强度和概率门槛，就可以独立确定UP/DOWN；总体数据健康仍要求至少两所现货交易所同时新鲜，未改为单源行情运行。反向退出的方向支持也同步为一所，并继续要求反向信号持续确认 `10s/2次更新`。其他价格带、每场2单、持仓和止损规则不变。

验证：BTC V8/追赶定向测试 `44 passed`，Dashboard/runner/相关策略全回归 `139 passed`。空仓重启后的 Dashboard PID 为 `41676`，仍为 `SHADOW_ONLY`；运行 API 已确认方向 `required_sources=1`、总体新鲜现货 `required_fresh_spot_count=2`、每场买入上限2。

## 2026-08-09 BTC V8 每场买入上限改为2

按用户要求，30秒大方向追赶模式每个5分钟市场的买入硬上限从1次改为2次。代码级 `CHASE_MAX_ENTRIES_PER_MARKET=2`，追赶模式直接使用该硬上限，因此即使恢复的旧场次快照仍记录1也不会错误阻止第2次；配置默认、`config.example.yaml` 和 Dashboard 活动设置均同步为 `max_entries_per_market=2`。其他信号、价格带、动态份数、持仓和退出规则不变。

验证：BTC V8/追赶定向测试 `44 passed`，Dashboard/runner/相关策略全回归 `139 passed`。空仓重启后的 Dashboard PID 为 `40768`，仍为 `SHADOW_ONLY`；运行 API 已确认新场 `btc-updown-5m-1786243200` 的场次快照与活动配置均为2，当前入场计数为0。

## 2026-08-09 BTC V8 30秒大方向持仓版

根据关闭绝对方向过滤后约一小时影子账本的亏损分析，V8 已从“30秒方向入场、几秒追赶止损”统一为较长持仓版本。绝对大方向否决继续关闭，方向仍由每家交易所30秒时间加权组合信号的中位数决定，但新增质量、价格、仓位、重入和退出保护。

- 入场硬门槛：组合方向幅度 `>=0.10bps`、强度 `>=0.20 sigma`、目标概率变化 `>=1pp`、至少两路完成平均的交易所同向、目标方向概率 `>=40%`；Polymarket 同方向最近3秒不下跌、净优势至少3美分、买入确认 `2.0s/2次` 保持不变。
- 买入价格带固定为 `15-70美分`。盘口每跳最大份数风险预算为 `$0.50`，按 `0.50 / tick_size` 计算份数上限；必要时不再强制花完配置的 `$5`，候选中记录 `risk_sized`、`max_quantity_by_tick` 和实际 quote。
- 每场代码级硬限制最多1次买入，配置默认、启动基线和 Dashboard 保存值均同步为 `max_entries_per_market=1`，从根源上阻止30秒平均信号未消退时连续追损。
- 持仓改为最短 `30s`、最长 `90s`，入场要求剩余时间 `>94s`。普通硬止损改为持仓满30秒后 `-$1.50`；任何时刻达到 `-$2.00` 触发 `chase_emergency_stop`。
- 反向退出要求当前反方向仍通过全部入场信号门槛且至少两路来源支持，并连续确认 `10s/2次更新`；主V8仓位与 Observed/P95 延迟执行仓均执行相同确认规则。普通目标追上、利润回撤和超时规则继续保留。
- 前端新增“目标方向概率低于40%”“买入价不在15至70美分”“紧急止损”原因，并把超时说明更新为90秒。

验证：`py_compile` 通过；BTC V8/追赶定向测试 `44 passed`，Dashboard/runner/相关策略全回归 `139 passed`。新增覆盖低目标概率、低价合约动态缩量与拒绝、30秒前普通亏损保护、即时紧急止损和两路反向支持。空仓重启后的 Dashboard PID 为 `35204`，仍为 `SHADOW_ONLY`；运行 API 已确认当前场与活动配置均为 `max_entries_per_market=1`、`required_sources=2`、绝对方向过滤关闭。首个运行样本只有 Coinbase 完成30秒平均，因此正确停在 `chase_consensus_insufficient`，新门槛已生效。

## 2026-08-09 BTC V8 关闭绝对大方向否决

按用户要求，`chase_absolute_direction_mismatch` 买入否决已关闭。UP/DOWN 现在完全由每家交易所30秒时间加权组合信号的中位数决定；Chainlink 当前30秒TWAP相对本轮开盘价的方向、幅度和对应公式概率继续写入 diagnostics，但只展示、不再阻止买入。方向幅度 `0.01bps`、强度 `0.02 sigma`、目标概率变化 `0.1pp`、至少一路方向支持、Polymarket 同方向盘口不下跌、净优势3美分和 `2.0s/2次` 确认均保持不变。新增运行诊断 `absolute_direction_filter_enabled=false`。

验证：反向价格与反向公式概率的测试样例仍可得到 `chase_signal`；`py_compile` 通过，BTC V8/追赶定向测试 `42 passed`，Dashboard/runner/相关策略全回归 `137 passed`。空仓重启后的 Dashboard PID 为 `32288`，运行 API 已确认 `absolute_direction_filter_enabled=false` 且方向信号可以返回 `chase_signal`；当前仍为 `SHADOW_ONLY`。

## 2026-08-09 BTC V8 30秒方向信号增频一档

用户反馈下单太少后，运行诊断显示弱信号阶段常被方向幅度/强度拦截，开盘价附近极小反向波动也会触发绝对方向不一致。此次不改30秒组合方向公式，不降低盘口和经济性保护，只放松方向触发一档：组合方向幅度 `0.03bps -> 0.01bps`、最小信号强度 `0.05 sigma -> 0.02 sigma`、目标概率变化 `0.3pp -> 0.1pp`。新增 Chainlink 相对本轮开盘价 `+-0.25bps` 中性区：位于中性区时由30秒平均组合信号决定 UP/DOWN，不因几乎为零的开盘偏移判绝对方向冲突；一旦离开中性区，Chainlink 开盘方向和对应公式概率仍必须严格同向。

保持不变：30秒时间加权平均、至少两路新鲜现货、至少一路方向支持、Polymarket 最近3秒同方向盘口不下跌、净优势至少3美分、买入确认 `2.0s/2次`、最短/最长持仓 `12s/45s`、`$1.00` 硬止损、入场剩余时间 `>55s`，以及 `SHADOW_ONLY`。修改后 `py_compile` 通过，`tests/test_btc_v8.py + tests/test_orderbook_chase.py` 为 `42 passed`，Dashboard/runner/相关策略全回归为 `137 passed`。空仓重启后的 Dashboard PID 为 `32752`；运行 API 已确认两路30秒平均就绪，并返回 `absolute_direction_neutral_bps=0.25`，说明新代码已经加载。

> 最新有效状态以本节为准；后面的 2026-07-28 及更早内容保留为历史记录，其中分支、PID、参数和统计已经过期。

## 2026-08-09 BTC V8 TWAP方向信号30秒平均

V8 的最终方向信号已从“每次评估的瞬时组合信号”改为“每家交易所最近30秒组合信号的时间加权平均”，再对各家平均值取中位数。目标概率、方向支持数、UP/DOWN方向和买入确认key均统一使用平均信号。

- 每家交易所独立记录瞬时组合信号，每个本机接收时间整秒最多一个样本，避免高频盘口更新造成样本权重偏差。
- 平均窗口 `30s`，至少 `15` 个样本且连续时间跨度至少 `20s`；相邻样本中断超过 `2.5s` 时只保留断点后的连续段并重新预热。
- 时间加权采用相邻样本的梯形积分除以实际连续跨度；先对每家交易所独立平均，再取平均信号中位数。
- 新公式标识为 `time_weighted_average_30s(spot_momentum_10s + 0.5*spot_momentum_30s + 0.35*calibrated_spot_twap_gap - twap_drift_10s - 0.5*twap_drift_30s)`；确认key改为 `twap_direction_average_30s`。
- diagnostics 同时保留 `twap_direction_instant_signal_returns` 和最终的 `twap_direction_signal_returns`，并新增每源平均状态、已就绪/预热来源及窗口参数。
- 基差校准、方向幅度 `0.03bps`、强度 `0.05 sigma`、目标概率变化 `0.3pp`、盘口、净优势、`2.0s/2次` 确认和退出参数未改变。
- 前端等待文案已改为“等待30秒方向平均历史”。

验证：目标测试 `42 passed`，Dashboard/runner/相关策略广覆盖 `137 passed`，`py_compile` 通过。空仓后已重启 Dashboard，当前 PID `1492`，仍为 `SHADOW_ONLY`。重启后的当前场次处于 `chainlink_open_unverified`，因此运行API尚未进入方向诊断分支；需等开盘价验证成功的场次才能看到平均预热字段。

## 2026-08-09 BTC V8 现货/TWAP交易所基差校准

V8 TWAP方向信号已从直接使用 `log(现货中间价 / Polymarket 30秒TWAP)` 升级为每家交易所独立校准。最终核对时发现外部启动的旧代码进程，确认空仓后已安全重启加载新实现；当前 Dashboard PID `14976`，仍为 `SHADOW_ONLY`。

- Binance、Coinbase、Kraken 分别维护基差历史，不共享基准；每个 Chainlink TWAP tick、每个来源最多记录一个时间配对样本。
- 现货盘口与TWAP接收时间差必须不超过 `1s` 才进入基差历史；使用 `received_at`，不使用交易所时钟决定先后。
- 基准基差取“当前前 `310s` 至前 `10s`”这段完整 `300s` 窗口的滚动中位数，排除最近 `10s`，避免当前领先立即污染基准。
- 校准至少需要 `30` 个样本且时间跨度至少 `60s`；按约1秒一个TWAP样本计算，冷启动通常约 `70s` 后就绪。预热期间校准缺口固定为0，现货10/30秒动量仍正常参与信号。
- 校准缺口为 `实时基差 - 基准基差`。滚动MAD用于限制异常值，截断范围为 `max(2bps, 6*MAD)`，并封顶 `10bps`。
- 新公式为 `spot_momentum_10s + 0.5*spot_momentum_30s + 0.35*calibrated_spot_twap_gap - twap_drift_10s - 0.5*twap_drift_30s`。
- `/api/state` 的 V8 diagnostics 新增原始基差、各源基准、校准后缺口、完整校准统计、已就绪/预热/截断来源、配对时间差和未配对来源；原 `spot_twap_gaps` 现表示校准后缺口。
- 基差历史跨5分钟场次保留，但进程重启后重新预热；未修改旧普通策略的 `edge_correction_usd`，V8 不再依赖该Binance单源修正。

验证：`python -m py_compile polybtc\btc_v8.py` 通过；`python -m pytest tests\test_btc_v8.py tests\test_orderbook_chase.py -q` 为 `41 passed`；Dashboard/runner/相关策略广覆盖为 `136 passed`。新增覆盖预热不使用原始价差、固定基差扣除、MAD异常截断和校准缺口进入组合方向信号。

运行API已确认新字段生效：冷启动阶段 `spot_twap_gaps` 为0；与TWAP接收时间差超过1秒的来源进入 `spot_twap_unpaired_sources`，不会写入基差历史。基准样本满足条件后才进入 `spot_twap_calibration_ready_sources`。

## 2026-08-09 BTC V8 TWAP大方向观察型高频档调整

为开始采集足够的影子成交，组合方向幅度调整为 `0.03bps`，最小信号强度调整为 `0.05 sigma`，目标概率变化调整为 `0.3pp`。方向确认改为至少一条现货领先源即可，但现货数据健康度仍要求至少两路交易所同时新鲜；公式绝对方向概率调整为 `UP/DOWN >=50%`，Chainlink 相对开盘的幅度门槛取消、仍必须与方向同向。Polymarket 同方向盘口近 `3s` 不下滑、净优势至少 `3¢`、买入确认 `2.0s/2次`、最短/最长持仓 `12s/45s`、`$1.00` 硬止损与 `>55s` 入场截止保持不变；仍为 `SHADOW_ONLY`，只会产生影子成交。

验证：`python -m py_compile polybtc\btc_v8.py` 通过；`python -m pytest tests\test_btc_v8.py tests\test_orderbook_chase.py -q` 为 `39 passed`。空仓后已重启 Dashboard，当前 PID `149224`。

运行验证：新场 `btc-updown-5m-1786209600` 在剩余约 `229s` 时通过 `2.0s/2次` 确认。V8 于 `2026-08-08T17:21:13Z` 建立 UP 影子仓，`$5.00`、入场价 `$0.46`、数量约 `10.87`；影子执行器的 Observed/P95 测量仓也均已建立，均不提交真实订单。持仓仍在管理中，禁止为修改参数重启。

## 2026-08-09 BTC V8 TWAP大方向可出单强度调整

为使两源共识的中等趋势在入场窗口内能够成交，组合方向幅度从 `1bps` 降至 `0.10bps`，最小信号强度从 `1.25 sigma` 降至 `0.20 sigma`，目标概率变化从 `4pp` 降至 `1pp`，Chainlink 相对本轮开盘的绝对同向幅度从 `1.5bps` 降至 `0.75bps`。公式绝对方向概率仍为 `UP/DOWN >=55%`；两路现货同向共识、同方向 Polymarket 近 `3s` 盘口不下滑、净优势至少 `3¢`、买入确认 `2.0s/2次`、最短/最长持仓 `12s/45s`、`$1.00` 硬止损与 `>55s` 入场截止保持不变，仍为 `SHADOW_ONLY`。

验证：`python -m py_compile polybtc\btc_v8.py` 通过；`python -m pytest tests\test_btc_v8.py tests\test_orderbook_chase.py -q` 为 `39 passed`。空仓后已重启 Dashboard，当前 PID `147400`。

## 2026-08-09 BTC V8 TWAP大方向概率变化第二档调整

为让已通过方向确认的中等趋势能够入场，目标概率变化门槛从 `8pp` 降至 `4pp`。组合方向 `>=1bps`、Chainlink 开盘同向 `>=1.5bps`、公式绝对方向概率 `>=55%`、至少两路现货同向共识、`1.25 sigma`、同方向 Polymarket 近 `3s` 盘口不下滑、净优势至少 `3¢`、买入确认 `2.0s/2次`、最短/最长持仓 `12s/45s` 与 `$1.00` 硬止损保持不变；仍为 `SHADOW_ONLY`。

验证：`python -m py_compile polybtc\btc_v8.py` 通过；`python -m pytest tests\test_btc_v8.py tests\test_orderbook_chase.py -q` 为 `39 passed`。空仓后已重启 Dashboard，当前 PID `141352`。

## 2026-08-09 BTC V8 TWAP大方向入场门槛第一档调整

因 2026-08-08 的首版大方向门槛在连续运行约一小时后未产生新影子单，已将方向确认调到可交易的第一档；仍为 `SHADOW_ONLY`，不提交真实订单。

- 组合方向幅度：`5bps -> 1bps`。
- Chainlink 相对本轮开盘的绝对同向幅度：`5bps -> 1.5bps`。
- 公式绝对方向概率：`UP/DOWN >=60% -> >=55%`。
- 保持不变：至少两路现货同向共识、`1.25 sigma`、目标概率变化 `8pp`、同方向 Polymarket 近 `3s` 盘口不下滑、净优势至少 `3¢`、买入确认 `2.0s/2次`、最短/最长持仓 `12s/45s` 与 `$1.00` 硬止损。
- 验证：`python -m py_compile polybtc\btc_v8.py` 通过；`python -m pytest tests\test_btc_v8.py tests\test_orderbook_chase.py -q` 为 `39 passed`；空仓后已重启 Dashboard，当前 PID `142080`。

## 2026-08-08 BTC V8 TWAP大方向当前运行状态

本节为当前最新状态。用户要求把 `BTC V8` 的盘口追赶升级为 `30秒TWAP大方向长持仓`，并保持其它分页/策略后台任务停掉；已完成并重启生效。

运行状态（本地时间 `2026-08-08 23:25 +08:00`）：

- Dashboard：`http://127.0.0.1:8765/`；WebSocket：`ws://127.0.0.1:8766/ws`；当前监听 Python PID `142808`。
- `/api/config`：`sources.enabled_assets=BTC`；`btc_v8.enabled=true`；`btc_v8.orderbook_chase_mode=true`；`orderbook_chase.enabled=true`。
- 其它策略 active 配置均已关闭：`pair_match=false`、`btc_recovery=false`、`btc_dynamic=false`、`real_trading=false`。
- `/api/state` 确认：`btc_recovery.status=disabled`、`btc_recovery.config.enabled=false`、`btc_recovery.round=null`；`orderbook_chase.status=v8_signal_following`、`paused=false`、`emergency_stopped=false`。
- TWAP大方向影子执行仍是 `SHADOW_ONLY`：只构造/签名影子 FOK，不提交真实订单；真实交易模块保持关闭。
- TWAP大方向硬门槛：买入确认 `2.0s/2次`，卖出确认 `0.50s/1次`，最短持仓 `12s`，最长持仓 `45s`，最小信号强度 `1.25 sigma`，组合方向幅度至少 `5bps`，公式绝对方向概率必须 `UP>=60% / DOWN>=60%`，Chainlink 相对开盘绝对同向至少 `5bps`，目标概率变化 `8pp`，同方向 Polymarket 盘口最近 `3s` 不能下滑，最小净优势 `3¢`，追上盈利退出至少 `$0.05`，可执行亏损达到 `$1.00` 立即硬止损，Chainlink/30秒TWAP 最大年龄 `5s`，入场必须剩余时间 `>55s`。

配置与前端：

- `config.example.yaml` 已作为启动基线切到 V8 盘口追赶：`btc_v8.enabled=true`、`btc_v8.orderbook_chase_mode=true`、`orderbook_chase.enabled=true`。
- `data/dashboard-settings.json` 的 active 运行时配置已同步，避免重启后被旧 70/40 设置覆盖。
- `web/index.html` 默认进入 `CHASE`，`<body>` 初始为 `chase-mode`，标题为 `polybtc · BTC TWAP大方向`；顶部资产/策略切换按钮仍保持可见。
- `scripts/run-dashboard.cmd` 显式使用 Codex bundled Python，并以 `--config config.example.yaml` 启动 Dashboard，避免误用系统 `WindowsApps\python.exe` 占位入口。
- 桌面 `C:\Users\Administrator\Desktop\BTC程序控制.cmd` 已改为 `BTC V8 Orderbook Chase Control`：`status` 读取 V8/chase，停 Dashboard 前检查 V8 持仓和追赶持仓，并提供 `pause-chase`、`resume-chase`、`emergency-chase`；本次备份为 `C:\Users\Administrator\Desktop\BTC程序控制.cmd.bak-20260808-v8-chase`。

运行时隔离：

- `polybtc/runner.py` 的守卫确保未启用的 pair/dynamic/v8/orderbook_chase/real_trading 不创建对应后台任务、信号采集、配置激活或控制循环；本次补充禁用 70/40 时不启动 `btc_recovery_resolution_loop`。
- `polybtc/btc_recovery.py` 已修正：`btc_recovery.enabled=false` 时不会从旧账本恢复当前 round，Dashboard 状态也明确返回 `disabled`，避免旧 70/40 round 污染当前状态。
- V8 TWAP大方向领先信号为 `spot_momentum_10s + 0.5*spot_momentum_30s + 0.35*spot_twap_gap - twap_drift_10s - 0.5*twap_drift_30s`；该组合信号只作为辅助领先信号，入场还必须通过绝对公式方向、Chainlink 开盘方向和 Polymarket 同方向盘口不下滑过滤。目标概率、UP/DOWN 支持交易所数量和买入确认 key 均按该组合领先信号计算。后续若评估历史段落中旧的 `0.25s`、`1¢`、`3s/5s` 等短追赶说明，以本节 TWAP大方向硬门槛为准。

70/40 历史注意：

- 之前已停止 `BTC 70/40策略` 恢复单，并把 `data/btc-recovery-ledger.sqlite3` 的旧 `btc_recovery_rounds` 和 `btc_recovery_fills` 清理后重新开始。
- 重置前备份位于 `data/btc-recovery-reset-backup-20260808T063935Z`，备份内共 60 个文件，其中 1 个 `.sqlite3` 和 57 个 `.csv`。
- `btc_recovery_controls` 中的 `recovery_orders_stopped=true` 仍保留；当前 70/40 引擎禁用，不应在未得到明确指令前恢复 70/40 或恢复单。

验证：

- bundled Python `-m py_compile polybtc\config.py polybtc\runner.py polybtc\btc_v8.py polybtc\dashboard.py`：通过。
- bundled Python `-m pytest tests\test_dashboard.py tests\test_btc_v8.py tests\test_orderbook_chase.py -q`：`64 passed`。
- bundled Python `-m pytest tests\test_btc_recovery.py tests\test_runner.py tests\test_dashboard.py tests\test_btc_v8.py tests\test_orderbook_chase.py -q`：`134 passed`。
- 本次 TWAP大方向长持仓变更后，bundled Python `-m pytest tests\test_btc_v8.py tests\test_orderbook_chase.py -q`：`39 passed`。
- 服务端 HTML 校验：默认 `body_chase_mode=true`、`selectedAsset='CHASE'`、`assetCHASE aria-selected=true`。

## 2026-08-08 盘口追赶高真实度影子交易

已新增独立 `BTC盘口追赶` 分页和影子执行层。现有 BTC V8 是唯一信号计算实例：追赶执行器直接消费 V8 已确认的 BUY 结果，不再创建第二个 `BtcV8Engine`，也不再重复接收或计算 Chainlink、现货和 Polymarket 盘口信号。

- 模式固定为 `SHADOW_ONLY`。每次启动生成仅驻留内存的无资金 EOA，只允许调用 `connect`、`build_fok_buy` 和 `build_fok_sell`；影子引擎没有 `post_order` 接口，API 当前明确返回 `post_order_available=false`。
- BUY/SELL 共用 `PolymarketSdkAdapter._build_fok_market_order()`，实际完成 SDK 订单构造和 EIP-712 签名，但不提交订单。私钥、API 凭证和签名原文不进入 SQLite、日志、API 或页面。
- BUY 的方向、目标概率、限价、金额、手续费和确认结果直接取现有 V8 成交事件。卖出继续读取现有 V8 的追上/反向市场诊断，但 Observed/P95 按各自延迟成交时间、价格、数量、5 秒持仓时间和浮盈回撤独立执行，避免复制 V8 instant 仓位的错误卖出时点。
- 持久连接每 2 秒测量一次 CLOB `/time` RTT，滚动保留 15 分钟。订单同时记录零延迟 `instant` 基准、当次 `observed` RTT 和滚动 `p95` RTT；P95 至少 30 个新鲜样本后才参与有效测试。
- RTT 持久连接在连接池超时后会自动重建，避免单次 `PoolTimeout` 让测速永久停滞；超过 5 秒没有新样本时仍按规则暂停有效测试。
- 延迟后只使用更新且不超过 1 秒的可信盘口。BUY 必须在限价内花完整笔金额，SELL 必须卖完整仓，否则 FOK 拒绝；卖出拒绝后必须等下一条盘口才重新构造和签名，298 秒仍未卖出则等待官方结算。
- 独立账本为 `data/orderbook-chase-ledger.sqlite3`，订单显示为 `CHASE#...`。未完成尝试在重启后标记为 `UNMEASURABLE_RESTART`；不可测量样本单独统计，不占 200 个有效测速回合。
- 页面显示单一 V8 信号源、RTT、构造签名耗时、总耗时、零延迟/Observed/P95 成交结果、FOK 存活率、延迟后盈亏和持仓。没有钱包、余额、allowance、武装或实盘控件。
- 统计使用 SQLite 聚合，不会在每次页面刷新时反序列化全部历史记录。专用账本中旧的 V8 场次表仅作为历史保留，当前运行不再写入。

运行状态（本地时间 `2026-08-08 08:09 +08:00`）：Dashboard 已在全部空仓、真实交易未武装的条件下重启为 Python PID `81304`，地址仍为 `http://127.0.0.1:8765/`。影子执行器状态为 `v8_signal_following`，API 返回 `shared_instance=true`、`duplicate_v8_engine=false`；临时 EOA 已连接，`post_order_available=false`。RTT 持续新鲜采样，200 回合速度统计继续运行，不会自动开放真实交易。

完整测试：`338 passed in 8.61s`。新增覆盖单一 V8 对象复用且追赶执行器不再次调用 V8 `evaluate()`、BUY/SELL FOK 共用构造、延迟后盘口移动、完整成交拒绝、卖出新盘口重试、重启恢复、RTT 连接池自愈、专用账本、Dashboard 下一场配置、仅允许 `GET /time` 的 HTTP 拦截及无 `post_order` 调用守卫。前端内联脚本语法和配置解析通过。

## 2026-08-03 BTC V8 多交易所自主模型

### 2026-08-03 18:16 盘口追赶模式当前状态

本节是当前最新状态。更新前工作区干净，分支为 `v1good`，HEAD 与远端 `origin/v1good` 均为 `4e97dbb 盘口追赶可行`。该提交包含 V8 盘口追赶、自动退出改进、Chainlink 开盘/当前显示、紧急亏损保护开关和对应测试；本次只更新 `HANDOFF.md`。

运行快照（本地时间 `2026-08-03 18:15:54 +08:00`）：

- Dashboard：`http://127.0.0.1:8765/`；WebSocket：`ws://127.0.0.1:8766/ws`；Python 服务 PID `77540`。
- V8 当前 `enabled=true`、`orderbook_chase_mode=true`、`auto_decision_mode=false`、`auto_emergency_loss_enabled=false`，仍然只做本地模拟，不连接任何真实下单适配器。
- 当前场为 `btc-updown-5m-1785752100`，状态 `waiting_for_edge`，空仓，本场买入次数 `0/3`，新鲜现货源 `2` 个。
- 活动模型为 `v8` 参数版本 `93`，已训练市场 `92`。盘口追赶使用独立短时估值和退出规则，但官方结算后的 V8 每场训练仍正常进行。
- 最近一条 V8 成交编号为 `#396`，退出原因为 `chase_timeout`。历史成交和盈亏不重算。

盘口追赶模式规则：

1. 所有领先判断严格按程序接收时间 `received_at` 排序，不使用交易所时间决定先后，避免本机与交易所时钟偏差制造虚假领先。
2. 使用新鲜现货相对 Chainlink 的 1 秒领先收益推算短时目标概率；至少两个现货源同向，信号强度至少 `0.35 sigma`，目标概率变化至少 `1.5` 个百分点。
3. 买入信号固定确认 `0.25` 秒且至少包含 `2` 次独立更新。该值原为 `0.5` 秒，因合格领先经常在半秒内消失而缩短；两所共识和其他门槛没有放松。
4. 入场按 Polymarket 完整卖盘模拟固定 `$5`，预计净优势为“追赶目标价减去实际买入均价、买入手续费、预计卖出手续费和滑点预留”，必须至少 `1` 美分/份。前端手动买入净优势输入在追赶模式下不生效。
5. 买入前还要求完整买入深度、市场最小份数和完整退出深度；按当前买盘立即整仓清仓的损失不得达到本金 `50%`。该入场保护独立于已关闭的紧急亏损卖出开关。
6. 买入后不再因为 `chase_signal_decayed` 单独卖出。现货相对 Chainlink 的领先自然会在 Chainlink 跟随后衰减，而这不代表 Polymarket 已经追上；衰减只保留为诊断信息，并会取消尚未完成的买入确认。
7. 已成交持仓只在以下情况主动退出：Polymarket 可执行净值追到入场目标且整仓净盈亏至少 `$0.02`；出现满足完整入场强度/共识条件的明确反向领先信号；或持仓达到 `5` 秒立即以 `chase_timeout` 整仓退出。所有卖出均要求完整买盘足以成交。
8. 旧的 `chase_signal_decayed` 历史订单不修改。修改前最近三笔该类退出为 `#382/#384/#386`，持仓约 `2.36/3.36/2.57` 秒，净盈亏约 `-$0.340/-$0.362/-$0.217`；它们用于说明旧规则会在 Polymarket 尚未追赶时提前承担双边手续费。

本机时钟在实现追赶模式后已校准。校准前 Windows 使用 `Local CMOS Clock` 且从未成功同步，本机稳定慢约 `1.627` 秒；当前 Windows Time 服务为自动启动，NTP 源为 `time.windows.com,0x8`，状态为已同步，独立复测偏差约 `1.5-1.9 ms`。不要再用校准前的交易所时间与本机接收时间差评估领先效果。

当前完整测试结果为 `312 passed in 8.41s`。新增覆盖包括追赶模式与自动模式互斥、现货领先及两所共识、双边费用后的入场、完整买卖生命周期、盈利追赶退出、反向信号、5 秒硬超时，以及“单纯信号衰减不得卖出”。前端内联脚本语法检查、Dashboard API 烟雾检查和运行日志检查均通过。

运维注意：修改 Python 后重启 Dashboard 前先查询 `/api/state` 的 `btc_v8.position`。有仓位时等待程序按当前场规则退出或结算，禁止中途重启；空仓后使用 `scripts/run-dashboard.cmd` 启动。V8 设置由 Dashboard 保存后从下一场 BTC 市场生效，当前场锁定自己的配置和模型版本。

V8 已作为独立的本地模拟模块实现，默认 `enabled=false`。它不会调用真实订单适配器，也不会读取、更新或覆盖 V1-V7 的 `data/btc-dynamic-ledger.sqlite3`；V8 的场次、每秒快照、持仓、成交、模型版本、控制项和 24 小时原始事件都位于 `data/btc-v8-ledger.sqlite3`。

- 现货源：Binance `BTCUSDT`、Coinbase `BTC-USD`、Kraken `BTC/USD`；合约特征源：Binance USDⓈ-M `BTCUSDT` 永续。
- Binance 现货使用 REST 深度快照和增量序列重建；Coinbase 使用 `matches`、`heartbeat`、`level2_batch` 并按 trade ID 补漏；Kraken 使用 25 档 book/trade 和顶部 10 档 CRC32 校验。
- Binance 永续采集成交、20 档深度、标记价、资金费率、持仓量和爆仓。当前系统代理环境可能不转发成交/标记价 WebSocket 帧，因此保留 WebSocket 订阅并用按成交 ID 去重的 REST 轮询补齐。
- 每个采集器使用独立有界队列。模型最多每 250ms 评估一次，盘口原始事件按来源、方向和 250ms 时间桶合并后批量写入 SQLite WAL；原始 JSON 使用 zlib BLOB 无损压缩，旧明文行仍可读取。
- `remaining_time` 使用完整市场时长：开盘 `+1`、中点 `0`、结算 `-1`，与买卖窗口无关。
- 概率为 Chainlink 结算公式加在线 Logistic 残差修正；新信号权重从零开始，修正幅度、权重和 L2 正则均有限制。每秒保存一个样本，官方结算后按该场平均梯度只训练一次。
- 新买入要求三个现货源中至少两个成交和盘口同时新鲜且未被异常值剔除；现货不足时已有持仓仍可依据可信 Chainlink 和 Polymarket 完整买盘卖出。
- 默认每次固定 `$5`，买入/卖出分别按完整盘口模拟并计手续费和滑点；默认买入优势 5 美分、卖出价值差 2 美分、买入确认 2 秒、卖出确认 1 秒、卖后冷却 3 秒、每场最多成功买入 3 次、单仓可执行止损 `$2.50`。
- `auto_decision_mode` 默认关闭。开启后，买入按扣除完整盘口成交、手续费和滑点后的正期望值决定，卖出/止损每 250ms 比较可执行清仓价值与模型继续持有价值；手动买卖门槛、确认参数和固定止损金额不参与自动模式。金额、来源新鲜度、盘口深度、交易次数和 285/298 秒边界仍是硬风控。
- 285 秒停止新买入，298 秒停止主动卖出，之后持有到官方结算。反向信号必须先卖完当前仓位，不允许直接翻仓。
- 模型版本在市场开始时锁定。训练或候选模型只会在开始时间晚于完成时间的新市场启用，当前市场不会中途切换；重启会恢复当前场、持仓、交易次数、活动/待启用模型和待生效设置。
- Dashboard 新增 `BTC V8` 页，显示来源健康、延迟、盘口校验、现货共识、公式/V8 概率、动作与拒绝原因、持仓方向/成交均价、按仓位合并的买卖记录、手续费、盈亏、特征贡献、校准、五个时间段 Brier 和各现货源可用/缺失状态的拆分表现。

候选模型命令：

```powershell
python -m polybtc btc-v8-retrain --model-key v8_candidate_name
python -m polybtc btc-v8-activate-model --model-key v8
```

V8 配置从 Dashboard 保存后仅在下一场 BTC 市场生效。当前完整测试为 `312 passed`；上线仍只允许本地 paper simulation，不应把烟雾测试、Brier 或模拟盈亏视为实盘收益证明。

隔离烟雾验收覆盖了完整的 `btc-updown-5m-1785714300`（本地 `07:45-07:50`）市场。首次进程结束时官方结果尚未发布；复用同一账本重启后恢复场次并补结算为 DOWN，该场 `trained=true`，模型版本只增加一次。两段运行合计保存 494 个每秒快照；新写入 3,912 条 zlib 原始事件平均 404.6 字节，随机解码 100 条的来源、接收时间和处理时间均完整。

## 2026-08-02 冻结 V3 已启用

更新时间：2026-08-02 15:12（Asia/Shanghai）。

- 当前活动模型：`v3`，参数版本 `413`，`frozen=true`，已训练市场固定为 `412`。
- V3 来源：V1 在本地时间 `2026-08-01 07:00:00`（UTC `2026-07-31T23:00:00Z`）之前全部已完成训练的精确回放状态。
- 昨日最高收益小时为本地 `06:00-07:00`：9 单、4 胜、模拟净收益 `+89.579422 USD`。V3采用该小时结束时的参数。
- 回放全部708次V1训练后与迁移前当前V1逐项比较，最大参数误差为 `0`，证明历史回放顺序和算法可精确重建模型。
- V3在 `btc-updown-5m-1785654600`（本地15:10开场）安全启用；上一场继续使用V1，没有中途切换。
- 首笔V3模拟订单为 `#001704`：UP、模型概率 `41.13%`、公式概率 `50.24%`、成交均价 `26¢`、固定本金 `$5`、实际 `19.2308` 份。
- 更新文档时服务PID为 `40148`，Dashboard仍为 `http://127.0.0.1:8765/`。
- 数据库迁移前备份：`data/btc-dynamic-ledger.pre-v3-20260802T070532Z.sqlite3`，SQLite `integrity_check=ok`，大小 `149,204,992` 字节。
- 完整测试：`274 passed in 7.23s`。

冻结语义：

- 新场次、快照和订单都保存 `model_key`；旧JSON缺省自动映射为 `online`。
- V3仍正常计算概率、采集训练快照、模拟下单、结算和统计，但结算时跳过梯度更新，`version=413`、`trained_markets=412` 和全部权重保持不变。
- V1保留在数据库键 `online` 下并继续保持可恢复；V1延迟结算只更新V1，不会替换当前V3。
- 活动/待启用模型使用SQLite控制项持久化。切换只在市场开始时间晚于安排时间的新市场执行，重启不会中途换模型。
- 模型重置在冻结模型活动期间或存在待切换模型时会被拒绝，避免清空V1或V3。
- 连败计数和冷却仍是动态策略全局控制；冷却时V3和公式对照都暂停下单，概率、快照与结算继续。
- V3沿用当前 `v1good` 分支的V1特征公式，包括旧 `remaining_time = clamp((remaining-15)/15)`；本次没有引入V2时间特征。

V3完整参数：

```json
{
  "model_key": "v3",
  "frozen": true,
  "version": 413,
  "trained_markets": 412,
  "bias": -0.024103691859420483,
  "weights": {
    "remaining_time": -0.026319437788597886,
    "volatility_ratio": -0.008106921581944843,
    "open_crossings": -0.008164415192579568,
    "up_market_gap": 0.28647290844256257,
    "up_depth_imbalance": 0.05644296996772717,
    "up_spread": -0.006386746416385831,
    "down_depth_imbalance": -0.05219656298661203,
    "down_spread": -0.004949381996085793,
    "binance_momentum_1s": 0.1236788836110844,
    "binance_momentum_3s": 0.18813847165754163,
    "binance_momentum_5s": 0.10872895228774383,
    "momentum_gap_1s": 0.11302095079364227,
    "momentum_gap_3s": 0.16226281566149703,
    "momentum_gap_5s": 0.047095028307672704,
    "binance_missing": -0.0013788829545626582
  }
}
```

手工安排下一场回滚V1时，程序必须先停止，再备份数据库，然后设置以下控制项；不要删除V3或覆盖 `online` 模型行：

```text
active_model_key            当前保持 v3
pending_model_key           设置为 online
pending_model_not_before    设置为安排回滚时的UTC时间
```

Dashboard“运行模型”显示格式为 `v3 · 参数v413 · 已冻结`；最近订单的版本列同时显示在线/公式与所属 `model_key`。统计新增 `summary.by_model`，未来V3订单可与V1历史拆分查看。

### 冻结模型库 V4-V7

2026-08-02 已从V1历史按结算顺序精确回放并新增四个待用模型，全部保存在 `data/btc-dynamic-ledger.sqlite3` 的 `btc_dynamic_model` 表中。迁移前备份为 `data/btc-dynamic-ledger.pre-v4-v7-20260802T101057Z.sqlite3`，`integrity_check=ok`。

| 模型键 | 参数版本 | 已训练市场 | 参数形成时间（本地） | bias | remaining_time | 状态 |
|---|---:|---:|---|---:|---:|---|
| `v4` | 401 | 400 | 2026-08-01 05:48:49 | 0.073365 | 0.071172 | 冻结待用 |
| `v5` | 404 | 403 | 2026-08-01 06:10:04 | 0.082711 | 0.080508 | 冻结待用 |
| `v6` | 407 | 406 | 2026-08-01 06:25:14 | 0.052526 | 0.050315 | 冻结待用 |
| `v7` | 410 | 409 | 2026-08-01 06:40:06 | 0.023955 | 0.021740 | 冻结待用 |

四个模型均包含完整15项权重，字段为 `frozen=true`、`source_model_key=online`，不会在结算时训练。安装后再次将全部V1历史回放到当前版本714，与数据库当前V1的全部参数最大误差仍为 `0`。活动控制保持 `active_model_key=v3`，没有设置待切换模型；V4-V7不会自行运行。以后启用任一模型时只安排下一场切换，不覆盖其他模型行。

## 2026-08-02 `v1good` 最新交接

更新时间：2026-08-02 06:06（Asia/Shanghai）。

### Git 与运行状态

- 工作目录：`D:\Users\Administrator\Documents\btc5fenzhong`
- 当前分支：`v1good`
- HEAD：`5b4b482 goodv11`
- 跟踪分支：`origin/v1good`，本地与远端提交一致。
- 远端：`https://github.com/lyc630904200-byte/btc5min.git`
- 仓库本地 Git 代理：`http.proxy` / `https.proxy` 均为 `http://127.0.0.1:7897`。
- Dashboard 正在运行：`http://127.0.0.1:8765/`
- WebSocket：`ws://127.0.0.1:8766/ws`
- 更新文档时服务 PID：`9376`，监听 `8765/8766`。
- 普通启动脚本 `scripts/run-dashboard.cmd` 不加载私钥，本次仍是本地模拟运行。

当前未提交修改必须保留，不要 reset、checkout 覆盖或从旧提交还原：

```text
 M config.example.yaml
 M polybtc/btc_dynamic.py
 M polybtc/config.py
 M polybtc/dashboard.py
 M tests/test_btc_dynamic.py
 M tests/test_dashboard.py
 M web/index.html
```

本次更新交接文档后还会新增 `M HANDOFF.md`。

### 当前模型事实

- `v1good` 当前只有原 `online` V1 模型；没有 `online_v2`、`feature_schema_version` 或 V1/V2 同场影子预测。此前讨论和实验过的 V2 迁移不在这个分支里。
- `remaining_time` 仍使用 V1 公式：`clamp((remaining_seconds - 15) / 15, -1, 1)`。剩余时间大于等于 30 秒时固定为 `+1`，只在最后 30 秒内连续下降到 `-1`。
- 当前四个训练时间槽按每场配置动态计算：`entry + (exit-entry) * [0, 1/4, 2/4, 3/4]`。当前窗口 `[0, 290)` 对应 `0 / 72.5 / 145 / 217.5` 秒。
- 时间槽只在目标秒后的 2 秒窗口内保存首个有效快照；`snapshot_second` 保存目标槽秒数，真实采集时间保存在 `created_at`。如果窗口内没有完整有效行情，该槽不会补采。
- 每个已结算市场将该场已有快照做平均梯度，只训练一次；模型、历史场次、快照和订单保存在 `data/btc-dynamic-ledger.sqlite3`。
- 更新文档时模型版本为 `615`，已训练市场 `614`，当前连续亏损计数为 `3`，冷却未触发。

### 未提交功能改动

1. 动态策略增加两种互斥下单方式：`quantity` 固定份数和 `quote` 固定本金。Dashboard 使用二选一控件，禁用未选中的输入框。
2. 固定金额模式按配置本金吃多档卖盘，当前设置为 `$5`；成交数量随盘口价格变化，手续费另计。深度不足、成交份数低于市场最小量或实际净优势不足时不下单。
3. 订单持久化并展示 `sizing_mode`、请求份数/金额、实际份数、本金和手续费；旧订单缺少新字段时仍按固定份数兼容加载。
4. 增加连续亏损休息：在线模型结算亏损时计数，盈利或不亏时清零；达到阈值后把冷却截止时间持久化到 SQLite，重启后继续生效。
5. 冷却期间在线模型和公式对照模型都停止下单及确认，但概率计算、行情处理、快照采集和结算训练继续运行。
6. 每日订单与盈亏按电脑本地时区的自然日统计，不再按 UTC 日期切日。
7. Dashboard 增加下单方式、计划份数/本金、连败/冷却状态和订单本金列。

当前活动参数：

```yaml
btc_dynamic:
  enabled: true
  sizing_mode: quote
  quantity: 10
  quote_amount_usd: 5
  entry_seconds_after_open: 0
  exit_seconds_after_open: 290
  min_net_edge_cents: 5
  slippage_reserve_cents: 1.35
  confirmation_seconds: 2
  confirmation_updates: 2
  loss_streak_limit: 5
  loss_cooldown_minutes: 30
```

配置从 Dashboard 保存后在下一场 BTC 市场生效。连败阈值采用产生该订单时随场次保存的配置。

### 更新时统计快照

- 在线模型：685 笔订单，683 笔已结算，250 胜，胜率 36.60%，已实现净盈亏 `+301.2631 USD`。
- 公式对照：796 笔订单，795 笔已结算，282 胜，胜率 35.47%，已实现净盈亏 `-90.4306 USD`。
- 全部已结算训练快照：2,958 个；在线 Brier `0.162924`，公式 Brier `0.167231`，在线方向准确率 `74.58%`。
- 2026-08-02 本地日截至更新时：在线 16 单、`-22.5136 USD`；公式 16 单、`-21.3529 USD`。
- 这些是文档更新时间点的累计模拟统计，会随着后续市场结算继续变化，不代表实盘收益。

### 启停与依赖

启动：

```powershell
scripts\run-dashboard.cmd
```

停止时按监听端口找到进程，结束后确认两个端口均已释放：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8765,8766
Stop-Process -Id <OwningProcess>
```

启动日志：`data/dashboard-live.stdout.log` 和 `data/dashboard-live.stderr.log`。

2026-08-02 启动时 Codex bundled Python 环境被刷新，项目依赖一度缺失并报 `ModuleNotFoundError: typer`。已使用下列命令恢复主依赖；如果运行时再次刷新，可重复执行：

```powershell
$runtimePython = 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $runtimePython -m pip install -e '.[dev]'
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/state
Invoke-RestMethod http://127.0.0.1:8765/api/config
```

最新完整测试：

```powershell
& $runtimePython -m pytest -q
# 272 passed in 6.42s
```

## 2026-07-28 Chainlink 开盘/当前显示提速

本次针对 BTC 动态胜率页签里 `Chainlink开盘` / `Chainlink当前` 开盘后显示偏慢做了修正：

- 开盘阈值页面验证的固定等待从 1 秒降到 250ms。
- Gamma event threshold 与 Polymarket 页面 openPrice/past results 改为并行请求，减少串行网络等待。
- 页面 openPrice 未刷新前，动态胜率先用 RTDS exact-start tick 显示未验证开盘价；如果 exact 00 秒 tick 缺失，则使用开盘后 2 秒内第一条 RTDS tick 作为未验证显示兜底。
- 未验证开盘价只写入动态胜率诊断字段，并标记 `chainlink_open_verified=false`；不会进入候选计算、不会触发模拟/真实下单。交易仍等 Polymarket 页面验证通过。
- 新市场切换时，动态胜率会立即复用主引擎已缓存的最新 RTDS tick，避免 `Chainlink当前` 等下一条 tick 才刷新。
- 默认 `threshold_page_retry_seconds` 从 2 秒调到 0.75 秒。

验证记录：

- 定向测试：`tests/test_runner.py tests/test_btc_dynamic.py`，44 passed。
- 全量测试：262 passed。
- 已重启 Dashboard，新进程 PID `33064`，Dashboard `http://127.0.0.1:8765/`，WebSocket `ws://127.0.0.1:8766/ws`。
- 当前运行输出目录：`data\20260728T051917Z`。
- 实测 `btc-updown-5m-1785216000` 在 `2026-07-28T05:20:00Z` 开盘后，于 `2026-07-28T05:20:13.981099Z` 完成 `polymarket_page_rtds_verified_open_price` 验证，动态胜率显示 `chainlink_open_verified=true`。

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
- 更新文档时进程 PID：`33064`
- 当前运行输出目录：`data\20260728T051917Z`
- 更新文档时 BTC 市场为 `btc-updown-5m-1785216000`，Chainlink tick 与当前秒同步，配置状态为 `active`，无待生效配置。
- BTC 动态胜率和 BTC 70/40 当前同时启用，共享可信 BTC 行情/盘口，但使用独立账本、持仓与统计。旧 BTC 单币入场和 BTC/ETH 新配对暂停，ETH 与已有仓位管理不受影响。
- 70/40 当前参数为：首单触发 92¢、首单最高限价 95¢、首单止盈 100¢、恢复单止盈 90¢、恢复触发/首单止损 0¢、恢复止损 20¢、首单 10 份、恢复单 400 份、窗口 `[240, 290)` 秒。
- 恢复单下单控制仍为“已停止”，状态已从 SQLite 恢复；首单继续运行，但不会新买恢复单。统计重置后累计观察 431 场、首单 247 笔、完成 246 场、胜率 95.53%、已实现净盈亏 `35.15786574 USD`，另有 1 场待结算。
- 动态胜率当前参数为：10 份、窗口 `[240, 290)` 秒、最低净优势 3¢/份、滑点预留 1.35¢/份、确认 2 秒且至少 2 次更新、最大概率修正 10 个百分点；训练快照为 240/252.5/265/277.5 秒。
- 动态模型已训练 149 个市场。统计重置后在线主单 23 笔、已结算 22 笔、净盈亏 `17.26711 USD`；公式对照 27 笔、已结算 26 笔、净盈亏 `9.63297 USD`。在线/公式 Brier 分别为 `0.094204/0.095081`。样本仍少，不应据此接入真实交易或频繁改参数。
- 真实交易配置当前关闭，未加载凭据、未武装、真实订单为 0。
- 最近完整测试：`262 passed`

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

## 2026-08-12 BTC领先预测独立影子策略

已新增顶部独立分页“BTC领先预测”，实现文件为：

- `polybtc/btc_lead_prediction.py`：多源校准、动态权重、TWAP窗口替换预测、冲击确认、Observed/P95影子成交、短线退出、风险暂停与官方结算。
- `data/btc-lead-prediction-ledger.sqlite3`：独立保存轮次、预测、冲击、尝试、仓位、来源健康和在线校准。
- `polybtc/runner.py`：Binance/Coinbase/Kraken现货采集改为BTC V8或领先预测任一开启即运行；Binance永续只进领先预测诊断。
- `polybtc/dashboard.py`、`web/index.html`：新增独立状态、下一场BTC生效配置和桌面三栏工作台。
- `tests/test_btc_lead_prediction.py`：覆盖配置、加权中位数、时间倒退、30秒替换窗口、无未来数据、冲击去重、精确$1双轨成交和重启中断。

关键运行约束：

- 默认 `enabled=false`，固定 `SHADOW_ONLY`，不会连接真实下单。
- 至少两个健康现货源，来源年龄与交易所时间年龄均不超过0.5秒；异常基差、时间倒退和离散度超过2bps时不交易。
- 预测周期固定1/3/5/8秒，主周期5秒；盘口响应少于30个已到期样本时保持“预测预热”。
- 跳动默认要求500ms幅度至少1.5bps、至少两个同向来源、持续0.8秒且至少3个真实行情更新时间；定时器不会计入确认次数。
- 同一连续冲击只生成一个 `shock_id`；信号失效后才允许识别下一次冲击。
- 每次决策精确使用$1 quote，盘口最小份数仅诊断；当前和延迟盘口都复核完整买入及预计退出深度。
- P95净收益使用90%悲观预测路径，并扣双边手续费与P95延迟成本；买入均价已包含盘口价差和滑点，不重复扣减。
- 卖出深度不足会逐盘口持续重试，3秒后标记“退出流动性失败”但不放弃退出。
- 连续3次P95亏损暂停30分钟；北京时间自然日P95累计亏损达到$10后暂停至下一日。
- 参数通过 `/api/config` 保存并显示“保存成功，下一场BTC生效”；当前持仓继续使用建仓时版本。

验证结果：

```text
392 passed
```

桌面浏览器在1600x1000视口验收通过：无控制台错误、无横向溢出，保存反馈正常。验收截图为 `data/btc-lead-desktop.png`。当前唯一服务运行在 `http://127.0.0.1:8767`，WebSocket为8768；原8765/8766实例已停止。
