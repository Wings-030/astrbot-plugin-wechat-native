const bridge = window.AstrBotPluginView;
const status = document.getElementById("connection-status");
const online = document.getElementById("wechat-status");
const reason = document.getElementById("failure-reason");
const button = document.getElementById("test-connection");
const select = document.getElementById("robot-select");
const chart = document.getElementById("daily-chart");
const detail = document.getElementById("chart-detail");
const number = new Intl.NumberFormat("zh-CN");
const chartHint = "将指针移到柱形上或用 Tab 查看每日数量";
let checking = false;
let botsLoaded = false;
let chartDays = [];
let viewRevision = 0;
const loginPanel = document.getElementById("login-panel");
const loginStatus = document.getElementById("login-status");
const loginQr = document.getElementById("login-qr");
const loginRetry = document.getElementById("login-retry");
const loginEntries = new Map();

function currentView(botId, revision) {
  return select.value === botId && revision === viewRevision;
}

function clearQr() {
  loginQr.hidden = true;
  loginQr.removeAttribute("src");
  delete loginQr.dataset.generation;
}

async function renderLogin(entry, result, botId, revision) {
  if (!currentView(botId, revision) || document.hidden || !entry.allowed) return;
  entry.state = result.state;
  entry.error = false;
  entry.terminal = ["expired", "failed", "rejected", "cancelled", "needs_login", "needs_review"].includes(result.state);
  entry.verify = result.state === "succeeded";
  loginPanel.hidden = false;
  loginRetry.hidden = !entry.terminal && !entry.verify && result.state !== "pending";
  loginRetry.disabled = false;
  loginStatus.textContent = {
    idle: "正在准备登录…", pending: "登录准备尚未完成，请重新获取以恢复原任务。",
    queued: "正在准备登录…", running: "正在准备登录…",
    starting: "正在准备登录…", resuming: "正在尝试恢复已有设备登录…",
    authenticating: "手机已确认，正在完成账号认证…",
    awaiting_scan: "请使用手机微信扫码登录。",
    awaiting_approval: "请在手机微信中确认登录。",
    succeeded: "登录任务已完成，正在确认微信上线。仍未上线时可重新获取二维码。",
    expired: "二维码或登录任务已过期，请重新获取二维码。",
    failed: "登录失败，请检查服务后重新获取二维码。",
    rejected: "手机端已拒绝登录，可重新获取二维码。",
    cancelled: "登录任务已取消，可重新获取二维码。",
    needs_login: "需要重新登录，请重新获取二维码。",
    needs_review: "登录结果待确认，请检查账号后手动重试。",
  }[result.state] || "登录状态暂不可用，请刷新检查。";
  if (result.state !== "awaiting_scan" || result.qr_available !== true) {
    clearQr();
    return;
  }
  const generation = result.qr_generation;
  if (!Number.isInteger(generation) || generation < 1) throw new Error("invalid QR generation");
  if (loginQr.dataset.generation === String(generation) && loginQr.hasAttribute("src")) return;
  clearQr();
  const qr = await bridge.apiGet("connection/login/qr", {bot_id: botId, generation});
  if (!currentView(botId, revision) || document.hidden || !entry.allowed) return;
  if (qr.generation !== generation || typeof qr.image !== "string"
      || !/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(qr.image) || qr.image.length > 1400000) {
    throw new Error("invalid QR image");
  }
  loginQr.src = qr.image;
  loginQr.dataset.generation = String(generation);
  loginQr.hidden = false;
}

function loginFailure(entry, botId, revision) {
  if (!currentView(botId, revision) || document.hidden || !entry.allowed) return;
  entry.terminal = true;
  entry.error = true;
  entry.verify = false;
  clearQr();
  loginPanel.hidden = false;
  loginStatus.textContent = "登录请求失败，请检查管理员权限、Admin Token 与 Native 服务后重试。";
  loginRetry.hidden = false;
  loginRetry.disabled = false;
}

async function beginLogin(botId, revision, retry = false) {
  const entry = loginEntries.get(botId);
  if (entry.busy || document.hidden) return;
  entry.busy = entry.started = true;
  entry.terminal = entry.verify = false;
  entry.error = false;
  clearQr();
  loginPanel.hidden = false;
  loginStatus.textContent = "正在准备登录…";
  loginRetry.disabled = true;
  loginRetry.hidden = true;
  try {
    const params = retry ? {bot_id: botId, retry: true} : {bot_id: botId};
    await renderLogin(entry, await bridge.apiPost("connection/login/start", params), botId, revision);
  } catch {
    loginFailure(entry, botId, revision);
  } finally {
    entry.busy = false;
  }
}

async function pollLogin() {
  const botId = select.value;
  const revision = viewRevision;
  const entry = loginEntries.get(botId);
  if (document.hidden || !entry?.started || !entry.allowed || entry.busy || entry.completed) return;
  if (entry.verify) {
    await checkConnection();
    return;
  }
  if (entry.terminal) return;
  entry.busy = true;
  try {
    await renderLogin(entry, await bridge.apiGet("connection/login/status", {bot_id: botId}), botId, revision);
    if (entry.verify && currentView(botId, revision)) await checkConnection();
  } catch {
    loginFailure(entry, botId, revision);
  } finally {
    entry.busy = false;
  }
}

function updateLogin(result, botId, revision) {
  let entry = loginEntries.get(botId);
  if (result.health === "online") {
    if (entry) { entry.completed = true; entry.allowed = false; }
    clearQr();
    loginPanel.hidden = true;
    return;
  }
  if (result.health === "offline" && result.auth_state === "unauthenticated") {
    if (!entry) {
      entry = {started: false, busy: false};
      loginEntries.set(botId, entry);
    }
    entry.allowed = true;
    if (entry.completed) {
      entry.completed = false;
      void renderLogin(entry, {state: "needs_login", qr_available: false}, botId, revision);
    } else if (!entry.started) {
      void beginLogin(botId, revision);
    } else if (entry.terminal) {
      if (entry.error) loginFailure(entry, botId, revision);
      else void renderLogin(entry, {state: entry.state, qr_available: false}, botId, revision);
    } else if (entry.verify) {
      void renderLogin(entry, {state: "succeeded", qr_available: false}, botId, revision);
    } else if (!entry.terminal && !entry.verify) {
      void pollLogin();
    }
    return;
  }
  clearQr();
  if (entry) entry.allowed = result.health === "offline" && entry.verify;
  loginPanel.hidden = !entry?.allowed;
  if (entry?.allowed) {
    loginStatus.textContent = "登录已完成，等待微信连接上线…";
    loginRetry.hidden = true;
  }
}

function render(state, wechatOnline = null, failureReason = "") {
  status.dataset.state = state;
  status.textContent = {
    unchecked: "未检测", checking: "检测中",
    connected: "已连接", failed: "连接失败",
  }[state];
  online.dataset.state = wechatOnline === true ? "online" : wechatOnline === false ? "offline" : "unknown";
  online.textContent = wechatOnline === true ? "在线" : wechatOnline === false ? "离线" : "未检测";
  reason.hidden = !failureReason;
  reason.textContent = failureReason;
}

function svgElement(name, attributes, text) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderChart() {
  const focusedDate = chart.contains(document.activeElement)
    ? document.activeElement.getAttribute("data-day") : null;
  let focusedGroup;
  chart.replaceChildren();
  const populated = chartDays.some(day => day.received !== null);
  document.getElementById("chart-empty").hidden = populated;
  if (!chartDays.length) return;
  const width = Math.max(240, chart.clientWidth);
  const height = 180;
  const left = 34;
  const plotHeight = 140;
  const step = (width - left - 6) / 7;
  const barWidth = Math.min(12, (step - 8) / 2);
  const max = Math.max(1, ...chartDays.flatMap(day => [day.received || 0, day.replied || 0]));
  const tick = Math.max(1, Math.ceil(max / 3));
  const ceiling = tick * 3;
  chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  for (let i = 0; i <= 3; i++) {
    const y = 8 + plotHeight * (1 - i / 3);
    chart.append(svgElement("line", {x1: left, x2: width, y1: y, y2: y, class: "grid-line"}));
    chart.append(svgElement("text", {x: left - 8, y: y + 4, "text-anchor": "end"}, number.format(i * tick)));
  }
  for (const [index, day] of chartDays.entries()) {
    const center = left + step * (index + 0.5);
    const label = day.received === null
      ? `${day.date} · 未采集`
      : `${day.date} · 接收 ${number.format(day.received)} · 已回复 ${number.format(day.replied)} · 失败 ${number.format(day.failed)} · 部分发送 ${number.format(day.partial)}`;
    const group = svgElement("g", {"data-day": day.date, tabindex: "0", role: "img", "aria-label": label});
    group.append(svgElement("title", {}, label));
    group.append(svgElement("rect", {x: center - step / 2 + 2, y: 4, width: step - 4, height: 148, rx: 4, class: "day-hit"}));
    for (const [series, offset] of [["received", -barWidth - 1.5], ["replied", 1.5]]) {
      const value = day[series];
      if (value === null) continue;
      const barHeight = value / ceiling * plotHeight;
      group.append(svgElement("rect", {x: center + offset, y: 8 + plotHeight - barHeight, width: barWidth, height: barHeight, rx: 2, class: series}));
    }
    if (day.received === null) group.append(svgElement("text", {x: center, y: 140, "text-anchor": "middle"}, "—"));
    group.append(svgElement("text", {x: center, y: 172, "text-anchor": "middle"}, day.date.slice(5).replace("-", "/")));
    group.addEventListener("pointerenter", () => { detail.textContent = label; });
    group.addEventListener("focus", () => { detail.textContent = label; });
    chart.append(group);
    if (day.date === focusedDate) focusedGroup = group;
  }
  focusedGroup?.focus({preventScroll: true});
}

function renderPanel(result, message = "") {
  const stats = result?.statistics;
  const available = stats?.available === true;
  for (const [id, value] of [
    ["today-received", stats?.today?.received],
    ["today-replied", stats?.today?.replied],
    ["today-failed", stats?.today?.failed],
    ["pending-review", stats?.pending_review],
  ]) {
    document.getElementById(id).textContent = available && Number.isInteger(value) ? number.format(value) : "—";
  }
  chartDays = available ? stats.days : [];
  detail.textContent = chartHint;
  renderChart();
  document.getElementById("panel-note").textContent = message || (available
    ? `按消息接收日汇总当前状态 · 时区 ${stats.timezone} · 开始采集 ${stats.since.slice(0, 16).replace("T", " ")} · 首日可能不足全天`
    : "暂无统计，请先启动升级后的机器人。已采集数据会在重启后保留。");
  const config = result?.config || {};
  const mode = {mention: "仅 @回复", all: "全部回复", probability: "概率回复", disabled: "关闭"};
  const fields = [
    ["私聊回复", "enable_private_reply"], ["群聊模式", "group_reply_mode"],
    ["媒体识别", "enable_media"], ["群聊摘要", "enable_group_summary"],
    ["历史工具", "enable_history_tools"], ["微信操作工具", "enable_wechat_actions"],
    ["红包暖场", "enable_group_red_packet_reply"], ["独立会话", "unique_session"],
    ["私聊白名单", "allowed_users"], ["群聊白名单", "allowed_groups"],
  ];
  const content = document.getElementById("config-content");
  content.replaceChildren();
  for (const [label, key] of fields) {
    const value = config[key];
    let text = "—";
    if (typeof value === "boolean") text = value ? "开启" : "关闭";
    else if (key === "group_reply_mode") text = mode[value] || "—";
    else if (Number.isInteger(value)) text = value === 0 ? "不限" : `${value} ${key === "allowed_users" ? "人" : "个群"}`;
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = text;
    row.append(term, description);
    content.append(row);
  }
}

async function loadPanel(botId, revision) {
  try {
    const result = await bridge.apiGet("connection/panel", {bot_id: botId});
    if (!currentView(botId, revision)) return;
    renderPanel(result, result.reason || result.statistics?.reason || "");
  } catch {
    if (!currentView(botId, revision)) return;
    renderPanel(null, "统计请求失败，请刷新重试");
  }
}

async function loadConnection(botId, revision) {
  try {
    const result = await bridge.apiGet("connection/check", {bot_id: botId});
    if (!currentView(botId, revision)) return;
    render(result.state === "connected" ? "connected" : "failed", result.wechat_online, result.reason || "");
    updateLogin(result, botId, revision);
  } catch {
    if (!currentView(botId, revision)) return;
    clearQr();
    loginPanel.hidden = true;
    const entry = loginEntries.get(botId);
    if (entry) entry.allowed = false;
    render("failed", null, "检测请求失败，请确认 AstrBot 登录有效后重试");
  }
}

async function checkConnection() {
  if (checking) return;
  checking = true;
  button.disabled = select.disabled = true;
  render("checking");
  try {
    if (!botsLoaded) {
      const result = await bridge.apiGet("connection/bots");
      select.replaceChildren();
      for (const bot of result.bots) {
        const option = document.createElement("option");
        option.value = bot.id;
        option.textContent = bot.id;
        select.append(option);
      }
      document.getElementById("robot-picker").hidden = result.bots.length < 2;
      botsLoaded = result.bots.length > 0;
    }
    if (!select.value) {
      render("failed", null, "请先在机器人配置中保存 WeChat Native 机器人");
      renderPanel(null, "保存并启动机器人后，这里将显示每日消息统计。");
      return;
    }
    const botId = select.value;
    const revision = viewRevision;
    await Promise.all([loadPanel(botId, revision), loadConnection(botId, revision)]);
    if (!currentView(botId, revision)) return;
    const now = new Date();
    const updated = document.getElementById("updated-at");
    updated.dateTime = now.toISOString();
    updated.textContent = `更新于 ${now.toLocaleTimeString("zh-CN", {hour12: false})}`;
  } catch {
    render("failed", null, "检测请求失败，请确认 AstrBot 登录有效后重试");
    renderPanel(null, "统计请求失败，请刷新重试");
  } finally {
    checking = false;
    button.disabled = select.disabled = false;
  }
}

button.addEventListener("click", checkConnection);
select.addEventListener("change", () => {
  viewRevision++;
  clearQr();
  loginPanel.hidden = true;
  renderPanel(null, "统计加载中…");
  checkConnection();
});
loginRetry.addEventListener("click", () => { void beginLogin(select.value, viewRevision, true); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearQr();
  } else {
    const entry = loginEntries.get(select.value);
    if (entry?.allowed && !entry.started) void beginLogin(select.value, viewRevision);
    else void pollLogin();
  }
});
new ResizeObserver(renderChart).observe(chart);
try {
  await bridge.ready();
  setInterval(() => { if (!document.hidden) checkConnection(); }, 30000);
  setInterval(pollLogin, 2000);
  await checkConnection();
} catch {
  render("failed", null, "检测请求失败，请确认 AstrBot 登录有效后重试");
  renderPanel(null, "统计请求失败，请刷新重试");
  button.disabled = false;
}
