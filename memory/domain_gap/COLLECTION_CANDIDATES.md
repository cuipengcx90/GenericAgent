# 域名 gap 候选清单（Chrome 高频访问 × 报告/记忆语料零提及）

- 快照：`temp/domain_gap/2026-09-27.json`（源=tools/domain_gap.py，只读；Chrome 副本 10 profile / visits 33266 = web 32467 + 自造域 419 + 扩展 250 + 非 http 130）
- 语料：`memory` + `temp/autonomous_reports`（工具口径 395 文件 / 14680550B；见「边界」第 3 条）
- 判定：域名整串或去 www/sub 基名在语料中零命中；`K`=窗口内访问次数阈值
- 末列口径：域名 base 首段（如 trytrellis / tutti / herdr）在两目录全部可读文件（464 个，已排除本清单自身）里的大小写不敏感**子串**次数——短标签会有误命中（如 `7x`），仅供参照、不参与 gap 判定；公共后缀基名（eu.org/edu.kg/pages.dev）标 —

## 0. 下次任务怎么用（可直接调用）
```
python3 tools/domain_gap.py scan --k 20,50,100 --out temp/domain_gap/<日期>.json   # 复算（只读）
python3 tools/domain_gap.py selftest                                              # 11/11
```
提升为正式采集源的两步（**改采集面前须用户批准**，本清单只给候选）：
1. 先实调验证该域确有可用能力（AI 中转站按 `fofa_cli_proxy_hunter_sop` 的三段决策：`/v1/models` 有列表≠可用、实调才 usable）；
2. 通过后再写进对应源清单（`project_update_monitor/sources.json` 之类），并按该 SOP 先跑无 `--notify` 的 dry-run。

## 分档事实（K 敏感性）
| K | ≥K 域名数 | 零提及域名 | 零提及访问次数 | 占全 web 访问 |
|---|---|---|---|---|
| 20 | 137 | 49 | 1741 | 5.36% |
| 50 | 66 | 8 | 539 | 1.66% |
| 100 | 37 | 0 | 0 | 0.00% |

稳定性：K=20→50 时零提及 49→8，两档共有的 8 个（低档保留率 16%）⇒ `nested_monotone`。

## A. AI 中转 / 公益站（New API 面板、福利站、Grok 镜像）（20 域 / 657 次）
消费方：`fofa_cli_proxy_hunter_sop`（OpenAI 兼容代理/New API 实调验证）

| 域名 | 访问次数 | 主时段(占比) | 页面标题（visits 记录原文） | base 首段子串次数 |
|---|---|---|---|---|
| api.fengwind.com | 74 | afternoon(55%) | Login - Fengwind API | 0 |
| api-welfalre.fengwind.com | 50 | morning(52%) | Fengwind API 福利站 | 0 |
| newapi.imagic.eu.org | 45 | morning(53%) | iMeagicAPI | — |
| wududu.edu.kg | 39 | morning(64%) | dudu公益站 | — |
| new-api.abrdns.com | 35 | morning(51%) | New API | 0 |
| newapi.zmoon.top | 35 | morning(54%) | 403 Forbidden | 0 |
| ai.huan666.de | 33 | afternoon(61%) | Huan API | 0 |
| grok.868986.xyz | 33 | afternoon(100%) | Grok | 0 |
| hlwy.org | 33 | afternoon(64%) | 翰林文苑 API | 0 |
| freeapi.dgbmc.top | 31 | morning(100%) | DGB 公益站 | 0 |
| api.zzzcoding.org | 30 | evening(100%) | API 密钥 - Sub2API | 0 |
| welfare.0xpsyche.me | 29 | evening(38%) | welfare.0xpsyche.me | 0 |
| ai.hybgzs.com | 26 | morning(50%) | 黑与白公益站 | 0 |
| www.mofas.one | 26 | afternoon(92%) | 魔方公益站 | 0 |
| 7x.hk | 24 | morning(88%) | 七倍算力 | 753 |
| api.find2ai.com | 24 | afternoon(100%) | 渠道状态 - find2ai | 0 |
| taotoken.net | 24 | evening(100%) | 首页 · Taotoken | 0 |
| platform.agnes-ai.cn | 23 | afternoon(100%) | Agnes | 0 |
| newapi.qwqtao.one | 21 | morning(71%) | Tao API | 0 |
| agnes-ai.cn | 22 | afternoon(100%) | Agnes Video V2.0 - Agnes-Ai Docs | 0 |

## B. 网盘 / IT 资源 / 教程站（夸克网盘搜索、课程站）（13 域 / 358 次）
消费方：`birdiesearch_monitor_sop`·`haisou_monitor_sop`（扩源，须先验证签名/额度）

| 域名 | 访问次数 | 主时段(占比) | 页面标题（visits 记录原文） | base 首段子串次数 |
|---|---|---|---|---|
| youkeit.xyz | 48 | evening(52%) | 优课it__全网最大.最专业的IT资源教程分享平台！ | 0 |
| zyscj.com | 37 | afternoon(100%) | 🔗网盘资源合集 | 资源收藏夹 | 0 |
| www.xianyuziyuan.com | 36 | evening(83%) | 课程中心-咸鱼资源 | 0 |
| aipanso.com | 30 | afternoon(73%) | 爱盘搜 - 夸克网盘搜索神器 | 0 |
| www.itxbzxw.com | 28 | morning(64%) | 小白自学网-国内最大的IT教程资源分享站 | 0 |
| www.xiaoyou.love | 26 | afternoon(100%) | 课程介绍 | 小由和小迪 | 0 |
| meow.tg | 22 | afternoon(100%) | 搜索 | TG喵 | 0 |
| kebaiwan.net | 21 | afternoon(71%) | 课百万-汇聚全网IT资源-每日更新 | 0 |
| www.fwfly.com | 21 | afternoon(100%) | 云盘搜索-薇飞导航 | 0 |
| itjc8.com | 20 | afternoon(70%) | IT教程吧 - 程序员的新大陆 | 更新最快的IT资源网！ | 0 |
| www.itkenet.com | 20 | afternoon(80%) | IT教程资源下载 – IT知识学习平台 | IT课堂网-全网资源免费下载，每天更新，不加密 | 0 |
| www.ukoou.com | 20 | evening(65%) | AIGC相关教程_AIGC相关资源 - 优库IT资源网 | 0 |
| adzhp.cc | 29 | afternoon(100%) | 爱达杂货铺-安全中心 | 加载中 | 0 |

## C. Agent / 多 Agent 工具与文档站（8 域 / 372 次）
消费方：`project_update_monitor/sources.json`（已有条目勿重复）+`herdr_orch_sop`·`trellis_integration_sop`

| 域名 | 访问次数 | 主时段(占比) | 页面标题（visits 记录原文） | base 首段子串次数 |
|---|---|---|---|---|
| docs.trytrellis.app | 83 | afternoon(45%) | trellis-meta - Trellis Doc | 4 |
| tutti.sh | 80 | afternoon(86%) | Tutti · VM — 多人多 Agent 实时协作空间 | 18 |
| herdr.dev | 56 | evening(95%) | Herdr: one terminal for the whole herd | 501 |
| new.sharedchat.cc | 40 | afternoon(55%) | Vibe Code 控制台 | 0 |
| www.moticlaw.com | 34 | evening(100%) | MotiClaw 官网 - 本地 AI 伙伴与智能体控制平台 | 0 |
| dg-ai-notes.pages.dev | 33 | evening(97%) | M01 · 第1章：开篇 —— 为什么 Pi-Agent 值得你花时间 | Pi Agent Book | — |
| pi-docs.aiuo.net | 23 | morning(70%) | PI-Desktop 文档 | 本地优先 AI 编程代理的文档系统 | 0 |
| www.onorca.dev | 23 | afternoon(61%) | Orca — The most powerful Agent Development Environment (ADE) | 0 |

## D. 工作与其他（无现成消费方）（8 域 / 354 次）
消费方：仅记录

| 域名 | 访问次数 | 主时段(占比) | 页面标题（visits 记录原文） | base 首段子串次数 |
|---|---|---|---|---|
| routerpark.com | 67 | morning(69%) | 社区论坛 - 分享与交流 | 0 |
| web4.52pokemon.cc | 65 | morning(49%) | 宝可梦免费加速 | 0 |
| open.qmai.com | 64 | morning(100%) | 文档中心 - 企迈技术服务平台 | 47 |
| www.icloud.com.cn | 43 | afternoon(100%) | 照片 - 图库 - iCloud 照片 | 13 |
| www.mintimate.cc | 33 | afternoon(100%) | 配置教程 | oh-my-rime输入法 | 0 |
| cn.bing.com | 32 | afternoon(53%) | site:reddit.com/r/applehelp Apple App Store chargeback bank dispute refund account disabled - 搜索 | 3 |
| haowallpaper.com | 27 | afternoon(100%) | 免费高清手机壁纸-海量背景图片-精美的壁纸网站「哲风壁纸」 | 0 |
| rp.mockplus.cn | 23 | afternoon(96%) | 运维面板 - 0715 - 摹客RP | 0 |

## 边界（勿过度解读）
1. **域名级零提及 ≠ 项目级零提及**：末列 herdr 501 / tutti 18 / trytrellis 4 次，说明这些项目本身进过报告（裸词口径另计：herdr 501 / tutti 18 / trellis 134 / orca 29，`onorca` 作为站点标签 0 次 ⇒ 读末列须注意「标签写法」差异），**只是从没记录过这些域名**。
2. **三个候选其实已在 `project_update_monitor/sources.json`**：`docs.trytrellis.app`=trellis 的 changelog_url、`www.onorca.dev`↔orca(stablyai/orca)、`tutti.sh`↔tutti(tutti-os/tutti) ⇒ **勿重复添源**，属「本地使用证据从未入库」而非「未监控」。
3. 语料面只含 `memory` 与 `temp/autonomous_reports`；配置文件（sources.json 等）里的域名仍会落进 gap ⇒ 本清单是「未被**报告/记忆**消费」，非「未被任何配置引用」。末列计数文件集比工具语料更宽（含工具按扩展名/体积排除的文件），仅供参照。
4. 数值绑 2026-09-27 快照，数据源是**两份节奏不同的冻结副本**：web 主体＝R415 每日快照 `temp/chrome_snapshots/2026-09-25/History`（Default profile），另加 R437 于 09-26 冻结的 9 个非 Default 副本 `temp/chrome_profiles/2026-09-26/*/History` ⇒ 复算须重跑第 0 节命令，勿引用本文旧数。
5. K=100 时零提及为 0 ⇒ 结论只在 K=20/50 档成立，禁外推为「全部高频域」。
