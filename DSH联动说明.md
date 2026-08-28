# dsh-pet × DSH 工作状态联动 + Token 花费（二次开发说明）

让桌宠实时感知 DeepSeek Harness 是否正在"干活"（工具运行/回合进行中），
工作时切到「写代码 / 吃Token / 敲桌面」等认真动画并闭嘴摸鱼，收工后恢复；
同时统计 DSH 会话的 token 用量并估算花费。**全部数据来自 DSH 会话本身**，
桌宠不需要绑定任何 API key。

## 架构

```
DSH 页面(浏览器/Electron 渲染器)
  └─ dsh-work-beacon.js  ← 注入的页内信标（仅负责"工作状态"）
        MutationObserver 观察 [data-state="ongoing"] 等 DOM 信号
        │  fetch POST /state  {"working":true,"detail":"tool",...}
        ▼
桌宠进程 WorkStateServer(127.0.0.1:47890)
        │  → Qt 信号桥 → PetWindow.set_work_state() 切工作动画

Token 账本（直读 DSH 会话日志，权威且不依赖页面）
DSH 落盘 ~/.dsh/sessions/<workspace>/session-<id>/session.jsonl.zstd
   （追加式 zstd 多帧；每回合 assistant/message 带 usage + 模型名）
        ▲ 每 2 秒轮询（后台线程）
        │  session_reader.py：find_current_session_file 找最新会话
        │  → 快速解压(read_across_frames, ~30ms) → 逐回合解析 usage + 模型
        │  → aggregate_all_sessions 汇总所有工作区全部会话（带 mtime+size 缓存）
        ▼
PetWindow.update_ledger()
   本会话 = 当前工作区最新会话总数
   累计   = 所有工作区全部会话总账
   （持久化快照 token_ledger.json）
```

## 改动文件（相对上游 MerZlin/dsh-pet-indesktop）

| 文件 | 说明 |
| --- | --- |
| `pet/work_state.py` | **新增**：本地 HTTP 接收端（127.0.0.1:47890，工作状态 `/state`） |
| `pet/session_reader.py` | **新增**：直读 DSH 会话日志（~/.dsh/sessions），逐回合解析 token 用量、模型名与**发生时间**（峰/谷分桶）；`aggregate_all_sessions` 全工作区汇总（含峰谷桶） |
| `pet/token_cost.py` | **新增**：DeepSeek 定价表（官方 2026-08 最新价；`v4-pro`/`v4-flash`/`v4-flash-vision-exp` 峰谷两档）+ 时间戳判峰谷 + 峰谷分桶混合计价（高峰用量×高峰价 + 低谷用量×低谷价）+ `format_number` 紧凑格式化（万/亿、K/M、完整三档；auto 档粒度调细，价格 4 位小数） |
| `pet/token_cost_dialog.py` | **新增**：Token 花费显示设置窗口（勾选字段/口径/格式，存 config.json） |
| `pet/window.py` | 工作态切换；`update_ledger`（双口径账本）；`token_cost_text` 按设置生成紧凑气泡（价格 4 位小数）；`open_token_cost_settings` |
| `pet/app.py` | 拉起 WorkStateServer；`_WorkStateBridge` 投递；每 2 秒轮询会话日志（当前会话 + 全工作区总账）；情绪监听（一宠跟人走）；`_set_app_icon` 鲸鱼 Dock 图标；托盘「Token 花费统计/设置」 |
| `pet/context_menus/modern.py` `legacy.py` | 右键菜单「Token 花费统计」「Token 花费设置」入口 |
| `pet/emotion_actor.py` | **新增**：情绪→动作混合引擎（本地关键词 + LLM 升级导演） |
| `beacon/dsh-work-beacon.js` | **新增**：页内信标（仅工作状态检测，URL 带内容哈希防缓存） |
| `scripts/inject-beacon.mjs` | **新增**：注入/回滚信标到 DSH 前端（SHA-256 备份） |
| `scripts/make-app.sh` | **新增**：一键生成「联动桌宠.app」访达/Dock 启动器 |
| `启动联动桌宠.command` / `停止联动桌宠.command` | **新增**：双击启停 |
| `requirements.txt` | 追加 `zstandard>=0.22`（读会话日志需要） |

## 使用

1. **启动桌宠**：双击 `启动联动桌宠.command`，或 `bash scripts/make-app.sh` 生成 `.app` 后用访达/Dock 启动
   （首次需先建 venv：`python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`）
2. **DSH 侧注入信标**（一次性，仅工作状态联动需要）：`node scripts/inject-beacon.mjs --target "<DSH_INSTALL_DIR>"`
3. **刷新 DSH 页面**：`Cmd+Shift+R`（让信标生效）
4. 开始干活吧——桌宠会切到「写代码」等动画；收工后恢复摸鱼
5. **查看 Token 花费**：右键桌宠 / 托盘图标 → **Token 花费统计**（气泡显示"本会话 + 累计"估算花费）
6. **自定义显示**：右键桌宠 / 托盘图标 → **Token 花费设置**，勾选要显示的数据（输入/输出/命中/推理/价格）、
   统计口径（本会话/累计）与数字格式（万·亿 / K·M / 完整），保存后立即预览

> Token 账本**不需要**信标或刷新，直接读 DSH 会话日志（见架构图）。

## 验证

```bash
curl -s http://127.0.0.1:47890/state          # 工作状态 + 信标诊断
curl -s http://127.0.0.1:47890/health         # 健康检查
# 账本查看：右键桌宠 / 托盘 →「Token 花费统计」；
# 或读持久化文件 ~/Library/Application Support/dsh-pet-standalone/token_ledger.json
```

> 信标诊断：`/state` 里 `beacon` 应为 `v5-usage`、`wsTap` 应为 `true`；
> 若 `beacon` 为空，说明 DSH 页面还没加载新版信标（硬刷新 `Cmd+Shift+R`，URL 带内容哈希会自动换新）。
> 信标只影响工作状态联动，不影响 Token 账本。

## 回滚 / 卸载

- **移除 DSH 页内信标**（仅影响工作状态联动，不影响 token 账本）：
  ```bash
  node scripts/inject-beacon.mjs --rollback <backup目录>
  # backup 在 .beacon-backup/dsh-work-beacon-<时间戳>/
  ```
- **还原桌宠代码**：`git checkout -- pet/ && rm pet/work_state.py pet/token_cost.py pet/token_cost_dialog.py pet/session_reader.py pet/emotion_actor.py`
- 旧的官方 `.app` 版桌宠未受影响，可随时启动（只是没有联动）

## 情绪响应（混合引擎 + 一宠跟人走）

桌宠空闲时（DSH 没在跑工具），检测到**用户最新一条消息**（任何工作区/会话，跟着人走）后判断情绪，播放贴合场景的动作：

- **触发源**：全局扫描所有工作区的会话日志，取"最新一条用户文本消息"（`latest_user_message_global`），
  用消息 time 字段跨会话比较；`current_sid` 追踪当前会话，用户切会话自动跟着切；
- **本地关键词情感**（零成本）：开心/庆祝/困/饿/思考/玩耍/被逗(大肥鱼→傲娇)等 → 直接映射现有动画；
- **LLM 升级**：置信度低 或 情绪强烈（生气/难过/惊讶/喜欢/激动）时，调 DeepSeek 小模型
  从动作标签表里选最贴合的一个（结构化 JSON，花少量 token）；
- **实时性**：会话日志每 **2 秒**轮询（用户消息即时落盘，反应延迟 ~1-2 秒）；
- **去重**：指纹 = 会话ID+seq，`set` 集合去重（上限 500 自动裁剪），重启后也只反应一次；
- **安全**：`sk-*` 密钥、长 token 串、系统注入（PERSONA_LOAD 等）自动过滤，不进情绪、不记日志；
- 仅**空闲时**响应（工具在跑不打断），**15 秒节流**，可在 config.json 关掉（`emotion_reactions_enabled`）。
- 相关文件：`pet/emotion_actor.py`（本地规则 + LLM 导演 + 动作映射）、
  `session_reader.latest_user_message_global`、`window.react_to_emotion`、`app` 情绪监听。

## 说明

- **Token 账本直读 DSH 会话日志**（`~/.dsh/sessions/*/session-*/session.jsonl.zstd`，追加式 zstd 多帧，快速解压），
  每 2 秒后台解析。**本会话 = 当前工作区最新会话**；**累计 = 所有工作区全部会话总账**（mtime+size 缓存，
  增量几乎零开销）。跨重启幂等，断线可补账，**不依赖页面刷新/信标**。
- **情绪响应与账本口径不同**：情绪响应 = "一宠跟人走"，全局跟随用户最新会话（跨工作区）；
  token 累计 = 所有工作区总账（见上）。两者各司其职。
- 信标只负责**工作状态**联动：只观察工具真的在跑的 DOM 信号（`[data-status="running"]` / `[data-running]` /
  工具卡片 running）；**纯聊天不算工作**（`data-state="ongoing"` 只表示回合进行中）。只**读**，不触碰业务逻辑；
  桌宠没开时信标静默失败，不影响 DSH。所有流量本机回环（127.0.0.1），不对外。
- usage 数据结构（来自 DSH `dsh-llm-deepseek` 的 `mapUsage`）：`{inputTokens, outputTokens, cacheReadTokens?, reasoningTokens?}`；模型名在 `assistant/message.message.source.model`。
- **Token 花费是估算**：DeepSeek API 只返回 token 数、不返回金额，金额 = token × 单价（参考官方定价，汇率 7.2）不是账单；`deepseek-v4` 系列为峰谷计费，**按每个回合实际发生时间划分高峰/低谷用量，分别计价后求和**（高峰 = UTC 周一~五 01:00-04:00、06:00-10:00，低谷为高峰一半），金额只增不减、不随当前时间跳变。定价与峰谷窗口可在 `pet/token_cost.py` 调整。
- **显示设置**存 `config.json`（键 `token_display_fields` / `token_display_scopes` / `token_display_format`），跨重启保留。
