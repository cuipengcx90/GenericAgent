# R441 · haisou `is_relevant` 误判修复 + 历史命中 A/B 复算

对应 TODO（temp/TODO.txt）第 120 行「能力 | 【第17批·三路评审 9/8/8=8.33（并列最高）】haisou `is_relevant` 误判修复 + 历史命中回测」。
本批**只做判据修复与回测**，不触碰额度/调度面（额度极紧与「轮转游标」仍是独立待办，见 §7）。

---

## 1. 结论摘要

| 项 | 结果 |
|---|---|
| 根因 | `max(tokens, key=len)` 在**等长**时取先出现者；`AI测试` 切成 `["ai","测试"]`（两个都是 2 字符）⇒ 核心词退化成 `ai` ⇒ 含 `ai` 即放行 |
| 修法 | 新增 `tools/haisou_relevance.py`（剔除通用 token 后取最长候选命中 + ASCII 连写兜底 + 可配置同义词表），生产脚本改为**引用同一源**（导入失败走内联降级版） |
| A/B（历史语料 1425 条） | 旧放行 **378** → 新放行 **353**；**旧放行/新拒绝 25**（全为杂项）；**新多收 0** ⇒ 新判据是旧判据的**真子集**，零新增风险面 |
| selftest | `SELFTEST PASS: 正例 12 / 负控 10 / 反向负控 4 / 断言 5 ⇒ 共 31 项` |
| 生产↔工具一致性 | 1425 条逐条比对，`prod_vs_tool diff: 0`；`JUDGER: tools/haisou_relevance.py (单一源)` |
| 回退路径负控 | 在无 `tools/` 的临时目录执行 ⇒ `JUDGER: inline-fallback (ModuleNotFoundError)`，且同一 bug 亦被修好（`AI测试/AI设计工作流 -> False`） |

---

## 2. 背景与 bug 复现

生产判据（修复前，`birdie_monitor/haisou_daily.py`）：

```python
def is_relevant(keyword, title):
    title_l = (title or "").lower()
    tokens = tokenize_keyword(keyword.lower())
    core_token = max(tokens, key=len)
    return core_token in title_l
```

`AI测试` / `AI开发` / `AI创业` 等关键词切词后为 `["ai", "…"]`，两段等长（2 字符）⇒
`max(..., key=len)` 返回**先出现的** `ai`，于是「AI设计工作流」「AI短剧」「AI公众号爆文变现」等
只要标题里出现 `ai` 全部放行。实测效果：09-22 / 09-23 / 09-25 累计 4 条推送**全为假阳性**
（如把「AI设计工作流设计师必学」判为 AI 测试课程）。

---

## 3. 新判据（`tools/haisou_relevance.py`，359 行 / 16076 B / sha256 `4ebec77fe92369b4f31be83bf408aed6f8fb41c72790f1493343e3ca89e01484`）

1. **剔除通用 token** 后再选核心词：`ai`/`aigc`/`ai+`/`大模型`/`课程`/`教程`/`视频`/`网课`/`全套`/`合集`/`资源`/`资料`/`学习`/`培训`/`训练营`，以及长度 ≤3 的纯 ASCII 片段；通用词**不能单独**作为放行依据。
2. 剩余候选 token 取**最长者**判命中（与旧行为同构——旧行为同样只要求一个 token 命中，只是没先剔通用词且等长取首）。
3. 命中方式：ASCII token 按**词边界**正则匹配（防 `ai` 命中 `said`）；CJK token 按规范化文本子串匹配。
4. **ASCII 连写兜底**：允许 `Claude Code` ← `ClaudeCode`、`Vibe Coding` ← `VibeCoding` 这类无空格连写形态（否则真阳会被误杀）。
5. **同义词表**可配置（内置默认 + 可选外部 `tools/haisou_relevance_synonyms.json`，本批未建该文件），且只收**同等具体**的词组——通用词（如「接口」「性能」「测试」「vibe」「出海」）不得进表，否则会重新引入假阳。

> 迭代记录（为什么是这套规则）：首版把「任一候选命中」+ 较宽同义词表一起上，A/B 立刻报出 **新多收 274 条**，逐条复核发现 MusicFree 接口 / 老头乐性能 / 电气自动化 / Summer Vibe / 懂王跨词等**真垃圾**被新放行 ⇒ 说明「新判据过宽」本身是风险。随后：把 normalize 从「删标点」改成「标点→空格」（防「一看就懂 (王絮)」拼出「懂王」跨词假阳）→ 同义词表只留同等具体词组 → 选择规则改为「过滤通用词后取最长候选命中」（≡旧规则但先剔通用词，**结构上保证不引入新放行**）。收紧后真阳仍有 5 条（ClaudeCode / VibeCoding 连写）被误杀，故补 ASCII 连写兜底。

---

## 4. 证据（全部为当场命令原始输出）

### 4.1 selftest

命令：`python3 tools/haisou_relevance.py --selftest`

```
SELFTEST PASS: 正例 12 / 负控 10 / 反向负控 4 / 断言 5 ⇒ 共 31 项
```

负控里包含「AI设计工作流」这类伪样本（必须被拒），并含**负控的负控**：旧判据必须在前 4 条真实误报上放行，
否则说明测试没测到位（判据没被真正替换）。

### 4.2 A/B 复算

命令：`python3 tools/haisou_relevance.py --ab`
语料：`birdie_monitor/haisou_seen_items.json`（447912 B，mtime 2026-09-25 11:16）

```
SELFTEST PASS: 正例 12 / 负控 10 / 反向负控 4 / 断言 5 ⇒ 共 31 项
AB 语料 1425 条 => 旧放行 378 / 新放行 353 / 旧放行新拒绝 25 / 新多收 0
报告已写 temp/haisou_relevance_ab.md
```

报告：`temp/haisou_relevance_ab.md`（52 行 / sha256 `9a88650a152e8ce87f7d28d75d6855a612d7830028bab4a82d9983e79f49f9a6`）

> 口径说明：`seen_items` 只记录**旧判据放行过**的条目，因此语料里不存在「旧拒绝」的真实样本，
> 「旧拒绝/新放行」只可能是旧判据因 token 选择而漏掉的同题条目。当该节为「（无）」时，
> 即证明新判据放行集合是旧集合的**真子集**：只减不增，零新增风险面。

### 4.3 差异面逐条判读（25 条，人工分类）

| 关键词 | 条数 | 典型条目 |
|---|---|---|
| `AI测试` | 19 | AI的25种可能（书单）／AI书单热点变现实战课／AI智能体课程怎么学／AI写小说app／AI超级制图下载2026／Ai象棋终极版／【AI剧】BL国产《纸鸢》／AI生成美女图片提示词大全／AI短剧创作课程／AI公众号爆文变现训练营／AI短视频2026零基础实操课／AI小王子CodeX实战课／AI设计工作流设计师必学 等 |
| `AI创业` | 6 | 【AI剧】BL国产《砚色遇柚光》／【AI剧】BL国产《纸鸢》／【AI剧】BL国产《黑月光的反击》／【AI剧】BL国产《屠夫与娇花》／【AI剧】国BL《本座的灵宠要上天》／AI视频教程丨徐艺晏·HR如何用办公小浣熊做好数据分析？ |

判读结论：25/25 均与「AI测试」「AI创业」**无关**（AI 剧 / AI 绘画 / 短视频 / 小说 / 书单 / 象棋 / 图片提示词），
全部为应拒条目；逐条 `explain()` 的 `hit` 均为 `None`（即：剔除通用词后无任何有效候选词命中）。**零误杀**。

### 4.4 生产侧接线与一致性

`birdie_monitor/haisou_daily.py`（253 行 / sha256 `7d2f1e31a2e94f824bec754484ab088e69a22ff4d839c3e14abb08a2e522423d`）：

- L145：`_JUDGER_SRC = "tools/haisou_relevance.py (单一源)"`
- L150：`from tools.haisou_relevance import is_relevant`（按 `__file__` 定位仓库根）
- L152–160：导入失败的**内联降级版**（无同义词表），并置 `_JUDGER_SRC = inline-fallback (...)` 便于事后核验走了哪条路
- L197：调用点 `if not is_relevant(keyword, title):`（唯一调用点，行为不变）

一致性实测（同一语料 1425 条，生产模块 vs 工具模块逐条比对）：

```
pairs 1425
JUDGER: tools/haisou_relevance.py (单一源)
prod_vs_tool diff: 0
AI测试/AI设计工作流 -> False
AI测试/软件测试全套 -> True
```

### 4.5 回退路径负控（证明「降级版也不是旧 bug」）

把 `haisou_daily.py` + `memory/keychain.py` 复制到 `/tmp/hd_nc`（**无 `tools/`**），以 `cwd=/tmp/hd_nc` 执行：

```
sys.path0 ['/tmp/hd_nc', '', '/opt/anaconda3/lib/python312.zip']
tools not importable: ModuleNotFoundError
JUDGER: inline-fallback (ModuleNotFoundError)
AI测试/AI设计工作流 -> False
AI测试/软件测试全套 -> True
```

（第一次尝试该负控时 `cwd` 仍在 GA 根 ⇒ `tools` 仍可导入、负控无效，必须显式换 cwd 才算真负控。）

---

## 5. 可复跑命令

```bash
cd ~/projects/GenericAgent
python3 tools/haisou_relevance.py --selftest
python3 tools/haisou_relevance.py --ab                 # 落 temp/haisou_relevance_ab.md
python3 tools/haisou_relevance.py --check "AI测试" "AI设计工作流设计师必学"
```

---

## 6. 部署面与回滚

- 部署面：`birdie_monitor/haisou_daily.py`（判据改为引用单一源）+ 新增 `tools/haisou_relevance.py`；无定时任务、无配置项变更、无状态文件改动。
- 判据为**纯函数**、无网络无副作用；`--ab` 只写 `temp/` 证据文件。
- 回滚：把 L145–160 恢复为原 9 行 `is_relevant` 定义（旧文本见 §2）即可，或让 `tools/haisou_relevance.py` 缺失（自动走内联降级版，行为≈旧版但已修 tie bug）。回滚不影响 `seen_items`（本批未改任何状态文件）。

---

## 7. 边界与残余

1. **不解决额度问题**：SOP 实测「第 1 次命中后即 403」，21 词 × 3 平台 ≈ 135 请求与额度差两个数量级；
   「轮转游标」（跨次跑记住扫到哪个词）仍是独立待办，本批未做。
2. **不解决抓取失败**：09-25 首例首页抓取失败（SSLEOFError + 静默 rc=0）属网络/站点面，与本判据无关。
3. **同义词表默认内置**：外部 `tools/haisou_relevance_synonyms.json` 未创建（属可选增强）；同义词表新增条目**必须**遵守「只收同等具体词组」，
   否则会重新引入假阳——建议每次改表都重跑 `--ab` 并复核「新多收」节是否为「（无）」。
4. **语料面偏置**：`seen_items` 只含旧判据放行过的条目 ⇒ A/B 只能证明「不新增放行」，**不能**证明「新拒绝的旧放行条目」在语义上确实是垃圾；
   本批用**逐条人工判读**（§4.3）补上这一环。
5. **本批未跑真链路**：判据是纯函数 + selftest/A/B/一致性/负控四重证据；未触发真实 haisou 抓取（额度极紧，且会消耗当日额度），
   真链路的首次验证将在下一次定时/手动运行时自然发生；届时看日志里是否仍有 `AI设计工作流` 这类假阳性即可。

---

## 8. 给 SOP 的订正（同批执行）

`memory/haisou_monitor_sop.md` 原记「`is_relevant` 退化取首 token，修法待批」——
本批已落地，故 SOP 尾注改为：判据已由 `tools/haisou_relevance.py` 单一源提供 + 生产脚本引用（带回退），
并注明**额度极紧**与**轮转游标仍是独立待办**两件事不受本批影响。
