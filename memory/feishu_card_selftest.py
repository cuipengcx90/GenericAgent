#!/usr/bin/env python3
"""飞书卡片结构自检：实发一张含翻页按钮的卡片，验证 JSON 是否被飞书接受。

为什么需要它：本地语法检查 + 单元测试**测不出字段合法性**，只有飞书服务端会返回
ErrPath/ErrMsg（例如把按钮包进 {"tag":"action"} 时报 200861 unsupported tag action）。
所以任何改动 fsapp.py 卡片结构（_build_locked / _step_panel / _page_buttons / _card_raw）
之后，必须跑一次本脚本再收工。

用法：
    python3 memory/feishu_card_selftest.py <open_id>          # 手写翻页卡（分页按钮）
    python3 memory/feishu_card_selftest.py --ask --from-log   # ask_user 候选按钮卡，open_id 自动取
    python3 memory/feishu_card_selftest.py --ask <open_id> --delete

    --ask     用 `frontends.fsapp._TaskCard` 真实构建器产出 ask 候选按钮卡（与线上卡片零漂移；
              手写结构会随 fsapp 改动漂移，这正是本脚本旧版的弱点）
    --delete  发送成功后立即撤回，零残留（自动化/日常校验推荐）
    --from-log open_id 从 logs/feishu_agent.log 里取最近发消息的真人 open_id
    # 坑：mykey.fs_allowed_users 配成 ['*'] 时是通配符（PUBLIC_ACCESS），**不是** open_id，
    #     拿它当参数会得到 99992351 invalid open_id；单测桩 ou_monitor/ou_intruder 也不是真人。

判据：输出 success=True 即结构合法；否则按返回的 ErrPath 定位出错 element。
      结构合法但**点击无反应** ⇒ 问题在回调侧（register_p2_card_action_trigger / value 里
      的 ga_card 注册表），不是卡片结构。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mykey  # noqa: E402
import lark_oapi as lark  # noqa: E402
from lark_oapi.api.im.v1 import (  # noqa: E402
    CreateMessageRequest, CreateMessageRequestBody, DeleteMessageRequest,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_card():
    """与 fsapp._TaskCard._build_locked 产出的结构保持一致：折叠步骤 + 直放的翻页按钮。"""
    return {
        "schema": "2.0",
        "config": {"streaming_mode": False, "width_mode": "fill"},
        "body": {"elements": [
            {"tag": "markdown", "content": "**卡片结构自检** — 能看到本卡片并显示下面三个按钮，即结构合法"},
            {"tag": "collapsible_panel", "expanded": False,
             "header": {"title": {"tag": "plain_text", "content": "Turn 1 · 示例步骤"}},
             "elements": [{"tag": "markdown", "content": "示例详情"}]},
            {"tag": "button", "type": "default",
             "text": {"tag": "plain_text", "content": "◀ 上一页"},
             "value": {"ga_card": "selftest", "page": 0}},
            {"tag": "button", "type": "text",
             "text": {"tag": "plain_text", "content": "1/2"}},
            {"tag": "button", "type": "primary",
             "text": {"tag": "plain_text", "content": "下一页 ▶"},
             "value": {"ga_card": "selftest", "page": 1}},
        ]},
    }


def open_id_from_log():
    """从运行时日志取最近发消息的真人 open_id（跳过单测桩）。"""
    import re
    from collections import Counter
    p = os.path.join(ROOT, "logs", "feishu_agent.log")
    s = open(p, encoding="utf-8", errors="replace").read()
    for oid, _n in Counter(re.findall(r"ou_[0-9a-f]{32}", s)).most_common():
        if oid not in ("ou_monitor", "ou_intruder"):
            return oid
    raise SystemExit("日志里找不到 ou_ 形式的 open_id")


def build_ask_card(to):
    """用 fsapp 自己的构建器产出 ask_user 候选按钮卡 —— 与线上真实卡片零漂移。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import frontends.fsapp as F
    card = F._TaskCard(to, "open_id", owner_open_id=to)
    card.set_ask_menu("__SELF_CHECK__", ["A", "B"])
    card.done("🧪 ask 按钮卡结构自检（--delete 会自动撤回）")
    raw = card._build_locked()
    return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    to = None
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    pos = [a for a in sys.argv[1:] if not a.startswith("--")]
    if pos:
        to = pos[0]
    elif "--from-log" in flags:
        to = open_id_from_log()
    if not to:
        print(__doc__)
        return 2
    content = build_ask_card(to) if "--ask" in flags else json.dumps(build_card(), ensure_ascii=False)
    client = (lark.Client.builder()
              .app_id(mykey.fs_app_id).app_secret(mykey.fs_app_secret)
              .log_level(lark.LogLevel.INFO).build())
    body = CreateMessageRequest.builder().receive_id_type("open_id").request_body(
        CreateMessageRequestBody.builder().receive_id(to).msg_type("interactive")
        .content(content).build()
    ).build()
    r = client.im.v1.message.create(body)
    print(f"to={to} len={len(content)} success={r.success()} code={r.code} msg={r.msg}")
    if not r.success():
        print("→ 结构被拒：按 ErrPath（形如 body.elements[N](tag: xxx)）定位到具体 element")
        return 1
    if "--delete" in flags and r.data and r.data.message_id:
        d = client.im.v1.message.delete(
            DeleteMessageRequest.builder().message_id(r.data.message_id).build())
        print(f"撤回 msg_id={r.data.message_id} code={d.code} {d.msg}")
    print("→ 结构合法。若按钮点击无反应，问题在回调订阅侧，不是卡片结构。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
