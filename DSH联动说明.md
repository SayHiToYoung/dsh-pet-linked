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
        ▲ 每 5 秒轮询
        │  session_reader.py：找最新会话 → 快速解压(read_across_frames)
        │  → 逐回合解析 usage → 增量更新
        ▼
PetWindow.update_ledger() → 本会话总数 + 累计增量（跨重启去重，持久化 token_ledger.json）
```

## 改动文件（相对上游 MerZlin/dsh-pet-indesktop）

| 文件 | 说明 |
| --- | --- |
| `pet/work_state.py` | **新增**：本地 HTTP 接收端（127.0.0.1:47890，工作状态 `/state`） |
| `pet/session_reader.py` | **新增**：直读 DSH 会话日志（~/.dsh/sessions），逐回合解析 token 用量与模型 |
| `pet/token_cost.py` | **新增**：DeepSeek 定价表（v4-flash $0.15/$0.29/$0.02 等）+ 估算 + 持久化 |
| `pet/window.py` | 工作态切换；`update_ledger`（会话日志账本，增量+去重）；气泡显示 输入/输出/命中/价格 |
| `pet/app.py` | 拉起 WorkStateServer；`_WorkStateBridge` 投递；每 5 秒轮询会话日志；`_set_app_icon` 鲸鱼 Dock 图标；托盘「Token 花费统计」 |
| `pet/context_menus/modern.py` `legacy.py` | 右键菜单「Token 花费统计」入口 |
| `beacon/dsh-work-beacon.js` | **新增**：页内信标（仅工作状态检测，URL 带内容哈希防缓存） |
| `scripts/inject-beacon.mjs` | **新增**：注入/回滚信标到 DSH 前端（SHA-256 备份） |
| `scripts/make-app.sh` | **新增**：一键生成「联动桌宠.app」访达/Dock 启动器 |
| `启动联动桌宠.command` / `停止联动桌宠.command` | **新增**：双击启停 |
| `requirements.txt` | 追加 `zstandard>=0.22`（读会话日志需要） |

## 使用

1. **启动桌宠**：双击 `启动联动桌宠.command`，或 `bash scripts/make-app.sh` 生成 `.app` 后用访达/Dock 启动
   （首次需先建 venv：`python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`）
2. **DSH 侧注入信标**（一次性）：`node scripts/inject-beacon.mjs --target "<DSH_INSTALL_DIR>"`
3. **刷新 DSH 页面**：`Cmd+Shift+R`（关键！让信标生效，含 usage 截获）
4. 开始干活吧——桌宠会切到「写代码」等动画；收工后恢复摸鱼
5. **查看 Token 花费**：右键桌宠 / 托盘图标 → **Token 花费统计**（气泡显示"本会话 + 累计"估算花费，跨重启保留在 `token_usage.json`）

## 验证

```bash
curl -s http://127.0.0.1:47890/state          # 工作状态 + 信标诊断
curl -s http://127.0.0.1:47890/health         # 健康检查
# 账本查看：右键桌宠 / 托盘 →「Token 花费统计」；
# 或读持久化文件 ~/Library/Application Support/dsh-pet-standalone/token_ledger.json
```

> 信标诊断：`/state` 里 `beacon` 应为 `v5-usage`、`wsTap` 应为 `true`；
> 若 `beacon` 为空，说明 DSH 页面还没加载新版信标（硬刷新 `Cmd+Shift+R`，URL 带内容哈希会自动换新）。

## 回滚 / 卸载

- **移除 DSH 页内信标**（仅影响工作状态联动，不影响 token 账本）：
  ```bash
  node scripts/inject-beacon.mjs --rollback <backup目录>
  # backup 在 .beacon-backup/dsh-work-beacon-<时间戳>/
  ```
- **还原桌宠代码**：`git checkout -- pet/ && rm pet/work_state.py pet/token_cost.py pet/session_reader.py`
- 旧的官方 `.app` 版桌宠未受影响，可随时启动（只是没有联动）

## 说明

- **Token 账本直读 DSH 会话日志**（`~/.dsh/sessions/*/session-*/session.jsonl.zstd`，追加式 zstd 多帧），
  每 5 秒解析最新会话，逐回合累计；跨重启按"会话总量增量"去重，断线可补账。**不依赖页面刷新/信标**。
- 信标只负责**工作状态**联动：观察 `[data-state="ongoing"]` 等 DOM 信号；只**读**，不触碰业务逻辑；
  桌宠没开时信标静默失败，不影响 DSH。所有流量本机回环（127.0.0.1），不对外。
- usage 数据结构（来自 DSH `dsh-llm-deepseek` 的 `mapUsage`）：`{inputTokens, outputTokens, cacheReadTokens?, reasoningTokens?}`；模型名在 `assistant/message.message.source.model`。
- **Token 花费是估算**：DeepSeek API 只返回 token 数、不返回金额，金额 = token × 单价（参考官方定价，汇率 7.2）不是账单；`deepseek-v4` 系列为峰谷计费，本估算按非高峰费率。定价可在 `pet/token_cost.py` 调整。
