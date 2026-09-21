
# R313 — watchdog_utils.py 文件系统事件监听工具封装

## 任务
封装 watchdog 库，提供简洁的文件系统事件监听 API，支持按事件类型/glob模式过滤、去抖、多目录监听。

## 产出
`memory/watchdog_utils.py` — 8 个公开函数 + Watcher 对象 + CLI

### 函数清单
| 函数 | 说明 |
|------|------|
| `watch(path, on_event, ...)` | 通用监听入口，支持全部配置 |
| `watch_patterns(path, patterns, on_event)` | 按 glob 模式过滤监听 |
| `on_created(path, callback)` | 只监听文件创建 |
| `on_modified(path, callback)` | 只监听文件修改 |
| `on_deleted(path, callback)` | 只监听文件删除 |
| `on_moved(path, callback)` | 只监听文件移动/重命名 |
| `watch_multi(configs)` | 同时监听多个目录 |
| `stop_all()` | 停止所有活跃 Watcher |
| `list_watchers()` | 列出活跃 Watcher 状态 |

### 设计亮点
- **去抖机制**：per-path Timer 去抖，macOS FSEvents 多次触发问题已解决（默认 0.3s 窗口）
- **Watcher 对象**：返回可控对象，`.stop()` 停止、`.is_alive` 查状态
- **全局注册表**：`stop_all()` 一键清理，防止泄漏
- **CLI 模式**：`python watchdog_utils.py [path] -r -p '*.py'` 直接终端使用

### 已知坑（已在 docstring 中记录）
- macOS FSEvents 可能对同一修改触发多次事件 → 去抖解决
- 同一目录多次 schedule 到同一 Observer 会报 RuntimeError → 每个 watch 用独立 Observer
- 删除/移动事件通常不重复，默认 debounce=0

## 测试结果
7/7 全通过：
- ✅ on_created — 文件创建触发回调
- ✅ on_modified — 文件修改触发回调
- ✅ on_deleted — 文件删除触发回调
- ✅ on_moved — 文件重命名触发回调
- ✅ watch_patterns — glob 过滤（*.py 匹配，*.txt 不匹配）
- ✅ list_watchers + stop_all — 注册表管理正常
- ✅ debounce — 3次快速写入合并为1次回调

## 依赖
- watchdog 4.0.1（已安装）
- Python 3.9+
