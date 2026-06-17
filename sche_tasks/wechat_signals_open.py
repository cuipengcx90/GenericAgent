#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from wechat_normalize import normalize
from wechat_open_common import load_yaml
from wechat_open_digest import write_json
from wechat_recall import recall_candidates
from wechat_render_daily import render_markdown as render_preview_markdown
from wechat_render_human_daily import render_human_daily
from wechat_theme_synth import synthesize_themes
from wechat_threading import build_threads
from wechat_view_mapper import map_views


def run_pipeline(input_path: str, output_dir: str, category: str | None = None) -> dict:
    in_path = Path(input_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = load_yaml(in_path)
    norm = normalize(raw)
    recall = recall_candidates(norm)
    threads = build_threads(recall)
    themes = synthesize_themes(threads)
    views = map_views(themes)
    md = render_human_daily(views, norm)
    preview_md = render_preview_markdown(views, norm)

    write_json(out_dir / 'raw_messages.json', norm)
    write_json(out_dir / 'recall_candidates.json', recall)
    write_json(out_dir / 'threads.json', threads)
    write_json(out_dir / 'themes.json', themes)
    write_json(out_dir / 'daily_digest.json', views)
    (out_dir / 'daily.md').write_text(md, encoding='utf-8')
    (out_dir / 'daily_preview.md').write_text(preview_md, encoding='utf-8')

    return {
        'output_dir': str(out_dir),
        'group_name': norm['group_name'],
        'total_msgs': norm['total_msgs'],
        'candidate_count': recall['candidate_count'],
        'thread_count': threads['thread_count'],
        'theme_count': themes['theme_count'],
        'category': themes['category'],
        'daily_md': str(out_dir / 'daily.md'),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True, help='raw yaml path')
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--category', choices=['coding', 'saas'])
    args = ap.parse_args()
    print(json.dumps(run_pipeline(args.input, args.output_dir, args.category), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
