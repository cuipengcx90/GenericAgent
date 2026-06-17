# CC 伪装知识（源自 Sophub SOP 69f2883）

> 本文件为学习记录，本机无实际部署。若需重建参考此处 + sophub原文。

## 架构
- proxy (Flask, PORT=5678) 拦截 GA → Anthropic /v1/messages，补齐 CC 特征后透传
- 核心文件: proxy.py + tool_name_mapping.json + 真CC MITM capture样本
- GA 入站工具名不变，proxy 出站伪装 CC，回程映射 CC工具名→GA工具名

## GA 9工具 → CC 映射口径
| GA | CC |
|---|---|
| code_run | Bash |
| file_read | Read |
| file_patch | Edit |
| file_write | Write |
| web_scan | WebFetch |
| web_execute_js | WebSearch |
| update_working_checkpoint | TodoWrite |
| ask_user | AskUserQuestion |
| start_long_term_update | TaskUpdate |

## 伪装关键点
- Headers: UA=claude-cli/{version}, X-Stainless-*, anthropic-beta(长串), X-Claude-Code-Session-Id
- CCH: seed=0x6E52736AC806831E, 基于最终body字节计算写入x-claude-code-client-sha256
- System: 三段结构 [CC_sys0, CC_sys1, CC_sys2+GA_english_SP]
- Session: 进程启动生成一次UUID，会话内复用，header与metadata.user_id.session_id同步
- Body归一: model/max_tokens/thinking(adaptive)/context_management/output_config.effort=max

## 排错经验（高价值）
- 工具alias必须一对一唯一；历史旧名(sessions_*)只做回程归一化不暴露
- 普通JSON与SSE流式chunk都要结构化remap tool_use name
- proxy与llmcore两条路径要同时维护映射
- Bash→code_run: command→script, 补type/cwd; Read→file_read: file_path→path, offset→start
- 改后必须: py_compile + 离线映射测试 + 精准重启验证端口

## 已知Gap
- UA suffix不固定，不当唯一真值
- billing header无真CC证据前不补
- stream true/false单独非异常
- tools数量可与CC不同，只要映射闭环
- account_uuid保留GA入站原值，只覆盖device_id
