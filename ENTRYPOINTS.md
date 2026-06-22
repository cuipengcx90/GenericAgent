# GenericAgent 入口速查

> 入口文件很多，但大多是**旧版本**或**被主入口调起的内部模块**。本表只保留每类唯一推荐入口，其余忽略即可。
> 结论依据：README 官方推荐 + 代码/端口实测（2026-06-04）。

## 日常只需 2 个

```bash
python frontends/desktop_pet_v2.pyw   # 桌宠常驻，被动收任务通知
python frontends/tuiapp_v2.py         # 终端干活，下指令
```

## 主入口（每类选一个）

| 用途 | ✅ 唯一推荐 | 启动命令 |
| :--- | :--- | :--- |
| 终端 TUI | `tuiapp_v2.py`（Textual，README 推荐基线） | `python frontends/tuiapp_v2.py` |
| 桌面宠物 | `desktop_pet_v2.pyw`（皮肤系统 + HTTP 控制） | `python frontends/desktop_pet_v2.pyw` |
| 网页界面 | `launch.pyw`（Streamlit） | `python launch.pyw` |
| IM 机器人 | 按平台**只启一个** | 见下表 |
| 编辑器接入 | `genericagent_acp_bridge.py`（ACP，如 Zed） | 由编辑器拉起 |

### IM 平台（按需启一个）

| 平台 | 入口 |
| :--- | :--- |
| 飞书 / Lark | `python frontends/fsapp.py` |
| 钉钉 | `python frontends/dingtalkapp.py` |
| 企业微信 | `python frontends/wecomapp.py` |
| 微信 | `python frontends/wechatapp.py` |
| QQ | `python frontends/qqapp.py` |
| Telegram | `python frontends/tgapp.py` |

## 忽略清单（旧版 / 底层模块，不用记）

| 文件 | 为什么忽略 |
| :--- | :--- |
| `tuiapp.py` | TUI v1，已被 v2 取代 |
| `tui_v3.py` | scrollback 范式的平行实验，仍在 "v2 parity"（追平 v2），未进官方文档 |
| `desktop_pet.pyw` | 桌宠 v1，90 行教学样例 |
| `stapp.py` / `stapp2.py` | 被 `launch.pyw` 调起的 Streamlit 底层 |
| `slash_cmds.py` / `*_cmd.py` / `session_names.py` / `cost_tracker.py` 等 | 各前端共享的命令/会话/成本内部模块 |

## 桌宠 HTTP 远程控制（实测端口 41983）

> 注意：实际监听 **41983**，与 `DESKTOP_PET_README.md` 文档里写的 51983 不一致，以实际为准。

```bash
curl "http://127.0.0.1:41983/?msg=hello"                 # 弹消息（英文）
curl "http://127.0.0.1:41983/?msg=%E4%BD%A0%E5%A5%BD"    # 弹消息（中文必须 URL 编码，否则 400）
curl "http://127.0.0.1:41983/?state=run"                 # 切动画：idle / walk / run / sprint
curl -X POST -d "task done" "http://127.0.0.1:41983/"     # POST 消息
```

## 选型三原则

1. **同类只用最新稳定版**：桌宠用 v2、TUI 用 v2（注意：TUI 的 v3 是新范式实验，非升级版，默认别用）。
2. **IM 前端按需启一个**：用哪个平台就启哪个，不用的别开，省资源。
3. **桌宠 + 一个主力交互端**足够：常驻桌宠收通知 + TUI/飞书 下指令，不需要全家桶。
