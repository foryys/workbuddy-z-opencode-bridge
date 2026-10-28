#!/usr/bin/env node
/**
 * OpenCode Go → WorkBuddy 桥接代理 v4
 *
 * v3 → v4 的唯一改动：出站走本地代理（VPN）。
 *
 * 为什么必须改：Node 的 http.request 不读系统代理设置，v3 一直直连 opencode.ai。
 * 实测同一条线路——
 *   直连（国内宽带 → Cloudflare）：ttfb 0.78 / 1.65 / 2.44s，TLS 抖动到 2.1s
 *   经本地代理(127.0.0.1:7890 → 境外中转)：ttfb 0.77 / 0.76 / 0.90s
 * 直连的抖动正是「并发两个任务就报网络错误」「长任务经常断」的来源。
 *
 * v4 用 HTTP CONNECT 把上游连接穿透到本地代理，并在 Agent 层做 keep-alive 复用，
 * 所以不会每个请求都重开隧道。
 *
 * 环境变量（新增的在前）：
 *   PROXY_URL           出站代理，默认 http://127.0.0.1:7890；填 direct 则直连
 *   PORT                默认 8787
 *   UPSTREAM_BASE       默认 https://opencode.ai/inference/go
 *   OPENCODE_SESSION    默认每次启动随机 UUID
 *   PROXY_RETRIES       连接阶段失败重试次数，默认 3
 *   CONNECT_TIMEOUT_MS  连接建立超时，默认 30000
 *   IDLE_TIMEOUT_MS     上游静默超时，默认 0（不限制）
 *   KEEPALIVE_TIMEOUT_MS 客户端空闲连接保留时长，默认 0（不主动关闭）
 */

import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import net from 'node:net';
import { Agent as HttpAgent } from 'node:http';
import { Agent as HttpsAgent } from 'node:https';
import { randomUUID, createHash } from 'node:crypto';
import { URL, fileURLToPath } from 'node:url';
import { Transform } from 'node:stream';
import zlib from 'node:zlib';
import path from 'node:path';
import os from 'node:os';
import { createWriteStream, writeFileSync, readFileSync, statSync, renameSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

// ── Windows 控制台代码页 ─────────────────────────────────────────────────────
// 新开的 cmd 窗口默认 936，而本程序所有中文都是 UTF-8 字节，不切代码页必然花屏。
// 启动即切 65001，退出时（含异常退出）还原成进入前的值——调用方 service.cmd 是 GBK
// 编码的批处理，若不还原，它后面那些 echo 会全部乱掉。
const CONSOLE_CP = (() => {
  if (process.platform !== 'win32') return null;
  const read = () => {
    try {
      const out = String(execFileSync('cmd', ['/c', 'chcp'], { encoding: 'latin1', windowsHide: true, timeout: 3000 }));
      const m = out.match(/(\d{3,5})/);
      return m ? m[1] : '936';
    } catch { return '936'; }
  };
  const set = (cp) => {
    try { execFileSync('cmd', ['/c', 'chcp', cp], { stdio: 'ignore', windowsHide: true, timeout: 3000 }); } catch { /* 无控制台时失败无妨 */ }
  };
  const before = read();
  if (before !== '65001') set('65001');
  return { before, restore: () => set(before) };
})();
if (CONSOLE_CP) process.on('exit', () => CONSOLE_CP.restore());

// ── 一次性状态查询：`node opencode-go-proxy.mjs status` ───────────────────────
// 网页看板已砍（2026-10-28），状态只在终端看。这条命令不启动服务、不碰日志文件，
// 只去问正在跑的那个实例要 /status，把答案画成一屏就退出。双击桌面那个 cmd 走的就是它。
if (process.argv[2] === 'status') {
  const port = Number(process.env.PORT || 8787);
  const S = { reset: '\x1b[0m', dim: '\x1b[2m', bold: '\x1b[1m', green: '\x1b[32m', red: '\x1b[31m', yellow: '\x1b[33m', cyan: '\x1b[36m', gray: '\x1b[90m', bgBlue: '\x1b[44m', white: '\x1b[97m' };
  const dw = (s) => { let w = 0; for (const ch of s) w += /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(ch) ? 2 : 1; return w; };
  const padTo = (s, n) => s + ' '.repeat(Math.max(0, n - dw(s)));
  const cut = (s, n) => { if (dw(s) <= n) return s; let o = '', w = 0; for (const ch of s) { const c = dw(ch); if (w + c > n - 1) break; o += ch; w += c; } return o + '…'; };
  const hhmmss = (t) => { const d = new Date(t), p = (n) => (n < 10 ? '0' : '') + n; return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()); };
  let s;
  try {
    const r = await fetch(`http://127.0.0.1:${port}/status`, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    s = await r.json();
  } catch (e) {
    console.log(`${S.bgBlue}${S.white}${S.bold} OpenCode Go 启动器 ${S.reset} ${S.red}${S.bold}● 没在跑${S.reset}`);
    console.log(`${S.gray}${'─'.repeat(72)}${S.reset}`);
    console.log(` 端口 ${port} 上没有应答（${e.message}）。客户端里那条模型现在发不出去。`);
    console.log(` 双击桌面「OpenCode Go 启动器」（托盘图标）就会把它拉起来。`);
    process.exit(1);
  }
  const now = Date.now(), lastAt = s.lastAt || 0, idle = lastAt ? now - lastAt : Infinity;
  const lastEv = s.recent && s.recent[0];
  const lastBad = lastEv && !(lastEv.status >= 200 && lastEv.status < 300) && !lastEv.aborted;
  const act = s.active || [];
  let dot, txt;
  if (act.length) { dot = S.green + S.bold; txt = '● 运行中'; }
  else if (!lastAt) { dot = S.gray; txt = '● 等待请求'; }
  else if (lastBad) { dot = S.red + S.bold; txt = '● 最近一次失败'; }
  else if (idle < 95000) { dot = S.green + S.bold; txt = '● 运行中'; }
  else { dot = S.gray; txt = '● 空闲'; }
  const brand = `${S.bgBlue}${S.white}${S.bold} OpenCode Go 启动器 ${S.reset} ${S.cyan}${S.bold}v${s.version}${S.reset}`;
  const W = 72;
  console.log(brand + ' '.repeat(Math.max(1, W - dw(' OpenCode Go 启动器 ') - dw('v' + s.version) - dw(txt))) + dot + txt + S.reset);
  console.log(S.gray + '─'.repeat(W) + S.reset);
  const ob = s.outbound === 'direct' ? '直连' : '经代理 ' + s.proxy;
  console.log(` 出站     ${s.outbound === 'direct' ? S.yellow : S.green}${ob}${S.reset}` + ' '.repeat(Math.max(1, 30 - dw(ob))) + ` 成功 ${S.green}${s.ok}${S.reset} / ${s.total}   失败 ${s.fail ? S.red + s.fail : '0'}${S.reset}`);
  const upPlain = `${Math.round(s.uptimeMs / 60000)} 分钟`;
  console.log(` 已运行   ${upPlain}` + ' '.repeat(Math.max(1, 30 - dw(upPlain))) + ` 最近请求 ${lastAt ? Math.round(idle / 1000) + ' 秒前' : '还没收到'}   并发 ${act.length}`);
  if (act.length) {
    console.log(` 进行中   ` + act.slice(0, 3).map((a) => {
      const sec = Math.round(a.elapsedMs / 1000);
      const col = sec > 300 ? S.red + S.bold : (sec > 120 ? S.yellow : S.green);
      return `${S.cyan}${cut(a.label || a.sid, 22)}${S.reset} ${col}${sec} 秒${sec > 300 ? ' 可能卡住' : ''}${S.reset}`;
    }).join(S.gray + '  ·  ' + S.reset));
  }
  console.log(S.gray + '─'.repeat(W) + S.reset);
  console.log(S.gray + '  ' + padTo('任务', 24) + padTo('时间', 10) + padTo('耗时', 10) + '结果' + S.reset);
  for (const e of (s.recent || []).slice(0, 12)) {
    const good = e.status >= 200 && e.status < 300;
    const res = good ? '成功' : (e.aborted ? '中断' : '失败');
    const bits = [];
    if (e.status) bits.push('HTTP ' + e.status);
    if (e.injected) bits.push('补思考 ' + e.injected);
    if (e.why && !e.aborted) bits.push(e.why);
    if (e.aborted) bits.push('客户端断开');
    console.log('  ' + S.cyan + padTo(cut(e.label || e.session || '', 24), 24) + S.reset
      + S.gray + padTo(hhmmss(e.at), 10) + S.reset
      + padTo((e.ms / 1000).toFixed(1) + ' 秒', 10)
      + (good ? S.green : (e.aborted ? S.yellow : S.red)) + res + S.reset + '  '
      + S.gray + cut(bits.join('  '), 20) + S.reset);
  }
  if (!(s.recent || []).length) console.log(S.gray + '  还没有请求。在 WorkBuddy 里发一条消息试试。' + S.reset);
  console.log(S.gray + '─'.repeat(W) + S.reset);
  console.log(S.gray + ` 日志：proxy.log　·　状态源：http://127.0.0.1:${port}/status` + S.reset);
  process.exit(0);
}

// 日志同时落盘：启动器窗口一关，屏幕上的一切就没了，出问题只能靠猜。
// 位置默认是同目录 proxy.log，超过 5MB 在下次启动时改名留一份。
const LOG_PATH = process.env.PROXY_LOG || path.join(process.cwd(), 'proxy.log');
try { if (statSync(LOG_PATH).size > 5 * 1024 * 1024) renameSync(LOG_PATH, LOG_PATH + '.1'); } catch { /* 还没有旧日志 */ }
const logStream = createWriteStream(LOG_PATH, { flags: 'a' });
const _log = console.log.bind(console);
const _err = console.error.bind(console);
// 终端界面：占住整个窗口自己画。设 TUI=0 退回原来的滚动日志。
const TUI_MODE = process.env.TUI === 'force' || (process.stdin.isTTY && process.stdout.isTTY && process.env.TUI !== '0');
const C = {
  reset: '\x1b[0m', dim: '\x1b[2m', bold: '\x1b[1m',
  green: '\x1b[32m', red: '\x1b[31m', yellow: '\x1b[33m', cyan: '\x1b[36m', gray: '\x1b[90m',
  bgBlue: '\x1b[44m', white: '\x1b[97m',
};
const emit = (w, args) => {
  const line = args.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' ');
  if (!TUI_MODE) w(line);          // 终端界面模式下不往屏幕上打，日志改由看板区域显示
  try { logStream.write(`[${new Date().toISOString()}] ${line}\n`); } catch { /* 写盘失败不影响转发 */ }
};
console.log = (...a) => emit(_log, a);
console.error = (...a) => emit(_err, a);

const PORT = Number(process.env.PORT || 8787);
const HOST = '127.0.0.1';

// 同目录的 config.json 是控制台（tui.py）写的，环境变量优先。
// 这里只读两个不敏感字段：上游地址、本地令牌。真钥匙是 DPAPI 密文，
// 只有 Python 那边解得开，靠环境变量传进来。
const CFG = (() => {
  try {
    const dir = path.dirname(fileURLToPath(import.meta.url));
    return JSON.parse(readFileSync(path.join(dir, 'config.json'), 'utf8'));
  } catch { return {}; }
})();
const UPSTREAM = (process.env.UPSTREAM_BASE || CFG.upstream || 'https://opencode.ai/inference/go').replace(/\/+$/, '');
const ENV_KEY = process.env.OPENCODE_API_KEY || '';
// 客户端填的"本地令牌"：代理收到它就换成真钥匙，真钥匙因此不必写进客户端配置。
const LOCAL_TOKEN = (process.env.PROXY_TOKEN || CFG.local_key || '').trim();
const RETRIES = Number(process.env.PROXY_RETRIES ?? 3);
const CONNECT_TIMEOUT = Number(process.env.CONNECT_TIMEOUT_MS || 30000);
const IDLE_TIMEOUT = Number(process.env.IDLE_TIMEOUT_MS || 0);
// 请求已发出、但上游一个字节都不回时的上限。跨境直连偶尔会被静默丢弃（TCP 还在、
// 对端不说话），这种连接永远不会自己好，只能到点掐掉，让客户端看到失败后重发。
// 实测成功的长请求最长 252 秒，所以留到 7 分钟。
const FIRST_BYTE_TIMEOUT = Number(process.env.FIRST_BYTE_TIMEOUT_MS || 420000);
// 请求体大（历史厚的会话能到 2–3MB）时，跨境直连上传容易被丢在中途。
// 开 GZIP=1 就把请求体压一下再发，通常能小到十分之一。上游不认会报 400，关掉即可。
const GZIP = process.env.GZIP === '1';
const KEEPALIVE_TIMEOUT = Number(process.env.KEEPALIVE_TIMEOUT_MS || 0);

const RAW_PROXY = (process.env.PROXY_URL || 'http://127.0.0.1:7890').trim();
// 直连模式下，TLS 握手超过预算就当作"这条路抖了"，掐掉换一次。
// 实测直连 opencode.ai 的握手会在 0.5s 和 11.8s 之间乱跳，重试通常能换到快的那条。
// 预算按尝试次数递增，最后一次不再掐：只抢一次快路，抢不到就老实等，
// 免得"本来就慢但能成"的请求被反复掐成失败。
const TLS_BUDGETS = (process.env.TLS_BUDGET_MS || '5000,15000,0')
  .split(',').map((x) => Number(x.trim())).filter((x) => Number.isFinite(x));
let PROXY = null;
if (RAW_PROXY && RAW_PROXY.toLowerCase() !== 'direct') {
  try {
    const u = new URL(RAW_PROXY.includes('://') ? RAW_PROXY : 'http://' + RAW_PROXY);
    PROXY = { host: u.hostname, port: Number(u.port || 7890) };
  } catch { PROXY = null; }
}
// 启动探测后可能翻成 true（代理端口没开时不用隧道，直接走直连）
let OUTBOUND_DIRECT = !PROXY;

const DEFAULT_SESSION = process.env.OPENCODE_SESSION || randomUUID();

// ——— 会话标题 ———
// 客户端每个请求都带 x-conversation-id（就是本地会话库的主键），拿它查 workbuddy.db
// 就能显示和客户端一模一样的标题，不用再从正文里猜。
const WB_DB = path.join(os.homedir(), '.workbuddy', 'workbuddy.db');
let wbDb = null;
try {
  const { DatabaseSync } = await import('node:sqlite');
  wbDb = new DatabaseSync(WB_DB, { readOnly: true });
} catch (e) {
  console.log('[title] 打不开会话库，任务名继续按正文猜：' + e.message);
}
const titleCache = new Map();
function titleFor(convId) {
  if (!convId || !wbDb) return '';
  if (titleCache.has(convId)) return titleCache.get(convId);
  let t = '';
  try {
    const row = wbDb.prepare('SELECT title, custom_title FROM sessions WHERE id = ?').get(convId);
    t = (row && (row.custom_title || row.title)) || '';
  } catch { t = ''; }
  titleCache.set(convId, t);
  return t;
}

const HOP_BY_HOP = new Set([
  'host', 'connection', 'content-length', 'keep-alive', 'transfer-encoding',
  'upgrade', 'proxy-authorization', 'proxy-connection', 'te', 'trailer',
]);

const RETRYABLE = new Set([
  'ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'EPIPE', 'EHOSTUNREACH',
  'ENETUNREACH', 'ENOTFOUND', 'EAI_AGAIN', 'ERR_SOCKET_CONNECTION_TIMEOUT',
  'ERR_TLS_HANDSHAKE_TIMEOUT', 'socket hang up', 'proxy connect failed',
  'tls handshake too slow',
]);
const RETRYABLE_STATUS = new Set([429, 502, 503, 504]);

let fallbackLogged = false;
function logFallback(msg) {
  if (fallbackLogged) return;
  fallbackLogged = true;
  console.log(`[warn] 出站代理不可用，已回退直连：${msg}`);
  console.log('[warn] 直连时跨境抖动会明显变大；需要走 VPN 请确认代理端口（默认 7890）已开启。');
}

// 经本地 HTTP 代理建立 CONNECT 隧道，返回裸 socket
function openTunnel(targetHost, targetPort, timeout) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: PROXY.host,
      port: PROXY.port,
      method: 'CONNECT',
      path: `${targetHost}:${targetPort}`,
      headers: { host: `${targetHost}:${targetPort}` },
      timeout,
    });
    req.on('connect', (res, socket) => {
      if (res.statusCode !== 200) {
        socket.destroy();
        return reject(new Error(`proxy connect failed: HTTP ${res.statusCode}`));
      }
      resolve(socket);
    });
    req.on('timeout', () => req.destroy(new Error('proxy connect failed: timeout')));
    req.on('error', (e) => reject(new Error(`proxy connect failed: ${e.code || e.message}`)));
    req.end();
  });
}

// 自定义 Agent：每个上游连接先穿隧道，再在隧道上做 TLS；Agent 自己负责 keep-alive 复用
class TunnelHttpsAgent extends HttpsAgent {
  createConnection(options, cb) {
    const targetHost = options.host;
    const targetPort = Number(options.port || 443);
    (async () => {
      const raw = await openTunnel(targetHost, targetPort, CONNECT_TIMEOUT);
      const sock = tls.connect({ socket: raw, servername: targetHost });
      await new Promise((res, rej) => {
        sock.once('secureConnect', res);
        sock.once('error', rej);
      });
      return sock;
    })().then(
      (sock) => cb(null, sock),
      (e) => {
        logFallback(e.message);
        // 回退直连：交给父类自己做 TLS
        try { super.createConnection(options, cb); } catch (e2) { cb(e2, null); }
      }
    );
  }
}

// 出站方式在启动时定一次：探一下本地代理端口，通就走隧道，不通就直接走直连。
// 这样"忘了先开 VPN"不会变成每个连接都白试一次隧道再回退。
let AGENTS = null;
function buildAgents(useProxy) {
  AGENTS = useProxy
    ? { 'http:': new HttpAgent({ keepAlive: true, maxSockets: 64 }), 'https:': new TunnelHttpsAgent({ keepAlive: true, maxSockets: 32 }) }
    : { 'http:': new HttpAgent({ keepAlive: true, maxSockets: 64 }), 'https:': new HttpsAgent({ keepAlive: true, maxSockets: 64 }) };
}

function probeProxy(host, port, timeoutMs) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    const finish = (ok) => { try { sock.destroy(); } catch {} resolve(ok); };
    sock.setTimeout(timeoutMs);
    sock.once('connect', () => finish(true));
    sock.once('timeout', () => finish(false));
    sock.once('error', () => finish(false));
    sock.connect(port, host);
  });
}

function buildHeaders(incoming) {
  const out = {};
  for (const [k, v] of Object.entries(incoming)) {
    if (k.startsWith('__')) continue;               // 内部标记，绝不外发
    if (!HOP_BY_HOP.has(k.toLowerCase())) out[k] = v;
  }
  const auth = out['authorization'] || '';
  if (LOCAL_TOKEN && auth === `Bearer ${LOCAL_TOKEN}`) {
    // 客户端拿的是本地令牌，不是真钥匙——在这里换掉。
    // 真钥匙因此只存在于这个进程的内存和加密配置文件里。
    if (ENV_KEY) out['authorization'] = `Bearer ${ENV_KEY}`;
  } else if (!auth && ENV_KEY) {
    out['authorization'] = `Bearer ${ENV_KEY}`;
  }
  if (incoming.__gzip) out['content-encoding'] = 'gzip';
  out['x-opencode-session'] = sessionFor(incoming);
  out['user-agent'] = 'workbuddy-opencode-go-proxy/4.9';
  out['accept-encoding'] = 'identity';
  delete out['x-wb-session'];
  return out;
}

// 会话 ID：优先用客户端显式指定的；否则按「首条用户消息」派生。
// 多个任务并行时若共用一个 session，上游可能把请求串行化或拒绝；
// 按会话首条消息派生可让不同任务互不干扰，同一任务的多轮仍命中同一 session。
function sessionFor(incoming) {
  if (incoming['x-wb-session']) return incoming['x-wb-session'];
  return incoming.__wbSession || DEFAULT_SESSION;
}

// 把各种形状的 content 归一成纯文本：字符串、块数组（{text}/{content}）、对象。
// 不做这一步，content 是数组时 String() 会得到 "[object Object]"，
// 于是所有会话派生出同一个 session —— 多个对话就会挤在一条上游会话上。
function textOf(c) {
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    return c.map((b) => (typeof b === 'string' ? b : (b && (b.text ?? b.content)) || '')).join('');
  }
  if (c && typeof c === 'object') return String(c.text ?? c.content ?? '');
  return '';
}

function deriveSessionInfo(body) {
  const out = { session: null, label: '' };
  if (!body || !body.length) return out;
  try {
    const j = JSON.parse(body.toString('utf8'));
    const msgs = Array.isArray(j.messages) ? j.messages : null;
    if (!msgs || !msgs.length) return out;
    const first = msgs.find((m) => m.role === 'user') || msgs[0];
    // 用全文，不截断：截 2000 字符时，两段开头相同的模板会撞成一条会话。
    const seed = textOf(first && first.content);
    if (!seed) return out;
    const h = createHash('sha256').update(seed).digest('hex').slice(0, 32);
    out.session = `${h.slice(0, 8)}-${h.slice(8, 12)}-4${h.slice(13, 16)}-a${h.slice(17, 20)}-${h.slice(20, 32)}`;
    // 标签：三级取法，越靠前越可信。
    // 这些会话的历史被客户端压过之后，<user_query> 标签往往不在了，只靠它取不到。
    const clean = (t) => t.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    const looksHuman = (v) => {
      if (!v || v.length < 6 || v.length > 40) return false;
      if (/call_|You MUST|^<\/?[a-z]|^\{|^\[/i.test(v)) return false;
      const cn = (v.match(/[\u4e00-\u9fa5]/g) || []).length;
      return cn / v.length >= 0.5;      // 一半以上是汉字才算"人话"
    };
    let label = '';
    // 1) 会话起名的格式：20260101|示例|说明版改版
    for (const m of msgs) {
      if (!m) continue;
      const hit = clean(textOf(m.content)).match(/\d{8}\|[^\s|]{1,8}\|[^\s|]{1,12}/);
      if (hit) { label = hit[0]; break; }
    }
    // 2) 指令标签
    if (!label) {
      for (const m of msgs) {
        if (!m || m.role !== 'user') continue;
        const mm = textOf(m.content).match(/<user_query>([\s\S]*?)<\/user_query>/);
        if (mm) { const v = clean(mm[1]); if (looksHuman(v)) { label = v; break; } }
      }
    }
    // 3) 任何一句像人话的中文
    if (!label) {
      for (const m of msgs) {
        if (!m || m.role !== 'user') continue;
        for (const seg of clean(textOf(m.content)).split(/[。！？；\n]/)) {
          const v = seg.trim();
          if (looksHuman(v)) { label = v; break; }
        }
        if (label) break;
      }
    }
    out.label = label ? label.slice(0, 26) : '';
  } catch { /* 解析失败就用默认身份 */ }
  return out;
}

// ——— 思考内容回填 ———
// DeepSeek 思考模式要求：历史里每个 assistant 消息都要带上当初的 reasoning_content。
// 客户端把这条模型当"通用自定义模型"（vendor=Custom），回传时不带，上游就时不时 400。
// 代理这一层能补：上游吐出来的 reasoning_content 按"回答正文"存档；存档里没有的，
// 塞一句占位——上游只校验这个字段在不在，不校验内容真伪，缺一条就整条请求被拒。
const reasoningByText = new Map();
const REASONING_CAP = 2000;
const HASH_CACHE_FILE = path.join(process.cwd(), 'reasoning-cache.json');
const hashText = (t) => createHash('sha256').update(t).digest('hex').slice(0, 32);

// 存档要能跨重启：长任务往往跨天接着跑
try {
  const saved = JSON.parse(readFileSync(HASH_CACHE_FILE, 'utf8'));
  if (saved && typeof saved === 'object') {
    for (const [k, v] of Object.entries(saved)) if (typeof v === 'string') reasoningByText.set(k, v);
  }
} catch { /* 第一次跑没有存档 */ }

let saveTimer = null;
function flushCache() {
  if (!reasoningByText.size) return;
  try {
    const tmp = HASH_CACHE_FILE + '.tmp';
    writeFileSync(tmp, JSON.stringify(Object.fromEntries(reasoningByText)), 'utf8');
    renameSync(tmp, HASH_CACHE_FILE);
  } catch (e) { console.log('[cache] 存档失败：' + e.message); }
}
function scheduleSave() {
  if (saveTimer) return;
  saveTimer = setTimeout(() => { saveTimer = null; flushCache(); }, 500);
}
process.on('exit', flushCache);
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => { flushCache(); process.exit(0); });
}

function remember(text, reasoning) {
  if (!text || !reasoning) return;
  const k = hashText(text);
  if (reasoningByText.get(k) === reasoning) return;
  reasoningByText.delete(k);
  reasoningByText.set(k, reasoning);
  if (reasoningByText.size > REASONING_CAP) {
    const oldest = reasoningByText.keys().next().value;
    reasoningByText.delete(oldest);
  }
  scheduleSave();
}

// 从上游回包里捡 reasoning_content：SSE 流和普通 JSON 都认
// 为什么有的响应存不下思考内容？打三行样本就够了。
let harvestProbe = 0;

function harvest(bufs, status) {
  if (status < 200 || status >= 300) return;
  const raw = Buffer.concat(bufs).toString('utf8');
  if (!raw) return;
  let content = '', reasoning = '';
  if (raw.trimStart().startsWith('data:')) {
    for (const line of raw.split('\n')) {
      const t = line.trim();
      if (!t.startsWith('data:')) continue;
      const pay = t.slice(5).trim();
      if (!pay || pay === '[DONE]') continue;
      try {
        const j = JSON.parse(pay);
        const c0 = j.choices && j.choices[0];
        const d = (c0 && (c0.delta || c0.message)) || {};
        if (typeof d.reasoning_content === 'string') reasoning += d.reasoning_content;
        if (typeof d.content === 'string') content += d.content;
      } catch { /* 半截 JSON，跳过 */ }
    }
  } else {
    try {
      const j = JSON.parse(raw);
      const c0 = j.choices && j.choices[0];
      const m = (c0 && (c0.message || c0)) || {};
      content = textOf(m.content);
      if (typeof m.reasoning_content === 'string') reasoning = m.reasoning_content;
    } catch { /* 不是 JSON，跳过 */ }
  }
  if (content && !reasoning && harvestProbe < 3) {
    harvestProbe++;
    console.log(`[harvest] 有正文(${content.length}字)但没解析到思考，原文开头=${JSON.stringify(raw.slice(0, 300))}`);
  }
  remember(content, reasoning);
}

const REASONING_PLACEHOLDER = '(reasoning omitted by local bridge)';

function injectReasoning(buf) {
  if (!buf || !buf.length) return { buf, injected: 0, filled: 0, dropped: 0 };
  let j;
  try { j = JSON.parse(buf.toString('utf8')); } catch { return { buf, injected: 0, filled: 0, dropped: 0 }; }
  if (!Array.isArray(j.messages)) return { buf, injected: 0, filled: 0, dropped: 0 };

  // 先清掉"既无正文、也无工具调用"的空壳 assistant 消息。
  // 这类多半是某次中断留下的，上游会直接 400：content or tool_calls must be set。
  let dropped = 0;
  const kept = [];
  for (const m of j.messages) {
    if (m && m.role === 'assistant') {
      const c = m.content;
      const empty = c === null || c === undefined || (typeof c === 'string' && !c.trim()) || (Array.isArray(c) && c.length === 0);
      if (empty && !m.tool_calls) { dropped++; continue; }
    }
    kept.push(m);
  }
  if (dropped) j.messages = kept;

  let injected = 0, filled = 0;
  for (const m of j.messages) {
    if (!m || m.role !== 'assistant') continue;
    // 占位是我自己塞的，客户端会把它存进历史再回传；不认出来的话，真值永远换不回去
    const cur = m.reasoning_content;
    if (typeof cur === 'string' && cur && cur !== REASONING_PLACEHOLDER) continue;
    const text = textOf(m.content);
    const hit = text ? reasoningByText.get(hashText(text)) : null;
    if (hit) { m.reasoning_content = hit; injected++; }
    else { m.reasoning_content = REASONING_PLACEHOLDER; filled++; }
  }
  if (!injected && !filled && !dropped) return { buf, injected: 0, filled: 0, dropped: 0 };
  return { buf: Buffer.from(JSON.stringify(j), 'utf8'), injected, filled, dropped };
}

// 上游回 400 时把这次请求体存下来：真实原因只能从原文里看出来
function dumpFail(reqBody, resBufs) {
  try {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const file = path.join(process.cwd(), `proxy-fail-${stamp}.json`);
    let req = null;
    try { req = JSON.parse(reqBody.toString('utf8')); } catch { req = String(reqBody).slice(0, 20000); }
    writeFileSync(file, JSON.stringify({
      request: req,
      response: Buffer.concat(resBufs).toString('utf8').slice(0, 8000),
    }, null, 2), 'utf8');
    console.log(`[dump] 上游回了 400，这次的请求体已存到 ${path.basename(file)}`);
  } catch (e) {
    console.log('[dump] 存失败：' + e.message);
  }
}

function onceAttempt(req, res, body, attempt, state) {
  return new Promise((resolve) => {
    let settled = false;
    let forwarded = false;
    const done = (o) => { if (!settled) { settled = true; resolve(o); } };

    if (state.clientGone) return done({ ok: false, why: 'client gone before attempt' });

    const u = new URL(UPSTREAM + req.url);
    const mod = u.protocol === 'https:' ? https : http;
    const isTls = u.protocol === 'https:';

    let firstByteTimer = null;
    const upstreamReq = mod.request({
      protocol: u.protocol,
      hostname: u.hostname,
      port: u.port || (u.protocol === 'https:' ? 443 : 80),
      path: u.pathname + u.search,
      method: req.method,
      headers: buildHeaders({ ...req.headers, __wbSession: state.session, __gzip: state.gzipped }),
      agent: AGENTS[u.protocol],
    }, (upstreamRes) => {
      if (firstByteTimer) clearTimeout(firstByteTimer);
      const status = upstreamRes.statusCode || 0;

      if (RETRYABLE_STATUS.has(status) && !forwarded && !state.clientGone && attempt < RETRIES) {
        upstreamRes.resume();
        upstreamReq.destroy();
        done({ retry: true, why: `upstream ${status}` });
        return;
      }

      const headers = {};
      for (const [k, v] of Object.entries(upstreamRes.headers)) {
        if (!HOP_BY_HOP.has(k.toLowerCase())) headers[k] = v;
      }
      try { res.writeHead(status, headers); } catch { /* 客户端已断 */ }
      forwarded = true;
      state.status = status;

      upstreamRes.on('error', (e) => {
        if (!res.writableEnded) res.destroy();
        done({ ok: false, why: `upstream stream: ${e.message}` });
      });
      upstreamRes.on('end', () => { if (!res.writableEnded) res.end(); done({ ok: true }); });

      // 一边转发一边偷看：把 reasoning_content 存起来备用，400 时把请求体落盘。
      // 偷看有上限（4MB），超了就不再收集，转发不受影响。
      const bufs = [];
      let accLen = 0;
      const tap = new Transform({
        transform(chunk, enc, cb) {
          if (accLen < 4 * 1024 * 1024) { bufs.push(chunk); accLen += chunk.length; }
          cb(null, chunk);
        },
      });
      tap.on('end', () => {
        try { harvest(bufs, status); } catch { /* 收集失败不影响转发 */ }
        if (status === 400) dumpFail(state.rawBody || body, bufs);
      });
      upstreamRes.pipe(tap).pipe(res);
    });

    state.upstreamReq = upstreamReq;

    const connectTimer = setTimeout(() => {
      if (!forwarded) upstreamReq.destroy(new Error('connect timeout'));
    }, CONNECT_TIMEOUT);

    firstByteTimer = setTimeout(() => {
      if (!forwarded) upstreamReq.destroy(new Error('first byte timeout'));
    }, FIRST_BYTE_TIMEOUT);

    upstreamReq.on('socket', (socket) => {
      const mark = () => clearTimeout(connectTimer);
      // v4.1 修复：走 CONNECT 隧道时，TunnelHttpsAgent 交回来的 socket 已经连好并完成 TLS，
      // connect / secureConnect 早就触发过了，这两个 once 永远不会再响 —— 计时器也永远不清。
      // 结果：上游只要 30 秒内没吐响应头（长思考、长任务排队），请求就会被代理自己杀掉，
      // 报成 502 connect timeout，看起来像网络断。所以这里先按当前状态判一次。
      if (!socket.connecting) mark();
      socket.once('connect', mark);
      socket.once('secureConnect', mark);

      // 直连模式的抖动闸门：只对"https 且这条 socket 还没握手完"的情况生效。
      // 跨境直连的 TLS 握手会在 0.5s 与 12s 之间乱跳，超预算就掐掉交给外层重试。
      // 预算逐次放宽（5s → 15s → 不掐），所以最坏情况只是回到"老实等"，
      // 不会把本来就慢但能成的请求掐成失败。已建好的复用连接不动。
      const tlsBudget = TLS_BUDGETS[Math.min(attempt, TLS_BUDGETS.length - 1)] || 0;
      if (OUTBOUND_DIRECT && tlsBudget > 0 && isTls && socket.connecting) {
        const tlsTimer = setTimeout(() => {
          if (!forwarded) upstreamReq.destroy(new Error('tls handshake too slow'));
        }, tlsBudget);
        socket.once('secureConnect', () => clearTimeout(tlsTimer));
        socket.once('close', () => clearTimeout(tlsTimer));
      }

      socket.setTimeout(IDLE_TIMEOUT);
      socket.setKeepAlive(true, 30000);
      socket.setNoDelay(true);
    });

    upstreamReq.on('error', (e) => {
      clearTimeout(connectTimer);
      if (firstByteTimer) clearTimeout(firstByteTimer);
      const code = e.code || e.message;
      if (state.clientGone) return done({ ok: false, why: 'client aborted' });
      if (!forwarded && RETRYABLE.has(code) && attempt < RETRIES) {
        return done({ retry: true, why: `attempt ${attempt + 1}: ${code}` });
      }
      if (!forwarded && !res.headersSent) {
        res.writeHead(502, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: { type: 'proxy_error', message: e.message } }));
      } else if (!res.writableEnded) {
        res.destroy();
      }
      done({ ok: false, why: e.message });
    });

    if (body && body.length) upstreamReq.end(body); else upstreamReq.end();
  });
}

// 同时在飞的请求按 session 计数：同一个 session 并发超过 1 时，上游会串行化或直接拒，
// 这是"两个对话必有一个报错"的一个候选原因。这里只观察、不改行为，让日志能看出来。
const inflight = new Map();
// 正在跑的请求：id -> {sid, label, started}。界面上"哪个任务在等上游、等了多久"靠它。
let taskSeq = 0;
const activeTasks = new Map();
const sessionLabels = new Map();   // 会话身份 -> 人看得懂的标签

// ——— 给窗口外面看的看板数据 ———
// 控制台日志是一直流下去的，人眼很难判断"现在到底有没有在跑"。这里留一份最近状态，
// 浏览器打开 http://127.0.0.1:8787/ 就能一眼看完。
const STARTED_AT = Date.now();
const VERSION = '4.9';
const RECENT_MAX = 200;
const recent = [];
const stat = { total: 0, ok: 0, fail: 0, repaired: 0, filled: 0 };

function recordEvent(ev) {
  stat.total++;
  if (ev.aborted) { /* 客户端自己断的，不算失败 */ }
  else if (ev.status >= 200 && ev.status < 300) stat.ok++;
  else stat.fail++;
  if (ev.injected) stat.repaired += ev.injected;
  if (ev.filled) stat.filled += ev.filled;
  recent.push(ev);
  if (recent.length > RECENT_MAX) {
    recent.shift();
    if (viewEnd !== null) viewEnd = Math.max(1, viewEnd - 1);
  }
}

// ——— 终端界面 ———
const pad2 = (n) => (n < 10 ? '0' + n : '' + n);
const hhmmss = (t) => { const d = new Date(t); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; };
function agoText(ms) {
  const s = Math.round(ms / 1000);
  if (s < 60) return s + ' 秒前';
  if (s < 3600) return Math.round(s / 60) + ' 分钟前';
  return Math.round(s / 3600) + ' 小时前';
}
function fit(s, w) {
  // 中文按两格宽算，超出截断、不足补空格，保证每行不出界
  let out = '', width = 0;
  for (const ch of s) {
    const cw = /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(ch) ? 2 : 1;
    if (width + cw > w) break;
    out += ch; width += cw;
  }
  return out + ' '.repeat(Math.max(0, w - width));
}
let tuiTimer = null;
// 翻页状态：null = 跟着最新走；数字 = 定在某一屏（下标指向"这一屏之后的第一个"）
let viewEnd = null;
const listRowsNow = () => Math.max(4, (process.stdout.rows || 30) - 10);
function clampView(v) {
  if (recent.length === 0) return null;
  if (v === null) return null;
  return Math.max(1, Math.min(v, recent.length));
}
function dispWidth(str) {
  let w = 0;
  for (const ch of str) {
    w += /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(ch) ? 2 : 1;
  }
  return w;
}

function drawTui() {
  if (!TUI_MODE) return;
  const cols = Math.max(72, process.stdout.columns || 100);
  const listRows = listRowsNow();
  const now = Date.now();
  const lastAt = recent.length ? recent[recent.length - 1].at : 0;
  const idle = lastAt ? now - lastAt : Infinity;
  const lastEv = recent.length ? recent[recent.length - 1] : null;
  const lastBad = lastEv && !(lastEv.status >= 200 && lastEv.status < 300) && !lastEv.aborted;

  let light, lightText;
  if (activeTasks.size) { light = C.green + C.bold; lightText = '● 运行中'; }
  else if (!lastAt) { light = C.gray; lightText = '● 等待请求'; }
  else if (lastBad) { light = C.red + C.bold; lightText = '● 最近一次失败'; }
  else if (idle < 95000) { light = C.green + C.bold; lightText = '● 运行中'; }
  else { light = C.gray; lightText = '● 空闲'; }

  const line = (s) => '\x1b[2K' + s + '\n';
  const hr = C.gray + '─'.repeat(cols) + C.reset;
  const follow = viewEnd === null;
  const end = follow ? recent.length : Math.min(viewEnd, recent.length);
  const start = Math.max(0, end - listRows);
  const items = recent.slice(start, end).reverse();

  // 列宽：任务 / 时间 / 耗时 / 结果。表头和数据行共用这一套，才不会错位。
  const LW = 26, TW = 8, MW = 9, RW = 4;
  const LEFT = 2 + LW + 1 + TW + 2 + MW + 2 + RW;   // 左侧固定列总显示宽度

  const out = [];
  out.push('\x1b[H');
  const brand = `${C.bgBlue}${C.white}${C.bold} OpenCode Go 启动器 ${C.reset} ${C.cyan}${C.bold}v${VERSION}${C.reset}`;
  const brandW = dispWidth(' OpenCode Go 启动器 ') + 1 + dispWidth('v' + VERSION);
  const rightTxt = follow ? lightText : '● 回看历史';
  const rightW = dispWidth(rightTxt);
  out.push(line(brand + ' '.repeat(Math.max(1, cols - brandW - rightW - 1)) + (follow ? light : C.yellow + C.bold) + rightTxt + C.reset));
  out.push(line(hr));

  // 两行必须让竖线落在同一列：每段先算纯文本宽度，补到固定宽度再上色
  const KEYW = 8;                       // 键名 4 个汉字宽
  const COL = Math.max(16, Math.min(26, Math.floor((cols - 16) / 2)));
  const pad = (plain) => ' '.repeat(Math.max(0, COL - dispWidth(plain)));
  const key = (k) => C.gray + fit(k, KEYW) + C.reset;
  const bar = `${C.gray}│${C.reset}`;

  const obPlain = OUTBOUND_DIRECT ? '直连' : ('经代理 ' + (PROXY ? PROXY.host + ':' + PROXY.port : ''));
  const obColor = OUTBOUND_DIRECT ? C.yellow + obPlain + C.reset : C.green + obPlain + C.reset;
  const idlePlain = lastAt ? agoText(idle) : '还没收到';
  const idleColor = lastAt ? C.bold + idlePlain + C.reset : C.gray + idlePlain + C.reset;

  const s1Plain = `${fit('出站', KEYW)} ${obPlain}`;
  const s2Plain = `${fit('最近请求', KEYW)} ${idlePlain}`;
  const okPlain = `成功 ${stat.ok} / ${stat.total}   失败 ${stat.fail}`;
  const runPlain = `运行 ${Math.round((now - STARTED_AT) / 60000)} 分钟`;

  out.push(line(` ${key('出站')} ${obColor}${pad(s1Plain)} ${bar}   ${C.green}成功 ${stat.ok}${C.reset} / ${stat.total}   失败 ${stat.fail ? C.red + stat.fail + C.reset : '0'}${pad(okPlain)} ${bar}   补思考 ${stat.repaired}   占位 ${stat.filled}`));
  out.push(line(` ${key('最近请求')} ${idleColor}${pad(s2Plain)} ${bar}   运行 ${Math.round((now - STARTED_AT) / 60000)} 分钟${pad(runPlain)} ${bar}   存档 ${reasoningByText.size} 条   并发 ${activeTasks.size}`));

  const act = [...activeTasks.values()];
  let actText;
  if (!act.length) {
    actText = C.gray + '无（等下一步请求）' + C.reset;
  } else {
    const shown = act.slice(0, 4).map((a) => {
      const sec = Math.round((now - a.started) / 1000);
      const col = sec > 300 ? C.red + C.bold : (sec > 120 ? C.yellow : C.green);
      return `${C.cyan}${fit(a.label, 20)}${C.reset} ${col}${String(sec).padStart(3)} 秒${sec > 300 ? ' 可能卡住' : ''}${C.reset}`;
    });
    if (act.length > 4) shown.push(C.gray + `…另 ${act.length - 4} 个` + C.reset);
    actText = shown.join(C.gray + '  ·  ' + C.reset);
  }
  out.push(line(` ${C.gray}进行中  ${C.reset}` + actText));
  out.push(line(hr));

  const posText = recent.length ? `第 ${start + 1}-${end} 条 / 共 ${recent.length} 条` : '';
  const headLeft = '  ' + fit('任务', LW) + ' ' + fit('时间', TW) + '  ' + fit('耗时', MW) + '  ' + fit('结果', RW);
  const gap = Math.max(1, cols - LEFT - dispWidth(posText));
  out.push(line(C.gray + headLeft + (posText ? ' '.repeat(gap) + posText : '') + C.reset));

  if (!items.length) out.push(line(`${C.gray}  还没有请求。在 WorkBuddy 里发一条消息试试。${C.reset}`));
  for (const e of items) {
    const good = e.status >= 200 && e.status < 300;
    const resPlain = good ? '成功' : (e.aborted ? '中断' : '失败');
    const resColor = good ? C.green : (e.aborted ? C.yellow : C.red);
    const bits = [];
    if (e.status) bits.push('HTTP ' + e.status);
    if (e.gzipped) bits.push('压缩');
    if (e.injected) bits.push('补思考 ' + e.injected);
    if (e.filled) bits.push('占位 ' + e.filled);
    if (e.dropped) bits.push('丢空壳 ' + e.dropped);
    if (e.why && !e.aborted) bits.push(e.why);
    if (e.aborted) bits.push('客户端断开');
    const rowLeft = '  ' + C.cyan + fit(e.label || e.session || '', LW) + C.reset
      + ' ' + C.gray + fit(hhmmss(e.at), TW) + C.reset
      + '  ' + fit((e.ms / 1000).toFixed(1) + ' 秒', MW)
      + '  ' + resColor + resPlain + C.reset + '  ';
    out.push(line(rowLeft + C.gray + fit(bits.join('  '), Math.max(8, cols - LEFT - 2)) + C.reset));
  }
  for (let i = items.length; i < listRows; i++) out.push(line(''));
  out.push(line(hr));
  const hint = follow
    ? '↑↓ 翻行 · PgUp/PgDn 翻屏 · Home 最早 · q 退出'
    : '↑↓ 翻行 · PgUp/PgDn 翻屏 · End 回到最新 · q 退出';
  out.push(line(`${C.gray} ${hint}${C.reset}`));
  process.stdout.write(out.join(''));
}
function startTui() {
  if (!TUI_MODE) return;
  // 窗口标题也带版本号，看任务栏就知道跑的是哪一版
  process.stdout.write(`\x1b]0;OpenCode Go 启动器 v${VERSION}\x07`);
  process.stdout.write('\x1b[?1049h\x1b[?25l');   // 切到备用屏、藏光标
  const stop = () => {
    try { process.stdout.write('\x1b[?25h\x1b[?1049l'); } catch {}
  };
  process.on('exit', stop);
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(sig, () => { stop(); flushCache(); process.exit(0); });
  }
  if (process.stdin.isTTY) {
    try {
      process.stdin.setRawMode(true);
      process.stdin.on('data', (b) => {
        const k = b.toString('utf8');
        const rows = listRowsNow();
        let handled = true;
        if (k === 'q' || k === '\u0003') { stop(); flushCache(); process.exit(0); }
        else if (k === '\x1b[A' || k === 'k') viewEnd = clampView((viewEnd === null ? recent.length : viewEnd) - 1);
        else if (k === '\x1b[B' || k === 'j') {
          const next = (viewEnd === null ? recent.length : viewEnd) + 1;
          viewEnd = next >= recent.length ? null : clampView(next);
        }
        else if (k === '\x1b[5~') viewEnd = clampView((viewEnd === null ? recent.length : viewEnd) - rows);
        else if (k === '\x1b[6~') {
          const next = (viewEnd === null ? recent.length : viewEnd) + rows;
          viewEnd = next >= recent.length ? null : clampView(next);
        }
        else if (k === '\x1b[H' || k === '\x1b[1~') viewEnd = clampView(rows);
        else if (k === '\x1b[F' || k === '\x1b[4~' || k === 'G') viewEnd = null;
        else handled = false;
        if (handled) drawTui();
      });
    } catch { /* 拿不到键盘就算，关窗口也能停 */ }
  }
  tuiTimer = setInterval(drawTui, 500);
  drawTui();
}

// 诊断：客户端到底有没有把"这是哪个会话"传过来。
// 前三次请求把请求头键名和疑似会话标识打印出来，看一眼就知道能不能直接查标题。
let headerProbe = 0;

async function relay(req, res, body) {
  const started = Date.now();
  if (headerProbe < 3) {
    headerProbe++;
    const keys = Object.keys(req.headers).join(',');
    const guess = Object.entries(req.headers)
      .filter(([k]) => /session|conversation|chat|thread|task|thread/i.test(k))
      .map(([k, v]) => `${k}=${String(v).slice(0, 48)}`).join(' | ');
    console.log(`[headers] 键名=${keys}`);
    console.log(`[headers] 疑似会话标识=${guess || '（没有）'}`);
  }
  // 先把缺的 reasoning_content 补上，再算身份、再发上游
  const repaired = injectReasoning(body);
  body = repaired.buf;
  const info = deriveSessionInfo(body);
  const convId = String(req.headers['x-conversation-id'] || '');
  const convTitle = titleFor(convId);
  if (convTitle) info.label = convTitle;
  // 压缩只在大请求上做：小请求没必要，多一层解压也可能被上游挑剔
  let sendBody = body;
  if (GZIP && body && body.length > 4096) {
    try { sendBody = zlib.gzipSync(body); } catch { sendBody = body; }
  }
  const state = { clientGone: false, upstreamReq: null, session: info.session, injected: repaired.injected, filled: repaired.filled || 0, dropped: repaired.dropped || 0, gzipped: sendBody !== body, rawBody: body };
  const sidKey = state.session || DEFAULT_SESSION;
  if (info.label && !sessionLabels.has(sidKey)) sessionLabels.set(sidKey, info.label);
  const taskKey = ++taskSeq;
  activeTasks.set(taskKey, { sid: sidKey.slice(0, 8), label: sessionLabels.get(sidKey) || sidKey.slice(0, 8), started });
  const n = (inflight.get(sidKey) || 0) + 1;
  inflight.set(sidKey, n);
  if (n > 1) console.log(`[concurrent] session=${sidKey.slice(0, 8)} 同时有 ${n} 个请求在飞（上游可能串行化或拒绝）`);

  res.on('close', () => {
    if (!res.writableEnded) {
      state.clientGone = true;
      if (state.upstreamReq) state.upstreamReq.destroy();
    }
  });

  let lastWhy = '';
  let aborted = false;
  try {
    for (let attempt = 0; attempt <= RETRIES; attempt++) {
      if (state.clientGone) { aborted = true; lastWhy = 'client aborted'; break; }
      const r = await onceAttempt(req, res, sendBody, attempt, state);
      if (r.ok || !r.retry) { lastWhy = r.why || ''; break; }
      lastWhy = r.why || '';
      if (state.clientGone) { aborted = true; break; }
      // 429 是上游限流，隔 0.4 秒再打等于撞墙；给它更长的退避
      const is429 = /429/.test(r.why || '');
      const wait = is429 ? [5000, 15000, 30000][Math.min(attempt, 2)] : 400 * (attempt + 1);
      console.log(`[retry ${attempt + 1}/${RETRIES}] ${req.method} ${req.url} — ${r.why}${is429 ? ` 等 ${wait / 1000} 秒` : ''}`);
      await new Promise((s) => setTimeout(s, wait));
    }
  } finally {
    activeTasks.delete(taskKey);
    const left = (inflight.get(sidKey) || 1) - 1;
    if (left > 0) inflight.set(sidKey, left); else inflight.delete(sidKey);
  }

  const ms = Date.now() - started;
  recordEvent({
    at: started, ms, method: req.method, path: req.url,
    status: state.status ?? 0, injected: state.injected || 0, filled: state.filled || 0, dropped: state.dropped || 0, gzipped: state.gzipped || false,
    label: sessionLabels.get(sidKey) || sidKey.slice(0, 8),
    why: aborted ? '客户端断开' : (lastWhy || ''), aborted,
  });
  if (aborted) {
    console.log(`[${ms}ms] ${req.method} ${req.url} ABORTED-BY-CLIENT (upstream stopped)`);
  } else {
    const sid = (req.headers['x-wb-session'] || state.session || DEFAULT_SESSION).slice(0, 8);
    // 把状态码写进日志：出问题时"上游回了什么"比"跑了多久"更有用。
    console.log(`[${ms}ms] ${req.method} ${req.url} session=${sid} status=${state.status ?? '-'}${state.injected ? ` 补思考=${state.injected}` : ''}${state.filled ? ` 占位=${state.filled}` : ''}${state.dropped ? ` 丢空壳=${state.dropped}` : ''}${state.gzipped ? ' gz' : ''}${lastWhy ? ` ERR:${lastWhy}` : ''}`);
  }
}

const server = http.createServer((req, res) => {
  if (req.url === '/' || req.url.startsWith('/?')) {
    // 网页看板已砍（2026-10-28 用户令）：状态只在终端看。这里只留一行指路，免得误点浏览器看到空白。
    res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end('OpenCode Go 启动器在跑。状态 JSON：/status   健康检查：/healthz\n');
  }
  if (req.url === '/status') {
    const lastAt = recent.length ? recent[recent.length - 1].at : 0;
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
    return res.end(JSON.stringify({
      version: VERSION, uptimeMs: Date.now() - STARTED_AT,
      hasKey: Boolean(ENV_KEY), localToken: Boolean(LOCAL_TOKEN),
      outbound: OUTBOUND_DIRECT ? 'direct' : 'proxy',
      proxy: PROXY ? `${PROXY.host}:${PROXY.port}` : '',
      upstream: UPSTREAM, total: stat.total, ok: stat.ok, fail: stat.fail,
      repaired: stat.repaired, lastAt,
      inflight: [...inflight.values()].reduce((a, b) => a + b, 0),
      active: [...activeTasks.values()].map((a) => ({ label: a.label, sid: a.sid, elapsedMs: Date.now() - a.started })),
      cacheSize: reasoningByText.size,
      recent: recent.slice(-RECENT_MAX).reverse(),
    }));
  }
  if (req.url === '/healthz') {
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end(JSON.stringify({
      ok: true, version: VERSION, upstream: UPSTREAM, hasKey: Boolean(ENV_KEY),
      localToken: Boolean(LOCAL_TOKEN),
      outbound: OUTBOUND_DIRECT ? 'direct' : `proxy ${PROXY.host}:${PROXY.port}`,
      retries: RETRIES, idleTimeout: IDLE_TIMEOUT,
      connectTimeout: CONNECT_TIMEOUT, keepAliveTimeout: KEEPALIVE_TIMEOUT,
    }));
  }
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => relay(req, res, Buffer.concat(chunks)).catch((e) => {
    console.error('[FATAL]', e.message);
    if (!res.headersSent) res.writeHead(500, { 'content-type': 'application/json' });
    if (!res.writableEnded) res.end(JSON.stringify({ error: { type: 'proxy_error', message: e.message } }));
  }));
  req.on('error', (e) => console.error('[REQ ERR]', e.message));
});

server.keepAliveTimeout = KEEPALIVE_TIMEOUT;
server.headersTimeout = 0;
server.requestTimeout = 0;
server.timeout = 0;

(async () => {
  if (PROXY) {
    const alive = await probeProxy(PROXY.host, PROXY.port, 800);
    if (!alive) {
      console.log(`[warn] 出站代理 ${PROXY.host}:${PROXY.port} 连不上，本次直接走直连。`);
      console.log('[warn] 能用，但跨境抖动会明显变大；想要稳，请先把网络代理开起来再重启我。');
      OUTBOUND_DIRECT = true;
    }
  }
  buildAgents(!OUTBOUND_DIRECT);

  // 常驻模式下的看门狗：出站走哪条路，启动那一刻定的不算数。
  // 挂着跑一整天，VPN 会中途起落；每分钟复核一次，状态真的翻了才换 agent 并记一笔，
  // 不让"开机时 VPN 没起"变成一整天的直连抖动。PROXY_WATCHDOG=0 可关。
  if (PROXY && process.env.PROXY_WATCHDOG !== '0') {
    let last = null;
    setInterval(async () => {
      const alive = await probeProxy(PROXY.host, PROXY.port, 800);
      if (last === null) { last = alive; return; }
      if (alive === last) return;
      last = alive;
      OUTBOUND_DIRECT = !alive;
      buildAgents(!OUTBOUND_DIRECT);
      console.log(`[switch] 出站改走 ${alive ? '代理 ' + PROXY.host + ':' + PROXY.port : '直连'}`);
      drawTui();
    }, 60000);
  }

  server.listen(PORT, HOST, () => {
    console.log(`OpenCode Go proxy v${VERSION} -> ${UPSTREAM}`);
    console.log(`listening http://${HOST}:${PORT}`);
    console.log(`outbound ${OUTBOUND_DIRECT ? 'DIRECT (no proxy)' : `via proxy ${PROXY.host}:${PROXY.port}`}`);
    console.log(`session  ${DEFAULT_SESSION}`);
    console.log(`retries ${RETRIES} | gzip ${GZIP ? 'ON' : 'off'} | connectTimeout ${CONNECT_TIMEOUT}ms | tlsBudget ${TLS_BUDGETS.join('/')}ms | idleTimeout ${IDLE_TIMEOUT || 'unlimited'} | keepAliveTimeout ${KEEPALIVE_TIMEOUT || 'unlimited'}`);
    if (!ENV_KEY) console.log('提示：没读到上游密钥。打开控制台按 [C] 填进去，保存后会自动重启。');
    if (LOCAL_TOKEN) console.log('本地令牌已启用：客户端填本地令牌即可，真钥匙不会落到客户端。');
    startTui();
  });
})();
