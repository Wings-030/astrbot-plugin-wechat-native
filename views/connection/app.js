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

async function loadPanel(botId) {
  try {
    const result = await bridge.apiGet("connection/panel", {bot_id: botId});
    renderPanel(result, result.reason || result.statistics?.reason || "");
  } catch {
    renderPanel(null, "统计请求失败，请刷新重试");
  }
}

async function loadConnection(botId) {
  try {
    const result = await bridge.apiGet("connection/check", {bot_id: botId});
    render(result.state === "connected" ? "connected" : "failed", result.wechat_online, result.reason || "");
  } catch {
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
    await Promise.all([loadPanel(select.value), loadConnection(select.value)]);
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
  renderPanel(null, "统计加载中…");
  checkConnection();
});
new ResizeObserver(renderChart).observe(chart);
try {
  await bridge.ready();
  setInterval(() => { if (!document.hidden) checkConnection(); }, 30000);
  await checkConnection();
} catch {
  render("failed", null, "检测请求失败，请确认 AstrBot 登录有效后重试");
  renderPanel(null, "统计请求失败，请刷新重试");
  button.disabled = false;
}
