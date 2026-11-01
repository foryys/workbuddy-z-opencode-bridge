# -*- coding: utf-8 -*-
"""OpenCode Go 的公共零件：配置存取、剪贴板、开机自启、代理启停。

tray.py（托盘）和 tui.py（控制台）都从这里拿，避免两份逻辑各写一遍。

**本项目不保存任何密钥。**
钥匙只存在 WorkBuddy 里，客户端每次请求带着 `Authorization` 头过来，
代理原样转发。所以本项目手里没有可泄露的东西——密钥泄不泄露，不归本项目管。
config.json 里只有上游地址、模型名、别名表、端口这几项。
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

# ---------------------------------------------------------------- DPAPI 解密
# 只剩解密：只用于「用户从旧版本升上来」时剥掉历史密文。
# 加密那半已删——本项目不再存任何密钥（钥匙归 WorkBuddy）。
# 留到下一个大版本就可以整段去掉。
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


def unprotect(b64):
    """旧版密文解密。只给「从旧版本升上来」时剥历史密文用。

    新配置里不会再写密文。读不出来（换了账号/机器）返回空串，
    调用方照常往下走，不当错误——反正本项目也不再依赖它。
    """
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
def defaults():
    return {
        'upstream': DEFAULT_UPSTREAM,
        'model': 'deepseek-v4.1-flash',
        'model_aliases': {},
        'port': PORT,
    }


def load():
    cfg = defaults()
    raw = {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        pass
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None and k not in ('local_enc', 'local_key', 'key_enc'):
                cfg[k] = v
    return cfg


def save(cfg):
    """原子写：先写临时文件再替换，避免掉电时把配置写成半截。

    只写这几项，**不写任何密钥**。旧版本留下的 key_enc / local_enc
    会在这里自然消失——本项目不再持有密钥，也就没有可泄露的东西。
    """
    out = {}
    for k, v in cfg.items():
        if k in ('local_key', 'local_enc', 'key_enc'):
            continue
        out[k] = v
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def stale_secrets(cfg=None):
    """config.json 里还留着旧版本的密文吗。给「升级后清一次」用。"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return False
    return bool(raw.get('key_enc') or raw.get('local_enc'))


def mask(secret):
    """sk-abcd…wxyz 这种形状，够认出是哪把钥匙，又不至于泄露。

    本项目已不存密钥，这个函数只留给「显示旧版本残留」和日志打码。
    """
    if not secret:
        return '(未设置)'
    if len(secret) <= 12:
        return secret[:3] + '*' * max(1, len(secret) - 5) + secret[-2:]
    return secret[:7] + '*' * 6 + secret[-4:]


# ------------------------------------------------- 与 WorkBuddy 的模型清单对接
def wb_config_dir():
    """WorkBuddy 的配置目录——跟客户端自己那套规则走，不写死。

    为什么不能直接拼 `~/.workbuddy`：客户端认两个环境变量改这个目录。
      1. WORKBUDDY_CONFIG_DIR / CODEBUDDY_CONFIG_DIR —— 整体挪到别处；
      2. WORKBUDDY_INSTANCE_NUMBER=N —— 多开用，目录变成 `~/.workbuddy-N`。
    走错目录的后果是静默的：条目写进去了、文件也变了，但客户端根本不读，
    界面没反应、也不报错，只能靠人猜。所以这里必须与客户端同源。
    注意「安装路径」跟这个无关：程序装哪个盘都行，配置永远在用户目录下。
    """
    for k in ('WORKBUDDY_CONFIG_DIR', 'CODEBUDDY_CONFIG_DIR'):
        v = str(os.environ.get(k) or '').strip()
        if v:
            return v
    n = str(os.environ.get('WORKBUDDY_INSTANCE_NUMBER') or '').strip()
    # homedir 优先；个别环境没有 USERPROFILE，退回 HOME
    home = os.path.expanduser('~') or os.environ.get('HOME') or ''
    return os.path.join(home, '.workbuddy' + ('-' + n if n else ''))


MODELS_JSON = os.path.join(wb_config_dir(), 'models.json')
LOCAL_PREFIX = 'sk-ocg-'
# 本机这条在 WorkBuddy 里的 id 后缀。要用它的原因是客户端按 id 去重：
# 两条都填真名时下拉里只剩一条（显示名不同也救不回来，10-30 实测过）。
SUFFIX = '-oc'


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


def legacy_secret_warning():
    """旧版本在本机留过密文时，给一句提醒。

    升上来的老用户 config.json 里可能还有 key_enc / local_enc。
    本项目已不存密钥，下一次保存就会自然清掉；这里只是让他知道一声，
    免得以为「我的钥匙被偷了/丢了」。
    """
    if not stale_secrets():
        return ''
    return '旧版本在本机存的密钥残留，已随本次保存清除（本项目不再存密钥）'


# ------------------------------------------- 把本机代理那条写进 WorkBuddy（自动配置）
# 为什么需要：客户端设置里那个「模型名称」框填的就是 id，没有单独填显示名的
# 地方；而它按 id 去重，两条 id 一样，下拉里就只剩一条。手工做这件事要改 id、
# 改名字、填地址、贴密钥四步，还得知道「保存时名字会被 id 冲掉」这个坑。
# 这里一次做完，而且可以反复执行。

def _read_models():
    """返回 (条目列表, 整个容器)。容器是 list 或含数组的 dict，写回时整份写。"""
    with open(MODELS_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, data
    if isinstance(data, dict):
        for k in ('models', 'list', 'items'):
            v = data.get(k)
            if isinstance(v, list):
                return v, data
    raise ValueError('models.json 结构不认识（既不是数组，也没有 models 数组）')


def _write_models(container):
    """整份写回：先备份、写临时文件、原子替换。返回备份文件名（原来没文件则 None）。

    备份名带序号避让：同一秒里连按两次 [W] 时，不加序号就会把第一次那份
    「写之前的原始件」覆盖掉，等于没备份。
    """
    bak = None
    if os.path.exists(MODELS_JSON):
        stamp = time.strftime('%Y%m%d-%H%M%S')
        bak = MODELS_JSON + '.bak-' + stamp
        k = 2
        while os.path.exists(bak):
            bak = '%s.bak-%s-%d' % (MODELS_JSON, stamp, k)
            k += 1
        shutil.copy2(MODELS_JSON, bak)
    d = os.path.dirname(MODELS_JSON)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = MODELS_JSON + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(container, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MODELS_JSON)
    return bak


def local_base_url(cfg=None):
    cfg = cfg or load()
    return 'http://127.0.0.1:%d/v1' % int(cfg.get('port') or PORT)


def is_our_url(url, cfg=None):
    """这条配置是不是指向本机代理 —— 只认端口，不认名字。"""
    cfg = cfg or load()
    p = int(cfg.get('port') or PORT)
    u = str(url or '')
    return ('127.0.0.1:%d' % p) in u or ('localhost:%d' % p) in u


def display_name(seed, tag='OC'):
    """从同一个模型那条的显示名派生本机这条。

    先去掉已有的 `OD · ` / `OC · ` 前缀再加回去，反复跑不会叠成
    `OC · OC · …`。
    """
    s = str(seed or '').strip()
    if ' · ' in s:
        s = s.split(' · ', 1)[1].strip()
    return ('%s · %s' % (tag, s)) if s else tag


def choose_model_id(base, items, cfg=None):
    """挑一个不与别人撞的 id：撞了就从 `-oc` 加到 `-oc2`、`-oc3`……

    判据是「别的地址占了这个 id」，不是「这名字看着像我的」——只认 url。
    id 撞了，请求会发错地方，而界面上两条都还在，最难查。
    """
    taken = {str(it.get('id') or '') for it in items
             if isinstance(it, dict) and not is_our_url(it.get('url'), cfg)}
    if base not in taken:
        return base
    cand = base + SUFFIX
    k = 2
    while cand in taken:
        cand = '%s%s%d' % (base, SUFFIX, k)
        k += 1
    return cand


def ensure_workbuddy_entry(cfg=None):
    """往 WorkBuddy 的模型清单里写（或修正）本机代理那一条。

    返回 (是否有改动, 给人看的一句话)。三条自我约束：
      1. 只动指向本机端口的那一条，别人的条目一字不改；
      2. id 被占就自动加后缀避让，并把同一份映射写进 model_aliases ——
         两边同源，才不会出现「客户端叫 A、代理只认 B」；
      3. 写前备份、写后回读，对不上立刻还原。
    幂等：已经是对的就一次都不写文件。
    """
    cfg = cfg or load()

    base = str(cfg.get('model') or '').strip() or 'deepseek-v4.1-flash'
    ours_url = local_base_url(cfg)

    try:
        items, box = _read_models()
    except FileNotFoundError:
        if not os.path.isdir(wb_config_dir()):
            # 客户端还没跑过，配置目录都不在。这时候建一个空的也没人读——
            # 客户端下次启动会按自己的规则初始化，反而可能覆盖我们的。
            return False, ('WorkBuddy 还没初始化过：先打开一次客户端，让它把配置目录建出来，再按 [W]')
        items = []
        box = items                      # 客户端还没建过自定义模型，我们建一个
    except Exception as e:
        return False, '读不了模型清单：%s' % e

    existing = next((it for it in items
                     if isinstance(it, dict) and is_our_url(it.get('url'), cfg)), None)
    sample = next((it for it in items
                   if isinstance(it, dict) and str(it.get('id')) == base), None)

    foreign = {str(it.get('id') or '') for it in items
               if isinstance(it, dict) and not is_our_url(it.get('url'), cfg)}
    cur = str((existing or {}).get('id') or '').strip()
    if cur and cur not in foreign:
        chosen = cur                     # 沿用现有 id，别让用户的选择失效
    else:
        chosen = choose_model_id(base, items, cfg)

    # 能力字段（是否支持工具/图片/思考、思考档位）照抄同一个模型那条，
    # 不靠猜。已有条目优先，这样以后客户端多出字段也不会被我们抹掉。
    seed = existing or sample or {}
    # 显示名从哪来：现成的优先。但它若已被客户端冲成 id（名字和 id 一个样），
    # 就不能拿它当种子——否则会修成「OC · deepseek-v4.1-flash-oc」这种废话。
    # 这时退回同一个模型那条真名的显示名。
    # 全新机器上一条参照都没有：不加前缀，直接用 id 本身，别造出
    # 「OC · deepseek-v4.1-flash」这种既长又没信息量的名字。
    name_seed = str(seed.get('name') or '').strip()
    ref = str((sample or {}).get('name') or '').strip()
    if name_seed and name_seed not in (base, chosen, str(seed.get('id') or '')):
        name = display_name(name_seed)
    elif ref:
        name = display_name(ref)
    else:
        name = chosen
    ent = {'id': chosen,
           'name': name,
           'vendor': str(seed.get('vendor') or 'Custom')}
    for k, v in seed.items():
        if k not in ('id', 'name', 'vendor', 'url', 'apiKey'):
            ent[k] = v
    ent['url'] = ours_url
    # 密钥栏：本机一律不碰。
    #   原本有值（用户填的 opencode 真钥匙）→ 原样保留，一个字都不改；
    #   原本空着 → 留空，等用户自己填。
    # 本项目不生成令牌、不写密钥，config.json 里也没有任何密钥。
    old_key = str((existing or {}).get('apiKey') or '').strip()
    ent['apiKey'] = old_key

    body_changed = existing is None or existing != ent

    al = dict(cfg.get('model_aliases') or {})
    al = {k: v for k, v in al.items() if not (v == base and k != chosen)}
    if chosen != base:
        al[chosen] = base
    alias_changed = al != (cfg.get('model_aliases') or {})

    if not body_changed and not alias_changed:
        return False, 'WorkBuddy 里那条已经是对的，没动文件'

    bak = None
    if body_changed:
        if existing is not None:
            items[items.index(existing)] = ent
        else:
            items.append(ent)
        try:
            bak = _write_models(box)
        except Exception as e:
            return False, '写不进模型清单：%s' % e
        try:                             # 回读：能解析、条目在、密钥与地址对得上
            back, _ = _read_models()
            got = next((it for it in back if str(it.get('id')) == chosen), None)
            if not got:
                raise ValueError('回读找不到 %s' % chosen)
            if str(got.get('apiKey')) != ent['apiKey']:
                raise ValueError('回读密钥不一致')
            if str(got.get('url')) != ours_url:
                raise ValueError('回读地址不一致')
        except Exception as e:
            if bak:
                shutil.copy2(bak, MODELS_JSON)
            return False, '写完回读对不上，已还原（%s）' % e

    if alias_changed:
        cfg['model_aliases'] = al
        save(cfg)

    msg = '已写入 WorkBuddy：%s，名字「%s」' % (chosen, ent['name'])
    if chosen != base:
        msg += '，发上游前自动改回 %s' % base
    msg += ('。钥匙那栏保留你填的' if old_key
            else '。钥匙那栏空着，请填 opencode 里那把真钥匙')
    if bak:
        msg += '；原文件备份 %s' % os.path.basename(bak)
    return True, msg + '。重启 WorkBuddy 生效'


def check_workbuddy_entry(cfg=None):
    """看 WorkBuddy 里那条现在什么状态，给界面做提示。

    返回 (状态, 一句话)。状态：ok / missing / drift / conflict / nofile / unreadable
    """
    cfg = cfg or load()
    base = str(cfg.get('model') or '').strip() or 'deepseek-v4.1-flash'
    try:
        items, _ = _read_models()
    except FileNotFoundError:
        return 'nofile', 'WorkBuddy 还没建过模型清单'
    except Exception:
        return 'unreadable', '模型清单读不了，先按 [W] 重写一次'

    ent = next((it for it in items
                if isinstance(it, dict) and is_our_url(it.get('url'), cfg)), None)
    if not ent:
        return 'missing', 'WorkBuddy 里还没有本机这条，按 [W] 写入'

    got_id = str(ent.get('id') or '')
    got_name = str(ent.get('name') or '')
    want = display_name(got_name or base)
    if got_name != want:                 # 客户端保存时用 id 覆盖了名字
        return 'drift', '显示名被客户端冲成了 %s，按 [W] 修回' % got_name
    if got_id != base and (cfg.get('model_aliases') or {}).get(got_id) != base:
        return 'conflict', '%s 没进别名表，上游会说模型不可用，按 [W] 修' % got_id
    k = str(ent.get('apiKey') or '').strip()
    if not k:
        return 'nokey', '条目在，密钥那栏空着 —— 请转到 WorkBuddy 上填写对应的 opencode 套餐密钥'
    if is_local_token(k):                # 旧版本遗留的令牌，本项目已不再签发
        return 'nokey', '那栏是旧版本的令牌，本项目已不签发 —— 改填 opencode 的真钥匙'
    return 'ok', '已配好（%s，钥匙在 WorkBuddy）' % got_name


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
    cands = []
    for k in ('ProgramFiles', 'ProgramFiles(x86)'):
        d = os.environ.get(k)
        if d:
            cands.append(os.path.join(d, 'nodejs', 'node.exe'))
    for p in cands:
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
    # 明确清掉这两项：本项目不保存、也不注入任何密钥。
    # 不清理的话，环境里若恰好有残留（旧版本、别的工具设过），
    # 代理会悄悄用上，出问题时谁都说不清钥匙从哪来的。
    env.pop('OPENCODE_API_KEY', None)
    env.pop('PROXY_TOKEN', None)
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


def wb_model_id(cfg=None):
    """WorkBuddy 里该填哪个 id：已经写过就用那条的，没写过就按避让规则算。

    手抄路径和自动写入必须给同一个答案，否则用户按抄的去填，又和自动写的
    那条撞上。
    """
    cfg = cfg or load()
    base = str(cfg.get('model') or '').strip() or 'deepseek-v4.1-flash'
    try:
        items, _ = _read_models()
    except Exception:
        return base
    ent = next((it for it in items
                if isinstance(it, dict) and is_our_url(it.get('url'), cfg)), None)
    if ent and str(ent.get('id') or '').strip():
        return str(ent['id']).strip()
    return choose_model_id(base, items, cfg)


def access_block(cfg=None):
    """手填时要用的那几项。密钥不在里面——本项目不碰钥匙。"""
    cfg = cfg or load()
    lines = ['OpenCode Go · 填进 WorkBuddy 的自定义模型',
             '接口地址  http://127.0.0.1:%d/v1/chat/completions'
             % int(cfg.get('port') or PORT)]
    want = wb_model_id(cfg)
    if want:
        lines.append('模型名称  %s' % want)
    lines.append('接口密钥  请转到 WorkBuddy 上填写对应的 opencode 套餐密钥')
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

