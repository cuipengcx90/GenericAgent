#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机个性化数据源可用性地图（R344 / TODO#3，全程只读）。

设计
- **只读**：源库一律不打开写、不 VACUUM、不 journal；查询走 `file:...?mode=ro` 的 **temp 副本**。
- **WAL 感知复制**：`copy` 会连 `-wal`/`-shm` 一起拷（实测：Notes 不带 WAL 只见到 2 个对象，带上后 8 个）。
- **TCC 规避**：`~/Library/Containers/...` 递归 `find` 会挂起（实测 >25s 超时），必须用 `ls` 限深遍历。
- 子命令：`probe`（存在/可读/复制）· `sample`（行数+3 行真实样例）· `apps`（sfl3 最近应用）· `report`（出 markdown 表）。

产出：`temp/datasource_map/{probe.json,samples.json,MAP.md}`
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys

H = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(os.path.dirname(HERE), "temp", "datasource_map")
DBCOPY = os.path.join(WORK, "db_copy")

# (id, 标签, 路径, 类别, 读取提示)
SOURCES = [
    ("knowledgec", "KnowledgeC（App 使用/通知/屏幕点亮）", H + "/Library/Application Support/Knowledge/knowledgeC.db", "SQLite", "直接副本可读，含 /app/usage 流"),
    ("chrome", "Chrome 历史", H + "/Library/Application Support/Google/Chrome/Default/History", "SQLite", "Chrome 运行中也可拷（WAL 一起拷更全）"),
    ("safari", "Safari 历史", H + "/Library/Safari/History.db", "SQLite", "副本可读，量小"),
    ("notes", "Notes（新库 NoteStore）", H + "/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite", "SQLite", "**必须连 -wal 拷**；正文在 ZICNOTEDATA（protobuf/gzip）"),
    ("notes_v7", "Notes（旧库 NotesV7）", H + "/Library/Containers/com.apple.Notes/Data/Library/Notes/NotesV7.storedata", "SQLite", "结构在、数据空（本机已迁新库）"),
    ("messages", "Messages（iMessage）", H + "/Library/Messages/chat.db", "SQLite", "可读但**空库**（_SqliteDatabaseProperties 之外全 0 行）"),
    ("mail", "Mail（Envelope Index）", H + "/Library/Mail/V10/MailData/Envelope Index", "SQLite", "可读但 0 封信（mailboxes=2，未收信）"),
    ("reminders", "Reminders", H + "/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/Data-local.sqlite", "SQLite", "可读但 0 条（未使用）"),
    ("photos", "Photos 库", H + "/Pictures/Photos Library.photoslibrary/database/Photos.sqlite", "SQLite", "可读但 ZASSET=0（iCloud 优化/库空）"),
    ("sfl_recent_apps", "最近应用（sharedfilelist）", H + "/Library/Application Support/com.apple.sharedfilelist/com.apple.LSSharedFileList.RecentApplications.sfl3", "bplist(NSKeyedArchiver)", "plutil -convert xml1 或 strings 提取"),
    ("sfl_recent_docs", "最近文档（sharedfilelist）", H + "/Library/Application Support/com.apple.sharedfilelist/com.apple.LSSharedFileList.RecentDocuments.sfl3", "bplist(NSKeyedArchiver)", "同上；路径存为 bookmark blob"),
    ("wechat", "微信容器（消息/联系人）", H + "/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files", "目录+加密DB", "**递归 find 会挂起**→ ls 限深；库在 wxid_*/db_storage，需密钥解密"),
]

QUERIES = {
    "knowledgec": [
        ("总对象数", "select count(*) from ZOBJECT"),
        ("流分布 top3", "select ZSTREAMNAME, count(*) c from ZOBJECT group by 1 order by c desc limit 3"),
        ("最近 App 使用", 'select datetime(ZSTARTDATE+978307200,"unixepoch","localtime") t, ZVALUESTRING from ZOBJECT where ZSTREAMNAME="/app/usage" order by ZSTARTDATE desc limit 3'),
    ],
    "chrome": [
        ("URL 总数", "select count(*) from urls"),
        ("最近访问", 'select substr(title,1,40), substr(url,1,50) from urls where title<>"" order by last_visit_time desc limit 3'),
    ],
    "safari": [
        ("条目数", "select count(*) from history_items"),
        ("最常访问", "select substr(url,1,50), visit_count from history_items order by visit_count desc limit 3"),
    ],
    "notes": [
        ("对象数(含WAL)", "select count(*) from ZICCLOUDSYNCINGOBJECT"),
        ("带标题", "select count(*) from ZICCLOUDSYNCINGOBJECT where ZTITLE1 is not null"),
        ("样例", "select substr(ZTITLE1,1,50) from ZICCLOUDSYNCINGOBJECT where ZTITLE1 is not null order by ZMODIFICATIONDATE desc limit 3"),
    ],
    "notes_v7": [
        ("笔记数", "select count(*) from ZNOTE"),
        ("正文数", "select count(*) from ZNOTEBODY"),
    ],
    "messages": [
        ("消息数", "select count(*) from message"),
        ("会话数", "select count(*) from chat"),
        ("非空表", "select 'see sample'"),
    ],
    "mail": [
        ("邮件数", "select count(*) from messages"),
        ("邮箱数", "select count(*) from mailboxes"),
    ],
    "reminders": [
        ("提醒数", "select count(*) from ZREMCDREMINDER"),
    ],
    "photos": [
        ("资源数", "select count(*) from ZASSET"),
    ],
}


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        return out if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "[TIMEOUT>%ss] 命令挂起未返回（TCC 阻塞的典型表现）" % timeout


def cmd_probe():
    os.makedirs(DBCOPY, exist_ok=True)
    res = {}
    for sid, label, path, kind, hint in SOURCES:
        rec = {"label": label, "path": path, "kind": kind, "hint": hint}
        rec["exists"] = os.path.exists(path)
        if not rec["exists"]:
            res[sid] = rec
            print("%-16s ❌ 不存在" % sid)
            continue
        if os.path.isdir(path):
            rec["dir_entries"] = len(os.listdir(path)) if os.access(path, os.R_OK) else None
            print("%-16s 目录 entries=%s" % (sid, rec["dir_entries"]))
        else:
            rec["size"] = os.path.getsize(path)
            try:
                with open(path, "rb") as f:
                    head = f.read(16)
                rec["readable"] = True
                rec["head"] = head[:15].decode("latin-1")
            except Exception as e:
                rec["readable"] = False
                rec["read_error"] = "%s: %s" % (type(e).__name__, e)
            # WAL 感知复制
            if rec.get("readable") and rec["size"] < 200 * 1024 * 1024:
                copied = []
                for suf, tag in (("", ""), ("-wal", ".wal"), ("-shm", ".shm")):
                    s = path + suf
                    if os.path.exists(s):
                        try:
                            shutil.copy2(s, os.path.join(DBCOPY, sid + ".db" + tag))
                            copied.append(suf or "main")
                        except Exception as e:
                            rec["copy_error"] = "%s: %s" % (type(e).__name__, e)
                rec["copied"] = copied
                rec["copy_ok"] = bool(copied)
                print("%-16s %.1fMB 可读 head=%r 复制=%s" % (sid, rec["size"] / 1e6, head[:8], rec["copied"]))
            else:
                print("%-16s %.1fMB 不可读: %s" % (sid, rec["size"] / 1e3, rec.get("read_error")))
        res[sid] = rec
    json.dump(res, open(os.path.join(WORK, "probe.json"), "w"), ensure_ascii=False, indent=1)
    print("\n→ %s/probe.json" % WORK)


def cmd_sample():
    out = {}
    for sid, qs in QUERIES.items():
        dbp = os.path.join(DBCOPY, sid + ".db")
        if not os.path.exists(dbp):
            out[sid] = {"err": "无副本（先跑 probe）"}
            print("%-12s ⏭ 无副本" % sid)
            continue
        con = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True)
        rec = {}
        for name, sql in qs:
            try:
                rows = con.execute(sql).fetchall()[:3]
                rec[name] = [str(x)[:70] for x in rows] if len(rows) == 1 and len(rows[0]) == 1 else [tuple(str(y)[:70] for y in r) for r in rows]
            except Exception as e:
                rec[name] = "ERR %s: %s" % (type(e).__name__, e)
        con.close()
        out[sid] = rec
        for k, v in rec.items():
            print("%-12s %-14s %s" % (sid, k, v))
    json.dump(out, open(os.path.join(WORK, "samples.json"), "w"), ensure_ascii=False, indent=1, default=str)
    print("\n→ %s/samples.json" % WORK)


def cmd_apps():
    """sfl3（NSKeyedArchiver bplist）→ 最近应用/最近文档条目。"""
    res = {}
    base = H + "/Library/Application Support/com.apple.sharedfilelist/com.apple.LSSharedFileList.%s.sfl3"
    for tag, f in (("RecentApplications", "recent_apps"), ("RecentDocuments", "recent_docs")):
        p = base % tag
        if not os.path.exists(p):
            res[f] = "missing"
            continue
        apps = _run("strings -a '%s' | grep -E '\\.app$|/Applications/' | sort -u | head -8" % p)
        paths = _run("strings -a '%s' | grep -E '^file:///' | sort -u | head -5" % p)
        cnt = _run("strings -a '%s' | grep -cE '\\.app$|^file:///'" % p)
        res[f] = {"size": os.path.getsize(p), "entry_hint_count": cnt, "apps": apps, "paths": paths}
        print("== %s (%dB, 计数 %s)" % (tag, os.path.getsize(p), cnt))
        print(apps)
    json.dump(res, open(os.path.join(WORK, "sfl.json"), "w"), ensure_ascii=False, indent=1)


def cmd_wechat():
    root = H + "/Library/Containers/com.tencent.xinWeChat"
    res = {"root": root}
    res["ls_root"] = _run("ls '%s' 2>&1 | head -5" % root, 15)
    res["find_depth1"] = _run("find '%s' -maxdepth 1 -type f 2>/dev/null | wc -l" % root, 15)
    res["find_recursive"] = _run("find '%s' -type f 2>/dev/null | wc -l" % root, 25)
    acc = root + "/Data/Documents/xwechat_files"
    res["accounts"] = _run("ls '%s' 2>&1 | head -6" % acc, 15)
    res["db_storage"] = _run("ls '%s'/wxid_*/db_storage 2>&1 | head -12" % acc, 15)
    res["msg_dir"] = _run("ls '%s'/wxid_*/msg 2>&1 | head -6" % acc, 15)
    json.dump(res, open(os.path.join(WORK, "wechat.json"), "w"), ensure_ascii=False, indent=1)
    for k, v in res.items():
        print("-- %s:\n%s" % (k, v))


def _num(rows):
    """从 [['(17400,)'], ...] / ['(17400,)'] / [['/app/usage','10000']] 里抽出可读计数。"""
    try:
        if not isinstance(rows, list) or not rows:
            return ""
        r0 = rows[0]
        if isinstance(r0, str):
            return r0.strip("(,). ").strip() or ("%s 组" % len(rows))
        if isinstance(r0, list) and len(r0) >= 1:
            return str(r0[0]).strip("(,). ").strip() or ("%s 组" % len(rows))
        return "%s 组" % len(rows)
    except Exception:
        return str(rows)[:20]


def _load(name):
    p = os.path.join(WORK, name)
    return json.load(open(p)) if os.path.exists(p) else {}


def cmd_report():
    probe, samples, sfl, wx = _load("probe.json"), _load("samples.json"), _load("sfl.json"), _load("wechat.json")
    L = []
    L.append("# 本机个性化数据源可用性地图（只读实测）\n")
    L.append("生成：`tools/personal_source_map.py report`　｜　基线 %s\n" % __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("## 一、结论表（能读 / 不能读 / 怎么读）\n")
    L.append("| 源 | 体积 | 可读? | 实测行数 | 读法 |")
    L.append("|---|---|---|---|---|")
    KEY = {"knowledgec": "总对象数", "chrome": "URL 总数", "safari": "条目数", "notes": "对象数(含WAL)",
           "notes_v7": "笔记数", "messages": "消息数", "mail": "邮件数", "reminders": "提醒数", "photos": "资源数"}
    for sid, label, path, kind, hint in SOURCES:
        p = probe.get(sid, {})
        s = samples.get(sid, {})
        size = "%.1fMB" % (p["size"] / 1e6) if p.get("size") else ("目录 %s 项" % p.get("dir_entries") if p.get("dir_entries") is not None else "-")
        if p.get("readable") is False:
            ok = "❌ " + p.get("read_error", "")
        elif s:
            ok = "✅" if "无副本" not in str(s) else "✅(未取副本)"
        else:
            ok = "✅(目录/bplist)" if p.get("exists") else "❌不存在"
        rows = ""
        if sid in KEY and isinstance(s.get(KEY[sid]), list):
            rows = _num(s[KEY[sid]])
        L.append("| %s | %s | %s | %s | %s |" % (label, size, ok, rows, hint))
    L.append("\n## 二、真实内容样例（每源前 3 行）\n")
    for sid, rec in samples.items():
        if not rec or "err" in rec:
            L.append("- **%s**：%s" % (sid, rec.get("err", "无")))
            continue
        L.append("- **%s**" % sid)
        for k, v in rec.items():
            L.append("    - %s：`%s`" % (k, v))
    if sfl:
        L.append("\n## 三、最近应用/文档（sharedfilelist sfl3）\n")
        for k, v in sfl.items():
            L.append("- **%s**（%s B）：\n```\n%s\n```" % (k, v.get("size"), v.get("apps")))
    if wx:
        L.append("\n## 四、微信容器（find 阻塞实证）\n")
        for k, v in wx.items():
            L.append("- `%s` → %s" % (k, str(v).replace("\n", " / ")[:200]))
    L.append("\n## 五、推荐：最值得挖掘的 1 个源\n")
    L.append("**KnowledgeC（`~/Library/Application Support/Knowledge/knowledgeC.db`）**")
    L.append("- 理由：唯一同时满足 ①**副本直读零授权** ②**量大**（实测 17400 条对象，`/app/usage` 10000 条）③**语义新**（App 使用时段、通知投递、屏幕点亮，等于逐分钟的数字生活底稿），且本机的 Messages/Mail/Photos/Reminders/Notes 全是空库，没有替代品。")
    L.append("- 已知既有基础：`Timing2`（300K 条 AppActivity）周报链路已存在（`timing2_weekly_report.py`，见 history.txt），可与之合并口径。")
    L.append("- 建议下一步（**待批**，本轮不做）：把 knowledgeC 的 `/app/usage` 与 Timing2 做交叉验证，产出「时间去哪了 v2」；注意 knowledgeC 默认只保留约 4 周历史（Apple 行为），越早建快照越好。")
    L.append("- 次选：Chrome 历史（12501 条 URL，含搜索意图），但隐私面更敏感、且已有 Safari/TrendRadar 覆盖阅读侧。\n")
    L.append("## 六、只读纪律与复用方式\n")
    L.append("- 全程**零写入源库**：所有查询都跑在 `temp/datasource_map/db_copy/` 的副本上（`file:...?mode=ro`），复制时连 `-wal`/`-shm` 一起拷（不带 WAL 会漏数据，实测 Notes 对象数 2 → 8）。")
    L.append("- 三个必须知道的坑：①微信容器 `find` 递归 = TCC 挂起（>25s 无返回），只能 `ls` 限深；②`~/Library/Containers` 下 DB 可直接读但目录遍历受限；③Notes 正文在 `ZICNOTEDATA`（protobuf + gzip），拿到标题 ≠ 拿到正文。")
    L.append("- 幂等复用：`python3 tools/personal_source_map.py all` 一键重生 `probe.json / samples.json / sfl.json / wechat.json / MAP.md`；判废只需看 MAP.md「结论表」里的实测列数。")
    md = "\n".join(L) + "\n"
    open(os.path.join(WORK, "MAP.md"), "w").write(md)
    print(md[:1500])
    print("\n→ %s/MAP.md (%d 字节)" % (WORK, len(md.encode())))


def main():
    a = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(WORK, exist_ok=True)
    if a in ("probe", "all"):
        cmd_probe()
    if a in ("sample", "all"):
        cmd_sample()
    if a in ("apps", "all"):
        cmd_apps()
    if a in ("wechat", "all"):
        cmd_wechat()
    if a in ("report", "all"):
        cmd_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
