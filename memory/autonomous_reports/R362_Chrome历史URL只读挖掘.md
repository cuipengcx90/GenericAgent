# R362 Chrome 历史 URL 只读挖掘（12,377 唯 URL / 31,498 次访问）

- 数据源：`~/Library/Application Support/Google/Chrome/Default/History`（33.2 MB，含 `History-journal` 一并拷贝）
- 纪律：**只为读**——拷贝到 `temp/chrome_mine/db/` 后全部查询走 `file:...?mode=ro`；源库 mtime 拷贝前=拷贝后（`History 10:43:22`、`-journal 11:04:22`），未写、未改、未 VACUUM、未建 journal
- 复现：`python3 temp/chrome_mine/mine.py`（明细 `out.json`/`agg.json`）；台账 `pan_ledger.md`；脚本 `temp/chrome_mine/mine.py`
- 时间跨度：2026-06-25 10:44 → 2026-09-23 10:33（**正好 90 天**）

## 0. 先说三条口径（否则数字会被误读）
1. **90 天滚动**：最早一条正好在 90 天前 ⇒ Chrome 默认保留期。这库**每过一天就丢一天**（同 knowledgeC 的 ≈4 周病）。
2. **至少 21.5% 的访问来自我们自己的自驾任务**：`linux.do 4368(87天)`、`github.com 1597(78天)`、`birdiesearch 297`、`haisou 203`、`trytrellis 86` = 6,551 次（每日日报/TL3 检查/项目监控/网盘监控都走这个 Chrome）。其中 linux.do、github 是**人机混用**、历史表无法区分 ⇒ 只有一条铁律：**凡"用户高频"结论，必须先声明这部分污染**，否则至少虚高五分之一。
3. 计数口径：`urls.visit_count` 求和 30,539（含同 URL 重复），`visits` 表 31,498 行，差别来自 Chrome 自身记账，不影响排序结论。

## 1. 结论 A（最大真缺口）：17 个 AI「公益站/中转站」共 4,116 次访问 = 总访问 13.5%，**没有任何任务在管**
域名与活跃天数（标题取样已定性）：

| 站点 | 访问 | 活跃天 | 标题取样 |
|---|---|---|---|
| micuapi.ai | 1426 | 69 | 米醋API |
| h-e.top | 297 | 52 | 烁 |
| agentrouter.org | 255 | 41 | Agent Router |
| anyrouter.top | 203 | 48 | AnyRouter｜Claude Code 免费共享平台 |
| x666.me | 196 | 48 | 薄荷 API / 薄荷公益站 |
| muyuan.do | 191 | 33 | 君の公益 |
| chybenzun.top | 187 | 35 | CHY公益站 |
| pp.ua | 181 | 59 | WONG公益站 |
| 4router.net | 176 | 17 | 4Router |
| hxi.me | 167 | 59 | 随时跑路公益站 |
| de5.net | 160 | 22 | V - API |
| quangneng.com | 146 | 9 | — |
| lilililwan.xyz | 136 | 46 | liWAN LAB |
| venlacy.com | 130 | 27 | Lanln |
| windhub.cc | 113 | 48 | Ark API |
| dev88.tech | 78 | 6 | DEV88公益 |
| 121628.xyz | 74 | 8 | — |

- **重复单页第 1 名就是 `micuapi.ai` 346 次**（第 3/4/9 名同站 230/228/165 次）⇒ 典型"手动点开看还有多少额度"。
- 管了吗？`grep -l micuapi\|x666\|muyuan\|hxi.me\|4router\|windhub\|… sche_tasks/*.json` = **零命中**；`tools/`、`reflect_scripts/` 无余额/额度探测脚本。
- 消费者：**一个每日额度看板**（探测 17 站余额/可用性 → 一条飞书或一张卡），可替代约 **46 次/天**的手动点击（4116/90）。
- 可行性已实测（匿名）：`anyrouter.top/api/status` → HTTP 200 但 body 是**JS 反爬挑战**（`var arg1='B85EA8…'`）；`micuapi.ai`、`h-e.top` 匿名请求 8s 超时（http=000）。⇒ **匿名不可行，必须复用浏览器已登录会话（TMWebDriver）或用户给 token**。
- 提案（**待批，本轮未实施**）：反射器（30min 轮询、`check()` 恒返 None、24h 指纹去重、全绿零输出，完全复刻 `07_l2_gate`/`price_alert` 范式）→ 每站只读一次余额；某站可用性变化/余额由正转 0 才私信。风险：涉及用户账号凭据、站点随时跑路（有的名字就叫"随时跑路公益站"），故只做只读探测，不自动注册/充值。

## 2. 结论 B：本机第 1 域名是自家业务后台 `smyze.cn` 6,478 次 / 72 天
重复单页全是运营页面：`用户列表` 284+227、`门店列表` 210、`角色管理` 129、`泡泡活动管理` 120、`饮品商品库` 119。
- 现状：已有 `weekly_inspection`(opserver SLS)、`weekly_report` 覆盖**运维/代码**侧；**没有**覆盖"运营侧数据每天翻一遍"。
- 消费者：可选「每日运营指标快照」（门店数/用户数/活动数→一行飞书）。**但这是我推断的需求，不是用户明说的** ⇒ 只作候选，不自动实施（避免再造一个"只给建议不动手"的僵尸任务，参见已归档的 folder_watcher 前例）。

## 3. 结论 C：1,047 条网盘分享链接从未归档 → 已导出可用台账
`pan.quark.cn 537 / pan.baidu.com 491 / alipansou 8 / dalipan 8 / alipan 2 / aliyundrive 1`；其中 URL 带 `pwd=` 或标题含提取码的 **323 条**。
- 产出：`temp/chrome_mine/pan_ledger.md`（域名+标题+次数+末访+完整 URL，含提取码 ⇒ **仅本地文件，不进报告**）
- 消费者：`baidu_pan_transfer_sop`(转存) / `birdiesearch_monitor`(新命中) 需要"以前存过没"时直接查台账；也避免重复搜索同一资源。
- 边界：台账是历史快照，链接可能已失效；不自动转存、不批量请求网盘。

## 4. 结论 D：90 天滚动 ⇒ 建议纳入"每日快照"（与 knowledgeC 同一条路）
`08_kc_snapshot.py`（已写好、**尚未接入**，等批准走 A 方案"复用 07 的 30min launchd"）正是干这个的。Chrome History 可挂同一路径：
- 成本：每天 1 份 ≈ 33 MB ⇒ 30 天 ≈ 1 GB。**更省的做法**：只导出 `urls+visits` 两张表为 gzip SQL/JSON（实测这两张表就是全部信号），体积可降一个量级。
- 消费者：①本次挖到的结论不会每天蒸发 ②将来"某域名什么时候开始/停止出现"这类问题可答（现在只能力保 90 天内）。

## 5. 结论 E：搜索词是**已被验证有效**的项目发现信号（这次挖到 13 个未监控候选）
现有 `project_update_monitor/sources.json` 8 项里有 **Paseo / Multica / Tutti / Orca** —— 正好也是历史里的搜索词（paseo 8、multica 15、tutti 8、orca 8）⇒ 说明"用户搜什么"确实被转化成了监控项。
仍未进监控的搜索词候选（次数）：`goose 7`、`omnigent 7`、`clauge 7`、`cliproxyapi 7`、`mediago 7`、`plan-tree 7`、`zentao mcp 7`、`humanize 7`、`maigret 7`、`molio 7`、`dsh-tui 7`、`ccg 7`、`charlestang/codex-obsidian 7`、`repo:nicobailon/pi-subagents 7`。
- 消费者：`project_update_monitor` 的 sources.json 扩展（该任务已跑通、加源零成本）。
- **但需用户挑选**：加了就会真推送，我不替用户决定追哪些项目（`cliproxyapi` 与我们 fofa 代理打猎工作最相关；`zentao mcp` 与业务 PM 相关）。

## 6. 结论 F：下载 40 条（近端）——只有 1 条有消费者，其余按验收③剔除
来源分布：`github.com 15 / aliyuncs 5 / 163.com 5 / sh.gov.cn 3 / beijing.gov.cn 2 / smyze.cn 2`。
- 文件例：`PI-Desktop-0.14.9-beta.1(142MB)`、`Pebrel-v1.7.0-macos-arm64(34.7MB)`、`DeepSeek-Harness-.pdf`、`出入库汇总_*.xlsx`(smyze 导出)。
- 唯一有消费者的发现：**下载清单与搜索词同源**（PI / Pebrel / DeepSeek-Harness 都在搜索词里）⇒ 印证结论 E 的信号有效性。
- `~/Downloads` 279 项 0.88 GB：**不提案清理**——folder_watcher 的教训是"只给建议不动手"的清理任务 500 跑 7 命中后被归档，不做第二个。

## 7. 按验收③明确剔除的发现（无消费者）
- `bilibili vip` 搜索 21 次（最高频搜索词）：个人消费诉求，无可自动化消费者。
- 政务/银行/税务：268 次访问 / 10 域（`sh.gov.cn 98`、`chinatax.gov.cn 94`、参保缴费等 PDF 含身份证号）⇒ **不进报告明细**，也不建议自动化（低频+高敏感）。
- 深夜时段（23:00 1058 次）与周内分布（周五 6405、周四 5636 最高，周六 2379 最低）：只能说明作息，**没有可执行消费者**，仅作背景。
- 10:00–11:00 / 13:00–17:00 各 2,800–3,700 次为工作日高峰，**与自驾任务运行时段重叠**（进一步印证口径 2 的污染）。

## 8. 本轮未做 / 待批
1. 额度看板反射器（结论 A）——需批准，且需确定登录态来源（浏览器会话 / 用户 token）。
2. Chrome History 每日快照（结论 D）——建议与 `08_kc_snapshot.py` 合并批准、走 07 的 30min launchd（零新增定时）。
3. sources.json 加源（结论 E）——等用户挑选项目。
4. 运营指标快照（结论 B）——等用户确认是否真有这个需求。

## 9. 记忆更新建议（**未写**，按 META-SOP/自主 SOP 只附在报告末尾待审）
- L1「定时/自主/反射」行补极简一句：
  `| Chrome历史(90天滚动,12,377 URL/31,498访问)只读挖掘R362: 21.5%访问系自驾任务自造须先剥离;最大真缺口=17个AI公益站4,116次(13.5%)无监控且匿名探测被JS挑战拦⇒须浏览器登录态;网盘分享链接1,047条已出台账temp/chrome_mine/pan_ledger.md;产物 temp/chrome_mine/`
- L3 建议：`procmem_scanner_sop` 或新 `chrome_history_mining` 小条——**"浏览器历史必先剥离自驾访问域"** 是复用性最强的一条（否则任何"用户高频"结论都会虚高 1/5）。
