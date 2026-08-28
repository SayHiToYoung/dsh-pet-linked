/**
 * dsh-work-beacon —— DSH 工作状态信标（二次开发注入脚本）。
 *
 * 运行在 DSH 前端页面内（经 index.html 注入），做两件事：
 *  1. 工作状态：观察 "正在工作" 的 DOM 信号（data-state="ongoing" 等），
 *     状态一变就 POST 给桌宠（127.0.0.1:47890/state）。
 *  2. Token 用量：包装 window.WebSocket 截获 DSH 事件流里的 usage 与模型
 *     （assistant/chunk 的 usage / assistant/message 的 usage + message.source.model），
 *     按增量 POST 给桌宠（127.0.0.1:47890/usage），桌宠据此估算花费。
 *     数据源完全是 DSH 自己的会话信息，桌宠不需要任何 API key。
 *
 * 设计原则：
 *  - 只读，不触碰 DSH 业务逻辑；失败静默（桌宠没开时完全不影响页面）；
 *  - 防重复注入；去抖 + 心跳；按 (turn,step) 去重避免同一回合重复计数；
 *  - 包装 WebSocket 时完整保留原语义。
 */
(function () {
  "use strict";
  if (window.__DSH_WORK_BEACON__) return;
  window.__DSH_WORK_BEACON__ = true;

  var VERSION = "v5-usage";   // 信标版本（诊断用：确认页面加载的是新版）
  var WS_TAPPED = false;      // WebSocket 截获是否成功挂上

  var PORT = (window.__DSH_WORK_BEACON_PORT__ | 0) || 47890;
  var STATE_ENDPOINT = "http://127.0.0.1:" + PORT + "/state";
  var USAGE_ENDPOINT = "http://127.0.0.1:" + PORT + "/usage";

  // ---------------------------------------------------------------- 工作状态
  var TOOL_SELECTORS = [
    '[data-state="ongoing"]',
    '[data-status="running"]',
    '[data-running]',
    '[data-role="tool"][data-state="running"]'
  ];
  var THINKING_SELECTORS = [
    '[data-state="loading"]',
    '[data-status="pending"]',
    '[aria-busy="true"]'
  ];

  var lastWorking = null;
  var lastDetail = null;
  var debounceTimer = null;
  var heartbeatTimer = null;
  var scanTimer = null;

  function query(selectors) {
    for (var i = 0; i < selectors.length; i++) {
      try {
        if (document.querySelector(selectors[i])) return true;
      } catch (e) { /* 非法选择器跳过 */ }
    }
    return false;
  }

  function currentState() {
    if (window.__DSH_WORK_BEACON_FORCE__ !== undefined) {
      return { working: !!window.__DSH_WORK_BEACON_FORCE__, detail: "manual" };
    }
    if (query(TOOL_SELECTORS)) return { working: true, detail: "tool" };
    if (query(THINKING_SELECTORS)) return { working: true, detail: "thinking" };
    return { working: false, detail: "" };
  }

  function sendState(force) {
    var st = currentState();
    if (!force && st.working === lastWorking && st.detail === lastDetail) return;
    lastWorking = st.working;
    lastDetail = st.detail;
    var body = JSON.stringify({
      working: st.working, detail: st.detail, beacon: VERSION, wsTap: WS_TAPPED,
      diag: { framesSeen: DIAG.framesSeen, usageSeen: DIAG.usageSeen,
              usagePosted: DIAG.usagePosted, postFailures: DIAG.postFailures,
              lastUsageAt: DIAG.lastUsageAt,
              pending: usagePending.input + usagePending.output + usagePending.cacheRead + usagePending.reasoning }
    });
    try {
      fetch(STATE_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        keepalive: true
      }).catch(function () { /* 静默 */ });
    } catch (e) { /* 静默 */ }
  }

  function scheduleState() {
    if (debounceTimer) return;
    debounceTimer = setTimeout(function () {
      debounceTimer = null;
      sendState(false);
    }, 300);
  }

  // ---------------------------------------------------------------- Token 用量（DSH 驱动）
  var usagePending = { input: 0, output: 0, cacheRead: 0, reasoning: 0 };
  var usagePostTimer = null;
  var usageDebounceMs = 500;
  var lastModel = "";          // 最近一次抓到的模型名
  var seenUsageKeys = {};      // "turn:step" 去重
  var seenUsageCount = 0;
  // 诊断计数
  var DIAG = { framesSeen: 0, usageSeen: 0, usagePosted: 0, postFailures: 0, lastUsageAt: 0 };

  /** 从事件里取 {usage, model, turn, step}（assistant/message 与 assistant/chunk）。 */
  function extractUsageInfo(event) {
    if (!event || typeof event !== "object") return null;
    var info = null;
    if (event.type === "assistant/message" && event.data) {
      if (event.data.usage) {
        info = info || {};
        info.usage = event.data.usage;
      }
      if (event.data.message && event.data.message.source && event.data.message.source.model) {
        info = info || {};
        info.model = event.data.message.source.model;
      }
      if (event.data.turn !== undefined) { info = info || {}; info.turn = event.data.turn; }
      if (event.data.step !== undefined) { info = info || {}; info.step = event.data.step; }
    }
    if (event.type === "assistant/chunk" && event.data && event.data.chunk &&
        event.data.chunk.type === "usage" && event.data.chunk.usage) {
      info = info || {};
      info.usage = event.data.chunk.usage;
      if (event.data.turn !== undefined) info.turn = event.data.turn;
      if (event.data.step !== undefined) info.step = event.data.step;
    }
    if (!info || !info.usage) {
      // 兜底递归：找 {inputTokens, outputTokens} 形状
      var found = (function scan(obj, depth) {
        if (!obj || typeof obj !== "object" || depth > 6) return null;
        if (typeof obj.inputTokens === "number" && typeof obj.outputTokens === "number") return obj;
        for (var k in obj) {
          if (Object.prototype.hasOwnProperty.call(obj, k)) {
            var r = scan(obj[k], depth + 1);
            if (r) return r;
          }
        }
        return null;
      })(event, 0);
      if (found) { info = info || {}; info.usage = found; }
    }
    return info && info.usage ? info : null;
  }

  function reportUsage(usage, model) {
    usagePending.input += usage.inputTokens || 0;
    usagePending.output += usage.outputTokens || 0;
    usagePending.cacheRead += usage.cacheReadTokens || 0;
    usagePending.reasoning += usage.reasoningTokens || 0;
    if (model) lastModel = model;
    DIAG.usageSeen += 1;
    DIAG.lastUsageAt = Date.now();
    if (usagePostTimer) return;
    usagePostTimer = setTimeout(function () {
      usagePostTimer = null;
      var pending = usagePending;
      usagePending = { input: 0, output: 0, cacheRead: 0, reasoning: 0 };
      var body = JSON.stringify({
        inputTokens: pending.input,
        outputTokens: pending.output,
        cacheReadTokens: pending.cacheRead,
        reasoningTokens: pending.reasoning,
        model: lastModel
      });
      try {
        fetch(USAGE_ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body,
          keepalive: true
        }).then(function (res) {
          if (res.ok) { DIAG.usagePosted += 1; }
          else { DIAG.postFailures += 1; usagePending.input += pending.input; usagePending.output += pending.output; usagePending.cacheRead += pending.cacheRead; usagePending.reasoning += pending.reasoning; }
        }).catch(function () {
          // 上报失败：把增量补回待发池，下次心跳重试
          DIAG.postFailures += 1;
          usagePending.input += pending.input;
          usagePending.output += pending.output;
          usagePending.cacheRead += pending.cacheRead;
          usagePending.reasoning += pending.reasoning;
        });
      } catch (e) {
        DIAG.postFailures += 1;
        usagePending.input += pending.input;
        usagePending.output += pending.output;
        usagePending.cacheRead += pending.cacheRead;
        usagePending.reasoning += pending.reasoning;
      }
    }, usageDebounceMs);
  }

  /** 递归扫描任意帧里的 model 字段（不依赖固定路径，兜底用）。 */
  function findModel(obj, depth) {
    if (!obj || typeof obj !== "object" || depth > 8) return null;
    if (typeof obj.model === "string" && obj.model.length >= 2 && obj.model.length <= 120) {
      return obj.model;
    }
    for (var k in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, k)) {
        var r = findModel(obj[k], depth + 1);
        if (r) return r;
      }
    }
    return null;
  }

  /** 处理一帧：更新模型名；去重后上报 usage。 */
  function handleFrame(event) {
    // 任何 assistant/message 都顺手记下模型名（即使本帧不带 usage）
    if (event && event.type === "assistant/message" && event.data && event.data.message &&
        event.data.message.source && event.data.message.source.model) {
      lastModel = String(event.data.message.source.model).slice(0, 120);
    } else if (!lastModel && event) {
      var m = findModel(event, 0);
      if (m) lastModel = String(m).slice(0, 120);
    }
    var info = extractUsageInfo(event);
    if (!info) return;
    var key = (info.turn !== undefined ? info.turn : "?") + ":" + (info.step !== undefined ? info.step : "?");
    if (key !== "?:") {
      if (seenUsageKeys[key]) return;   // 同一 (turn,step) 只计一次
      seenUsageKeys[key] = true;
      seenUsageCount += 1;
      if (seenUsageCount > 2000) {      // 防内存无限增长
        var keys = Object.keys(seenUsageKeys);
        for (var i = 0; i < Math.floor(keys.length / 2); i++) delete seenUsageKeys[keys[i]];
        seenUsageCount = Object.keys(seenUsageKeys).length;
      }
    }
    reportUsage(info.usage, info.model);
  }

  function captureMessage(ev) {
    var data = ev && ev.data;
    if (typeof data !== "string") return; // 二进制帧跳过
    DIAG.framesSeen += 1;
    var frame;
    try { frame = JSON.parse(data); } catch (e) { return; }
    // 服务器帧形如 {type:"server-request", payload:{...事件...}}
    var event = (frame && frame.payload && typeof frame.payload.type === "string") ? frame.payload : frame;
    handleFrame(event);
  }

  /** 包装 window.WebSocket：截获 message，保留原语义。 */
  function installWebSocketTap() {
    var NativeWS = window.WebSocket;
    if (!NativeWS || window.__DSH_WS_TAPPED__) return;
    window.__DSH_WS_TAPPED__ = true;

    var proto = NativeWS.prototype;
    var origAdd = proto.addEventListener;
    var onMsgDesc = Object.getOwnPropertyDescriptor(proto, "onmessage");

    if (origAdd) {
      proto.addEventListener = function (type, listener, options) {
        if (type === "message") {
          var wrapped = function (ev) { captureMessage(ev); return listener.call(this, ev); };
          return origAdd.call(this, type, wrapped, options);
        }
        return origAdd.call(this, type, listener, options);
      };
    }
    if (onMsgDesc && onMsgDesc.set) {
      Object.defineProperty(proto, "onmessage", {
        get: onMsgDesc.get,
        set: function (fn) {
          var wrapped = typeof fn === "function"
            ? function (ev) { captureMessage(ev); return fn.call(this, ev); }
            : fn;
          return onMsgDesc.set.call(this, wrapped);
        },
        configurable: true
      });
    }
    WS_TAPPED = true;
  }

  /** 心跳时把积压的用量重试发出去（桌宠重启/短暂离线后自愈）。 */
  function flushUsagePending() {
    if (usagePostTimer) return;
    var total = usagePending.input + usagePending.output + usagePending.cacheRead + usagePending.reasoning;
    if (total <= 0) return;
    var pending = usagePending;
    usagePending = { input: 0, output: 0, cacheRead: 0, reasoning: 0 };
    var body = JSON.stringify({
      inputTokens: pending.input,
      outputTokens: pending.output,
      cacheReadTokens: pending.cacheRead,
      reasoningTokens: pending.reasoning,
      model: lastModel
    });
    try {
      fetch(USAGE_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        keepalive: true
      }).then(function (res) {
        if (res.ok) { DIAG.usagePosted += 1; }
        else { DIAG.postFailures += 1; usagePending.input += pending.input; usagePending.output += pending.output; usagePending.cacheRead += pending.cacheRead; usagePending.reasoning += pending.reasoning; }
      }).catch(function () {
        DIAG.postFailures += 1;
        usagePending.input += pending.input;
        usagePending.output += pending.output;
        usagePending.cacheRead += pending.cacheRead;
        usagePending.reasoning += pending.reasoning;
      });
    } catch (e) {
      DIAG.postFailures += 1;
      usagePending.input += pending.input;
      usagePending.output += pending.output;
      usagePending.cacheRead += pending.cacheRead;
      usagePending.reasoning += pending.reasoning;
    }
  }

  // ---------------------------------------------------------------- 启动
  // 1) WebSocket 包装要在页面创建连接之前 —— 本脚本是 <head> 里的普通脚本，
  //    在 app 的 defer 模块执行前运行，时机正确。
  try { installWebSocketTap(); } catch (e) { /* 包装失败不影响其余 */ }

  // 2) 工作状态观察
  try {
    var observer = new MutationObserver(scheduleState);
    observer.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["data-state", "data-status", "data-running", "data-role", "aria-busy"]
    });
  } catch (e) { /* 退化轮询 */ }

  scanTimer = setInterval(function () { sendState(false); }, 300);
  heartbeatTimer = setInterval(function () { sendState(true); flushUsagePending(); }, 8000);

  if (document.readyState === "complete") {
    sendState(true);
  } else {
    window.addEventListener("load", function () { sendState(true); });
  }
})();