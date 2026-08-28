# dsh-pet × DSH 工作状态联动 + Token 花费（二次开发说明）

让桌宠实时感知 DeepSeek Harness 是否正在"干活"（工具运行/回合进行中），
工作时切到「写代码 / 吃Token / 敲桌面」等认真动画并闭嘴摸鱼，收工后恢复；
同时统计 DSH 会话的 token 用量并估算花费。**全部数据来自 DSH 会话本身**，
桌宠不需要绑定任何 API key。

## 架构

```
DSH 页面(浏览器/Electron 渲染器)
  └─ dsh-work-beacon.js  ← 注入的页内信标（v3）
       ① MutationObserver 观察 [data-state="ongoing"] 等 DOM 信号（300ms 去抖）
          │  fetch POST /state  {"working":true,"detail":"tool","beacon":"v3-usage","wsTap":true}
       ② 包装 window.WebSocket 截获 DSH 事件流里的 usage 与模型
          （assistant/message 的 usage + message.source.model / assistant/chunk 的 usage）
          按 (turn,step) 去重；增量上报
          │  fetch POST /usage  {inputTokens, outputTokens, cacheReadTokens, reasoningTokens, model}
        ▼
桌宠进程(pet/work_state.py 的 WorkStateServer，线程安全，127.0.0.1:47890)
        │  变化 → Qt Signal 桥 → 主线程
        ▼
PetWindow
   set_work_state() → 工作池动画 / 禁闲聊 / 禁移动
   add_token_usage() → 累计 session + 持久化 lifetime（config 目录 token_usage.json）
   show_token_cost() → 气泡展示"本会话 + 累计"估算花费（按 DSH 上报的模型定价）
```

## 改动文件（相对上游 MerZlin/dsh-pet-indesktop）

| 文件 | 说明 |
| --- | --- |
| `pet/work_state.py` | **新增**：本地 HTTP 接收端；`/state` `/usage` `/health` 端点；记录信标版本 |
| `pet/token_cost.py` | **新增**：DeepSeek 定价表（v4-flash $0.15/$0.29/$0.02 等）+ 估算 + 持久化 |
| `pet/window.py` | 工作态切换；`add_token_usage` / `show_token_cost`；`.venv` 外持久化累计 |
| `pet/app.py` | 拉起 WorkStateServer；`_WorkStateBridge` 投递状态与用量；`_set_app_icon` 鲸鱼 Dock 图标；托盘「Token 花费统计」 |
| `pet/context_menus/modern.py` `legacy.py` | 右键菜单「Token 花费统计」入口 |
| `beacon/dsh-work-beacon.js` | **新增**：页内信标（DOM 状态 + WebSocket usage 截获 + 版本/截获状态上报） |
| `scripts/inject-beacon.mjs` | **新增**：注入/回滚信标到 DSH 前端（SHA-256 备份） |
| `scripts/make-app.sh` | **新增**：一键生成「联动桌宠.app」访达/Dock 启动器 |
| `启动联动桌宠.command` / `停止联动桌宠.command` | **新增**：双击启停 |

## 使用

1. **启动桌宠**：双击 `启动联动桌宠.command`，或 `bash scripts/make-app.sh` 生成 `.app` 后用访达/Dock 启动
   （首次需先建 venv：`python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt`）
2. **DSH 侧注入信标**（一次性）：`node scripts/inject-beacon.mjs --target "<DSH_INSTALL_DIR>"`
3. **刷新 DSH 页面**：`Cmd+Shift+R`（关键！让信标生效，含 usage 截获）
4. 开始干活吧——桌宠会切到「写代码」等动画；收工后恢复摸鱼
5. **查看 Token 花费**：右键桌宠 / 托盘图标 → **Token 花费统计**（气泡显示"本会话 + 累计"估算花费，跨重启保留在 `token_usage.json`）

## 验证

```bash
curl -s http://127.0.0.1:47890/state          # 状态 + 信标版本（beacon/wsTap 诊断）
curl -s http://127.0.0.1:47890/usage          # token 累计 + 模型
curl -s http://127.0.0.1:47890/health         # 健康检查
# 手动模拟：
curl -s -X POST http://127.0.0.1:47890/usage \
  -H "Content-Type: application/json" \
  -d '{"inputTokens":1000,"outputTokens":200,"cacheReadTokens":500,"model":"deepseek-v4-flash"}'
```

> 信标版本诊断：`/state` 里 `beacon` 应为 `v3-usage`、`wsTap` 应为 `true`；
> 若 `beacon` 为空，说明 DSH 页面还没加载新版信标（需要硬刷新或清缓存）。

## 回滚 / 卸载

- **移除 DSH 页内信标**：
  ```bash
  node scripts/inject-beacon.mjs --rollback <backup目录>
  # backup 在 .beacon-backup/dsh-work-beacon-<时间戳>/
  ```
- **还原桌宠代码**：`git checkout -- pet/ && rm pet/work_state.py pet/token_cost.py`
- 旧的官方 `.app` 版桌宠未受影响，可随时启动（只是没有联动）

## 说明

- 信标只**读** DSH 页面 DOM / 事件流，不触碰业务逻辑；桌宠没开时信标静默失败，不影响 DSH。
- 所有流量都是本机回环（127.0.0.1），不对外。
- 工作态判定信号：`[data-state="ongoing"]`（本版 DSH 主信号）、`[data-status="running"]`、`[data-running]`、`[aria-busy="true"]`、`[data-state="loading"]`、`[data-status="pending"]`。
- usage 数据结构（来自 DSH `dsh-llm-deepseek` 的 `mapUsage`）：`{inputTokens, outputTokens, cacheReadTokens?, reasoningTokens?}`；模型名在 `assistant/message.message.source.model`。
- **Token 花费是估算**：DeepSeek API 只返回 token 数、不返回金额，金额 = token × 单价（参考官方定价，汇率 7.2）不是账单；`deepseek-v4` 系列为峰谷计费，本估算按非高峰费率。定价可在 `pet/token_cost.py` 调整。
- usage 截获基于 DSH 事件流（WebSocket 帧），页面刷新后开始统计；桌宠未运行期间产生的用量会漏计（尽力而为）。
