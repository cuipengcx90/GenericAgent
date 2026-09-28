#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间衰减源的「数据级留存覆盖」层（R495 / TODO L197）。

## 问题
`tools/rolling_source_snapshot.py verify`（R483）的判据是「**日期目录 × 必需文件**」——
它只回答「那天有没有采集」，不回答「那天**的数据**还在不在」。

时间衰减源有两种丢失方式：
  1. **漏采**：某天没跑采集 ⇒ 没有该天目录（verify 报缺口）。
  2. **数据过期**：源侧滚动删除（Chrome visits ~90d、kc /app/usage ~23-25d、
     pmset 逐日 .asl、wifilog 单日轮转）⇒ 补采不了。

关键实测（2026-09-29）：**漏采 ≠ 数据丢**。较晚的快照产物往往已经把缺失那天
的数据一起带走了（Chrome/kc 的库是「多天一份」，pmset 是「逐日一份 asl」）。
本模块把「日 D 的数据在不在」做成 **可复算的探针**，并落 append-only 账本，
再由 verify 以 `--coverage-ledger` 消费：覆盖日不再算 ALERT。

## 判据（rule v1，每源一条，逐条附证据）
  * chrome  ：日 D ∈ 某份更晚快照的 `History` 的 visits 日区间 [min,max]
               （库是滚动 90 天，区间内缺失 = 合法静默，非丢失）
  * kc      ：日 D ∈ 某份更晚快照 `knowledgeC.db` 的 `/app/usage` 流日区间
               （无该流时退化为 ZOBJECT 全流区间，证据 kind 标出）
  * pmset   ：日 D 的逐日 `.asl` 文件存在于某份更晚快照目录（**文件级**证据；
               该日零睡眠窗属合法静默，不作为判据）
  * wifilog ：某份更晚快照的 `wifi.log` 里存在该日的行（行级证据；文件单日轮转
               ⇒ 旧快照里没有就是真丢）
  严格性：证据所在快照日期必须 **> 日 D**（同一天采的只算 PARTIAL，不满一天）。

## 用法
  python3 tools/rolling_source_coverage.py probe  --source kc --day 2026-09-27
  python3 tools/rolling_source_coverage.py claim  --apply          # 落账本(dry-run 默认)
  python3 tools/rolling_source_coverage.py audit                   # 复核账本是否仍成立
  python3 tools/rolling_source_coverage.py window                  # 每源内容级留存窗口
  python3 tools/rolling_source_coverage.py salvage --apply         # 一次性抢救采数(默认 dry-run)
  python3 tools/rolling_source_coverage.py selftest
  python3 tools/rolling_source_coverage.py proof                   # 只读自证

只读边界：probe/audit/window/proof/selftest 零写入（proof 会自证快照树逐字未变）；
claim 仅追加账本；salvage 仅写 `<root>/<day>/salvaged.json`。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rolling_source_snapshot as rss  # noqa: E402  复用源登记面/审计内核，零重写

RULE_VERSION = 'coverage-rule/v1'
SCHEMA = 'ga.rolling_source_coverage/v1'
LEDGER_DEFAULT = Path(rss.GA) / 'temp' / 'source_retention' / 'coverage.jsonl'
DATE_RE = rss.DATE_RE
SOURCES = rss.SOURCES


# ---------------------------------------------------------------- 基础
def _as_date(s):
    return rss._as_date(s)


def _sha_head(p: Path, n: int = 16) -> str:
    try:
        return rss.sha256_file(p)[:n]
    except Exception as e:  # noqa: BLE001
        return 'ERR:%s' % str(e)[:40]


def snap_dirs(root) -> list:
    """快照树里所有日期目录，按日期升序。"""
    r = Path(root)
    out = []
    if not r.exists():
        return out
    for d in sorted(r.iterdir()):
        if d.is_dir() and DATE_RE.match(d.name):
            out.append((d.name, d))
    return out


def _ro(db: Path):
    con = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=8.0)
    con.text_factory = lambda b: b.decode('utf-8', 'replace')
    return con


def _day_bounds(db: Path, expr: str, table: str, where: str = '') -> dict:
    """给定本地日表达式，返回 {min_day,max_day,n}；失败 fail-closed。"""
    if not db.exists():
        return {'ok': False, 'error': 'missing-artifact'}
    try:
        con = _ro(db)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)[:120]}
    try:
        sql = ("select min(%s), max(%s), count(*) from %s %s"
               % (expr, expr, table, where))
        row = con.execute(sql).fetchone()
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)[:120]}
    finally:
        con.close()
    lo, hi, n = (row + (None,))[:3] if row else (None, None, 0)
    if not lo or not hi:
        return {'ok': False, 'error': 'empty'}
    return {'ok': True, 'min_day': str(lo)[:10], 'max_day': str(hi)[:10], 'n': int(n or 0)}


def _rows_on_day(db: Path, expr: str, table: str, day: str, where: str = '') -> int:
    try:
        con = _ro(db)
    except Exception:  # noqa: BLE001
        return -1
    try:
        w = (' ' + where) if where else ''
        joiner = ' and' if where else ' where'
        sql = "select count(*) from %s%s%s (%s)=?" % (table, w, joiner, expr)
        return int(con.execute(sql, (day,)).fetchone()[0])
    except Exception:  # noqa: BLE001
        return -1
    finally:
        con.close()


# ---------------------------------------------------------------- 逐源探针
def _probe_chrome(day: str, root, today: str) -> dict:
    for dname, d in snap_dirs(root):
        if not (day < dname <= today):
            continue
        hist = d / 'History'
        if not hist.exists():
            continue
        b = _day_bounds(hist, "date(visit_time/1000000-11644473600,'unixepoch','localtime')",
                        'visits')
        if not b.get('ok'):
            continue
        if b['min_day'] <= day <= b['max_day']:
            return _mk(day, dname, 'chrome.visits_range', hist,
                       {'min_day': b['min_day'], 'max_day': b['max_day'], 'n': b['n'],
                        'rows_on_day': _rows_on_day(
                            hist, "date(visit_time/1000000-11644473600,'unixepoch','localtime')",
                            'visits', day)})
    return _none(day, 'chrome')


def _probe_kc(day: str, root, today: str) -> dict:
    for dname, d in snap_dirs(root):
        if not (day < dname <= today):
            continue
        db = d / 'knowledgeC.db'
        if not db.exists():
            continue
        # 主判据：/app/usage 流
        expr = "date(ZSTARTDATE+978307200,'unixepoch','localtime')"
        where = "where ZSTREAMNAME='/app/usage'"
        b = _day_bounds(db, expr, 'ZOBJECT', where)
        kind = 'kc.app_usage_range'
        if not b.get('ok'):
            b = _day_bounds(db, expr, 'ZOBJECT')          # 退化：全流
            kind = 'kc.all_streams_range'
            where = ''
        if not b.get('ok'):
            continue
        if b['min_day'] <= day <= b['max_day']:
            return _mk(day, dname, kind, db,
                       {'min_day': b['min_day'], 'max_day': b['max_day'], 'n': b['n'],
                        'rows_on_day': _rows_on_day(db, expr, 'ZOBJECT', day, where)})
    return _none(day, 'kc')


def _probe_pmset(day: str, root, today: str) -> dict:
    fname = '%s.asl' % day.replace('-', '.')
    for dname, d in snap_dirs(root):
        if not (day < dname <= today):
            continue
        f = d / fname
        if f.exists():
            return _mk(day, dname, 'pmset.asl_file', f,
                       {'file': fname, 'bytes': f.stat().st_size,
                        'snapshot_date': dname, 'windows_on_day': _pm_windows(d, day)})
    return _none(day, 'pmset')


def _pm_windows(d: Path, day: str) -> int:
    """附带信息（不作判据）：该快照 events.json 里属于 day 的睡眠窗数。-1=不可得。"""
    try:
        j = json.loads((d / 'events.json').read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return -1
    n = 0
    try:
        for w in j.get('windows') or []:
            if str(w.get('enter', ''))[:10] == day:
                n += 1
    except Exception:  # noqa: BLE001
        return -1
    return n


def _day_tokens(day: str) -> list:
    dt = _as_date(day)
    if not dt:
        return [day]
    toks = [dt.strftime('%b %d'), dt.strftime('%b %e'), dt.strftime('%b  %d'), day]
    out = []
    for t in toks:
        if t not in out:
            out.append(t)
    return out


def _probe_wifilog(day: str, root, today: str) -> dict:
    toks = _day_tokens(day)
    for dname, d in snap_dirs(root):
        if not (day < dname <= today):
            continue
        f = d / 'wifi.log'
        if not f.exists():
            continue
        try:
            txt = f.read_text(encoding='utf-8', errors='replace')
        except Exception:  # noqa: BLE001
            continue
        lines = txt.split('\n')
        hit, first = 0, None
        for ln in lines:
            if any(t in ln for t in toks):
                hit += 1
                if first is None:
                    first = ln[:120]
        if hit:
            return _mk(day, dname, 'wifilog.day_lines', f,
                       {'tokens': toks, 'n_lines': hit, 'total_lines': len(lines),
                        'first_line': first})
    return _none(day, 'wifilog')


def _mk(day: str, snap_date: str, kind: str, artifact: Path, extra: dict) -> dict:
    return {'source': None, 'day': day, 'status': 'COVERED', 'rule': RULE_VERSION,
            'evidence': dict({'kind': kind, 'snapshot_date': snap_date,
                              'artifact': str(artifact),
                              'artifact_sha256_16': _sha_head(artifact)}, **extra)}


def _none(day: str, source: str) -> dict:
    return {'source': source, 'day': day, 'status': 'UNCOVERED', 'rule': RULE_VERSION,
            'evidence': {'kind': 'no_evidence', 'snapshot_date': None}}


PROBES = {'chrome': _probe_chrome, 'kc': _probe_kc,
          'pmset': _probe_pmset, 'wifilog': _probe_wifilog}


def probe_day(source: str, day: str, root=None, today: str = None) -> dict:
    """单源单日「数据是否还在」探针。fail-closed：拿不到证据一律 UNCOVERED。"""
    if source not in PROBES:
        return {'source': source, 'day': day, 'status': 'UNCOVERED', 'rule': RULE_VERSION,
                'evidence': {'kind': 'unregistered-source', 'snapshot_date': None}}
    t = today or datetime.date.today().isoformat()
    r = root if root is not None else SOURCES[source]['root']
    out = PROBES[source](day[:10], r, t[:10])
    out['source'] = source
    return out


# ---------------------------------------------------------------- 内容级留存窗口
def content_window(source: str, root=None, today: str = None) -> dict:
    """每源的「数据实测留存窗口」：最新一份产物里数据覆盖的日区间。"""
    t = (today or datetime.date.today().isoformat())[:10]
    r = Path(root if root is not None else SOURCES[source]['root'])
    dirs = snap_dirs(r)
    out = {'source': source, 'root': str(r), 'today': t, 'n_snapshots': len(dirs),
           'latest_snapshot': dirs[-1][0] if dirs else None,
           'window_min': None, 'window_max': None, 'window_days': None,
           'days_until_oldest_loss': None, 'method': None, 'evidence': None}
    if not dirs:
        return out
    dname, d = dirs[-1]
    if source == 'chrome':
        hist = d / 'History'
        b = _day_bounds(hist, "date(visit_time/1000000-11644473600,'unixepoch','localtime')",
                        'visits') if hist.exists() else {'ok': False}
        out['method'] = 'chrome.visits_range'
        out['evidence'] = {k: b.get(k) for k in ('min_day', 'max_day', 'n')} if b.get('ok') else None
        if b.get('ok'):
            out.update(window_min=b['min_day'], window_max=b['max_day'])
    elif source == 'kc':
        db = d / 'knowledgeC.db'
        b = _day_bounds(db, "date(ZSTARTDATE+978307200,'unixepoch','localtime')", 'ZOBJECT',
                        "where ZSTREAMNAME='/app/usage'") if db.exists() else {'ok': False}
        out['method'] = 'kc.app_usage_range'
        out['evidence'] = {k: b.get(k) for k in ('min_day', 'max_day', 'n')} if b.get('ok') else None
        if b.get('ok'):
            out.update(window_min=b['min_day'], window_max=b['max_day'])
    elif source == 'pmset':
        days = []
        for p in sorted(d.iterdir()):
            m = re.match(r'(\d{4})\.(\d{2})\.(\d{2})', p.name)
            if m:
                days.append('%s-%s-%s' % m.groups())
        out['method'] = 'pmset.asl_filenames'
        out['evidence'] = {'n_dated_files': len(days)}
        if days:
            out.update(window_min=min(days), window_max=max(days))
    elif source == 'wifilog':
        f = d / 'wifi.log'
        days = set()
        if f.exists():
            txt = f.read_text(encoding='utf-8', errors='replace')
            for ln in txt.split('\n')[:1] + txt.split('\n'):
                m = re.search(r'\b([A-Z][a-z]{2})\s+(\d{1,2})\b', ln)
                if m:
                    try:
                        dt = datetime.datetime.strptime(
                            '%s %s %s' % (m.group(1), m.group(2), dname[:4]), '%b %d %Y')
                        days.add(dt.date().isoformat())
                    except Exception:  # noqa: BLE001
                        pass
        out['method'] = 'wifilog.day_tokens'
        out['evidence'] = {'n_days_seen': len(days)}
        if days:
            out.update(window_min=min(days), window_max=max(days))
    lo, hi = _as_date(out['window_min']), _as_date(out['window_max'])
    if lo and hi:
        out['window_days'] = (hi - lo).days + 1
    if lo:
        # 距「窗口起点滚出」还有几天：起点每过一天就永远少一天
        out['days_until_oldest_loss'] = max(0, (lo - _as_date(t)).days + 1)
    return out


# ---------------------------------------------------------------- 账本
def _ledger_id(rec: dict) -> str:
    key = '%s|%s|%s|%s' % (rec.get('source'), rec.get('day'), rec.get('status'),
                           json.dumps(rec.get('evidence') or {}, sort_keys=True,
                                      ensure_ascii=False))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]


def read_ledger(path=None) -> list:
    p = Path(path or LEDGER_DEFAULT)
    out = []
    if not p.exists():
        return out
    for i, ln in enumerate(p.read_text(encoding='utf-8').split('\n'), 1):
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception as e:  # noqa: BLE001
            out.append({'_line': i, '_bad': str(e)[:80]})
            continue
        rec['_line'] = i
        out.append(rec)
    return out


def latest_claims(path=None) -> dict:
    """(source,day) -> 最后一条记录。"""
    out = {}
    for rec in read_ledger(path):
        if rec.get('_bad'):
            continue
        out[(rec.get('source'), rec.get('day'))] = rec
    return out


def ledger_append(recs, path=None) -> dict:
    """append-only：同一 (source,day,status,evidence) 已存在则跳过（幂等）。"""
    p = Path(path or LEDGER_DEFAULT)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    for rec in read_ledger(p):
        if not rec.get('_bad'):
            existing[rec.get('ledger_id')] = True
    new = []
    for r in recs:
        lid = _ledger_id(r)
        if lid in existing:
            continue
        r = dict(r)
        r['ledger_id'] = lid
        r['ts'] = datetime.datetime.now().isoformat(timespec='seconds')
        r['schema'] = SCHEMA
        new.append(r)
        existing[lid] = True
    if new:
        with open(p, 'a', encoding='utf-8') as fh:
            for r in new:
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    return {'path': str(p), 'appended': len(new), 'skipped': len(recs) - len(new)}


# ---------------------------------------------------------------- 子命令
def build_claims(source='all', today=None, roots=None, live=None) -> dict:
    """对每个源的 missing 日（recoverable + lost）逐日跑数据级探针。"""
    names = list(SOURCES) if source == 'all' else [source]
    today = (today or datetime.date.today().isoformat())[:10]
    out = {'today': today, 'sources': {}}
    for n in names:
        au = rss.audit_source(n, root=(roots or {}).get(n), today=today,
                              live=(live or {}).get(n) if live else None)
        days = list(au['missing_lost']) + list(au['missing_recoverable'])
        rows = []
        for day in days:
            pr = probe_day(n, day, root=(roots or {}).get(n), today=today)
            pr['audit_class'] = 'LOST' if day in au['missing_lost'] else 'RECOVERABLE'
            rows.append(pr)
        out['sources'][n] = {'verdict': au['verdict'], 'missing_days': au['missing_days'],
                             'pending_today': au['pending_today'],
                             'days_left': au['days_left'], 'probes': rows}
    return out


def cmd_claim(args) -> int:
    res = build_claims(source=args.source, today=getattr(args, 'today', None))
    recs = []
    for n, blk in res['sources'].items():
        for pr in blk['probes']:
            if pr['status'] in ('COVERED', 'PARTIAL'):
                recs.append({'source': n, 'day': pr['day'], 'status': pr['status'],
                             'rule': pr['rule'], 'evidence': pr['evidence'],
                             'audit_class': pr.get('audit_class')})
    print('■ 留存覆盖认领（今天=%s，账本=%s）' % (res['today'], args.ledger))
    for n, blk in res['sources'].items():
        if not blk['probes']:
            print('─ %-8s %s  缺口=0' % (n, blk['verdict']))
            continue
        print('─ %-8s %s  缺口 %d 天' % (n, blk['verdict'], len(blk['probes'])))
        for pr in blk['probes']:
            ev = pr['evidence']
            print('   %s %s  %-14s %s' % (
                pr['day'], pr['status'], ev.get('kind'),
                ('经 %s 快照' % ev.get('snapshot_date')) if ev.get('snapshot_date') else ''))
    if getattr(args, 'apply', False):
        r = ledger_append(recs, path=args.ledger)
        print('─ 落账本: 新增 %d 条 / 幂等跳过 %d 条  → %s'
              % (r['appended'], r['skipped'], r['path']))
    else:
        print('─ 候选 %d 条（dry-run：加 --apply 落账本）' % len(recs))
    return 0


def cmd_audit(args) -> int:
    claims = latest_claims(args.ledger)
    if not claims:
        print('账本为空或不存在: %s' % args.ledger)
        return 2
    today = (getattr(args, 'today', None) or datetime.date.today().isoformat())[:10]
    ok, bad = [], []
    for (src, day), rec in sorted(claims.items()):
        pr = probe_day(src, day, today=today)
        same_art = (pr['evidence'].get('snapshot_date') == rec['evidence'].get('snapshot_date'))
        ent = {'source': src, 'day': day, 'claimed': rec.get('status'),
               'now': pr['status'], 'artifact_same': same_art,
               'claimed_artifact': rec['evidence'].get('artifact'),
               'now_artifact': pr['evidence'].get('artifact')}
        if pr['status'] == rec.get('status') == 'COVERED' and same_art:
            ok.append(ent)
        else:
            bad.append(ent)
    print('■ 账本复核（%d 条声明；今天=%s）' % (len(claims), today))
    for e in bad:
        print('   ✗ %s %s  声明=%s 现状=%s 同产物=%s' % (
            e['source'], e['day'], e['claimed'], e['now'], e['artifact_same']))
    print('─ 摘要: 仍成立 %d / 失效 %d  rc=%d' % (len(ok), len(bad), 1 if bad else 0))
    return 1 if bad else 0


def cmd_window(args) -> int:
    today = (getattr(args, 'today', None) or datetime.date.today().isoformat())[:10]
    rows = [content_window(n, today=today) for n in (list(SOURCES) if args.source == 'all'
                                                     else [args.source])]
    if args.json:
        print(json.dumps({'schema': SCHEMA, 'today': today, 'windows': rows},
                         ensure_ascii=False, indent=2))
        return 0
    print('■ 内容级留存窗口（数据实测，不依赖目录存在；今天=%s）' % today)
    for r in rows:
        print('─ %-8s 快照 %d 份 最新=%s  数据窗 %s → %s (%s 天)  距窗口起点滚出 %s 天  [%s]'
              % (r['source'], r['n_snapshots'], r['latest_snapshot'],
                 r['window_min'], r['window_max'], r['window_days'],
                 r['days_until_oldest_loss'], r['method']))
    return 0


SALVAGE_SQL = {
    'chrome': ("select v.id, v.visit_time, u.url, u.title from visits v "
               "left join urls u on u.id = v.url "
               "where date(v.visit_time/1000000-11644473600,'unixepoch','localtime') = ?",
               'History'),
    'kc': ("select ZSTREAMNAME, ZSTARTDATE, ZENDDATE, ZVALUESTRING from ZOBJECT "
           "where date(ZSTARTDATE+978307200,'unixepoch','localtime') = ?", 'knowledgeC.db'),
}


def salvage_one(source: str, day: str, root=None, today=None, apply: bool = False) -> dict:
    """一次性抢救采数：把**源里仍能读到的**该日行抽成 `<root>/<day>/salvaged.json`。

    边界（写进产物）：这是**派生抽取**，不是源库全量拷贝；不含该源未登记的列/流；
    源侧已滚出窗口时必然零行（那就承认丢失，绝不造假行）。
    """
    t = (today or datetime.date.today().isoformat())[:10]
    r = Path(root if root is not None else SOURCES[source]['root'])
    dest_dir = r / day
    dest = dest_dir / 'salvaged.json'
    rec = {'schema': 'ga.rolling_source_salvage/v1', 'source': source, 'day': day,
           'ts': datetime.datetime.now().isoformat(timespec='seconds'),
           'kind': 'derived-extract', 'note': '派生抽取：非源库全量拷贝，不含未登记列/流',
           'rows': 0, 'dest': str(dest)}
    if source in SALVAGE_SQL:
        sql, art_name = SALVAGE_SQL[source]
        db = Path(SOURCES[source]['db'])
        if not db.exists():
            return dict(rec, status='NO_SOURCE', error='missing-source')
        try:
            con = _ro(db)
            cur = con.execute(sql, (day,))
            rows = [list(x) for x in cur.fetchall()]
            cols = [d[0] for d in cur.description]
            con.close()
        except Exception as e:  # noqa: BLE001
            return dict(rec, status='ERROR', error=str(e)[:160])
        if not rows:
            return dict(rec, status='EMPTY', note=rec['note'] + '；源侧该日零行=真丢失')
        payload = {'schema': rec['schema'], 'source': source, 'day': day, 'ts': rec['ts'],
                   'kind': rec['kind'], 'note': rec['note'], 'source_artifact': art_name,
                   'columns': cols, 'rows': rows}
        rec['rows'] = len(rows)
        if apply:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            rec['bytes'] = dest.stat().st_size
            rec['status'] = 'SALVAGED'
        else:
            rec['status'] = 'PLAN'
        return rec
    if source == 'pmset':
        fname = '%s.asl' % day.replace('-', '.')
        srcf = Path(SOURCES[source]['src_dir']) / fname
        rec.update({'src_file': str(srcf), 'rows': None})
        if not srcf.exists():
            return dict(rec, status='EMPTY', note=rec['note'] + '；源侧无该日 asl=真丢失')
        if apply:
            import shutil
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcf, dest_dir / fname)
            rec['bytes'] = (dest_dir / fname).stat().st_size
            rec['status'] = 'SALVAGED'
        else:
            rec['status'] = 'PLAN'
        return rec
    return dict(rec, status='UNSUPPORTED',
                error='wifilog 单日轮转，源侧无历史可抽（只能靠每日采）')


def cmd_salvage(args) -> int:
    claims = build_claims(source=args.source, today=getattr(args, 'today', None))
    plans = []
    for n, blk in claims['sources'].items():
        for pr in blk['probes']:
            if pr['status'] == 'UNCOVERED':
                plans.append(salvage_one(n, pr['day'], today=claims['today'],
                                         apply=getattr(args, 'apply', False)))
    print('■ 一次性抢救采数（%s；今天=%s）'
          % ('apply' if getattr(args, 'apply', False) else 'dry-run', claims['today']))
    if not plans:
        print('─ 无「无覆盖」缺口日：无需抢救（覆盖日的数据已在更晚快照里）')
        return 0
    for p in plans:
        print('─ %-8s %s  %-10s rows=%s %s' % (p['source'], p['day'], p['status'],
                                               p.get('rows'), p.get('note', '')))
    return 0


def cmd_probe(args) -> int:
    r = probe_day(args.source, args.day, today=getattr(args, 'today', None))
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else
          '%s %s -> %s  %s' % (args.source, args.day, r['status'], r['evidence']))
    return 0 if r['status'] in ('COVERED', 'PARTIAL') else 1


def cmd_proof(args) -> int:
    """只读自证：probe/audit/window 前后快照树逐字未变。"""
    names = list(SOURCES)
    before = {n: rss._tree_index(SOURCES[n]['root']) for n in names}
    today = (getattr(args, 'today', None) or datetime.date.today().isoformat())[:10]
    for n in names:
        content_window(n, today=today)
        for rec in read_ledger(args.ledger):
            if rec.get('_bad'):
                continue
            probe_day(rec.get('source'), rec.get('day'), today=today)
    after = {n: rss._tree_index(SOURCES[n]['root']) for n in names}
    same = before == after
    nfiles = sum(len(v) for v in after.values())
    print('■ 只读自证: %s（快照树文件 %d，源 %d）' % ('unchanged' if same else 'CHANGED',
                                                    nfiles, len(names)))
    return 0 if same else 1


# ---------------------------------------------------------------- selftest
def _mk_chrome_db(p: Path, days: list) -> None:
    con = sqlite3.connect(str(p))
    con.execute('create table visits(id integer primary key, url integer, visit_time integer)')
    con.execute('create table urls(id integer primary key, url text, title text)')
    for i, day in enumerate(days):
        dt = datetime.datetime.strptime(day, '%Y-%m-%d').replace(hour=12)
        us = int((dt.timestamp() + 11644473600) * 1000000)
        con.execute('insert into visits(id,url,visit_time) values(?,?,?)', (i + 1, 1, us))
    con.commit()
    con.close()


def _mk_kc_db(p: Path, days: list, stream: str = '/app/usage') -> None:
    con = sqlite3.connect(str(p))
    con.execute('create table ZOBJECT(Z_PK integer primary key, ZSTREAMNAME text,'
                ' ZSTARTDATE real, ZENDDATE real, ZVALUESTRING text)')
    for i, day in enumerate(days):
        dt = datetime.datetime.strptime(day, '%Y-%m-%d').replace(hour=12)
        con.execute('insert into ZOBJECT values(?,?,?,?,?)',
                    (i + 1, stream, dt.timestamp() - 978307200, dt.timestamp() - 978307200 + 60, 'x'))
    con.commit()
    con.close()


def _fixture(base: Path, days_snap: list, chrome_db_days: list = None,
             kc_days: list = None, pmset_files: list = None,
             wifi_days: list = None) -> Path:
    root = base
    for d in days_snap:
        dd = root / d
        dd.mkdir(parents=True, exist_ok=True)
        if chrome_db_days is not None:
            _mk_chrome_db(dd / 'History', chrome_db_days)
        if kc_days is not None:
            _mk_kc_db(dd / 'knowledgeC.db', kc_days)
        if pmset_files is not None:
            (dd / 'events.json').write_text(json.dumps({'windows': []}), encoding='utf-8')
            for f in pmset_files:
                (dd / f).write_text('asl', encoding='utf-8')
        if wifi_days is not None:
            (dd / 'wifi.log').write_text('\n'.join(
                '%s x' % t for t in wifi_days) + '\n', encoding='utf-8')
    return root


def selftest() -> int:
    import shutil
    import tempfile
    n_pass = n_fail = 0
    fails = []

    def expect(name, cond, detail=''):
        nonlocal n_pass, n_fail
        if cond:
            n_pass += 1
            print('  ✓ %s' % name)
        else:
            n_fail += 1
            fails.append(name)
            print('  ✗ %s  %s' % (name, detail))

    base = Path(tempfile.mkdtemp(prefix='rsc_selftest_'))
    try:
        print('■ chrome：更晚快照把缺失日带走 ⇒ COVERED')
        r = _fixture(base / 'c1', ['2026-09-25', '2026-09-29'],
                     chrome_db_days=['2026-09-20', '2026-09-26', '2026-09-28'])
        p = probe_day('chrome', '2026-09-26', root=r, today='2026-09-29')
        expect('chrome 区间覆盖 ⇒ COVERED 且证据指向 09-29 快照',
               p['status'] == 'COVERED' and p['evidence']['snapshot_date'] == '2026-09-29',
               str(p))
        expect('chrome 证据含区间与当日行数',
               p['evidence']['min_day'] == '2026-09-20' and p['evidence']['rows_on_day'] == 1, str(p))

        print('■ 负控 NC1：日不在任何更晚快照的区间内 ⇒ UNCOVERED')
        r = _fixture(base / 'c2', ['2026-09-25', '2026-09-29'],
                     chrome_db_days=['2026-09-26', '2026-09-29'])
        p = probe_day('chrome', '2026-09-20', root=r, today='2026-09-29')
        expect('区间外 ⇒ UNCOVERED', p['status'] == 'UNCOVERED', str(p))

        print('■ 负控 NC2：只有同日快照（不满一天）⇒ 不算覆盖')
        r = _fixture(base / 'c3', ['2026-09-26'], chrome_db_days=['2026-09-26'])
        p = probe_day('chrome', '2026-09-26', root=r, today='2026-09-26')
        expect('同日快照 ⇒ UNCOVERED（严格 >day）', p['status'] == 'UNCOVERED', str(p))

        print('■ 负控 NC3：快照目录存在但产物缺失 ⇒ fail-closed')
        r = _fixture(base / 'c4', ['2026-09-25'])
        (r / '2026-09-29').mkdir(parents=True, exist_ok=True)
        p = probe_day('chrome', '2026-09-26', root=r, today='2026-09-29')
        expect('缺 History ⇒ UNCOVERED', p['status'] == 'UNCOVERED', str(p))

        print('■ kc：/app/usage 区间覆盖')
        r = _fixture(base / 'k1', ['2026-09-29'], kc_days=['2026-09-02', '2026-09-27'])
        p = probe_day('kc', '2026-09-27', root=r, today='2026-09-29')
        expect('kc ⇒ COVERED 且 kind=kc.app_usage_range',
               p['status'] == 'COVERED' and p['evidence']['kind'] == 'kc.app_usage_range', str(p))
        print('■ 负控 NC4：流名不符（无 /app/usage）⇒ 退化判据 + kind 标注')
        r = _fixture(base / 'k2', ['2026-09-29'], kc_days=['2026-09-27', '2026-09-29'])
        (r / '2026-09-29' / 'knowledgeC.db').unlink()
        _mk_kc_db(r / '2026-09-29' / 'knowledgeC.db', ['2026-09-27', '2026-09-29'],
                  stream='/display/isBacklit')
        p = probe_day('kc', '2026-09-27', root=r, today='2026-09-29')
        expect('退化到全流 ⇒ COVERED 且 kind=kc.all_streams_range',
               p['status'] == 'COVERED' and p['evidence']['kind'] == 'kc.all_streams_range', str(p))

        print('■ pmset：逐日 asl 文件存在 ⇒ COVERED（文件级证据）')
        r = _fixture(base / 'p1', ['2026-09-26', '2026-09-29'],
                     pmset_files=['2026.09.27.asl', '2026.09.29.asl'])
        p = probe_day('pmset', '2026-09-27', root=r, today='2026-09-29')
        expect('asl 在 09-29 快照里 ⇒ COVERED',
               p['status'] == 'COVERED' and p['evidence']['file'] == '2026.09.27.asl', str(p))
        print('■ 负控 NC5：该日 asl 只在同/更早快照 ⇒ UNCOVERED')
        r = _fixture(base / 'p2', ['2026-09-26'], pmset_files=['2026.09.26.asl'])
        p = probe_day('pmset', '2026-09-26', root=r, today='2026-09-26')
        expect('同日 asl ⇒ UNCOVERED', p['status'] == 'UNCOVERED', str(p))

        print('■ wifilog：行级证据（真丢失必须判 UNCOVERED = 核心负控）')
        r = _fixture(base / 'w1', ['2026-09-26', '2026-09-29'],
                     wifi_days=['Sep 29', 'Sep 28'])
        p = probe_day('wifilog', '2026-09-27', root=r, today='2026-09-29')
        expect('无 09-27 行 ⇒ UNCOVERED（真丢失）', p['status'] == 'UNCOVERED', str(p))
        p = probe_day('wifilog', '2026-09-28', root=r, today='2026-09-29')
        expect('有 09-28 行 ⇒ COVERED 且 n_lines≥1',
               p['status'] == 'COVERED' and p['evidence']['n_lines'] >= 1, str(p))
        print('■ 负控 NC6：伪造「单日轮转源已覆盖」必被拒（快照里确无该日行）')
        r = _fixture(base / 'w2', ['2026-09-26', '2026-09-29'], wifi_days=['Sep 29'])
        p = probe_day('wifilog', '2026-09-25', root=r, today='2026-09-29')
        expect('09-25 在快照里无行 ⇒ UNCOVERED', p['status'] == 'UNCOVERED', str(p))

        print('■ 未登记源 ⇒ fail-closed')
        p = probe_day('nope', '2026-09-27', root=base, today='2026-09-29')
        expect('未登记源 ⇒ UNCOVERED 且 kind=unregistered-source',
               p['status'] == 'UNCOVERED' and p['evidence']['kind'] == 'unregistered-source',
               str(p))

        print('■ 账本：幂等追加 + audit 复核（含篡改负控）')
        led = base / 'ledger.jsonl'
        recs = [{'source': 'chrome', 'day': '2026-09-26', 'status': 'COVERED',
                 'rule': RULE_VERSION,
                 'evidence': {'kind': 'chrome.visits_range', 'snapshot_date': '2026-09-29',
                              'artifact': 'x'}}]
        a = ledger_append(recs, path=led)
        b = ledger_append(recs, path=led)
        expect('重复追加幂等（appended 1 → 0）', a['appended'] == 1 and b['appended'] == 0,
               '%s / %s' % (a, b))
        r = _fixture(base / 'c5', ['2026-09-29'],
                     chrome_db_days=['2026-09-26', '2026-09-29'])
        p = probe_day('chrome', '2026-09-26', root=r, today='2026-09-29')
        expect('审计用探针在夹具上复现 COVERED', p['status'] == 'COVERED', str(p))
        print('■ 负控 NC7：账本声明「已覆盖」但产物被删 ⇒ audit 必报失效')
        r2 = _fixture(base / 'c6', ['2026-09-29'], chrome_db_days=['2026-09-26'])
        recs2 = [{'source': 'chrome', 'day': '2026-09-26', 'status': 'COVERED',
                  'rule': RULE_VERSION,
                  'evidence': {'kind': 'chrome.visits_range', 'snapshot_date': '2026-09-29',
                               'artifact': str(r2 / '2026-09-29' / 'History')}}]
        led2 = base / 'ledger2.jsonl'
        ledger_append(recs2, path=led2)
        p_ok = probe_day('chrome', '2026-09-26', root=r2, today='2026-09-29')
        (r2 / '2026-09-29' / 'History').unlink()
        p_bad = probe_day('chrome', '2026-09-26', root=r2, today='2026-09-29')
        expect('删产物后探针转 UNCOVERED（声明不再成立）',
               p_ok['status'] == 'COVERED' and p_bad['status'] == 'UNCOVERED',
               '%s / %s' % (p_ok['status'], p_bad['status']))

        print('■ salvage：源侧零行 ⇒ EMPTY（绝不造假行）')
        td = Path(tempfile.mkdtemp(prefix='rsc_salvage_'))
        try:
            db = td / 'History'
            _mk_chrome_db(db, ['2026-09-26'])
            old = dict(SOURCES['chrome'])
            SOURCES['chrome'] = dict(old, db=str(db))
            rec = salvage_one('chrome', '2026-09-20', root=td / 'root', apply=False)
            expect('源侧无该日行 ⇒ EMPTY 且不写盘',
                   rec['status'] == 'EMPTY' and not (td / 'root' / '2026-09-20').exists(),
                   str(rec))
            rec2 = salvage_one('chrome', '2026-09-26', root=td / 'root', apply=True)
            expect('源侧有行 ⇒ SALVAGED 且产物含该日行数',
                   rec2['status'] == 'SALVAGED' and rec2['rows'] == 1
                   and (td / 'root' / '2026-09-26' / 'salvaged.json').exists(), str(rec2))
            expect('salvage 产物不冒充必需文件 History（verify 不会被骗绿）',
                   not (td / 'root' / '2026-09-26' / 'History').exists())
            SOURCES['chrome'] = old
        finally:
            shutil.rmtree(td, ignore_errors=True)

        print('■ content_window 与 ledger 读取健壮性')
        r = _fixture(base / 'w3', ['2026-09-29'], chrome_db_days=['2026-09-01', '2026-09-29'])
        w = content_window('chrome', root=r, today='2026-09-29')
        expect('chrome 窗口 = 09-01..09-29 / 29 天',
               w['window_min'] == '2026-09-01' and w['window_max'] == '2026-09-29'
               and w['window_days'] == 29, str(w))
        expect('距窗口起点滚出天数 ≥ 0（起点已过 ⇒ 0）',
               w['days_until_oldest_loss'] == 0, str(w))
        bad_led = base / 'bad.jsonl'
        bad_led.write_text('{"source":"chrome","day":"2026-09-26"}\nnot-json\n', encoding='utf-8')
        rows = read_ledger(bad_led)
        expect('坏行不丢：读到 2 行且坏行标 _bad', len(rows) == 2 and any(r.get('_bad') for r in rows),
               str(rows))
        print('■ 负控 NC8：空账本目录/不存在的账本 ⇒ 不崩、报空')
        expect('不存在账本 ⇒ []', read_ledger(base / 'nope.jsonl') == [])
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print('\n== selftest: %d passed / %d failed ==' % (n_pass, n_fail))
    if fails:
        print('FAILED: %s' % fails)
    return 1 if n_fail else 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='时间衰减源的「数据级留存覆盖」层（R495）')
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('probe', help='单源单日数据级探针')
    p.add_argument('--source', required=True, choices=list(SOURCES))
    p.add_argument('--day', required=True)
    p.add_argument('--today', default=None)
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser('claim', help='把「缺口日仍被更晚快照覆盖」落账本（默认 dry-run）')
    p.add_argument('--source', default='all', choices=['all'] + list(SOURCES))
    p.add_argument('--today', default=None)
    p.add_argument('--ledger', default=str(LEDGER_DEFAULT))
    p.add_argument('--apply', action='store_true')
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser('audit', help='复核账本声明是否仍成立（只读）')
    p.add_argument('--ledger', default=str(LEDGER_DEFAULT))
    p.add_argument('--today', default=None)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser('window', help='内容级留存窗口（只读）')
    p.add_argument('--source', default='all', choices=['all'] + list(SOURCES))
    p.add_argument('--today', default=None)
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_window)

    p = sub.add_parser('salvage', help='一次性抢救采数（默认 dry-run，--apply 落盘）')
    p.add_argument('--source', default='all', choices=['all'] + list(SOURCES))
    p.add_argument('--today', default=None)
    p.add_argument('--apply', action='store_true')
    p.set_defaults(func=cmd_salvage)

    p = sub.add_parser('proof', help='只读自证：跑前跑后快照树逐字一致')
    p.add_argument('--ledger', default=str(LEDGER_DEFAULT))
    p.add_argument('--today', default=None)
    p.set_defaults(func=cmd_proof)

    sub.add_parser('selftest').set_defaults(func=lambda a: selftest())

    args = ap.parse_args(argv)
    if not getattr(args, 'func', None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
