"""
场景5：文件夹自动处理
监控指定目录新增文件 → Agent 自动分类/重命名/归档并推飞书
启动：python agentmain.py --reflect reflect_scripts/05_folder_watcher.py
"""
import sys, os, time
sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu

INTERVAL = 15   # 每15秒检查一次

# ===== 配置区 =====
WATCH_DIR    = os.path.expanduser("~/Downloads")   # 监控的目录
ARCHIVE_DIR  = os.path.expanduser("~/Documents/归档")  # 归档目标目录
# 忽略的扩展名（临时文件）
IGNORE_EXTS  = {".crdownload", ".part", ".tmp", ".DS_Store"}
# 文件稳定判断：最后修改到现在超过N秒才处理（避免处理下载中的文件）
STABLE_SECS  = 10
# ==================

_seen_files: dict[str, float] = {}   # {路径: 首次发现时间}
_processing: set = set()

def check():
    try:
        now = time.time()
        new_files = []

        for fname in os.listdir(WATCH_DIR):
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in IGNORE_EXTS:
                continue
            fpath = os.path.join(WATCH_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            if fpath in _processing:
                continue
            mtime = os.path.getmtime(fpath)
            # 首次见到：记录
            if fpath not in _seen_files:
                _seen_files[fpath] = now
                continue
            # 文件已稳定（下载完成）
            if now - mtime >= STABLE_SECS and now - _seen_files[fpath] >= STABLE_SECS:
                new_files.append(fpath)

        if not new_files:
            return None

        fpath = new_files[0]
        _processing.add(fpath)
        fname = os.path.basename(fpath)
        size_kb = os.path.getsize(fpath) // 1024
        return (
            f"Downloads目录新增文件：{fname}（{size_kb}KB）\n"
            f"完整路径：{fpath}\n"
            f"请：1) 判断文件类型和用途 2) 给它起一个规范的文件名（格式：日期_描述.扩展名）"
            f" 3) 将其移动到 {ARCHIVE_DIR} 下合适的子目录（不存在则创建）"
            f" 4) 告诉我做了什么"
        )
    except Exception as e:
        print(f"[文件夹监控失败] {e}")
        return None

def on_done(result: str):
    notify_feishu(f"📁 文件自动归档\n\n{result}")