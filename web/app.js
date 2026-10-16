/* ============================================================
   AI 工作群 · 多模型协作 —— 前端交互
   * WebSocket 实时接收群聊事件流（成员状态 / 逐字上屏 / 记录员汇总）
   * @ 唤起成员选择器；消息里的 @成员 高亮
   * 左侧工作台：项目 / 今日组长 / 模型成员 / 连通性 / 用量估算
   注意：后端接口与事件协议未改动，本文件只负责渲染与交互。
   ============================================================ */
'use strict';

/* 元素获取。
   ---------------------------------------------------------------
   界面上「多余的部分」被删掉后，本文件里仍可能引用那些 id（比如成本面板、
   连通性卡片）。直接返回 null 会让 `.addEventListener` / `.textContent=` 当场抛错，
   所以这里找不到时返回一个**游离的占位节点**：所有读写都变成无害的 no-op。
   这样 HTML 可以放心精简，不必同步改动上千行渲染代码。 */
const _stubEl = (() => {
  const node = document.createElement('div');
  node.value = '';
  node.textContent = '';
  return node;
})();
const $ = (id) => document.getElementById(id) || _stubEl;
const streamEl = $('stream');
const inputEl = $('input');
const sendBtn = $('send-btn');
const membersEl = $('members');
const healthEl = $('health');
const wsDot = $('ws-dot');
const wsText = $('ws-text');
const tagRun = $('tag-run');
const tagAdapter = $('tag-adapter');
const emptyHint = $('empty-hint');
const mentionPop = $('mention-pop');

const state = {
  members: [],
  // 当前会话："group" 群聊，或 "dm:<成员id>" 和该成员的 1:1 私聊。
  // 照 Grok Bot：每个 agent 有自己跟用户的私聊，历史是统一的、只是视图分开。
  channel: 'group',
  msgStore: [],            // 所有消息（两个会话混在一起），渲染时按 channel 过滤

  bubbles: new Map(),      // message id -> DOM 节点
  running: false,
  ws: null,
  retry: 0,
  mentionIndex: 0,
  trimmedInRun: 0,         // 本轮上下文裁剪次数（用于按轮聚合提示）
  trimmedLast: null,       // 最近一次 context_trimmed 事件
  health: null,            // /api/health 原始响应
  providers: [],           // 可用性列表
  calls: [],               // /api/diagnostics 的 call_log
  mockOnly: false,
  projects: [],            // 仅前端记忆（后端未实现项目管理）
  currentProject: '',
  captainId: '',
  budgetToday: 0,
  budgetSession: 0,
  spendDate: '',
  // 服务端今日用量（原始计数，权威来源）。null = 还没拿到。
  usageToday: null,
  userAvatar: '',          // 用户自定义头像（dataURL，来自 localStorage 的 aig-user-avatar）
  avatars: {},             // 成员自定义头像缓存：id -> dataURL（localStorage 的 aig-avatar-<id>）
  avatarTarget: '',        // 文件选择器当前服务的对象：'' = 用户自己，否则是成员 id
  reasoning: {},           // agentId -> { chars, box, done }：实时思考进度
  activeMsg: {},           // agentId -> 正在流式输出的消息节点（思考区挂到这里）
  reasoningCards: {},      // agentId -> 思考先到时创建的独立卡片（message_start 到达后并回气泡）
};

/* ------------------------------ 通用工具 ------------------------------ */
function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** 把 @成员 渲染成高亮 span；正则与后端 config.extract_mentions 保持一致（含 · 间隔号） */
/* id → 显示名。@ 用的是 id（@captain），但人看的是显示名（DeepSeek），
   所以渲染时换过来。**存下来的原文不动** —— 调度和去重都依赖 id，
   只是显示这一层做替换。悬停时用 title 把 id 露出来，方便排查。 */
function mentionDisplayMap() {
  const map = new Map();
  (state.members || []).forEach((m) => {
    if (m && m.id) map.set(String(m.id).toLowerCase(), m.name || m.id);
  });
  return map;
}

function renderRich(text) {
  const map = mentionDisplayMap();
  return esc(text).replace(
    /@([A-Za-z0-9_\-.·\u4e00-\u9fff\u30fb\u2022]+)/g,
    (whole, id) => {
      const name = map.get(String(id).toLowerCase());
      const label = name || id;
      return `<span class="mention" title="${esc(id)}">@${esc(label)}</span>`;
    },
  );
}

function fmtTime(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}

function el(tag, cls, html) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function scrollToBottom(force = false) {
  const near = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 220;
  if (force || near) streamEl.scrollTop = streamEl.scrollHeight;
}

function roleBadge(role) {
  const map = {
    captain: ['captain', '组长'],
    member: ['member', '组员'],
    recorder: ['recorder', '汇总'],
    user: ['user', '用户'],
  };
  const [cls, label] = map[role] || ['member', role || '成员'];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

/** 头像用姓名缩写（截图里的 CX / DS / KM 风格）。
 *  先剥掉「组长·」「组员·」「记录员·」这类角色前缀，否则会取成「组D」这种怪缩写。 */
function initials(name) {
  const raw = String(name || '?').trim();
  const s = raw.replace(/^(组长|组员|记录员|演示|模型)\s*[·.、\-\s]\s*/u, '').trim() || raw;
  const ascii = s.match(/[A-Za-z0-9]+/g);
  if (ascii && ascii.length && !/[\u4e00-\u9fff]/.test(s)) return ascii.join('').slice(0, 2).toUpperCase();
  const tokens = s.split(/[\s·\-—_]+/).filter(Boolean);
  if (tokens.length >= 2) {
    const a = tokens[0][0] || '', b = tokens[1][0] || '';
    return (a + b).toUpperCase();
  }
  if (/[\u4e00-\u9fff]/.test(s)) return s.replace(/[^\u4e00-\u9fff]/g, '').slice(0, 2) || s.slice(0, 2);
  return s.slice(0, 2).toUpperCase();
}

/* ------------------------------ 头像 ------------------------------
   后端 members[].avatar 有两种形态：
     1. 相对图片路径（avatars/kimi.ico 这种）→ 渲染 <img src="/static/avatars/kimi.ico">
     2. 文字 / emoji（🧭 🌙 …）→ 按文字渲染；为空时退回姓名缩写
   图片加载失败（文件缺失、路径打错）会退回文字，不出现破图。
   用户本人的头像可以自定义（dataURL），见下方「用户头像」一节。 */
const AVATAR_KEY = 'aig-user-avatar';
const MEMBER_AVATAR_PREFIX = 'aig-avatar-';   // 成员自定义头像：aig-avatar-<成员id>
// dataURL 长度上限。头像现在是**文件**，不再受 localStorage 5MB 配额限制，
// 所以这个值放宽到跟后端一致（members.AVATAR_MAX_CHARS）——
// 纯粹是防手滑选了个几十 MB 的原图，不是存储限制。
const AVATAR_MAX_CHARS = 4000000;

/** 成员自定义头像：优先用户设置（dataURL），其次配置里的 avatar 路径，最后文字/emoji */
function memberAvatarKey(id) {
  return MEMBER_AVATAR_PREFIX + id;
}

function memberAvatarOverride(id) {
  if (!id) return '';
  // 头像现在来自后端（state.avatars 由 /api/avatars 填充），不再读 localStorage，
  // 因此也**不再受 localStorage 5MB 配额限制**，换版本也不会丢。
  return state.avatars[id] || '';
}

/** 长按 400ms 触发（触屏上的「右键」），松开/移出即取消 */
function bindLongPress(node, fn, ms = 400) {
  if (!node) return;
  let timer = null;
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  node.addEventListener('touchstart', () => { cancel(); timer = setTimeout(fn, ms); }, { passive: true });
  node.addEventListener('touchend', cancel);
  node.addEventListener('touchcancel', cancel);
  node.addEventListener('touchmove', cancel);
}

function isAvatarPath(a) {
  return typeof a === 'string' && /^avatars\/[\w\-.]+\.(png|ico|svg|jpe?g|gif|webp)$/i.test(a.trim());
}

function avatarSrc(a) {
  return '/static/' + String(a).trim().replace(/^\/+/, '');
}

/** 头像图片的候选地址（按顺序尝试）：
 *  后端配置写的是 avatars/kimi.png，但随包发布的可能是同名的 kimi.ico
 *  （实测：web/avatars 下只有 kimi.ico / claude.ico / deepseek.ico / zhipu.ico）。
 *  所以非 .ico 的路径会再补一个「同基名 .ico」的候选，命中不了才退回文字。
 *  每次降级都会在控制台留痕，不静默。 */
function avatarCandidates(src) {
  if (/^data:/i.test(src)) return [src];
  const list = [src];
  const rel = src.replace(/^\/static\//, '');
  if (/\.(png|svg|jpe?g|webp)$/i.test(rel)) list.push('/static/' + rel.replace(/\.[^.]+$/, '.ico'));
  return list;
}

/** 把头像内容填进一个圆形框：overrideSrc（用户自定义 dataURL）优先，
 *  其次是 avatars/ 路径，最后是文字/emoji，再退回 fallbackText。 */
function fillAvatar(box, avatarField, fallbackText, overrideSrc) {
  if (!box) return;
  box.innerHTML = '';
  const resolved = overrideSrc || (isAvatarPath(avatarField) ? avatarSrc(avatarField) : '');
  if (resolved) {
    const chain = avatarCandidates(resolved);
    const img = document.createElement('img');
    img.className = 'avatar-img';
    img.alt = '';
    let idx = 0;
    img.src = chain[0];
    img.addEventListener('error', () => {
      idx += 1;
      if (idx < chain.length) {
        console.warn('[avatar] 图片不存在，尝试备用路径：', chain[idx], '（原路径：' + chain[0] + '）');
        img.src = chain[idx];
        return;
      }
      img.remove();
      box.textContent = String(fallbackText || '');
    });
    box.appendChild(img);
    return;
  }
  const text = (typeof avatarField === 'string' && avatarField.trim() && !isAvatarPath(avatarField))
    ? avatarField.trim()
    : String(fallbackText || '');
  box.textContent = text;
}

/* ------------------------------ 侧栏：项目（仅前端记忆） ------------------------------ */
function loadProjects() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('aigroup.projects') || 'null'); } catch (e) { saved = null; }
  if (!saved || !Array.isArray(saved.list) || !saved.list.length) {
    state.projects = ['默认项目'];
    state.currentProject = '默认项目';
  } else {
    state.projects = saved.list;
    state.currentProject = saved.current || saved.list[0];
  }
  renderProjects();
}

function saveProjects() {
  try {
    localStorage.setItem('aigroup.projects', JSON.stringify({ list: state.projects, current: state.currentProject }));
  } catch (e) { /* 忽略存储失败 */ }
}

function renderProjects() {
  const sel = $('project-select');
  if (!sel) return;
  sel.innerHTML = '';
  state.projects.forEach((p) => {
    const o = document.createElement('option');
    o.value = p; o.textContent = p;
    if (p === state.currentProject) o.selected = true;
    sel.appendChild(o);
  });
  const title = $('chat-title');
  if (title) title.textContent = state.currentProject + ' · 多模型协作群聊';
}

/* ------------------------------ 侧栏：用量估算 ------------------------------ */
/* 说明：后端 call_log 只提供「字符数」，不提供 token 与账单金额。
   这里按字符数估算 token 再套用目录价，因此**是估算，不是账单口径**。
   价格单位：元 / 百万 token（按 1 USD ≈ 7.2 CNY 折算目录价）。 */
const PRICE_CNY_PER_MTOK = {
  'deepseek': { in: 2.2, out: 8.6 },
  'moonshotai-cn': { in: 6.8, out: 28.8 },
  'moonshotai': { in: 6.8, out: 28.8 },
  'doubao': { in: 0.8, out: 2.0 },
  'openai': { in: 10.0, out: 40.0 },
  'mock': { in: 0, out: 0 },
};
const CHARS_PER_TOKEN = 2.5;   // 中英混排的经验值
const BUDGET_CAP = 10.0;

/** 按 provider 估算花费。数据源二选一：
 *
 *  1. **服务端今日累计**（state.usageToday）—— 权威来源。它按天持久化全部调用，
 *     大退不丢，也不受 call_log 200 条上限影响；
 *  2. 退化到 state.calls —— 服务端数据还没拿到时用（会少算，只是过渡）。
 *
 * 两者**不叠加**：服务端那份已经包含了本会话的调用，叠加就重复计费了。
 */
function estimateCostByProvider() {
  const usd = {};   // provider -> 元
  const add = (prov, chars, reasoning) => {
    if (!prov) return;
    const price = PRICE_CNY_PER_MTOK[prov] || { in: 0, out: 0 };
    const outTok = (Number(chars) || 0) + (Number(reasoning) || 0);
    const outToken = (outTok / CHARS_PER_TOKEN);
    const inToken = outToken * 3;      // 组员每轮都要重发上下文，粗略按 3 倍输入估
    usd[prov] = (usd[prov] || 0) + (inToken / 1e6) * price.in + (outToken / 1e6) * price.out;
  };

  const today = state.usageToday;
  if (today && today.providers) {
    Object.entries(today.providers).forEach(([prov, v]) => add(prov, v.chars, v.reasoning_chars));
    return usd;
  }
  state.calls.forEach((c) => {
    if (c.ok === false) return;        // 失败调用不计费（实际计费口径另说）
    add(String(c.provider || '').trim(), c.chars, c.reasoning_chars);
  });
  return usd;
}

/** 取服务端今日用量。启动时拉一次，之后每轮结束再拉。 */
async function loadUsage() {
  try {
    const res = await fetch('/api/usage');
    const data = await res.json();
    if (data && data.ok) {
      state.usageToday = data;
      refreshBudget();
    }
  } catch (e) { /* 拿不到就退回按 call_log 估，不影响使用 */ }
}

function refreshBudget() {
  const perProv = estimateCostByProvider();
  const total = Object.values(perProv).reduce((a, b) => a + b, 0);

  // 日期以服务端为准（避免本机时区/系统时间不一致导致跨天判断错位）
  const today = (state.usageToday && state.usageToday.date)
    || new Date().toISOString().slice(0, 10);
  if (state.spendDate && state.spendDate !== today) {
    state.budgetToday = 0;      // 跨天了：服务端也会自动开新的一天
  }
  state.spendDate = today;
  // 今日 = 服务端持久化的全天累计（大退不丢、不受 call_log 上限影响）
  state.budgetSession = total;
  state.budgetToday = total;

  const fmt = (n) => '¥' + n.toFixed(2);
  if ($('budget-session')) $('budget-session').textContent = fmt(state.budgetSession);
  if ($('budget-today')) $('budget-today').textContent = fmt(state.budgetToday);
  if ($('budget-cap')) $('budget-cap').textContent = fmt(BUDGET_CAP);

  const note = $('budget-note');
  if (note) {
    const parts = Object.entries(perProv)
      .filter(([, v]) => v > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `${k} ${fmt(v)}`);
    const base = `按调用字符数估算，非账单口径（服务端只记录字符数）· 上限 ${fmt(BUDGET_CAP)}`;
    note.textContent = parts.length ? base + ' · ' + parts.join(' / ') : base;
  }
}

/* ------------------------------ 侧栏：成员 ------------------------------ */
const ROLE_LABEL = { captain: '组长', member: '组员', recorder: '汇总' };
const STATE_LABEL = { idle: '待命', thinking: '思考中', done: '已发言', error: '出错' };

/* 左栏「名字下面那行小字」= 实时工作状态。
 * 文案优先级：成员对象上的 status（后端新增的中文字段，如「正在撰写回复…」）
 *           → 本地按 state 兜底映射（后端目前只推 state，等价文案在下面）。
 * 后端补上 status 之后前端不用改，直接用更细的文案。 */
const STATE_TEXT = {
  idle: '待命',
  thinking: '正在思考…',
  writing: '正在撰写回复…',
  done: '已完成发言',
  error: '调用失败',
};

function memberStatusText(m) {
  const s = (m && typeof m.status === 'string') ? m.status.trim() : '';
  if (s) return s;
  return STATE_TEXT[(m && m.state) || ''] || STATE_TEXT.idle;
}

/** 状态行的视觉分级：思考/撰写 → 主题色 + 脉冲；失败 → 红；已完成 → 正常色；其余 → 次级灰 */
function statusTone(m, text) {
  const st = (m && m.state) || '';
  const t = String(text || '');
  if (st === 'error' || /失败|错误|error/i.test(t)) return 'error';
  if (st === 'thinking' || st === 'writing' || /思考|撰写|生成|进行中/.test(t)) return 'active';
  if (st === 'done' || /完成|已发言/.test(t)) return 'done';
  return 'idle';
}

/** 组长排最前（其余保持后端顺序），让「谁是组长」一眼可见 */
function sortMembers(list) {
  const rank = (m) => (m && m.role === 'captain' ? 0 : (m && m.role === 'recorder' ? 2 : 1));
  return [...list].sort((a, b) => rank(a) - rank(b));
}

function memberStateLabel(s) {
  return STATE_LABEL[s] || '待命';
}

/** 左栏职责行：只显示后端给的 duty（自定义职责描述）。
 *  duty 为空 → 整行不渲染；模型路由（route）不再出现在左栏。 */
function memberDuty(m) {
  return (m && typeof m.duty === 'string') ? m.duty.trim() : '';
}

/** 切换会话（群聊 ↔ 某个成员的私聊）。照 Grok Bot 的做法。 */
function switchChannel(ch) {
  if (ch === state.channel) return;
  state.channel = ch;
  // 清空消息区，重放当前会话的消息
  if (typeof streamEl !== 'undefined' && streamEl) streamEl.innerHTML = '';
  state.bubbles.clear();
  state.msgStore.filter((m) => (m.channel || 'group') === ch).forEach((m) => addMessage(m));
  updateChannelBar();
  document.querySelectorAll('.ws-item.dm-active').forEach((n) => n.classList.remove('dm-active'));
  const cur = document.querySelector(`.ws-item[data-id="${CSS.escape(ch.replace(/^dm:/, ''))}"]`);
  if (cur && ch !== 'group') cur.classList.add('dm-active');
  if (typeof scrollToBottom === 'function') scrollToBottom(true);
}

/** 顶部那条「正在和 X 私聊 · 回到群聊」 */
function updateChannelBar() {
  let bar = document.getElementById('channel-bar');
  const isDm = state.channel !== 'group';
  if (!isDm) { if (bar) bar.remove(); return; }
  const id = state.channel.slice(3);
  const mem = (state.members || []).find((m) => m.id === id);
  const name = (mem && mem.name) || id;
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'channel-bar';
    const host = document.querySelector('.chat-header') || document.body;
    host.parentNode.insertBefore(bar, host.nextSibling);
  }
  bar.innerHTML = `<span class="cb-text">正在和 <b>${name}</b> 私聊 · 只有你们两个人看得到</span>`
    + `<button class="cb-back" type="button">回到群聊</button>`;
  bar.querySelector('.cb-back').onclick = () => switchChannel('group');
}

/** 侧栏那一行放什么。
 *
 * 原来是"待命 / 正在思考" —— 信息量几乎为零：六个人全是"待命"，
 * 看不出谁刚说过什么。照 Grok Bot 改成**这个人的最后一条发言**。
 *
 * 优先级：正在思考 > 正在写 > 最后一条发言 > 状态文字（没说过话时）
 */
function memberSidebarLine(m) {
  const rSt = state.reasoning[m.id];
  if (rSt && !rSt.done && rSt.chars) return `已想 ${rSt.chars} 字…`;
  const st = m.state || 'idle';
  if (st === 'thinking') return '正在思考…';
  if (st === 'writing') return '正在写…';
  const mine = (state.msgStore || []).filter((x) => x.sender === m.id && x.content);
  const last = mine[mine.length - 1];
  if (last) {
    const t = String(last.content).replace(/\s+/g, ' ').trim();
    return t.length > 44 ? t.slice(0, 44) + '…' : t;
  }
  return memberStatusText(m);
}

function renderMembers() {
  const all = Array.isArray(state.members) ? state.members : [];
  // hidden=true 的成员不进左栏名单（是否隐藏由后端决定）；组长排最前
  const list = sortMembers(all.filter((m) => !m.hidden));
  membersEl.innerHTML = '';
  const countEl = $('member-count');
  if (countEl) countEl.textContent = list.length;

  list.forEach((m) => {
    const role = m.role || 'member';
    const st = m.state || 'idle';
    const statusText = memberStatusText(m);
    const tone = statusTone(m, statusText);
    // 思考型模型思考期间：状态行补上实时字数，让人一眼看出「在动，不是卡死」
    const rSt = state.reasoning[m.id];
    const rChars = (rSt && !rSt.done && rSt.chars) ? rSt.chars : 0;
    const statusLine = memberSidebarLine(m);
    const custom = !!memberAvatarOverride(m.id);
    // 姓名只用配置里的原名（不拼角色后缀）；身份用徽章表达，不塞进名字里
    const node = el('div', `ws-item role-${role} ${st === 'thinking' ? 'current' : ''}`);
    node.dataset.id = m.id;
    node.innerHTML = `
      <div class="ws-main">
        <div class="ws-name">
          ${esc(m.name || m.id)}
          <span class="ws-role ${esc(role)}">${esc(ROLE_LABEL[role] || role)}</span>
        </div>
        <div class="ws-status ${rChars ? 'active' : tone}">${esc(statusLine)}</div>
      </div>
      <span class="ws-state ${st}" title="${esc(statusLine)}"></span>`;

    const avatarBox = el('div', `ws-avatar avatar-editable${custom ? ' custom' : ''}`);
    fillAvatar(avatarBox, m.avatar, initials(m.name || m.id), memberAvatarOverride(m.id));
    avatarBox.title = custom ? '点击更换头像 · 右键/长按恢复默认' : '点击自定义这个成员的头像';
    avatarBox.addEventListener('click', (e) => { e.stopPropagation(); pickAvatar(m.id); });
    avatarBox.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      if (memberAvatarOverride(m.id)) resetAvatarFor(m.id);
    });
    bindLongPress(avatarBox, () => { if (memberAvatarOverride(m.id)) resetAvatarFor(m.id); });
    node.insertBefore(avatarBox, node.firstChild);
    // 点成员 = 和它私聊（照 Grok Bot 的 1:1 DM）
    node.classList.add('dm-open');
    node.title = `和 ${m.name || m.id} 私聊`;
    node.addEventListener('click', (ev) => {
      if (ev.target.closest('button')) return;   // 别抢成员操作按钮
      switchChannel('dm:' + m.id);
    });
    membersEl.appendChild(node);
  });

  // 顶栏显示当前组长（配置里 role=captain 的成员；隐藏的组长也照常显示在这个 chip 上）
  const cap = all.find((m) => m.role === 'captain');
  const chip = $('captain-chip');
  if (chip) chip.textContent = '组长：' + (cap ? (cap.name || cap.id) : '未指定');

  renderHeadCount();
  renderRosterDetail();
}

/* 「成员」标签页：把侧栏信息展开成明细卡片（DSH 里对应「查看团队」） */
function renderRosterDetail() {
  const box = $('roster-detail');
  if (!box) return;
  const list = sortMembers(Array.isArray(state.members) ? state.members : []);
  const roleName = { captain: '组长', member: '组员', recorder: '记录员' };
  box.innerHTML = '';
  list.forEach((m) => {
    const card = el('div', 'detail-card');
    const rows = [
      ['角色', roleName[m.role] || m.role],
      ['状态', memberStatusText(m)],
      // 左栏那行改成实时状态了，职责挪到这里显示
      ['职责', memberDuty(m) || (ROLE_LABEL[m.role] || '—')],
      ['隐藏', m.hidden ? '是（不出现在左栏名单）' : '否'],
      ['模型路由', m.route || '—'],
      ['备用路由', m.fallback_route || '—'],
      ['派发权', m.can_dispatch ? '有（可 @ 他人）' : '无（只发言）'],
      ['单轮上限', (m.max_turns_per_run != null ? m.max_turns_per_run + ' 次' : '—')],
    ];
    card.innerHTML =
      `<div class="detail-head"><h3>${esc(m.name || m.id)}</h3>` +
      (m.role === 'captain' ? '<span class="badge captain">组长</span>' : '') +
      '</div>' +
      rows.map(([k, v]) => `<div class="detail-row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('');

    // 组长 / 组员 赋权入口（需求 4）：调用 POST /api/member-role 后刷新成员列表
    const toCaptain = m.role === 'captain';
    const btn = el('button', 'role-btn', toCaptain ? '设为组员' : '设为组长');
    btn.dataset.id = m.id;
    btn.dataset.role = toCaptain ? 'member' : 'captain';
    btn.title = toCaptain ? '把该成员降为组员' : '把该成员设为组长';
    btn.addEventListener('click', () => setMemberRole(m.id, btn.dataset.role));
    card.appendChild(btn);
    box.appendChild(card);
  });
}

/* ------------------------- 组长/组员 赋权（需求 4） -------------------------
 * 后端接口：POST /api/member-role  body {"id": "...", "role": "captain"|"member"}
 * 成功 → 用返回的快照刷新成员列表；失败 → 在「成员」页与对话流里都给出可见提示
 * （接口不存在时会是 404，同样必须看得见，不能静默）。 */
function rosterMessage(text, level) {
  const box = $('roster-msg');
  if (box) {
    box.textContent = text;
    box.className = 'roster-msg ' + (level || 'info');
    box.classList.remove('hidden');
  }
  addNotice(text, level === 'error' ? 'error' : 'cleared');
}

async function refreshSnapshot() {
  try {
    const res = await fetch('/api/snapshot');
    if (!res.ok) return null;
    const data = await res.json();
    applySnapshot(data);
    return data;
  } catch (e) {
    return null;
  }
}

async function setMemberRole(id, role) {
  if (!id || !role) return false;
  try {
    const res = await fetch('/api/member-role', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, role }),
    });
    if (!res.ok) {
      // 后端的 400/404/409/500 都带 {ok:false, error:"中文原因"}，把它原样展示出来
      let detail = '';
      try {
        const j = await res.json();
        detail = (j && (j.error || j.detail)) || '';
      } catch (e) { detail = ''; }
      rosterMessage(
        `设置失败：POST /api/member-role 返回 HTTP ${res.status}` +
        (detail ? ` —— ${detail}` : '') + `；成员 ${id} 的角色未改变。`,
        'error',
      );
      return false;
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (data && Array.isArray(data.members)) {
      applySnapshot(data);
    } else if (data && data.snapshot && Array.isArray(data.snapshot.members)) {
      applySnapshot(data.snapshot);
    } else {
      await refreshSnapshot();
    }
    const who = (state.members.find((m) => m.id === id) || {}).name || id;
    rosterMessage(`已把 ${who} 设为${role === 'captain' ? '组长' : '组员'}。`, 'ok');
    return true;
  } catch (e) {
    rosterMessage(`设置失败：网络或服务异常（${e && e.message ? e.message : e}）；成员 ${id} 的角色未改变。`, 'error');
    return false;
  }
}

/* 「上下文」标签页：额度护栏 + 用量 + 最近调用（数据来自 /api/diagnostics） */
function renderContextDetail() {
  const box = $('context-detail');
  if (!box) return;
  const L = state.limits || {};
  const perProv = estimateCostByProvider();
  const calls = (state.calls || []).slice(-12).reverse();
  const card1 = el('div', 'detail-card');
  card1.innerHTML = `<h3>额度护栏（本轮）</h3>` + [
    ['单轮发言上限', L.max_messages != null ? L.max_messages + ' 次（记录员不计入）' : '—'],
    ['@ 触发深度', L.max_depth != null ? L.max_depth + ' 层' : '—'],
    ['上下文字符上限', L.max_context_chars != null ? L.max_context_chars + ' 字' : '—'],
    ['模型调用总上限', L.max_model_calls_per_run != null ? L.max_model_calls_per_run + ' 次' : '—'],
  ].map(([k, v]) => `<div class="detail-row"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`).join('');

  const card2 = el('div', 'detail-card');
  const cost = Object.entries(perProv).filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ¥${v.toFixed(4)}`).join('　') || '本会话暂无成功调用';
  card2.innerHTML = `<h3>用量估算</h3>` +
    `<div class="detail-row"><span class="k">按 provider</span><span class="v">${esc(cost)}</span></div>` +
    `<div class="detail-row"><span class="k">合计</span><span class="v">¥${Object.values(perProv).reduce((a, b) => a + b, 0).toFixed(4)}（估算，非账单口径）</span></div>` +
    `<div class="detail-row"><span class="k">本会话调用</span><span class="v">${(state.calls || []).length} 次</span></div>`;

  const card3 = el('div', 'detail-card');
  card3.innerHTML = `<h3>最近调用（真实路由与成败）</h3>` + (calls.length
    ? calls.map((c) => {
        const badge = c.ok === false ? '✗' : '✓';
        return `<div class="detail-row"><span class="k">${esc(badge + ' ' + (c.provider || ''))}</span>` +
          `<span class="v">${esc((c.model || '') + ' · ' + (c.chars || 0) + ' 字 · ' + (c.elapsed_s || 0) + 's')}` +
          `${c.error ? ' · ' + esc(String(c.error).slice(0, 60)) : ''}</span></div>`;
      }).join('')
    : '<div class="detail-row"><span class="v">本会话暂无调用记录</span></div>');

  box.innerHTML = '';
  box.appendChild(card1);
  box.appendChild(card2);
  box.appendChild(card3);
}

/** 就地更新某个成员的状态。
 *  注意：第二个形参曾命名为 state，**遮蔽了全局 state 对象**，导致
 *  `state.members.findIndex` 变成对字符串取属性 → 每次 agent_status 事件都抛
 *  TypeError 并中断后续渲染（用户表现为「发出去的消息看不见」）。
 *  这里改名为 agentStatus，并复用上一轮已缓存的成员信息避免丢字段。 */
function patchMember(id, agentStatus, name, avatar, statusText) {
  if (!id) return;
  const list = Array.isArray(state.members) ? state.members : [];
  const prev = list.find((m) => m.id === id);
  if (prev) {
    prev.state = agentStatus;
    if (statusText) prev.status = statusText;
    else if (!statusText) delete prev.status;   // 事件没带 status 时用 state 兜底文案
    if (name) prev.name = name;
    if (avatar) prev.avatar = avatar;
  } else {
    list.push({
      id,
      name: name || id,
      avatar: avatar || '📝',
      state: agentStatus,
      status: statusText || '',
      role: 'recorder',
      route: '',
    });
    state.members = list;
  }
  // 新一轮思考开始：清掉上一轮的记录（上一轮的思考区留在旧气泡里，不受影响）
  if (agentStatus === 'thinking') {
    const st = state.reasoning[id];
    if (st && st.done) delete state.reasoning[id];
  }
  // 该成员这一轮结束了：思考区收起、字数定稿
  if (agentStatus === 'done' || agentStatus === 'error' || agentStatus === 'idle') {
    finalizeReasoning(id);
  }
  renderMembers();
}

/* ------------------------------ 侧栏：连通性 ------------------------------ */
function renderHealth() {
  if (!healthEl) return;
  healthEl.innerHTML = '';
  const list = state.providers || [];
  if (!list.length) {
    healthEl.appendChild(el('div', 'health-row', '<span class="dot"></span><span class="name">查询中…</span>'));
    return;
  }
  list.forEach((p) => {
    const row = el('div', 'health-row');
    row.innerHTML = `<span class="dot ${p.ok ? 'ok' : 'bad'}"></span>
      <span class="name">${esc(p.provider)}</span>
      <span class="detail" title="${esc(p.detail || '')}">${esc(p.ok ? '可用' : p.detail || '不可用')}</span>`;
    healthEl.appendChild(row);
  });
  if (state.mockOnly) {
    healthEl.appendChild(el('div', 'health-row',
      '<span class="dot"></span><span class="name">离线模式</span><span class="detail">服务以 SMOKE_MOCK_ONLY 启动</span>'));
  }
}

/* ------------------------------ 聊天流 ------------------------------ */
function removeEmptyHint() {
  if (emptyHint && emptyHint.parentNode) emptyHint.remove();
}

function addNotice(text, level = 'info') {
  removeEmptyHint();
  const node = el('div', `notice ${level}`, esc(text));
  streamEl.appendChild(node);
  scrollToBottom(true);
}

/** 每条消息的 token 与估算费用行（照视频里那款：模型名 · tokens · 估算 ¥x.xx） */
function costFootHtml(msg) {
  if (!msg || msg.role === 'user') return '';
  const m = msg.route ? msg.route.split('/').slice(-1)[0] : '';
  const tok = Number(msg.tokens || 0);
  const parts = [];
  if (m) parts.push(esc(m));
  if (tok > 0) {
    parts.push(`${tok.toLocaleString()} tokens`);
    const cny = estimateMsgCost(msg.route || '', tok);
    if (cny > 0) parts.push(`估算 ¥${cny.toFixed(3)}`);
  } else if (msg.route && msg.route.startsWith('mock/')) {
    parts.push('离线通道 · 0 token');
  }
  const tags = [];
  if (msg.proactive) tags.push('<span class="msg-tag proactive">主动上报</span>');
  const rc = Number(msg.reasoning_chars || 0);
  if (!parts.length && !tags.length) return '';
  return `<div class="msg-foot">${parts.map((x) => `<span>${x}</span>`).join('<span class="foot-sep">·</span>')}${tags.join('')}</div>`;
}

/** 按消息路由估单条费用（元）。价格与左栏估算同一张表。 */
function estimateMsgCost(route, tokens) {
  const prov = String(route || '').split('/')[0];
  const price = PRICE_CNY_PER_MTOK[prov];
  if (!price) return 0;
  // 单条消息只有输出 token 可观；输入按 3 倍输出粗估（与左栏口径一致）
  const outTok = tokens || 0;
  const inTok = outTok * 3;
  return (inTok / 1e6) * price.in + (outTok / 1e6) * price.out;
}

/** 「回复某条消息」引用条（问题三：显示被回复内容摘要，点击定位） */
function replyQuoteHtml(msg) {
  if (!msg || !msg.reply_to) return '';
  const summary = msg.reply_summary || '（原消息已不在当前视图）';
  return `<div class="reply-quote" data-target="${esc(msg.reply_to)}">↩ ${renderRich(summary)}</div>`;
}

/** 「主动上报」徽章行（问题三：重要结论直接呈报） */
function proactiveBadgeHtml(msg) {
  if (!msg || !msg.proactive) return '';
  return '<div class="proactive-line">▲ 主动上报 · 这条发言已直接呈报给你</div>';
}

function msgMetaHtml(senderName, role, route, warning, ts) {
  // 2026-10-14 用户要求：**消息头部不再显示模型名**。
  // 原来头部写 `智谱  组员  zhipu/GLM-5.3-Flash  11:07:09`，而底部那行已经写了
  // 「GLM-5.3-Flash · 1,356 tokens · 估算费用」—— 同一个信息出现两次，头部还很挤。
  // 现在头部只留「谁 · 什么身份 · 什么时候」。
  //
  // 但**降级告警要在头部保留**：那条回复其实不是它自己产的，这件事必须显眼，
  // 不能只藏在底部那行里。
  const model = warning
    ? `<span class="msg-model degraded">${esc(route || '')}</span>`
    : '';
  return `
    <span class="msg-name">${esc(senderName)}</span>
    ${roleBadge(role)}
    ${model}
    <span class="msg-time">${fmtTime(ts)}</span>`;
}

function addMessage(msg) {
  // 进仓库（去重）—— 切会话时靠它重放
  const ch = msg.channel || 'group';
  if (!state.msgStore.some((m) => m.id === msg.id)) state.msgStore.push(msg);
  // 不是当前会话：只入库，不画
  if (ch !== state.channel) { updateChannelBar(); return; }
  removeEmptyHint();
  const node = el('div', `msg ${msg.role || 'member'}`);
  node.dataset.id = msg.id;
  node.dataset.sender = msg.sender || '';
  const short = initials(msg.sender_name || msg.sender);
  node.innerHTML = `
    <div class="msg-avatar"></div>
    <div class="msg-body">
      <div class="msg-meta">${msgMetaHtml(msg.sender_name, msg.role, msg.route, msg.warning, msg.created_at)}</div>
      ${replyQuoteHtml(msg)}
      ${attachmentsHtml(msg)}
      ${proactiveBadgeHtml(msg)}
      <div class="bubble"></div>
      <div class="warn-line hidden"></div>
      ${costFootHtml(msg)}
    </div>`;
  fillMessageAvatar(node, msg);
  const bubble = node.querySelector('.bubble');
  bubble.innerHTML = renderRich(msg.content || '');
  // 历史消息不再还原思考框：推理过程统一去「模型思考」控制台看，聊天框只留答案。
  if (msg.warning) {
    const warn = node.querySelector('.warn-line');
    warn.textContent = '⚠ ' + msg.warning;
    warn.classList.remove('hidden');
  }
  streamEl.appendChild(node);
  state.bubbles.set(msg.id, node);
  scrollToBottom(true);
  return node;
}

function startMessage(ev) {
  removeEmptyHint();
  const node = el('div', `msg ${ev.role || 'member'}`);
  node.dataset.id = ev.id;
  node.dataset.sender = ev.sender || '';
  const short = initials(ev.sender_name || ev.sender);
  node.innerHTML = `
    <div class="msg-avatar"></div>
    <div class="msg-body">
      <div class="msg-meta">${msgMetaHtml(ev.sender_name, ev.role, ev.requested_route, '', undefined)}</div>
      <div class="bubble streaming"></div>
      <div class="warn-line hidden"></div>
    </div>`;
  fillMessageAvatar(node, {
    role: ev.role, sender: ev.sender, sender_name: ev.sender_name, sender_avatar: ev.sender_avatar,
  });
  streamEl.appendChild(node);
  state.bubbles.set(ev.id, node);
  if (ev.sender) {
    state.activeMsg[ev.sender] = node;
    // 思考先到的情况：把已经建好的思考区并进这条气泡，并删掉临时卡片
    const st = state.reasoning[ev.sender];
    if (st && st.box && st.box.isConnected) {
      const body = node.querySelector('.msg-body');
      if (body) body.insertBefore(st.box, body.querySelector('.bubble'));
      const card = state.reasoningCards[ev.sender];
      if (card && card.isConnected) card.remove();
      delete state.reasoningCards[ev.sender];
    }
  }
  scrollToBottom(true);
  return node;
}

/** 消息头像：AI 成员用「用户自定义头像 → 配置 avatar → 缩写」，
 *  用户用自定义头像或默认「我」。点击换图、右键/长按恢复默认（与左栏同一套）。 */
function fillMessageAvatar(node, msg) {
  const box = node.querySelector('.msg-avatar');
  if (!box) return;
  const isUser = msg.role === 'user' || msg.role === 'me';
  const senderId = msg.sender || '';
  if (isUser) {
    fillAvatar(box, '我', '我', state.userAvatar || '');
  } else {
    fillAvatar(box, msg.sender_avatar, initials(msg.sender_name || msg.sender),
      memberAvatarOverride(senderId));
  }
  const target = isUser ? '' : senderId;
  box.addEventListener('click', () => pickAvatar(target));
  box.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    resetAvatarFor(target);
  });
  if (!isUser) bindLongPress(box, () => resetAvatarFor(target));
}

/* ============================================================================
   实时思考过程（思考型模型 kimi-k2.6 实测：先写 1345 字思考、23 秒后才吐 49 字正文）
   后端契约：
     {"kind":"agent_reasoning","agent":"<成员id>","text":"<增量>","chars":<累计思考字数>}
     message_end 的 message 新增 reasoning_chars
   本段只负责渲染，收不到该事件时自然什么都不做（旧后端也不会报错）。
   ============================================================================ */

/** 从成员列表里取展示用的元信息（agent_reasoning 只带 id） */
function agentMeta(id) {
  const m = (state.members || []).find((x) => x.id === id) || {};
  return {
    id,
    name: m.name || id,
    role: m.role || 'member',
    avatar: m.avatar || '',
  };
}

function updateReasoningHead(box) {
  if (!box) return;
  const head = box.querySelector('.reasoning-head');
  if (!head) return;
  // 静态摘要（历史消息只有总字数）用 dataset.chars；实时思考用 state.reasoning
  const staticChars = box.dataset.chars;
  const st = state.reasoning[box.dataset.agent] || { chars: 0 };
  const n = (staticChars !== undefined && staticChars !== '') ? Number(staticChars) : (st.chars || 0);
  const collapsed = box.classList.contains('collapsed');
  head.textContent = collapsed ? `💭 思考了 ${n} 字 ▸` : `💭 思考中… 已想 ${n} 字 ▾`;
}

/** 点标题行展开 / 收起（默认：思考中展开，正文到达后自动收起） */
function toggleReasoning(box) {
  if (!box || box.classList.contains('empty')) return;
  box.classList.toggle('collapsed');
  updateReasoningHead(box);
}

/** collapsed=true 且给了 charsStatic 时是「静态摘要」：不可点、不参与实时状态 */
function createReasoningBox(agentId, collapsed, charsStatic) {
  const isStatic = (typeof charsStatic === 'number');
  const box = el('div', `reasoning-box${collapsed ? ' collapsed' : ''}${isStatic ? ' empty' : ''}`);
  box.dataset.agent = agentId;
  if (isStatic) box.dataset.chars = String(charsStatic);
  box.innerHTML = '<div class="reasoning-head"></div><div class="reasoning-text"></div>';
  if (!isStatic) {
    box.querySelector('.reasoning-head').addEventListener('click', () => toggleReasoning(box));
  }
  // 手动向上翻阅时不要被自动滚到底打断
  const textEl = box.querySelector('.reasoning-text');
  textEl.addEventListener('scroll', () => {
    textEl.dataset.userScrolled = (textEl.scrollHeight - textEl.scrollTop - textEl.clientHeight > 24) ? '1' : '';
  });
  updateReasoningHead(box);
  return box;
}

/* 思考已移出气泡，旧的 ensureReasoningBox 已删除（见下方空实现）。 */

/** 左栏状态行的实时字数（节流到每帧最多一次，避免高频事件造成重排） */
let reasoningBadgePending = null;
let reasoningBadgeScheduled = false;
const scheduleFrame = (typeof requestAnimationFrame === 'function')
  ? requestAnimationFrame.bind(null)
  : (fn) => setTimeout(fn, 16);

function applyMemberReasoningBadge(agentId, chars) {
  const items = membersEl ? membersEl.children : [];
  for (let i = 0; i < items.length; i++) {
    if (items[i].dataset.id !== agentId) continue;
    const statusEl = items[i].querySelector('.ws-status');
    if (!statusEl) return;
    const m = (state.members || []).find((x) => x.id === agentId) || {};
    statusEl.textContent = `${memberStatusText(m)} · 已想 ${chars} 字`;
    if (!statusEl.classList.contains('active')) statusEl.className = 'ws-status active';
    return;
  }
}

function scheduleMemberReasoningBadge(agentId, chars) {
  reasoningBadgePending = { agentId, chars };
  if (reasoningBadgeScheduled) return;
  reasoningBadgeScheduled = true;
  scheduleFrame(() => {
    reasoningBadgeScheduled = false;
    const p = reasoningBadgePending;
    if (p) applyMemberReasoningBadge(p.agentId, p.chars);
  });
}

/** 思考流可能每秒几十条：滚动也跟着节流，避免每条都触发强制重排 */
let reasoningScrollAt = 0;
function scheduleReasoningScroll() {
  const now = Date.now();
  if (now - reasoningScrollAt < 120) return;
  reasoningScrollAt = now;
  scrollToBottom();
}

/** 收到一条 agent_reasoning：**只喂给「模型思考」控制台，不再往聊天气泡里塞**。
 *
 *  用户要求聊天框保持干净 —— 里面只留答案，模型的推理过程去
 *  右上「⋯ → 模型思考」那个页面看（那一页是实时控制台，只显示此刻在想的那个）。 */
function handleReasoning(ev) {
  if (!ev || !ev.agent) return;
  const agentId = ev.agent;
  const st = state.reasoning[agentId] || (state.reasoning[agentId] = { chars: 0, done: false });
  const text = String(ev.text || '');
  const reported = Number(ev.chars);
  st.chars = Number.isFinite(reported) && reported >= 0 ? reported : (st.chars || 0) + text.length;
  st.done = false;
  feedThinkConsole(agentId, text, st.chars);
  scheduleMemberReasoningBadge(agentId, st.chars);
}

/** 气泡里不再显示思考，这两个函数保留成空实现：
 *  旧代码里还有调用点，删掉它们要动的地方更多，留着更安全。 */
function ensureReasoningBox() { return null; }
function collapseReasoningForMessage() { /* 思考已不在气泡里，无需收起 */ }

/** 该成员这一轮结束：只标记状态，聊天框里本来就没有思考区要收 */
function finalizeReasoning(agentId, finalChars) {
  const st = state.reasoning[agentId];
  if (!st) return;
  const n = Number(finalChars);
  if (Number.isFinite(n) && n > 0) st.chars = n;
  st.done = true;
  delete state.activeMsg[agentId];
  const card = state.reasoningCards[agentId];
  if (card && card.isConnected) card.remove();
  delete state.reasoningCards[agentId];
  markThinkConsoleDone(agentId, st.chars);
}

function appendDelta(ev) {
  const node = state.bubbles.get(ev.id);
  if (!node) return;
  const bubble = node.querySelector('.bubble');
  // 增量先累积在 dataset 里，再统一渲染，保证 @高亮 不会跨分片被切断
  bubble.dataset.raw = (bubble.dataset.raw || '') + ev.delta;
  bubble.innerHTML = renderRich(bubble.dataset.raw);
  scrollToBottom();
}

function endMessage(ev) {
  const node = state.bubbles.get(ev.id);
  if (!node) return;
  const bubble = node.querySelector('.bubble');
  bubble.classList.remove('streaming');
  // 这一轮结束：思考区收起并把字数定稿（message_end 的 reasoning_chars 是权威值）
  if (ev.message && ev.message.sender) {
    finalizeReasoning(ev.message.sender, ev.message.reasoning_chars);
  }
  if (ev.message) {
    bubble.dataset.raw = ev.message.content;
    bubble.innerHTML = renderRich(ev.message.content);
    const meta = node.querySelector('.msg-meta');
    if (meta) {
      meta.innerHTML = msgMetaHtml(ev.message.sender_name, ev.message.role, ev.message.route,
        ev.message.warning, ev.message.created_at);
    }
    // 引用条 / 主动上报徽章 / token 费用行（message_end 才带完整字段）
    const body = node.querySelector('.msg-body');
    if (body) {
      const oldQuote = body.querySelector('.reply-quote');
      if (oldQuote) oldQuote.remove();
      const oldBadge = body.querySelector('.proactive-line');
      if (oldBadge) oldBadge.remove();
      const oldFoot = body.querySelector('.msg-foot');
      if (oldFoot) oldFoot.remove();
      const bubbleEl = body.querySelector('.bubble');
      const warnEl = body.querySelector('.warn-line');
      // 用 createElement + insertBefore（比 insertAdjacentHTML 兼容性更好，且便于测试桩模拟）
      if (ev.message.reply_to && bubbleEl) {
        const q = el('div');
        q.innerHTML = replyQuoteHtml(ev.message).trim();
        if (q.firstChild) bubbleEl.parentNode.insertBefore(q.firstChild, bubbleEl);
      }
      if (ev.message.proactive && bubbleEl) {
        const b = el('div');
        b.innerHTML = proactiveBadgeHtml(ev.message).trim();
        if (b.firstChild) bubbleEl.parentNode.insertBefore(b.firstChild, bubbleEl);
      }
      if (warnEl) {
        const f = el('div');
        f.innerHTML = costFootHtml(ev.message).trim();
        if (f.firstChild) warnEl.parentNode.insertBefore(f.firstChild, warnEl.nextSibling);
      }
    }
    if (ev.message.warning) {
      const warn = node.querySelector('.warn-line');
      warn.textContent = '⚠ ' + ev.message.warning;
      warn.classList.remove('hidden');
    }
  }
  if (ev.error) {
    bubble.innerHTML = `<span style="color:var(--danger)">调用失败：${esc(ev.error)}</span>`;
  }
  scrollToBottom();
}

/* 本轮路由小结：把 engine 的 route_summary 渲染成显眼的告警条。
   同源（多个角色落在同一 provider）或存在失败成员时用警告色。
   注意：服务端在「本轮还没人发言」时也会给出 route_summary（providers 为空），
   那种空壳不该占一条气泡——否则刷新后会平白多出一条空告警。 */
function addRouteSummary(ev) {
  // 2026-10-13 用户要求：删掉「本轮路由分布」这个块。
  // 保留函数名空转，是为了不动那几个调用点（WS 事件、初始快照恢复）。
  return;
  const providers = (ev && ev.providers) || {};
  const failed = (ev && ev.failed && ev.failed.length) || 0;
  const degraded = (ev && ev.degraded_count) || 0;
  const hasData = Object.keys(providers).length > 0 || failed > 0 || degraded > 0;
  if (!hasData) return;

  const shared = ev.shared_providers && Object.keys(ev.shared_providers).length > 0;
  const warn = !!shared || failed > 0;

  removeEmptyHint();
  const node = el('div', `notice route-summary ${warn ? 'warn' : ''}`);
  node.appendChild(el('div', 'rs-head',
    warn ? '⚠ 本轮路由：跨厂商交叉验证已被打折' : '本轮路由分布'));
  node.appendChild(el('div', 'rs-body', esc(ev.message || '')));

  const chips = el('div', 'rs-chips');
  Object.keys(providers).forEach((p) => {
    const isShared = shared && ev.shared_providers[p];
    chips.appendChild(el('span', `rs-chip ${isShared ? 'shared' : ''}`, `${esc(p)} × ${providers[p]}`));
  });
  if (degraded > 0) chips.appendChild(el('span', 'rs-chip shared', `降级 ${degraded}`));
  if (failed) chips.appendChild(el('span', 'rs-chip failed', `失败未产出 ${failed}`));
  if (chips.children.length) node.appendChild(chips);

  streamEl.appendChild(node);
  scrollToBottom();
}

/* 上下文裁剪的按轮聚合提示：逐条刷屏没有意义，只报本轮次数与最近一次细节。 */
function addTrimNotice() {
  const ev = state.trimmedLast;
  if (!ev) return;
  addNotice(
    `⚠ 上下文超限：本轮共 ${state.trimmedInRun} 次裁剪，最近一次为 ${ev.agent}（丢弃 ${ev.dropped} 条较早发言，` +
    `保留 ${ev.kept_chars}/${ev.budget} 字）${ev.truncated_pinned ? '，且本轮原话已被按预算截断' : ''}。`,
    'warn'
  );
}

/* ------------------------------ 事件分发 ------------------------------ */
function handleEvent(ev) {
  switch (ev.kind) {
    case 'hello':
      applySnapshot(ev.snapshot);
      break;
    case 'room_state':
      // 防御：任何非数组载荷都不能把 state.members 打成 undefined，
      // 否则后续 patchMember/renderMembers 会连锁抛错并静默停更界面。
      state.members = Array.isArray(ev.members) ? ev.members : [];
      renderMembers();
      break;
    case 'agent_reasoning':
      // 思考型模型的实时思考增量：边收边渲染（收不到这个事件的旧后端也不受影响）
      handleReasoning(ev);
      break;
    case 'agent_status':
      // 事件里可能同时带 state 与 status（后端补了 status 就直接用）
      patchMember(ev.agent_id, ev.state, ev.name, ev.avatar, ev.status);
      break;
    case 'message':
      updateChannelBar();
      addMessage(ev.message);
      break;
    case 'message_start':
      startMessage(ev);
      break;
    case 'message_delta':
      appendDelta(ev);
      break;
    case 'message_end':
      endMessage(ev);
      break;
    case 'route_changed':
      // 降级是显式事件：气泡上立刻标注「已降级」，绝不静默换模型
      markDegraded(state.bubbles.get(ev.id), ev.warning, ev.route);
      break;
    case 'route_summary':
      // 本轮实际路由分布：同源/降级/失败必须显著可见，
      // 否则「同一家厂商自辩」会被误读成「多厂商交叉验证」。
      addRouteSummary(ev);
      break;
    case 'context_trimmed':
      // 上下文被裁剪同样不能静默；逐条刷屏无益，按轮聚合后在 run_end 汇总提示。
      state.trimmedInRun += 1;
      state.trimmedLast = ev;
      break;
    case 'notice':
      addNotice(ev.message, ev.level || 'info');
      break;
    case 'error':
      addNotice('⚠ ' + ev.message, 'error');
      break;
    case 'run_start':
      state.running = true;
      state.trimmedInRun = 0;
      state.trimmedLast = null;
      // 新一轮：清空思考进度
      state.reasoning = {};
      state.activeMsg = {};
      state.reasoningCards = {};
      thinkReset();          // 思考控制台只留本轮
      tagRun.textContent = '讨论中…';
      tagRun.classList.add('running');
      updateSend();
      break;
    case 'run_end':
      state.running = false;
      if (state.trimmedInRun > 0) addTrimNotice();
      state.trimmedInRun = 0;
      state.trimmedLast = null;
      tagRun.textContent = '空闲';
      tagRun.classList.remove('running');
      updateSend();
      refreshDiagnostics();
      break;
    case 'cleared':
      streamEl.innerHTML = '';
      state.bubbles.clear();
      state.reasoning = {};
      state.activeMsg = {};
      state.reasoningCards = {};
      thinkReset();
      streamEl.appendChild(emptyHint);
      addNotice('聊天记录已清空', 'cleared');
      break;
    default:
      break;
  }
}

/** 降级标注：把消息气泡上的模型徽章标成降级态并补一行说明 */
function markDegraded(node, warning, route) {
  if (!node) return;
  const model = node.querySelector('.msg-model');
  if (model && route) {
    model.textContent = route;
    model.classList.add('degraded');
  }
  if (warning) {
    const warn = node.querySelector('.warn-line');
    if (warn) {
      warn.textContent = '⚠ ' + warning;
      warn.classList.remove('hidden');
    }
  }
}

function applySnapshot(snapshot) {
  if (!snapshot) return;
  state.members = Array.isArray(snapshot.members) ? snapshot.members : [];
  renderMembers();
  if (snapshot.limits) {
    const L = snapshot.limits;
    state.limits = L;
    // 2026-10-14：标题栏要的是「群名 + 人数」，所以限额**不再挤占 #chat-sub**，
    // 改成挂在标题行的 title 上（鼠标悬停能看到），人数由 renderHeadCount 负责。
    const head = document.querySelector('.main-head');
    if (head) {
      head.title = `当前护栏：单轮 ≤ ${L.max_messages} 次发言 · @ 深度 ≤ ${L.max_depth}`
        + ` · 上下文 ≤ ${L.max_context_chars} 字`;
    }
    renderHeadCount();
  }
  if (snapshot.adapters) {
    tagAdapter.textContent = snapshot.adapters.ag2 ? 'AG2 内核可用' : '自研内核';
    tagAdapter.title = snapshot.adapters.detail || '';
  }
  if (snapshot.messages && snapshot.messages.length) {
    removeEmptyHint();
    streamEl.innerHTML = '';
    state.bubbles.clear();
    snapshot.messages.forEach((m) => addMessage(m));
  }
  // 刷新后必须能重新看到本轮路由告警，否则「同源自辩」只在刷新前可见，等于没有
  if (snapshot.route_summary) {
    const rs = snapshot.route_summary;
    addRouteSummary({
      message: rs.text,
      providers: rs.providers,
      shared_providers: rs.shared_providers,
      degraded_count: rs.degraded_count,
      failed: rs.failed,
    });
  }
  state.running = !!snapshot.running;
  tagRun.textContent = state.running ? '讨论中…' : '空闲';
  tagRun.classList.toggle('running', state.running);
  updateSend();
}

/* ------------------------------ WebSocket ------------------------------ */
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/group`);
  state.ws = ws;

  ws.onopen = () => {
    state.retry = 0;
    wsDot.className = 'brand-dot ok';
    wsText.textContent = '已连接';
  };
  ws.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
  };
  ws.onclose = () => {
    wsDot.className = 'brand-dot bad';
    state.retry += 1;
    wsText.textContent = `断开，${Math.min(state.retry * 2, 10)}s 后重连…`;
    setTimeout(connect, Math.min(state.retry * 2000, 10000));
  };
  ws.onerror = () => { wsDot.className = 'brand-dot bad'; wsText.textContent = '连接异常'; };
}

/* ------------------------------ 发送 ------------------------------ */
/** 按钮在两种角色间切换：平时是「发送」，本轮跑起来时是「停止」。
 *
 * 之前一轮跑起来只能杀进程 —— 一轮里开着 Kimi 那种 20 秒的调用时，
 * 除了等没有别的办法。
 */
function updateSend() {
  if (state.running) {
    sendBtn.textContent = '停止';
    sendBtn.classList.add('stop');
    sendBtn.disabled = false;
    return;
  }
  sendBtn.textContent = '发送';
  sendBtn.classList.remove('stop');
  sendBtn.disabled = !inputEl.value.trim();
}

/** 请求停止本轮。后端两层机制：置标志位（不再开新的）+ 取消在途请求。 */
async function requestStop() {
  sendBtn.disabled = true;
  sendBtn.textContent = '停止中…';
  try {
    const r = await fetch('/api/stop', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (d.ok) addNotice('已请求停止本轮。已经跑完的发言会保留。', 'warn');
    else addNotice('停止失败：' + (d.error || r.status), 'error');
  } catch (e) {
    addNotice('停止失败：' + e.message, 'error');
  }
  updateSend();
}

function send() {
  // 本轮还在跑 —— 这个按钮现在的身份是「停止」
  if (state.running) { requestStop(); return; }
  const text = inputEl.value.trim();
  if (!text) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    addNotice('连接已断开，正在重连，请稍后再发。', 'error');
    return;
  }
  // 带上附件：文本文件的内容后端会注入消息（模型真能读到），图片只做展示
  const atts = pendingAttachments.slice();
  state.ws.send(JSON.stringify({ action: 'chat', text, attachments: atts, channel: state.channel }));
  if (atts.length) {
    pendingAttachments = [];
    renderAttachTray();
  }
  inputEl.value = '';
  inputEl.style.height = 'auto';
  hideMentionPop();
  updateSend();
}

sendBtn.addEventListener('click', send);

inputEl.addEventListener('keydown', (e) => {
  if (mentionPop.classList.contains('hidden')) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  } else if (e.key === 'Enter' || e.key === 'Tab') {
    e.preventDefault();
    pickMention(state.mentionIndex);
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    state.mentionIndex = (state.mentionIndex + 1) % state.mentionItems.length;
    paintMentionActive();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    state.mentionIndex = (state.mentionIndex - 1 + state.mentionItems.length) % state.mentionItems.length;
    paintMentionActive();
  } else if (e.key === 'Escape') {
    hideMentionPop();
  }
});

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + 'px';
  updateSend();
  maybeShowMentionPop();
});

/* ------------------------- @ 成员选择器 ------------------------- */
state.mentionItems = [];

function maybeShowMentionPop() {
  const value = inputEl.value;
  const caret = inputEl.selectionStart;
  const before = value.slice(0, caret);
  const m = before.match(/@([A-Za-z0-9_\-.\u4e00-\u9fff]*)$/);
  if (!m) { hideMentionPop(); return; }
  const query = m[1].toLowerCase();
  // 第一项固定是「全体成员」——@ 一次唤醒所有人（含组长）
  const allItem = { id: '全体成员', name: '全体成员', avatar: '📢', isAll: true, hidden: false };
  const allHit = !query || '全体成员'.includes(query) || 'all'.includes(query)
    || 'everyone'.includes(query);
  state.mentionItems = (allHit ? [allItem] : []).concat(
    (state.members || [])
      .filter((mem) => !mem.hidden)   // 与左栏一致：隐藏成员不参与 @
      .filter((mem) =>
        !query || mem.id.toLowerCase().includes(query) || (mem.name || '').toLowerCase().includes(query))
  );
  if (!state.mentionItems.length) { hideMentionPop(); return; }
  state.mentionIndex = 0;
  mentionPop.innerHTML = '';
  state.mentionItems.forEach((mem, i) => {
    const item = el('div', 'mention-item', `
      <span class="mention-avatar"></span>
      <span>${esc(mem.name || mem.id)}${mem.isAll ? '（所有人 · 含组长）' : ''}</span>
      <span class="route">${mem.isAll ? '@全体成员' : esc(mem.id)}</span>`);
    // 与左栏/消息气泡共用同一份自定义头像
    fillAvatar(item.querySelector('.mention-avatar'), mem.avatar, initials(mem.name || mem.id),
      memberAvatarOverride(mem.id));
    item.addEventListener('click', () => pickMention(i));
    mentionPop.appendChild(item);
  });
  mentionPop.classList.remove('hidden');
  paintMentionActive();
}

function paintMentionActive() {
  [...mentionPop.children].forEach((c, i) =>
    c.classList.toggle('active', i === state.mentionIndex));
}

function pickMention(index) {
  const mem = state.mentionItems[index];
  if (!mem) return;
  const value = inputEl.value;
  const caret = inputEl.selectionStart;
  const insert = mem.isAll ? '全体成员' : mem.id;
  const before = value.slice(0, caret).replace(/@([A-Za-z0-9_\-.\u4e00-\u9fff]*)$/, '@' + insert + ' ');
  inputEl.value = before + value.slice(caret);
  inputEl.focus();
  inputEl.selectionStart = inputEl.selectionEnd = before.length;
  hideMentionPop();
  updateSend();
}

function hideMentionPop() {
  mentionPop.classList.add('hidden');
  state.mentionItems = [];
}

/* ------------------------------ 侧栏数据刷新 ------------------------------ */
async function refreshHealth() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    state.providers = data.providers || [];
    state.mockOnly = !!data.mock_only;
    renderHealth();
    if (data.adapters) {
      tagAdapter.textContent = data.adapters.ag2 ? 'AG2 内核可用' : '自研内核';
      tagAdapter.title = data.adapters.detail || '';
    }
  } catch (e) {
    state.providers = [];
    renderHealth();
  }
}

async function refreshDiagnostics() {
  try {
    const res = await fetch('/api/diagnostics');
    const data = await res.json();
    state.calls = data.call_log || [];
    state.mockOnly = state.mockOnly || !!data.mock_only;
    if (data.limits) state.limits = data.limits;
    refreshBudget();
    // call_log 只在后端内存里（且上限 200 条），不能当今日的权威来源。
    // 每轮结束顺带把服务端持久化的今日用量拉回来。
    loadUsage();
    renderContextDetail();
  } catch (e) {
    /* 离线或服务未就绪时保持旧值 */
  }
}

/* ------------------------------ 标签页 ------------------------------ */
function wireTabs() {
  const tabs = $('tabs');
  if (!tabs) return;
  tabs.addEventListener('click', (e) => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    const view = btn.dataset.view || 'chat';
    [...tabs.querySelectorAll('.tab')].forEach((t) => t.classList.toggle('active', t === btn));
    ['chat', 'roster', 'context'].forEach((v) => {
      const box = $('view-' + v);
      if (box) box.classList.toggle('active', v === view);
    });
    if (view === 'roster') renderRosterDetail();
    if (view === 'context') renderContextDetail();
  });
  // 「上下文洞察」导航项直接跳到该标签页
  document.querySelectorAll('.nav-item').forEach((item) => {
    if (item.textContent.includes('上下文')) {
      item.addEventListener('click', () => {
        const t = tabs.querySelector('[data-view="context"]');
        if (t) t.click();
      });
    }
  });
}

/* ------------------------------ 侧栏交互 ------------------------------ */
function wireSidebar() {
  const projSel = $('project-select');
  if (projSel) {
    projSel.addEventListener('change', () => {
      state.currentProject = projSel.value;
      saveProjects();
      renderProjects();
    });
  }
  const newBtn = $('proj-new');
  if (newBtn) {
    newBtn.addEventListener('click', () => {
      const name = window.prompt('新项目名称：', '项目' + (state.projects.length + 1));
      if (!name) return;
      const n = name.trim();
      if (!n || state.projects.includes(n)) return;
      state.projects.push(n);
      state.currentProject = n;
      saveProjects();
      renderProjects();
    });
  }
  const editBtn = $('proj-edit');
  if (editBtn) {
    editBtn.addEventListener('click', () => {
      const name = window.prompt('重命名当前项目：', state.currentProject);
      if (!name) return;
      const n = name.trim();
      if (!n) return;
      const i = state.projects.indexOf(state.currentProject);
      if (i >= 0) state.projects[i] = n;
      state.currentProject = n;
      saveProjects();
      renderProjects();
    });
  }
  const arcBtn = $('proj-archive');
  if (arcBtn) {
    arcBtn.addEventListener('click', () => {
      if (state.projects.length <= 1) { window.alert('至少保留一个项目。'); return; }
      const i = state.projects.indexOf(state.currentProject);
      if (i >= 0) state.projects.splice(i, 1);
      state.currentProject = state.projects[0];
      saveProjects();
      renderProjects();
      addNotice('项目已从界面列表移除（后端未实现项目管理，聊天记录不受影响）', 'cleared');
    });
  }

  const mentionBtn = $('btn-mention');
  if (mentionBtn) {
    mentionBtn.addEventListener('click', () => {
      inputEl.focus();
      const pos = inputEl.value.length;
      inputEl.value += '@';
      inputEl.selectionStart = inputEl.selectionEnd = pos + 1;
      maybeShowMentionPop();
      updateSend();
    });
  }
}

/* ------------------------- 深浅色主题 -------------------------
 * 主题值写在 <html data-theme="…"> 上，CSS 变量随之切换。
 * 首次加载的取值已在 <head> 的内联脚本里定好（防闪白），这里只负责按钮与持久化。 */
function currentTheme() {
  // 取不到属性时按深色算 —— 与 <head> 里的默认保持一致
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

function applyTheme(theme, persist) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = document.querySelector('#theme-toggle .theme-icon');
  const label = $('theme-label');
  // 按钮显示「切换到什么」，不是「当前是什么」
  if (icon) icon.textContent = theme === 'dark' ? '☀' : '🌙';
  if (label) label.textContent = theme === 'dark' ? '浅色' : '深色';
  if (persist) {
    try { localStorage.setItem('aig-theme', theme); } catch (e) { /* 隐私模式下忽略 */ }
  }
}

function wireTheme() {
  applyTheme(currentTheme(), false);
  const btn = $('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      applyTheme(currentTheme() === 'dark' ? 'light' : 'dark', true);
    });
  }
}

/* ------------------------- 桌面窗口（Tauri，无边框） -------------------------
 * 页面既能在普通浏览器里打开，也能被 Tauri 装进无边框窗口。区别只有一个：
 * 桌面端 window.__TAURI__ 存在，于是给 <body> 打上 .is-desktop，
 * 让标题栏与窗口按钮显示出来（浏览器里它们一直是 display:none）。
 * 所有调用都做存在性判断，缺 API 时不允许抛错打断页面初始化。 */
function wireDesktop() {
  const T = window.__TAURI__;
  const hasWin = !!(T && T.window && typeof T.window.getCurrentWindow === 'function');

  if (hasWin) {
    document.body.classList.add('is-desktop');
  }

  const win = hasWin ? T.window.getCurrentWindow() : null;
  const safe = (fn) => (...args) => {
    try { return fn(...args); } catch (e) { /* 窗口已关闭等情况下不抛 */ }
  };

  const bind = (id, handler) => {
    const b = $(id);
    if (b) b.addEventListener('click', handler);
  };
  bind('tb-min', safe(() => win && win.minimize()));
  bind('tb-max', safe(() => win && win.toggleMaximize()));
  bind('tb-close', safe(() => win && win.close()));

  // 自检上报：把这台桌面环境的能力用一次 GET 记录下来（uvicorn 访问日志可见），
  // 便于排查「窗口里按钮点不动」到底是 API 没注入还是调用被拒。
  try {
    fetch('/api/health?desktop=' + (hasWin ? 'tauri-window-ok' : 'no-tauri-api')).catch(() => {});
  } catch (e) { /* 忽略 */ }
}

/* ------------------------- 头像自定义（用户 + 任意成员） -------------------------
 * 点某个头像 → 选图片 → 以 dataURL 存进 localStorage：
 *   用户自己：aig-user-avatar        成员：aig-avatar-<成员id>
 * 恢复默认：右键（桌面）/ 长按 400ms（触屏）。
 * 自定义头像优先于配置里的 avatar，且在左栏、消息气泡、@ 列表三处共用同一份。
 * 所有 localStorage 访问都包 try/catch：隐私模式 / 配额满时不允许抛错打断页面。 */
function loadUserAvatar() {
  // 与成员头像同源：由 loadAvatars() 从后端一次性取回
  state.userAvatar = state.userAvatar || '';
}

/** 从后端取回全部自定义头像（用户自己 + 所有成员），一次请求。 */
async function loadAvatars() {
  try {
    const res = await fetch('/api/avatars');
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) return;
    const map = data.avatars || {};
    state.avatars = {};
    Object.keys(map).forEach((k) => {
      if (k === 'user') state.userAvatar = map[k];
      else state.avatars[k] = map[k];
    });
    applyUserAvatar();
    (state.members || []).forEach((m) => applyMemberAvatar(m.id));
  } catch (e) { /* 取不到就用默认头像，不影响使用 */ }
}

/* 一次性迁移：把老版本存在 localStorage 里的头像搬到后端存成文件。
   搬完就清掉 localStorage 的键，避免两边都有、以后改一处忘一处。
   只跑一次 —— 迁移完 localStorage 里就没有这些键了。 */
async function migrateLocalAvatars() {
  const jobs = [];
  const readLocal = (key) => { try { return localStorage.getItem(key) || ''; } catch (e) { return ''; } };

  const userData = readLocal(AVATAR_KEY);
  if (userData) jobs.push({ target: 'user', dataUrl: userData, key: AVATAR_KEY });

  (state.members || []).forEach((m) => {
    if (!m || !m.id) return;
    const key = memberAvatarKey(m.id);
    const v = readLocal(key);
    if (v) jobs.push({ target: m.id, dataUrl: v, key });
  });

  if (!jobs.length) return;
  let moved = 0;
  for (const job of jobs) {
    try {
      const res = await fetch('/api/avatar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: job.target, dataUrl: job.dataUrl }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        try { localStorage.removeItem(job.key); } catch (e) { /* 忽略 */ }
        moved += 1;
      }
    } catch (e) { /* 单张失败不影响其它 */ }
  }
  if (moved) {
    console.info(`[avatar] 已把 ${moved} 张本地头像迁移到 server/config/avatars/`);
    await loadAvatars();
  }
}

/** 把自定义头像应用到左栏「我」那一行 + 所有已渲染的用户消息 */
function applyUserAvatar() {
  const box = $('me-avatar');
  if (box) fillAvatar(box, '我', '我', state.userAvatar || '');
  state.bubbles.forEach((node) => {
    if (!node.classList.contains('user')) return;
    const av = node.querySelector('.msg-avatar');
    if (av) fillAvatar(av, '我', '我', state.userAvatar || '');
  });
}

/** 打开文件选择器：targetId 为空 = 用户自己，否则是该成员的 id */
function pickAvatar(targetId) {
  state.avatarTarget = targetId || '';
  const picker = $('avatar-file');
  if (picker) picker.click();
}

function pickUserAvatar() { pickAvatar(''); }

/** 成员自定义头像生效后，三处一起刷新：左栏列表 / 该成员的消息气泡 / @ 选择器 */
function applyMemberAvatar(id) {
  renderMembers();
  state.bubbles.forEach((node) => {
    if (!id || node.dataset.sender !== id) return;
    const av = node.querySelector('.msg-avatar');
    if (av) {
      const m = (state.members || []).find((x) => x.id === id) || {};
      fillAvatar(av, m.avatar, initials(m.name || id), memberAvatarOverride(id));
    }
  });
  if (mentionPop && !mentionPop.classList.contains('hidden')) maybeShowMentionPop();
}

async function resetAvatarFor(targetId) {
  const target = targetId || 'user';
  try {
    await fetch(`/api/avatar/${encodeURIComponent(target)}`, { method: 'DELETE' });
  } catch (e) { /* 删不掉也要把界面恢复，别卡住 */ }
  if (targetId) {
    delete state.avatars[targetId];
    // 后端会把 models.yaml 的 avatar 清掉；成员视图要重新拉一次才看得到
    try {
      const res = await fetch('/api/snapshot');
      const data = await res.json();
      if (Array.isArray(data.members)) state.members = data.members;
    } catch (e) { /* 忽略 */ }
    applyMemberAvatar(targetId);
  } else {
    state.userAvatar = '';
    applyUserAvatar();
  }
}

function resetUserAvatar() { resetAvatarFor(''); }

async function saveAvatarFor(targetId, dataUrl) {
  // 存到后端变成文件（server/config/avatars/custom_<目标>.<ext>），
  // 不再塞进 localStorage —— 那样看不见、换版本会丢、还受 5MB 配额限制。
  const target = targetId || 'user';
  try {
    const res = await fetch('/api/avatar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target, dataUrl }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      window.alert('头像保存失败：' + (data.error || `HTTP ${res.status}`));
      return false;
    }
    if (target === 'user') state.userAvatar = dataUrl;
    else state.avatars[target] = dataUrl;
    return true;
  } catch (err) {
    window.alert('头像保存失败：' + (err && err.message ? err.message : err));
    return false;
  }
}

function wireUserAvatar() {
  loadUserAvatar();
  // 头像存在后端（server/config/avatars/），拉一次全拿到；
  // 顺便把老版本遗留在 localStorage 里的头像迁过去（只跑一次，迁完就清键）。
  loadAvatars().then(migrateLocalAvatars);
  applyUserAvatar();

  const row = $('me-row');
  if (row) {
    // 左栏「我」整行可点（视觉上头像才是按钮，但整行命中区更友好）
    row.addEventListener('click', (e) => {
      if (e.target.closest('#avatar-file')) return;
      pickAvatar('');
    });
    row.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      if (!state.userAvatar) return;
      if (window.confirm('恢复默认头像？')) resetAvatarFor('');
    });
    bindLongPress(row, () => { if (state.userAvatar) resetAvatarFor(''); });
  }

  const picker = $('avatar-file');
  if (picker) {
    picker.addEventListener('change', () => {
      const file = picker.files && picker.files[0];
      const targetId = state.avatarTarget || '';
      picker.value = '';   // 允许重复选择同一个文件
      if (!file) return;
      if (!/^image\//.test(file.type)) { window.alert('请选择图片文件（png / jpg / gif / svg…）。'); return; }
      const reader = new FileReader();
      reader.addEventListener('load', async () => {
        const dataUrl = String(reader.result || '');
        if (!dataUrl.startsWith('data:image/')) { window.alert('读取失败：这不是有效的图片。'); return; }
        if (dataUrl.length > AVATAR_MAX_CHARS) {
          window.alert('图片太大了，请换一张小一点的。');
          return;
        }
        // 存到后端（写文件）；失败会自己弹提示，这里只管成功后的界面刷新
        const saved = await saveAvatarFor(targetId, dataUrl);
        if (!saved) return;
        if (targetId) {
          applyMemberAvatar(targetId);
        } else {
          applyUserAvatar();
        }
      });
      reader.addEventListener('error', () => { window.alert('读取图片失败，请换一张试试。'); });
      reader.readAsDataURL(file);
    });
  }
}

/* ------------------------------ 初始化 ------------------------------ */
function init() {
  // 启动就把今日用量拉回来 —— 否则大退后侧栏会从 ¥0.00 重新开始
  loadUsage();
  loadProjects();
  wireSidebar();
  wireTabs();
  wireTheme();
  wireDesktop();
  wireUserAvatar();
  wireAddMember();
  wireHeadMenu();
  wireMemberMenu();
  wireThinkConsole();
  wireEditMember();
  wireSettings();
  wireMessageActions();
  wireSearch();
  wireGroupName();
  wireEmoji();
  wireAttachments();
  wireDetectKey();
  wireTestModel();
  wireTestAll();
  wireModelMenu();

  document.querySelectorAll('.sample').forEach((btn) => {
    btn.addEventListener('click', () => {
      inputEl.value = btn.textContent.trim();
      updateSend();
      inputEl.focus();
    });
  });

  $('reset-btn').addEventListener('click', async () => {
    if (!confirm('确定清空全部聊天记录？')) return;
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ action: 'reset' }));
    } else {
      await fetch('/api/reset', { method: 'POST' });
      streamEl.innerHTML = '';
      streamEl.appendChild(emptyHint);
    }
  });

  connect();
  refreshHealth();
  refreshDiagnostics();
  setInterval(() => { refreshHealth(); refreshDiagnostics(); }, 15000);
  updateSend();
}


/* ==========================================================================
   2026-10-13 · 界面精简新增的三块逻辑
   --------------------------------------------------------------------------
   1) wireAddMember —— 侧栏「＋」打开添加成员弹窗：填名字 / 提供商 / 模型 / API Key
   2) wireMemberMenu —— 点击成员行弹出小菜单：设为组长、移除成员
   3) wireHeadMenu  —— 右上「⋯」菜单：运行信息、清空聊天记录
   后端接口：GET /api/member-presets、POST /api/member、DELETE /api/member/{id}
   ========================================================================== */

/* 说话风格模板：与后端 group/members.py 的 ROLE_TEMPLATES 一一对应 */
const ROLE_TEMPLATES = [
  { file: 'member_analyst.md', label: '论证担当 —— 长文论证 · 深挖依据' },
  { file: 'member_critic.md', label: '反驳担当 —— 快速反驳 · 挑风险' },
  { file: 'member_engineer.md', label: '工程落地担当 —— 能不能真跑 · 要付多少代价' },
];

/* 与后端 members._slug_id 保持同一套规则，让用户填名字时就知道 @ 出来是什么。
   规则：保留中英文数字与 _ - .，小写；为空或纯数字则退化成 member。
   两边若有偏差，最坏只是预览不准，不影响实际写入。 */
function previewMemberId(name) {
  let base = (name || '').toLowerCase()
    .replace(/[^a-z0-9_\-.\u4e00-\u9fff]+/g, '')
    .replace(/^[-_.]+|[-_.]+$/g, '');
  if (!base || /^[0-9]+$/.test(base)) base = 'member';
  const taken = (state.members || []).map((m) => String(m.id).toLowerCase());
  let candidate = base;
  let n = 2;
  while (taken.includes(candidate)) { candidate = base + n; n += 1; }
  return candidate;
}

let presetsCache = null;

async function loadPresets() {
  if (presetsCache) return presetsCache;
  try {
    const res = await fetch('/api/member-presets');
    const data = await res.json();
    presetsCache = Array.isArray(data.presets) ? data.presets : [];
  } catch (err) {
    presetsCache = [];
  }
  return presetsCache;
}

/* --------------------------------------------------------- 添加成员弹窗 */
function wireAddMember() {
  const modal = $('add-modal');
  if (!modal || !modal.classList) return;
  const nameEl = $('am-name');
  const providerEl = $('am-provider');
  const noteEl = $('am-note');
  const baseField = $('am-baseurl-field');
  const baseEl = $('am-baseurl');
  const modelEl = $('am-model');
  const keyEl = $('am-key');
  const roleEl = $('am-role');
  const avatarEl = $('am-avatar');
  const promptEl = $('am-prompt');
  const idPreview = $('am-id-preview');
  const msgEl = $('am-msg');
  const submitEl = $('am-submit');


  let presets = [];

  function syncProvider() {
    const preset = presets.find((p) => p.id === providerEl.value);
    if (!preset) return;
    noteEl.textContent = preset.note || '';
    baseField.hidden = preset.id !== 'custom';
    if (preset.id !== 'custom') baseEl.value = preset.baseUrl || '';
    setModelOptions(preset.models || []);
    // 判断条件必须是「为空 **或** 不在新提供商的列表里」→ 才重置为第一个。
    // 原来漏了一个 `!`，语义整个反了：
    //   · 模型在当前列表里 → 反而被覆盖成第一个（冲掉用户的合法选择）
    //   · 模型不在列表里   → 反而被保留（留下上一个提供商的模型 id）
    // 后果很实在：手动把提供商从 DeepSeek 切到智谱，模型字段仍写着
    // `deepseek-v4-flash`，一提交就是个模型不存在的成员，调用必然失败。
    const stillValid = modelEl.value
      && !!modelList.querySelector(`option[value="${CSS.escape(modelEl.value)}"]`);
    if (!stillValid) {
      modelEl.value = (preset.models || [])[0] || '';
    }
    // 该提供商已配过密钥 → 提示可以留空
    keyEl.placeholder = preset.hasKey
      ? '已配置过，可留空'
      : 'sk-…（只会写进 server/.env）';
  }

  function showMsg(text, ok) {
    msgEl.textContent = text;
    msgEl.classList.remove('hidden');
    msgEl.classList.toggle('ok', !!ok);
  }
  function clearMsg() {
    msgEl.classList.add('hidden');
    msgEl.textContent = '';
    msgEl.classList.remove('ok');
  }

  async function open() {
    clearMsg();
    modal.classList.remove('hidden');
    if (!presets.length) {
      presets = await loadPresets();
      providerEl.innerHTML = '';
      presets.forEach((p) => {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.label + (p.hasKey ? ' ✓已有密钥' : '');
        providerEl.appendChild(opt);
      });
    }
    syncProvider();
    if (promptEl) promptEl.value = '';
    if (idPreview) idPreview.textContent = '—';
    nameEl.focus();
  }

  function close() {
    modal.classList.add('hidden');
  }

  // 边打名字边显示 @ 用的 id（与后端 _slug_id 同规则）
  nameEl.addEventListener('input', () => {
    if (idPreview) idPreview.textContent = previewMemberId(nameEl.value);
  });

  $('add-member-btn').addEventListener('click', open);
  $('am-close').addEventListener('click', close);
  $('am-cancel').addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) close();
  });
  providerEl.addEventListener('change', syncProvider);

  submitEl.addEventListener('click', async () => {
    const name = (nameEl.value || '').trim();
    if (!name) return showMsg('请填写成员名字');
    if (/\s/.test(name)) return showMsg('名字里不能有空格（否则 @提及 会失效）');
    if (!(modelEl.value || '').trim()) return showMsg('请填写模型 id');
    if (providerEl.value === 'custom' && !(baseEl.value || '').trim()) {
      return showMsg('自定义端点必须填接口地址');
    }
    submitEl.disabled = true;
    clearMsg();
    try {
      const res = await fetch('/api/member', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          provider: providerEl.value,
          model: (modelEl.value || '').trim(),
          apiKey: (keyEl.value || '').trim(),
          baseUrl: (baseEl.value || '').trim(),
          role: roleEl.value,
          // 界面上不再填「职责」；组长名单会自动从提示词里摘一句
          duty: '',
          rolePrompt: promptEl ? promptEl.value : '',
          avatar: (avatarEl.value || '').trim(),
          template: '',   // 「说话风格」已移除；不传提示词时后端用默认模板
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        showMsg(data.error || `添加失败（HTTP ${res.status}）`);
        return;
      }
      const c = data.created || {};
      showMsg(`已添加 ${c.name || name}（${c.route || ''}）${c.hasKey ? '' : ' · 注意：该提供商还没有可用密钥'}`, true);
      nameEl.value = ''; keyEl.value = ''; avatarEl.value = ''; promptEl.value = '';
      presetsCache = null;   // 下次打开重新拉，hasKey 可能变了
      setTimeout(close, 1400);
    } catch (err) {
      showMsg('请求失败：' + (err && err.message ? err.message : err));
    } finally {
      submitEl.disabled = false;
    }
  });
}

/* --------------------------------------------------------- 成员操作菜单 */
function wireMemberMenu() {
  let menu = null;
  function closeMenu() {
    if (menu && menu.parentNode) menu.parentNode.removeChild(menu);
    menu = null;
  }
  document.addEventListener('click', closeMenu);
  document.addEventListener('scroll', closeMenu, true);

  membersEl.addEventListener('click', (e) => {
    const row = e.target.closest ? e.target.closest('.ws-item') : null;
    if (!row) return;
    // 点头像是在换头像，不该弹菜单
    if (e.target.closest && e.target.closest('.avatar-editable')) return;
    e.stopPropagation();
    closeMenu();

    const id = row.dataset.id;
    const member = (state.members || []).find((m) => m.id === id);
    if (!member) return;
    const isCaptain = member.role === 'captain';

    menu = el('div', 'member-menu');
    const editBtn = el('button', '', '编辑（换 Key / 改模型）');
    editBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      closeMenu();
      openEditMember(id);
    });
    menu.appendChild(editBtn);

    const capBtn = el('button', '', isCaptain ? '已是组长' : '设为组长');
    capBtn.disabled = isCaptain;
    capBtn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      closeMenu();
      await setMemberRole(id, 'captain');
    });
    menu.appendChild(capBtn);

    const delBtn = el('button', 'danger', '移除成员');
    delBtn.disabled = isCaptain;
    delBtn.title = isCaptain ? '请先让另一位成员当组长' : '';
    delBtn.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      closeMenu();
      if (!confirm(`确定把「${member.name || id}」移出群？\n（会从 config/models.yaml 删除，不可撤销）`)) return;
      try {
        const res = await fetch(`/api/member/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
          rosterMessage(data.error || `删除失败（HTTP ${res.status}）`, true);
        } else {
          rosterMessage(`已移除 ${data.name || id}`, false);
        }
      } catch (err) {
        rosterMessage('请求失败：' + (err && err.message ? err.message : err), true);
      }
    });
    menu.appendChild(delBtn);

    document.body.appendChild(menu);
    const rect = row.getBoundingClientRect();
    menu.style.left = Math.round(rect.left + 12) + 'px';
    menu.style.top = Math.round(Math.min(rect.bottom + 4, window.innerHeight - 110)) + 'px';
  });
}

/* ------------------------------------------------------------ 右上菜单 */
function wireHeadMenu() {
  const btn = $('menu-btn');
  const menu = $('head-menu');
  if (!btn || !menu || !menu.classList) return;

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.classList.toggle('hidden');
  });
  document.addEventListener('click', (e) => {
    if (!menu.classList.contains('hidden') && !menu.contains(e.target)) {
      menu.classList.add('hidden');
    }
  });

  menu.addEventListener('click', async (e) => {
    const target = e.target.closest ? e.target.closest('button[data-menu]') : null;
    if (!target) return;
    menu.classList.add('hidden');
    const what = target.dataset.menu;
    if (what === 'think') {
      showThinkConsole();
      return;
    }
    if (what === 'export') {
      exportChat();
      return;
    }
    if (what === 'tasks') {
      showTaskPanel();
      return;
    }
    if (what === 'usage') {
      showUsagePanel();
      return;
    }
    if (what === 'workspace') {
      // 在程序里看工作区 —— 不去拉 explorer（那是 0xc0000142 的来源）
      showWorkspacePanel();
      return;
    }
    if (what === 'settings') {
      openSettings();
      return;
    }
    if (what === 'reset') {
      if (!confirm('确定清空全部聊天记录？')) return;
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ action: 'reset' }));
      } else {
        await fetch('/api/reset', { method: 'POST' });
        streamEl.innerHTML = '';
      }
    } else if (what === 'info') {
      showInfoPanel();
    }
  });
}

/* 运行信息：把隐藏的 #health / #context-detail 搬进一个浮层显示，关掉再搬回去，
   这样既不占常驻界面，又复用了已有的渲染逻辑。 */
/** 轻提示：浮一条，几秒后自动消失。
 * 打开工作区这类"结果在文件系统里、界面上没地方显示"的操作靠它给反馈。 */
function showToast(text, kind) {
  let box = document.getElementById('toast-box');
  if (!box) {
    box = el('div', 'toast-box');
    box.id = 'toast-box';
    document.body.appendChild(box);
  }
  const node = el('div', 'toast' + (kind ? ' ' + kind : ''), String(text));
  box.appendChild(node);
  setTimeout(() => { if (node.parentNode) node.parentNode.removeChild(node); }, 3200);
}


/** 工作区面板：列文件、看内容、复制路径。
 *
 * 为什么要有这个：之前「打开工作区」是去拉 explorer.exe，
 * 而它在打包后的程序里起不来（0xc0000142）。把看文件这件事
 * **做进程序内部**，就不依赖任何外部程序了 —— 打不开也不影响你看东西。
 */
async function showWorkspacePanel() {
  const old = document.getElementById('ws-modal');
  if (old && old.parentNode) old.parentNode.removeChild(old);

  const modal = el('div', 'modal');
  modal.id = 'ws-modal';
  const card = el('div', 'modal-card');

  const head = el('div', 'modal-head');
  head.appendChild(el('span', '', '工作区'));
  const x = el('button', 'modal-x', '✕');
  head.appendChild(x);
  card.appendChild(head);

  const body = el('div', 'modal-body');
  card.appendChild(body);
  modal.appendChild(card);
  document.body.appendChild(modal);

  // 顶部：路径 + 操作
  const bar = el('div', 'ws-bar');
  const pathText = el('code', 'ws-path', '读取中…');
  pathText.title = '工作区目录';
  bar.appendChild(pathText);
  const copyBtn = el('button', 'btn-mini', '复制路径');
  const openBtn = el('button', 'btn-mini', '在资源管理器打开');
  const actions = el('div', 'ws-actions');
  actions.appendChild(copyBtn);
  actions.appendChild(openBtn);
  bar.appendChild(actions);
  body.appendChild(bar);

  const listBox = el('div', 'ws-list');
  body.appendChild(listBox);

  const preview = el('pre', 'ws-preview');
  preview.style.display = 'none';
  body.appendChild(preview);

  function close() {
    if (modal.parentNode) modal.parentNode.removeChild(modal);
  }
  x.addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
  });

  let dir = '';
  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(dir);
      showToast('路径已复制，粘到资源管理器地址栏即可打开', 'ok');
    } catch (e) {
      showToast('复制失败，请手动选中路径复制', 'bad');
    }
  });

  openBtn.addEventListener('click', async () => {
    try {
      const res = await fetch('/api/open-workspace', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (data.ok) showToast('已打开', 'ok');
      else showToast('自动打开失败，请用「复制路径」自己打开', 'bad');
    } catch (e) {
      showToast('自动打开失败，请用「复制路径」自己打开', 'bad');
    }
  });

  function fmtSize(n) {
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(2) + ' MB';
  }

  async function loadList() {
    listBox.textContent = '';
    let data = {};
    try {
      const res = await fetch('/api/workspace');
      data = await res.json();
    } catch (e) {
      listBox.appendChild(el('div', 'ws-empty', '读取失败：' + e.message));
      return;
    }
    dir = data.dir || '';
    pathText.textContent = dir || '（未知）';
    const files = data.files || [];
    if (!files.length) {
      listBox.appendChild(el('div', 'ws-empty',
        '工作区还是空的。让某个成员产出文件试试，比如：\n@Kimi 写个 hello.txt，内容是 hello world'));
      return;
    }
    listBox.appendChild(el('div', 'ws-count',
      `共 ${files.length} 个文件，${fmtSize(data.totalBytes || 0)}`));
    files.forEach((f) => {
      const row = el('div', 'ws-file');
      row.appendChild(el('span', 'ws-file-name', f.path));
      row.appendChild(el('span', 'ws-file-size', fmtSize(f.bytes || 0)));
      row.addEventListener('click', () => loadFile(f.path, row));
      listBox.appendChild(row);
    });
  }

  async function loadFile(rel, row) {
    listBox.querySelectorAll('.ws-file.active').forEach((n) => n.classList.remove('active'));
    if (row) row.classList.add('active');
    preview.style.display = 'block';
    preview.textContent = '读取中…';
    try {
      const res = await fetch('/api/workspace/file?path=' + encodeURIComponent(rel));
      const data = await res.json();
      if (!data.ok) { preview.textContent = '读取失败：' + (data.error || res.status); return; }
      preview.textContent = data.content + (data.truncated ? '\n\n…（内容过长，已截断）' : '');
    } catch (e) {
      preview.textContent = '读取失败：' + e.message;
    }
  }

  await loadList();
}


/** 用量与额度：按成员分解的今日消耗 + 各厂商真实余额。
 *
 * 为什么值得单独做一张表：
 *   侧栏只显示「今日总共花了多少」，看不出**谁在花钱、谁在干活**。
 *   而"7 个模型各有所长"这件事，只有按成员看用量才能验证 ——
 *   如果某个成员一次都没被调用过，那它再"擅长"也没用上。
 *
 * 余额部分**不假装统一**：只有 DeepSeek / Kimi 提供接口，
 * 其余的如实写「该厂商未提供」，不编数字。
 */
async function showUsagePanel() {
  const old = document.getElementById('usage-modal');
  if (old && old.parentNode) old.parentNode.removeChild(old);

  const modal = el('div', 'modal');
  modal.id = 'usage-modal';
  const card = el('div', 'modal-card');
  const head = el('div', 'modal-head');
  head.appendChild(el('span', '', '用量与额度'));
  const x = el('button', 'modal-x', '✕');
  head.appendChild(x);
  card.appendChild(head);

  const body = el('div', 'modal-body');
  card.appendChild(body);
  modal.appendChild(card);
  document.body.appendChild(modal);

  const bar = el('div', 'ws-bar');
  const note = el('div', 'ws-path', '读取中…');
  bar.appendChild(note);
  const actions = el('div', 'ws-actions');
  const refreshBtn = el('button', 'btn-mini', '刷新余额');
  actions.appendChild(refreshBtn);
  bar.appendChild(actions);
  body.appendChild(bar);

  const table = el('div', 'usage-table');
  body.appendChild(table);

  const quotaBox = el('div', 'usage-quota');
  body.appendChild(quotaBox);

  function close() {
    if (modal.parentNode) modal.parentNode.removeChild(modal);
  }
  x.addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
  });

  function money(n) { return '¥' + (Number(n) || 0).toFixed(3); }
  function num(n) { return (Number(n) || 0).toLocaleString('zh-CN'); }

  async function load(force) {
    note.textContent = '读取中…';
    let usage = {}, balances = {};
    try {
      const [u, b] = await Promise.all([
        fetch('/api/usage').then((r) => r.json()).catch(() => ({})),
        fetch('/api/balance' + (force ? '?refresh=1' : ''))
          .then((r) => r.json()).catch(() => ({})),
      ]);
      usage = u || {};
      balances = (b && b.balances) || {};
    } catch (e) {
      note.textContent = '读取失败：' + e.message;
      return;
    }

    const members = state.members || [];
    const byMember = usage.members || {};
    const byProv = usage.providers || {};

    // 每个成员：先按 provider 分摊 —— 一个 provider 可能挂着多个成员，
    // 但服务端的 provider 计数是**合在一起**的，没法精确拆到人。
    // 所以这里按"该 provider 下有几个成员"平均分，并标注清楚是估算。
    const provMembers = {};
    members.forEach((m) => {
      const p = m.provider || '';
      (provMembers[p] = provMembers[p] || []).push(m.id);
    });

    note.textContent = `日期 ${usage.date || '—'}　共 ${usage.calls || 0} 次调用`
      + `（失败 ${usage.failed || 0} 次）`;

    table.textContent = '';
    const head2 = el('div', 'usage-row usage-head');
    ['成员', '今日花费', '调用', 'tokens', '余额'].forEach((t) => {
      head2.appendChild(el('span', 'usage-cell', t));
    });
    table.appendChild(head2);

    let totalCost = 0;
    members.forEach((m) => {
      const p = m.provider || '';
      const share = 1 / Math.max(1, (provMembers[p] || []).length);
      const pv = byProv[p] || {};
      const mine = byMember[m.id] || {};
      // 优先用**成员级**计数（服务端有记），没有再按 provider 平均分
      const chars = mine.chars != null ? Number(mine.chars)
        : Math.round(Number(pv.chars || 0) * share);
      const reasoning = mine.reasoning_chars != null ? Number(mine.reasoning_chars)
        : Math.round(Number(pv.reasoning_chars || 0) * share);
      const calls = mine.calls != null ? Number(mine.calls)
        : Math.round(Number(pv.calls || 0) * share);

      const price = PRICE_CNY_PER_MTOK[p] || { in: 0, out: 0 };
      const outTok = (chars + reasoning) / CHARS_PER_TOKEN;
      const inTok = outTok * 3;
      const cost = (inTok / 1e6) * price.in + (outTok / 1e6) * price.out;
      totalCost += cost;

      const row = el('div', 'usage-row');
      row.appendChild(el('span', 'usage-cell usage-name', m.name || m.id));
      row.appendChild(el('span', 'usage-cell', money(cost)));
      row.appendChild(el('span', 'usage-cell', calls || '—'));
      row.appendChild(el('span', 'usage-cell', outTok ? num(Math.round(outTok)) : '—'));

      const bal = balances[p];
      const cell = el('span', 'usage-cell');
      if (bal && bal.ok) {
        // 厂商原样返回很多位小数（实测 Kimi 给到 9.4090805），显示两位就够
        const amount = Number(bal.amount);
        const shown = Number.isFinite(amount) ? amount.toFixed(2) : bal.amount;
        cell.textContent = (bal.currency === 'USD' ? '$' : '¥') + shown;
        cell.title = '厂商返回的真实余额';
        cell.classList.add('usage-bal-ok');
      } else if (bal && bal.unsupported) {
        cell.textContent = '厂商未提供';
        cell.classList.add('usage-bal-none');
      } else if (bal && bal.error) {
        cell.textContent = '查询失败';
        cell.title = bal.error;
        cell.classList.add('usage-bal-none');
      } else {
        cell.textContent = '—';
      }
      row.appendChild(cell);
      table.appendChild(row);
    });

    const foot = el('div', 'usage-row usage-foot');
    foot.appendChild(el('span', 'usage-cell', '合计'));
    foot.appendChild(el('span', 'usage-cell', money(totalCost)));
    foot.appendChild(el('span', 'usage-cell', usage.calls || 0));
    foot.appendChild(el('span', 'usage-cell', ''));
    foot.appendChild(el('span', 'usage-cell', ''));
    table.appendChild(foot);

    // 配额详情（目前只有 Kimi 给）
    quotaBox.textContent = '';
    Object.entries(balances).forEach(([pid, b]) => {
      if (!b.quota) return;
      const who = (b.members || []).join('、') || pid;
      const lines = Object.entries(b.quota)
        .filter(([, v]) => v != null)
        .map(([k, v]) => `${k} ${num(v)}`);
      if (!lines.length) return;
      const box = el('div', 'quota-item');
      box.appendChild(el('div', 'quota-title', `${who} 的配额（厂商接口返回）`));
      box.appendChild(el('div', 'quota-body', lines.join(' · ')));
      quotaBox.appendChild(box);
    });

    const tip = el('div', 'usage-tip');
    tip.textContent = '花费按调用字符数估算，非账单口径。余额为厂商接口返回值，'
      + '缓存约 2 分钟；标「厂商未提供」的是它没有这个接口，不是查失败。';
    body.appendChild(tip);
  }

  refreshBtn.addEventListener('click', () => load(true));
  await load(false);
}


/** 任务板：跨轮次、跨重启保留的进度。
 *
 * 为什么需要它：验收只管一轮。关掉 app 第二天回来说"继续"，
 * 上一轮做到哪了就只剩聊天记录 —— 有了板子才知道"还有哪条没做完"。
 */
async function showTaskPanel() {
  const old = document.getElementById('task-modal');
  if (old && old.parentNode) old.parentNode.removeChild(old);

  const modal = el('div', 'modal');
  modal.id = 'task-modal';
  const card = el('div', 'modal-card');
  const head = el('div', 'modal-head');
  head.appendChild(el('span', '', '任务板'));
  const x = el('button', 'modal-x', '✕');
  head.appendChild(x);
  card.appendChild(head);
  const body = el('div', 'modal-body');
  card.appendChild(body);
  modal.appendChild(card);
  document.body.appendChild(modal);

  const bar = el('div', 'ws-bar');
  const note = el('div', 'ws-path', '读取中…');
  bar.appendChild(note);
  const actions = el('div', 'ws-actions');
  const clearBtn = el('button', 'btn-mini', '清空任务板');
  actions.appendChild(clearBtn);
  bar.appendChild(actions);
  body.appendChild(bar);

  const list = el('div', 'task-list');
  body.appendChild(list);

  function close() {
    if (modal.parentNode) modal.parentNode.removeChild(modal);
  }
  x.addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
  });

  const STATUS = { todo: '待办', doing: '进行中', done: '已完成', blocked: '卡住' };
  const ORDER = { doing: 0, todo: 1, blocked: 2, done: 3 };

  clearBtn.addEventListener('click', async () => {
    if (!confirm('清空任务板？')) return;
    try {
      await fetch('/api/tasks', { method: 'DELETE' });
      showToast('任务板已清空', 'ok');
      await load();
    } catch (e) { showToast('清空失败：' + e.message, 'bad'); }
  });

  async function load() {
    note.textContent = '读取中…';
    let data = {};
    try {
      data = await (await fetch('/api/tasks')).json();
    } catch (e) {
      note.textContent = '读取失败：' + e.message;
      return;
    }
    const tasks = data.tasks || [];
    const c = data.counts || {};
    note.textContent = tasks.length
      ? `共 ${tasks.length} 条：${STATUS.todo} ${c.todo || 0} · ${STATUS.doing} ${c.doing || 0}`
        + ` · ${STATUS.done} ${c.done || 0} · ${STATUS.blocked} ${c.blocked || 0}`
      : '任务板是空的';
    list.textContent = '';
    if (!tasks.length) {
      const empty = el('div', 'ws-empty',
        '还没有任务。组长派活时会把任务登记进来，验收后更新状态。\n'
        + '下次接着说「继续」，它就能看到还有哪条没做完。');
      list.appendChild(empty);
      return;
    }
    tasks.slice().sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9))
      .forEach((t) => {
        const row = el('div', 'task-row task-' + (t.status || 'todo'));
        row.appendChild(el('span', 'task-status', STATUS[t.status] || t.status || '?'));
        const mid = el('div', 'task-mid');
        mid.appendChild(el('div', 'task-title', t.title || '(无标题)'));
        if (t.note) mid.appendChild(el('div', 'task-note', t.note));
        row.appendChild(mid);
        row.appendChild(el('span', 'task-owner', t.owner ? '@' + t.owner : ''));
        list.appendChild(row);
      });
  }

  await load();
}


function showInfoPanel() {
  const existing = document.getElementById('info-modal');
  if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

  const home = $('view-context');
  const healthBox = $('health');
  const detailBox = $('context-detail');

  const modal = el('div', 'modal');
  modal.id = 'info-modal';
  const card = el('div', 'modal-card');
  const head = el('div', 'modal-head');
  head.appendChild(el('span', '', '运行信息'));
  const x = el('button', 'modal-x', '✕');
  head.appendChild(x);
  const body = el('div', 'modal-body');
  card.appendChild(head);
  card.appendChild(body);
  modal.appendChild(card);
  document.body.appendChild(modal);

  body.appendChild(healthBox);
  body.appendChild(detailBox);
  refreshHealth();
  refreshDiagnostics();

  function close() {
    home.appendChild(healthBox);
    home.appendChild(detailBox);
    if (modal.parentNode) modal.parentNode.removeChild(modal);
    document.removeEventListener('keydown', onKey);
  }
  function onKey(e) { if (e.key === 'Escape') close(); }
  x.addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', onKey);
}

/* ==========================================================================
   模型思考 · 实时控制台
   --------------------------------------------------------------------------
   用户要求：聊天框里不显示推理过程，只留答案；思考收进右上「⋯ → 模型思考」。
   这一页是**实时控制台**：
     · 只显示「此刻正在思考」的那个成员，跟着流式滚动；换人时自动切过去
     · 底部一排小标签列出本轮各成员思考了多少字，点一下切过去看它
     · 每轮开始清空（用户选了「只留本轮」）
   数据来源仍是后端的 agent_reasoning 事件，只是不再渲染进气泡。
   ========================================================================== */
state.think = { byAgent: {}, order: [], current: null, open: false };

/** 新一轮开始 / 清空记录：丢掉上一轮的思考 */
function thinkReset() {
  state.think = { byAgent: {}, order: [], current: null, open: state.think.open };
  renderThinkConsole();
}

/** 收到思考增量：累加进该成员的缓冲，并把控制台切到它 */
function feedThinkConsole(agentId, text, chars) {
  const t = state.think;
  let entry = t.byAgent[agentId];
  if (!entry) {
    entry = t.byAgent[agentId] = { text: '', chars: 0, done: false };
    t.order.push(agentId);
  }
  if (text) entry.text += text;
  if (Number.isFinite(chars)) entry.chars = chars;
  entry.done = false;
  t.current = agentId;
  renderThinkConsole(true);
}

/** 该成员这一轮结束：定稿字数 */
function markThinkConsoleDone(agentId, chars) {
  const entry = state.think.byAgent[agentId];
  if (!entry) return;
  entry.done = true;
  if (Number.isFinite(chars) && chars > 0) entry.chars = chars;
  renderThinkConsole();
}

/** 把控制台渲染出来。没打开时只更新状态、不碰 DOM。 */
function renderThinkConsole(streaming) {
  const t = state.think;
  if (!t.open) return;
  const textEl = $('think-text');
  const metaEl = $('think-meta');
  const chipsEl = $('think-chips');
  if (!textEl || !textEl.classList) return;

  const id = t.current;
  const entry = id ? t.byAgent[id] : null;

  // 顶部：谁在想、想了多少、什么状态
  if (!id || !entry) {
    metaEl.innerHTML = '';
    textEl.className = 'think-text idle';
    textEl.textContent = '空闲 —— 还没有人在思考。发一条消息就会在这里实时显示推理过程。';
    chipsEl.innerHTML = '';
    return;
  }

  const meta = agentMeta(id);
  const live = streaming && !entry.done;
  metaEl.innerHTML = `
    <span class="tm-name">${esc(meta.name || id)}</span>
    <span class="tm-role">${esc(ROLE_LABEL[meta.role] || meta.role || '')}</span>
    <span class="tm-chars">已想 ${entry.chars.toLocaleString()} 字</span>
    <span class="tm-state ${live ? 'live' : ''}">${live ? '思考中…' : '已结束'}</span>`;

  // 正文：整段推理，跟着滚
  textEl.className = 'think-text';
  const stick = textEl.dataset.userScrolled !== '1';
  if (textEl.dataset.agent !== id) {
    textEl.dataset.agent = id;
    textEl.textContent = entry.text;
    delete textEl.dataset.userScrolled;
  } else {
    textEl.textContent = entry.text;
  }
  if (stick) textEl.scrollTop = textEl.scrollHeight;

  // 底部：本轮每个成员的字数，点一下切过去
  chipsEl.innerHTML = '';
  t.order.forEach((aid) => {
    const e = t.byAgent[aid];
    if (!e) return;
    const m = agentMeta(aid);
    const chip = el('span', `think-chip${aid === id ? ' on' : ''}`);
    chip.innerHTML = `${esc(m.name || aid)} <span class="tc-n">${e.chars.toLocaleString()} 字${e.done ? '' : ' …'}</span>`;
    chip.title = '点一下看这个成员的思考';
    chip.addEventListener('click', () => {
      state.think.current = aid;
      renderThinkConsole(!e.done);
    });
    chipsEl.appendChild(chip);
  });
}

function showThinkConsole() {
  state.think.open = true;
  $('think-modal').classList.remove('hidden');
  renderThinkConsole();
}

function hideThinkConsole() {
  state.think.open = false;
  $('think-modal').classList.add('hidden');
}

function wireThinkConsole() {
  const modal = $('think-modal');
  if (!modal || !modal.classList) return;
  $('think-close').addEventListener('click', hideThinkConsole);
  modal.addEventListener('click', (e) => { if (e.target === modal) hideThinkConsole(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) hideThinkConsole();
  });
  const textEl = $('think-text');
  if (textEl && textEl.addEventListener) {
    // 用户自己往上翻时，别再强行拉到底
    textEl.addEventListener('scroll', () => {
      const atBottom = textEl.scrollHeight - textEl.scrollTop - textEl.clientHeight < 24;
      if (atBottom) delete textEl.dataset.userScrolled;
      else textEl.dataset.userScrolled = '1';
    });
  }
}

/* ==========================================================================
   编辑成员（需求 1）+ 对话设置（需求 4）
   --------------------------------------------------------------------------
   · 编辑成员：最常用的场景是「换 API Key」，所以 Key 单独放最上面、留空即不修改。
     接口 PATCH /api/member/{id}，只提交改动过的字段。
   · 对话设置：单轮发言数 / @ 深度 / 上下文上限。接口 GET+PUT /api/limits，
     改完后端会热替换护栏，**不用重启**。
   ========================================================================== */

let editingId = null;
// 刚载入的提示词原文：用来判断用户有没有真的改过
let lastLoadedPrompt = '';

function openEditMember(agentId) {
  const m = (state.members || []).find((x) => x.id === agentId);
  if (!m) { rosterMessage('找不到这个成员', true); return; }
  editingId = agentId;

  $('em-title').textContent = `编辑成员 · ${m.name || agentId}`;
  $('em-summary').innerHTML =
    `<span class="em-chip">id：${esc(agentId)}</span>` +
    `<span class="em-chip">路由：${esc(m.route || '—')}</span>` +
    `<span class="em-chip ${m.hasKey === false ? 'bad' : ''}">` +
    `${m.hasKey === false ? '⚠ 没有可用密钥' : '密钥就绪'}</span>`;

  $('em-key').value = '';
  $('em-name').value = m.name || '';
  $('em-model').value = (m.route || '').includes('/') ? (m.route || '').split('/').slice(1).join('/') : '';
  // 「职责」字段已从界面移除 —— 组长名单会自动从提示词里摘一句。
  // 角色提示词是异步取回后填的，见 loadRolePromptIntoEdit()。
  $('em-avatar').value = m.avatar && !String(m.avatar).startsWith('avatars/') ? m.avatar : '';
  $('em-turns').value = m.max_turns_per_run || 2;
  $('em-temp').value = typeof m.temperature === 'number' ? m.temperature : 0.7;
  $('em-maxtok').value = m.max_tokens || 3000;

  $('em-msg').classList.add('hidden');
  $('edit-modal').classList.remove('hidden');
  setTimeout(() => $('em-key').focus(), 30);

  // 提示词单独拉（可能几 KB，不必塞进每次状态推送里）
  loadRolePromptIntoEdit(agentId);
}

/** 把成员当前的系统提示词读进编辑面板 */
async function loadRolePromptIntoEdit(agentId) {
  const box = $('em-prompt');
  if (box) box.value = '载入中…';
  try {
    const res = await fetch(`/api/member-role-content/${encodeURIComponent(agentId)}`);
    const data = await res.json().catch(() => ({}));
    if (editingId !== agentId) return;        // 用户已经切到别的成员了
    if (!res.ok || !data.ok) {
      if (box) box.value = '';
      showEditMsg(data.error || '提示词读取失败');
      return;
    }
    if (box) { box.value = data.text || ''; lastLoadedPrompt = (data.text || '').trim(); }
    const hint = document.querySelector('#edit-modal .field-hint');
    if (hint && data.roleFile) hint.dataset.file = data.roleFile;
  } catch (err) {
    if (box) box.value = '';
    showEditMsg('提示词读取失败：' + (err && err.message ? err.message : err));
  }
}

function closeEditMember() {
  $('edit-modal').classList.add('hidden');
  editingId = null;
}

function wireEditMember() {
  const modal = $('edit-modal');
  if (!modal || !modal.classList) return;
  $('em-close').addEventListener('click', closeEditMember);
  $('em-cancel').addEventListener('click', closeEditMember);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeEditMember(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeEditMember();
  });

  $('em-submit').addEventListener('click', async () => {
    const id = editingId;
    if (!id) return;
    // 只提交「改动过」的字段，避免把界面上的默认值误写进配置
    const payload = {};
    const key = ($('em-key').value || '').trim();
    if (key) payload.apiKey = key;
    const name = ($('em-name').value || '').trim();
    if (name) payload.name = name;
    const model = ($('em-model').value || '').trim();
    const currentModel = ((state.members || []).find((x) => x.id === id) || {}).route || '';
    if (model && !currentModel.endsWith('/' + model)) payload.model = model;
    const promptText = ($('em-prompt').value || '').trim();
    // 提示词整篇覆盖；和原值相同就不提交，省一次没意义的写盘
    if (promptText && promptText !== lastLoadedPrompt) payload.rolePrompt = promptText;
    const avatar = ($('em-avatar').value || '').trim();
    if (avatar) payload.avatar = avatar;
    const turns = Number($('em-turns').value);
    if (Number.isFinite(turns)) payload.maxTurnsPerRun = turns;
    const temp = Number($('em-temp').value);
    if (Number.isFinite(temp)) payload.temperature = temp;
    const maxtok = Number($('em-maxtok').value);
    if (Number.isFinite(maxtok)) payload.maxTokens = maxtok;

    if (!Object.keys(payload).length) {
      showEditMsg('没有要修改的内容');
      return;
    }
    $('em-submit').disabled = true;
    try {
      const res = await fetch(`/api/member/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        showEditMsg(data.error || `保存失败（HTTP ${res.status}）`);
        return;
      }
      const changed = (data.updated && data.updated.changed) || [];
      showEditMsg(`已保存：${changed.join('、')}`, true);
      presetsCache = null;
      setTimeout(closeEditMember, 900);
    } catch (err) {
      showEditMsg('请求失败：' + (err && err.message ? err.message : err));
    } finally {
      $('em-submit').disabled = false;
    }
  });
}

function showEditMsg(text, ok) {
  const box = $('em-msg');
  box.textContent = text;
  box.classList.remove('hidden');
  box.classList.toggle('ok', !!ok);
}

/* ------------------------------------------------------------ 对话设置 */
async function openSettings() {
  $('settings-modal').classList.remove('hidden');
  $('st-msg').classList.add('hidden');
  const host = $('st-fields');
  host.innerHTML = '<div class="st-loading">读取中…</div>';
  try {
    const res = await fetch('/api/limits');
    const data = await res.json();
    const lim = data.limits || {};
    host.innerHTML = '';
    Object.keys(lim).forEach((k) => {
      const item = lim[k];
      const row = el('label', 'field');
      row.innerHTML = `
        <span class="field-label">${esc(item.label)}</span>
        <div class="st-range">
          <input type="range" data-limit="${k}" min="${item.min}" max="${item.max}"
                 value="${item.value}" />
          <input type="number" data-limitnum="${k}" min="${item.min}" max="${item.max}"
                 value="${item.value}" />
        </div>
        <span class="field-hint">可填 ${item.min} ~ ${item.max}</span>`;
      host.appendChild(row);
      const range = row.querySelector('input[type=range]');
      const num = row.querySelector('input[type=number]');
      range.addEventListener('input', () => { num.value = range.value; });
      num.addEventListener('input', () => { range.value = num.value; });
    });
  } catch (err) {
    host.innerHTML = `<div class="st-loading">读取失败：${esc(String(err))}</div>`;
  }
}

function closeSettings() {
  $('settings-modal').classList.add('hidden');
}

function wireSettings() {
  const modal = $('settings-modal');
  if (!modal || !modal.classList) return;
  $('st-close').addEventListener('click', closeSettings);
  $('st-cancel').addEventListener('click', closeSettings);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeSettings(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeSettings();
  });

  $('st-submit').addEventListener('click', async () => {
    const body = {};
    document.querySelectorAll('#st-fields input[data-limitnum]').forEach((input) => {
      const value = Number(input.value);
      if (Number.isFinite(value)) body[input.dataset.limitnum] = value;
    });
    $('st-submit').disabled = true;
    try {
      const res = await fetch('/api/limits', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        showStMsg(data.error || `保存失败（HTTP ${res.status}）`);
        return;
      }
      showStMsg('已保存，立刻生效', true);
      renderLimitsChip(data.applied);
      setTimeout(closeSettings, 800);
    } catch (err) {
      showStMsg('请求失败：' + (err && err.message ? err.message : err));
    } finally {
      $('st-submit').disabled = false;
    }
  });
}

function showStMsg(text, ok) {
  const box = $('st-msg');
  box.textContent = text;
  box.classList.remove('hidden');
  box.classList.toggle('ok', !!ok);
}

/** 顶栏那行小字：把当前护栏写出来，改了立刻反映 */
function renderLimitsChip(applied) {
  // 2026-10-14：这里原来往 #chat-sub 写限额文字，把「人数」挤掉了。
  // 用户要的标题栏是「群名 + 人数」，所以限额改挂在 title 上（悬停可见），正文只留人数。
  if (!applied) return;
  const parts = [];
  if (applied.max_messages) parts.push(`单轮 ≤ ${applied.max_messages} 次发言`);
  if (applied.max_depth) parts.push(`@ 深度 ≤ ${applied.max_depth}`);
  if (applied.max_context_chars) parts.push(`上下文 ≤ ${applied.max_context_chars} 字`);
  const head = document.querySelector('.main-head');
  if (head && parts.length) head.title = '当前护栏：' + parts.join(' · ');
  renderHeadCount();
}

/* ==========================================================================
   2026-10-14 · 聊天框功能补全（需求 5）
   --------------------------------------------------------------------------
   补的是「一个聊天框该有的基本操作」：
     · 鼠标移到消息上 → 复制 / 引用 / 删除
     · 顶栏 ⌕ → 搜索消息（前端过滤，不改记录）
     · ⋯ → 导出聊天记录
   引用不依赖后端：把被引用的内容作为引用块插进输入框，模型自然能看到。
   ========================================================================== */

let hoveredMsgId = null;

function messageTextById(id) {
  const node = state.bubbles.get(id);
  if (!node) return '';
  const bubble = node.querySelector('.bubble');
  return bubble ? (bubble.dataset.raw || bubble.textContent || '') : '';
}

function senderNameById(id) {
  const node = state.bubbles.get(id);
  if (!node) return '';
  const nameEl = node.querySelector('.msg-name');
  return nameEl ? nameEl.textContent : '';
}

/** 鼠标悬停到消息上时，在它旁边浮出一排小按钮 */
function wireMessageActions() {
  const bar = el('div', 'msg-actions hidden');
  bar.id = 'msg-actions';
  bar.innerHTML = `
    <button data-act="copy" title="复制这条发言">复制</button>
    <button data-act="quote" title="引用到输入框">引用</button>
    <button data-act="del" title="从当前视图移除">删除</button>`;
  document.body.appendChild(bar);
  state.msgActions = bar;

  streamEl.addEventListener('mouseover', (e) => {
    const node = e.target.closest ? e.target.closest('.msg') : null;
    if (!node || !node.dataset.id) { hideMsgActions(); return; }
    hoveredMsgId = node.dataset.id;
    const r = node.getBoundingClientRect();
    bar.classList.remove('hidden');
    // 贴在消息右侧；贴近窗口右缘时改贴左侧
    const barW = 168;
    let left = r.right - barW - 6;
    if (left < 6) left = 6;
    bar.style.left = Math.round(left) + 'px';
    bar.style.top = Math.round(r.top + 2) + 'px';
  });

  streamEl.addEventListener('mouseleave', hideMsgActions);
  document.addEventListener('scroll', hideMsgActions, true);

  bar.addEventListener('click', async (e) => {
    const btn = e.target.closest ? e.target.closest('button[data-act]') : null;
    if (!btn || !hoveredMsgId) return;
    const id = hoveredMsgId;
    const act = btn.dataset.act;
    if (act === 'copy') await copyMessage(id);
    else if (act === 'quote') quoteMessage(id);
    else if (act === 'del') deleteMessage(id);
    hideMsgActions();
  });
}

function hideMsgActions() {
  if (state.msgActions) state.msgActions.classList.add('hidden');
}

async function copyMessage(id) {
  const text = messageTextById(id);
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    addNotice('已复制这条发言', 'info');
  } catch (err) {
    addNotice('复制失败：' + (err && err.message ? err.message : err), 'warn');
  }
}

/** 引用：把「谁说了什么」压缩成一行放进输入框，模型按普通文本即可读懂 */
function quoteMessage(id) {
  const who = senderNameById(id) || '成员';
  let text = messageTextById(id).replace(/\s+/g, ' ').trim();
  if (text.length > 120) text = text.slice(0, 120) + '…';
  const prefix = `↩ 引用 ${who}：「${text}」\n请针对上面这句回应。\n\n`;
  inputEl.value = prefix + (inputEl.value || '');
  inputEl.focus();
  inputEl.selectionStart = inputEl.selectionEnd = inputEl.value.length;
  updateSend();
}

/** 删除：只从当前视图移除（后端记录不动，刷新还会回来）。
 *  这样「看花眼了想清掉一条」是零风险的；要彻底清空请用 ⋯ → 清空聊天记录。 */
function deleteMessage(id) {
  const node = state.bubbles.get(id);
  if (!node) return;
  node.remove();
  state.bubbles.delete(id);
  addNotice('已从当前视图移除这条发言（后端记录未动，刷新会回来）', 'info');
}

/* ------------------------------------------------------------ 消息搜索 */
function wireSearch() {
  const bar = $('search-bar');
  const input = $('search-input');
  if (!bar || !bar.classList) return;

  function apply() {
    const q = (input.value || '').trim().toLowerCase();
    let hits = 0;
    state.bubbles.forEach((node) => {
      if (!q) { node.classList.remove('filtered-out'); return; }
      const text = (node.textContent || '').toLowerCase();
      const hit = text.includes(q);
      node.classList.toggle('filtered-out', !hit);
      if (hit) hits += 1;
    });
    $('search-count').textContent = q ? `${hits} 条匹配` : '';
  }

  $('search-btn').addEventListener('click', () => {
    bar.classList.toggle('hidden');
    if (!bar.classList.contains('hidden')) { input.focus(); apply(); }
    else { input.value = ''; apply(); }
  });
  $('search-close').addEventListener('click', () => {
    bar.classList.add('hidden');
    input.value = '';
    apply();
  });
  input.addEventListener('input', apply);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { $('search-close').click(); }
  });
}

/* ------------------------------------------------------------ 导出记录 */
function exportChat() {
  const rows = [];
  streamEl.querySelectorAll('.msg, .notice').forEach((node) => {
    if (node.classList.contains('notice')) {
      rows.push(`—— ${node.textContent.trim()} ——`);
      return;
    }
    const who = (node.querySelector('.msg-name') || {}).textContent || '';
    const bubble = node.querySelector('.bubble');
    const text = bubble ? (bubble.dataset.raw || bubble.textContent || '') : '';
    const foot = (node.querySelector('.msg-foot') || {}).textContent || '';
    if (!text.trim()) return;
    rows.push(`【${who}】${text.trim()}`);
    if (foot.trim()) rows.push(`      （${foot.trim()}）`);
    rows.push('');
  });
  if (!rows.length) { addNotice('还没有可导出的内容', 'warn'); return; }

  const stamp = new Date().toLocaleString('zh-CN');
  const head = `AI 工作群 · 聊天记录\n导出时间：${stamp}\n${'='.repeat(46)}\n\n`;
  const blob = new Blob([head + rows.join('\n')], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `AI工作群-${new Date().toISOString().slice(0, 10)}.txt`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  addNotice('已导出聊天记录', 'info');
}

/* ==========================================================================
   2026-10-14 · 群名 / 人数、emoji、图片与文件附件
   --------------------------------------------------------------------------
   · 群名：点标题就能改，存 localStorage（纯界面概念，不动后端）
   · 人数：跟在群名后面，跟着 state.members 走
   · emoji：面板点选插入光标处
   · 图片：选择文件 或 直接 Ctrl+V 粘贴截图 → 缩略图预览 → 随消息发出
           （图片会显示在聊天气泡里；**不会**发给模型，原因见后端说明）
   · 文件：选择文件 → 文本文件的内容会真的注入消息，模型读得到；其他类型只记文件名
   ========================================================================== */

const GROUP_NAME_KEY = 'aig-group-name';

// 文档类附件的原始大小上限。base64 会让体积涨约 33%，
// 再大会把 WebSocket 那一帧撑得很夸张，也拖慢整轮派发。
const docLimit = 6 * 1024 * 1024;
let pendingAttachments = [];

/* ------------------------------------------------------------ 群名 + 人数 */
function currentGroupName() {
  try {
    return localStorage.getItem(GROUP_NAME_KEY) || 'AI 工作群';
  } catch (e) {
    return 'AI 工作群';
  }
}

function applyGroupName() {
  const el = $('chat-title');
  if (el && el.textContent !== undefined) el.textContent = currentGroupName();
}

/** 点群名就地编辑：回车/失焦保存，Esc 取消 */
function wireGroupName() {
  const el = $('chat-title');
  if (!el || !el.addEventListener) return;
  applyGroupName();

  el.addEventListener('click', () => {
    if (el.isContentEditable) return;
    el.contentEditable = 'true';
    el.classList.add('editing');
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  });

  function commit(save) {
    if (!el.isContentEditable) return;
    el.contentEditable = 'false';
    el.classList.remove('editing');
    const next = (el.textContent || '').trim().replace(/\s+/g, ' ');
    if (save && next) {
      try { localStorage.setItem(GROUP_NAME_KEY, next.slice(0, 24)); } catch (e) { /* 忽略 */ }
    }
    applyGroupName();   // 无论存不存，都回到规范值
  }

  el.addEventListener('blur', () => commit(true));
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit(true); el.blur(); }
    if (e.key === 'Escape') { e.preventDefault(); commit(false); el.blur(); }
  });
}

/** 人数：(3) */
function renderHeadCount() {
  const n = (state.members || []).filter((m) => !m.hidden).length;
  const el = $('chat-sub');
  if (el && el.textContent !== undefined) el.textContent = `(${n})`;
}

/* ------------------------------------------------------------ emoji 面板 */
const EMOJI_SET = [
  '😀', '😄', '😅', '🤣', '😊', '🙂', '😉', '😍',
  '🤔', '🧐', '😐', '😴', '🙄', '😮', '😢', '😭',
  '😤', '😡', '🤯', '🥳', '👍', '👎', '👌', '🙏',
  '💪', '🤝', '👏', '🙌', '🔥', '✨', '⭐', '💡',
  '✅', '❌', '⚠️', '❓', '❗', '💯', '🎉', '🚀',
  '🐛', '🔧', '📌', '📎', '📝', '📊', '💻', '🧠',
];

function wireEmoji() {
  const pop = $('emoji-pop');
  if (!pop || !pop.classList) return;
  pop.innerHTML = EMOJI_SET.map((e) => `<button type="button">${e}</button>`).join('');

  $('btn-emoji').addEventListener('click', (e) => {
    e.stopPropagation();
    hideMentionPop();
    pop.classList.toggle('hidden');
    if (!pop.classList.contains('hidden')) {
      // 点面板外就收起
      setTimeout(() => {
        document.addEventListener('click', function once(ev) {
          if (!pop.contains(ev.target) && ev.target !== $('btn-emoji')) {
            pop.classList.add('hidden');
          }
          document.removeEventListener('click', once);
        });
      }, 0);
    }
  });

  pop.addEventListener('click', (e) => {
    const btn = e.target.closest ? e.target.closest('button') : null;
    if (!btn) return;
    insertAtCursor(btn.textContent || '');
  });
}

/** 在输入框光标处插入文本（没有焦点就追加到末尾） */
function insertAtCursor(text) {
  const value = inputEl.value || '';
  const start = typeof inputEl.selectionStart === 'number' ? inputEl.selectionStart : value.length;
  const end = typeof inputEl.selectionEnd === 'number' ? inputEl.selectionEnd : value.length;
  inputEl.value = value.slice(0, start) + text + value.slice(end);
  const pos = start + text.length;
  inputEl.focus();
  try {
    inputEl.selectionStart = inputEl.selectionEnd = pos;
  } catch (e) { /* 忽略 */ }
  updateSend();
}

/* ------------------------------------------------------------ 附件 */
function humanSize(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + ' B';
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
  return (v / 1024 / 1024).toFixed(1) + ' MB';
}

function renderAttachTray() {
  const tray = $('attach-tray');
  if (!tray || !tray.classList) return;
  if (!pendingAttachments.length) {
    tray.classList.add('hidden');
    tray.innerHTML = '';
    return;
  }
  tray.classList.remove('hidden');
  tray.innerHTML = '';
  pendingAttachments.forEach((item, index) => {
    const chip = el('div', 'attach-chip');
    if (item.kind === 'image' && item.dataUrl) {
      chip.innerHTML = `<img src="${esc(item.dataUrl)}" alt="" />`;
    } else {
      chip.innerHTML = `<span class="ac-icon">${item.kind === 'image' ? '🖼' : '📎'}</span>`;
    }
    chip.innerHTML += `<span class="ac-name">${esc(item.name)}</span>`
      + `<span class="ac-size">${humanSize(item.size)}</span>`;
    const x = el('button', 'ac-x', '✕');
    x.title = '移除这个附件';
    x.addEventListener('click', () => {
      pendingAttachments.splice(index, 1);
      renderAttachTray();
    });
    chip.appendChild(x);
    tray.appendChild(chip);
  });
}

function addImageFile(file) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      pendingAttachments.push({
        kind: 'image',
        name: file.name || '粘贴的图片.png',
        mime: file.type || 'image/png',
        size: file.size || 0,
        dataUrl: String(reader.result || ''),
      });
      renderAttachTray();
      resolve();
    };
    reader.onerror = () => resolve();
    reader.readAsDataURL(file);
  });
}

function addTextOrBinaryFile(file) {
  return new Promise((resolve) => {
    const isText = /^(text\/|application\/(json|xml|javascript|x-yaml|yaml))/.test(file.type || '')
      || /\.(txt|md|csv|json|xml|ya?ml|log|py|js|ts|tsx|jsx|html|css|java|go|rs|c|h|cpp|sql|sh|ps1|bat)$/i.test(file.name || '');
    if (!isText) {
      // 二进制 / Office 文件：**不能在前端当文本读** ——
      // .docx/.xlsx/.pptx 本质是 ZIP 包，readAsText 读出来是一团乱码。
      // 所以转成 base64 交给后端解析（见 group/documents.py，纯标准库）。
      if (file.size > docLimit) {
        window.alert(
          `「${file.name}」有 ${(file.size / 1048576).toFixed(1)} MB，`
          + `超过 ${(docLimit / 1048576).toFixed(0)} MB 上限。\n`
          + '请压缩一下，或者只把需要 AI 看的那部分复制成文本贴进来。');
        resolve();
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result || '');
        const b64 = dataUrl.includes(',') ? dataUrl.split(',')[1] : '';
        pendingAttachments.push({
          kind: 'file', name: file.name || '未命名', mime: file.type || '',
          size: file.size || 0, text: '', data: b64,
        });
        renderAttachTray();
        resolve();
      };
      reader.onerror = () => resolve();
      reader.readAsDataURL(file);
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      pendingAttachments.push({
        kind: 'file', name: file.name || '未命名', mime: file.type || 'text/plain',
        size: file.size || 0, text: String(reader.result || ''),
      });
      renderAttachTray();
      resolve();
    };
    reader.onerror = () => resolve();
    reader.readAsText(file, 'utf-8');
  });
}

function wireAttachments() {
  const imgInput = $('pick-image');
  const fileInput = $('pick-file');
  if (!imgInput || !imgInput.addEventListener) return;

  $('btn-image').addEventListener('click', () => imgInput.click());
  $('btn-file').addEventListener('click', () => fileInput.click());

  imgInput.addEventListener('change', async () => {
    for (const f of Array.from(imgInput.files || [])) await addImageFile(f);
    imgInput.value = '';
  });
  fileInput.addEventListener('change', async () => {
    for (const f of Array.from(fileInput.files || [])) await addTextOrBinaryFile(f);
    fileInput.value = '';
  });

  // Ctrl+V 直接粘贴截图 —— 这是 Windows 上最顺手的截图流程
  inputEl.addEventListener('paste', async (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    let handled = false;
    for (const it of Array.from(items)) {
      if (it.kind === 'file' && (it.type || '').startsWith('image/')) {
        const f = it.getAsFile();
        if (f) { await addImageFile(f); handled = true; }
      }
    }
    if (handled) {
      e.preventDefault();
      addNotice('已把剪贴板里的图片加为附件', 'info');
    }
  });
}

/** 附件气泡：图片显示缩略图，文件显示名称胶囊 */
function attachmentsHtml(msg) {
  const list = (msg && msg.attachments) || [];
  if (!list.length) return '';
  const parts = list.map((a) => {
    if (a.kind === 'image' && a.dataUrl) {
      return `<div class="msg-attach"><img src="${esc(a.dataUrl)}" alt="${esc(a.name)}" `
        + `onclick="window.open(this.src,'_blank')" title="点开看大图" /></div>`;
    }
    if (a.kind === 'image') {
      return `<div class="msg-attach file-only"><span>🖼</span>${esc(a.name)}</div>`;
    }
    return `<div class="msg-attach file-only"><span>📎</span>${esc(a.name)}`
      + (a.truncated ? '<i>（内容较长，已截断）</i>' : '') + '</div>';
  });
  return `<div class="msg-attach-row">${parts.join('')}</div>`;
}


/* ==========================================================================
   API Key 归属识别
   --------------------------------------------------------------------------
   粘贴 Key → 自动探测属于哪家 → 自动切好提供商 → 把该账号可用的模型填进下拉。
   格式判断不可靠（`sk-` 被一大批厂商共用），所以走真实探测。
   ========================================================================== */

let detectSeq = 0;

// 当前可选的模型列表（自定义下拉的数据源）
state.modelOptions = state.modelOptions || [];

function detecting(on, text) {
  const btn = $('am-detect');
  const msg = $('am-detect-msg');
  if (btn && btn.textContent !== undefined) {
    btn.disabled = !!on;
    btn.textContent = on ? '识别中…' : '识别';
  }
  if (msg && msg.classList) {
    if (text) { msg.textContent = text; msg.classList.remove('hidden'); }
    else { msg.classList.add('hidden'); }
  }
}

async function detectKey() {
  const input = $('am-key');
  const key = (input && input.value ? input.value : '').trim();
  if (!key) { detecting(false, '请先粘贴 API Key'); return; }

  // 自定义端点时把地址一起带上，就只探它一个
  const isCustom = ($('am-provider').value || '') === 'custom';
  const baseUrl = isCustom ? ($('am-baseurl').value || '').trim() : '';

  const seq = ++detectSeq;
  detecting(true, '正在探测…');
  try {
    const res = await fetch('/api/detect-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, baseUrl }),
    });
    const data = await res.json().catch(() => ({}));
    if (seq !== detectSeq) return;          // 又点了新的识别，丢弃这次结果
    if (!res.ok || !data.ok) {
      detecting(false, data.error || `探测失败（HTTP ${res.status}）`);
      return;
    }
    const hits = data.hits || [];
    if (!hits.length) {
      const why = (data.unclear || []).map((x) => x.label + '：' + x.detail).slice(0, 3).join('；');
      detecting(false, '没能识别出归属。' + (why ? '（' + why + '）' : '')
        + ' 可以直接手动选提供商。');
      return;
    }
    const hit = hits[0];
    const n = (hit.models || []).length;
    // 措辞必须准确：/models 给的是「端点列出的目录」，不是「你账号都能用」。
    // 千问的 /models 会返回 176 个平台目录，而账号可能只开通了其中一两个。
    let msg = `识别为「${hit.label}」。${hit.detail || ''}`;
    if (data.ambiguous) {
      msg = `有 ${hits.length} 家都接受这把 Key（${hits.map((x) => x.label).join('、')}），`
        + `已选「${hit.label}」，可手动切换。${hit.detail || ''}`;
    }
    if (n > 30) {
      msg += ' —— 这么多通常是平台全量目录，不代表你的账号都开通了，'
        + '挑中哪个就点「试一下」验证。';
    } else if (n) {
      msg += '，已填入模型列表。';
    }
    detecting(false, msg);
    applyDetectedProvider(hit);
  } catch (err) {
    if (seq === detectSeq) detecting(false, '请求失败：' + (err && err.message ? err.message : err));
  }
}

/** 把探测结果落到表单上：切提供商 + 刷新模型下拉 */
function applyDetectedProvider(hit) {
  const sel = $('am-provider');
  if (sel && sel.value !== hit.provider) {
    // 选项可能还没加载完（弹窗刚打开、presets 请求还在路上）。
    // 此时 `sel.value = 'zhipu'` 会**静默失败**，用户看到的提供商是空的 ——
    // 而且没有任何报错，很难查。所以缺了就直接补一个选项。
    const exists = Array.prototype.some.call(
      sel.options || [], (o) => o.value === hit.provider);
    if (!exists) {
      const opt = document.createElement('option');
      opt.value = hit.provider;
      opt.textContent = hit.label || hit.provider;
      sel.appendChild(opt);
    }
    sel.value = hit.provider;
    sel.dispatchEvent(new Event('change'));
  }
  setModelOptions(hit.models || []);
  // 模型：以**探测到的真实列表**为准。
  // 不能只在"为空"时才填 —— 切提供商后表单里往往残留着上一个提供商的模型 id
  //（见 syncProvider 那段注释），那样会留下一个用不了的值。
  const modelInput = $('am-model');
  const models = hit.models || [];
  // 默认值要挑「像对话模型的」—— 小米的列表里带 TTS/ASR，
  // 直接把第一个填进去很可能填到语音模型上，一试就报错。
  const preferred = (hit.chatModels && hit.chatModels.length) ? hit.chatModels : models;
  if (modelInput && models.length) {
    const current = (modelInput.value || '').trim();
    if (!models.includes(current)) modelInput.value = preferred[0] || models[0];
  }
}

function wireDetectKey() {
  const btn = $('am-detect');
  if (!btn || !btn.addEventListener) return;
  btn.addEventListener('click', detectKey);
  // 粘贴/输入后自动试一次：停 800ms 不再输入再发，避免边输边打请求
  let timer = null;
  $('am-key').addEventListener('input', () => {
    clearTimeout(timer);
    if (($('am-key').value || '').trim().length < 16) return;
    timer = setTimeout(detectKey, 800);
  });
}


/* ==========================================================================
   模型可用性验证（「试一下」）
   --------------------------------------------------------------------------
   /models 只能说明端点列出了这个模型，说明不了你的账号开通了它。
   唯一可靠的办法是真发一次请求 —— 这里只花 1 个 token。
   ========================================================================== */

async function testModelOnce() {
  const model = ($('am-model').value || '').trim();
  const provider = ($('am-provider').value || '').trim();
  const key = ($('am-key').value || '').trim();
  const baseUrl = provider === 'custom' ? ($('am-baseurl').value || '').trim() : '';
  const btn = $('am-test');

  if (!model) { showTestMsg('请先填模型 id'); return; }
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = '测试中…';
  showTestMsg('正在用「' + model + '」发一次最小请求…');
  try {
    const res = await fetch('/api/test-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // key 留空时后端会用配置里已保存的那把
      body: JSON.stringify({ provider, model, key, baseUrl }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      showTestMsg('✓ ' + model + ' 可以用', true);
    } else {
      showTestMsg('✗ ' + (data.error || '调用失败')
        + (data.status ? `（HTTP ${data.status}）` : ''), false, true);
    }
  } catch (err) {
    showTestMsg('请求失败：' + (err && err.message ? err.message : err));
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function showTestMsg(text, good, bad) {
  const msg = $('am-test-msg');
  if (!msg || !msg.classList) return;
  msg.textContent = text;
  msg.classList.remove('hidden');
  msg.classList.toggle('ok', !!good);
  msg.classList.toggle('bad', !!bad);
}

function wireTestModel() {
  const btn = $('am-test');
  if (!btn || !btn.addEventListener) return;
  btn.addEventListener('click', testModelOnce);
}


/* ==========================================================================
   批量实测（「测一遍」）
   --------------------------------------------------------------------------
   实际困境：平台「免费额度」页和 /models 都列一大堆，真去调用却 403
   Access to model denied。所以把候选模型各发一次 1-token 请求，
   **只把真能用的留在下拉里**，用户就不会再选到一个必然报错的模型。
   ========================================================================== */

async function testAllModels() {
  const provider = ($('am-provider').value || '').trim();
  const key = ($('am-key').value || '').trim();
  const baseUrl = provider === 'custom' ? ($('am-baseurl').value || '').trim() : '';
  const btn = $('am-test-all');

  // 候选就是当前下拉里的模型 —— 也就是识别出来的那份目录
  const all = modelOptions();
  // 先剔掉明显不是对话模型的，省掉一大半无用请求
  const nonChat = /(tts|asr|voice|speech|embed|rerank|whisper|image|ocr|mt-|video|wan)/i;
  const candidates = all.filter((m) => !nonChat.test(m)).slice(0, 60);

  if (!candidates.length) { showTestMsg('没有可测的模型（先把上面的「识别」跑一遍）'); return; }

  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = '测试中…';
  showTestMsg(`正在并发实测 ${candidates.length} 个模型（每个 1 token）…`);
  try {
    const res = await fetch('/api/test-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, models: candidates, key, baseUrl }),
    });
    const data = await res.json().catch(() => ({}));
    if (!data.ok) { showTestMsg(data.error || '批量测试失败', false, true); return; }

    const usable = data.usable || [];
    if (!usable.length) {
      showTestMsg(`测了 ${data.tested} 个，没有一个能用。`
        + '多半是模型没在平台开通，或者这把 Key 不属于这个账号。', false, true);
      return;
    }
    // 下拉只留能用的
    const modelInput = $('am-model');
    const keep = usable.includes((modelInput.value || '').trim()) ? modelInput.value : usable[0];
    setModelOptions(usable, keep);
    showTestMsg(`测了 ${data.tested} 个，${usable.length} 个可用（下拉已只留这几个）：`
      + usable.slice(0, 6).join('、') + (usable.length > 6 ? ' …' : ''), true);
  } catch (err) {
    showTestMsg('请求失败：' + (err && err.message ? err.message : err));
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function wireTestAll() {
  const btn = $('am-test-all');
  if (!btn || !btn.addEventListener) return;
  btn.addEventListener('click', testAllModels);
}


/* ==========================================================================
   模型下拉（自定义）
   --------------------------------------------------------------------------
   为什么不用原生 <datalist>：它**会按输入框里已有的文字过滤选项**。
   「测一遍」跑完框里已经写着某个模型名，点开下拉就只剩匹配那一个 ——
   用户看到 31 个可用却只弹出 1 个，像是没测出来。
   自定义面板点开时**永远先给全量**，输入了才过滤。
   ========================================================================== */

function modelOptions() {
  // 一切以这个数组为准，HTML 里的面板只是它的渲染结果
  return state.modelOptions || [];
}

function renderModelMenu(filter) {
  const menu = $('am-model-menu');
  if (!menu || !menu.innerHTML === undefined) return;
  const all = modelOptions();
  const q = (filter || '').trim().toLowerCase();
  const hits = q ? all.filter((m) => m.toLowerCase().includes(q)) : all;

  if (!all.length) {
    menu.innerHTML = '<div class="mm-empty">还没有模型列表 —— 先点「识别」跑一遍</div>';
    return;
  }
  if (!hits.length) {
    menu.innerHTML = `<div class="mm-empty">没有匹配「${esc(filter)}」的模型</div>`;
    return;
  }
  const current = ($('am-model').value || '').trim();
  menu.innerHTML =
    `<div class="mm-head">共 ${all.length} 个${q ? `，匹配 ${hits.length} 个` : ''}</div>`
    + hits.map((m) =>
      `<div class="mm-item${m === current ? ' active' : ''}" data-model="${esc(m)}">${esc(m)}</div>`
    ).join('');
}

function openModelMenu() {
  const menu = $('am-model-menu');
  if (!menu) return;
  // 点开时**一律不带过滤词**。
  // 如果拿输入框里已有的值当过滤词，31 个候选会被过滤成 1 个 ——
  // 那正是原生 datalist 的毛病，不能在这儿再犯一次。
  // 只有用户真的在打字（input 事件）时才过滤。
  renderModelMenu('');
  menu.classList.remove('hidden');
}

function closeModelMenu() {
  const menu = $('am-model-menu');
  if (menu) menu.classList.add('hidden');
}

/** 设置候选模型列表（识别、测一遍、切提供商时都走这里） */
function setModelOptions(list, value) {
  state.modelOptions = Array.isArray(list) ? list.slice() : [];
  const input = $('am-model');
  if (input && typeof value === 'string') input.value = value;
  if (!$('am-model-menu').classList.contains('hidden')) openModelMenu();
}

function wireModelMenu() {
  const input = $('am-model');
  const menu = $('am-model-menu');
  if (!input || !input.addEventListener) return;

  // 点输入框 → 展开全量（不是过滤后的残列表）
  input.addEventListener('focus', openModelMenu);
  input.addEventListener('click', openModelMenu);
  // 手动打字才过滤
  input.addEventListener('input', () => {
    renderModelMenu(input.value);
    menu.classList.remove('hidden');
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModelMenu();
    // 回车选中第一个匹配项，省得用鼠标
    if (e.key === 'Enter' && !menu.classList.contains('hidden')) {
      const first = menu.querySelector('.mm-item');
      if (first) {
        e.preventDefault();
        input.value = first.dataset.model;
        closeModelMenu();
        showTestMsg('已选择：' + input.value);
      }
    }
  });

  menu.addEventListener('mousedown', (e) => {
    // mousedown 而不是 click：click 会先触发 input 的 blur，面板已经关了
    const item = e.target.closest ? e.target.closest('.mm-item') : null;
    if (!item) return;
    e.preventDefault();
    input.value = item.dataset.model;
    closeModelMenu();
    showTestMsg('已选择：' + input.value);
  });

  document.addEventListener('click', (e) => {
    if (!menu.contains(e.target) && e.target !== input) closeModelMenu();
  });
}

/* 放在最后：新追加的代码里有 const 声明，必须等它们初始化完再跑 init()，
   否则会撞上暂时性死区（Cannot access before initialization），整个初始化就断了。 */
init();
