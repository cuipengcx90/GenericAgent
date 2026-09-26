# R437 Chrome 多 profile 身份分流证据表

- 日期：2026-09-27 ｜ 类型：工程/分析 ｜ 状态：工具落地 + 真跑一次（只读）
- 工具：`tools/chrome_profile_identity.py`（747 行 / 37468 B，纯 stdlib，只读）
- 产物：`temp/chrome_identity/2026-09-27/report_frozen.json`（16706 B / 606 行）+ `report_frozen.md`（4734 B / 33 行）
- 复跑：见 §8

## 1. 要回答的问题

R423 消费 Chrome `Default` profile 时发现本机有多 profile；R430 量出「9 个非 Default profile 在
91 天窗口内 2788 次 http = 全机 32886 的 8.48%」，但它是一份**判决性对账**（会不会推翻 R423 的 7 条决策），
没有回答「**这 8.48% 到底是谁的**」。本表把 R430 的漏采面逐 profile 归因，并附身份面（登录态/扩展/引擎）
与来源出处，回答「哪些 profile 值得进采集面、哪些是历史残留」。

## 2. 方法与口径（只读）

- 数据面：Default 取 R415 快照 `temp/chrome_snapshots/2026-09-25/History`；其余 profile 取 R430 冻结副本
  `temp/chrome_profiles/2026-09-26/<profile>/History`。两侧均为**同一冻结时点的副本**，非实时库。
- 对账：逐行与 R430 基线 `temp/chrome_profiles/consume_2026-09-26.json` 比对（http 访问数/原始 visits/域数/首末访问等）。
  基线未提供的字段计为「不参与对账」，**不判失配**（避免假失配）。
- 只读保证：全程不开源库；`Local State` 取 sha256 前后一致（见 JSON `readonly_proof`）；
  `--copies-dir` 模式先跑副本 sha256 自校验，篡改/截断/缺失即 `rc=2` 拒绝出表（fail-closed）。
- 隐私：显示名只输出「首字符+***」；`gaia_id`/`user_name`/`gaia_name` **只输出布尔是否存在**，不落任何值。

## 3. 结果（9 行）

| profile | 显示名(脱敏) | 已登录 | 账号类型 | 最后活跃(profile.active_time) | 最后访问(History) | http访问 | 占本表% | 占全机% | 原始visits | 域数 | Top3 域名 | 扩展数 | 默认搜索引擎 | 来源 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Default | 崔*** | 是 | 非托管 | 2026-09-27 00:43 | 2026-09-25 22:18 | 30098 | 91.52% | 91.52% | 30403 | 865 | linux.do(3904)、op-test.smyze.cn(3132)、op.smyze.cn(2014) | 115 | — | `/Users/cuip/projects/GenericAgent/temp/chrome_snapshots/2026-09-25/History` |
| Profile 10 | t*** | 否 | 非托管 | 2026-04-04 16:03 | 2026-04-04 16:08 | 94 | 0.29% | 0.29% | 94 | 4 | chatgpt.com(64)、auth.openai.com(23)、api.cpass.cc(6) | 1 | — | `temp/chrome_profiles/2026-09-26/Profile 10/History` |
| Profile 11 | t*** | 否 | 非托管 | 2026-04-04 17:11 | 2026-04-04 17:11 | 68 | 0.21% | 0.21% | 68 | 4 | chatgpt.com(48)、auth.openai.com(9)、api.cpass.cc(8) | 1 | — | `temp/chrome_profiles/2026-09-26/Profile 11/History` |
| Profile 3 | 破*** | 是 | 非托管 | 2026-04-24 21:37 | 2026-04-24 21:39 | 276 | 0.84% | 0.84% | 276 | 21 | anyrouter.top(62)、ocn93f5d9olj.feishu.cn(39)、us-east-1.run.claw.cloud(29) | 2 | — | `temp/chrome_profiles/2026-09-26/Profile 3/History` |
| Profile 4 | 测*** | 否 | 非托管 | 2026-09-27 00:47 | 2026-09-21 11:42 | 1031 | 3.14% | 3.14% | 1106 | 38 | localhost(308)、agentrouter.org(139)、test-deploy-admin.smyze.cn(113) | 6 | — | `temp/chrome_profiles/2026-09-26/Profile 4/History` |
| Profile 6 | 果*** | 是 | 非托管 | 2026-04-04 17:10 | 2026-04-04 17:10 | 121 | 0.37% | 0.37% | 121 | 9 | accounts.google.com(28)、auth.openai.com(23)、myaccount.google.com(23) | 1 | — | `temp/chrome_profiles/2026-09-26/Profile 6/History` |
| Profile 7 | 玉*** | 是 | 非托管 | 2026-05-06 11:15 | 2026-04-05 16:32 | 177 | 0.54% | 0.54% | 177 | 23 | accounts.google.com(32)、chatgpt.com(30)、api.cpass.cc(25) | 1 | — | `temp/chrome_profiles/2026-09-26/Profile 7/History` |
| Profile 8 | t*** | 是 | 非托管 | 2026-04-04 16:01 | 2026-04-04 16:17 | 655 | 1.99% | 1.99% | 655 | 29 | chatgpt.com(135)、accounts.google.com(99)、myaccount.google.com(84) | 1 | — | `temp/chrome_profiles/2026-09-26/Profile 8/History` |
| Profile 9 | g*** | 否 | 非托管 | 2026-04-04 17:11 | 2026-04-04 16:08 | 366 | 1.11% | 1.11% | 366 | 13 | accounts.google.com(189)、chatgpt.com(57)、google.com(35) | 1 | — | `temp/chrome_profiles/2026-09-26/Profile 9/History` |

- 本表 http 合计：**32886** ｜ R430 全机基线：**32886** ｜ 逐行对账 mismatch：**0**（参与对账字段数 42；基线未提供的字段不计入，避免假失配）
- 附注：另有含 History 但不在 `info_cache` 的目录（无身份面，未入表）=['Guest Profile']

## 证据出处（逐字段 → 文件 → 现取命令）

| 字段 | 来源文件 | 现取命令 |
|---|---|---|
| 显示名/登录态/最后活跃/账号类型 | `/Users/cuip/Library/Application Support/Google/Chrome/Local State` → `profile.info_cache` | `python3 -c "import json;d=json.load(open('Local State'));print(list(d['profile']['info_cache']))"` |
| 默认搜索引擎 | `/Users/cuip/Library/Application Support/Google/Chrome/<profile>/Preferences` → `default_search_provider_data.template_url_data.short_name` | `python3 -c "import json;print(json.load(open('Preferences'))['default_search_provider_data']['template_url_data']['short_name'])"` |
| 扩展数 | `/Users/cuip/Library/Application Support/Google/Chrome/<profile>/Extensions/*/`（只列一级子目录名） | `ls "$CHROME_ROOT/Default/Extensions" \| wc -l` |
| http访问/域数/Top3/时间窗 | Default：`/Users/cuip/projects/GenericAgent/temp/chrome_snapshots/2026-09-25/History`；其余：`temp/chrome_profiles/2026-09-26/<profile>/History` | `python3 tools/chrome_profile_identity.py report`（内部走 `chrome_history_consume.load_visits`）；副本 sha256 复核 `python3 tools/chrome_profiles_consume.py verify` |
| 全机占比分母 | R430 基线 JSON 的 `combined_all_profiles.http_visits` = 32886 | 见上表末行 |

复跑：`python3 tools/chrome_profile_identity.py report --chrome-root "/Users/cuip/Library/Application Support/Google/Chrome" --copies-dir temp/chrome_profiles/2026-09-26 --snapshot /Users/cuip/projects/GenericAgent/temp/chrome_snapshots/2026-09-25 --out-md <OUT.md> --json <OUT.json>`

### 3.1 8.48% 漏采面归因（本表新增量）

非 Default 合计 2788 次 = 全机 32886 的 8.48%，逐 profile 拆解（占漏采面比例）：

| profile | http 访问 | 占漏采面 | 已登录 | 最后活跃(info_cache) | 最后访问(History) | 主要落点 |
|---|---|---|---|---|---|---|
| Profile 4 | 1031 | 36.98% | 否 | 2026-09-27 00:47 | 2026-09-21 11:42 | localhost(308)、agentrouter.org(139)、test-deploy-admin.smyze.cn(113) |
| Profile 8 | 655 | 23.49% | 是 | 2026-04-04 16:01 | 2026-04-04 16:17 | chatgpt.com(135)、accounts.google.com(99)、myaccount.google.com(84) |
| Profile 9 | 366 | 13.13% | 否 | 2026-04-04 17:11 | 2026-04-04 16:08 | accounts.google.com(189)、chatgpt.com(57)、google.com(35) |
| Profile 3 | 276 | 9.90% | 是 | 2026-04-24 21:37 | 2026-04-24 21:39 | anyrouter.top(62)、ocn93f5d9olj.feishu.cn(39)、us-east-1.run.claw.cloud(29) |
| Profile 7 | 177 | 6.35% | 是 | 2026-05-06 11:15 | 2026-04-05 16:32 | accounts.google.com(32)、chatgpt.com(30)、api.cpass.cc(25) |
| Profile 6 | 121 | 4.34% | 是 | 2026-04-04 17:10 | 2026-04-04 17:10 | accounts.google.com(28)、auth.openai.com(23)、myaccount.google.com(23) |
| Profile 10 | 94 | 3.37% | 否 | 2026-04-04 16:03 | 2026-04-04 16:08 | chatgpt.com(64)、auth.openai.com(23)、api.cpass.cc(6) |
| Profile 11 | 68 | 2.44% | 否 | 2026-04-04 17:11 | 2026-04-04 17:11 | chatgpt.com(48)、auth.openai.com(9)、api.cpass.cc(8) |

三点可判读（均为表内数据的直接归纳，非「用途假设」列）：

1. **单点主导**：Profile 4 一家占 36.98%，且其 `localhost(308)` 是唯一超过 Default 单域量级以外的
   自造域信号 —— 它的「最后活跃」是 2026-09-27 00:47（当前窗口内），而「最后访问」停在 09-21，
   即**窗口仍在被打开、但不再产生 http 访问**（info_cache 的 active_time 与 History 不同源）。
2. **集群残留**：Profile 6/8/9/10/11 的最后活跃集中在 2026-04-04 16:01–17:11 一小时区间，域数 4–29，
   内容同质（chatgpt/accounts.google/api.cpass.cc）⇒ 判定为**同批创建后弃用**的调试 profile。
3. **真实使用面**：Profile 3/7 有登录态 + 有 04 月内的连续使用，窗口外但非残渣。

对 R430/R423 的效应：R423 的 D6「采集面扩不扩」按本表应**只扩 Profile 4（+1031）与 Profile 3/8（+931）**，
其余 5 个合计 826 次（2.51%）属历史残留，扩面对画像无增益 —— 即 R430 的 8.48% 里，
**≈6.0 个百分点是残留噪音，≈2.7 个百分点才是活跃面**（1031+655+276=1962 / 32886 = 5.97%）。

## 4. 验收对照（TODO#11 五条）

| # | 验收项 | 实测 |
|---|---|---|
| ① | 不落 gaia_id/邮箱 | `has_gaia_id/has_user_name/has_gaia_name` 仅布尔；显示名 `崔***`/`t***` 等；JSON 全文无邮箱字符串 |
| ② | 读 Login Data/Web Data 必拒 rc≠0 | NC4/NC5/NC6：`Login Data`「hit=Login Data」、`Cookies` 命中、`Web Data` ⇒ `rc=2` + `[REFUSE] 拒绝读取硬敏感文件` |
| ③ | 每行给来源文件路径与现取命令 | MD §证据出处 5 行「字段 → 来源文件 → 现取命令」；每行另有 `visits_source` 路径 |
| ④ | 真跑 9 行且 visits 与 R430 ±0 | 行数 9 = info_cache 9；`table_http_total=32886` == `baseline_combined_http=32886`；`mismatch_total=0`（参与对账 42 字段） |
| ⑤ | 表内不设「用途假设」列 | 表列共 15 个：profile/显示名(脱敏)/已登录/账号类型/最后活跃/最后访问/http访问/占本表%/占全机%/原始visits/域数/Top3域名/扩展数/默认搜索引擎/来源 —— 无「用途/推测」列 |

## 5. 工具与自测

- `python3 tools/chrome_profile_identity.py --selftest` ⇒ **23/23 passed**（T1–T11 + NC1–NC11）。
  负控覆盖：硬敏感文件拒读（NC4/5/6）、基线不一致 ⇒ `rc=3`（NC7）、缺 `Local State` ⇒ `rc=2`（NC8）、
  副本篡改 ⇒ verify 报 TAMPERED（NC9）、**基线缺字段不算失配**（NC10）、**指定副本目录被篡改 ⇒ `rc=2`**（NC11b）。
- 防假绿：T9b 断言 `compared_total>0`（否则「零比对也报 mismatch=0」是假绿）；NC11a 断言未篡改时副本自校验通过
  （否则 NC11b 可能因目录不存在而"通过"）。

## 6. 落地过程中修掉的坑（6 条）

1. 敏感 guard 收到 `str` 时未归一为 `Path`，导致 `Login Data` 漏网 ⇒ 统一 `Path(path)`。
2. 基线时间戳口径必须 `%Y-%m-%d %H:%M` + `astimezone()`，否则与 R430 基线恒失配。
3. `--copies-dir` 目录缺 `manifest.json` 时曾静默按「无副本」继续 ⇒ 改为 `rc=2` fail-closed。
4. selftest 早期读真库不可复现 ⇒ 改**真拷贝夹具 + 合成 Default 快照**（同一份夹具同时供 T 与 NC）。
5. `--snapshot` 传文件路径会被内部拼成 `file/History` ⇒ 传入即归一化（文件→其父目录）。
6. NC11 负控最初指向夹具 root（无 `manifest.json`）导致假失败 ⇒ 改指真实副本落点 `work_root/<day>`。

## 7. 边界与未做（明示）

- 时间口径：Default 用 2026-09-25 快照、非 Default 用 2026-09-26 冻结副本（与 R430 一致）；**跨日不一致已声明**，
  不做「同刻全机」断言。
- `Guest Profile` 有 `History` 但不在 `info_cache`（无身份面）⇒ 只作附注，**不入表、不计入合计**。
- 引擎字段：9 行 `short_name` 全为 `null`（本机 Preferences 未含该键）⇒ 输出 `—` 且 `source_ok=true`，不作「未设置」断言。
- 未接线：未接 `task_freshness`/定时；未改 R423/R430 既有产物（只读消费）。
- 未做：`/app/mediaUsage` 类等其它源不在本表范围（见 R436）。

## 8. 复跑命令

```
python3 tools/chrome_profile_identity.py report   --chrome-root "/Users/cuip/Library/Application Support/Google/Chrome"   --copies-dir temp/chrome_profiles/2026-09-26   --snapshot temp/chrome_snapshots/2026-09-25   --baseline temp/chrome_profiles/consume_2026-09-26.json   --out-md temp/chrome_identity/2026-09-27/report_frozen.md   --json  temp/chrome_identity/2026-09-27/report_frozen.json
# 副本自校验：python3 tools/chrome_profile_identity.py verify --copies-dir temp/chrome_profiles/2026-09-26
# 自测：       python3 tools/chrome_profile_identity.py --selftest
```

（注：去掉 `--copies-dir` 时走实时库，会读当前 Chrome 库，行数与合计随使用漂移 ⇒ 只有冻结副本模式才可与基线 ±0。）
