#!/usr/bin/env python3
"""WeChat群消息分页全量拉取工具 — GA subagent/主agent通用

用法:
  python wechat_fetch_helper.py --groups "群名1,群名2" --date 2026-05-17 --output-dir ./temp/wechat_batch/
  python wechat_fetch_helper.py --category saas --date-range 2026-05-12~2026-05-18 --output-dir ./temp/wechat_batch/

功能:
  1. 从 groups.json 解析群名/id
  2. 分页循环拉取全量消息(PAGE_SIZE=2000, 循环直到batch<PAGE_SIZE)
  3. 按 {output_dir}/{date}/{group_slug}.yaml 存储原始消息
  4. 输出 manifest.json 供后续subagent消费

依赖: pyyaml (GA环境已有)
"""
import argparse, json, os, sys, urllib.request, urllib.error
from datetime import date, timedelta, datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    # fallback: 用subprocess调curl + 手动解析
    yaml = None

GROUPS_JSON = '/Users/cuip/develop/obsidian-note/WeChat-Signals/_config/groups.json'
CHATLOG_API = 'http://127.0.0.1:5030'
PAGE_SIZE = 2000


def load_groups(groups_json=GROUPS_JSON):
    with open(groups_json) as f:
        return json.load(f)


def resolve_groups(config, group_names=None, category=None):
    """按群名或品类筛选群列表, 返回 [{name, id, category, output_dir}]"""
    result = []
    seen_ids = set()
    for cat_key in ['saas', 'coding']:
        cat = config.get(cat_key, {})
        for g in cat.get('groups', []):
            if g['id'] in seen_ids:
                continue
            seen_ids.add(g['id'])
            if category and cat_key != category:
                continue
            if group_names:
                matched = any(kw in g['name'] for kw in group_names)
                if not matched:
                    continue
            result.append({
                'name': g['name'],
                'id': g['id'],
                'category': cat_key,
                'output_dir': cat.get('output_dir', f'{cat_key}/Daily')
            })
    return result


def slug(name, max_len=20):
    """生成文件名安全的slug"""
    import re
    s = re.sub(r'[^\w\u4e00-\u9fff]+', '-', name)[:max_len].strip('-')
    return s or 'unknown'


def fetch_messages_chatlog(group_id, target_date, api_base=CHATLOG_API):
    """分页循环拉取指定群指定日期的全量消息, 返回消息列表"""
    all_msgs = []
    offset = 0
    while True:
        url = f'{api_base}/api/v1/history?chat={group_id}&time={target_date}&limit={PAGE_SIZE}&offset={offset}'
        try:
            resp = urllib.request.urlopen(url, timeout=30)
            raw = resp.read().decode('utf-8')
        except urllib.error.URLError as e:
            print(f'  [ERROR] fetch failed: {e}', file=sys.stderr)
            break
        except Exception as e:
            print(f'  [ERROR] unexpected: {e}', file=sys.stderr)
            break

        if yaml:
            data = yaml.safe_load(raw) or {}
        else:
            import tempfile, subprocess
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False)
            tmp.write(raw)
            tmp.close()
            result = subprocess.run(
                [sys.executable, '-c', f'import yaml,json; print(json.dumps(yaml.safe_load(open("{tmp.name}"))))'],
                capture_output=True, text=True
            )
            os.unlink(tmp.name)
            data = json.loads(result.stdout) if result.returncode == 0 else {}

        batch = data.get('messages', [])
        all_msgs.extend(batch)
        print(f'  offset={offset}, got={len(batch)}, total={len(all_msgs)}')
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return all_msgs


def parse_day_bounds(target_date: str):
    dt = datetime.strptime(target_date, '%Y-%m-%d')
    start = int(dt.timestamp())
    end = int((dt + timedelta(days=1)).timestamp())
    return start, end


def normalize_decrypt_message(item, target_date: str):
    ts = int(item.get('timestamp') or 0)
    time_str = item.get('time')
    if not time_str and ts:
        time_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    elif not time_str:
        time_str = f'{target_date} 00:00:00'
    content = item.get('content', '')
    if content is None:
        content = ''
    return {
        'local_id': item.get('local_id', ''),
        'timestamp': ts,
        'time': time_str,
        'sender': item.get('sender') or 'unknown',
        'type': item.get('type') or 'text',
        'content': content,
    }


def fetch_messages_decrypt(group, target_date: str, decrypt_json_dir: str):
    src_dir = Path(decrypt_json_dir)
    if not src_dir.exists():
        raise FileNotFoundError(f'decrypt json dir not found: {src_dir}')

    gid = group['id']
    gname = group['name']
    gslug = slug(gname)
    candidates = []
    fallback_by_content = []
    exact_name = f'{gslug}_{target_date}.json'

    for p in sorted(src_dir.glob('*.json')):
        name = p.name
        matched_by_name = gid in name or gslug in name or gname in name or name == exact_name
        raw = None
        if matched_by_name:
            candidates.append(p)
            continue
        try:
            raw = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        username = str(raw.get('username') or '')
        chat = str(raw.get('chat') or '')
        if username == gid or chat == gid or chat == gname:
            fallback_by_content.append((p, raw))

    chosen = None
    for p in candidates:
        try:
            raw = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        username = str(raw.get('username') or '')
        chat = str(raw.get('chat') or '')
        if username == gid or chat == gid or chat == gname:
            chosen = (p, raw)
            break

    if chosen is None and fallback_by_content:
        chosen = fallback_by_content[0]

    if chosen is None and candidates:
        p = candidates[0]
        chosen = (p, json.loads(p.read_text(encoding='utf-8')))

    if chosen is None:
        raise FileNotFoundError(f'未在 {src_dir} 找到群 {gname}({gid}) 的 decrypt JSON')

    src_path, raw = chosen
    start_ts, end_ts = parse_day_bounds(target_date)
    out = []
    for item in raw.get('messages', []):
        ts = int(item.get('timestamp') or 0)
        if ts and not (start_ts <= ts < end_ts):
            continue
        norm = normalize_decrypt_message(item, target_date)
        if norm['time'][:10] != target_date and ts == 0:
            continue
        out.append(norm)
    out.sort(key=lambda x: (int(x.get('timestamp') or 0), str(x.get('local_id') or '')))
    print(f'  decrypt source={src_path}, total_in_file={len(raw.get("messages", []))}, matched_date={len(out)}')
    return out


def fetch_messages(group, target_date, source='decrypt', api_base=CHATLOG_API, decrypt_json_dir=None):
    if source == 'decrypt':
        if not decrypt_json_dir:
            decrypt_json_dir = 'temp/wechat-decrypt-test/exports_today'
        return fetch_messages_decrypt(group, target_date, decrypt_json_dir)
    return fetch_messages_chatlog(group['id'], target_date, api_base=api_base)


def date_range(start_s, end_s):
    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)
    days = []
    cur = start
    while cur <= end:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def main():
    parser = argparse.ArgumentParser(description='WeChat群消息全量分页拉取')
    parser.add_argument('--groups', type=str, help='群名关键词,逗号分隔 (模糊匹配)')
    parser.add_argument('--category', choices=['saas', 'coding'], help='按品类筛选')
    parser.add_argument('--date', type=str, help='单日日期 YYYY-MM-DD')
    parser.add_argument('--date-range', type=str, help='日期范围 YYYY-MM-DD~YYYY-MM-DD')
    parser.add_argument('--output-dir', type=str, required=True, help='输出目录')
    parser.add_argument('--groups-json', type=str, default=GROUPS_JSON, help='groups.json路径')
    parser.add_argument('--api', type=str, default=CHATLOG_API, help='chatlog API地址')
    parser.add_argument('--source', choices=['chatlog', 'decrypt'], default='decrypt', help='消息源')
    parser.add_argument('--decrypt-json-dir', type=str, default='temp/wechat-decrypt-test/exports_today', help='wechat-decrypt 导出 JSON 目录')
    args = parser.parse_args()

    if args.date:
        dates = [args.date]
    elif args.date_range:
        parts = args.date_range.split('~')
        if len(parts) != 2:
            print('ERROR: --date-range 格式应为 YYYY-MM-DD~YYYY-MM-DD', file=sys.stderr)
            sys.exit(1)
        dates = date_range(parts[0], parts[1])
    else:
        dates = [(date.today() - timedelta(days=1)).isoformat()]

    config = load_groups(args.groups_json)
    group_names = [g.strip() for g in args.groups.split(',')] if args.groups else None
    groups = resolve_groups(config, group_names=group_names, category=args.category)

    if not groups:
        print('ERROR: 无匹配群, 请检查 --groups 或 --category', file=sys.stderr)
        sys.exit(1)

    print(f'=== 拉取计划: {len(groups)}群 × {len(dates)}天 source={args.source} ===')
    for g in groups:
        print(f'  [{g["category"]}] {g["name"]}')

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {'dates': dates, 'groups': [], 'files': [], 'source': args.source}

    for g in groups:
        g_slug = slug(g['name'])
        manifest['groups'].append({'name': g['name'], 'id': g['id'], 'category': g['category'], 'slug': g_slug})

        for d in dates:
            print(f'\n[{d}] {g["name"]} ({g["id"]})')
            msgs = fetch_messages(g, d, source=args.source, api_base=args.api, decrypt_json_dir=args.decrypt_json_dir)

            if not msgs:
                print(f'  → 无消息, 跳过')
                continue

            day_dir = output_dir / d
            day_dir.mkdir(parents=True, exist_ok=True)
            out_file = day_dir / f'{g_slug}.yaml'

            content = {
                'chat': g['name'],
                'username': g['id'],
                'group_name': g['name'],
                'group_id': g['id'],
                'category': g['category'],
                'date': d,
                'message_count': len(msgs),
                'messages': msgs
            }
            if yaml:
                with open(out_file, 'w', encoding='utf-8') as f:
                    yaml.dump(content, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            else:
                with open(out_file, 'w', encoding='utf-8') as f:
                    json.dump(content, f, ensure_ascii=False, indent=2)

            manifest['files'].append({
                'path': str(out_file),
                'group': g['name'],
                'group_id': g['id'],
                'category': g['category'],
                'date': d,
                'message_count': len(msgs),
                'slug': g_slug
            })
            print(f'  → {len(msgs)} msgs → {out_file}')

    manifest_path = output_dir / 'manifest.json'
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f'\n=== 完成! manifest: {manifest_path} ===')
    print(f'总文件数: {len(manifest["files"])}')


if __name__ == '__main__':
    main()
