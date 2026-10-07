# sharedchat 每日领额 SOP (L3 精简版; 完整版与凭据在 temp/, 勿入 git)

权威指针: temp/sharedchat_sop/{SOP.md, accounts.md+accounts.json(凭据台账, 含明文密码), daily_claim_status.md, claim_ledger.jsonl, findings.md}

## 已验证事实 (2026-10-07)
- 站点 new.sharedchat.cc; API base https://new.sharedchat.cc/codex (wire=responses, 模型 15 个)
- 传输层: python urllib 直打 /v1/* 必带浏览器 UA 头; CF 403 code1010=挑战页拦指纹(非账号问题), curl_cffi 次选
- ★claim 语义(对 NEW1 真发实测): 套餐每日 00:00 到期; isActive=false 才真领(claimed=true); active 时发 claim 返 200+「当前套餐仍在有效期内，到期后可以再次领取」claimed=false 无害
- 额度规则(用户口径, 待 0 点后首次真领实测回填): 每次 +15, 日上限 50
- 工具: python3 tools/sharedchat_claim_daily.py {--run|--status|--selftest}; 台账 claim_ledger.jsonl append-only
- 定时: sche_tasks/sharedchat_claim_daily.json 北京 09:30 daily (prompt 头 GA_ROOT 锚定行, 禁 launchd/crontab——用户规则)
- bg_task 坑: start 的 cmd 是位置参数(--shell 形式), 不认 --cmd
- 断流类 transient(IncompleteRead/BrokenPipeError) 必须重试(实测旧码无重试连环 ERROR)
- 403 响应也要解析 body(区分 CF 挑战页与业务错误码)
- 凭据仅存 temp/sharedchat_sop/ (gitignored); temp 被清理则需向用户重新索取
- 注册/邀请流程用户自理(2026-10-07 用户明确接管); NEW2/NEW3 待用户提供后登记进 accounts.json
