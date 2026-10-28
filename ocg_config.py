# -*- coding: utf-8 -*-
"""OpenCode Go 的公共零件：配置存取、本机加密、剪贴板、开机自启、代理启停。

tray.py（托盘）和 tui.py（控制台）都从这里拿，避免两份逻辑各写一遍。

密钥存放策略：
  config.json 里只放 key_enc —— CryptProtectData 加过密的 Base64。
  这把锁绑「当前 Windows 账号 + 当前机器」，换个账号或把文件拷到别的电脑都解不开。
  代理进程需要的明文钥匙只通过环境变量传进去，不落盘。
"""

import base64
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from ctypes import wintypes as wt

# 必须以本文件位置为准，不能用 sys.argv[0]：
# 那样从别处 import（自检脚本、python -c）时配置路径会跑到调用方目录，
# 读不到真配置就只能发新令牌 —— 表现是"已填的密钥莫名其妙失效"。
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, 'config.json')
PORT = int(os.environ.get('PORT', '8787'))
BASE = 'http://127.0.0.1:%d' % PORT
DEFAULT_UPSTREAM = 'https://opencode.ai/inference/go'
PROXY = os.path.join(HERE, 'opencode-go-proxy.mjs')

# ---------------------------------------------------------------- DPAPI
kernel32 = ctypes.windll.kernel32
crypt32 = ctypes.windll.crypt32


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _take(blob):
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


def protect(text):
    """明文 -> Base64 密文。失败返回空串，调用方要能接受。"""
    if not text:
        return ''
    try:
        src, _keep = _blob(text.encode('utf-8'))
        out = DATA_BLOB()
        ok = crypt32.CryptProtectData(ctypes.byref(src), None, None, None,
                                      None, 0, ctypes.byref(out))
        if not ok:
            return ''
        return base64.b64encode(_take(out)).decode('ascii')
    except Exception:
        return ''


def unprotect(b64):
    if not b64:
        return ''
    try:
        raw = base64.b64decode(b64)
        src, _keep = _blob(raw)
        out = DATA_BLOB()
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None,
                                        None, 0, ctypes.byref(out))
        if not ok:
            return ''
        return _take(out).decode('utf-8', 'replace')
    except Exception:
        return ''


# ---------------------------------------------------------------- 配置
def _new_token():
    return 'sk-ocg-' + uuid.uuid4().hex[:32]


def defaults():
    return {
        'upstream': DEFAULT_UPSTREAM,
        'model': 'deepseek-v4.1-flash',
        'local_key': _new_token(),
        'key_enc': '',
        'port': PORT,
    }


def load():
    """内存里的 cfg 用明文（要显示、要复制），落盘的永远是密文。

    local_enc 解不开（换了机器或换了账号）就发一把新的本地令牌，
    否则客户端拿着旧令牌来，代理认不出，看起来像"代理坏了"。
    """
    cfg = defaults()
    raw = {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        pass
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None and k not in ('local_enc', 'local_key'):
                cfg[k] = v
        if raw.get('local_enc'):
            cfg['local_key'] = unprotect(raw['local_enc']) or _new_token()
        elif raw.get('local_key'):          # 旧版明文，读进来，下次保存就转密文
            cfg['local_key'] = raw['local_key']
    if not cfg.get('local_key'):
        cfg['local_key'] = _new_token()
    return cfg


def save(cfg):
    """原子写：先写临时文件再替换，避免掉电时把配置写成半截。

    两把钥匙都只存密文：
      key_enc   —— 上游真钥匙（从 opencode 拿的），代理要用
      local_enc —— 本地令牌（给 WorkBuddy 填的），本身就是"密码"
    明文一个都不进文件。
    """
    out = {}
    for k, v in cfg.items():
        if k in ('local_key', 'local_enc'):
            continue
        out[k] = v
    lk = cfg.get('local_key') or ''
    if not lk:
        lk = _new_token()
        cfg['local_key'] = lk
    out['local_enc'] = protect(lk)
    out['key_enc'] = cfg.get('key_enc') or ''
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def has_upstream(cfg):
    return bool(cfg.get('key_enc'))


def rekey(cfg):
    """换一把新的本地令牌。随时可换，但换完老客户端会 401，要重新粘一次。"""
    cfg['local_key'] = _new_token()
    return cfg['local_key']


def get_key(cfg):
    return unprotect(cfg.get('key_enc', ''))


def set_key(cfg, plain):
    cfg['key_enc'] = protect(plain.strip()) if plain.strip() else ''
    return cfg


def mask(secret):
    """sk-abcd…wxyz 这种形状，够认出是哪把钥匙，又不至于泄露。"""
    if not secret:
        return '(未设置)'
    if len(secret) <= 12:
        return secret[:3] + '*' * max(1, len(secret) - 5) + secret[-2:]
    return secret[:7] + '*' * 6 + secret[-4:]


# ------------------------------------------------- 从 WorkBuddy 回收旧钥匙
# 场景：WorkBuddy 里早就填过真钥匙，代理一直是「透传」在用它。
# 后来 opencode 后台不再显示那把钥匙，用户手头拿不到，但机器上还留着一份。
# 与其让他去重新申请，不如就地把它搬进加密配置——顺手也把明文从
# WorkBuddy 的 models.json 里挪走。
MODELS_JSON = os.path.join(os.path.expanduser('~'), '.workbuddy', 'models.json')
LOCAL_PREFIX = 'sk-ocg-'


def is_local_token(value):
    return str(value or '').strip().startswith(LOCAL_PREFIX)


def detect_proxy_entry():
    """找出 WorkBuddy 里指向本代理的那条模型配置。找不到返 None。"""
    try:
        with open(MODELS_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None
    items = data if isinstance(data, list) else []
    if isinstance(data, dict):
        items = data.get('models') or data.get('list') or []
    want = ('127.0.0.1:%d' % PORT, 'localhost:%d' % PORT)
    for it in items:
        if not isinstance(it, dict):
            continue
        url = str(it.get('url') or '')
        if any(w in url for w in want):
            return it
    return None


def import_from_workbuddy(cfg, force=False):
    """把 WorkBuddy 里那把上游钥匙搬进 config.json（加密）。

    返回 (是否成功, 给人看的一句话)。
    """
    it = detect_proxy_entry()
    if not it:
        return False, 'WorkBuddy 里没有指向 %d 端口的模型配置' % PORT
    key = str(it.get('apiKey') or '').strip()
    if not key:
        return False, '那条模型配置里没有钥匙'
    if is_local_token(key):
        return False, 'WorkBuddy 用的已经是本地令牌，不需要导'
    if cfg.get('key_enc') and not force:
        return False, '已经存过上游密钥了，这次没覆盖'
    set_key(cfg, key)
    save(cfg)
    return True, '已从 WorkBuddy 导入上游密钥并加密保存（%s）' % mask(key)


def swap_workbuddy_token(cfg):
    """把 models.json 里那把真钥匙换成软件发的本地令牌。

    真钥匙从此只以密文存在 config.json，明文不再落在 WorkBuddy 的配置里。
    改动前先备份；写回是原子替换。
    """
    it = detect_proxy_entry()
    if not it:
        return False, 'WorkBuddy 里没有指向 %d 端口的模型配置' % PORT
    old = str(it.get('apiKey') or '').strip()
    if is_local_token(old):
        return False, 'WorkBuddy 用的已经是本地令牌了'
    if not cfg.get('key_enc'):
        return False, '先把上游密钥导入本机，否则换完就没人能换回来了'
    try:
        with open(MODELS_JSON, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        return False, '读不到 models.json：%s' % e

    stamp = time.strftime('%Y%m%d-%H%M%S')
    bak = MODELS_JSON + '.bak-' + stamp
    try:
        shutil.copy2(MODELS_JSON, bak)
    except Exception as e:
        return False, '备份失败，没敢动：%s' % e

    token = cfg.get('local_key') or ''
    hit = 0
    for it2 in (data if isinstance(data, list) else data.get('models') or []):
        if not isinstance(it2, dict):
            continue
        url = str(it2.get('url') or '')
        if ('127.0.0.1:%d' % PORT) in url or ('localhost:%d' % PORT) in url:
            it2['apiKey'] = token
            hit += 1
    if not hit:
        return False, '没找到要替换的条目'
    tmp = MODELS_JSON + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MODELS_JSON)
    except Exception as e:
        return False, '写入失败：%s' % e
    return True, 'WorkBuddy 里已换成令牌，原文件备份在 %s。重启 WorkBuddy 生效' % os.path.basename(bak)


# ---------------------------------------------------------------- 剪贴板
u32 = ctypes.windll.user32
u32.OpenClipboard.argtypes = [wt.HWND]
u32.SetClipboardData.restype = wt.HANDLE
u32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# 64 位下句柄是 8 字节，不声明 restype 会被截成 int32，
# GlobalLock 返回 0 然后 memmove 直接访问违例。
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]


def copy_text(text):
    """把文本塞进系统剪贴板（纯 ctypes，不装 pyperclip）。"""
    if not text:
        return False
    data = text.replace('\r\n', '\n')
    size = (len(data) + 1) * 2
    for _ in range(6):                      # 别的程序占着剪贴板时重试几次
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, ctypes.create_unicode_buffer(data), size)
        kernel32.GlobalUnlock(h)
        return bool(u32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        u32.CloseClipboard()


u32.GetClipboardData.restype = wt.HANDLE
u32.GetClipboardData.argtypes = [wt.UINT]


def read_text():
    """读系统剪贴板里的纯文本。

    为什么需要：控制台里按 Ctrl+V，有些终端（含 CREATE_NEW_CONSOLE 起的
    conhost）不会自己去粘贴，而是把 0x16 这个控制字符交给程序。我们的
    输入框只收 >= 空格的字符，于是 0x16 被丢掉 —— 用户以为粘上了，
    实际上一个字都没进去。自己读剪贴板就绕开了终端行为差异。
    """
    for _ in range(6):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ''
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ''
        p = kernel32.GlobalLock(h)
        if not p:
            return ''
        try:
            return ctypes.wstring_at(p) or ''
        finally:
            kernel32.GlobalUnlock(h)
    except Exception:
        return ''
    finally:
        u32.CloseClipboard()


def clean_key(text):
    """把用户粘过来的东西收拾成一把钥匙。

    实际粘贴的东西五花八门：带首尾换行、带引号、整行
    `Authorization: Bearer sk-xxx`、`export KEY=sk-xxx`、或者不小心
    复制了两遍。这些都不该让用户自己删，否则填进去的是一串带杂质的
    字符串，代理拿去请求上游必然 401，而界面上看着"已填"。
    """
    s = (text or '').strip().strip('`"\'').strip()
    if not s:
        return ''

    parts = [p for p in re.split(r'\s+', s) if p]
    if len(parts) > 1 or '=' in s:
        cand = []
        for p in parts:
            if '=' in p:                     # KEY=sk-xxx 取等号后面
                p = p.split('=', 1)[1]
            p = p.strip('`"\'').strip()
            if p.lower().startswith(('sk-', 'sk_')):
                cand.append(p)
        if cand:
            s = cand[-1]                     # 重复粘贴时取最后一份
        elif parts:
            s = parts[-1].split('=', 1)[-1]
    return s.strip('`"\'').strip()


# ---------------------------------------------------------------- 开机自启
def startup_dir():
    return os.path.join(os.environ['APPDATA'],
                        r'Microsoft\Windows\Start Menu\Programs\Startup')


def startup_lnk():
    return os.path.join(startup_dir(), 'OpenCode Go 托盘.lnk')


def autostart_on():
    return os.path.exists(startup_lnk())


def set_autostart(on):
    """靠「启动」文件夹里有没有那个快捷方式来决定，等价、可见、好恢复。"""
    try:
        lnk = startup_lnk()
        if on:
            src = os.path.join(HERE, 'OpenCodeGo-自启.lnk')
            if not os.path.exists(src):
                src = os.path.join(HERE, 'OpenCodeGo-托盘.lnk')
            if not os.path.exists(src):
                return autostart_on(), '缺少快捷方式模板，先运行一次 _make-shortcut.py'
            shutil.copy2(src, lnk)
            return True, '开机自启：已开启'
        if os.path.exists(lnk):
            os.remove(lnk)
        return False, '开机自启：已关闭'
    except Exception as e:
        return autostart_on(), '切换失败：%s' % e


def toggle_autostart():
    return set_autostart(not autostart_on())


# ---------------------------------------------------------------- 代理启停
def find_node():
    p = shutil.which('node')
    if p:
        return p
    cands = glob.glob(os.path.expanduser(
        r'~\.workbuddy\binaries\node\versions\*\node.exe'))
    if cands:
        return sorted(cands)[-1]
    for p in (r'C:\Program Files\nodejs\node.exe',
              r'C:\Program Files (x86)\nodejs\node.exe'):
        if os.path.exists(p):
            return p
    return None


def alive(timeout=1.5):
    try:
        with urllib.request.urlopen(BASE + '/healthz', timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def status(timeout=1.6):
    try:
        with urllib.request.urlopen(BASE + '/status', timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception:
        return None


def proxy_env(cfg=None):
    """把配置翻译成代理进程的环境变量。明文钥匙只活在这个 env 里。"""
    cfg = cfg or load()
    env = dict(os.environ)
    env['TUI'] = '0'                       # 后台实例没有控制台，别跑按键界面
    env['PORT'] = str(cfg.get('port') or PORT)
    env['UPSTREAM_BASE'] = cfg.get('upstream') or DEFAULT_UPSTREAM
    env['PROXY_TOKEN'] = cfg.get('local_key') or ''
    key = get_key(cfg)
    if key:
        env['OPENCODE_API_KEY'] = key
    else:
        env.pop('OPENCODE_API_KEY', None)
    return env


def start(log=print, wait=10.0):
    node = find_node()
    if not node:
        log('找不到 node，无法拉起代理')
        return False
    if alive(1.0):
        return True
    env = proxy_env()
    flags = 0x00000008 | 0x08000000        # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        with open(os.path.join(HERE, 'proxy-tray.out'), 'ab') as out:
            subprocess.Popen([node, PROXY], cwd=HERE, env=env,
                             stdout=out, stderr=out, creationflags=flags,
                             close_fds=True)
    except Exception as e:
        log('拉起代理失败: %r' % (e,))
        return False
    t0 = time.time()
    while time.time() - t0 < wait:
        if alive(0.8):
            log('代理已就绪 (端口 %d)' % PORT)
            return True
        time.sleep(0.4)
    log('拉起后 %.0f 秒仍无应答' % wait)
    return False


def stop(log=print):
    """按端口找进程杀掉。比按镜像名更准——不会误伤别的 node。"""
    pids = []
    try:
        out = subprocess.run(['netstat', '-ano'], capture_output=True,
                             creationflags=0x08000000, timeout=15)
        for line in out.stdout.decode('ascii', 'replace').splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper().startswith('TCP') \
                    and parts[1].endswith(':%d' % PORT) \
                    and parts[3].upper() == 'LISTENING':
                pids.append(parts[4])
    except Exception as e:
        log('查端口失败: %r' % (e,))
        return False
    for pid in set(pids):
        try:
            subprocess.run(['taskkill', '/PID', pid, '/F'],
                           capture_output=True, creationflags=0x08000000,
                           timeout=15)
            log('已结束占用 %d 的进程 %s' % (PORT, pid))
        except Exception as e:
            log('结束 %s 失败: %r' % (pid, e))
    t0 = time.time()
    while time.time() - t0 < 5:
        if not alive(0.6):
            return True
        time.sleep(0.3)
    return not alive(0.6)


def restart(log=print):
    stop(log)
    time.sleep(0.4)
    return start(log)


def access_block(cfg=None):
    """复制给 WorkBuddy 用的那段文本 —— 这是软件"吐"出来的东西，不是用户填的。"""
    cfg = cfg or load()
    lines = ['OpenCode Go · 填进 WorkBuddy 的自定义模型',
             '接口地址  http://127.0.0.1:%d/v1/chat/completions'
             % int(cfg.get('port') or PORT),
             '接口密钥  %s' % (cfg.get('local_key') or '')]
    if cfg.get('model'):
        lines.append('模型名称  %s' % cfg['model'])
    return '\n'.join(lines)


if __name__ == '__main__':
    """给 service.cmd 用的命令行入口。

    为什么要有它：批处理没法安全地把自己的目录拼进一段 Python 代码
    （路径结尾是反斜杠，塞进字符串就会吃掉引号）。让 Python 从自己的
    __file__ 推目录，就绕开了这个坑，而且启动时会带上解出来的密钥。
    """
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'start'
    if cmd == 'start':
        start(log=lambda s: None)
    elif cmd == 'stop':
        stop()
    elif cmd == 'status':
        d = status()
        print(access_block() if not d else
              '运行中 v%s，已跑 %.0f 分钟，成功 %s / %s'
              % (d.get('version'), d.get('uptimeMs', 0) / 60000.0,
                 d.get('ok'), d.get('total')))
    else:
        print('用法: ocg_config.py [start|stop|status]')

