"""
psutil_utils.py — 系统实时监控工具封装
依赖: psutil
功能:
  - cpu_snapshot()          : CPU使用率/核心数/频率/每核使用率
  - memory_snapshot()       : 内存总量/可用/已用/百分比 + swap
  - disk_snapshot()         : 磁盘分区/使用量/IO计数器
  - network_snapshot()      : 网络接口/连接数/IO计数器
  - process_snapshot(pid)   : 单进程详情（CPU/内存/线程/打开文件等）
  - top_processes(n, sort_by): 按CPU或内存排序的Top N进程
  - system_snapshot()       : 全量快照（CPU+内存+磁盘+网络+启动时间）
  - watch_resource(interval, callback, duration): 定期采集并回调

坑:
  - macOS 上 cpu_freq() 可能返回 None（Apple Silicon 不暴露频率）
  - disk_io_counters() 在 macOS 某些版本返回 None
  - process 信息需要权限，某些进程会 AccessDenied
  - per_cpu 使用率首次调用返回 0.0（需 interval > 0）
  - 网络连接 net_connections() 需 root 权限，普通用户可能报错
"""

from __future__ import annotations
import datetime
import json
import time
from typing import Any, Callable, Dict, List, Optional, Union

import psutil


# ── 内部工具 ──────────────────────────────────────────────────

def _ts() -> str:
    """ISO 格式时间戳"""
    return datetime.datetime.now().isoformat(timespec='seconds')


def _safe(func, *args, default=None, **kwargs):
    """安全调用，捕获权限/不支持错误"""
    try:
        return func(*args, **kwargs)
    except (psutil.AccessDenied, psutil.NoSuchProcess, NotImplementedError,
            PermissionError, RuntimeError, AttributeError, FileNotFoundError, OSError):
        return default


def _bytes_to_gb(b: int) -> float:
    """字节转 GB，保留2位小数"""
    return round(b / (1024 ** 3), 2)


def _bytes_to_mb(b: int) -> float:
    """字节转 MB，保留2位小数"""
    return round(b / (1024 ** 2), 2)


# ── 公开 API ──────────────────────────────────────────────────

def cpu_snapshot(per_cpu: bool = False, interval: float = 0.5) -> Dict[str, Any]:
    """
    CPU 快照。

    Args:
        per_cpu: 是否返回每核使用率
        interval: 采样间隔秒数（cpu_percent 需要）

    Returns:
        {timestamp, cpu_percent, cpu_count_logical, cpu_count_physical,
         cpu_freq_mhz, per_cpu_percent?, load_avg}
    """
    result = {
        'timestamp': _ts(),
        'cpu_percent': psutil.cpu_percent(interval=interval),
        'cpu_count_logical': psutil.cpu_count(logical=True),
        'cpu_count_physical': psutil.cpu_count(logical=False),
    }

    # CPU 频率（Apple Silicon 可能不支持）
    freq = _safe(psutil.cpu_freq)
    if freq:
        result['cpu_freq_mhz'] = {
            'current': round(freq.current, 1),
            'min': round(freq.min, 1),
            'max': round(freq.max, 1),
        }
    else:
        result['cpu_freq_mhz'] = None

    if per_cpu:
        result['per_cpu_percent'] = psutil.cpu_percent(interval=interval, percpu=True)

    # Load average (1, 5, 15 min)
    try:
        result['load_avg'] = [round(x, 2) for x in psutil.getloadavg()]
    except (AttributeError, OSError):
        result['load_avg'] = None

    return result


def memory_snapshot() -> Dict[str, Any]:
    """
    内存快照（物理内存 + swap）。

    Returns:
        {timestamp, ram: {total_gb, available_gb, used_gb, percent},
         swap: {total_gb, used_gb, free_gb, percent}}
    """
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()

    return {
        'timestamp': _ts(),
        'ram': {
            'total_gb': _bytes_to_gb(vm.total),
            'available_gb': _bytes_to_gb(vm.available),
            'used_gb': _bytes_to_gb(vm.used),
            'percent': vm.percent,
        },
        'swap': {
            'total_gb': _bytes_to_gb(sw.total),
            'used_gb': _bytes_to_gb(sw.used),
            'free_gb': _bytes_to_gb(sw.free),
            'percent': sw.percent,
        },
    }


def disk_snapshot(all_partitions: bool = False) -> Dict[str, Any]:
    """
    磁盘快照（分区使用量 + IO计数器）。

    Args:
        all_partitions: True=包含所有分区，False=只含物理磁盘

    Returns:
        {timestamp, partitions: [{device, mountpoint, fstype, total_gb, used_gb, free_gb, percent}],
         io_counters: {read_mb, write_mb, read_count, write_count}}
    """
    parts = psutil.disk_partitions(all=all_partitions)
    partitions = []
    for p in parts:
        try:
            usage = psutil.disk_usage(p.mountpoint)
            partitions.append({
                'device': p.device,
                'mountpoint': p.mountpoint,
                'fstype': p.fstype,
                'total_gb': _bytes_to_gb(usage.total),
                'used_gb': _bytes_to_gb(usage.used),
                'free_gb': _bytes_to_gb(usage.free),
                'percent': usage.percent,
            })
        except (PermissionError, OSError):
            partitions.append({
                'device': p.device,
                'mountpoint': p.mountpoint,
                'fstype': p.fstype,
                'error': 'access_denied',
            })

    io = _safe(psutil.disk_io_counters)
    io_data = None
    if io:
        io_data = {
            'read_mb': _bytes_to_mb(io.read_bytes),
            'write_mb': _bytes_to_mb(io.write_bytes),
            'read_count': io.read_count,
            'write_count': io.write_count,
        }

    return {
        'timestamp': _ts(),
        'partitions': partitions,
        'io_counters': io_data,
    }


def network_snapshot(include_connections: bool = False) -> Dict[str, Any]:
    """
    网络快照（接口统计 + IO + 可选连接列表）。

    Args:
        include_connections: 是否包含活跃连接（需要权限）

    Returns:
        {timestamp, io: {bytes_sent_mb, bytes_recv_mb, ...},
         interfaces: {name: [{addr, netmask, family}]},
         connections_count?, connections?}
    """
    io = psutil.net_io_counters()
    addrs = psutil.net_if_addrs()

    interfaces = {}
    for name, addr_list in addrs.items():
        interfaces[name] = [
            {
                'address': a.address,
                'netmask': a.netmask,
                'family': str(a.family),
            }
            for a in addr_list
        ]

    result = {
        'timestamp': _ts(),
        'io': {
            'bytes_sent_mb': _bytes_to_mb(io.bytes_sent),
            'bytes_recv_mb': _bytes_to_mb(io.bytes_recv),
            'packets_sent': io.packets_sent,
            'packets_recv': io.packets_recv,
            'errin': io.errin,
            'errout': io.errout,
        },
        'interfaces': interfaces,
    }

    if include_connections:
        conns = _safe(psutil.net_connections, default=[])
        if conns is not None:
            result['connections_count'] = len(conns)
            # 只统计状态分布，不列出全部
            status_dist = {}
            for c in conns:
                s = c.status if hasattr(c, 'status') else 'unknown'
                status_dist[s] = status_dist.get(s, 0) + 1
            result['connection_status'] = status_dist

    return result


def process_snapshot(pid: Optional[int] = None, name: Optional[str] = None) -> Union[Dict, List[Dict]]:
    """
    进程详情快照。

    Args:
        pid: 指定PID
        name: 按名称模糊匹配（返回所有匹配进程列表）

    Returns:
        单进程dict 或 进程列表
    """
    def _proc_info(p: psutil.Process) -> Dict[str, Any]:
        try:
            with p.oneshot():
                info = {
                    'pid': p.pid,
                    'name': _safe(p.name, default='?'),
                    'status': _safe(p.status, default='?'),
                    'cpu_percent': _safe(p.cpu_percent, default=0.0),
                    'memory_mb': _bytes_to_mb(_safe(lambda: p.memory_info().rss, default=0)),
                    'memory_percent': round(_safe(p.memory_percent, default=0.0), 2),
                    'num_threads': _safe(p.num_threads, default=0),
                    'create_time': _safe(lambda: datetime.datetime.fromtimestamp(p.create_time()).isoformat(), default='?'),
                    'username': _safe(p.username, default='?'),
                    'cmdline': _safe(p.cmdline, default=[]),
                }
                return info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {'pid': p.pid, 'error': 'access_denied_or_gone'}

    if pid is not None:
        try:
            p = psutil.Process(pid)
            return _proc_info(p)
        except psutil.NoSuchProcess:
            return {'pid': pid, 'error': 'no_such_process'}

    if name is not None:
        results = []
        name_lower = name.lower()
        for p in psutil.process_iter(['pid', 'name']):
            try:
                if name_lower in (p.info['name'] or '').lower():
                    results.append(_proc_info(p))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return results

    raise ValueError("必须指定 pid 或 name 参数")


def top_processes(n: int = 10, sort_by: str = 'memory') -> List[Dict[str, Any]]:
    """
    按 CPU 或内存排序的 Top N 进程。

    Args:
        n: 返回数量
        sort_by: 'memory' 或 'cpu'

    Returns:
        [{pid, name, cpu_percent, memory_mb, memory_percent, username}]
    """
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'memory_percent', 'username']):
        try:
            info = p.info
            procs.append({
                'pid': info['pid'],
                'name': info['name'] or '?',
                'cpu_percent': info['cpu_percent'] or 0.0,
                'memory_mb': _bytes_to_mb(info['memory_info'].rss) if info['memory_info'] else 0,
                'memory_percent': round(info['memory_percent'] or 0.0, 2),
                'username': info['username'] or '?',
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            pass

    key = 'memory_mb' if sort_by == 'memory' else 'cpu_percent'
    procs.sort(key=lambda x: x[key], reverse=True)
    return procs[:n]


def system_snapshot(per_cpu: bool = False) -> Dict[str, Any]:
    """
    全量系统快照（CPU + 内存 + 磁盘 + 网络 + 启动时间 + 进程统计）。

    Returns:
        {timestamp, uptime_hours, process_count, cpu, memory, disk, network}
    """
    boot = psutil.boot_time()
    uptime_sec = time.time() - boot

    return {
        'timestamp': _ts(),
        'boot_time': datetime.datetime.fromtimestamp(boot).isoformat(timespec='seconds'),
        'uptime_hours': round(uptime_sec / 3600, 2),
        'process_count': len(psutil.pids()),
        'cpu': cpu_snapshot(per_cpu=per_cpu, interval=0.3),
        'memory': memory_snapshot(),
        'disk': disk_snapshot(),
        'network': network_snapshot(),
    }


def watch_resource(
    interval: float = 5.0,
    callback: Optional[Callable[[Dict], None]] = None,
    duration: Optional[float] = None,
    metrics: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    定期采集系统指标并回调。

    Args:
        interval: 采集间隔（秒）
        callback: 每次采集后调用的回调函数，接收 snapshot dict
        duration: 总持续时间（秒），None=无限循环（需手动中断）
        metrics: 要采集的指标列表 ['cpu','memory','disk','network']，None=全部

    Returns:
        所有采集到的快照列表（duration 有限时）
    """
    allowed = {'cpu', 'memory', 'disk', 'network'}
    if metrics:
        metrics_set = set(m.lower() for m in metrics) & allowed
    else:
        metrics_set = allowed

    snapshots = []
    start = time.time()

    try:
        while True:
            snap = {'timestamp': _ts()}
            if 'cpu' in metrics_set:
                snap['cpu'] = cpu_snapshot(interval=0.2)
            if 'memory' in metrics_set:
                snap['memory'] = memory_snapshot()
            if 'disk' in metrics_set:
                snap['disk'] = disk_snapshot()
            if 'network' in metrics_set:
                snap['network'] = network_snapshot()

            snapshots.append(snap)

            if callback:
                callback(snap)

            if duration and (time.time() - start) >= duration:
                break

            time.sleep(interval)
    except KeyboardInterrupt:
        pass

    return snapshots


# ── CLI ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--json':
        snap = system_snapshot()
        print(json.dumps(snap, indent=2, ensure_ascii=False, default=str))
    else:
        print("psutil_utils — 系统监控工具")
        print("  python psutil_utils.py --json    # 输出全量快照JSON")
        print()
        print("  from psutil_utils import cpu_snapshot, memory_snapshot, system_snapshot")
        print("  snap = system_snapshot()")