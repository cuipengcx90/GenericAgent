# 重点项目更新监控 SOP

## 目的与范围
监控可扩展来源表中的 GitHub 项目及官网 Changelog，输出上游更新、修复、新能力和基于本地环境的升级建议。系统只采集、分析、通知；**绝不自动升级、改配置或修改项目依赖**。

## 已落地组件
- 来源表：`../project_update_monitor/sources.json`
- 脚本：`../project_update_monitor/monitor.py`
- 幂等基线：`../project_update_monitor/state.json`（运行成功才原子覆盖）
- 当前已登记的来源以 `sources.json` 实际内容为准（非固定数量），新增来源自动纳入卡片，无需改脚本或提示词。示例已有：Paseo、Ruflo、Multica、Tutti、Orca、GenericAgent、Claude Codex Bridge、Trellis。

## 工作流
1. 读取来源表与成功基线；未知来源的首次执行使用 `--initialize`，只建立基线，严禁把历史版本当新消息推送。
2. GitHub 优先走结构化 API：Release、Tag、默认分支 Commit；官网 Changelog 使用标准化正文 SHA-256 检测变化。
3. 对比 `release_tag`、`tag`、`changelog_hash`；只以版本标识（含 RC/Beta/alpha 等预发布 tag）变化为通知依据；同版本号下的 commit_sha 前进（未产生新 tag/release）不算更新，避免噪音。
4. 读取本机 CLI 版本、关联路径与项目配置，结合预发布标识、变更类别与升级策略给出建议。
5. 固定报告必须有：中文结论、上游链接/版本、变更归类、中文核心变更摘要、本地环境核对、自动升级禁用说明。英文项目名、命令、目录等专有技术名词保留，但不得整段转发英文原文；质量闸门不通过不得推送。
6. 成功通知后才允许推进已通知基线；采集或通知失败不得覆盖旧基线，并在下一轮重试。无 `--notify` 的本地预览不得推进存在变化的来源基线，防止漏报。
7. 无 GitHub Release（`release_body` 为空）但配置了 `changelog_url` 的来源（如 Trellis 用 Mintlify 文档站发布日志），直接 fetch `changelog_url` 拿到的是 SPA 空壳，无实质内容；改为额外 fetch `changelog_url` 去掉尾部斜杠后加 `.md` 后缀（Mintlify 标准约定），可拿到纯 Markdown 原文存入 `changelog_body` 字段，作为 `chinese_change_summary` 的内容源，优先级为 `release_body > changelog_body > commit_message`；该 fetch 用 try/except 包裹防止 404 中断整体流程，且不影响原有 `changelog_hash`（仍用原始 `changelog_url` 正文算，避免变更检测逻辑漂移）。
8. `sources.json` 里的 `changelog_url` **必须指向通用/最新页**（如 Trellis 的 `.../zh/changelog`），禁止写成某个具体版本号的路径（如 `.../zh/changelog/v0.7.0-beta.1`）。Mintlify 文档站的通用 changelog 页 `.md` 端点只返回“当前最新版本”的正文（不是全部历史），一旦上游发新版本，这个页面内容和 hash 都会自动前进；若误填成具体版本路径，等新版本发布后该 URL 会永久卡在旧版本内容，`changelog_hash` 也不会再变化，导致后续更新永远检测不到。新增 Mintlify/文档站类来源时，先用 `.md` 后缀验证返回的是完整版本历史入口页而非某个 pinned 版本。

9. 纯英文原文的翻译固定走本机 `codex exec --sandbox read-only --skip-git-repo-check --ephemeral --output-last-message <tmpfile>`（脚本内确定性调用，非本会话手动翻译）：因为 `monitor.py` 是被 `sche_tasks/project_update_monitor.json` 每 6 小时无人值守唤起的独立子进程，唤起它的临时 agent 会话只转发 stdout，脚本必须能在没有任何 LLM 会话陪同的情况下自主完成翻译，故把"英译中"这一原子操作外包给脚本可直接调用的本机已登录 CLI，其余判断（是否已中文/是否触发翻译/闸门是否通过）保持脚本内确定性代码。返回值要求等长 JSON 字符串数组，逐条校验必须含中文字符，否则抛 `TranslationError`（不静默降级，直接让该来源本轮标记异常、不覆盖旧基线）。
10. 翻译前必须先清洗两类噪音，否则会误判为翻译失败或残留英文：①同一 bullet 内官方把中文说明和英文重复说明无分隔直接拼接（如"中文句子。 English repeat sentence."），需取最后一个中文句末标点（。！？）之后的剩余文本，若该剩余文本不含中文字符就整段截掉；②纯资源下载链接行（如 `[macOS xxx](url.dmg)`）会被行提取器误当作正文 bullet 收纳，以及裸露无括号包裹的 `by @user in <url>` 落款（不同于已处理的括号内 `(by @user in url)` 格式）需要专门扩展正则清理，二者不过滤会导致该 bullet 全英文突破闸门失败。

11. 本机 CLI 版本探测存在**非交互 shell 假阴性**（2026-09-22 实测确认）：`local_commands` 在脚本子进程里执行，拿不到 `~/.zshrc` 里定义的同名 **zsh 函数**，PATH 里也可能没有真实二进制。已知案例：`ccb`（Claude Codex Bridge）在 `~/.zshrc:301` 是函数 `ccb() { GHOSTTY_SHELL_INTEGRATION= command ccb "$@" ; }`，非交互探测必然失败 → 卡片把"已安装且已升级到 v8.7.1"误报成"本机未检测到安装/项目引用"，进而给出"暂不更新"而非"评估更新"的**错误建议**。新增/维护来源时，`local_commands` 优先选 PATH 上真实存在的二进制（如 `orca --version`），或补 `local_paths`（如 `~/.local/share/codex-dual`、`~/.ccb/ccb.config`）作为旁证；改探测方式前先确认，避免误报。
12. 判读 stderr 噪音（2026-09-22 实测确认）：本机 `/opt/anaconda3/bin/python3` 的 numpy 已升到 **2.4.4**，而 pyarrow 仍是 NumPy 1.x ABI 编译 → `import sklearn`/`import pyarrow` 直接 `ImportError: numpy.core.multiarray failed to import`。GA 的 `llmcore.py:51 _llmlingua_warmup` 后台线程导入 `llmlingua` 即命中此链，每次进程启动在 stderr 打约 20KB、10 段 traceback（实测 `python3 -c "import llmlingua"` → rc=1，LLMLingua 提示压缩实际不可用），9 月 1 日日志尚无此噪音。判读要点：**只要 exit=0，这就是无害噪音，不是采集异常**（不要当成 exit=2 处理，更不要因此重复推送）；真正的修复属 GA 环境层面的决策（`numpy<2` / 升级 pyarrow / 隔离 venv），需请示后再动。

## 升级建议分级
- 安全修复且本机受影响：立即人工处理。
- 本机已安装的稳定版修复/高价值功能：建议评估更新，先查破坏性变更、依赖和回滚。
- Beta/RC/nightly：观察后更新；隔离环境验证后再决定。
- GenericAgent、Trellis 或会修改核心配置的数据迁移：人工决策，先用 Git worktree 演练、验证任务/session/通知流并准备回滚。
- 本机无安装或无引用：暂不更新，继续监控。

## 新增来源
在 `sources.json` 增加对象：唯一 `id`、`name`、`repo`、可选 `changelog_url`、`importance`、`local_commands`、`local_paths`、`upgrade_policy`。先执行一次 `--initialize` 建基线，再纳入定时任务。改前校验 JSON，改后编译并执行监控脚本。

## 运行与验证
```bash
python3 ../project_update_monitor/monitor.py --initialize  # 新来源首次建基线，无推送
python3 ../project_update_monitor/monitor.py               # 日常差异报告
python3 -m py_compile ../project_update_monitor/monitor.py
```
检查 `state.json` 包含每个来源；无更新时应输出“无新增变化”状态；有更新输出中文摘要。API/网页失败必须显示来源级异常，不能静默吞掉或推进该来源状态。

## 通知与调度
定时定义放 `../sche_tasks/`。调度间隔建议 `every_6h`，`max_delay_hours` 不大于 8。通知只走已配置的飞书私信、飞书群和钉钉群通道；报告生成与质量校验完成后仅推送一次，避免不同入口重复发送。每次成功检查均用一张飞书交互卡片按 `sources.json` 顺序完整列出全部项目（数量随来源表增删自动变化）：版本标识有变化的项目显示精简中文摘要、更新建议与 Release/Commit 链接；无变化项目只显示"与上次推送相比无版本更新"和当前版本，禁止重复转发长英文原文。采集异常时不发送可能遗漏项目的正常状态卡，避免误报。人工验收/本地预览严禁携带 `--notify` 对真实通道试探，验证一律走只读快照对比。

## exit=2 诊断（2026-09-23 实测沉淀）
`exit=2` 真实语义（代码级）：本轮 `errors` 非空 → **不建卡、不推送**（L316），且 `can_commit` 为假 → **基线不推进**（L311）。基线即通知幂等键，故 exit=2 = "本轮不出卡"，**不是丢数据**：下一周期会把同一批更新重新报告（绝不静默）。
- 常见真因：**本机 codex exec 翻译冷启动超时**（`_translate_to_chinese()` 内部 `timeout=120`）。实测单项热态需 ~104s（Paseo），冷态首调直接 >120s → `TranslationError` → 整轮 all-or-nothing 不出卡。GitHub 配额通常没问题（2026-09-23 实测 remaining 4971/5000、8 源 releases API 全 ~1s 200）。
- 诊断三步（全部零副作用）：①`gh api rate_limit --jq .resources.core` + 逐源 `releases?per_page=1` 快速排除限流；②**不带 `--notify`** 跑 `monitor.py`（不推送、不落盘基线），异常明细在 stderr 的「## 采集异常（未覆盖既有基线）」段；③查 `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl` 的起始与末次写入时间，判断翻译调用是否被 120s 内超时掐断。
- 运行特征坑：脚本全程静默、**结束才一次性打印**；单轮（8 源快照 ~45s + 3 次翻译）可达 **~4 分钟**，用 60s/180s 的工具超时会误判为"卡死"。须后台起 + 轮询。**macOS 无 `setsid`、无 `timeout` 命令**：用 `subprocess.Popen(..., start_new_session=True)` 起后台并立即返回；逐源诊断限时用 `signal.alarm`。
- 自测陷阱：用 `signal.alarm` 包 `report()` 时抛出的 `TimeoutError` 会被 `except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError)` 吞成 `TranslationError: ... TimeoutError: internal timeout`——**这是脚手架假象而非脚本真错**，别据此下结论（2026-09-23 实际踩到）。
- 未根治项（提案，属改自身源码需请示）：给翻译调用加"首调放宽超时 / 失败重试 1 次"，避免单个翻译超时导致整轮不出卡。

## 卡片图标规范
为提升可读性，卡片固定使用以下图标前缀（改动集中在 `monitor.py` 的 `classify`/`project_card_element`/`build_notification_card`）：
- 标题：🚀 重点项目更新监控；汇总行：有更新用 🔔，全部无更新用 📊。
- 单项目徽标：🔔 有版本更新 / ✅ 无版本更新；项目名前缀 📦，前面另加 `序号.`（`project_card_element` 的 `index` 参数，随 `sources.json` 顺序 1.2.3. 排列），与下方"核心变更"列表内部的 1.2.3. 序号是两处不同层级，不要混淆。
- 字段前缀：🏷️ 当前版本、🗂️ 变更类型、💡 建议、🔗 Release/Commit 链接按钮。
- 变更分类标签：🔒 安全、💥 破坏性变更、🐛 修复、✨ 新增能力、⚡ 性能、🔎 待人工解读。
新增分类或字段时同步在此登记，保持图标语义一致，避免同一含义用不同符号。

