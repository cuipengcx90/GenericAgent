# Launchpad 文件夹空壳排障 SOP

## 适用
macOS Launchpad 自定义文件夹：标题在、数据库里也有子项，但打开后中心无 App 图标（空壳）。交接文档「写库能建分类但打不开/用不了」即此症状。

## 根因（已实测坐实）
文件夹子项的 `items.ordering` 出现**重复值或非连续杂散值**（如 `0,0,1` 或混入 `15/47/274` 等被系统触发器写入的大值）时，Dock/Launchpad 渲染引擎**拒绝渲染该文件夹内容**——SQL 能写进、DB 查询也正常，但界面只剩标题。修成每文件夹连续 `0..N-1` 后立即恢复满渲染。

## 突破口（为什么之前一直失败，怎么才成功）
1. **别在 Dock 运行时直写 DB**：Dock 活着时其 items 更新触发器会覆盖直写结果。必须先 `launchctl bootout` 停 Dock，写库时 `update dbinfo set value=1 where key='ignore_items_update_triggers'` 抑制触发器，再 `bootstrap`+`kickstart` 重启。
2. **真正的坎是 ordering 连续性**，不是“写不进”。早期误以为是注入失败，反复重写都没用；逐字段对照可正常打开的**系统「其他」文件夹**才发现差异只在 ordering。修复 = 每个文件夹独立、按 `rowid` 保序重排为 `0..N-1`，**不要跨文件夹/跨页合并排序**。
3. **验收方法本身是最大陷阱**：App 图标没有文字，OCR 读不到内部 App 名 → 会把“其实满渲染”误判成“空”。正确做法：截图后用 `cv2.Canny`+连通域计数比图标数量（**空壳≈6，满渲染≈100+**），并拿系统「其他」文件夹做对照基线。只靠 OCR 或 SQL 查询都会误判。

## 执行步骤
1. 备份 Launchpad SQLite DB（`$(getconf DARWIN_USER_DIR)com.apple.dock.launchpad/db/db`）。
2. `launchctl bootout gui/$(id -u)/com.apple.Dock.agent` 停 Dock，确认 `pgrep -x Dock` 为空。
3. 连接 DB 用 **autocommit 模式**（`isolation_level=None`）：先 `update dbinfo` 抑制触发器 → 对每个命名文件夹按 `rowid` 升序写连续 `ordering=0..N-1`。
4. `PRAGMA wal_checkpoint`、`PRAGMA integrity_check` 应为 ok，且校验每文件夹 ordering 恰为 `0..N-1`、无残留异常。
5. `bootstrap`+`kickstart` 重启 Dock，睡几秒。
6. 视觉验收：`open -a Launchpad` → OCR 确认网格在前台(标题命中≥3) → 点开≥2个不同文件夹 → Canny 连通域计数确认满渲染。

## 坑与边界
- **事务报 “cannot start a transaction within a transaction”**：因先前 `UPDATE` 已隐式开事务，又显式 `BEGIN`。解法：用 autocommit 模式手动控制，或先 commit，别在未结事务内重复 `BEGIN`。
- **本机 GUI 翻页不可靠**：滚轮/左拖手势/方向键都无法稳定切 Launchpad 分页，别依赖翻页做多页验收；ordering 修复是每文件夹独立、与页码无关的确定性操作，抽查同页两个不同文件夹即可代表全体。
- **应用总数核对**：Launchpad DB 收录数 = `select count(*) from apps`（本机 219）。散落未分类的通常是 macOS 自带系统 App（App Store/Safari/邮件/日历等），交接文档一般只分类第三方工具，属正常非丢失。App 归属查询：`apps.item_id` 关联 `items.rowid`，文件夹标题在 `groups.title`（app→folderpage(type3)→folder(type2,有title)）。

## 实测补充：全新 bootstrap 后的渲染确认
在已完成 ordering 修复并停止/重启 Dock 后，必须等待 Dock 完成全新 bootstrap，再做 UI 验收；仅看到目录标题或 SQL 关系不能算成功。2026-09-12 实测：全新 bootstrap 后以下目录均可打开并显示真实 App：
- AI原生IDE：Antigravity、Kiro、Qoder
- 终端：Termius、Netcat、Warp、Ghostty、iTerm
- 数据库工具：DBeaver、Another Redis Desktop Manager、Navicat Premium、DataGrip
- Git/代码工具：Fork、Sabun、SnippetsLab
- AI聊天客户端：Claude、ChatGPT
验收截图保存在任务产物 `evidence/latest_ui/probe_folder_{open,terminal,db,git,aichat}.png`；已通过飞书私信发送文字说明和 5 张原图，接口返回 code 0。截图必须是在文件夹已打开、内部 App 可见时抓取，不能只发 Launchpad 外层封面或静态总览。

## 最终验收与用户交付（不可省略）
完整闭环的固定顺序是：**①按映射完成分组 → ②验证分组可用 → ③逐页截图并直接发给用户确认**；只完成写库、SQL 校验或口头汇报都不算交付完成。

1. **分组完成**：按交接映射创建/填充文件夹，并修正每个文件夹的 `ordering=0..N-1`。
2. **验证可用**：先做 DB 完整性、文件夹归属和 ordering 连续性校验；重启 Dock 后打开 Launchpad，抽查不同文件夹确实展开并有图标渲染（Canny 连通域对照系统「其他」文件夹）。
3. **逐页截图验收**：确认截图画面是真正前台的 Launchpad（不能是桌面或其他窗口）；切到每一个分页后各保存一张原图。每次翻页后与前页做图像差异检查，重复图或桌面图一律作废并重抓。分页控制若经视觉识别得到的是 Retina 物理像素，发送 Quartz 点击前必须换算为逻辑坐标，避免点空白处退出 Launchpad。
4. **直接发送，不只报路径**：在最终回复中把每页截图以附件/内联图片直接发给用户，并按“第 1 页、第 2 页……”标注；可附总览拼图，但不能以本地文件路径代替图片。用户看过图片才是最终人工验收点。

## 逐目录截图验收的三个实测坑（2026-09-12，都导致过大量重试）
1. **翻页后必须实时 OCR 确认前台再取坐标（最隐蔽）**：用页点循环遍历分页后，脚本常从「之前保存的某页截图」里取图标坐标，而此刻真实前台已停在别的分页 → 在错误页用同一坐标打开成了别的目录（如本该开 IDE 却开了下载工具）。根因不是坐标偏移，是「取坐标的截图 ≠ 当前前台页」。铁律：每次点击页点/翻页后，先重新抓屏 OCR 确认当前页标题，再从**这张刚抓的图**上取坐标操作，禁用任何历史截图坐标。
2. **OCR 判空不可靠，禁单独用它判「目录为空」**：`?` 等符号归一化后变空字符串，会被 `空 in 任意文本` 误判为命中所有目录；`AI` 常被识别成 `Al`。验收匹配器必须：①排除归一化后为空的文本；②双条件通过才算真实——「目录名命中」+「至少一个计划内预期 App 名命中」，缺任一即判 invalid 并人工复核原图，不能因 OCR 噪声把非空目录误报成空。
3. **飞书发图接口签名**：`reflect_scripts.reflect_common` 里发图是 `send_feishu_image(img_path, open_id=FEISHU_USER)`，**只收路径、无 title 参数**；发文本是 `notify_feishu(text)`（不是 `send_feishu_text`）。图片消息本身不带标题，需要图注就先 `notify_feishu('页-序号「目录名」')` 再 `send_feishu_image(path)`，成功判据是返回 `code==0`。逐张落盘回执，末尾统计 `成功数/总数`。
4. **独占页布局差异**：`IDE / 编辑器` 单独占一整页（不与其它目录同页共存），其目录标签在 Retina 物理像素 y≈426，与普通网格页偏移不同；不能沿用其它页的固定 −112 偏移，须按当前页实测标签中心。
## 可复用实施 Runbook：从“空目录”到可渲染（2026-09-12 实测）

> 此处记录的是**中间到底怎样做**，用于后续增量调整或空壳复发时照做。不要改成“先在 UI 拖拽试试”：原生 Launchpad 拖拽受分页、坐标、辅助功能权限影响而脆弱；本次稳定解是控制 Dock 生命周期后，以 DB 三层关系做确定性修复，再用真实 UI 验收。

### 1. 先建立事实，不在错误 DB 上操作

```bash
DB="$(getconf DARWIN_USER_DIR)com.apple.dock.launchpad/db/db"
sqlite3 "$DB" '.tables'
sqlite3 "$DB" '.schema items'
sqlite3 "$DB" '.schema groups'
sqlite3 "$DB" '.schema apps'
```

- **不要硬编码** `/var/folders/...`：每个用户会话的 `DARWIN_USER_DIR` 会变；必须由 `getconf` 实时计算。
- 本机已验证结构：`items` 是关系/排序表；`groups` 为有标题的文件夹；`apps` 是 App 元信息。字段名是 `item_id`，不是猜测的 `id` 或 `s.item_id`。
- 一个用户文件夹的可渲染链路必须完整：

```text
folder shell: items.rowid=F, type=2, flags=1 + groups.item_id=F, groups.title=目录名
    └─ inner page: items.rowid=P, parent_id=F, type=3, flags=0, ordering=0
          └─ app item: items.rowid=A, parent_id=P, type=4, flags=0, ordering=0..N-1
                └─ apps.item_id=A（标题、bundleid）
```

- **常见误解**：把 App 的 `parent_id` 直接指向文件夹 `F`，或只有 folder→page、没有 app→page；DB 仍能查出“好像归类了”，但 Launchpad 不渲染。

### 1.1 本次与前序“目录已建、软件不可见”的技术分界（不可凭模型名猜测）

- 从最终保留的 DB/UI 事实看，前序执行至少达到了“创建 folder 外壳、并把 App 记录迁移到某个目录相关层级”的阶段；但这**不等于**可证明其具体哪一条 SQL、哪一个字段或哪次 Dock 同步有错。没有该次执行的完整 SQL、事务日志和改前/改后 DB 快照，不能把根因断言为“必然是层级错”。
- 本次之所以能发现问题，不是因为先验知道 Launchpad 私有 schema，而是把“DB 显示已归类、真实 UI 却空”的矛盾当作未完成信号，逐层取证：①读取实际 schema/触发器；②对每一 folder/page/App 输出 `rowid,parent_id,type,flags,ordering`；③对比一个可渲染对象与空壳对象；④停 Dock 排除同步回写；⑤一次只修复三层关系和每页连续排序；⑥重新 bootstrap Dock 后真实展开目录。最终是“连续 `ordering` + 正确 F→P→A 链 + Dock 生命周期”组合满足后，UI 才恢复渲染。
- **绝对禁止的错误归因**：不能说“创建 folder 成功”就代表“软件已放入可用目录”；不能说“SQL 返回成功”就代表“UI 会显示”；也不能因使用者/模型名称不同就归因于模型强弱。根因应以可复现的 DB、Dock 与 UI 对照证据确认。
- 对以后接手其他工具/其他模型创建的分类：先保存原样证据并跑第 5 节决策表；若缺完整证据，只报告“UI 未渲染，待按三层关系、排序、Dock 同步逐项定位”，不追溯臆测具体操作者的错误。

### 2. 改前快照、变更集与停止条件

1. 用户只要求一个 App 归类时，只动该 App、源/目标目录；顶层目录重排、批量迁移、未知 App 归类均须先确认范围。
2. 备份 `db`、同目录可能存在的 `db-wal` 与 `db-shm` 三件套，记录时间、目标 folder/page/App rowid、标题和 bundleid。
3. 先查询而非直接更新；找不到唯一 App 或 folder/page，**停止**，不能凭名字模糊 `LIKE` 把 `云存储` 与 `CLI / 云存储小工具` 混为一谈。
4. 保留现有 `ordering` 的 App 查询顺序；若用户给了目录内明确顺序，以该顺序构造变更集，否则不得暗中改成字母序。

### 3. 安全写入的精确顺序

下面是已实际运行的修复脚本 `temp/repair_launchpad_folder_render.py` 的关键流程，后续应复用脚本/同等逻辑，不要手敲零散 SQL：

```python
DB = subprocess.check_output(['getconf', 'DARWIN_USER_DIR'], text=True).strip() \
     + 'com.apple.dock.launchpad/db/db'
uid = subprocess.check_output(['id', '-u'], text=True).strip()
svc = f'gui/{uid}/com.apple.Dock.agent'

# A. 先停 Dock；返回失败或 pgrep 仍能找到 Dock 都立即中止，绝不写库。
subprocess.run(['launchctl', 'bootout', svc], check=True)
assert subprocess.run(['pgrep', '-x', 'Dock'], capture_output=True).returncode != 0

# B. autocommit 连接，但显式 BEGIN IMMEDIATE；这样锁住一次修复窗口。
con = sqlite3.connect(DB, isolation_level=None)
c = con.cursor()
c.execute('BEGIN IMMEDIATE')
c.execute("UPDATE dbinfo SET value=1 WHERE key='ignore_items_update_triggers'")

# C. 对每个 folder 的每一个 inner page，先列出该 page 的真实 App item；
#    仅按该 page 自己的 (ordering,rowid) 保序，禁止跨目录/跨页混排。
apps = c.execute('''SELECT i.rowid FROM items i JOIN apps a ON a.item_id=i.rowid
                    WHERE i.parent_id=? ORDER BY i.ordering,i.rowid''', (P,)).fetchall()
c.execute('UPDATE items SET type=2,flags=1 WHERE rowid=?', (F,))
c.execute('UPDATE items SET parent_id=?,type=3,flags=0,ordering=0 WHERE rowid=?', (F,P))
for order, (app_id,) in enumerate(apps):
    c.execute('UPDATE items SET parent_id=?,type=4,flags=0,ordering=? WHERE rowid=?',
              (P,order,app_id))

# D. 同一事务内校验，再恢复触发器、提交和 checkpoint。
got = [r[0] for r in c.execute(
    'SELECT ordering FROM items WHERE parent_id=? AND type=4 ORDER BY ordering', (P,))]
assert got == list(range(len(apps)))
c.execute("UPDATE dbinfo SET value=0 WHERE key='ignore_items_update_triggers'")
c.execute('COMMIT')
c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
```

**为什么必须这样做：**

- `Dock` 活着时写入会被其 items 更新触发器回写；此前“写进去又消失/排序变成 250、276”等问题由此产生。
- `ignore_items_update_triggers=1` 只应覆盖当前受控事务；必须在 `COMMIT` 前恢复为 `0`，避免把 DB 留在异常同步状态。
- `isolation_level=None` + 显式 `BEGIN IMMEDIATE` 防止“前一个 UPDATE 已隐式开启事务，再 BEGIN”触发 `cannot start a transaction within a transaction`。
- 目录内绝对不允许 `0,0,1`、`1,2,3,250` 或跨页共享序号。渲染所需的是**每一个 P 独立**的 `0..N-1`。
- 异常必须 `ROLLBACK`；无论修复是否成功，都要保证 Dock 最终恢复启动，不能留下无 Dock 的桌面。

### 4. Dock 恢复、数据库校验与 UI 判定

```python
# 事务结束/连接关闭后恢复，不可在 DB 事务内启动 Dock
subprocess.run(['launchctl','bootstrap',f'gui/{uid}',
                '/System/Library/LaunchAgents/com.apple.Dock.plist'], check=True)
subprocess.run(['launchctl','kickstart','-k',svc], check=True)
time.sleep(5)  # 等全新 bootstrap
```

1. DB 校验依次为：`PRAGMA integrity_check = ok`；目标 App 的 `apps.item_id → items(parent_id=P,type=4)`；每个受影响 page 的排序严格等于 `range(N)`。
2. 再打开真实 Launchpad，实际展开**每个受影响文件夹**。如果目录标题出现但里面无图标，DB 校验通过也判失败，回到第 1 节检查三层关系和第 3 节的排序/触发器步骤。
3. 视觉自动验收不能只靠 OCR：App 图标经常没有可读文字；以目录标题 + 至少一个预期 App OCR + 图标 Canny/连通域数量共同判定。实测空壳约 6 个边缘连通域、满渲染约 100+，但阈值必须以同屏系统“其他”目录为对照，不能硬编码为唯一成功标准。
4. 若 UI 操作失败：一次 F4 / 点击无效果后，先探测辅助功能授权、当前前台窗口和实际屏幕；翻页后必重新截图、确认当前页标题，再据该**新截图**取坐标。绝不复用上一页截图的坐标。

### 5. “现象 → 先查什么 → 修复什么”的诊断决策表

| 现象 | 首先取证 | 修复/停止动作 |
|---|---|---|
| DB 查不到 folder/page/app 链 | 输出 `items/groups/apps` schema 及目标 rowid 链，而不是猜 SQL 列名 | 补全/纠正 `F(type2)→P(type3)→A(type4)`；不做 UI 拖拽兜底 |
| 链完整但文件夹空壳 | 打印该 P 下 App 的 `ordering,type,flags,parent_id`；检查 Dock 是否曾运行 | 停 Dock，打开触发器抑制窗口，按 `(ordering,rowid)` 将该 P 重编为 `0..N-1`，重启 Dock |
| 排序写完又跳号/丢失 | `pgrep -x Dock`、`dbinfo.ignore_items_update_triggers`、WAL 文件状态 | 停止并恢复备份；只有 Dock 已停且事务受控时才重写 |
| SQL/截图显示“可能有内容”但无法确认 | 真实打开目标目录，截取目录内页，与系统“其他”目录同条件对比 | OCR + 图标视觉联合验证；不确定即 invalid，不能以静态总览对外交付 |
| 点开的不是目标目录 | 记录当前页 OCR、截图哈希和目录中心坐标 | 翻页后重新截屏重新定位，按物理页+精确目录名映射，不用名称子串 |
| 飞书回执成功但用户没看到 | 保存 `msg_type/image`、message_id、回执及用户反馈 | 仅称“服务端已接受”；改用用户可访问的发送通道/格式前先实测，不重复宣称已交付 |

## 当前分类基线、交付记录与后续调整（2026-09-12）

> **定位与可变性**：本节是 Launchpad 分类的唯一可维护基线；后续用户提出增删、归类或重排时，在本节更新“当前基线/变更记录”，不要把某次旧截图或旧计划当成永久真相。具体计划源为任务目录的 `launchpad_plan_preview.json`；用户给出的初版方案图为 `feishu_media/img_v3_0215f_d88.jpg`。本节记录的是工具读取/DB核验得到的事实，不代表未来状态。

### A. 本次执行与验收结论

1. **问题与修复路径**：最初出现“目录标题存在、App 已写入 DB、但 UI 打开为空”的空壳问题。已验证有效路径为：停 Dock → 抑制 items 更新触发器 → 写入正确的 folder→folder-page→app 层级 → 每文件夹处理内部排序 → 重启/全新 bootstrap Dock → 真实 Launchpad 打开目录确认图标渲染。不能用 SQL 关系、目录封面或单独 OCR 代替 UI 验收。
2. **UI 验收**：37 个用户分类目录均有真实“打开目录后”的截图；每页切换后重新抓取当前前台画面并做差异检查，避免旧页坐标误开其它目录。目录是否有内容应综合“标题 + 至少一个预期 App OCR 命中”与视觉图标连通域判定，不能以 OCR 判空。
3. **交付资料**：产物目录 `temp/evidence/latest_ui/final_delivery/`。其中含 37 个目录内页、4 个真实 Launchpad 页总览及 1 张全局缩略总图；`delivery_manifest.json` 记录文件存在性，`feishu_*_receipts.json` 记录发送回执。曾补发：第2页 9 张、第1/3页目录内页 29 张、4页总览+全局图 5 张。
4. **交付边界（必须如实汇报）**：飞书接口的 `code==0` 且 `msg_type=image` 只能证明服务端接受了图片消息，**不能证明用户客户端已可见**。最终完成口径必须是“图片已发送 + 用户已确认可见/无需更多截图”；用户已明确表示截图够了后，停止重复发送。
5. **能力边界复盘**：成功来自本机系统级闭环（SQLite/Dock 生命周期控制、GUI/视觉核验、故障恢复和飞书交付），不是仅生成分类建议或 SQL。若其它代码代理具备同等的 Mac、Dock 控制、屏幕验证及 IM 权限，也可以按此 SOP 完成；不可把未完成简单归因为模型能力差。

### B. 当前分类快照（实测）

- 用户目标分类：**37 个目录**；初版逻辑方案为 3 个业务页，显式计划 **152 个 App**。
- 当前 Launchpad 用户目录内实测：**178 个 App**；另外有系统“游戏/其他”等非用户分类目录，不计入上述 37 类。
- 当前物理视觉布局：4 页——综合开发与 AI（20 目录）、办公与沟通（8）、`IDE / 编辑器` 独占页（1）、工具与系统（9）。`IDE / 编辑器` 被 Launchpad 自动排成独占页是布局差异，不是分类缺失。
- 与用户初版图的关系：**37 类主框架一致，不是逐项冻结的一模一样**。初版“待确认”中的多数 App 已按用途细分；仍待用户决定的为 `CORE Keygen`、`danbo`、`LiveAgent`。当前另纳入文档/笔记的 Pages、Numbers、Keynote，以及 Claude Code 生态中的 Skill Recorder (Source) 等，故实测总数高于初版显式清单。

#### 目录顺序（当前逻辑基线）

1. **开发与 AI（20）**：IDE / 编辑器；AI 原生 IDE；终端；数据库工具；API 调试；Git / 代码工具；容器 / 虚拟化开发；建模 / 设计工具；JVM 诊断；CLI / 云存储小工具；环境 / 包管理；小程序开发；AI 聊天客户端；AI Agent / CLI 壳；桌面 AI Agent 平台；Claude Code 生态工具；多 Agent 编排 / 协作；Agent 开发辅助；AI 设计协作；AI 语音。
2. **办公与沟通（8）**：文档 / 笔记；邮件 / 团队协作；云存储；翻译；内容创作；时间管理；即时通讯；浏览器。
3. **工具与系统（9）**：下载工具；媒体播放；截图 / 剪贴板；系统清理 / 监控；窗口 / 菜单栏管理；虚拟化 / 模拟器；网络 / 代理 / 远程；安全；待确认。

### C. 排序评价与调整准则

1. 当前顶层是**按用途与工作上下文分段的人工排序**，不是按 App/目录字母序，也不是按最近使用。开发区整体遵循“编码→运行/调试→交付与专项工具→AI 工具链”，办公区与系统区分离；对开发者总体可用，但不是严格的“高频优先”布局。
2. 推荐的开发者高频优先顺序为：`IDE / 编辑器 → AI 原生 IDE → 终端 → Git / 代码工具 → 数据库工具 → API 调试 → 容器 / 虚拟化开发 → 环境 / 包管理 → CLI / 云存储小工具 → 建模 / 设计工具 → JVM 诊断 → 小程序开发`，之后再放 AI 工具链。文件夹内 App 应按“个人使用频率 → 同类备选 → 实验/低频”排序，不按字母序。
3. 当前实测有 8 个目录的 DB 内部 `ordering` 仍有跳号：IDE / 编辑器、AI Agent / CLI 壳、桌面 AI Agent 平台、Claude Code 生态工具、多 Agent 编排 / 协作、文档 / 笔记、截图 / 剪贴板、网络 / 代理 / 远程。因此不可宣称当前已是严格、可维护的规则排序；任何重排时应同时修正全部受影响目录为连续 `0..N-1`，再做完整 UI 复验。

### D. 后续调整的固定流程

1. **先确认意图**：明确是新增/移出/改归属/顶层重排/目录内按频率排序；对未知用途 App 不擅自归类，保留在“待确认”。
2. **建立变更集**：从当前 DB 和本节基线读取，而非盲用旧截图；列出受影响目录、目标顺序、App 名称/Bundle ID 与预期总数，先让用户确认任何大范围重排。
3. **执行前备份，停 Dock 后修改**：遵循本 SOP 第14–20行的 DB/服务生命周期要求；禁止 Dock 运行时直接写库。
4. **完整性与 UI 验收**：检查 DB 完整性、App 归属、每个受影响目录的 `ordering=0..N-1`；重启 Dock 后打开受影响目录，确认真实图标渲染。多页截图时每次翻页均实时抓屏、确认前台、重新取坐标。
5. **汇报与资料归档**：说明“变更前→变更后、未确定项、实际物理页布局、验证方式”；更新本节的当前快照/变更记录。仅在用户要求或需要验收时发送截图；图片发送回执不替代用户可见确认。

### F. 失败复盘与复发防护（强制执行，2026-09-12）

> **本节优先级高于“看起来已经写入成功”的任何中间结果。** 本任务曾多次因把 DB、截图文件或接口回执误当成用户可用结果而错误宣布完成；今后任一红线触发，立即停止宣称完成，按对应恢复条件处理。

| 曾发生的失败 | 根因 / 禁止动作 | 允许继续或宣布完成的条件 |
|---|---|---|
| 文件夹标题存在、DB 中也有 App，但 UI 打开为空 | 只创建/修改了表面 parent 关系，或子项 `items.ordering` 重复、跳号、被 Dock 触发器改写；**禁止**以 SQL 查询到记录作为成功依据 | 停 Dock 后修正正确三层关系及受影响目录的连续 `0..N-1` 排序，重启 Dock；真实打开目录，看见 App 图标才算通过 |
| Dock 运行时写 DB，修改一会儿出现、一会儿消失 | Dock/Launchpad 同步与触发器会覆盖外部写入；**禁止**在 Dock 存活时直接批量写库后即验证 | 写前备份、停 Dock、按 SOP 生命周期修改、启动全新 Dock bootstrap 后复验 |
| 在 Launchpad 反复按 F4 / 旧坐标点击，未解决却不断截图验证 | 快捷键和辅助功能控制未生效，或翻页后使用历史截图坐标；**连续一次操作无效果即不得原样重复**，先探测前台/UI/权限 | 重新抓取当前物理屏幕，OCR/视觉确认前台页与目标，再从当前图取坐标；若权限阻断，切换到可用入口，不把失败点击写成进展 |
| 把目录封面、静态 HTML、未展开页面截图发给用户 | 截图不是“目录内 App 可见”的验收画面；**禁止**以封面或生成的 mockup 代替真实目录内页 | 每个目录须真实展开截图；验收至少满足目录名 + 一个预期 App 可见/OCR 命中 + 图标视觉存在，异常人工复核 |
| OCR 误判目录为空或误把错误目录判为正确 | 归一化后空字符串、`AI`/`Al` 等噪声，以及只做文字匹配；**禁止**单独用 OCR 判空或只凭一个弱关键词通过 | 过滤空归一化文本；同时核验目录名、至少一个预期 App、视觉图标连通域；任一失败即标 invalid，不得自动报成功 |
| 截图只覆盖部分页/部分目录，或云存储与 CLI/云存储串图 | 历史截图和文件名模糊匹配造成映射错位；**禁止**根据名字子串猜目录或先说“全量已发”后再找缺口 | 用“物理页号 + 目录固定映射”逐项建 manifest；逐项检查存在性、真实内容与发送状态，统计必须等于计划数量 |
| 飞书 `code=0` 后就宣称用户已看到图片 | 接口只说明服务端接受消息，不能证明客户端显示；**禁止**把回执成功等同于用户验收成功 | 只汇报“已发送、回执成功”；任务交付完成需用户确认可见或用户明确说“截图够了/无需更多” |
| 遇到交付缺口时先总结“已闭环” | 把局部成功覆盖未完成项；**禁止**批量操作结束后不逐项核对就发布完成结论 | 先持久化成功部分和失败清单；按 manifest 对账（目录内页、每页总览、全局图、用户要求的文字回答），未清零不得称全部完成 |
| 用户要求“不擅自处理”的未知 App 被自动归类 | 把猜测当事实；**禁止**对用途/安全属性未确认的 App 自作分类或删除 | 保留“待确认”，明确列出 App 名；仅在用户确认用途或给出明确规则后移动 |

**执行闸门（不可跳过）**：

1. **改动前**：读取本 SOP 当前基线；备份 DB；列出变更集和不可确定项。涉及顶层重排或批量归类，先获得用户确认。
2. **修改后、截图前**：检查受影响目录的关系和连续排序；重新启动 Dock；对每个受影响目录真实展开验证。任何一个空壳，回到根因排查，不发验收图。
3. **交付前**：对 manifest 做“计划数 = 产物数 = 已核验数”的对账；区分“已发送回执”与“用户已看到”。
4. **最终汇报**：必须明确写出已完成、未完成、用户待决定项、实际物理布局差异；绝不把“待用户验收”表述成“用户已验收”。

### E. 变更记录

- **2026-09-12**：完成从空壳目录到可渲染分类的修复与 UI/截图验收；建立本节作为可持续调整基线。未在本次自动执行“开发者高频优先”重排，也未擅自处理 CORE Keygen、danbo、LiveAgent。
- **2026-09-12（实施细节补充）**：新增“可复用实施 Runbook”：动态定位 DB、F→P→A 三层模型、备份/停 Dock/事务与触发器隔离、连续排序重建、Dock bootstrap、DB+真实 UI 双验收，以及现象→取证→修复决策表。后续不允许只凭抽象原则或临时手写 SQL 重试。
- **2026-09-29**：本 SOP 已全量镜像到 OB 知识库（vault `~/develop/obsidian-note`）=`60 Wiki/topics/macos-launchpad-folder-organize.md`，并在 `60 Wiki/INDEX.md` 登记；vault 侧本地 git 提交 75328b5b（vault 无远端）。给非 GA 的 agent 看时以 vault 版为准，数值时效见该页尾部说明。

