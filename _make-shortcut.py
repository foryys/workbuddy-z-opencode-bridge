# -*- coding: utf-8 -*-
"""生成两个 .lnk（走系统 IShellLink，不手写字节）。

  OpenCodeGo-托盘.lnk   pythonw tray.py               桌面那份，双击出窗口
  OpenCodeGo-自启.lnk   pythonw tray.py --autostart   开机自启模板，只出图标

目标是 pythonw.exe——它不带控制台，所以托盘本身不会闪黑框；
控制台窗口是 tray.py 主动用 python.exe + CREATE_NEW_CONSOLE 拉起来的。

用法：python _make-shortcut.py            只建模板
      python _make-shortcut.py --install  顺带铺到桌面和「启动」文件夹
"""
import ctypes
import io
import os
import shutil
import sys
import uuid
from ctypes import (POINTER, WINFUNCTYPE, byref, c_int, c_long, c_uint,
                    c_void_p, c_wchar_p)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TRAY = os.path.join(HERE, 'tray.py')
ICO = os.path.join(HERE, 'icon.ico')

CANDIDATES = [
    os.path.expanduser(r'~\AppData\Local\Programs\Python\Python312\pythonw.exe'),
    os.path.expanduser(r'~\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe'),
]
PYW = next((p for p in CANDIDATES if os.path.exists(p)), None)
if not PYW:
    sys.exit('找不到 pythonw.exe')

ole32 = ctypes.windll.ole32


class GUID(ctypes.Structure):
    _fields_ = [('b', ctypes.c_ubyte * 16)]


def G(s):
    g = GUID()
    ctypes.memmove(byref(g), uuid.UUID(s).bytes_le, 16)
    return g


CLSID_ShellLink = '{00021401-0000-0000-C000-000000000046}'
IID_IShellLinkW = '{000214F9-0000-0000-C000-000000000046}'
IID_IPersistFile = '{0000010B-0000-0000-C000-000000000046}'

ole32.CoInitializeEx.argtypes = [c_void_p, c_uint]
ole32.CoInitializeEx.restype = c_long
ole32.CoCreateInstance.argtypes = [c_void_p, c_void_p, c_uint, c_void_p,
                                   POINTER(c_void_p)]
ole32.CoCreateInstance.restype = c_long

ole32.CoInitializeEx(None, 2)


def build(dst, args='', desc=''):
    p = c_void_p()
    hr = ole32.CoCreateInstance(byref(G(CLSID_ShellLink)), None, 1,
                                byref(G(IID_IShellLinkW)), byref(p))
    if (hr & 0xFFFFFFFF) or not p.value:
        sys.exit('创建 IShellLink 失败 hr=0x%08X' % (hr & 0xFFFFFFFF))
    vt = ctypes.cast(p, POINTER(POINTER(c_void_p)))[0]

    def call(idx, restype, *argtypes):
        return WINFUNCTYPE(restype, c_void_p, *argtypes)(vt[idx])

    call(20, c_long, c_wchar_p)(p, PYW)                      # SetPath
    call(11, c_long, c_wchar_p)(p, args)                     # SetArguments
    call(9, c_long, c_wchar_p)(p, HERE)                      # SetWorkingDirectory
    call(7, c_long, c_wchar_p)(p, desc)                      # SetDescription
    icon = ICO if os.path.exists(ICO) else PYW
    call(17, c_long, c_wchar_p, c_int)(p, icon, 0)           # SetIconLocation
    call(15, c_long, c_int)(p, 1)                            # SetShowCmd

    pf = c_void_p()
    call(0, c_long, c_void_p, POINTER(c_void_p))(
        p, byref(G(IID_IPersistFile)), byref(pf))
    if not pf.value:
        sys.exit('拿不到 IPersistFile')
    pvt = ctypes.cast(pf, POINTER(POINTER(c_void_p)))[0]
    save = WINFUNCTYPE(c_long, c_void_p, c_wchar_p, c_int)(pvt[6])
    hr = save(pf, dst, 1)
    print('  %-24s %5d 字节  hr=0x%08X' % (os.path.basename(dst),
                                           os.path.getsize(dst),
                                           hr & 0xFFFFFFFF))


def startup_dir():
    return os.path.join(os.environ['APPDATA'],
                        r'Microsoft\Windows\Start Menu\Programs\Startup')


def main():
    print('pythonw: %s' % PYW)
    print('图标:    %s' % (ICO if os.path.exists(ICO) else '(缺 icon.ico)'))
    tray_lnk = os.path.join(HERE, 'OpenCodeGo-托盘.lnk')
    auto_lnk = os.path.join(HERE, 'OpenCodeGo-自启.lnk')
    build(tray_lnk, '"%s"' % TRAY, 'OpenCode Go — 代理常驻，右下角托盘图标')
    build(auto_lnk, '"%s" --autostart' % TRAY, 'OpenCode Go — 开机静默启动')

    if '--install' not in sys.argv:
        return
    desk = os.path.join(os.path.expanduser('~'), 'Desktop')
    if not os.path.isdir(desk):
        desk = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
    dst = os.path.join(desk, 'OpenCode Go 启动器.lnk')
    shutil.copy2(tray_lnk, dst)
    print('  桌面 -> %s' % dst)
    was_on = os.path.exists(os.path.join(startup_dir(), 'OpenCode Go 托盘.lnk'))
    if was_on:
        shutil.copy2(auto_lnk, os.path.join(startup_dir(), 'OpenCode Go 托盘.lnk'))
        print('  开机自启模板已同步更新（本来就开着，保持开）')


if __name__ == '__main__':
    main()
