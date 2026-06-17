#!/usr/bin/env python3
"""
sqlite_probe.py — 本地SQLite快速侦查工具
功能：schema查询 / 行数统计 / 数据抽样 / 自定义查询 / CLI支持
用法：
    from sqlite_probe import probe_schema, probe_row_counts, probe_sample, probe_query
    python memory/sqlite_probe.py <db_path> [--table T] [--sql SQL] [--sample N]
"""

import sqlite3
import json
import sys
import os
from typing import Optional, List, Dict, Any


# ─────────────────────────────────────────────
# 核心API
# ─────────────────────────────────────────────

def probe_schema(db_path: str) -> Dict[str, List[Dict]]:
    """
    返回所有表的字段信息。
    结构：{table_name: [{"cid": 0, "name": "id", "type": "INTEGER", ...}, ...]}
    """
    db_path = os.path.expanduser(db_path)
    result = {}
    try:
        conn_ctx = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        conn_ctx = sqlite3.connect(db_path)
    with conn_ctx as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r["name"] for r in cur.fetchall()]
        for tbl in tables:
            cur.execute(f"PRAGMA table_info({tbl})")
            result[tbl] = [dict(r) for r in cur.fetchall()]
    return result


def probe_row_counts(db_path: str) -> Dict[str, int]:
    """
    返回所有表的行数统计。
    结构：{table_name: row_count}
    """
    db_path = os.path.expanduser(db_path)
    result = {}
    try:
        conn_ctx = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        conn_ctx = sqlite3.connect(db_path)
    with conn_ctx as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM [{tbl}]")
                result[tbl] = cur.fetchone()[0]
            except Exception as e:
                result[tbl] = f"ERROR: {e}"
    return result


def probe_sample(db_path: str, table: str, n: int = 5,
                 order_by: Optional[str] = None) -> List[Dict]:
    """
    从指定表抽样前N行。
    order_by: 可指定排序字段（如 "ROWID DESC"）
    """
    db_path = os.path.expanduser(db_path)
    order_clause = f"ORDER BY {order_by}" if order_by else ""
    sql = f"SELECT * FROM [{table}] {order_clause} LIMIT {int(n)}"
    try:
        conn_ctx = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        conn_ctx = sqlite3.connect(db_path)
    with conn_ctx as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def probe_query(db_path: str, sql: str,
                params: Optional[tuple] = None) -> List[Dict]:
    """
    执行任意只读SQL，返回行列表。
    """
    db_path = os.path.expanduser(db_path)
    try:
        conn_ctx = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        conn_ctx = sqlite3.connect(db_path)
    with conn_ctx as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]


def probe_summary(db_path: str) -> Dict[str, Any]:
    """
    一键综合摘要：表数、总行数、文件大小、表列表。
    """
    db_path_exp = os.path.expanduser(db_path)
    schema = probe_schema(db_path)
    counts = probe_row_counts(db_path)
    total_rows = sum(v for v in counts.values() if isinstance(v, int))
    file_size = os.path.getsize(db_path_exp)
    return {
        "db_path": db_path_exp,
        "file_size_bytes": file_size,
        "table_count": len(schema),
        "total_rows": total_rows,
        "tables": {
            t: {"columns": len(cols), "rows": counts.get(t, "?")}
            for t, cols in schema.items()
        }
    }


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="SQLite快速侦查工具")
    parser.add_argument("db", help="SQLite数据库路径")
    parser.add_argument("--schema", action="store_true", help="显示表结构")
    parser.add_argument("--counts", action="store_true", help="显示各表行数")
    parser.add_argument("--sample", type=int, metavar="N", help="抽样N行（需指定--table）")
    parser.add_argument("--table", type=str, help="目标表名")
    parser.add_argument("--sql", type=str, help="执行自定义SQL")
    parser.add_argument("--summary", action="store_true", help="综合摘要（默认）")
    args = parser.parse_args()

    if args.schema:
        s = probe_schema(args.db)
        for tbl, cols in s.items():
            print(f"\n[{tbl}]  ({len(cols)} cols)")
            for c in cols:
                print(f"  {c['cid']:>2}. {c['name']:<20} {c['type']}")
    elif args.counts:
        counts = probe_row_counts(args.db)
        for t, n in sorted(counts.items(), key=lambda x: -x[1] if isinstance(x[1], int) else 0):
            print(f"  {n:>8}  {t}")
    elif args.sample is not None:
        if not args.table:
            print("❌ --sample 需配合 --table 使用")
            sys.exit(1)
        rows = probe_sample(args.db, args.table, args.sample)
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    elif args.sql:
        rows = probe_query(args.db, args.sql)
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        summary = probe_summary(args.db)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _cli()