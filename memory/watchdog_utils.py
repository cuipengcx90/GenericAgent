"""
watchdog_utils.py — 文件系统事件监听工具封装
依赖: watchdog (pip install watchdog), Python 3.9+
功能:
  - watch(path, on_event, ...)         : 启动监听，返回 Watcher 对象
  - watch_patterns(path, patterns, on_event, ...) : 按 glob 模式过滤监听
  - on_created(path, callback, ...)    : 只监听文件创建
  - on_modified(path, callback, ...)   : 只监听文件修改
  - on_deleted(path, callback, ...)    : 只监听文件删除
  - on_moved(path, callback, ...)      : 只监听文件移动/重命名
  - watch_multi(configs)               : 同时监听多个目录，不同回调
  - stop_all()                         : 停止所有活跃的 Watcher

坑:
  - macOS 上 watchdog 默认用 FSEvents，文件修改可能触发多次事件（去抖很重要）
  - 目录事件和文件事件混在一起，默认只监听文件事件（ignore_directories=True）
  - 创建大文件时可能先触发 created 再触发多次 modified，注意幂等
  - recursive=True 时大目录树会消耗较多资源
  - Observer 是守护线程，主进程退出后自动停止
  - 回调在 Observer 线程中执行，注意线程安全
  - watch 返回的 Watcher 对象必须保持引用，否则会被 GC 回收导致监听停止
"""

import os
import time
import threading
import fnmatch
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any, Union, Set
from dataclasses import dataclass, field

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileSystemEvent,
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    DirCreatedEvent,
    DirModifiedEvent,
    DirDeletedEvent,
    DirMovedEvent,
)

# ── 全局注册表 ──────────────────────────────────────────────────

_active_watchers: List["Watcher"] = []
_lock = threading.Lock()

# ── 去抖工具 ──────────────────────────────────────────────────

class _Debouncer:
    """per-path 去抖：同一文件在 debounce 窗口内只触发最后一次"""
    def __init__(self, delay: float):
        self.delay = delay
        self._timers: Dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def call(self, key: str, fn: Callable, *args, **kwargs):
        with self._lock:
            if key in self._timers:
                self._timers[key].cancel()
            t = threading.Timer(self.delay, self._fire, args=(key, fn, args, kwargs))
            t.daemon = True
            t.start()
            self._timers[key] = t

    def _fire(self, key: str, fn: Callable, args, kwargs):
        with self._lock:
            self._timers.pop(key, None)
        fn(*args, **kwargs)

    def cancel_all(self):
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()


# ── Handler ──────────────────────────────────────────────────

class _CallbackHandler(FileSystemEventHandler):
    """内部 handler：支持事件类型过滤 + glob 模式过滤 + 去抖"""

    def __init__(
        self,
        callback: Callable[[FileSystemEvent], None],
        event_types: Optional[Set[str]] = None,
        patterns: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
        ignore_directories: bool = True,
        debounce: float = 0.0,
    ):
        super().__init__()
        self.callback = callback
        self.event_types = event_types  # {'created','modified','deleted','moved'} or None=all
        self.patterns = patterns
        self.ignore_patterns = ignore_patterns
        self.ignore_directories = ignore_directories
        self._debouncer = _Debouncer(debounce) if debounce > 0 else None

    def on_any_event(self, event: FileSystemEvent):
        # 1. 目录过滤
        if self.ignore_directories and event.is_directory:
            return

        # 2. 事件类型过滤
        etype = event.event_type  # 'created','modified','deleted','moved'
        if self.event_types and etype not in self.event_types:
            return

        # 3. glob 模式过滤（匹配文件名）
        src = event.src_path
        basename = os.path.basename(src)
        if self.patterns:
            if not any(fnmatch.fnmatch(basename, p) for p in self.patterns):
                return
        if self.ignore_patterns:
            if any(fnmatch.fnmatch(basename, p) for p in self.ignore_patterns):
                return

        # 4. 去抖或直接调用
        if self._debouncer:
            self._debouncer.call(f"{etype}:{src}", self.callback, event)
        else:
            self.callback(event)

    def stop(self):
        if self._debouncer:
            self._debouncer.cancel_all()


# ── Watcher 对象 ──────────────────────────────────────────────

@dataclass
class Watcher:
    """表示一个活跃的文件监听，提供 stop() 方法"""
    path: str
    observer: Observer
    handler: _CallbackHandler
    _stopped: bool = field(default=False, repr=False)

    def stop(self):
        """停止此监听"""
        if not self._stopped:
            self.handler.stop()
            self.observer.stop()
            self.observer.join(timeout=5)
            self._stopped = True
            with _lock:
                if self in _active_watchers:
                    _active_watchers.remove(self)

    @property
    def is_alive(self) -> bool:
        return not self._stopped and self.observer.is_alive()

    def __del__(self):
        if not self._stopped:
            try:
                self.stop()
            except Exception:
                pass


# ── 公开 API ──────────────────────────────────────────────────

def watch(
    path: Union[str, Path],
    on_event: Callable[[FileSystemEvent], None],
    *,
    recursive: bool = False,
    event_types: Optional[Set[str]] = None,
    patterns: Optional[List[str]] = None,
    ignore_patterns: Optional[List[str]] = None,
    ignore_directories: bool = True,
    debounce: float = 0.3,
) -> Watcher:
    """
    启动文件系统监听。

    Args:
        path: 监听目录路径
        on_event: 回调函数，接收 FileSystemEvent
        recursive: 是否递归监听子目录
        event_types: 要监听的事件类型集合 {'created','modified','deleted','moved'}，None=全部
        patterns: glob 模式白名单（匹配文件名），如 ['*.py', '*.txt']
        ignore_patterns: glob 模式黑名单
        ignore_directories: 是否忽略目录事件（默认True）
        debounce: 去抖延迟秒数（同一文件同类型事件合并），0=不去抖

    Returns:
        Watcher 对象，调用 .stop() 停止监听
    """
    path = str(Path(path).resolve())
    if not os.path.isdir(path):
        raise ValueError(f"路径不存在或不是目录: {path}")

    handler = _CallbackHandler(
        callback=on_event,
        event_types=event_types,
        patterns=patterns,
        ignore_patterns=ignore_patterns,
        ignore_directories=ignore_directories,
        debounce=debounce,
    )
    observer = Observer()
    observer.schedule(handler, path, recursive=recursive)
    observer.daemon = True
    observer.start()

    w = Watcher(path=path, observer=observer, handler=handler)
    with _lock:
        _active_watchers.append(w)
    return w


def watch_patterns(
    path: Union[str, Path],
    patterns: List[str],
    on_event: Callable[[FileSystemEvent], None],
    *,
    recursive: bool = False,
    debounce: float = 0.3,
) -> Watcher:
    """
    按 glob 模式过滤监听。

    Args:
        path: 监听目录
        patterns: glob 模式列表，如 ['*.py', '*.json']
        on_event: 回调
        recursive: 递归
        debounce: 去抖

    Returns:
        Watcher
    """
    return watch(path, on_event, recursive=recursive, patterns=patterns, debounce=debounce)


def on_created(
    path: Union[str, Path],
    callback: Callable[[FileSystemEvent], None],
    *,
    patterns: Optional[List[str]] = None,
    recursive: bool = False,
    debounce: float = 0.3,
) -> Watcher:
    """只监听文件创建事件"""
    return watch(path, callback, recursive=recursive, event_types={'created'},
                 patterns=patterns, debounce=debounce)


def on_modified(
    path: Union[str, Path],
    callback: Callable[[FileSystemEvent], None],
    *,
    patterns: Optional[List[str]] = None,
    recursive: bool = False,
    debounce: float = 0.3,
) -> Watcher:
    """只监听文件修改事件"""
    return watch(path, callback, recursive=recursive, event_types={'modified'},
                 patterns=patterns, debounce=debounce)


def on_deleted(
    path: Union[str, Path],
    callback: Callable[[FileSystemEvent], None],
    *,
    patterns: Optional[List[str]] = None,
    recursive: bool = False,
    debounce: float = 0.0,
) -> Watcher:
    """只监听文件删除事件（删除事件不去抖，默认debounce=0）"""
    return watch(path, callback, recursive=recursive, event_types={'deleted'},
                 patterns=patterns, debounce=debounce)


def on_moved(
    path: Union[str, Path],
    callback: Callable[[FileSystemEvent], None],
    *,
    patterns: Optional[List[str]] = None,
    recursive: bool = False,
    debounce: float = 0.0,
) -> Watcher:
    """只监听文件移动/重命名事件"""
    return watch(path, callback, recursive=recursive, event_types={'moved'},
                 patterns=patterns, debounce=debounce)


def watch_multi(configs: List[Dict[str, Any]]) -> List[Watcher]:
    """
    同时监听多个目录。

    Args:
        configs: 配置列表，每项为 dict：
            {
                'path': '/some/dir',
                'on_event': callback_fn,
                'recursive': False,      # optional
                'patterns': ['*.py'],     # optional
                'debounce': 0.3,          # optional
                'event_types': {'created'}, # optional
            }

    Returns:
        Watcher 列表
    """
    watchers = []
    for cfg in configs:
        path = cfg.pop('path')
        on_event = cfg.pop('on_event')
        w = watch(path, on_event, **cfg)
        watchers.append(w)
    return watchers


def stop_all():
    """停止所有活跃的 Watcher"""
    with _lock:
        to_stop = list(_active_watchers)
    for w in to_stop:
        try:
            w.stop()
        except Exception:
            pass


def list_watchers() -> List[Dict[str, Any]]:
    """列出所有活跃的 Watcher 状态"""
    with _lock:
        return [
            {'path': w.path, 'alive': w.is_alive, 'stopped': w._stopped}
            for w in _active_watchers
        ]


# ── CLI ──────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='watchdog_utils CLI — 文件系统事件监听')
    parser.add_argument('path', nargs='?', default='.', help='监听目录（默认当前目录）')
    parser.add_argument('-r', '--recursive', action='store_true', help='递归监听子目录')
    parser.add_argument('-p', '--patterns', nargs='*', help='glob 过滤模式，如 *.py *.txt')
    parser.add_argument('-t', '--types', nargs='*', choices=['created', 'modified', 'deleted', 'moved'],
                        help='事件类型过滤')
    parser.add_argument('-d', '--debounce', type=float, default=0.3, help='去抖延迟（秒）')
    args = parser.parse_args()

    def _print_event(event):
        ts = time.strftime('%H:%M:%S')
        print(f"[{ts}] {event.event_type:10s} {event.src_path}")

    et = set(args.types) if args.types else None
    w = watch(args.path, _print_event, recursive=args.recursive,
              event_types=et, patterns=args.patterns, debounce=args.debounce)
    print(f"👁 监听中: {w.path} (recursive={args.recursive}, debounce={args.debounce}s)")
    print("按 Ctrl+C 停止")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
        print("\n已停止")