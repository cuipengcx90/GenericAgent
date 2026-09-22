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
2. **模型调用改统一入口(2026-07-28)**：不再用 `NativeClaudeSession` 单模型直连,改调 `llmcore.build_mixin_session()`(读 `mykey.py:mixin_config`,跨模型自动兜底,详见 trendradar_sop 第13章),`sess.ask(msg)` 返回 generator 需 join
3. **当月数据不全**：默认取上月(datetime.now().replace(day=1) - timedelta(days=1))
4. **numpy warning 无害**：LLMLingua warmup 失败不影响主流程，忽略即可
5. **推送前质量闸门(2026-07-28新增)**：`_validate_report(text)` 校验必备段(HelloGitHub/月度精选/月刊/数据来源)缺失、含中断标记(`!!!Error:`)、正文过短(<约800字)则抛 `ReportQualityError` 阻断推送,绝不发残缺报告。
6. **头部 CoT(思维链)泄漏(2026-09-22实测)**：模型偶发吐**纯文本**英文 planning(`I need to...`/`Let me organize`/`Format:`/`Final answer`),**无 XML 标签**→`_strip_llm_tags` 抓不到→整段随报告推送(本次前168行≈9376字污染,真报告从第169行起,标题`🌟 HelloGitHub 精选月报`在文中出现两次)。修=新增 `_strip_cot_preamble()` 以标题正则 `^\s*🌟\s*HelloGitHub\s*精选月报.*$` 取**最后一个**标题行起截断 + `_cot_head_hits()` 900字探针告警。同类缺陷见 `run_tech_trends`/trendradar(均"LLM思考过程污染产出物")。
