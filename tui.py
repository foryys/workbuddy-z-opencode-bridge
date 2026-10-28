# -*- coding: utf-8 -*-
"""OpenCode Go · 终端控制台

真控制台程序：ANSI 转义 + 等宽字体 + 字符画边框，零第三方依赖。

  双击桌面「OpenCode Go 启动器」 -> 托盘图标 + 这个窗口
  关掉窗口（Q / 点 X）         -> 只是收起界面，代理照常跑
  要彻底停                     -> 按 X，或托盘菜单「退出」

按键：
  C  配置（上游地址 / 接口密钥 / 默认模型 / 开机自启）
  Y  复制接入信息到剪贴板（粘到 WorkBuddy 里就能用）
  A  开机自启开关     R  刷新     Q  关窗口     X  全部退出
"""

import ctypes
import json
import os
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from ctypes import wintypes as wt
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ocg_config as C                      # noqa: E402

PORT = C.PORT
BASE = C.BASE

S = '\x1b[0m'
BOLD = '\x1b[1m'


def fg(n):
    return '\x1b[38;5;%dm' % n


CYAN = fg(45)
TEAL = fg(37)
GREEN = fg(40)
RED = fg(203)
YELLOW = fg(221)
WHITE = fg(253)
GREY = fg(248)
DIM = fg(244)
FAINT = fg(240)
EDGE = fg(245)          # 边框专用：比 DIM 暗一档，但在深色底上必须看得见

k32 = ctypes.windll.kernel32
u32 = ctypes.windll.user32
MUTEX_NAME = 'Local\\OpenCodeGoTui'
HWND_FILE = os.path.join(HERE, 'tui.hwnd')


# ------------------------------------------------------------------ 控制台
def init_console():
    try:
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleTitleW('OpenCode Go · 控制台')
    except Exception:
        pass
    try:
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)   # ENABLE_VT
    except Exception:
        pass
    try:
        if sys.stdout and sys.stdout.isatty():
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    set_font()


def set_font(face='Consolas', size=18):
    """conhost 默认点阵字很难看，换成 Consolas。失败就算了。"""
    class COORD(ctypes.Structure):
        _fields_ = [('X', ctypes.c_short), ('Y', ctypes.c_short)]

    class CFIEX(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_ulong),
                    ('nFont', ctypes.c_ulong),
                    ('dwFontSize', COORD),
                    ('FontFamily', ctypes.c_ulong),
                    ('FontWeight', ctypes.c_ulong),
                    ('FaceName', ctypes.c_wchar * 32)]

    try:
        h = k32.GetStdHandle(-11)
        fi = CFIEX()
        fi.cbSize = ctypes.sizeof(CFIEX)
        fi.nFont = 0
        fi.dwFontSize = COORD(0, size)
        fi.FontFamily = 54          # FF_MODERN | TMPF_TRUETYPE
        fi.FontWeight = 400
        fi.FaceName = face
        k32.SetCurrentConsoleFontEx(h, False, ctypes.byref(fi))
    except Exception:
        pass


def size():
    """按控制台实际可见区域取尺寸。

    不去强改窗口/缓冲区大小：新型终端用自己记的尺寸，硬改只会让缓冲区
    比可见区窄，右侧和底部露出一片黑边。
    """
    try:
        sz = os.get_terminal_size()
        return max(78, sz.columns), max(18, sz.lines)
    except Exception:
        return 100, 24


# ------------------------------------------------------------------ 宽度
def dw(s):
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
    return w


def pad(s, n):
    d = n - dw(s)
    return s + ' ' * d if d > 0 else s


def cut(s, n):
    out = ''
    room = n
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
        if cw > room:
            break
        out += ch
        room -= cw
    return out


class L(object):
    """一行内容：若干带颜色的片段，最后统一补齐/截断到指定宽度。"""

    def __init__(self):
        self.parts = []
        self.w = 0

    def add(self, text, color=''):
        if not text:
            return self
        self.parts.append((text, color))
        self.w += dw(text)
        return self

    def render(self):
        return ''.join((c + t + S) if c else t for t, c in self.parts)

    def fit(self, width):
        if self.w <= width:
            self.parts.append((' ' * (width - self.w), ''))
            return self.render()
        out = []
        room = width
        for t, c in self.parts:
            if room <= 0:
                break
            keep = ''
            for ch in t:
                cw = 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
                if cw > room:
                    break
                keep += ch
                room -= cw
            out.append((c + keep + S) if c else keep)
        if room > 0:
            out.append(' ' * room)
        return ''.join(out)


def row(width, line):
    inner = width - 4
    return '%s|%s %s %s|%s' % (EDGE, S, line.fit(inner), EDGE, S)


def bar(width, title=None, right=None, rcol=''):
    """分隔行：+-- 标题 ---------- 右侧 --+

    右侧块 = 1 空格 + 文本 + ' --+'（4），合计 dw(right)+5；
    左侧块 = '+-- '（4）+ 标题 + ' '（1）。
    """
    if not title:
        return '%s+%s+%s' % (EDGE, '-' * (width - 2), S)
    if right:
        n = width - 10 - dw(title) - dw(right)
    else:
        n = width - 8 - dw(title)
    if n < 1:
        n = 1
    s = EDGE + '+-- ' + S + CYAN + title + S + ' ' + EDGE + '-' * n + S
    if right:
        s += ' ' + S + (rcol or DIM) + right + S + EDGE + ' --+' + S
    else:
        s += EDGE + '--+' + S
    return s


# ------------------------------------------------------------------ 状态
ST = {
    'd': None, 'off': True, 'err': '',
    'autostart': False,
    'flash': '', 'flash_at': 0.0, 'flash_secs': 4.0,
    'page': 'main',
    'edit': None, 'buf': '',
    'models': None, 'msel': 0,
    'cfg': C.load(),
    'busy': '',
    'at': 0.0,
    'wb': None, 'wb_at': 0.0,       # WorkBuddy 里那条模型配置的缓存
}

FIELDS = {
    'upstream': ('上游地址', 'https://opencode.ai/inference/go'),
    'key': ('上游密钥', '按 Ctrl+V 粘贴（或右键粘贴），回车保存'),
    'model': ('默认模型', '填上游支持的模型 ID，或按 [M] 从列表挑'),
}


def flash(msg, secs=6.0):
    ST['flash'] = msg
    ST['flash_at'] = time.time()
    ST['flash_secs'] = secs


def fetch():
    d = C.status(1.6)
    if d is None:
        ST['off'] = True
        ST['d'] = None
        ST['err'] = 'no response'
    else:
        ST['d'] = d
        ST['off'] = False
        ST['err'] = ''
    # WorkBuddy 那边用的是真钥匙还是本地令牌？决定主页该怎么说话。
    # 每秒读一次小 json 可以接受，但没必要：10 秒刷一次就够。
    if time.time() - ST.get('wb_at', 0) > 10:
        ST['wb'] = C.detect_proxy_entry()
        ST['wb_at'] = time.time()
    ST['at'] = time.time()


def upstream_state():
    """钥匙在谁手上 —— 按客户端那栏判，不按本机存没存判。

    这个软件的设计是：上游密钥保存在 WorkBuddy 里，启动器只负责桥接。
    所以客户端那栏填什么才是主线索；本机的密文只管三种情况：

    wb      客户端直接带真钥匙来 → 代理原样转发（正常形态）
    local   客户端带的是本地令牌 → 代理用本机密文换掉再发上游
    broken  客户端带令牌，本机密文解不开（换过账号/机器）→ 必然 401
    no-key  客户端带令牌，本机压根没存过密文 → 必然 401
    empty   客户端没配这条，本机也没有
    """
    cfg = ST['cfg']
    wb = ST.get('wb') or {}
    wbkey = str(wb.get('apiKey') or '').strip()
    if wbkey and not C.is_local_token(wbkey):
        return 'wb'
    if C.get_key(cfg):
        return 'local'
    if C.has_upstream(cfg):
        return 'broken'          # 密文在，但解不开
    if wbkey:
        return 'no-key'          # 客户端拿令牌来，本机没钥匙可换
    return 'empty'


def hms(ms):
    s = int(ms / 1000)
    return '%02d:%02d:%02d' % (s // 3600, (s % 3600) // 60, s % 60)


def status_color(code):
    if code >= 500:
        return RED
    if code >= 400:
        return YELLOW
    if code >= 200:
        return GREEN
    return GREY


def ago(ms):
    if not ms:
        return '--'
    sec = int(ms / 1000)
    if sec < 60:
        return '%d 秒前' % sec
    if sec < 3600:
        return '%d 分钟前' % (sec // 60)
    return '%d 小时前' % (sec // 3600)


def notice_text(page=None):
    """提示行按页面给话。上一版三个页面共用一句，在配置页里说"去配置页看"很傻。"""
    if ST['flash'] and time.time() - ST['flash_at'] < ST['flash_secs']:
        return ST['flash']
    if ST['busy']:
        return ST['busy']
    page = page or ST['page']
    us = upstream_state()
    if page == 'config':
        if us == 'wb':
            return '钥匙在 WorkBuddy 里，代理只转发。换钥匙去客户端改，这里不用动。'
        if us == 'local':
            return '客户端持令牌，代理用本机密文换。要抄的是「第二步」那三项。'
        if us == 'broken':
            return '旧密文解不开（换了账号或机器）。按 [1] 重新填一把。'
        if us == 'no-key':
            return '客户端持令牌，本机没钥匙可换。按 [1] 填一把，或 [I] 取。'
        return '按 [1] 填上游密钥，或 [I] 从 WorkBuddy 取（那边配过就不用手打）。'
    if page == 'models':
        return '↑↓ 挑一个，回车设为默认模型；Esc 返回上一层。'
    if us == 'wb':
        return '正常：钥匙在 WorkBuddy，代理原样转发。'
    if us == 'local':
        return '正常：客户端只持令牌，真钥匙在代理的密文里。'
    if us == 'broken':
        return '密文解不开了（换过账号或机器）—— 按 [C] 进配置，按 1 重新填一把。'
    if us == 'no-key':
        return '客户端持令牌，本机没钥匙可换 —— 按 [C] 进配置按 1 填一把。'
    return '还差一步：按 [C] 填密钥，或按 [I] 从 WorkBuddy 取。'


# ------------------------------------------------------------------ 主页
def draw_main(W, H):
    out = []
    d = ST['d']
    off = ST['off']
    cfg = ST['cfg']

    if off:
        st_txt, st_col = '[!] 未运行', RED + BOLD
        ver = ''
    else:
        st_txt = '[+] 运行中'
        st_col = GREEN + BOLD
        ver = 'v' + str(d.get('version', '?'))

    out.append(bar(W, 'OPENCODE GO  桥接代理' + (('  ' + ver) if ver else ''),
                   st_txt, st_col))

    def kv(label, value, vcol=WHITE, tail='', tcol=''):
        l = L().add(' ').add(pad(label, 10), GREY).add(' ', DIM).add(value, vcol)
        if tail:
            room = (W - 4) - l.w - dw(tail) - 2
            if room > 0:
                l.add(' ' * room).add(tail, tcol or DIM)
        out.append(row(W, l))

    # 上半：客户端要填的两项 + 上游密钥在谁手上（都是"能不能用"的关键）
    us = upstream_state()
    wb = ST.get('wb') or {}
    kv('接口地址', 'http://127.0.0.1:%d/v1/chat/completions'
       % int(cfg.get('port') or PORT), WHITE)
    kv('接口密钥', C.mask(cfg.get('local_key')), CYAN, '复制 [Y]')
    if us == 'wb':
        # 主形态：钥匙在 WorkBuddy，代理只转发。这把钥匙一律不露片段。
        kv('上游密钥', '已在 WorkBuddy 中填写', GREEN, '代理只转发', DIM)
    elif us == 'local':
        live = (not off) and bool(d.get('hasKey'))
        kv('上游密钥', '已在本机加密保存', GREEN if live else YELLOW,
           '客户端持令牌' if live else '代理没加载',
           GREEN if live else YELLOW)
    elif us == 'broken':
        # 密文在，解不开。换 Windows 账号、换机器、重装系统都会这样。
        # 这时说「还没填」是误导——他填过，是旧密文作废了，得重填一把。
        kv('上游密钥', '(旧密文解不开)', RED,
           '换过账号或机器 → 按 [C] 重新填', RED)
    elif us == 'no-key':
        kv('上游密钥', '(缺)', RED, '客户端拿令牌来，本机没钥匙 → 会 401', RED)
    else:
        kv('上游密钥', '(还没填)', YELLOW, '按 [C] 进配置，再按 1 粘贴', YELLOW)

    # 下半：代理自身的状态
    if off:
        kv('代理状态', '端口 %d 没有应答' % PORT, RED)
        kv('怎么办', '双击桌面「OpenCode Go 启动器」重新拉起', YELLOW)
        kv('已运行', '--:--:--', DIM)
    else:
        kv('上游地址', cut(str(d.get('upstream', '-')), max(10, W - 22)), WHITE)
        ob = d.get('outbound', '')
        if ob == 'direct':
            kv('出站方式', '直连（没走本地代理）', WHITE)
        else:
            kv('出站方式', '经本地代理 %s' % (d.get('proxy') or '-'), WHITE)
        kv('已运行', hms(d.get('uptimeMs', 0)), WHITE,
           '刷新 ' + time.strftime('%H:%M:%S'))

    # ---- 请求统计
    out.append(bar(W, '请求统计'))
    if off:
        out.append(row(W, L().add(' ').add('等待代理重新应答……', DIM)))
        out.append(row(W, L()))
    else:
        total = d.get('total', 0)
        ok = d.get('ok', 0)
        fail = d.get('fail', 0)
        infl = d.get('inflight', 0)
        cache = d.get('cacheSize', 0)
        last = d.get('lastAt', 0)
        l = L().add(' ')
        l.add('总计 ', GREY).add(str(total).ljust(6), WHITE)
        l.add('成功 ', GREY).add(str(ok).ljust(5), GREEN)
        l.add('失败 ', GREY).add(str(fail).ljust(5), RED if fail else WHITE)
        l.add('进行中 ', GREY).add(str(infl).ljust(4), CYAN if infl else WHITE)
        l.add('缓存 ', GREY).add(str(cache), DIM)
        out.append(row(W, l))

        if total:
            rate = ok * 100.0 / total
            cells = 20
            fill = int(round(rate / 100.0 * cells))
            bc = GREEN if rate >= 95 else (YELLOW if rate >= 80 else RED)
            l = L().add(' ').add('成功率 ', GREY)
            l.add('[' + '#' * fill, bc).add('.' * (cells - fill), FAINT)
            l.add('] ', DIM).add('%5.1f%%' % rate, bc)
            l.add('   ').add('补写思考 %d' % d.get('repaired', 0), DIM)
        else:
            l = L().add(' ').add('成功率 ', GREY)
            l.add('[' + '.' * 20 + ']', FAINT).add('   --', DIM)
            l.add('   ').add('最近一次 %s' % ago(time.time() * 1000 - last), DIM)
        out.append(row(W, l))

    # ---- 最近请求
    out.append(bar(W, '最近请求'))
    recent = (d or {}).get('recent', []) if not off else []
    inner = W - 4
    avail = max(1, H - len(out) - 4)

    hl = L().add(' ').add(pad('时间', 10), GREY).add(' ')
    hl.add(pad('方法', 7), GREY).add(pad('路径', 26), GREY)
    # 右侧三列和数据行用同一套宽度：状态 4 + 间隔 4 + 耗时 7
    gap = inner - (hl.w + 15)
    hl.add(' ' * max(2, gap)).add('状态', GREY).add('    ').add('  耗时', GREY)
    out.append(row(W, hl))

    body = avail - 1
    if body < 1:
        body = 1
    if not recent:
        out.append(row(W, L().add(' ').add('（还没有请求）', FAINT)))
        for _ in range(max(0, body - 1)):
            out.append(row(W, L()))
    else:
        shown = recent[:body]
        for item in shown:
            ts = datetime.fromtimestamp(item.get('at', 0) / 1000).strftime('%H:%M:%S')
            code = item.get('status', 0)
            ms = '%dms' % item.get('ms', 0)
            l = L().add(' ').add(pad(ts, 10), FAINT).add(' ')
            l.add(pad(item.get('method', ''), 7), TEAL)
            l.add(cut(item.get('path', ''), 24), GREY)
            right_w = 4 + 4 + 7
            gap = inner - (l.w + right_w)
            l.add(' ' * max(2, gap))
            l.add(('%d' % code).rjust(4), status_color(code))
            l.add('    ').add(ms.rjust(7), DIM)
            out.append(row(W, l))
        for _ in range(max(0, body - len(shown))):
            out.append(row(W, L()))

    # ---- 底部
    out.append(bar(W))
    l = L().add(' ')
    l.add('[C]', CYAN).add(' 配置  ', GREY)
    l.add('[Y]', CYAN).add(' 复制配置  ', GREY)
    l.add('[A]', CYAN).add(' 自启:', GREY)
    l.add('开  ' if ST['autostart'] else '关  ',
          GREEN if ST['autostart'] else DIM)
    l.add('[R]', CYAN).add(' 刷新  ', GREY)
    l.add('[Q]', CYAN).add(' 关窗口  ', GREY)
    l.add('[X]', RED).add(' 全退', GREY)
    out.append(row(W, l))
    out.append(row(W, L().add(' ').add(notice_text(),
                                    YELLOW if ST['flash'] else FAINT)))
    out.append(bar(W))
    return out


# ------------------------------------------------------------------ 配置页
def draw_config(W, H):
    """两段式：上半是你填的，下半是软件给你的。

    上一版把两把钥匙都叫「接口密钥」，用户不知道哪把是自己填、哪把是拿去贴的。
    现在区块标题直接写明「你填进去 / 抄进 WorkBuddy」，名字也分开：
      上游密钥 —— 从 opencode 拿的真钥匙，只有代理用
      接口密钥 —— 软件发的本地令牌，填进 WorkBuddy 的
    """
    out = []
    cfg = ST['cfg']
    out.append(bar(W, '配置', 'Esc 返回'))
    out.append(row(W, L()))

    def line(label, value, vcol, num='', act='', note='', ncol=FAINT):
        l = L().add('   ').add(pad(label, 10), GREY).add(' ', DIM)
        l.add(value, vcol)
        if note:
            l.add('   ').add(note, ncol)
        if num:
            tail = '[%s] %s' % (num, act)
            room = (W - 4) - l.w - dw(tail) - 2
            if room > 0:
                l.add(' ' * room).add(tail, CYAN)
        out.append(row(W, l))

    # ---- 第一步：你填
    out.append(bar(W, '第一步 · 上游钥匙', 'WorkBuddy 里那把', CYAN))
    us = upstream_state()
    wb = ST.get('wb') or {}
    if us == 'wb':
        line('上游密钥', '已在 WorkBuddy 中填写', GREEN, '', '',
             '代理转发时直接用', DIM)
    elif us == 'local':
        line('上游密钥', '已在本机加密保存', GREEN, '1', '更换',
             '客户端持令牌', DIM)
    elif us == 'broken':
        line('上游密钥', '(旧密文解不开)', RED, '1', '重新填', '换了账号或机器', RED)
        out.append(row(W, L().add('   ').add(
            '密文绑本机账号，换机器或重装就解不开，只能重填一把。',
            YELLOW)))
    elif us == 'no-key':
        line('上游密钥', '(缺)', RED, '1', '填一把', '本机没钥匙，换不了令牌', RED)
    else:
        line('上游密钥', '(还没填)', YELLOW, '1', '填写', '必需', YELLOW)
    line('上游地址', cut(cfg.get('upstream', ''), max(10, W - 26)), WHITE, '2', '修改')
    line('默认模型', cfg.get('model') or '(未设置)',
         WHITE if cfg.get('model') else DIM, '3', '修改')
    line('开机自启', '已开启' if ST['autostart'] else '已关闭',
         GREEN if ST['autostart'] else DIM, '4', '切换')

    # ---- 第二步：软件给你
    out.append(bar(W, '第二步 · 抄进 WorkBuddy',
                   '软件生成的，不用手打', CYAN))
    port = int(cfg.get('port') or PORT)
    line('接口地址', 'http://127.0.0.1:%d/v1/chat/completions' % port, WHITE)
    line('接口密钥', C.mask(cfg.get('local_key')), CYAN, '5', '换一把')
    line('模型名称', cfg.get('model') or '(未设置)', WHITE)
    if us == 'wb':
        out.append(row(W, L().add('   ').add(
            '接口密钥填真钥匙就行；想让客户端不存明文，才填下面那把令牌。',
            FAINT)))
    elif us == 'local':
        out.append(row(W, L().add('   ').add(
            '客户端只持令牌：接口密钥填 sk-ocg- 那把。', FAINT)))
    elif us == 'broken':
        out.append(row(W, L().add('   ').add(
            '[!] 旧密文解不开，抄过去一定 401。先按 [1] 重填。', YELLOW)))
    elif us in ('no-key', 'empty'):
        out.append(row(W, L().add('   ').add(
            '[!] 本机没有可用钥匙，填令牌会 401。先按 [1] 填一把。', YELLOW)))
    elif ST['off']:
        out.append(row(W, L().add('   ').add(
            '[!] 代理没在跑，抄过去也连不上。双击桌面图标拉起。', YELLOW)))
    else:
        out.append(row(W, L().add('   ').add(
            '按 [Y] 把上面三项一次复制走（不含表头，方便分段粘）', FAINT)))

    # ---- 怎么填
    room = H - len(out) - 4
    if room >= 7:
        out.append(bar(W, '在 WorkBuddy 里怎么填'))
        steps = [
            '1  模型设置里新增一个「自定义 / OpenAI 兼容」模型',
            '2  接口地址整串照抄（含 /v1/chat/completions）',
            '3  密钥填「接口密钥」那把（本地令牌，不是上游真钥匙）',
            '4  模型名称填上面对应的那个；拿不准按 [M] 从上游列表挑',
            '5  保存后发一句话，回主页看「最近请求」是不是 200',
        ]
        for t in steps:
            out.append(row(W, L().add('   ').add(t, GREY)))
        room = H - len(out) - 4

    # ---- 密钥怎么存的（用户最关心这条，顺便把版面填实）
    if room >= 5:
        out.append(bar(W, '密钥怎么存的'))
        for t in ('两把钥匙都只存密文：Windows DPAPI 加密，绑当前账号 + 这台机器',
                  'config.json 里没有明文；文件拷到别的电脑或别的账号也解不开',
                  '上游真钥匙只经环境变量传给代理进程，不写进任何文件'):
            out.append(row(W, L().add('   ').add(t, DIM)))
        room = H - len(out) - 4
    for _ in range(max(0, room)):
        out.append(row(W, L()))

    out.append(bar(W))
    l = L().add(' ')
    l.add('[1][2][3]', CYAN).add(' 编辑  ', GREY)
    l.add('[4]', CYAN).add(' 自启  ', GREY)
    l.add('[5]', CYAN).add(' 换令牌  ', GREY)
    l.add('[I]', GREEN).add(' 导入  ', GREY)
    l.add('[T]', GREEN).add(' 清明文  ', GREY)
    l.add('[M]', CYAN).add(' 拉模型  ', GREY)
    l.add('[Y]', GREEN).add(' 复制  ', GREY)
    l.add('[Esc]', CYAN).add(' 返回', GREY)
    out.append(row(W, l))
    out.append(row(W, L().add(' ').add(notice_text(),
                                    YELLOW if ST['flash'] else FAINT)))
    out.append(bar(W))
    return out


# ------------------------------------------------------------------ 模型页
def draw_models(W, H):
    out = []
    ids = ST['models'] or []
    out.append(bar(W, '上游模型', 'Esc 返回'))
    out.append(row(W, L().add(' ').add(
        '共 %d 个 · ↑↓ 选择 · Enter 设为默认模型' % len(ids), DIM)))
    avail = max(1, H - 6)
    top = max(0, min(ST['msel'] - avail // 2, max(0, len(ids) - avail)))
    if top > ST['msel']:
        top = ST['msel']
    end = min(len(ids), top + avail)
    for i in range(top, end):
        on = (i == ST['msel'])
        l = L().add(' ').add('>' if on else ' ', CYAN if on else '')
        l.add(' ' + ids[i], WHITE if on else GREY)
        out.append(row(W, l))
    for _ in range(max(0, avail - (end - top))):
        out.append(row(W, L()))

    out.append(bar(W))
    l = L().add(' ')
    l.add('↑↓', CYAN).add(' 选择  ', GREY)
    l.add('PgUp/PgDn', CYAN).add(' 翻页  ', GREY)
    l.add('Enter', GREEN).add(' 选用  ', GREY)
    l.add('Esc', CYAN).add(' 返回', GREY)
    out.append(row(W, l))
    out.append(row(W, L().add(' ').add(notice_text(),
                                    YELLOW if ST['flash'] else FAINT)))
    out.append(bar(W))
    return out


# ------------------------------------------------------------------ 输入态
def draw_edit(W, H):
    base = {'main': draw_main, 'config': draw_config, 'models': draw_models}
    out = base[ST['page']](W, H)
    field = ST['edit']
    label = FIELDS.get(field, (field, ''))[0]
    hint = FIELDS.get(field, ('', ''))[1]
    l = L().add(' ').add(label + ' > ', CYAN)
    if field == 'key':
        # 密钥不上屏。但得让人看得见"到底粘进来没有"，
        # 所以用圆点数表示长度 —— 看得见长度，看不见内容。
        if ST['buf']:
            l.add('*' * min(len(ST['buf']), 48), WHITE)
            l.add('█', CYAN)
            l.add('   已输入 %d 个字符' % len(ST['buf']), GREEN)
        else:
            l.add('█', CYAN).add('  ' + hint, FAINT)
    else:
        l.add(ST['buf'], WHITE).add('█', CYAN)
        if not ST['buf']:
            l.add('  ' + hint, FAINT)
    out[-2] = row(W, l)                    # 替换提示行
    out[-3] = row(W, L().add(' ').add(
        'Enter 保存   Ctrl+V 粘贴   Ctrl+C 清空   Esc 取消', DIM))
    return out


# ------------------------------------------------------------------ 渲染
def draw():
    W, H = size()
    if ST['edit']:
        lines = draw_edit(W, H)
    elif ST['page'] == 'config':
        lines = draw_config(W, H)
    elif ST['page'] == 'models':
        lines = draw_models(W, H)
    else:
        lines = draw_main(W, H)
    return lines, W, H


def render():
    """逐行用光标定位，不用 \\n 推进。

    行宽正好等于窗口宽度时，写完最后一个字符光标就停在"待折行"位置，
    再发一个 \\n 会多走一行 —— 整屏随之往下错位，最后两行叠在一起。
    显式 CUP 到第 i 行第 1 列，就不会有这个问题。
    """
    lines, W, H = draw()
    buf = []
    for i, ln in enumerate(lines[:H]):
        buf.append('\x1b[%d;1H' % (i + 1))
        buf.append(ln)
        buf.append('\x1b[K')
    buf.append('\x1b[J')
    sys.stdout.write(''.join(buf))
    sys.stdout.flush()


# ------------------------------------------------------------------ 动作
def copy_access():
    """按顺序来：没填上游密钥就不发"看起来能用"的配置。

    上一版是先复制再提醒，用户粘进 WorkBuddy 发现 401，回来问"是不是坏了"。
    现在直接把光标带到填密钥那一栏——差的就是这一步。
    """
    cfg = ST['cfg']
    if not C.has_upstream(cfg):
        flash('先填上游密钥：粘贴后回车。填完我再把配置给你', 12.0)
        ST['page'] = 'config'               # 不然输入行会画在主页上，看不出在填什么
        start_edit('key')
        return
    ok = C.copy_text(C.access_block(cfg))
    if not ok:
        flash('复制失败：剪贴板被别的程序占着，再按一次 [Y]')
    elif ST['off']:
        flash('已复制。但代理没在跑，粘过去也连不上 —— 先双击桌面图标', 9.0)
    else:
        flash('已复制：接口地址 + 接口密钥 + 模型，直接粘进 WorkBuddy')


def toggle_autostart():
    on, msg = C.toggle_autostart()
    ST['autostart'] = on
    flash(msg)


def do_save(need_restart=True):
    """返回有没有真的落盘。调用方靠这个决定能不能说「存好了」。"""
    cfg = ST['cfg']
    try:
        C.save(cfg)
    except Exception as e:
        flash('保存失败：%s' % e)
        return False
    if ST['off']:
        flash('已保存。代理没在跑，双击桌面图标拉起后生效')
        return True
    if not need_restart:
        flash('已保存')
        return True
    ST['busy'] = '正在重启代理让新配置生效……'
    render()
    ok = C.restart(log=lambda s: None)
    ST['busy'] = ''
    fetch()
    flash('已保存，代理已重启' if ok else '已保存，但代理重启失败（看 proxy.log）')
    return True


def do_import():
    """把 WorkBuddy 里那把上游钥匙收进本机加密配置。

    这不是「替你输入」的偷懒做法，而是应对一种真实处境：
    opencode 后台只显示一次，之后列表里看不到，用户手头就没钥匙了。
    但机器上还留着一份在 WorkBuddy 的 models.json 里——就地搬过来。
    """
    cfg = ST['cfg']
    ok, msg = C.import_from_workbuddy(cfg)
    if not ok:
        flash(msg, 10.0)
        return
    ST['wb'] = C.detect_proxy_entry()
    ST['wb_at'] = time.time()
    do_save()
    flash(msg + '。现在按 [Y] 复制', 12.0)


def do_strip_plaintext():
    """把 WorkBuddy 里那栏真钥匙换成令牌，明文只留密文那一份。

    前置条件必须检查：代理得先真的加载了钥匙。否则换完令牌，
    代理手上没钥匙可换，请求立刻 401——用户会以为是我搞坏了。
    """
    cfg = ST['cfg']
    if not C.has_upstream(cfg):
        flash('本机还没有钥匙，换了就没人能换回来。先按 [I] 导入', 10.0)
        return
    if not ST['off'] and not (ST['d'] or {}).get('hasKey'):
        flash('代理还没加载钥匙。先让它重启一次（按 [S]），再来清明文', 10.0)
        return
    ok, msg = C.swap_workbuddy_token(cfg)
    if not ok:
        flash(msg, 10.0)
        return
    ST['wb'] = C.detect_proxy_entry()
    ST['wb_at'] = time.time()
    flash(msg, 14.0)


def fetch_models():
    cfg = ST['cfg']
    tok = cfg.get('local_key') or ''
    ST['busy'] = '正在向上游要模型列表……'
    render()
    req = urllib.request.Request(
        BASE + '/v1/models', headers={'Authorization': 'Bearer ' + tok})
    err = ''
    ids = []
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
        items = j.get('data') if isinstance(j, dict) else j
        for it in items or []:
            if isinstance(it, dict):
                mid = it.get('id') or it.get('name')
            else:
                mid = it
            if mid:
                ids.append(str(mid))
    except urllib.error.HTTPError as e:
        err = '上游回 %d' % e.code
        if e.code in (401, 403):
            err += '：密钥没生效，先 [S] 保存并重启'
    except Exception as e:
        err = '拉了 25 秒没结果（%s）' % type(e).__name__
    ST['busy'] = ''
    if err:
        flash(err)
        return
    if not ids:
        flash('上游没有返回任何模型')
        return
    ids.sort()
    ST['models'] = ids
    ST['msel'] = 0
    ST['page'] = 'models'
    flash('拉到 %d 个模型' % len(ids))


def start_edit(field):
    """密钥不回显：编辑框永远从空白开始。

    把真钥匙填进输入框会让它在屏幕上亮着，旁边有人就看见。
    想换就直接粘新的，粘完回车覆盖；直接回车＝不动它。
    """
    cfg = ST['cfg']
    if field == 'upstream':
        cur = cfg.get('upstream', '')
    elif field == 'key':
        cur = ''                            # 明文不上屏
    elif field == 'model':
        cur = cfg.get('model', '')
    else:
        cur = ''
    ST['edit'] = field
    ST['buf'] = cur or ''


def commit_edit():
    field = ST['edit']
    val = ST['buf'].strip()
    cfg = ST['cfg']
    was_key = field == 'key'
    if field == 'upstream':
        cfg['upstream'] = val or C.DEFAULT_UPSTREAM
    elif field == 'key':
        # 右键粘贴/终端自己粘贴时，内容是当字符流进来的，不经过
        # paste_into_buf，所以提交这里还要再洗一遍（去引号、去 Bearer 前缀）
        cleaned = C.clean_key(val)
        if not cleaned:                     # 空着回车＝不改，别把已存的擦了
            ST['edit'] = None
            ST['buf'] = ''
            flash('没输入，上游密钥保持不变')
            return
        val = cleaned
        C.set_key(cfg, val)
    elif field == 'model':
        cfg['model'] = val
    ST['edit'] = None
    ST['buf'] = ''
    ok = do_save(need_restart=(field != 'model'))
    if was_key and ok:
        # 填完密钥就停在这儿最容易迷路，直接告诉他下一步。
        # 只在真的存上时才说这句话：上一版无条件覆盖提示，
        # 保存失败也照样显示「已加密存好」，用户以为填了其实没填。
        flash('上游密钥已加密存好（%s）。现在按 [Y] 把配置复制走' % C.mask(val), 12.0)
    elif was_key and not ok:
        flash('上游密钥没存上，按 [1] 重填一次（原因见上一行）', 12.0)


def request_quit_all():
    k32.OpenEventW.restype = wt.HANDLE
    k32.OpenEventW.argtypes = [ctypes.c_uint, wt.BOOL, wt.LPCWSTR]
    h = k32.OpenEventW(0x0002, False, 'Local\\OpenCodeGoTrayQuit')
    if h:
        k32.SetEvent(h)
        return True
    return False


# ------------------------------------------------------------------ 按键
def paste_into_buf():
    """Ctrl+V 自己读剪贴板。

    不能指望终端替我们粘贴：CREATE_NEW_CONSOLE 起的是 conhost，
    Ctrl+V 常常被当成控制字符 0x16 送进来，而输入框只收 >= 空格的字符，
    于是被静默丢掉 —— 用户以为粘上了，实际一个字都没进来，
    回车就是「没输入」，然后再看到「上游密钥还没填」，一头雾水。
    """
    raw = C.read_text()
    if not raw:
        flash('剪贴板里没有文字。先复制密钥，再按 Ctrl+V，或者右键粘贴', 8.0)
        return
    was = ST['edit']
    val = raw
    if was == 'key':
        val = C.clean_key(raw)
    else:
        val = raw.strip().replace('\r', ' ').replace('\n', ' ')
    if not val:
        flash('剪贴板内容看着不像密钥，检查一下复制的是什么', 8.0)
        return
    ST['buf'] = val
    if was == 'key':
        flash('已粘贴 %d 个字符，回车保存' % len(val), 6.0)
    return


def on_edit_key(k):
    if k == '\r':
        commit_edit()
    elif k == '\x16':                      # Ctrl+V
        paste_into_buf()
    elif k == '\x1b':
        ST['edit'] = None
        ST['buf'] = ''
    elif k in ('\x08', '\x7f'):
        ST['buf'] = ST['buf'][:-1]
    elif k == '\x03':                      # Ctrl+C：清掉已输入的，不退出
        ST['buf'] = ''
        flash('已清空输入框，没保存')
    elif len(k) == 1 and k >= ' ':
        if dw(ST['buf']) < 200:
            ST['buf'] += k
    return True


def on_config_key(k):
    if k in ('\x1b', 'q'):
        ST['page'] = 'main'
    elif k == '1':
        start_edit('key')
    elif k == '2':
        start_edit('upstream')
    elif k == '3':
        start_edit('model')
    elif k == '4':
        toggle_autostart()
    elif k == '5':
        C.rekey(ST['cfg'])
        do_save()
        flash('已换一把新令牌。回 WorkBuddy 把「接口密钥」重新粘一次，老的立刻失效', 10.0)
    elif k == 'i':
        do_import()
    elif k == 't':
        do_strip_plaintext()
    elif k == 's':
        do_save()
    elif k == 'y':
        copy_access()
    elif k == 'm':
        fetch_models()
    return True


def on_models_key(k):
    ids = ST['models'] or []
    if k in ('\x1b', 'q') or not ids:
        ST['page'] = 'config'
        return True
    if k == 'up':
        ST['msel'] = max(0, ST['msel'] - 1)
    elif k == 'down':
        ST['msel'] = min(len(ids) - 1, ST['msel'] + 1)
    elif k == 'pgup':
        ST['msel'] = max(0, ST['msel'] - 10)
    elif k == 'pgdn':
        ST['msel'] = min(len(ids) - 1, ST['msel'] + 10)
    elif k == 'home':
        ST['msel'] = 0
    elif k == 'end':
        ST['msel'] = len(ids) - 1
    elif k == '\r':
        ST['cfg']['model'] = ids[ST['msel']]
        ST['page'] = 'config'
        do_save(need_restart=False)
        flash('默认模型已设为 %s' % ids[ST['msel']])
    return True


def on_main_key(k):
    if k in ('q', '\x1b'):
        return False
    if k == 'c':
        ST['page'] = 'config'
    elif k == 'y':
        copy_access()
    elif k == 'a':
        toggle_autostart()
    elif k == 'r':
        fetch()
        flash('已刷新')
    elif k == 'x':
        if request_quit_all():
            flash('已通知托盘退出')
            return False
        flash('托盘没在跑；按 [Q] 只关窗口，代理不会停')
    return True


# ------------------------------------------------------------------ 入口
def guard_single():
    k32.CreateMutexW.restype = wt.HANDLE
    k32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if k32.GetLastError() == 183:           # ERROR_ALREADY_EXISTS
        return None
    return h


def main():
    hmtx = guard_single()
    if not hmtx:
        # 托盘那边会先查这个互斥体，正常不会走到这；手动开第二次时别开第二个窗
        sys.stdout.write('控制台已经打开了，看看任务栏。\r\n')
        sys.stdout.flush()
        time.sleep(1.2)
        return

    init_console()
    ST['autostart'] = C.autostart_on()
    ST['cfg'] = C.load()

    try:
        hwnd = u32.GetConsoleWindow()
        if hwnd:
            with open(HWND_FILE, 'w') as f:
                f.write(str(hwnd))
    except Exception:
        pass

    sys.stdout.write('\x1b[?25l\x1b[2J')
    fetch()

    import msvcrt
    next_tick = 0.0
    dirty = True
    try:
        while True:
            now = time.monotonic()
            if now >= next_tick:
                if not ST['edit']:
                    fetch()
                    ST['autostart'] = C.autostart_on()
                next_tick = now + 1.0
                dirty = True

            if dirty:
                render()
                dirty = False

            if msvcrt.kbhit():
                k = msvcrt.getwch()
                if k in ('\x00', '\xe0'):
                    c2 = msvcrt.getwch()
                    k = {'H': 'up', 'P': 'down', 'I': 'pgup', 'Q': 'pgdn',
                         'G': 'home', 'O': 'end'}.get(c2, '')
                    if not k:
                        continue
                elif not ST['edit'] and k.isalpha():
                    k = k.lower()
                if ST['edit']:
                    alive_ = on_edit_key(k)
                elif ST['page'] == 'config':
                    alive_ = on_config_key(k)
                elif ST['page'] == 'models':
                    alive_ = on_models_key(k)
                else:
                    alive_ = on_main_key(k)
                dirty = True
                if not alive_:
                    break
            time.sleep(0.04)
    finally:
        sys.stdout.write('\x1b[?25h\x1b[0m\x1b[2J\x1b[H')
        sys.stdout.flush()
        try:
            os.remove(HWND_FILE)
        except Exception:
            pass


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        sys.stdout.write('\x1b[?25h\x1b[2J\x1b[H')
        traceback.print_exc()
        input('\n按回车关闭...')
