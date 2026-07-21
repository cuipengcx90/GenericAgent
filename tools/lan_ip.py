#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动态探测本机局域网 IPv4，生成归档站访问 URL。

家庭 WiFi 走 DHCP，IP 每天可能变（曾写死 192.168.1.40，实际已漂到 .44），
所以归档链接不能写死。晨报/晚报/归档页都在生成时现探测当天 IP。

坑（见 output_archive_sop）：
- Clash TUN 会造出假地址 198.18.0.1，必须排除。
- macOS 上会有一堆虚拟 192.168.x.0/.3 (bridge/vmnet/utun)，只认默认路由网卡(en0)。
- 优先用默认路由网卡的 IPv4；取不到再从 socket 兜底。
"""
from __future__ import annotations

import subprocess


def _default_iface() -> str | None:
    try:
        out = subprocess.run(
            ['/sbin/route', '-n', 'get', 'default'],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('interface:'):
                return line.split(':', 1)[1].strip()
    except Exception:
        pass
    return None


def _iface_ipv4(iface: str) -> str | None:
    try:
        ip = subprocess.run(
            ['/usr/sbin/ipconfig', 'getifaddr', iface],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return ip or None
    except Exception:
        return None


def _socket_fallback() -> str | None:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        # 排除 Clash TUN 假地址
        if ip and not ip.startswith('198.18.'):
            return ip
    except Exception:
        pass
    return None


def get_lan_ip() -> str | None:
    """返回默认路由网卡的 IPv4；失败返回 None。"""
    iface = _default_iface()
    if iface:
        ip = _iface_ipv4(iface)
        if ip and not ip.startswith('198.18.'):
            return ip
    return _socket_fallback()


def archive_url(port: int = 8899) -> str:
    """归档站 URL。探测不到 IP 时回退到 localhost（本机仍可访问）。"""
    ip = get_lan_ip() or '127.0.0.1'
    return f'http://{ip}:{port}/'


if __name__ == '__main__':
    print(archive_url())
