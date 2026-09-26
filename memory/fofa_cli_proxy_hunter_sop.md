# FOFA CLI Proxy Hunter SOP

CTF/沙箱场景：从 FOFA 发现并**验证** OpenAI 兼容代理资产（指纹 `body="CLI Proxy API Server" && body="models"`）。
教程正文在 skill 内，本 SOP 只记指针+触发+缺省+关键坑。

## 位置
- Skill 根：`~/.codex/skills/fofa-cli-proxy-hunter/`
- 总纲 `SKILL.md`；切片策略 `references/slice-strategy.md`
- 切片包 `templates/`：`default_slices.json`(CLI Proxy 默认全量)｜`newapi_slices.json`(New API 专用)｜`slices.json`(default 的兼容副本)

## 触发输入（用户最多给一样，都不给就跑内置默认切片包）
- 搜索词（如 `body="gpt-5.5" && body="models"`）
- FOFA JSON/target 文件路径（如 `/tmp/fofa_result.json`）
- FOFA curl 文本（用于动态签名翻页，粘贴 curl）
- **profile 选择**：用户提 New API/newapi 相关发现 → `--profile newapi`(默认 `cli_proxy`)；profile 决定内置切片包 + 缺省 FOFA 指纹查询

## 物理执行入口（优先一键编排器）
```
python ~/.codex/skills/fofa-cli-proxy-hunter/scripts/auto_fofa_cli_proxy_hunter.py [--profile cli_proxy|newapi]
```
编排器常用参数：`--profile`(切片包/缺省查询)｜`--query`(覆盖缺省指纹)｜`--slice-family port,geo,model_keyword`(按 family/name 过滤切片,可重复或逗号分隔)｜`--max-slices N`(0=全部)｜`--pages`｜`--models-only`(只到 /v1/models 不实调)｜`--model-keyword gpt`(只留/实调含该词的模型 id)｜`--test-max-tokens`(chat 实调 max_tokens,默认8)｜`--stop-after-first-usable`(单目标出一个 usable 即停后续实调)｜`--fofa-delay`(默认0.6,实跑用3更稳)｜`--fofa-context-max-age 300`
手动分步脚本（编排器调试/窄回放时用）：
- `extract_fofa_assets.py fofa*.json --out targets.txt` 从 FOFA JSON/JSONL 抽 base URL
- `openai_compatible_probe.py --targets .. --keys k1,k2` 探 `/v1/models`(多 key 逗号分隔对比,key_label 区分)；加 `--verify-models` 逐模型实调；`--model-keyword`/`--test-max-tokens`/`--stop-after-first-usable` 同编排器；`--summarize-only <jsonl> --summary out.json` 只重新汇总已有结果不重探
  - 探测前会把含完整端点(`/v1/models`、`/v1/chat/completions`、`/v1/completions`、`/v1/images/generations`、`/v1/responses`、裸 `/v1`)的行**归一化回 base URL** 再测，target 列表可直接喂端点 URL
- `fofa_auth_context.py` 解析粘贴的 curl→临时上下文+解密前端签名密钥+逐页/切片动态签名(`test-page` 子命令验分页)；`fofa_sign_search.py` 跑签名 v1/search

## 内置切片包（profile→维度）
- **cli_proxy(默认)** `default_slices.json`：基础查询/时间/端口/协议/地理(国家→CN region)/ASN 组织/域名 IP 形态/响应指纹/证书/模型关键词(gpt·gpt-5.5·codex-auto-review·claude·gemini·gpt-image)/API 路径/兜底排除
- **newapi** `newapi_slices.json`：基础指纹(`body/title="New API"`·`new-api`·`newapi`)/OpenAI 路径/New API UI(login·登录·console·token·quota·令牌·渠道·余额·用户)/常见端口(3000·3001·7000·8000 等)/响应指纹/地理/域名 IP 形态/模型关键词(gpt·claude·gemini)

## key 说明（含 newapi）与伪 key 探鉴权
- **无 profile 专属默认 key**：cli_proxy/newapi 的 `--keys` 缺省都是占位符 `your-api-key-1,your-api-key-2`(probe/auto 同源)，**不会通过鉴权**，跑真实验证必须自传候选 key（`k1,k2` 或 `env:NAME`）。New API 的 token 由各实例控制台按用户生成(常 `sk-`)，没有全局默认 key。
- **伪 key 探鉴权边界(现有能力即可)**：脚本不带内置"无鉴权/匿名"模式(始终发 `Authorization: Bearer {key}`)，但可传一个明显假的 key 探开放实例——`/v1/models` 仍 `models_nonempty`(200有id)=该实例不校验鉴权(开放,可直用)；返回 `auth_or_forbidden`(401/403)=需真 key。据此用现有 status 语义即可区分开放/受限，无需改脚本。

## 自动模式流水线（编排器 6 阶段）
1 按 `--profile` 载入切片包(无搜索词→内置对应包;有搜索词→插到包最前，除非用户说"只跑这个"否则仍跑内置;缺省查询也由 profile 决定) → 2 真实请求 FOFA 分页/切片(需鉴权上下文,或用户给的 JSON/targets) → 3 提取去重 targets.txt → 4 探 `/v1/models`(models_probe.jsonl+summary) → 5 筛 models_nonempty 逐模型实调(invocation_probe.jsonl+summary) → 6 生成中文 report.md
- 已有 `/tmp/fofa_*` 或 `model_invocation_results.jsonl` 会话产物 → 优先复用增量汇总，不重抓
- 默认走自动模式，别让用户手动跑分步脚本

## FOFA 鉴权上下文（curl→动态签名，三子命令）
- `fofa_auth_context.py check --context <ctx> --page 1` 预检旧上下文有效性；通过则复用，失败/缺失/过期才向用户索要当前 FOFA 页完整 curl
- `store-curl --curl-file <f> --context <ctx> --cache-private-key` 解析粘贴 curl 写临时上下文(解密前端签名密钥)
- `test-page --context <ctx> --page N` 小范围分页验证
- 上下文默认 `/tmp/fofa_cli_proxy_hunter_auth_context.json`(0600,仅引用不读)；`--fofa-context-max-age 3600` 免重签

## 扩大范围（切片策略，详表见 references/slice-strategy.md）
单查询分页受限(如第7页起权限限制)或命中量大时，拆独立切片，每切片独立JSONL，主代理最后去重合并。
- 触发扩大：默认包命中面小/被限页/要更全 → 调大 `--max-slices`、`--pages`，或用 `--slice-family port,geo,model_keyword` 聚焦某族补窄切片(可重复/逗号分隔)；New API 目标切 `--profile newapi`
- 切片维度(A-J)：A更新时间 B端口(80/443/8080/8085/8443/8788/9999/长尾排除) C协议(http/https/非标) D地理(国家→CN再按region) E ASN/组织(云厂商/IDC) F域名IP形态(is_domain/is_ipv6/后缀) G响应指纹(title/server/status_code) H证书(subject/issuer/自签/托管) I路径与模型关键词(/v1/*、gpt/claude/gemini/codex/gpt-image) J兜底排除长尾
- 某维度命中过多→按下一级继续拆(时间按季/月，CN按region)
- 保持低频：扩切片时 `--fofa-delay` 不降(默认0.6，实跑用3更稳)，避免风控

## 子代理并行（仅拆独立发现维度，各写独立文件）
| 子代理 | 切片族 | 输出 |
|---|---|---|
| A | 时间/新鲜度 | fofa_time_slices.jsonl |
| B | 端口/协议 | fofa_port_protocol_slices.jsonl |
| C | 国家/地区 | fofa_geo_slices.jsonl |
| D | ASN/组织/云 | fofa_org_slices.jsonl |
| E | 指纹(header/server/status/证书) | fofa_fingerprint_slices.jsonl |
| F | 模型关键词/OpenAI路径 | fofa_model_keyword_slices.jsonl |
子代理只回报：切片名/FOFA查询/尝试页范围/资产数/输出路径/错误或限页信息。禁多代理写同一JSONL。全部完成后主代理去重合并。

## 三段决策（逐级收紧，第3步决定性）
1. FOFA 指纹命中 = 仅主机匹配
2. `/v1/models` 200 且有 `data[].id` = key 能拉列表（**不算可用**）
3. 最小实调成功 = 唯一正向 `usable`（HTTP200 且有 `choices[]` 或图像 `data[]`）；脚本按模型名选端点：
   - 文本/chat 模型→`/v1/chat/completions`，payload：单条 user `reply exactly: ping` + `max_tokens:8` + `temperature:0` + `stream:false`
   - 图像模型→`/v1/images/generations`，payload：`prompt:ping` + `size:256x256` + `n:1`

## /v1/models 原始 status 语义
`models_nonempty`(200且有data[].id,留待实调)｜`models_empty`(200无id,低优)｜`auth_or_forbidden`(401/403,key被拒)｜`rate_limited_or_quota`(429,降并发/延后)｜`upstream_unavailable_*`(代理通但上游鉴权/服务失败)

## 结果分级
A=实调 usable｜B=列表有但限流/配额(429)｜C=上游auth/服务不可用｜D=models空(弱信号)｜E=401/403/超时(排除)

## 关键坑 / 红线
- 模型 id 只从原始 `/v1/models` 的 `data[].id` 取，**禁**从摘要串解析
- 两段报告严格分开：真实可用 / 只列表不可用，禁合并
- 分页受限→换切片；多子代理并行时**各写独立文件**，主代理最后去重合并（去重键序 `host → ip+port+protocol → domain+port+protocol → url → ip+port`；合并时保留 `matched_slices` 记录命中来源作覆盖证据）
- 低频低 token 探测，避免同目标反复触发冷却；`--workers` 探列表可 8，实调降到 4
- 安全：不打印长期 FOFA `Authorization`/cookie/私钥/签名密钥；只读用户给的文件+skill文件+探测输出
- 失败降级：429→降并发/延后重测；单目标失败不中断其他目标

## 实跑操作要点（本机验证过）
- **长任务必后台**：编排器/探测常>60s，前台调用会被默认超时强杀→用 `nohup ... > out.log 2>&1 &` 后台跑，sleep后轮询产出文件(report.md/*_summary.json)
- 目标多时分两步：先 `--models-only` 出候选，再仅对 `models_nonempty` 候选跑 `--verify-models --max-models-per-target 2 --sleep-between-invokes 2 --workers 2`(低频少量)
- 实调 HTTP 500 且 body 报 `Post https://api.anthropic.com/... failed` = 代理上游key失效 → C级不可用(列表能拉≠可用，正是本SOP价值)
- 类 `url.txt` 轻量快检预设(单 key 快筛某族模型)：`--model-keyword gpt --verify-models --test-max-tokens 1 --stop-after-first-usable --workers 20 --timeout 12`

## 最终报告格式
两张表（可用表：Target/可调用模型数/模型族/key可用性/判定；列表表：Target//v1/models/失败原因/判定）+ 证据文件块（`*_probe.jsonl` / `*_summary.json`）。

## 本机访问证据里的中转/公益站种子（选填，非 FOFA 结果）
只读工具 `tools/domain_gap.py` 会把「本机 Chrome 高频访问、而 memory+报告语料零提及」的域名列出来，其中 A 节即 New API / 公益站 / 镜像类（2026-09-27 快照：20 域 / 657 次，如 `api.fengwind.com` 74、`routerpark.com` 67、`web4.52pokemon.cc` 65、`api-welfalre.fengwind.com` 50）。
- 用途：给 `--profile newapi` 或类 `url.txt` 轻量快检**备一批候选 base URL**，先 `--models-only` 出候选，再按本 SOP 三段决策实调。
- 清单落点：`memory/domain_gap/COLLECTION_CANDIDATES.md`（A 节）；重算＝`python3 tools/domain_gap.py scan`（数据源随每日 Chrome 快照更新）。
- ★口径限制：这些域名**只是本机访问过的站点**，不等于已验证可用的代理；也不保证收录进 FOFA——是否 usable 仍以本 SOP 的实调结果为准。
