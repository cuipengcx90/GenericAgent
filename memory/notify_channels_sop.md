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
- `run_tech_trends.py` / `eia_supply_chain_digest_comtrade.py`: 脚本内部推送（仅飞书，待集成钉钉）

## 坑点
- 钉钉Webhook无签名校验，直接POST即可；飞书群Webhook需hmac签名
- 钉钉消息体用 `msgtype`+`text.content`；飞书用 `msg_type`+`content.text`
