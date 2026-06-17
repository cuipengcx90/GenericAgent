# HelloGitHub 月度推送 SOP

## 脚本位置
- `sche_tasks/run_hellogithub_monthly.py` + `sche_tasks/hellogithub_monthly.json`

## 数据源 API（已验证）
- 基础: `https://api.hellogithub.com/v1/`
- 月度精选榜: `GET /v1/?sort_by=featured&rank_by=monthly&tid=all&year=Y&month=M&page=P`
  - 返回 ~40条(2页)，has_more 控制翻页
  - 当月数据极少，必须取上月
- 月刊Volume: `GET /v1/periodical/` → volumes列表获取最新num
  - `GET /v1/periodical/volume/?num=N` → 13分类~40项目

## 关键坑点
1. **飞书 webhook 是纯文本**：msg_type=text 不渲染 Markdown，prompt 必须要求 AI 输出纯文本+emoji排版
2. **NativeClaudeSession 用法**：需 cfg 参数 `cfg=next((v for k,v in mykeys.items() if 'native' in k and 'claude' in k), None)`，`sess.ask(msg)` 返回 generator 需 join
3. **当月数据不全**：默认取上月(datetime.now().replace(day=1) - timedelta(days=1))
4. **numpy warning 无害**：LLMLingua warmup 失败不影响主流程，忽略即可