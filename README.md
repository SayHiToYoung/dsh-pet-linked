# dsh-pet-indesktop（DSH 联动版）

基于 [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)（其源于 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)）的**二次开发分支**。

在原项目"独立桌面宠物"的基础上，新增了两项 DSH 深度集成能力：

1. 🐋 **DSH 工作状态联动** —— 桌宠实时感知 DeepSeek Harness 是否在跑工具/回合，
   工作时切到「写代码 / 吃Token / 敲桌面」等认真动画并闭嘴摸鱼，收工后恢复；
2. 💰 **DSH Token 花费统计** —— 完全由 DSH 会话数据驱动（模型名 + token 用量都来自
   DSH 事件流），桌宠零 API 绑定，实时累计并估算花费。

> 数据全部走本机回环（127.0.0.1），无遥测、无外部请求。DSH 侧只注入一个只读信标脚本，
> 不修改 DSH 业务逻辑，可随时回滚。

---

## ✨ 本分支新增内容

### 工作状态联动
- 页内信标观察 DSH 的 `data-state="ongoing"` 等 DOM 信号（300ms 去抖 + 心跳）
- 桌宠 `_pick_next` 增加工作态分支：只播「工作池动画 + 待机」，不移动、不闲聊
- 开工/收工有气泡反馈

### Token 花费统计（直读 DSH 会话日志）
- **权威数据源**：直接解析 DSH 落盘的会话日志
  `~/.dsh/sessions/*/session-*/session.jsonl.zstd`（追加式 zstd 多帧，快速解压），
  每 5 秒后台刷新，跨重启幂等、断线可补账——不依赖页面/信标
- **双口径**：本会话 = 当前工作区最新会话；累计 = 所有工作区全部会话总账
- 逐回合提取 usage（输入/输出/缓存命中/推理）与模型名，按实际模型自动选定价档
  （`deepseek-v4-flash` / `deepseek-chat` / `deepseek-reasoner`）
- 花费估算 = token × 单价（USD/百万）× 汇率 7.2；快照持久化到 `token_ledger.json`
- **可自定义显示**：右键/托盘 →「Token 花费设置」，勾选要显示的数据、口径、数字格式
  （万·亿 / K·M / 完整），保存即预览
- 托盘 / 右键菜单「Token 花费统计」气泡展示（紧凑数字，不撑爆文本框）

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