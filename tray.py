# -*- coding: utf-8 -*-
"""OpenCode Go 托盘 —— 零依赖，只用 Python 标准库 + Windows 原生接口。

双击桌面的「OpenCode Go 启动器」：右下角出现图标，同时弹出控制台窗口。
开机自启走 --autostart 参数：只出图标，不弹窗口。

右键菜单三件事：打开控制台 / 开机自启（勾选项）/ 退出。
关掉控制台窗口不等于停服——代理还在跑，托盘也还在。
"""

import ctypes
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes as wt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ocg_config as C                      # noqa: E402

LOG = os.path.join(HERE, 'tray.log')
ICO = os.path.join(HERE, 'icon.ico')
TUI_HWND = os.path.join(HERE, 'tui.hwnd')
TUI_MUTEX = 'Local\\OpenCodeGoTui'
PORT = C.PORT


# ---------------------------------------------------------------- 日志
def log(msg):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


if sys.stdout is None:          # pythonw 下没有 stdout，别让 print 炸掉
    sys.stdout = open(LOG, 'a', encoding='utf-8')
    sys.stderr = sys.stdout


# ---------------------------------------------------------------- Win32
u = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM)

WM_USER = 0x0400
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TRAYICON = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
MF_STRING, MF_SEPARATOR, MF_CHECKED = 0x0, 0x800, 0x8
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0100, 0x0002

ID_STATUS, ID_AUTOSTART, ID_QUIT = 1, 2, 3


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint),
                ('style', ctypes.c_uint),
                ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int),
                ('hInstance', wt.HINSTANCE),
                ('hIcon', wt.HICON),
                ('hCursor', wt.HANDLE),
                ('hbrBackground', wt.HBRUSH),
                ('lpszMenuName', wt.LPCWSTR),
                ('lpszClassName', wt.LPCWSTR),
                ('hIconSm', wt.HICON)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint),
                ('hWnd', wt.HWND),
                ('uID', ctypes.c_uint),
                ('uFlags', ctypes.c_uint),
                ('uCallbackMessage', ctypes.c_uint),
                ('hIcon', wt.HICON),
                ('szTip', ctypes.c_wchar * 128),
                ('dwState', ctypes.c_uint),
                ('dwStateMask', ctypes.c_uint),
                ('szInfo', ctypes.c_wchar * 256),
                ('uVersion', ctypes.c_uint),
                ('szInfoTitle', ctypes.c_wchar * 64),
                ('dwInfoFlags', ctypes.c_uint),
                ('guidItem', ctypes.c_ubyte * 16),
                ('hBalloonIcon', wt.HICON)]


u.CreateWindowExW.restype = wt.HWND
u.CreateWindowExW.argtypes = [ctypes.c_uint, wt.LPCWSTR, wt.LPCWSTR,
                              ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                              wt.HINSTANCE, wt.LPVOID]
u.DefWindowProcW.restype = LRESULT
u.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]
u.LoadImageW.restype = wt.HANDLE
u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, ctypes.c_uint,
                         ctypes.c_int, ctypes.c_int, ctypes.c_uint]
u.CreatePopupMenu.restype = wt.HMENU
u.TrackPopupMenu.restype = ctypes.c_int
u.RegisterClassExW.restype = ctypes.c_ushort
u.RegisterWindowMessageW.restype = ctypes.c_uint
u.MessageBoxW.restype = ctypes.c_int

shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint,
                                      ctypes.POINTER(NOTIFYICONDATAW)]

state = {'hwnd': None, 'nid': None}
_wndproc_ref = None

IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040


def load_icon():
    """优先用自己画的 icon.ico；读不到再退回系统图标，别让托盘挂空。"""
    for size in (32, 16, 0):
        if os.path.exists(ICO):
            h = u.LoadImageW(None, ICO, IMAGE_ICON, size, size,
                             LR_LOADFROMFILE)
            if h:
                return h
    return u.LoadIconW(None, 32512)      # IDI_APPLICATION


def set_tip(text, balloon=False, title='OpenCode Go'):
    nid = state['nid']
    if not nid:
        return
    nid.szTip = text[:127]
    nid.uFlags = NIF_TIP
    if balloon:
        nid.szInfo = text[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = 0x00000040          # NIIF_INFO
        nid.uFlags |= NIF_INFO
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))


# ---------------------------------------------------------------- 控制台
def _python_exe():
    """sys.executable 是 pythonw.exe（无控制台），要换成同目录的 python.exe。"""
    exe = sys.executable or ''
    low = exe.lower()
    if low.endswith('pythonw.exe'):
        p = os.path.join(os.path.dirname(exe), 'python.exe')
        if os.path.exists(p):
            return p
    if low.endswith('python.exe'):
        return exe
    import shutil
    return shutil.which('python') or exe


def tui_running():
    """tui.py 启动时持有这个互斥体，有它说明窗口已经开着。"""
    kernel32.OpenMutexW.restype = wt.HANDLE
    kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    h = kernel32.OpenMutexW(0x00100000, False, TUI_MUTEX)   # SYNCHRONIZE
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def focus_tui():
    """能拉就拉到前台（经典 conhost 有效；Windows Terminal 里可能没反应）。"""
    ok = False
    try:
        with open(TUI_HWND) as f:
            h = int(f.read().strip())
        if h and u.IsWindow(h):
            u.ShowWindow(h, 9)                # SW_RESTORE
            u.SetForegroundWindow(h)
            ok = True
    except Exception:
        pass
    if ok:
        return True
    # 退一步：找一个标题含 OpenCode Go 的可见窗口
    try:
        found = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(h, p):
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            if 'OpenCode Go' in buf.value:
                found.append(h)
                return False
            return True

        u.EnumWindows(cb, 0)
        if found:
            u.ShowWindow(found[0], 9)
            u.SetForegroundWindow(found[0])
            return True
    except Exception:
        pass
    return False


def show_console():
    """弹出控制台；已经开着就只把它提上来，绝不开第二个窗口。"""
    if tui_running():
        if not focus_tui():
            log('控制台已在运行，但没能抢到前台')
        return
    script = os.path.join(HERE, 'tui.py')
    if not os.path.exists(script):
        u.MessageBoxW(None, '找不到 tui.py', 'OpenCode Go', 0x00000040)
        return
    try:
        import subprocess
        subprocess.Popen([_python_exe(), script], cwd=HERE,
                         creationflags=0x00000010,   # CREATE_NEW_CONSOLE
                         close_fds=True)
    except Exception as e:
        log('打开控制台失败: %r' % (e,))
        u.MessageBoxW(None, '打开控制台失败：%r' % (e,), 'OpenCode Go',
                      0x00000040)


# ---------------------------------------------------------------- 菜单
def do_quit():
    log('用户在托盘里选择退出')
    C.stop(log=log)
    nid = state['nid']
    if nid:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    u.DestroyWindow(state['hwnd'])


def popup_menu(hwnd):
    hmenu = u.CreatePopupMenu()
    u.AppendMenuW(hmenu, MF_STRING, ID_STATUS, '打开控制台')
    u.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
    flags = MF_STRING | (MF_CHECKED if C.autostart_on() else 0)
    u.AppendMenuW(hmenu, flags, ID_AUTOSTART, '开机自启')
    u.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
    u.AppendMenuW(hmenu, MF_STRING, ID_QUIT, '退出')
    u.SetForegroundWindow(hwnd)
    pt = wt.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    cmd = u.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                           pt.x, pt.y, 0, hwnd, None)
    u.PostMessageW(hwnd, 0, 0, 0)
    u.DestroyMenu(hmenu)
    if cmd == ID_STATUS:
        show_console()
    elif cmd == ID_AUTOSTART:
        _on, msg = C.toggle_autostart()
        log(msg)
        set_tip('OpenCode Go · ' + ('自启已开' if _on else '自启已关'))
    elif cmd == ID_QUIT:
        do_quit()


def wndproc(hwnd, msg, wparam, lparam):
    try:
        if msg == WM_TRAYICON:
            ev = lparam & 0xFFFF
            if ev == WM_LBUTTONUP:
                show_console()
            elif ev == WM_RBUTTONUP:
                popup_menu(hwnd)
            return 0
        if msg == WM_CLOSE:            # 没有可见窗口，但保险起见
            do_quit()
            return 0
        if msg == WM_DESTROY:
            u.PostQuitMessage(0)
            return 0
        if state.get('taskbar_msg') and msg == state['taskbar_msg']:
            # 资源管理器重启后图标会消失，重新挂上
            if state['nid']:
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(state['nid']))
            return 0
    except Exception:
        log('wndproc 异常:\n' + traceback.format_exc())
    return u.DefWindowProcW(hwnd, msg, wparam, lparam)


def watchdog():
    """代理被人为杀掉或自己崩了，重新拉起来（会带上最新配置）。"""
    while True:
        time.sleep(20)
        try:
            if not C.alive(1.2):
                log('探测到代理不在，尝试重新拉起')
                if C.start(log=log):
                    set_tip('OpenCode Go · 运行中')
        except Exception:
            log('看门狗异常:\n' + traceback.format_exc())


def quit_watcher():
    """控制台里按 X 时，它 SetEvent 这个命名事件，这里收到就让托盘退出。"""
    kernel32.CreateEventW.restype = wt.HANDLE
    kernel32.CreateEventW.argtypes = [wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, ctypes.c_uint]
    try:
        h = kernel32.CreateEventW(None, True, False, 'Local\\OpenCodeGoTrayQuit')
    except Exception:
        return
    if not h:
        log('创建退出事件失败')
        return
    while True:
        if kernel32.WaitForSingleObject(h, 0xFFFFFFFF) == 0:
            log('收到控制台的退出请求')
            if state.get('hwnd'):
                u.PostMessageW(state['hwnd'], WM_CLOSE, 0, 0)
            return


def expected_version():
    """代理文件里写的版本号。用来判断"跑着的那个是不是旧代码"。"""
    try:
        with open(os.path.join(HERE, 'opencode-go-proxy.mjs'), 'r',
                  encoding='utf-8') as f:
            for line in f:
                if 'const VERSION' in line:
                    return line.split("'")[1]
    except Exception:
        pass
    return ''


def ensure_fresh():
    """代理在跑但版本比文件旧（刚更新过代码）→ 重启一次，让改动生效。"""
    d = C.status(1.5)
    if not d:
        return
    want, have = expected_version(), str(d.get('version') or '')
    if want and have and want != have:
        log('代理是 v%s，文件是 v%s，重启一次' % (have, want))
        C.restart(log=log)
        set_tip('OpenCode Go · 运行中')


def main():
    want_console = '--autostart' not in sys.argv

    # 只允许一份托盘：已经在跑就不再挂第二个图标
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
    hmtx = kernel32.CreateMutexW(None, False, 'Local\\OpenCodeGoTray')
    if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        log('托盘已在运行；本次只当启动器用（要窗口就开窗口）')
        if want_console:
            show_console()
        return
    _ = hmtx

    log('托盘启动，端口 %d，want_console=%s' % (PORT, want_console))
    if not C.alive():
        C.start(log=log)
    else:
        log('代理本来就在跑，直接挂图标')
        ensure_fresh()

    global _wndproc_ref
    _wndproc_ref = WNDPROC(wndproc)

    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _wndproc_ref
    wc.hInstance = hinst
    wc.lpszClassName = 'OpenCodeGoTrayWnd'
    if not u.RegisterClassExW(ctypes.byref(wc)):
        log('RegisterClassEx 失败: %d' % kernel32.GetLastError())
        return

    hwnd = u.CreateWindowExW(0, 'OpenCodeGoTrayWnd', 'OpenCode Go', 0,
                             0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        log('CreateWindowEx 失败: %d' % kernel32.GetLastError())
        return
    state['hwnd'] = hwnd
    state['taskbar_msg'] = u.RegisterWindowMessageW('TaskbarCreated')

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAYICON
    nid.hIcon = load_icon()
    nid.szTip = 'OpenCode Go · 运行中'
    ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
    log('图标挂载 %s (cbSize=%d)' % ('成功' if ok else '失败',
                                     ctypes.sizeof(NOTIFYICONDATAW)))
    if not ok:
        return
    state['nid'] = nid

    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=quit_watcher, daemon=True).start()

    # 手动双击：先把窗口亮出来，用户才知道东西起来了
    if want_console:
        threading.Timer(0.6, show_console).start()

    msg = wt.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u.TranslateMessage(ctypes.byref(msg))
        u.DispatchMessageW(ctypes.byref(msg))
    log('托盘退出')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('托盘致命错误:\n' + traceback.format_exc())
