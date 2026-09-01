# dsh-pet-indesktop（DSH 联动版）

基于 [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)（其源于 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)）的**二次开发分支**。

在原项目"独立桌面宠物"的基础上，新增了几项 DSH 深度集成能力：

1. 🐋 **DSH 工作状态联动** —— 桌宠实时感知 DeepSeek Harness 是否在跑工具/回合，
   工作时切到「写代码 / 吃Token / 敲桌面」等认真动画并闭嘴摸鱼，收工后恢复；
2. 💰 **DSH Token 花费统计** —— 完全由 DSH 会话数据驱动（模型名 + token 用量都来自
   DSH 事件流），桌宠零 API 绑定，实时累计并估算花费；
3. 🏢 **办公区联动（dsh-agent-office）** —— 与 DSH 里的「多智能体办公区」插件联动：
   office 把主控鲸状态实时推来镜像；「送去桌面 / 叫回办公室」双向搬家；归属状态机
   保证同一只鲸同一时刻只在办公室或桌面一处渲染。
4. 🎮 **情境感知** —— 监听前台应用/进程，知道「现在该不该打扰」：
   打游戏缩角落静音、开会自动隐身、DSH/IDE 在前台则进入工作陪伴态。只感知情境、
   不读用户内容，本地权限、无封号风险。设置界面有「情境感知」页可自定义关键词与
   「别打扰我」开关；统一事件流优先级为「情境 > 办公区镜像 > 工作信标」。详见
   [docs/情境感知-无处不在-2026-09-01.md](./docs/情境感知-无处不在-2026-09-01.md)。
5. 🕰️ **会议关怀** —— 记录开会时长，散会后按开了多久分档体贴一句「辛苦啦」
   （默认 30 / 60 / 120 分钟三档，可调）；同一次会议只结算一次、有冷却防刷屏。
   设置界面「情境感知 → 会议关怀」可开关与调档。

> 数据全部走本机回环（127.0.0.1），无遥测、无外部请求。DSH 侧只注入一个只读信标脚本，
> 不修改 DSH 业务逻辑，可随时回滚。

---

## ✨ 本分支新增内容

### 工作状态联动
- 页内信标只观察「工具真的在跑」的 DOM 信号（`data-status="running"` / `data-running`），
  纯聊天不算工作（300ms 去抖 + 心跳）
- 桌宠 `_pick_next` 增加工作态分支：只播「工作池动画 + 待机」，不移动、不闲聊
- 开工/收工有气泡反馈

### 办公区联动（与 dsh-agent-office 插件）
- **状态镜像**：`POST /office/root` → `notify_root` → `window.mirror_agent()`，把主控鲸
  的状态（思考 / 跑工具 / 等批准…）映射成桌宠动画
- **搬家**：`POST /office/handoff {dir:"to_desktop"|"to_office", fromScreen}` →
  `handoff_enter / handoff_leave / _handoff_glide` 沿屏幕坐标滑行进出，形象与办公区同款（shenshen）
- **归属状态机**：`desktop_list()` / `set_on_desktop()` / `office_root_id()` 维护
  「谁在桌面」，办公区轮询 `/office/desktop` 对齐，避免同一只鲸两处同画
- **信号优先级**：office 状态（7 秒心跳）压制旧 DOM 信标；心跳失效后信标自动接管，
  避免两套工作态打架
- 单测：`tests/test_office_handoff.py`

### Token 花费统计（直读 DSH 会话日志）
- **权威数据源**：直接解析 DSH 落盘的会话日志
  `~/.dsh/sessions/*/session-*/session.jsonl.zstd`（追加式 zstd 多帧，快速解压），
  每 2 秒后台刷新，跨重启幂等、断线可补账——不依赖页面/信标
- **双口径**：本会话 = 当前工作区最新会话；累计 = 所有工作区全部会话总账
- 逐回合提取 usage（输入/输出/缓存命中/推理）与模型名，**按模型分桶、逐模型计价**
  （`deepseek-v4-pro` / `deepseek-v4-flash` / `kimi` / `claude` 等各自独立记账）
- 定价采用官方 2026-08 最新价（USD/百万），`v4-pro` / `v4-flash` / `v4-flash-vision-exp`
  为**峰谷两档**（峰谷是 DeepSeek 专属逻辑）：DeepSeek 按每个回合实际发生时间分桶
  （高峰 = UTC 周一~五 01:00-04:00、06:00-10:00），**真实花费 = 高峰用量×高峰价 +
  低谷用量×低谷价**；kimi 等单一定价模型不分峰谷、只用单价。**会话内切换模型不串价**，
  金额只增不减、不随当前时间跳变；其余模型回落默认档
- 花费估算 = token × 单价（USD/百万）× 汇率 7.2；快照持久化到 `token_ledger.json`
- **可自定义显示**：右键/托盘 →「Token 花费设置」，勾选要显示的数据、口径、数字格式
  （万·亿 / K·M / 完整），保存即预览；默认"万·亿"档已调细粒度（10 万以下完整数字、
  万/亿保留更多小数位），让增量变化肉眼可见；价格保留 4 位小数
- **价格表可视化编辑**：设置窗口里可直接修改各模型的高峰/低谷单价（USD/百万）与高峰时段
  （UTC 小时）；**可自行「添加模型」**（输入 kimi/claude/gpt 等前缀即可按前缀命中计价；
  窗口会列出会话里**实际用过的模型名**供对照，填真实名的开头即可保证命中），
  官方调价或引入新模型都无需改代码，保存立即生效、跨重启保留
  （config.json：`token_pricing` / `token_peak_hours`）
- 托盘 / 右键菜单「Token 花费统计」气泡展示（紧凑数字，不撑爆文本框）

### 情绪响应（混合引擎，一宠跟人走）
- 空闲时（工具没在跑）读用户最新消息，本地关键词情感 + LLM 升级（高情绪/低置信）
  选贴合动作播放；2 秒内响应、集合去重、`sk-*` 密钥与系统注入自动过滤
- **一宠跟人走**：全局跟随用户最新会话（跨工作区自动切换），切到哪个项目就反应哪个

### 主动关怀（久坐 / 深夜 / 卡住 / 欢迎回来）
- 移植自 dsh-whale-musume v1.8.0，四条触发线：
  - **久坐**：连续工作 25 分钟 → 提醒活动一下
  - **深夜**：深夜 23:00–6:00 连续忙 10 分钟 → 劝睡
  - **卡住**：同一任务状态停滞 8 分钟 → 换个思路
  - **欢迎回来**：离开 3 分钟以上回来 → 打招呼
- 铁律「陪着不是指挥」：**台词只在空闲时播报**，工作态绝不插嘴；关怀之间至少
  间隔 15 分钟，避免变成噪音；深夜劝睡属于关怀保留
- 每 30 秒后台检查一次；**设置界面 → 桌宠行为 →「主动关怀」**可开关四条提醒线并
  调整各阈值（分钟），保存立即生效、无需重启；底层存 `proactive_care_enabled` /
  `proactive_care_thresholds`（秒），见 `pet/proactive_care.py`

### 易用性
- `scripts/make-app.sh` 一键生成「联动桌宠.app」访达/Dock 启动器（带鲸鱼图标）
- `启动联动桌宠.command` / `停止联动桌宠.command` 双击启停
- 修复：源码运行显示 Python 图标 → 改为鲸鱼 Dock 图标；强制 `arch -arm64` 避免
  universal2 Python 在 LaunchServices 下以 x86_64 加载 arm64 依赖崩溃

---

## 🚀 快速开始（macOS）

```bash
# 1. 准备环境（Python 3.10+）
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 2. 启动桌宠
./启动联动桌宠.command          # 或生成 .app 后用访达/Dock 图标启动
# 或
bash scripts/make-app.sh        # 生成 联动桌宠.app
open "联动桌宠.app"

# 3. 刷新 DSH 页面（关键！让页内信标加载）
#    DSH 页面按 Cmd+Shift+R

# 4. 开始使用
#    右键桌宠 / 托盘 → 「Token 花费统计」「DeepSeek 余额」等
```

**DSH 侧信标注入**（一次性，已带备份与回滚）：

```bash
node scripts/inject-beacon.mjs --target "<DSH_INSTALL_DIR>"
# 回滚: node scripts/inject-beacon.mjs --rollback <backup目录>
```

> 详细架构、接口、回滚步骤见 [DSH联动说明.md](./DSH联动说明.md)。

---

## 📄 文档

- [DSH联动说明.md](./DSH联动说明.md) —— 本次二次开发的完整架构与说明
- [docs/情境感知-无处不在-2026-09-01.md](./docs/情境感知-无处不在-2026-09-01.md) —— 下一阶段方向与开发计划（情境感知）
- [docs/UPSTREAM-README.md](./docs/UPSTREAM-README.md) —— 上游作者原始 README（存档）
- docs/ —— 上游开发过程中的其他记录

---

## 🙏 致谢与版权

本项目是二次开发，尊重并保留原作者版权：

| 项目 | 作者 | 许可证 | 说明 |
| --- | --- | --- | --- |
| [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) | PC2005-cloud | MIT | 原项目：DSH 桌宠的基础交互思路、动画链行为模型与资源组织 |
| [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) | MerZlin | MIT | 本分支的基础：Python/PySide6 独立桌宠实现 |

本分支在其上新增「DSH 工作状态联动」「DSH Token 花费统计」等能力（见文首），
所有动画素材、截图等版权归原项目所有。

## License

[MIT](./LICENSE)（保留上游版权声明）