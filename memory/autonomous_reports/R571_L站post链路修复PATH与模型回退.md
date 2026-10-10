# R571 L 站 post 链路修复:PATH 注入 + 模型回退链(2026-10-11)

用户指令:「问题就修啊,有问题就修,你要保证稳定性啊」。本批在 R569 只读根因裁决之后**落地 F1 修复**,
并顺带解决真跑时暴露的上游额度问题(同一管道第二层),两条自测 + 一次受限 PATH 端到端真跑全过。

## 1. 背景(承 R569)

R569 判定 L 站发帖 `post` 从未真跑,两道闸门:

- **闸门一(设计使然)**:`post` 仅在等级「获赞 earn」缺项 ∧ 有草稿时排队;历届获赞 47–50 / 门槛 20 恒 MET ⇒ 永不进 post 路由。
- **闸门二(真缺陷)**:daemon 进程 `PATH` 不含 `/opt/homebrew/bin`,而 `codex` 是 `#!/usr/bin/env node` 脚本
  ⇒ `gen_text()` spawn 时 `rc=127 env: node: No such file or directory` ⇒ forge 连标题都生成不出(`NO_TOPIC`)。
  铁证:同款受限 PATH 下直调 `acts.gen_text()` 0.2s 返回 `OUT=None`。

## 2. 本批改动(3 文件,工作区 M)

### 2.1 `tools/linuxdo_acts.py`(+69/−9)

- 新增 `CODEX_PATH_EXTRA = ("/opt/homebrew/bin", "/usr/local/bin")` 与 `codex_env(bin_)`:
  把 `dirname(codex_bin)`、`dirname(CODEX_BIN)`、两个常见落点、原 `PATH` 去重保序拼起来 ⇒ 子进程能解析 `node`。
- `gen_text()`:`subprocess.run(..., env=codex_env(bin_))`,并把单次超时改成 **deadline 预算**
  (`left = deadline - now`,`left <= 5` 即 `budget_exhausted` 停手)⇒ 无论链多长都不会挂住调用方。
- 新增 `CODEX_MODEL_CHAIN`(env `LJQ_CODEX_MODEL` 可覆盖,默认 `4router-terra/gpt-5.6-terra`,
  `4router-terra/gpt-6.1-sol`,`hxi/gpt-5.6-sol`),`codex exec -m <id>` 依次尝试;
  **配置默认模型(`""` 即不带 `-m`)排在链尾兜底**,避免先烧一轮已知 403 的等待。
- `codex_bin` 显式传入(自测/替身)时**不做模型回退**,保持替身语义。

### 2.2 `tools/linuxdo_post_forge.py`(+52/−10)- `classify_gen_fail(why)`:把生成失败分两类 —— `GEN_ENV`(环境/上游:codex 不在、`rc=127`、403 无额度、超时)
  与 `GEN_FAIL`(真失败)。topic 生成空输出不再裸报 `NO_TOPIC`,而是 `why=GEN_ENV:<首因>` ⇒ 事后一眼可判归属。
- 新增 `gen-check` 子命令:在**当前进程环境**下真跑一次生成并打印 `rc`/`verdict`,用于 daemon 式受限 PATH 下的一键体检。
- 草稿落盘前仍必过 `linuxdo_deai.py` 闸门(不变)。

### 2.3 `tools/linuxdo_browse_daemon.py`(+43/−13)

- 新增 `_forge_env()`:惰性 `shutil.which("codex")`,把其所在目录 + `/opt/homebrew/bin` + `/usr/local/bin` 补进 `PATH`;
  `_maybe_forge_draft` 的 `Popen(..., env=_forge_env())` ⇒ 守护侧不再依赖进程继承的 PATH。
- `_forge_gate()` 读 `FORGE_LEDGER` 末行:若 `why` 以 `GEN_ENV` 开头 ⇒ 走**短退避** `FORGE_ENV_MIN_GAP_S` + 抖动,
  而不是默认 3–7 天日级冷却 —— 否则上游恢复后仍要白等数天。

## 3. 真跑前暴露的第二层:上游额度(magpie)

真跑(修 PATH 后)codex 子进程已正常起(命令行含 `-m gpt-5.6-terra`,不再是 rc=127),但最终 `rc=1`:

```
rc=1/config 403 Forbidden: sharedchat-k1: 您当前没有可用的 Codex 订阅额度或余额
(url http://127.0.0.1:3425/v1/responses)
```

- 本机 codex 走 **magpie 代理**:`~/.codex/config.toml` 的 `model="sharedchat-k1/gpt-5.6-sol"`,
  `model_provider="magpie"`,`base_url=http://127.0.0.1:3425/v1`,`wire_api="responses"`。
- magpie 进城:`magpie web --addr 127.0.0.1:7799 --no-open`,实际监听 3425;`GET /v1/models` 返 **77** 个模型 id(多池)。
- 探针(带 magpie 头并发直打 `/v1/responses`,15 模型)⇒ 仅 `4router-terra` 三档 + `hxi/gpt-5.6-sol` 为 200 OK。
- 真 CLI 复核(`codex exec -m 4router-terra/gpt-5.6-terra`)被接受:13.7s / 56s 双 OK ⇒ `-m` 吃**带 provider 前缀**的 id。

## 4. 验证(全部当场原始输出)

| 项 | 命令 | 结果 |
|----|------|------|
| acts 自测 | `python3 tools/linuxdo_acts.py --selftest` | 65/65 PASS |
| forge 自测 | `python3 tools/linuxdo_post_forge.py selftest` | 28 checks / 0 failed |
| daemon 自测 | `python3 tools/linuxdo_browse_daemon.py --selftest` | 37/37 PASS |
| 受限 PATH 体检 | daemon 式 PATH 下 `forge gen-check --gen-timeout 90` | 旧码必 rc=127;**新码 rc=0 verdict=OK** |
| 端到端真跑 | daemon 式受限 PATH 下 `forge forge --dry-run` | **rc=0 / score=100**,出稿见下 |

端到端产出(证据 `temp/r569_fix/forge_e2e_limited_path.txt`,991B):

- 标题:`SkillsBench 复盘:没做 matched experiment,结论就别下太满`
- `score=100`,`warn=[]`,`material=temp/multica_research/loopx_repo_head/docs/archive/incidents/skillsbench-goal-baseline-comparison-incident-20260711.md`
- `material_sha=90ad625e285f5045`,`draft_path=null`(dry-run 不落盘)
- 生成器走新链(`rc=0 model=<已验证 id>`),内容全中文、无分点/小标题 ⇒ 过 deai 闸门。

## 5. 残留与边界

- **F2(上游通道)**:`sharedchat-k1` 额度层 403 仍未消除;本批以「模型回退链」绕过(不依赖单一池),
  但若 4router-terra / hxi 也耗尽,生成仍会失败 ⇒ 属上游供给面,见台账 `DECISIONS_R569_post_pipeline.md` F2。
- **F3(产品语义)**:获赞恒 MET ⇒ 现实里 `post` 仍不会排队。本批**不改动**该路由条件(守「不为凑数发帖」防检测铁律)。
- 本批不新增任何定时任务、不改随机化参数;生成时刻仍由 daemon 随机段决定。
- 047:daemon 需重启才载新码(见 §6)。

## 6. 上线动作

`tools/linuxdo_acts.py` 被 daemon 进程 import ⇒ **必须重启 daemon 才能载新码**;
`tools/linuxdo_post_forge.py` 的 `gen-check` 供运维侧即席体检。运行面证明见 §4 端到端真跑。