#!/usr/bin/env python3
"""飞书卡片结构自检：实发一张含翻页按钮的卡片，验证 JSON 是否被飞书接受。

为什么需要它：本地语法检查 + 单元测试**测不出字段合法性**，只有飞书服务端会返回
ErrPath/ErrMsg（例如把按钮包进 {"tag":"action"} 时报 200861 unsupported tag action）。
所以任何改动 fsapp.py 卡片结构（_build_locked / _step_panel / _page_buttons / _card_raw）
之后，必须跑一次本脚本再收工。

用法：
    python3 memory/feishu_card_selftest.py <open_id>
    # open_id 可从日志取：grep "收到消息" logs/feishu_agent.log | tail -1
    # 形如 ou_xxxxxxxx

判据：输出 success=True 即结构合法；否则按返回的 ErrPath 定位出错 element。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mykey  # noqa: E402
import lark_oapi as lark  # noqa: E402
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody  # noqa: E402


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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    to = sys.argv[1]
    client = (lark.Client.builder()
              .app_id(mykey.fs_app_id).app_secret(mykey.fs_app_secret)
              .log_level(lark.LogLevel.INFO).build())
    body = CreateMessageRequest.builder().receive_id_type("open_id").request_body(
        CreateMessageRequestBody.builder().receive_id(to).msg_type("interactive")
        .content(json.dumps(build_card(), ensure_ascii=False)).build()
    ).build()
    r = client.im.v1.message.create(body)
    print(f"success={r.success()} code={r.code} msg={r.msg}")
    if not r.success():
        print("→ 结构被拒：按 ErrPath（形如 body.elements[N](tag: xxx)）定位到具体 element")
        return 1
    print("→ 结构合法。若按钮点击无反应，问题在回调订阅侧，不是卡片结构。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
