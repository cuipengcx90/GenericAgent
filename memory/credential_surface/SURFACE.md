# 凭证面映射与失效判据表（credential surface）

> 生成方式：由 `tools/credential_surface.py` 的声明面 + judge JSON **机械生成**（本文件不含任何凭证值）。
> 口径快照：2026-09-28 11:55:12（本机 GA 仓；快照以外不作判据）

## 1. 汇总
- 声明条目 **25** 条；显式排除 **12** 条；coverage 扫到凭证 token 事件 **35** 处，未声明 **0** 处
- 判定分布：OK 14、OPTIONAL_MISSING 11
- 硬问题 hard=0、hard_other=0、UNVERIFIABLE=0、泄漏守卫 hits=0 ⇒ `judge` rc=0 PASS
- 存储种类：`browser_cookie`×3、`config_var`×13、`env_var`×4、`ga_keychain_name`×2、`macos_keychain_item`×3

## 2. 凭证 → 消费者映射表

| # | 凭证 ID | 存储位置（locator） | 消费者 | 状态 | 判定 | 备注（提示级 issue） |
|---|---------|--------------------|--------|------|------|---------------------|
| 1 | `gc_haisou_session` | ga_keychain_name (name=haisou_hs_web_session) | birdie_monitor/haisou_daily.py | PRESENT | OK | — |
| 2 | `gc_typesafe_jev` | ga_keychain_name (name=typesafe_jev_api_key) | ~/develop/herdr-agents/control-plane/acceptance_v0/channel_probe.py | PRESENT | OK | EXTERNAL_UNCHECKED |
| 3 | `mk_claude_code` | macos_keychain_item (name=Claude Code-credentials) | ~/.claude (Claude Code CLI) | PRESENT | OK | EXTERNAL_UNCHECKED, EVIDENCE_NONE |
| 4 | `mk_genericagent` | macos_keychain_item (name=GenericAgent) | macOS keychain 通用条目（用途未登记） | PRESENT | OK | EXTERNAL_UNCHECKED, EVIDENCE_NONE |
| 5 | `mk_typesafe_jev_absent` | macos_keychain_item (name=typesafe_jev_api_key) | ~/develop/herdr-agents/control-plane/acceptance_v0/channel_probe.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EXTERNAL_UNCHECKED |
| 6 | `mv_fs_app_id` | config_var (path=mykey.py, name=fs_app_id) | frontends/fsapp.py；reflect_scripts/reflect_common.py | PRESENT | OK | EVIDENCE_NONE |
| 7 | `mv_fs_app_secret` | config_var (path=mykey.py, name=fs_app_secret) | frontends/fsapp.py；reflect_scripts/reflect_common.py | PRESENT | OK | EVIDENCE_NONE |
| 8 | `mv_dingtalk_client_id` | config_var (path=mykey.py, name=dingtalk_client_id) | frontends/dingtalkapp.py；reflect_scripts/notify_dingtalk.py | PRESENT | OK | EVIDENCE_NONE |
| 9 | `mv_dingtalk_client_secret` | config_var (path=mykey.py, name=dingtalk_client_secret) | frontends/dingtalkapp.py；reflect_scripts/notify_dingtalk.py | PRESENT | OK | EVIDENCE_NONE |
| 10 | `mv_tg_bot_token` | config_var (path=mykey.py, name=tg_bot_token) | frontends/tgapp.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 11 | `mv_discord_bot_token` | config_var (path=mykey.py, name=discord_bot_token) | frontends/dcapp.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 12 | `mv_qq_app_id` | config_var (path=mykey.py, name=qq_app_id) | frontends/qqapp.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 13 | `mv_qq_app_secret` | config_var (path=mykey.py, name=qq_app_secret) | frontends/qqapp.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 14 | `mv_wecom_bot_id` | config_var (path=mykey.py, name=wecom_bot_id) | frontends/wecomapp.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 15 | `mv_wecom_secret` | config_var (path=mykey.py, name=wecom_secret) | frontends/wecomapp.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 16 | `mv_llm_apikey` | config_var (path=mykey.py, name=apikey) | llmcore (import mykey) | PRESENT | OK | EXTERNAL_UNCHECKED, EVIDENCE_NONE |
| 17 | `mv_oai_configs` | config_var (path=mykey.py, name=native_oai_config) | llmcore NativeOAISession | PRESENT | OK | EXTERNAL_UNCHECKED, EVIDENCE_NONE |
| 18 | `mv_claude_configs` | config_var (path=mykey.py, name=native_claude_config2) | llmcore NativeClaudeSession | PRESENT | OK | EXTERNAL_UNCHECKED, EVIDENCE_NONE |
| 19 | `ev_dpdomain_password` | env_var (name=DPDOMAIN_PASSWORD) | tools/dpdomain_renew.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL |
| 20 | `ev_t0_passphrase` | env_var (name=T0_PASSPHRASE) | tools/t0_snapshot.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL |
| 21 | `ev_e2e_control_token` | env_var (name=GA_E2E_CONTROL_TOKEN) | frontends/desktop_bridge.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 22 | `ev_hub_token` | env_var (name=GA_HUB_TOKEN) | frontends/hub.py | MISSING | OPTIONAL_MISSING | STORE_MISSING_OPTIONAL, EVIDENCE_NONE |
| 23 | `bc_linuxdo_t` | browser_cookie (profile=Default, host=linux.do, name=_t) | TMWebDriver 同源 fetch /search.json | PRESENT | OK | EXTERNAL_UNCHECKED |
| 24 | `bc_connect_session` | browser_cookie (profile=Default, host=connect.linux.do, name=auth.session-token) | 定时任务 linuxdo_trust_level_check | PRESENT | OK | EXTERNAL_UNCHECKED |
| 25 | `bc_x_auth_token` | browser_cookie (profile=Default, host=.x.com, name=auth_token) | x_ai_intel_daily 抓取（浏览器实体标签） | PRESENT | OK | EXTERNAL_UNCHECKED |

## 3. 失效症状 → 探测器判据表

判据 = 该条的**存储探针**（只判存在性/名字，绝不读值）+ **证据行回源**（下表 evidence 必须逐字命中）。

| 凭证 ID | 症状（observed） | 证据行（path:line，须含 must_contain） |
|---------|-----------------|----------------------------------------|
| `gc_haisou_session` | 会员会话 Cookie(HttpOnly)；过期/缺失⇒抓取 0 条、403 或登录页 | memory/haisou_monitor_sop.md:9（`hs_web_session`）；memory/haisou_monitor_sop.md:98（`403`） |
| `gc_typesafe_jev` | Jev 裁判渠道 key；缺失⇒channel_probe reach 失败、judge --require-probe rc=3 | memory/keychain_sop.md:79（`typesafe_jev_api_key`） |
| `mk_claude_code` | Claude Code CLI 登录凭据；缺失⇒CLI 要求重新登录 | — |
| `mk_genericagent` | 用途未登记：存在但无消费者映射（待补） | — |
| `mk_typesafe_jev_absent` | 跨存储命名混淆：该名在自建 keychain 在册、macOS keychain 无此条目（rc=44） | memory/keychain_sop.md:79（`typesafe_jev_api_key`） |
| `mv_fs_app_id` | 飞书自建应用 App ID；缺失⇒fsapp/reflect 通知 401/权限错 | — |
| `mv_fs_app_secret` | 飞书自建应用 Secret；缺失⇒发消息 401 | — |
| `mv_dingtalk_client_id` | 钉钉应用 client_id；缺失⇒钉钉通道 401 | — |
| `mv_dingtalk_client_secret` | 钉钉应用 client_secret；缺失⇒钉钉通道 401 | — |
| `mv_tg_bot_token` | Telegram bot token；缺失⇒TG 通道 401 | — |
| `mv_discord_bot_token` | Discord bot token；缺失⇒DC 通道 401 | — |
| `mv_qq_app_id` | QQ 机器人 AppID；缺失⇒QQ 通道失败 | — |
| `mv_qq_app_secret` | QQ 机器人 Secret；缺失⇒QQ 通道 401 | — |
| `mv_wecom_bot_id` | 企微 bot id；缺失⇒企微通道失败 | — |
| `mv_wecom_secret` | 企微 secret；缺失⇒企微通道 401 | — |
| `mv_llm_apikey` | LLM 主 key；缺失⇒会话 401/AUTH（对应槽位探活 AUTH 类） | — |
| `mv_oai_configs` | OpenAI 兼容槽位配置族（native_oai_config{,2..6}）；缺失⇒对应槽 AUTH | — |
| `mv_claude_configs` | Claude 槽位配置族（native_claude_config2/3/4）；缺失⇒对应槽 AUTH | — |
| `ev_dpdomain_password` | 域名续期密码（密码登录兜底路径）；缺失⇒绝不发登录请求、直接判恢复失败 rc=6 | memory/dpdomain_renew_sop.md:34（`DPDOMAIN_PASSWORD`）；memory/dpdomain_renew_sop.md:35（`rc=6`） |
| `ev_t0_passphrase` | T0 备份加密口令（进程环境注入，不出现在命令行）；缺失⇒加密/校验失败 | memory/autonomous_reports/R381_T0备份耐久化二期.md:158（`T0_PASSPHRASE`） |
| `ev_e2e_control_token` | E2E 控制面令牌；未登记成文症状（仅代码消费者） | — |
| `ev_hub_token` | Hub 通道令牌；未登记成文症状（仅代码消费者） | — |
| `bc_linuxdo_t` | 论坛登录态（Discourse `_t`）；缺失⇒search.json 403 not_logged_in、/t/<id>.json 匿名 404 | memory/linuxdo_deep_read_sop.md:155（`not_logged_in`）；memory/linuxdo_deep_read_sop.md:156（`_t`） |
| `bc_connect_session` | connect 独立会话（与论坛登录态两套，不可互推）；缺失⇒TL3 检查读不到个人卡 | memory/linuxdo_deep_read_sop.md:157（`auth.session-token`） |
| `bc_x_auth_token` | X(Twitter) 登录态；缺失⇒驱动会话恒 0、done 报告 Saved 命中 0 | memory/x_ai_intel_daily_sop.md:18（`x.com`） |

## 4. 显式排除面（非凭证）

| token | 排除理由 |
|-------|---------|
| `GA_KEYSYM_STYLE` | 非凭证：终端键位风格开关 |
| `TERM_SESSION_ID` | 非凭证：终端会话标识（环境自动生成） |
| `WT_SESSION` | 非凭证：Windows Terminal 会话标识（环境自动生成） |
| `name` | 误报：memory/keychain.py:1 docstring 内的 keys.name 示例 |
| `proxy` | 非凭证：网络代理地址配置 |
| `fs_allowed_users` | 非凭证：飞书通道白名单 |
| `dingtalk_allowed_users` | 非凭证：钉钉通道白名单 |
| `tg_allowed_users` | 非凭证：TG 通道白名单 |
| `discord_allowed_users` | 非凭证：Discord 通道白名单 |
| `qq_allowed_users` | 非凭证：QQ 通道白名单 |
| `wecom_allowed_users` | 非凭证：企微通道白名单 |
| `wecom_welcome_message` | 非凭证：企微欢迎文案 |

## 5. 用法与边界

```bash
python3 tools/credential_surface.py plan     --decl memory/credential_surface/credentials.json  # 只跑探针
python3 tools/credential_surface.py judge    --decl memory/credential_surface/credentials.json  # 四闸门 + 泄漏守卫，rc 0/1/2
python3 tools/credential_surface.py coverage --decl memory/credential_surface/credentials.json  # 仓内凭证 token 与声明面对账
python3 tools/credential_surface.py proof    --decl memory/credential_surface/credentials.json  # 只读自证（文件 sha 不变）
python3 tools/credential_surface.py selftest                                                  # 20 项（含 16 类负控）
```

- rc 语义：`0` 全绿；`1` 有硬问题（STALE_PATH/TOKEN_GONE/EVIDENCE_GONE/EVIDENCE_LINE_DRIFT/UNDECLARED/DECLARED_FICTIONAL）；`2` fail-closed（UNVERIFIABLE：探针不可判/声明面或 fixture 缺失）；`3` 声明结构错误。
- 存储探针：`ga_keychain_name`（`memory/keychain.py` 的 `keys.ls()` 名字表）、`macos_keychain_item`（`security find-generic-password -s <name>` **无** `-g`，不出值；rc=44 ⇒ 不存在）、`config_var`（`mykey.py` **AST 只取顶层变量名**，不读取值）、`env_var`（只判 `os.environ` 中是否存在该键）、`browser_cookie`（Chrome Cookies 只读副本，`file:...?mode=ro`，**只判 cookie 名存在性**）。
- 泄漏守卫：所有输出经疑似密钥值扫描，命中即 rc 非 0；声明面自身在 `--selftest` 中用 `FAKE-*` 合成值，不触碰真密钥。

## 6. 未决 / 待确认（提示级，不参与判定）

- `mk_genericagent`（macOS 钥匙串条目，存在）：**未登记任何消费者**，用途未明 ⇒ 待确认可删或补用途。
- 6 条 `config_var`（tg/discord/qq×2/wecom×2）本机 `mykey.py` 未配置：四家 IM 通道**未启用**，消费者走 `require_runtime(...)` 明确提示（非崩溃）⇒ 记 `OPTIONAL_MISSING`。
- 4 条 `env_var`（DPDOMAIN_PASSWORD/T0_PASSPHRASE/GA_E2E_CONTROL_TOKEN/GA_HUB_TOKEN）本机环境未设置 ⇒ 对应能力需显式提供 env 才可用；其中 2 条无成文失效症状（`EVIDENCE_NONE`）。
- `EXTERNAL_UNCHECKED` = 消费者在仓外（llmcore / herdr-agents channel_probe / TMWebDriver 同源 fetch / 定时任务 / 浏览器实体标签）⇒ 本器只登记不判。
