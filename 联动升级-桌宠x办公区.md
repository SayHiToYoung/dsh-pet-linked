# 桌宠 × 办公区 联动升级方案(含「搬家」)

> 目标:让 `dsh-pet-indesktop`(桌面独立 Qt 桌宠)和 `dsh-agent-office`(DSH web 插件·多智能体办公区)从"各管各"变成一个整体。基于两仓库现有代码,不推倒重来。

## 0. 现状盘点(读代码得到的事实)

| | dsh-pet-indesktop(桌宠) | dsh-agent-office(办公区) |
|---|---|---|
| 形态 | 独立 Qt/PySide6 桌面进程 | DSH/Cordis web 插件(worker + 浏览器注入) |
| 数据源 | 直读 `~/.dsh/sessions/*.jsonl.zstd` | 直读 `~/.dsh/sessions/*.jsonl.zstd`(**同一份**) |
| 立绘 | 鲸娘(whale-musume) | 鲸娘(whale-musume,**同一套** `dsh-whale-state-*.webp`) |
| 已算出的状态 | 整体 working/idle(DOM 信标)+ token 账本 + 用户情绪 | **每只鲸娘的 per-agent 状态 + 智能体树**(`lib/status.js` → `scanStatus`) |
| 对外接口 | 本地 HTTP `127.0.0.1:47890`(`/state` `/usage` `/emote` `on_change/on_usage/on_emote`) | 只读路由 `/api/dsh-agent-office/status`(整棵树+实时状态) |

**断点**:桌宠看到的是"DSH 整体在不在忙"(信标,粗),对 office 的每只鲸娘状态一无所知;office 也不知道桌宠存在。两者同源却不通。

`scanStatus()` 返回结构(权威、已现成):
```jsonc
{ "workspace","session","now",
  "agents":[ { "id","label"/*主控鲸/小鲸*/,"title","origin"/*root|sub*/,"parent","depth",
               "model","state"/*idle|thinking|tool|waiting|writing|done|assigned|error*/,
               "text","detail","currentTool","busy","tokens","turn","step","activeAt","error" } ] }
```

---

## 1. 分三层,搬家是顶层(但 L1 是它的前提)

### L1 — 数据打通:桌宠订阅 office 的 status(office → 桌宠,最小改动)

新增 `pet/office_link.py`:一个后台轮询(复用桌宠现成的 2s 轮询模式),GET
`http://127.0.0.1:<DSH_WEB_PORT>/api/dsh-agent-office/status`,拿到整棵鲸娘树,按聚合规则驱动现有方法:

- **任一 `waiting`(等批准)→ 桌宠转身戳你**。审批是全流程里唯一真正需要人的节点,而 DSH 常缩在后台、桌宠常驻可见——这是**最高性价比的联动**,DOM 信标根本给不了。接 `window.react_to_emotion` / 冒气泡"有个分身在等你点头"。
- **主控鲸(origin=root)的 state → 桌宠主情绪/主动画**;有任一 busy → 干活态(可气泡"3 只在忙");全 `done` → 庆祝;全 `idle` → 摸鱼。接现有 `set_work_state`。
- **office `/status` 成为权威工作态源**;原 DOM 信标降级为"没装 office 插件时的兜底"(避免两套工作态打架——**务必别让两个信号各算一套**)。

需要的只有 DSH web 端口:`lib/index.js` 启动时会 `logger.info` 打出 `webServer.host/port`,填进桌宠 `config.json`(如 `office_status_port`),取不到就回落信标。

> L1 做完,你已经能感受到质变:桌宠开始"懂"每只分身,尤其审批召唤。而且它是搬家的前提——搬家要靠这里的 per-agent 身份和状态。

### L2 — 可选:改成 office 推送

让 office worker(`index.js`)算完 status 主动 POST 到 47890。但 `scanStatus` 目前是**按 HTTP 请求懒算**的、worker 里没有后台循环,要推送得自己加 timer。相比之下"桌宠拉"直接复用现成路由、**office 零改动**。除非要极低延迟,否则 **L1 已够,推荐先 L1**。

### L3 — 搬家(cross-surface handoff)

因为两边同一套鲸娘立绘,"搬家" = **某只鲸娘离开办公室、走到桌面变成桌宠**(反之走回)。身份连续性天然成立(传同一个 sprite/preset id 即可)。

**归属状态机**(避免同一只鲸被两处同时画):每个 `agentId` 处于
`at_office | traveling | at_desktop`,同一时刻只有一处渲染它。

桌宠有 HTTP server,天然当**协调者**。给 `work_state.py` 加两个端点:
```
GET  /office/desktop         -> {"agents":["<id>", ...]}   // 谁已在桌面
POST /office/handoff         <- {"agentId","dir":"to_desktop|to_office",
                                 "fromScreen":{"x","y"},"sprite","label","model"}
```

**office → 桌面**
1. office.js 里点某只鲸(或她头上"送去桌面"钮)→ 算她的**屏幕坐标**:
   `elemRect(getBoundingClientRect) + {window.screenX, window.screenY}`,乘 `devicePixelRatio` 换算(见坐标坑)。
2. POST `/office/handoff {agentId, dir:"to_desktop", fromScreen, sprite, label, model}`。
3. office 引擎给她演"走到窗口边缘 + 淡出",并把该 id 记入本地隐藏集;之后每轮渲染跳过 `/office/desktop` 返回的 id(下次轮询就不再画她)。
4. 桌宠收到 → `PetWindow.move(x,y)` 到 `fromScreen`(DSH 窗口边)→ 播"从边缘走入"→ 采用该鲸身份,并在 L1 的 status 里**只镜像这只鲸**的状态(她 tool 桌宠就敲键盘、她 waiting 桌宠就戳你)。

**桌面 → office(反向)**
1. 点桌宠(或右键"回办公室")→ 桌宠用 `frameGeometry()` 取自己全局坐标 → POST `dir:"to_office"`。
2. 桌宠演"走向 DSH 窗口 + 淡出",从 `desktop_agents` 移除该 id。
3. office 轮询 `/office/desktop` 发现她回来了 → 演"从大门走回最近空工位"(引擎已有走位逻辑 `DESK_XS`/空地)。

**连续飞行(可选打磨)**:想要真正跨越两窗口之间空隙的连续飞行,再加第三个**全屏透明 + 鼠标穿透 + 置顶**的 Qt 覆盖窗,在 `fromScreen → toScreen` 间用一帧帧画飞行分身(office 隐藏 + 桌宠隐藏,只有覆盖窗在画)。**但先做"边缘走入/淡出"版本就已足够连续**(Marvis 级神似),覆盖窗留作二期。

---

## 2. 改动点清单(落到真实文件)

### 桌宠侧(dsh-pet-indesktop)
| 文件 | 改动 |
|---|---|
| `pet/office_link.py` | **新增**:轮询 office `/status` → 聚合规则 → 回调桌宠;维护 `desktop_agents` 与镜像目标 agentId |
| `pet/work_state.py` | 加端点 `GET /office/desktop`、`POST /office/handoff`;新增 `on_handoff` 回调 |
| `pet/app.py` | 拉起 `office_link`;把 handoff 回调桥到主线程 → `window` 的搬家动画;config 读 `office_status_port` |
| `pet/window.py` | 新增 `enter_from_edge(from_xy, sprite, identity)` / `leave_to_edge(target_xy)` 两段动画;`mirror_agent(agent_state)`(把某只鲸的 state 映射到现有动画,复用 `set_work_state` 的映射表) |
| `config.json` | `office_status_port`(DSH web 端口)、`handoff_default_agent`(见下方待定项) |

### 办公区侧(dsh-agent-office)
| 文件 | 改动 |
|---|---|
| `assets/office.js` | 每只鲸加"送去桌面"交互;`screenCoordsOf(el)`;POST 桌宠 47890;渲染时跳过 `/office/desktop` 里的 id;反向到位后复位 |
| `assets/office.css` | 走入/淡出边缘的过渡类;"送去桌面"钮样式 |
| `lib/client.js` / `ASSET_V` | 改了 assets 记得 `ASSET_V +1`(避免 3600s 旧缓存) |

> 两侧都**保持零侵入 DSH**:office 仍只读 `~/.dsh`、不碰业务;桌宠没开时 office 的 POST 静默失败(照抄信标那套容错);office 没装时桌宠回落信标。全程本机回环。

---

## 3. 坐标坑(macOS,务必注意)
- **逻辑点 vs 物理像素**:浏览器 `getBoundingClientRect` 是 CSS px = 逻辑点;Qt `move()` 也是逻辑点——大体对得上。Retina 下若用到物理像素,统一除以/乘 `devicePixelRatio`,别混。
- **多显示器偏移**:`window.screenX/Y` 是相对**整个虚拟桌面**的;Qt `frameGeometry()`/`screen().geometry()` 同样是全局坐标系。跨屏时以各自 screen 的 geometry 为基准换算。
- **飞行途中窗口被拖动**:以 handoff `begin` 时刻的坐标快照为准即可(短动画,拖动概率低);要严谨就让覆盖窗版本实时跟随源/目标窗口 bounds。
- **降级**:取不到屏幕坐标(权限/接口异常)时,直接淡出→在目标默认位淡入,不做走位,保证不崩。

---

## 4. 建议实施顺序
1. **L1**:`office_link.py` 拉 status + 审批召唤 + 主控鲸情绪(半天,立刻见效)。
2. **搬家最小闭环**:单只鲸(默认主控鲸)双向搬家,用"边缘走入/淡出",不上覆盖窗。
3. **多只 + 交互打磨**:点谁谁来、桌面同时站多只、覆盖窗连续飞行。

## 5. 一个待定项(影响 L3 代码)
桌宠代表谁?
- (a) 固定 = 主控鲸(桌宠始终是队长,分身只在办公室);
- (b) 用户点谁谁来(桌面可站任意/多只);
- (c) 桌宠 = 全队聚合(一只小人代表整支队伍的情绪,不搬具体某只)。
这决定 `mirror_agent` 是镜像单个 id 还是聚合,建议先按 (a) 做最小闭环,再放开到 (b)。

---

## 附:已实现「搬家最小闭环」(固定主控鲸)—— 本次改动

采用「office 主动推送」而非桌宠拉取,桌宠无需知道 DSH web 端口。

### 改动文件
| 仓库 | 文件 | 改动 |
|---|---|---|
| 桌宠 | `pet/work_state.py` | 新增端点 `POST /office/root`(镜像主控鲸)、`POST /office/handoff`(搬家)、`GET /office/desktop`;`WorkStateServer` 增 `on_root/on_handoff` 回调 + `_desktop` 归属集合 |
| 桌宠 | `pet/window.py` | 新增 `mirror_agent()`(复用 `set_work_state/react_to_emotion/show_bubble` + 审批召唤 + 干完庆祝)、`handoff_enter()/handoff_leave()/_handoff_glide()`(边缘走入/走回滑行 + 淡入) |
| 桌宠 | `pet/app.py` | `_WorkStateBridge` 增 `office_root/handoff` 信号(线程→主线程);`WorkStateServer` 接 `on_root/on_handoff` |
| 办公区 | `assets/office.js` | 每轮把主控鲸状态+审批数 POST 到桌宠 `47890/office/root`(指纹去重);foot 加「送去桌面/叫回办公室」开关 → `POST /office/handoff`(带屏幕坐标);`sentToDesktop` 期间办公区隐藏主控鲸 |
| 办公区 | `assets/office.css` | `.ao-tohome` 开关样式 |
| 办公区 | `lib/client.js` | `ASSET_V` +1(避免旧缓存) |

### 数据流
```
DSH 会话日志 → office status.js →(office.js 每 1.6s)→ POST 桌宠 /office/root → mirror_agent()
                                                    → 审批数>0 → 桌宠戳你「有 N 个分身等你点头」
foot「送去桌面」→ POST /office/handoff{dir,fromScreen} → 桌宠 handoff_enter() 从 DSH 窗口边走入
             ← office 隐藏主控鲸(sentToDesktop)
foot「叫回办公室」→ dir=to_office → 桌宠 handoff_leave() 走回 → office 复现主控鲸
```

### 如何验证
1. 桌宠:`bash 启动联动桌宠.command`(或已装 .app 直接开)。
2. 办公区:确保插件已装并 `ASSET_V` 生效——DSH web 硬刷新 `Cmd+Shift+R`;右下角 🐋 展开办公区。
3. 起一个会跑工具/带子代理的 DSH 任务:桌宠应随主控鲸切「认真工作」动画;有子代理等批准时桌宠冒泡戳你。
4. 办公区底栏点「🏠 送去桌面」:主控鲸从办公室消失,桌宠从 DSH 窗口边走入并冒泡;再点「叫回办公室」走回、办公区复现主控鲸。
5. 排查:`curl -s 127.0.0.1:47890/office/desktop`(看归属集合);桌宠日志 `~/Library/Application Support/dsh-pet-standalone/pet.log`;office 推送失败会静默(桌宠没开不影响)。

### 已知取舍 / 后续
- 坐标为近似(浏览器 chrome 高度估算 + Retina 逻辑点),多屏或缩放下可能偏几十像素——桌宠已对屏幕可用区做 clamp,不会飞出屏。要精确再上「透明鼠标穿透覆盖窗」做连续飞行。
- 桌宠常驻,故「回办公室」后桌宠仍在桌面(只是不再强制镜像/走位);如需真正「消失」可在 handoff_leave 末尾 hide()。
- 目前固定主控鲸;放开到「点谁谁来/多只」时,把 mirror 的单 id 换成按 agentId 分别镜像即可(见 §5 待定项)。
