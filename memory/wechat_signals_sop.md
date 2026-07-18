# WeChat Signals SOP

适用: 每日00:01按 `groups.json` 当前配置的全部微信群抓取消息，分桶分类，生成Obsidian日报+飞书推送。
周报: 每周一09:00自动聚合上周日报生成周报（`sche_tasks/wechat_weekly.json`）。
环境配置见 L2 `[WeChat Signals]`。

## 执行架构 (解密DB方案 + subagent并行模式)
- **调度入口**: `sche_tasks/wechat_signals.json`(日报) / `wechat_weekly.json`(周报) → scheduler.py → GA主agent
- **数据源**: wechat-decrypt解密后的SQLite DB (纯sqlite3, 无需sqlcipher)
- **执行方式**: 主agent调度:
  1. 翻群同步: `temp/wechat_auto_sync.py` (AppleScript切换群聊触发Mac微信同步)
  2. 解密DB: `cd temp/wechat-decrypt && python decrypt.py` (需sudo, 每次翻群后重新解密)
  3. 提取+日报: `temp/wechat_daily_auto.py --date YYYY-MM-DD` (解密DB→JSON+Obsidian MD)
  4. AI摘要: 主agent对提取结果进行LLM分类汇总 (大群用subagent并行)
- **一键管道**: `temp/wechat_pipeline.py` (翻群→解密→提取→日报)
- **Key管理**: GA通过 mykey.py 内置LLM配置，subagent继承主agent环境
- **⚠️ 废弃**: chatlog API方案已废弃; `wechat_classify.py` / `wechat_fetch.py` 已归档至 `temp/archive/`

## 方案B: OCR截图链路 (不碰微信进程/不解密/无sudo, 单群)

适用: 不想读DB/不想sudo解密时的单群日报。纯GUI截图+Vision OCR, 确定性HTML渲染出图。
- **编排脚本**: `temp/run_wechat_ocr_daily.py --group "群名" --date YYYY-MM-DD [--skip-collect] [--outdir DIR]`
  串起三步: collector → normalize → poster --chat。**报告默认按天落 obsidian vault**: 不传`--outdir`时自动输出到 `/Users/cuip/develop/obsidian-note/微信日报/<date>/`(常量 `OBSIDIAN_VAULT`/`REPORT_SUBDIR`)。中间产物 chat_clean 固定留 `wechat_ocr_test/` 不污染 vault。
- **链路**:
  1. `wechat_ocr_test/collector.py`(GUI: activate微信→定位聊天区→截屏+CGEvent滚动→批量Vision OCR→相邻帧去重拼接) → `transcript.txt`(OCR平铺行)
  2. `wechat_report_llm.py normalize transcript.txt 群名 chat_clean.txt` → `chat_clean_{群}_{date}.txt`(标准 `[HH:MM] 昵称:内容`)
  3. `wechat_poster.py --chat chat_clean --group 群 --date date --outdir DIR` → `report_{群}.png`(海报) + `report_{群}.md`(同名md, 便检索)。DIR默认=vault按天目录(见上)。
- **海报本质**: LLM抽取/分类成JSON→套HTML模板→Chrome(selenium)整页截图。**确定性排版, 非gpt-image图像生成**, 文字可控可复现。md与PNG同步产出。
- **定时任务**: `sche_tasks/wechat_cc_daily_13h.json`(13:00 daily, GA自带scheduler按目录扫描*.json, 非OS crontab)。窗口=昨13:00→今13:00(`--win-start-hour 13 --win-days 1`)。prompt含3群名+逐群命令(带`--group/--date/--win-start-hour/--win-days`, **不带`--outdir`走vault按天默认**)。⚠️旧记录提的 `wechat_daily_18h.json`/`_18h` 已废弃(改13点档)。

### 方案B关键坑 (实测)
1. **poster --chat内部已跑extract→organize→html→png→md**(make_poster), 编排脚本**别再单独跑extract/organize**, 否则重复LLM调用。
2. **collector支持 `--group "群名"` 自动切群**(2026-06实测修复)。切群走**左上角会话搜索框**(不是 `Cmd+F`! Cmd+F打开的是"搜一搜"搜全网公众号文章, 永远搜不到群): 点搜索框→剪贴板粘群名→截左列OCR→定位"群聊"分类区间(标题"群聊"到下一分类"服务号/联系人"之间)→`name_match`命中后点击进群。
   - **进群后必校验** `read_title` 标题栏, `name_match` 用最长公共子串(LCS)容错(覆盖群名≥60%或≥5连续字即命中), 抗OCR把群名开头读残(如"ccb Ag"→"J")。校验不过则拒绝盲采退出(exit 2), **严禁采错群当成功**。
   - 当前已在目标群则跳过搜索直接采。坐标铁律: bounds是逻辑坐标, OCR返回物理2x相对截图原点, 点击逻辑坐标=截图原点+OCR/2(Retina)。
3. **时间窗自停(②)**: collector采每帧OCR后用 `wechat_timewin.parse_marker_dt` 解析最早时间标记, 连续2帧早于窗口起点(默认昨13:00)即停采, 避免无脑滚到群创建之初。normalize侧再用 `filter_by_window` 二次过滤越界消息。
   - **⚠️两个真bug(2026-06群6边界case暴露,已修+离线验证)**: ①`parse_marker_dt`原只认相对标记("今天/昨天/星期X HH:MM"), 不认微信对一周前旧消息显示的**绝对日期**("5月8日 18:51"/"2025年5月8日 20:44")→旧消息密集群每帧fmin全None→自停判据永远凑不齐→翻满40帧停不下来。已加 `_ABS_DATE_RE` 解析绝对月日(年缺省按"今天"参考年推断+跨年修正:月日落在参考日未来则算去年)。②自停循环里 fmin=None 时 `older` 既不增也不减→冻结在1永远到不了2。已加 `crossed` 标志: 向上滚单调变旧, 一旦确认越界(older≥1)后续None帧也继承累加older, 回窗口内才重置。
   - **空群正常**: 若群最新消息本就早于窗口(如群最后活跃停在上月), 报告为空是正确结果, 非bug。验证手段: 用落盘的 `raw_ocr.json` 离线回放(每帧 `frame_min_dt` + 模拟自停循环), 不占GUI。
4. **⚠️编排脚本必须把参数透传给collector**: `run_wechat_ocr_daily.py` 调collector时务必带 `--group/--date/--win-start-hour/--win-days`(2026-06修复前漏传, 导致群校验+时间锚点全空转, 这是"误采个人聊天/错群"的代码级根因)。collector里 `switch_to_group` 旧Cmd+F版已删, 只保留四参会话搜索框版。
4. **耗时长**: 单群 collector滚动采集+三阶段LLM+Chrome截图约5-8min, 后台nohup跑勿用前台timeout。
5. 校验产物: PNG须>100KB、md非空, 否则视失败查 `run_daily.log` 干净行。
6. **⚠️锁屏前置检查(2026-06-29实测铁证)**: 方案B靠GUI(微信置前台→screencapture截聊天区→Vision OCR), **屏幕锁定时整链路第一步即废**——微信主窗口无法激活(`System Events` windowCount=0, `activate`/`open -a`/AX全无效), 截图截不到聊天内容。表象易误判为"切群失败/标题OCR空"。**定时任务启动前必先探 `Quartz.CGSSessionCopyCurrentDictionary().get('CGSSessionScreenIsLocked')`, ==True 则直接快速跳过+飞书通知说明原因, 不做任何激活重试**(本次未前置检查白耗14轮)。同理无人值守长跑场景, 屏保/锁屏会随时打断方案B, 优先用方案A(解密DB, 不依赖GUI)。

### 多群采集现状
- **方案B(OCR)仅支持单群**, 逐群靠 `--group` 搜索框切群循环, 20-30群×5-8min=2-3小时GUI自动化, 慢且脆(微信UI状态易扰), 不推荐规模化。
- **20-30群规模用方案A(解密DB)**: `wechat-decrypt/monitored_groups.json`(29群)已支持批量, 读DB无需逐群滚屏。OCR方案定位补充(无sudo/不碰进程的单群兜底)。

## 分类规则

### SaaS groups (category: saas)
| Bucket | Icon | Keywords |
|---|---|---|
| seo | 🔍 | Google, Ahrefs, keyword, ranking, SEO, content, backlink |
| pricing | 💰 | Stripe, Lemon Squeezy, Paddle, MRR, ARR, pricing, tax |
| infra | 🌐 | domain, Cloudflare, Vercel, deploy, CDN, DNS, hosting |
| growth | 📈 | ProductHunt, Twitter/X, growth, acquisition, conversion |
| risks | ⚠️ | ban, compliance, GDPR, risk, lawsuit, policy change |

### AI Coding groups (category: coding)
| Bucket | Icon | Keywords |
|---|---|---|
| tools | 🔧 | Claude, GPT, Gemini, SDK, API, release, model |
| workflow | 🔄 | CI/CD, spec, workflow, lint, git, testing |
| multimodel | 🤖 | agent, MCP, orchestration, multi-model, token |
| architecture | 📐 | architecture, design pattern, monorepo, microservice |
| pitfalls | ⚠️ | debug, performance, cost, OOM, timeout, pitfall |

### Signal strength
- **strong**: Actionable advice, verified experience, important tool release, concrete data
- **medium**: Valuable discussion, opinion clash, trend signal, tool recommendation
- **weak**: Casual mention, unverified rumor, personal preference

## Obsidian 输出格式

### 文件路径
- SaaS: `SaaS出海/Daily/{YYYY-MM-DD}-{slug}.md`
- Coding: `AICoding/Daily/{YYYY-MM-DD}-{slug}.md`
- slug: group name前10字符，特殊字符替换为`-`

### YAML frontmatter (mandatory)
```yaml
---
date: YYYY-MM-DD          # ⚠️ 不加引号，Dataview需裸日期
category: saas | coding
group: "full group name"
group_id: "xxxxx@chatroom"
signals_count: N
top3:
  - "signal 1"
  - "signal 2"
  - "signal 3"
tags: [tag1, tag2, tag3]
signal_strength:
  strong: N
  medium: N
  weak: N
---
```

### Wikilinks convention
- People: [[nickname]]  · Tools: [[Stripe]], [[Claude Code]]  · Concepts: [[SEO]], [[MCP]]

## 定时任务

- **`sche_tasks/wechat_daily_18h.json`** (enabled, 18:00 daily) — 18点滚动窗口日报+海报。窗口=**昨18:00→今18:00**(非自然日)。流程: 翻群同步→`wechat_pipeline.py --window --end-hour 18`(decrypt_db.py→extract --since/--until→summarize→poster)→飞书推送(附海报PNG)。date_str归属窗口终点(今天)。
- **`sche_tasks/wechat_daily_sqlcipher.json`** (disabled) — 旧版23:50整天sqlcipher直读模式,已停用,保留备查。
- **无人值守约束(关键)**: 解密key需`sudo`读进程内存提取→存`all_keys.json`(一次性,提取后可长期复用)。若`all_keys.json`缺失则定时任务无法自动解密,prompt内置Step4降级告警(推飞书说明原因,绝不静默)。微信须前台运行供AppleScript翻群。
- **窗口参数**: `wechat_pipeline.py --window [--end-hour N]`(默认18); 底层`wechat_daily_auto.py --since/--until 'YYYY-MM-DD HH:MM'`覆盖整天窗口。

## 手动触发 (自定义范围 + subagent并行)

用户可随时指定任意群+任意日期范围生成日报/周报，使用Map模式subagent并行处理。

### 口头触发语义规范
- **单日 + 日报**：生成 **1份日报**
  - 例：`帮我生成 XX群 2026-05-18 的日报`
- **日期范围 + 日报**：按天生成，**范围内每天各1份日报**
  - 例：`帮我生成 XX群 2026-05-12~2026-05-18 的日报` → 共7份日报
- **日期范围 + 周报/汇总/总结**：生成 **1份聚合报告**
  - 例：`帮我生成 XX群 2026-05-12~2026-05-18 的周报`
  - 例：`帮我生成 XX群 2026-05-12~2026-05-18 的汇总`
- **多个群 + 日期范围**：默认对每个群分别输出；若用户明确要求“合并”，则生成跨群聚合版
- **未说明类型**：默认按“汇总”理解，并先向用户复述确认

### 工具
- **`temp/wechat_pipeline.py`** — 一键管道(翻群→解密→提取→**海报PNG**→日报)。`--no-poster`跳过海报
- **`temp/wechat_poster.py`** — 海报stage: `wechat_messages_{date}.json` → 每个活跃群一张海报PNG。内部链: messages→chat.txt→report_llm两阶段→render_html→selenium精确截图。独立用法: `python temp/wechat_poster.py <messages.json> [--group 群名] [--outdir 目录]`
- **`temp/wechat_daily_auto.py`** — 解密DB提取+Obsidian日报生成
- **`temp/wechat_auto_sync.py`** — AppleScript自动翻群触发微信同步
- **`temp/wechat-decrypt/decrypt_db.py`** — 消息DB解密(无参运行,读all_keys.json,需先sudo提取key)
- **`temp/wechat-decrypt/monitored_groups.json`** — 监控群列表(29群)
- 用法: `python temp/wechat_pipeline.py --skip-sync --skip-decrypt --date 2026-05-19`

### Map模式流程 (主agent执行)
```
1. [Fetch阶段] 主agent调 code_run 执行 wechat_fetch_helper.py → 生成 temp/wechat_batch/{date}/{slug}.yaml + manifest.json
2. [分发阶段] 读 manifest.json，按群启动subagent (每群独立上下文，避免大群消息污染):
   - 每个subagent的 context.json 包含: 消息文件绝对路径、群信息、SOP分类规则、输出路径
   - 启动: python ../agentmain.py --task wc_{slug} --bg
   - input.txt: "读context.json，按wechat_signals_sop分类规则分析消息，生成Obsidian日报写入指定路径"
3. [监控阶段] 主agent轮询各subagent output.txt，等待 [ROUND END]
4. [汇总阶段] 收集所有日报路径，推送飞书通知
```

### 单群少量消息 → 主agent直接处理
群数≤3 或 单群消息<500条时，无需subagent，主agent inline处理更高效。

## 关键坑点 (实测验证)

1. **解密DB方案(当前)** — wechat-decrypt解密后的DB用普通sqlite3直接查询，无需sqlcipher
2. **DB结构**: message_*.db含Name2Id表(chatroom_id→usrName) + Msg_{MD5(usrName)}表(消息); contact.db含contact表(wxid→nick_name)
3. **消息格式**: 群聊消息content格式为"wxid_xxx:\n实际内容"，需解析分离sender和content
4. **翻群同步必要**: Mac微信不会主动同步所有群历史，必须AppleScript切换群聊触发同步，翻群后重新解密
5. **解密需sudo**: `cd temp/wechat-decrypt && sudo python decrypt.py` 读取进程内存获取key
6. **WAL flush延迟** — WeChat SQLite WAL模式，消息可能滞后。翻群后等几秒再解密
7. **link/appmsg内容是XML** — 用regex提取: `re.search(r'<title>(.*?)</title>', content)`
8. **chatlog API已废弃** — 不再使用chatlog HTTP API方案
9. **screencapture 必须绝对路径(方案B关键坑)** — 定时任务用 anaconda 环境，PATH 缺 `/usr/sbin`，`screencapture` 找不到 → collector 的 `capture()` 静默不生成截图 → OCR items=0 → 切群候选空/采集全失败。`collector.py` 的 `capture()` 已改用绝对路径 `/usr/sbin/screencapture`。排查信号: 截图文件 exists=False 而非内容空。
10. **微信窗口最小化导致"未找到窗口"(方案B关键坑)** — `AXMinimized=true`(最小化到Dock)时 `activate` 不解最小化，CGWindowList 看不到窗口 → collector 报"未找到微信窗口"。每群处理前用 osascript 设 `AXMinimized=false` 解最小化前置(System Events 能看到窗口数≠CGWindowList 可见)。
11. **飞书发海报PNG(方案B推送)** — reflect_common 无现成发图，notify_feishu 只发文本。发图流程: `im/v1/images`(multipart, image_type=message) 上传得 image_key → `im/v1/messages` 发 msg_type=image。token 复用 reflect_common._get_token()。reflect_common 已新增 upload_feishu_image()/send_feishu_image() helper。send_feishu_image 返回 dict code==0 即成功。
12. **NumPy 1.x/2.4.4 warmup 噪音(2026-07-02实测,非致命)** — `llmcore._llmlingua_warmup` 后台线程 import llmlingua→nltk/sklearn/pyarrow 链报 `ImportError: numpy.core.multiarray failed to import`,巨量 traceback 污染 stderr。**仅 LLMLingua 压缩预热失败,主链(OCR→normalize→LLM extract→html→png)完全不受影响**,报告正常产出。日志里看到该 traceback 别当故障追;判定单群成败只认 `[完成]` 标记或 poster 阶段真实报错。
13. **空群=时间窗过滤"保留0"是正常业务态,应跳过非失败(2026-07-02实测)** — collector 切群成功且采到消息,但 `[Stage0] 时间窗过滤 保留0 丢弃N`(该群最近活动早于窗口起点)时,chat_clean 为 0 字符,poster `[Stage1] 输入0字符` 后 exit 1。这不是 bug——按"空群跳过并在推送文案说明原因,绝不静默"处理,不重试。误当失败会白耗轮次。

## 海报风长图渲染 (实测踩坑, wechat_render_html.py)

目标: 把日报JSON渲染成海报风dashboard PNG。`_org_{slug}.json`(8版块) → HTML → Chrome截图。

1. **多列布局别用CSS `column-count`/`column-fill`** — headless需固定超高窗口(如40000)截全页，CSS多列会被超高窗口撑满(col1占满整窗高度反而更高/不balance)。正解: **渲染器层贪心分列** — 按预估高度把sections分配进N个flex列div(`.body{display:flex}` + `.col{flex:1}`)，高度自然可控，与窗口高无关。
2. **截图高度用selenium测真实`scrollHeight`，不要猜窗口高度+PIL裁白边** — 深色渐变背景会让"按底色比对裁白边"算法失效(每行都≠单一底色，整图判定有内容裁不掉)，反复猜窗口高度是死循环。正解:
   ```python
   drv.set_window_size(908,1000); drv.get('file://'+abspath); time.sleep(1.2)
   h=drv.execute_script("return document.documentElement.scrollHeight")
   drv.set_window_size(908,h+20); time.sleep(0.8); drv.save_screenshot(out)
   ```
   `--force-device-scale-factor=2`输出2x，CSS宽908→PNG宽1816。一次到位无需裁切。
3. **改渲染器后必验HTML非空** — `open(out,'w').write(render(doc))`若render抛异常会写0字节空文件，Chrome渲染全白页。**禁用`2>/dev/null`吞报错**；改完先`wc -c`确认HTML字节数合理再截图。
4. **验证两列均衡**: PIL按亮像素(`a.sum(2)>650`)分左右半区找各列最低card行，差值应≈0px；并确认底部40px有亮像素(footer存在)。
5. **超大PNG**: `Image.MAX_IMAGE_PIXELS=None`；诊断只采样中线单列省内存，勿整图reshape。
