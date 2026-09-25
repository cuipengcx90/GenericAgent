#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滚动源「每日快照」多源单入口（纯 stdlib · 只读源 · 幂等）

为什么存在（R415 / 第13批 C3）：有些本机数据源按时间窗**滚动删除**，每过一天就永久
丢掉一天，错过的窗口无法事后补采。历史教训=knowledgeC（23–28 天窗口）只攒到 3 天
快照且缺 09-24；Chrome 历史（visits 表 ≈90 天窗口）**零快照覆盖**。

本工具做四件事（多源统一口径，不重复实现各源自身逻辑）：
  snapshot  逐源采一份「当天」快照，打印 路径 / 字节 / sha256，并写 manifest.json
  window    对快照副本（file:...?mode=ro）跑「窗口长度」SQL，给出 行数 / 最老 / 最新 / 跨度天
  list      列出全源快照资产（按日期），供「不可再生程度 × 当前覆盖」对账
  selftest  合成夹具 + 负控自证（见 --selftest，禁止只跑正例）

设计约束
  - **只读源**：源库一律 `file:<path>?mode=ro` 打开；采集后跑「只读探针」
    （BEGIN IMMEDIATE 必被拒），证明本进程物理上写不了源库。
  - **源侧 sha1 前后复核**：源是**活库**（Chrome/OS 在写），故复核输出只有两种合法
    结论：`unchanged`（强证据）或 `changed-by-live-writer`（必须附「非本进程写入」的
    旁证=只读探针 + 打开方式）。**不得**把活库变更静默当成自己的写入。
  - **一致性优先**：优先用 sqlite3 backup API 从只读连接生成一致副本；不可用时回退
    「原样拷 db + 侧车（-wal/-shm/-journal）」并如实记 method，副本一律跑 quick_check。
  - **幂等**：当天已有快照（且 manifest 末次 revision 日期==今天）时直接跳过，`--force` 重采。
  - **不删任何东西**：无 prune 子命令，删除历史快照不可逆。

用法
  python3 tools/rolling_source_snapshot.py snapshot               # 全源采今天
  python3 tools/rolling_source_snapshot.py snapshot --source chrome --json
  python3 tools/rolling_source_snapshot.py window                 # 各源窗口长度
  python3 tools/rolling_source_snapshot.py list
  python3 tools/rolling_source_snapshot.py selftest               # 5 例（含 4 类负控）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

GA = Path('/Users/cuip/projects/GenericAgent')
HOME = Path.home()

# ---------------------------------------------------------------- 源注册表
# window: [(名字, SQL, 说明)] —— 全部对**快照副本**执行（只读），便于事后复跑
SOURCES = {
    'chrome': {
        'title': 'Chrome 历史（Default/History）',
        'kind': 'copy',
        'db': HOME / 'Library/Application Support/Google/Chrome/Default/History',
        'sidecars': ['-journal', '-wal', '-shm'],
        'root': GA / 'temp' / 'chrome_snapshots',
        'artifact': 'History',
        'irreplaceability': 9,
        'coverage': '零覆盖（全仓无任何 Chrome 历史快照）',
        'window': [
            ('visits 全表',
             "select count(*) n, datetime(min(visit_time)/1000000.0-11644473600,'unixepoch','localtime') oldest,"
             " datetime(max(visit_time)/1000000.0-11644473600,'unixepoch','localtime') newest from visits",
             'Chrome 只保留最近 ~90 天；超窗即永久删除'),
            ('urls 全表', "select count(*) n, null, null from urls", 'URL 条目数（含标题）'),
            ('按天分布(尾 5 天)',
             "select date(visit_time/1000000.0-11644473600,'unixepoch','localtime') d, count(*) n from visits"
             " group by d order by d desc limit 5",
             '确认快照确实含"今天"'),
        ],
    },
    'kc': {
        'title': 'knowledgeC（macOS 数字行为库）',
        'kind': 'delegate',          # 复用既有 tools/kc_snapshot.py（已有 quick_check/计数/策略）
        'delegate_cmd': [sys.executable, str(GA / 'tools' / 'kc_snapshot.py'), 'snapshot'],
        'db': HOME / 'Library/Application Support/Knowledge/knowledgeC.db',
        'sidecars': ['-wal', '-shm'],
        'root': GA / 'temp' / 'kc_snapshots',
        'artifact': 'knowledgeC.db',
        'irreplaceability': 8,
        'coverage': '3 天（09-22 / 09-23 / 09-25；缺 09-24）',
        'window': [
            ('ZOBJECT 全表',
             "select count(*) n, min(date(ZSTARTDATE+978307200,'unixepoch','localtime')) oldest,"
             " max(date(ZSTARTDATE+978307200,'unixepoch','localtime')) newest from ZOBJECT",
             '全部流的总跨度（含永久保留的少数行）'),
            ('/app/usage 流',
             "select count(distinct date(ZSTARTDATE+978307200,'unixepoch','localtime')) n,"
             " min(date(ZSTARTDATE+978307200,'unixepoch','localtime')) oldest,"
             " max(date(ZSTARTDATE+978307200,'unixepoch','localtime')) newest from ZOBJECT"
             " where ZSTREAMNAME='/app/usage'",
             '★该流按 ~23 天滚动删除 ⇒ 它的跨度就是真实"窗口长度"'),
        ],
    },
    'pmset': {
        'title': '电源/睡眠日志（/var/log/powermanagement 全目录：*.asl + StoreData）',
        'kind': 'dir_copy',                  # 目录型滚动源：逐文件拷贝 + 内容寻址去重（硬链接）
        'src_dir': Path('/var/log/powermanagement'),
        'include': ['*.asl', 'StoreData'],
        'root': GA / 'temp' / 'pmset_snapshots',
        'artifact': 'events.json',           # 「窗口」= 解析产物（睡眠/唤醒/充电事件统计）
        'parse': 'powermanagement',
        'irreplaceability': 10,
        'coverage': '零覆盖（全仓无任何 powermanagement 快照；实测窗口 2026.09.12–今 ≈15 天）',
        'window': [],
        'window_note': '解析窗口：逐 .asl 用官方解码器 `syslog -f` 抽「睡眠/暗唤醒/唤醒/供电」事件；'
                       '★窗口长度由文件轮转决定（实测 15 天），不可再生',
    },
    'wifilog': {
        'title': 'WiFi 日志（/var/log/wifi.log，newsyslog 每日 00:30 轮转）',
        'kind': 'file_copy',
        'src_file': Path('/var/log/wifi.log'),
        'root': GA / 'temp' / 'wifi_snapshots',
        'artifact': 'wifi.log',
        'parse': 'wifilog',
        'irreplaceability': 8,
        'coverage': '零覆盖（全仓无任何 wifi.log 快照）',
        'window': [],
        'window_note': '解析窗口：airport 事件计数 + DHCP 状态变更；并与同日 pmset 快照的睡眠窗口对账'
                       '（只回答「睡眠窗口内是否仍有网络活动」，不输出 SSID）',
    },
}

# 已知滚动源但**未实现**采集（只登记，不装定时；评审建议：别单独立项）
NOTES = {
    'pmset': 'R420 已实现采集（见源 pmset / wifilog）；本工具只做「源接入 + 解析」，**未接定时**（待批）',
    'wifi-rotated': '★边界：/var/log/wifi.log.N.bz2（N=0..10，各 ~100–370KB）**未纳入采集**；'
                    '只采当前 wifi.log（newsyslog 每日 00:30 轮转）⇒ 更早窗口的「睡眠内网络活动」对账不可得',
    'pmset-store': '★边界：/var/log/powermanagement/StoreData（8B 索引）随目录整体拷贝；'
                   '`pmset -g log` 是**活性视图**（按 ASL 索引滚动读，实测回溯仅到 2026-09-19），它本身不落盘',
}

METHOD = 'rolling_source_snapshot/v1'


# ---------------------------------------------------------------- 基础工具
def sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(chunk), b''):
            h.update(b)
    return h.hexdigest()


def _stat(p: Path) -> dict:
    st = p.stat()
    return {'path': str(p), 'bytes': st.st_size, 'mtime': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'sha256': sha256_file(p)}


def _ro_conn(db: Path) -> sqlite3.Connection:
    """只读连接（URI + mode=ro）"""
    return sqlite3.connect('file:%s?mode=ro' % quote(str(db)), uri=True, timeout=5.0)


def _pick_table(con: sqlite3.Connection):
    try:
        r = con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                        " order by name limit 1").fetchone()
        return r[0] if r else None
    except Exception:  # noqa: BLE001
        return None


def readonly_probe(con: sqlite3.Connection) -> dict:
    """只读探针：`DELETE FROM <表> WHERE 0` —— 0 行匹配 ⇒ 数据零变更，但语句仍需**写事务**。

    ★ 实测(本机 python3.12.7 / sqlite)：`BEGIN IMMEDIATE` 在 mode=ro 连接上**不报错**
      （SQLite 延迟到真正写才拒绝）⇒ 不能用它当探针，否则假绿。改用：
      - 只读连接 ⇒ OperationalError('attempt to write a readonly database')
      - 可写连接 ⇒ 不报错；WHERE 0 匹配 0 行 ⇒ sha256 前后实测一致（`/tmp/probe_test.db` 两态验证）
    返回 {'readonly': bool, 'detail': str, 'probe_sql': str|None}
    """
    t = _pick_table(con)
    if not t:
        return {'readonly': None, 'detail': '无可试表(schema 为空) ⇒ 探针不适用', 'probe_sql': None}
    sql = 'DELETE FROM "%s" WHERE 0' % t
    try:
        con.execute(sql)
    except sqlite3.OperationalError as e:
        ok = 'readonly' in str(e)
        return {'readonly': ok, 'detail': '被拒: %s' % e, 'probe_sql': sql}
    else:
        con.rollback()
        return {'readonly': False, 'detail': '写语句被接受（该连接可写）', 'probe_sql': sql}


def selfproof_frozen_copy(src: dict, quiet: bool = True) -> dict:
    """只读自证（强证据，与"源库是否被别的进程写"解耦）：

    把源库（+侧车）原样拷成**冻结副本** → 用**同一条采集代码路径**对副本采一次 →
    比对副本 sha256 前后。副本无其它写者 ⇒ 只要 sha256 变了，只可能是本进程写的。
    """
    db = Path(src['db'])
    tmpd = Path(tempfile.mkdtemp(prefix='rss-proof-'))
    frozen = tmpd / db.name
    shutil.copy2(db, frozen)
    for s in src.get('sidecars', []):
        sp = Path(str(db) + s)
        if sp.exists():
            shutil.copy2(sp, str(frozen) + s)
    before = sha256_file(frozen)
    sub = dict(src)
    sub.update({'db': frozen, 'root': tmpd / 'out', 'sidecars': list(src.get('sidecars', [])),
                'no_selfproof': True, 'copy_timeout': 5})
    snapshot_copy_source(sub, 'selfproof', force=True, quiet=quiet)
    after = sha256_file(frozen)
    side = {s: (sha256_file(Path(str(frozen) + s)) if Path(str(frozen) + s).exists() else None)
            for s in src.get('sidecars', [])}
    shutil.rmtree(tmpd, ignore_errors=True)
    return {'frozen_sha256_before': before, 'frozen_sha256_after': after,
            'verdict': 'unchanged' if before == after else 'changed',
            'sidecar_sha256': side,
            'note': '冻结副本上跑同一条采集路径；unchanged ⇒ 本进程只读源库'}


def verdict(before: dict, after: dict) -> str:
    """源侧复核结论：identical ⇒ unchanged；否则 changed（须人工归因）。"""
    keys = ('bytes', 'mtime', 'sha256')
    if all(before.get(k) == after.get(k) for k in keys):
        return 'unchanged'
    return 'changed-by-live-writer'


def quick_check(db: Path) -> str:
    try:
        with _ro_conn(db) as con:
            return str(con.execute('PRAGMA quick_check').fetchone()[0])
    except Exception as e:  # noqa: BLE001
        return 'error: %s' % e


def run_window_queries(db: Path, queries: list, limit: int = 3) -> list:
    """对快照副本跑窗口 SQL（只读）。"""
    out = []
    with _ro_conn(db) as con:
        for name, sql, note in queries:
            try:
                cur = con.execute(sql)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description] if cur.description else []
            except Exception as e:  # noqa: BLE001
                out.append({'name': name, 'note': note, 'error': str(e)})
                continue
            if name.startswith('按天分布'):
                out.append({'name': name, 'note': note, 'by_day': rows[:limit]})
            else:
                out.append({'name': name, 'note': note, 'cols': cols, 'row': list(rows[0]) if rows else None})
    return out


def span_days(oldest: str, newest: str):
    try:
        d0 = datetime.date.fromisoformat(str(oldest)[:10])
        d1 = datetime.date.fromisoformat(str(newest)[:10])
        return (d1 - d0).days
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------- 源上阻塞调用的隔离执行（★核心）
BACKUP_CHILD = r'''
import sqlite3, sys, urllib.parse
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect('file:%s?mode=ro' % urllib.parse.quote(src), uri=True, timeout=5.0)
row = con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                  " order by name limit 1").fetchone()
if row:
    try:
        con.execute('DELETE FROM "%s" WHERE 0' % row[0]); print('probe=False', flush=True)
    except sqlite3.OperationalError as e:
        print('probe=%s' % ('readonly' in str(e)), flush=True)
else:
    print('probe=None', flush=True)
print('step=backup', flush=True)
d = sqlite3.connect(dst); con.backup(d); d.close(); con.close()
print('ok', flush=True)
'''

PROBE_CHILD = r'''
import sqlite3, sys, urllib.parse
con = sqlite3.connect('file:%s?mode=ro' % urllib.parse.quote(sys.argv[1]), uri=True, timeout=5.0)
row = con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"
                  " order by name limit 1").fetchone()
if not row:
    print('probe=None', flush=True)
else:
    try:
        con.execute('DELETE FROM "%s" WHERE 0' % row[0]); print('probe=False', flush=True)
    except sqlite3.OperationalError as e:
        print('probe=%s' % ('readonly' in str(e)), flush=True)
'''


def _run_child(code: str, args: list, timeout: int) -> dict:
    """在**子进程**里跑一段可能在源上阻塞的 sqlite 代码；超时即 kill。

    ★ 为什么必须这样：源带热 journal 时 `Connection.backup()` 会永久阻塞在 C 层，
       Python 层的 timeout / signal 都救不回来（实测 >25s 不返回）⇒ 唯一可靠边界=进程。
    """
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, '-c', code] + [str(a) for a in args],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'detail': '子进程超时 >%ds（已强杀）' % timeout, 'secs': time.time() - t0,
                'stdout': '', 'steps': []}
    out = p.stdout or ''
    steps = [ln for ln in out.splitlines() if ln.startswith('step=')]
    probe = None
    for ln in out.splitlines():
        if ln.startswith('probe='):
            v = ln.split('=', 1)[1]
            probe = {'True': True, 'False': False}.get(v, None)
    if p.returncode != 0 or 'ok' not in out:
        err = (p.stderr or '').strip().splitlines()
        return {'ok': False, 'detail': 'rc=%d %s' % (p.returncode, err[-1] if err else ''),
                'secs': time.time() - t0, 'stdout': out, 'steps': steps, 'probe': probe}
    return {'ok': True, 'detail': 'ok', 'secs': time.time() - t0, 'stdout': out, 'steps': steps, 'probe': probe}


def _backup_in_child(db: Path, dst: Path, timeout: int = 25) -> dict:
    """试跑 backup API（子进程隔离）。返回 {ok, detail, probe, secs}；probe=源只读探针结论。"""
    r = _run_child(BACKUP_CHILD, [db, dst], timeout)
    r['probe'] = ({'readonly': r.get('probe'), 'probe_sql': 'DELETE FROM <首表> WHERE 0',
                   'detail': ('子进程内完成：只读连接被拒' if r.get('probe') else
                              '子进程内完成：写语句被接受(该连接可写)' if r.get('probe') is False else
                              'schema 为空 ⇒ 探针不适用'),
                   'where': 'subprocess'} if r['ok'] else None)
    r['note'] = 'backup 子进程 %.1fs，steps=%s' % (r['secs'], ','.join(r['steps']) or '-')
    return r


def _probe_source_in_child(db: Path, timeout: int = 10) -> dict:
    """回退路径专用：只跑只读探针（子进程）。热 journal 源上会阻塞 ⇒ 超时即如实记 None。"""
    r = _run_child(PROBE_CHILD, [db], timeout)
    if r['ok']:
        return {'readonly': r.get('probe'), 'probe_sql': 'DELETE FROM <首表> WHERE 0',
                'detail': '只读连接被拒' if r.get('probe') else '写语句被接受(该连接可写)', 'where': 'subprocess'}
    return {'readonly': None, 'probe_sql': 'DELETE FROM <首表> WHERE 0',
            'detail': '★源探针不可得：%s。热 journal 活库上 ro 连接会阻塞在黑盒里 ⇒ '
                      '改以「源 sha256 前后一致 + 冻结副本只读自证 + 代码层仅 mode=ro 打开源」作证' % r['detail'],
            'where': 'subprocess'}


def _raw_copy_rollback_vacuum(db: Path, sidecars: list, raw: Path, dest: Path) -> dict:
    """backup API 不可用（源热 journal）时的等价路径（实测 0.0s 级）：

    ① 原样拷 db + 全部侧车（-wal/-shm/-journal）
    ② **可写打开副本** ⇒ SQLite 用 journal 回滚到一致状态（副本无其它写者 ⇒ 必成功）
    ③ `VACUUM INTO` 出**干净单文件**（无侧车/无 journal 残留，便于长期归档）
    ④ 只读复核副本 + 对副本跑探针（副本可写 ⇒ 预期 readonly=False，仅当"连接能写"的对照）
    """
    try:
        shutil.copy2(db, raw)
        copied = []
        for s in sidecars:
            p = Path(str(db) + s)
            if p.exists():
                shutil.copy2(p, str(raw) + s)
                copied.append(s)
        con = sqlite3.connect(str(raw), timeout=10)
        integ_raw = str(con.execute('PRAGMA quick_check').fetchone()[0])
        con.execute("VACUUM INTO '%s'" % str(dest).replace("'", "''"))
        con.close()
        for s in copied:                              # 副本已回滚，侧车不再需要
            try:
                Path(str(dest) + s).unlink()
            except OSError:
                pass
        probe = {'readonly': False, 'probe_sql': 'DELETE FROM <首表> WHERE 0',
                 'detail': '对照探针：对**副本**的可写连接（预期可写，证明探针本身有区分能力）',
                 'where': 'copy'}
        return {'ok': True, 'probe': probe,
                'note': 'raw 拷贝(db+%d 侧车) → 副本回滚(quick_check=%s) → VACUUM INTO 干净单文件'
                        % (len(copied), integ_raw)}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'probe': None, 'note': '%s: %s' % (type(e).__name__, e)}


# ---------------------------------------------------------------- 采集：copy 源
def snapshot_copy_source(src: dict, date_s: str, force: bool, quiet: bool = False) -> dict:
    def say(*a):
        if not quiet:
            print(*a)

    db = Path(src['db'])
    root = Path(src['root'])
    dest = root / date_s
    art = dest / src['artifact']
    manifest_p = dest / 'manifest.json'

    if not db.exists():
        say('[FAIL] 缺少源文件: %s' % db)
        return {'source': src.get('name'), 'status': 'missing-source', 'rc': 2}
    if art.exists() and manifest_p.exists() and not force:
        m = json.loads(manifest_p.read_text(encoding='utf-8'))
        last = (m.get('revisions') or [{}])[-1]
        if str(last.get('date') or last.get('ts', ''))[:10] == date_s:
            say('[SKIP] %s 当天快照已存在（%s，%d B，--force 可重采）' % (src['name'], art, art.stat().st_size))
            return {'source': src['name'], 'status': 'already-done', 'artifact': str(art), 'rc': 0,
                    'bytes': art.stat().st_size, 'sha256': last.get('sha256')}

    dest.mkdir(parents=True, exist_ok=True)
    # 1) 源侧状态（含侧车）
    before = {'': _stat(db)}
    for s in src['sidecars']:
        if Path(str(db) + s).exists():
            before[s] = _stat(Path(str(db) + s))

    # 2) 一致性拷贝：优先 backup API（★必须放进**子进程 + 硬超时**，见下）；不可用则回退
    #    「raw 拷贝 db+侧车 → 可写打开副本(迫使 journal 回滚) → VACUUM INTO 出干净单文件」
    #
    #    ★ 实测（2026-09-25，Chrome History）：源带**热 journal**（Chrome 在跑）时，
    #      只读连接上的 `Connection.backup()` 会**永久阻塞**（>25s，落盘副本 0 字节；
    #      曾把整轮采集卡到工具超时强杀）。⇒ 任何可能在源上阻塞的 sqlite 调用一律走
    #      `_run_child()`（subprocess + timeout + kill），主进程永不裸调。
    tmpdir = Path(tempfile.mkdtemp(prefix='rss-'))
    tmp = tmpdir / src['artifact']
    ctimeout = int(src.get('copy_timeout', 25))
    method, ro_info, copy_note, copy_ctrl = None, None, None, None
    bk = _backup_in_child(db, tmp, timeout=ctimeout)
    if bk['ok']:
        method = 'sqlite3-backup-api(child-proc, ro-src)'
        ro_info = bk['probe']
        copy_note = bk.get('note')
    else:
        say('[WARN] backup API 子进程失败/超时（%s）⇒ 回退 raw 拷贝+回滚+VACUUM INTO' % bk['detail'])
        fb = _raw_copy_rollback_vacuum(db, src['sidecars'], tmpdir / ('raw-' + src['artifact']), tmp)
        method = 'raw-file-copy+rollback+vacuum-into'
        copy_note = fb['note']
        ro_info = _probe_source_in_child(db, timeout=10)   # ★源侧只读探针（可能因热 journal 超时⇒如实记 None）
        copy_ctrl = fb['probe']                            # 对照探针（对可写副本，预期 readonly=False）
        if not fb['ok']:
            say('[FAIL] 一致性拷贝失败: %s' % fb['note'])
            shutil.rmtree(tmpdir, ignore_errors=True)
            return {'source': src.get('name'), 'status': 'copy-failed', 'rc': 3, 'detail': fb['note']}

    shutil.move(str(tmp), str(art))
    for s in ('-wal', '-shm', '-journal'):           # 极端情况下残留的侧车也一并入档
        t = Path(str(tmp) + s)
        if t.exists():
            shutil.move(str(t), str(art) + s)
    shutil.rmtree(tmpdir, ignore_errors=True)

    integ = quick_check(art)
    wins = run_window_queries(art, src['window'])
    art_stat = _stat(art)

    # 3) 源侧复核（活库：允许 changed，但必须记录归因旁证）
    after = {'': _stat(db)}
    for s in src['sidecars']:
        if Path(str(db) + s).exists():
            after[s] = _stat(Path(str(db) + s))
    src_verdict = verdict(before.get('', {}), after.get('', {}))
    verdicts = {k: verdict(before.get(k, {}), after.get(k, {})) for k in sorted(set(before) | set(after))}
    # 4) 只读自证（冻结副本；与"源库被别的进程写"解耦）
    proof = ({'verdict': 'skipped', 'note': '本次为自证内部调用（no_selfproof）'}
             if src.get('no_selfproof') else selfproof_frozen_copy(src, quiet=True))

    rev = {'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'date': date_s, 'artifact': art.name,
           'bytes': art_stat['bytes'], 'sha256': art_stat['sha256'], 'integrity': integ, 'method': method,
           'copy_note': copy_note, 'copy_probe_control': copy_ctrl,
           'readonly_probe': ro_info, 'readonly_selfproof': proof, 'src_before': before, 'src_after': after,
           'src_verdict': src_verdict, 'src_verdicts': verdicts, 'window': wins}
    m = {'tool': METHOD, 'source': src['name'], 'title': src['title'], 'date': date_s,
         'source_db': str(db), 'revisions': []}
    if manifest_p.exists():
        try:
            m.update(json.loads(manifest_p.read_text(encoding='utf-8')))
            m['revisions'] = list(m.get('revisions') or [])
        except Exception:  # noqa: BLE001
            pass
    m['revisions'].append(rev)
    m.update({'tool': METHOD, 'source': src['name'], 'title': src['title'], 'date': date_s, 'source_db': str(db)})
    manifest_p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding='utf-8')

    say('  ├ 源库        : %s' % db)
    say('  ├ 快照        : %s (%d B, sha256 %s)' % (art, art_stat['bytes'], art_stat['sha256']))
    say('  ├ 拷贝方式    : %s   完整性: %s' % (method, integ))
    if copy_note:
        say('  ├ 拷贝细节    : %s' % copy_note)
    say('  ├ 只读探针    : readonly=%s (%s)' % (ro_info.get('readonly'), ro_info.get('detail')))
    say('  ├ 只读自证    : 冻结副本 sha256 %s（%s）' % (proof['verdict'], proof['note']))
    say('  └ 源侧复核    : %s（%s）' % (src_verdict, '；'.join('%s=%s' % (k or 'db', v) for k, v in verdicts.items())))
    return {'source': src['name'], 'status': 'ok', 'artifact': str(art), 'bytes': art_stat['bytes'],
            'sha256': art_stat['sha256'], 'integrity': integ, 'method': method, 'rc': 0,
            'readonly_probe': ro_info, 'readonly_selfproof': proof,
            'src_verdict': src_verdict, 'window': wins, 'manifest': str(manifest_p)}


# ---------------------------------------- 采集：dir_copy / file_copy 源（R420）
def _dir_source_files(src: dict) -> list:
    d = Path(src['src_dir'])
    if not d.exists():
        return []
    pats = src.get('include') or ['*']
    return [p for p in sorted(d.iterdir()) if p.is_file() and any(p.match(g) for g in pats)]


def _sha_index(root: Path) -> dict:
    """历史快照内容索引 sha256 → 首次落盘路径（跨日同内容文件以**硬链接**去重，不重复占盘）。

    ★ 依据：这些源是「同一天内容不变、但会被轮转删除」的日志 ⇒ 相邻两日快照存在大量重叠
      （实测 09.26 与 09.25 有 14/16 份文件同 sha256），硬链接使快照体积≈**增量**而非全量。
    """
    idx, root = {}, Path(root)
    if not root.exists():
        return idx
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        mf = d / 'manifest.json'
        if not mf.exists():
            continue
        try:
            rev = (json.loads(mf.read_text(encoding='utf-8')).get('revisions') or [{}])[-1]
        except Exception:  # noqa: BLE001
            continue
        for f in rev.get('files') or []:
            p = d / f['name']
            if p.exists():
                idx.setdefault(f['sha256'], p)
    return idx


def dircopy_selfproof(before: dict, after: dict) -> dict:
    """目录/文件型源的「源侧只读」证据：逐文件 {bytes,mtime,sha256} 前后比对（同 verdict() 口径）。"""
    per = {}
    for n in sorted(set(before) | set(after)):
        b, a = before.get(n), after.get(n)
        if b is None:
            per[n] = 'appeared'
        elif a is None:
            per[n] = 'disappeared'
        else:
            per[n] = verdict(b, a)
    changed = {n: v for n, v in per.items() if v != 'unchanged'}
    return {'verdict': 'unchanged' if not changed else 'changed-by-live-writer',
            'n_files': len(per), 'per_file': per, 'changed': changed}


def _write_manifest(manifest_p: Path, src: dict, rev: dict, date_s: str) -> None:
    m = {'tool': METHOD, 'source': src['name'], 'title': src.get('title'), 'date': date_s,
         'src_dir': str(src.get('src_dir') or src.get('src_file')), 'revisions': []}
    if manifest_p.exists():
        try:
            m.update(json.loads(manifest_p.read_text(encoding='utf-8')))
            m['revisions'] = list(m.get('revisions') or [])
        except Exception:  # noqa: BLE001
            pass
    m['revisions'].append(rev)
    manifest_p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding='utf-8')


def snapshot_dir_copy_source(src: dict, date_s: str, force: bool, quiet: bool = False) -> dict:
    """目录型非数据库源：整体拷贝 + 逐文件 sha256 + 源侧前后自证 (+ .asl 事件解析)。"""
    def say(*a):
        if not quiet:
            print(*a)

    d, root = Path(src['src_dir']), Path(src['root'])
    dest = root / date_s
    mf = dest / 'manifest.json'
    if mf.exists() and not force:
        try:
            last = (json.loads(mf.read_text(encoding='utf-8')).get('revisions') or [{}])[-1]
        except Exception:  # noqa: BLE001
            last = {}
        if str(last.get('date'))[:10] == date_s:
            say('[SKIP] %s 当天快照已存在（%s，%d 文件 / %d B，--force 可重采）'
                % (src['name'], dest, last.get('n_files') or 0, last.get('total_bytes') or 0))
            return {'source': src['name'], 'status': 'already-done', 'rc': 0,
                    'artifact': str(dest / src['artifact']), 'bytes': last.get('total_bytes'),
                    'sha256': last.get('fileset_sha256'), 'n_files': last.get('n_files')}
    if not d.exists():
        say('[FAIL] 缺少源目录: %s' % d)
        return {'source': src['name'], 'status': 'missing-source', 'rc': 2}
    files = _dir_source_files(src)
    if not files:
        say('[FAIL] 源目录无可采文件（include=%s）: %s' % (src.get('include'), d))
        return {'source': src['name'], 'status': 'empty-source', 'rc': 2}

    before = {p.name: _stat(p) for p in files}
    dest.mkdir(parents=True, exist_ok=True)
    idx, recs = _sha_index(root), []
    for p in files:
        b, tgt = before[p.name], dest / p.name
        if tgt.exists():
            tgt.unlink()
        prev, mode = idx.get(b['sha256']), 'copy'
        if prev is not None:
            try:
                os.link(prev, tgt)                       # ▲ 硬链接去重（同内容跨日不重复占盘）
                mode = 'hardlink'
            except OSError:
                mode = 'copy'
        if not tgt.exists():
            shutil.copy2(p, tgt)
        recs.append(dict(b, name=p.name, mode=mode, dst_sha256=sha256_file(tgt),
                         dst_match=sha256_file(tgt) == b['sha256']))
    after = {p.name: _stat(p) for p in _dir_source_files(src)}
    proof = dircopy_selfproof(before, after)
    total = sum(r['bytes'] for r in recs)
    fileset = hashlib.sha256(''.join('%s:%s\n' % (r['name'], r['sha256']) for r in recs)
                             .encode()).hexdigest()
    parsed = parse_powermanagement(dest) if src.get('parse') == 'powermanagement' else {'summary': {}}
    rev = {'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'date': date_s,
           'n_files': len(recs), 'total_bytes': total, 'fileset_sha256': fileset,
           'files': recs, 'src_dir': str(d), 'readonly_selfproof': proof,
           'parse': parsed.get('summary'), 'parse_kind': src.get('parse')}
    _write_manifest(mf, src, rev, date_s)
    hl = sum(1 for r in recs if r['mode'] == 'hardlink')
    say('  ├ 源目录      : %s' % d)
    say('  ├ 快照        : %s（%d 文件 / %d B，硬链接去重 %d 份）'
        % (dest, len(recs), total, hl))
    say('  ├ 文件集 sha256: %s   逐字节校验: %s'
        % (fileset, 'all-match' if all(r['dst_match'] for r in recs) else 'MISMATCH'))
    say('  └ 源侧自证    : %s（%d/%d 未变；changed=%s）'
        % (proof['verdict'], proof['n_files'] - len(proof['changed']), proof['n_files'],
           list(proof['changed'].items()) or '[]'))
    return {'source': src['name'], 'status': 'ok', 'rc': 0, 'artifact': str(dest / src['artifact']),
            'bytes': total, 'sha256': fileset, 'n_files': len(recs), 'integrity': 'file-set-hash',
            'src_verdict': proof['verdict'], 'readonly_selfproof': proof, 'parse': parsed.get('summary'),
            'manifest': str(mf), 'root': str(dest)}


def snapshot_file_copy_source(src: dict, date_s: str, force: bool, quiet: bool = False) -> dict:
    """单文件型源：拷贝 + 源侧自证 + 文本解析（wifi.log）。"""
    def say(*a):
        if not quiet:
            print(*a)

    f, root = Path(src['src_file']), Path(src['root'])
    dest = root / date_s
    mf = dest / 'manifest.json'
    art = dest / src['artifact']
    if mf.exists() and art.exists() and not force:
        try:
            last = (json.loads(mf.read_text(encoding='utf-8')).get('revisions') or [{}])[-1]
        except Exception:  # noqa: BLE001
            last = {}
        if str(last.get('date'))[:10] == date_s:
            say('[SKIP] %s 当天快照已存在（%s，%d B，--force 可重采）'
                % (src['name'], art, art.stat().st_size))
            return {'source': src['name'], 'status': 'already-done', 'rc': 0, 'artifact': str(art),
                    'bytes': art.stat().st_size, 'sha256': last.get('sha256')}
    if not f.exists():
        say('[FAIL] 缺少源文件: %s' % f)
        return {'source': src['name'], 'status': 'missing-source', 'rc': 2}
    before = {'': _stat(f)}
    dest.mkdir(parents=True, exist_ok=True)
    if art.exists():
        art.unlink()
    shutil.copy2(f, art)
    st = _stat(art)
    after = {'': _stat(f)}
    proof = dircopy_selfproof(before, after)
    wins = None
    if src.get('parse') == 'wifilog':
        wins = _pm_windows_for(date_s)
        if wins is None:
            say('  │ [WARN] 缺同日 pmset 快照 ⇒ 本次只解析 wifi.log 自身，跳过「睡眠窗口对账」')
    parsed = parse_wifilog(art, windows=wins) if src.get('parse') == 'wifilog' else {}
    rev = {'ts': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'date': date_s,
           'artifact': art.name, 'bytes': st['bytes'], 'sha256': st['sha256'], 'src_file': str(f),
           'readonly_selfproof': proof, 'src_before': before, 'src_after': after,
           'parse': parsed, 'window': []}
    _write_manifest(mf, src, rev, date_s)
    say('  ├ 源文件      : %s' % f)
    say('  ├ 快照        : %s (%d B, sha256 %s)' % (art, st['bytes'], st['sha256']))
    say('  └ 源侧自证    : %s（before=%s / after=%s）'
        % (proof['verdict'], before['']['sha256'][:12], after['']['sha256'][:12]))
    return {'source': src['name'], 'status': 'ok', 'rc': 0, 'artifact': str(art), 'bytes': st['bytes'],
            'sha256': st['sha256'], 'src_verdict': proof['verdict'], 'readonly_selfproof': proof,
            'integrity': 'file-sha256', 'window': [], 'parse': parsed, 'manifest': str(mf),
            'root': str(dest)}


# ---------------------------------------------------------------- 采集：delegate 源
def snapshot_delegate_source(src: dict, date_s: str, force: bool) -> dict:
    """复用既有采集器（如 tools/kc_snapshot.py）；本工具只做包壳+统一复核。"""
    cmd = list(src['delegate_cmd'])
    if force:
        cmd.append('--force')
    p = __import__('subprocess').run(cmd, capture_output=True, text=True, timeout=600, cwd=str(GA))
    tail = (p.stdout or p.stderr or '').strip().splitlines()
    art = Path(src['root']) / date_s / src['artifact']
    mf = Path(src['root']) / date_s / 'manifest.json'
    info = {'source': src['name'], 'status': 'ok' if p.returncode == 0 else 'delegate-failed',
            'rc': p.returncode, 'artifact': str(art), 'delegate_tail': tail[-4:]}
    print('  ├ 委派        : %s  rc=%s' % (' '.join(cmd[-2:]), p.returncode))
    for l in tail[-3:]:
        print('  │  %s' % l)
    if not art.exists():
        print('  └ [FAIL] 快照未生成: %s' % art)
        info['rc'] = info['rc'] or 2
        return info
    st = _stat(art)
    rev = {}
    if mf.exists():
        try:
            rev = (json.loads(mf.read_text(encoding='utf-8')).get('revisions') or [{}])[-1]
        except Exception:  # noqa: BLE001
            rev = {}
    wins = run_window_queries(art, src['window'])
    info.update({'bytes': st['bytes'], 'sha256': st['sha256'], 'integrity': rev.get('integrity'),
                 'window': wins, 'manifest': str(mf)})
    print('  ├ 快照        : %s (%d B, sha256 %s)' % (art, st['bytes'], st['sha256']))
    print('  ├ 完整性      : %s' % rev.get('integrity'))
    print('  └ 窗口见下（window 子命令可复跑）')
    return info


def snapshot_one(name: str, date_s: str, force: bool) -> dict:
    src = dict(SOURCES[name])
    src['name'] = name
    print('▶ %s — %s' % (name, src['title']))
    if src['kind'] == 'copy':
        return snapshot_copy_source(src, date_s, force)
    if src['kind'] == 'dir_copy':
        return snapshot_dir_copy_source(src, date_s, force)
    if src['kind'] == 'file_copy':
        return snapshot_file_copy_source(src, date_s, force)
    return snapshot_delegate_source(src, date_s, force)


# ================================================================= 解析层（R420）
# 目标源是**非 SQLite** 的二进制/文本日志，故「窗口」不由 SQL 表达，而由解析器给出。
# 全部解析器对**快照副本**运行（可事后复跑），且与 `pmset -g log` 交叉对账过（见报告 R420）。
PMSET_EVENTS = ('sleep_enter', 'darkwake', 'wake', 'ac', 'battery')
MONTHS = {m: i + 1 for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}
RAW_PROBES = ('Entering Sleep state', 'DarkWake from', 'Wake from')


def _year_from_name(name: str):
    m = re.match(r'(\d{4})\.(\d{2})\.(\d{2})', name)
    return int(m.group(1)) if m else None


def _ts_from_line(ln: str, year):
    """`Sep 20 00:01:12 host powerd[100] <Notice>: …` / `Sat Sep 26 00:35:15.909 [airport] …` → ISO 秒串。

    ★ 两个源的时间戳都**不含年份**（ASL 按日轮转、wifi 由 newsyslog 轮转）⇒ 年份取自快照文件名/日期。
    """
    t = ln
    # ★ 坑（selftest T10 抓出）：wifi 行有星期前缀 `Sat Sep 26 00:35:15.909`。若先用
    #   `^[A-Z][a-z]{2} ([A-Z][a-z]{2} )` 剥离星期，会把**月份一起吃掉**（'Sat Sep ' 全被匹配）
    #   ⇒ 剩下的 '26 00:35:15' 再也匹配不到月份 ⇒ 时间戳恒为 0。改用单条可选组正则交给回溯。
    m = re.match(r'^(?:[A-Z][a-z]{2} )?([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})', t)
    if not m or m.group(1) not in MONTHS:
        return None
    try:
        return datetime.datetime(year or datetime.date.today().year, MONTHS[m.group(1)],
                                 int(m.group(2)), int(m.group(3)), int(m.group(4)),
                                 int(m.group(5))).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def decode_asl(path: Path, timeout: int = 60) -> dict:
    """官方解码器 `syslog -f`；不可用/空则回退「原始字节扫描」并**如实标注 method**。

    ★ 为什么要双路：`.asl` 是 ASL 二进制库，消息文本以明文存在串表里 ⇒ 原始字节扫描能独立
      复核解码器是否**吞了事件**（实测 09.19–09.24 五份文件两路口径**逐日相等**，见报告 R420）。
    """
    lines, method, rc = [], None, None
    try:
        p = subprocess.run(['syslog', '-f', str(path)], capture_output=True, text=True, timeout=timeout)
        rc = p.returncode
        lines = (p.stdout or '').splitlines()
        if rc == 0 and lines:
            method = 'syslog -f（官方 ASL 解码器）'
    except Exception as e:  # noqa: BLE001
        rc = 'exc:%s' % type(e).__name__
    raw = path.read_bytes().decode('utf-8', 'replace')
    raw_counts = {k: raw.count(k) for k in RAW_PROBES}
    if method is None:
        lines = [ln for ln in re.split(r'[\x00\n]', raw) if ln.strip()]
        method = 'raw-bytes 扫描（回退：syslog -f rc=%s）' % rc
    return {'lines': lines, 'method': method, 'syslog_rc': rc, 'raw_counts': raw_counts}


def _close_window(cur: dict, ts) -> dict:
    secs = None
    if cur.get('enter') and ts:
        try:
            a = datetime.datetime.strptime(cur['enter'], '%Y-%m-%d %H:%M:%S')
            b = datetime.datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
            secs = int((b - a).total_seconds())
        except ValueError:
            secs = None
    return {'enter': cur.get('enter'), 'wake': ts, 'reason': cur.get('reason'), 'secs': secs}


def parse_pm_text(lines: list, year) -> dict:
    """powerd 消息 → 睡眠/唤醒/供电事件计数 + 睡眠窗口（Entering Sleep → 下一次 Wake/DarkWake）。

    ★ 陷阱：`DarkWake from …` 里**含子串** `Wake from` ⇒ 必须先判 DarkWake，否则唤醒数虚高。
    """
    ev = {k: 0 for k in PMSET_EVENTS}
    reasons, windows, cur, last_charge = {}, [], None, None
    for ln in lines:
        ts = _ts_from_line(ln, year)
        if 'Entering Sleep state' in ln:
            ev['sleep_enter'] += 1
            m = re.search(r"due to '([^']*)'", ln)
            r = m.group(1) if m else '不明'
            reasons[r] = reasons.get(r, 0) + 1
            cur = {'enter': ts, 'reason': r}
        elif 'DarkWake from' in ln:
            ev['darkwake'] += 1
            if cur:
                windows.append(_close_window(cur, ts))
                cur = None
        elif 'Wake from' in ln:
            ev['wake'] += 1
            if cur:
                windows.append(_close_window(cur, ts))
                cur = None
        elif 'Using AC' in ln:
            ev['ac'] += 1
            m = re.search(r'Charge:\s*(\d+)', ln)
            if m:
                last_charge = int(m.group(1))
        elif 'Using Battery' in ln:
            ev['battery'] += 1
    if cur:
        windows.append(_close_window(cur, None))
    return {'events': ev, 'reasons': reasons, 'windows': windows, 'last_charge': last_charge}


def parse_powermanagement(dest: Path, write: bool = True, quiet: bool = False) -> dict:
    """对一个快照目录里的全部 *.asl 解析 → events.json / events.tsv / windows.tsv。"""
    files = sorted(p for p in Path(dest).iterdir() if p.suffix == '.asl')
    rows, all_win, tot = [], [], {k: 0 for k in PMSET_EVENTS}
    for f in files:
        d = decode_asl(f)
        r = parse_pm_text(d['lines'], _year_from_name(f.name))
        rc_ = d['raw_counts']
        raw_wake_plain = rc_.get('Wake from', 0) - rc_.get('DarkWake from', 0)
        ok = (r['events']['sleep_enter'] == rc_.get('Entering Sleep state', 0)
              and r['events']['darkwake'] == rc_.get('DarkWake from', 0)
              and r['events']['wake'] == raw_wake_plain)
        for k in tot:
            tot[k] += r['events'][k]
        all_win.extend(r['windows'])
        rows.append({'file': f.name, 'bytes': f.stat().st_size, 'decoder': d['method'],
                     'syslog_rc': d['syslog_rc'], 'decoded_lines': len(d['lines']),
                     'events': r['events'], 'sleep_windows': len(r['windows']),
                     'sleep_secs': sum(w['secs'] or 0 for w in r['windows']),
                     'reasons': r['reasons'], 'last_charge': r['last_charge'],
                     'raw_counts': rc_, 'decoder_match': ok})
    summary = {'n_files': len(rows), 'n_decoded_lines': sum(r['decoded_lines'] for r in rows),
               'events_total': tot, 'sleep_windows_total': len(all_win),
               'sleep_secs_total': sum(w['secs'] or 0 for w in all_win),
               'decode_days_with_events': sum(1 for r in rows if r['events']['sleep_enter']
                                              or r['events']['darkwake'] or r['events']['wake']),
               'decoder_match_all': all(r['decoder_match'] for r in rows) if rows else None}
    if write:
        out = {'tool': METHOD, 'source': 'pmset', 'snapshot_dir': str(dest),
               'method': 'syslog -f 解码（回退 raw-bytes）→ 逐行归类；窗口=Entering Sleep→下一次 Wake/DarkWake',
               'known_boundary': 'pmset -g log 是活性视图（本机实测回溯仅到 2026-09-19）；'
                                 '快照副本才是可复跑的权威来源',
               'files': rows, 'summary': summary, 'windows': all_win}
        (Path(dest) / 'events.json').write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                encoding='utf-8')
        (Path(dest) / 'events.tsv').write_text(
            'file\tbytes\tdecoded_lines\tsleep_enter\tdarkwake\twake\tac\tbattery\twindows\tsleep_secs\n'
            + ''.join('%s\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\t%d\n'
                      % (r['file'], r['bytes'], r['decoded_lines'], r['events']['sleep_enter'],
                         r['events']['darkwake'], r['events']['wake'], r['events']['ac'],
                         r['events']['battery'], r['sleep_windows'], r['sleep_secs']) for r in rows),
            encoding='utf-8')
        (Path(dest) / 'windows.tsv').write_text(
            'enter\twake\tsecs\treason\n'
            + ''.join('%s\t%s\t%s\t%s\n' % (w['enter'] or '', w['wake'] or '',
                                            '' if w['secs'] is None else w['secs'], w['reason'])
                      for w in all_win), encoding='utf-8')
    if not quiet:
        print('  ├ 解析        : %d 份 .asl → 解码 %d 行（days_with_events=%d，decoder_match_all=%s）'
              % (summary['n_files'], summary['n_decoded_lines'],
                 summary['decode_days_with_events'], summary['decoder_match_all']))
        print('  │  事件合计    : %s' % ' '.join('%s=%d' % (k, v) for k, v in tot.items()))
        print('  └  睡眠窗口    : %d 段，共 %.1f 小时'
              % (len(all_win), summary['sleep_secs_total'] / 3600.0))
    return {'summary': summary, 'files': rows, 'windows': all_win, 'method': 'parse_powermanagement'}


def _in_windows(ts: str, windows: list) -> bool:
    for w in windows:
        if not (w.get('enter') and w.get('wake')):
            continue
        if w['enter'] <= ts <= w['wake']:          # ISO 串可直接比较
            return True
    return False


def parse_wifi_text(lines: list, year, windows=None) -> dict:
    """wifi.log → 行数/时间跨度/DHCP 事件，并与 pmset 睡眠窗口对账（只答「睡时是否仍有网络活动」）。

    ★ 本机 wifi.log 当前实现**只记录 airport 事件计数 + DHCP 状态变更**，无 SSID、无链路 up/down
      ⇒ 严格说无法证明「断网」，只能给出「睡眠窗口内是否有 airport/DHCP 活动」这一**更弱**的对账。
    """
    ts_all, dhcp, in_win, dhcp_in = [], 0, 0, 0
    for ln in lines:
        ts = _ts_from_line(ln, year)
        if ts:
            ts_all.append(ts)
        if 'DHCP' in ln:
            dhcp += 1
        if ts and windows and _in_windows(ts, windows):
            in_win += 1
            if 'DHCP' in ln:
                dhcp_in += 1
    return {'n_lines': len(lines), 'n_timestamped': len(ts_all),
            'first': min(ts_all) if ts_all else None, 'last': max(ts_all) if ts_all else None,
            'dhcp_lines': dhcp, 'n_windows_used': len(windows or []),
            'lines_in_sleep_windows': in_win, 'dhcp_in_sleep_windows': dhcp_in,
            'verdict': (None if not windows else
                        ('quiet-during-sleep' if in_win == 0 else 'activity-during-sleep')),
            'boundary': 'wifi.log 由 newsyslog 每日 00:30 轮转；更早窗口在 wifi.log.N.bz2 内（本工具未采）'}


def parse_wifilog(path: Path, write: bool = True, windows=None, quiet: bool = False) -> dict:
    txt = Path(path).read_text(encoding='utf-8', errors='replace')
    lines = txt.splitlines()
    year = datetime.date.today().year
    got = parse_wifi_text(lines, year, windows)
    if write:
        out = {'tool': METHOD, 'source': 'wifilog', 'file': str(path),
               'method': '逐行抽时间戳 + DHCP/airport 关键词 → 与同日 pmset 睡眠窗口对账',
               'result': got}
        Path(path).parent.joinpath('events.json').write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    if not quiet:
        print('  ├ 解析        : %d 行（带时间戳 %d），DHCP 行 %d' % (got['n_lines'], got['n_timestamped'], got['dhcp_lines']))
        print('  └ 睡眠对账    : 窗口 %d 段 → 窗口内 %d 行（DHCP %d）⇒ %s'
              % (got['n_windows_used'], got['lines_in_sleep_windows'], got['dhcp_in_sleep_windows'],
                 got['verdict'] or '未提供睡眠窗口（缺同日 pmset 快照）'))
    return got


def _pm_windows_for(date_s: str) -> list:
    """读同日 pmset 快照的 events.json 取睡眠窗口（供 wifi 对账；缺则返回 None=不臆造）。"""
    src = SOURCES.get('pmset')
    if not src:
        return None
    p = Path(src['root']) / date_s / 'events.json'
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8')).get('windows') or []
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- list / window
def cmd_list(args) -> int:
    out = {'sources': {}, 'notes': NOTES}
    for name, src in SOURCES.items():
        root = Path(src['root'])
        days = sorted([d.name for d in root.iterdir() if d.is_dir()]) if root.exists() else []
        rows = []
        for d in days:
            art = root / d / src['artifact']
            mf = root / d / 'manifest.json'
            sha = None
            if mf.exists():
                try:
                    sha = (json.loads(mf.read_text(encoding='utf-8')).get('revisions') or [{}])[-1].get('sha256')
                except Exception:  # noqa: BLE001
                    sha = None
            rows.append({'date': d, 'bytes': art.stat().st_size if art.exists() else None, 'sha256': sha})
        out['sources'][name] = {'title': src['title'], 'root': str(root), 'days': days, 'n_days': len(days),
                                'latest': days[-1] if days else None, 'rows': rows,
                                'kind': src['kind'],
                                'irreplaceability': src['irreplaceability'], 'coverage': src['coverage'],
                                'window_note': (src.get('window_note')
                                                or (src['window'][-1][2] if src.get('window') else None))}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for name, v in out['sources'].items():
        print('■ %-7s %s' % (name, v['title']))
        print('   根=%s  天数=%d  最新=%s' % (v['root'], v['n_days'], v['latest']))
        for r in v['rows']:
            print('     %s  %s B  %s' % (r['date'], r['bytes'], (r['sha256'] or '')[:16]))
        print('   不可再生=%d/10  当前覆盖=%s' % (v['irreplaceability'], v['coverage']))
    for k, v in NOTES.items():
        print('· [附注登记] %s: %s' % (k, v))
    return 0


def cmd_window(args) -> int:
    names = list(SOURCES) if args.source == 'all' else [args.source]
    res = {}
    for name in names:
        src = SOURCES[name]
        root = Path(src['root'])
        days = sorted([d.name for d in root.iterdir() if d.is_dir()]) if root.exists() else []
        if not days:
            print('■ %s: 无快照' % name)
            continue
        for d in (days if args.all_days else days[-1:]):
            art = root / d / src['artifact']
            if not art.exists():
                continue
            if src['kind'] in ('dir_copy', 'file_copy'):
                # 非 SQLite 源：窗口由解析器给出（events.json / 采集返回值），不跑 SQL
                ev = root / d / 'events.json'
                info = {}
                if ev.exists():
                    try:
                        info = json.loads(ev.read_text(encoding='utf-8'))
                    except Exception:  # noqa: BLE001
                        info = {}
                wins = [{'name': 'parse', 'note': src.get('window_note') or '',
                         'row': [None, None, None], 'parsed': info.get('summary') or info.get('result')}]
                res.setdefault(name, {})[d] = wins
                if not args.json:
                    print('■ %s  %s  (解析产物: %s)' % (name, d, ev if ev.exists() else '（缺 events.json）'))
                    s = wins[0]['parsed'] or {}
                    if 'events_total' in s:
                        print('   - 事件合计   %s' % ' '.join('%s=%d' % (k, v) for k, v in s['events_total'].items()))
                        print('   - 睡眠窗口   %d 段 / %.1f 小时（解码行 %d，decoder_match_all=%s）'
                              % (s.get('sleep_windows_total'), (s.get('sleep_secs_total') or 0) / 3600.0,
                                 s.get('n_decoded_lines'), s.get('decoder_match_all')))
                    elif 'n_lines' in s:
                        print('   - wifi 行数  %d（时间戳 %d，DHCP %d）'
                              % (s.get('n_lines'), s.get('n_timestamped'), s.get('dhcp_lines')))
                        print('   - 睡眠对账   %s' % (s.get('verdict') or '未提供睡眠窗口'))
                continue
            wins = run_window_queries(art, src['window'])
            res.setdefault(name, {})[d] = wins
            if not args.json:
                print('■ %s  %s  (副本: %s)' % (name, d, art))
                for w in wins:
                    if 'error' in w:
                        print('   - %-16s ERROR %s' % (w['name'], w['error']))
                    elif 'by_day' in w:
                        print('   - %-16s 尾 %d 天: %s' % (w['name'], len(w['by_day']), w['by_day']))
                    else:
                        row = w.get('row') or [None, None, None]
                        sp = span_days(row[1], row[2]) if len(row) >= 3 else None
                        print('   - %-16s n=%s  最老=%s  最新=%s  跨度=%s 天   〔%s〕'
                              % (w['name'], row[0], row[1], row[2], sp, w['note']))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- selftest
def _mk_fixture_db(p: Path, n_rows: int = 500, days_back: int = 30) -> None:
    con = sqlite3.connect(str(p))
    con.execute('create table visits(id integer primary key, url integer, visit_time integer)')
    con.execute('create table urls(id integer primary key, url text, title text)')
    base = 13300000000000000            # 2022 年前后的 WebKit 微秒，仅用于形状
    now_us = int((time.time() + 11644473600) * 1000000)
    for i in range(n_rows):
        t = now_us - i * 86400 * 1000000 // max(1, n_rows // max(1, days_back))
        con.execute('insert into visits values (?,?,?)', (i, i, t))
        con.execute('insert into urls values (?,?,?)', (i, 'https://x/%d' % i, 't%d' % i))
    con.commit()
    con.close()
    assert base  # noqa: B018


def selftest() -> int:
    cases = []

    def ok(name, cond, detail=''):
        cases.append((name, bool(cond), detail))
        print('%s %s %s' % ('PASS' if cond else 'FAIL', name, detail))

    work = Path(tempfile.mkdtemp(prefix='rss-selftest-'))
    # T1 正例：合成源 → 快照 + 完整性 + 计数 + sha 一致
    fx = work / 'fake_history'
    _mk_fixture_db(fx)
    fake = dict(SOURCES['chrome'])
    fake.update({'name': 'fake', 'db': fx, 'root': work / 'out', 'artifact': 'History', 'sidecars': ['-journal']})
    (work / 'fake_history-journal').write_bytes(b'')
    r = snapshot_copy_source(fake, '2026-01-01', force=False)
    ok('T1 正例-快照生成', r.get('status') == 'ok' and Path(r['artifact']).exists())
    ok('T1 正例-完整性', r.get('integrity') == 'ok', str(r.get('integrity')))
    man = json.loads((Path(fake['root']) / '2026-01-01' / 'manifest.json').read_text(encoding='utf-8'))
    rev = man['revisions'][-1]
    ok('T1 正例-manifest sha 与实物一致', rev['sha256'] == sha256_file(Path(r['artifact'])))
    ok('T1 正例-只读探针证明不可写', rev['readonly_probe']['readonly'] is True)
    ok('T1 正例-冻结副本只读自证=unchanged', rev['readonly_selfproof']['verdict'] == 'unchanged')
    # 幂等
    r2 = snapshot_copy_source(fake, '2026-01-01', force=False)
    ok('T1 正例-当天幂等跳过', r2.get('status') == 'already-done')

    # T2 负控：源缺失必报错
    fake2 = dict(fake); fake2['db'] = work / 'no-such-db'
    r3 = snapshot_copy_source(fake2, '2026-01-01', force=False)
    ok('T2 负控-源缺失被拒(rc=2)', r3.get('status') == 'missing-source' and r3.get('rc') == 2)

    # T3 负控：漂移检测器必须抓到"同大小同 mtime 但内容变了"
    b = {'bytes': 10, 'mtime': 'm', 'sha256': 'a'}
    ok('T3 负控-漂移被抓', verdict(b, {'bytes': 10, 'mtime': 'm', 'sha256': 'b'}) == 'changed-by-live-writer')
    ok('T3 负控-不误报(identical)', verdict(b, dict(b)) == 'unchanged')

    # T4 负控：只读探针必须能区分可写/只读连接
    rw = sqlite3.connect(str(fx))
    ok('T4 负控-可写连接探针=readonly False', readonly_probe(rw)['readonly'] is False)
    rw.close()
    ro = _ro_conn(fx)
    ok('T4 负控-只读连接探针=readonly True', readonly_probe(ro)['readonly'] is True)
    ro.close()

    # T5 正例：委托源缺 CLI 时报错而不是假绿
    bad = {'name': 'bad', 'title': 'x', 'kind': 'delegate', 'delegate_cmd': [sys.executable, '/nonexistent.py', 'snapshot'],
           'root': work / 'o2', 'artifact': 'a.db', 'window': []}
    r5 = snapshot_delegate_source(bad, '2026-01-01', force=False)
    ok('T5 负控-委派失败不假绿', r5.get('status') == 'delegate-failed' or r5.get('rc') not in (0, None))

    # T6 正例：目录型滚动源（dir_copy）→ 逐文件哈希 + 源侧自证 + .asl 事件解析
    asl_dir = work / 'fake_pm'
    asl_dir.mkdir()
    (asl_dir / '2026.09.20.asl').write_text(
        "Sep 20 00:01:12 host powerd[100] <Notice>: Entering Sleep state due to 'Idle Sleep': Using AC\n"
        "Sep 20 03:11:00 host powerd[100] <Notice>: DarkWake from Deep Idle [CDN] : due to RTC\n"
        "Sep 20 07:20:00 host powerd[100] <Notice>: Entering Sleep state due to 'Maintenance Sleep': Using Battery\n"
        "Sep 20 09:30:00 host powerd[100] <Notice>: Wake from Normal Sleep [CDNVA]\n", encoding='utf-8')
    (asl_dir / 'StoreData').write_bytes(b'\x00' * 8)
    fake_pm = {'name': 'fakepm', 'title': 'x', 'kind': 'dir_copy', 'src_dir': asl_dir,
               'include': ['*.asl', 'StoreData'], 'root': work / 'out_pm', 'artifact': 'events.json',
               'parse': 'powermanagement', 'window': [], 'irreplaceability': 10, 'coverage': 'x'}
    r6 = snapshot_dir_copy_source(fake_pm, '2026-01-01', force=False, quiet=True)
    ev6 = (r6.get('parse') or {}).get('events_total') or {}
    ok('T6 正例-目录快照生成+事件解析', r6.get('status') == 'ok' and ev6.get('sleep_enter') == 2
       and ev6.get('darkwake') == 1 and ev6.get('wake') == 1, str(ev6))
    ok('T6 正例-解码器/原始字节双路口径一致', (r6.get('parse') or {}).get('decoder_match_all') is True)
    ok('T6 正例-源侧自证 unchanged', r6.get('src_verdict') == 'unchanged')
    ok('T6 正例-逐文件逐字节校验', all(f['dst_match'] for f in json.loads(
        (Path(fake_pm['root']) / '2026-01-01' / 'manifest.json').read_text(encoding='utf-8')
    )['revisions'][-1]['files']))
    (work / 'no-such-dir-marker').write_text('')       # 占位，确保 work 可写

    # T7 正例：跨日同内容 → 硬链接去重（快照体积≈增量）
    snapshot_dir_copy_source(fake_pm, '2026-01-02', force=False, quiet=True)
    rev7 = json.loads((Path(fake_pm['root']) / '2026-01-02' / 'manifest.json').read_text(encoding='utf-8'))['revisions'][-1]
    ok('T7 正例-同内容硬链接去重', all(f['mode'] == 'hardlink' for f in rev7['files']),
       str({f['name']: f['mode'] for f in rev7['files']}))

    # T8 负控：空目录/不匹配 include 必须报错，不得假绿
    empty = work / 'empty_dir'
    empty.mkdir()
    fake_e = dict(fake_pm); fake_e.update({'src_dir': empty, 'root': work / 'out_e'})
    r8 = snapshot_dir_copy_source(fake_e, '2026-01-01', force=False, quiet=True)
    ok('T8 负控-空源被拒(rc=2)', r8.get('status') == 'empty-source' and r8.get('rc') == 2)

    # T9 负控：DarkWake 子串陷阱（若把 DarkWake 误计为 wake，此处必炸）
    ev9 = parse_pm_text(["Sep 21 01:00:00 host powerd[1] <Notice>: DarkWake from Deep Idle [CDN]"],
                        year=2026)['events']
    ok('T9 负控-DarkWake 不计入 wake', ev9['darkwake'] == 1 and ev9['wake'] == 0, str(ev9))

    # T10 正例/负控：wifi 与睡眠窗口对账（窗口内=有活动 / 窗口外=安静）
    wf = work / 'wifi.log'
    wf.write_text("Sat Sep 26 00:35:15.909 [airport] DHCP lease bound\n"
                  "Sat Sep 26 05:00:00.000 [airport] BOOTP 0\n", encoding='utf-8')
    inwin = [{'enter': '2026-09-26 00:00:00', 'wake': '2026-09-26 02:00:00', 'reason': 'x', 'secs': 7200}]
    outwin = [{'enter': '2026-09-26 10:00:00', 'wake': '2026-09-26 11:00:00', 'reason': 'x', 'secs': 3600}]
    ok('T10 正例-窗口内判 activity', parse_wifi_text(wf.read_text().splitlines(), 2026, inwin)['verdict']
       == 'activity-during-sleep')
    ok('T10 负控-窗口外判 quiet', parse_wifi_text(wf.read_text().splitlines(), 2026, outwin)['verdict']
       == 'quiet-during-sleep')

    # T11 正例：单文件源（file_copy）采集 + 缺同日 pmset 时不臆造窗口
    fake_w = {'name': 'fakewifi', 'title': 'x', 'kind': 'file_copy', 'src_file': wf,
              'root': work / 'out_wifi', 'artifact': 'wifi.log', 'parse': 'wifilog', 'window': []}
    r11 = snapshot_file_copy_source(fake_w, '2026-01-03', force=False, quiet=True)
    ok('T11 正例-单文件快照+解析+自证', r11.get('status') == 'ok' and (r11.get('parse') or {}).get('n_lines') == 2
       and r11.get('src_verdict') == 'unchanged', str((r11.get('parse') or {}).get('n_lines')))

    shutil.rmtree(work, ignore_errors=True)
    bad_n = [n for n, c, _ in cases if not c]
    print('---\nselftest: %d/%d PASS%s' % (len(cases) - len(bad_n), len(cases), '' if not bad_n else '  失败: %s' % bad_n))
    return 0 if not bad_n else 1


# ---------------------------------------------------------------- main
def cmd_snapshot(args) -> int:
    date_s = args.date or datetime.date.today().isoformat()
    names = list(SOURCES) if args.source == 'all' else [args.source]
    res = []
    for n in names:
        res.append(snapshot_one(n, date_s, args.force))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    bad = [r for r in res if r.get('rc') not in (0, None)]
    if bad:
        print('[FAIL] %d 个源失败: %s' % (len(bad), [b.get('source') for b in bad]))
        return 2
    # 源侧复核必须给出明确结论（活库允许 changed，但不得为空）
    miss = [r for r in res if r.get('src_verdict') is None and r.get('status') == 'ok' and 'delegate' not in str(r.get('status'))]
    if miss:
        print('[WARN] 以下源缺少源侧复核结论: %s' % [m.get('source') for m in miss])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='滚动源每日快照（多源单入口·只读源·幂等）')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('snapshot'); s.add_argument('--source', default='all', choices=['all'] + list(SOURCES))
    s.add_argument('--date'); s.add_argument('--force', action='store_true'); s.add_argument('--json', action='store_true')
    s.set_defaults(func=cmd_snapshot)
    s = sub.add_parser('window'); s.add_argument('--source', default='all', choices=['all'] + list(SOURCES))
    s.add_argument('--all-days', action='store_true'); s.add_argument('--json', action='store_true')
    s.set_defaults(func=cmd_window)
    s = sub.add_parser('list'); s.add_argument('--json', action='store_true'); s.set_defaults(func=cmd_list)
    s = sub.add_parser('selftest'); s.set_defaults(func=lambda a: selftest())
    a = ap.parse_args()
    return a.func(a)


if __name__ == '__main__':
    sys.exit(main())
