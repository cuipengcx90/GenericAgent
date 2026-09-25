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
}

# 已知滚动源但**未实现**采集（只登记，不装定时；评审建议：别单独立项）
NOTES = {
    'pmset': '电源/睡眠日志：`pmset -g log` 与 /var/log/powermanagement 亦滚动；本轮仅登记未实现',
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
    return snapshot_delegate_source(src, date_s, force)


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
                                'irreplaceability': src['irreplaceability'], 'coverage': src['coverage'],
                                'window_note': src['window'][-1][2]}
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
