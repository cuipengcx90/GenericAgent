# 通知推送通道 SOP

## 通道清单
| 通道 | 模块 | 配置 |
|------|------|------|
| 飞书私信 | reflect_scripts/reflect_common.py `notify_feishu()` | mykeys: fs_app_id/fs_app_secret |
| 飞书群 | reflect_scripts/notify_feishu_group.py | config/feishu_group.json |
| 钉钉群 | reflect_scripts/notify_dingtalk_group.py | config/dingtalk_group.json |

## 集成点
- `helper.py` complete_task(): 飞书群 + 钉钉群（自主任务完成时自动推送）
- `sche_tasks` prompt 内指令: 晨报/晚报含飞书私信+飞书群+钉钉群三路推送
- `run_tech_trends.py`: 脚本内部推送**三通道**（飞书私信 + 飞书群 + 钉钉群，见该文件 L292/299/304-308）
- `eia_supply_chain_digest_comtrade.py`: 现仅见于 `temp/eia_supply_chain_digest.py`（`sche_tasks/` 下无同名脚本，仅 `sche_tasks/eia_weekly.json` 任务声明）⇒ 原记「仅飞书，待集成钉钉」的适用范围未经复核，勿据此断言

## 坑点
- 钉钉Webhook无签名校验，直接POST即可；飞书群Webhook需hmac签名
- 钉钉消息体用 `msgtype`+`text.content`；飞书用 `msg_type`+`content.text`
- sche_tasks下LLM直出报告(sess.ask())未经清洗会把系统提示强制要求的`<summary>`/`<thinking>`等内部标签原样带入推送内容(2026-07-31修复)。前端fsapp.py/wechatapp.py/plan_state.py各自有`_clean()`剥离`<thinking><summary><tool_use><file_content>`标签，但sche_tasks三个直连LLM的脚本(run_trendradar_digest.py/run_tech_trends.py/run_hellogithub_monthly.py)绕过了前端未经清洗。已在三者report赋值处补同款`_strip_llm_tags()`(复制_TAG_PATS逻辑，未抽公共util)。新增sche_tasks脚本用sess.ask()生成报告时必须同样清洗，检查方式：`grep -RIln "sess.ask(" sche_tasks/*.py`。


## 坑点（续）

- **钉钉群 text 消息有体积上限**：超限时服务端返回 `errmsg`「body 大小不合法;请保持在 20000bytes 以内」。本机口径 = 请求体 `json.dumps({"msgtype":"text","text":{"content":text}}).encode()`（`ensure_ascii` 默认 True ⇒ CJK 转 `\uXXXX`，报告类文本实测膨胀约 1.4×）。2026-10-01 `github_trending` 首次命中（请求体 34012 B；报告 23965 B / 17681 字符）。★阈值与体积**非单调**：09-23 以 30154 B 成功、10-01 以 34012 B 失败 ⇒ 判定一律以返回体 `errmsg` 为准，勿据单一数字反推规则，也勿用文件字节数替代请求体口径。
- **推送行的前缀 emoji 不是送达凭据**：`run_tech_trends.py:308` 只 print `ding_result['errmsg']`，前缀恒为 ✅ ⇒ 失败同样打 ✅（2026-10-01 日志「钉钉群: ✅ body 大小不合法…」）。判送达须看 errmsg/errcode 语义（同族纪律：代码路径 > 自述/装饰符）。
- **CoT 泄漏与推送体积耦合**：同日报告在标题锚点前的泄漏 9341 字符（占 52.8%），按待批修法裁掉后请求体仍 20586 B > 20000 ⇒ **单靠裁剪不足以解超限**，须并配分片或压缩正文（见 `temp/pending_decisions/DECISIONS_github_trending_cot_leak.md` D4–D6）。
