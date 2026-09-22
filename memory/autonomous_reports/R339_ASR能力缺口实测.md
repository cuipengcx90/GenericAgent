# R339 本地语音转写（ASR）能力缺口实测

- 日期：2026-09-22
- 类型：能力缺口实测（TODO#4，评审 6/10）
- 约束：只读业务/敏感数据；不写 memory 散文（SOP 提案待审）；唯一新增代码文件 = `memory/asr_cli.py`
- 一句话结论：**前提错位**——"完全没有音频/语音输入节点"已过期（`memory/whisper_utils.py` 早已存在且全 API 实测通过）；真实缺口是**没有调用入口/SOP/模型选型依据 + 解释器绑定陷阱**。本次把这三项全部补齐并做了真机端到端验证。

## 1. 环境事实（实测）

| 项 | 实测结果 | 证据 |
|---|---|---|
| faster-whisper | **1.2.1**，仅存在于 `/opt/anaconda3/bin/python3`（Py 3.12.7） | `python3 -c "import faster_whisper"` → `1.2.1 /opt/anaconda3/bin/python3` |
| homebrew python | `/opt/homebrew/bin/python3` = 3.13.3，**无 faster-whisper** | 负控运行：`ModuleNotFoundError: No module named 'faster_whisper'` |
| ffmpeg | 8.0.1（`/opt/homebrew/bin/ffmpeg`） | `ffmpeg -version` |
| whisper.cpp | `whisper-cli` 已装（homebrew ggml 0.24.0，Metal/BLAS 后端） | 运行输出 `loaded BLAS backend` / `loaded CPU backend ... apple_m4` |
| mlx-whisper | **未安装** | `ModuleNotFoundError: No module named 'mlx_whisper'` |
| 既有能力件 | `memory/whisper_utils.py`（176 行，faster-whisper 封装：`transcribe / transcribe_to_srt / detect_language`，含模型缓存） | 本次全部函数实测调用成功 |
| 模型缓存 | `~/.cache/huggingface` 216M→新增 tiny/base/small；`temp/models/ggml-tiny.bin` 77MB | `du -sh`、`ls -l` |

**关键坑 1（最重要）**：`whisper_utils` 只能被 anaconda 解释器导入；GA 的 shell 里 `python3` 解析到 homebrew 3.13 ⇒ 直接调用必然 `ModuleNotFoundError`。任何自动化/定时任务调用 ASR 必须写死 `/opt/anaconda3/bin/python3`。

## 2. 真音频端到端（夹具=真实语音信号，真值可控）

夹具用 macOS `say` 合成真实人声语音（非噪声/静音样例），**真值逐字已知**，可精确量化误差：

- zh 短句真值：`今天的会议改到下午三点，请记得带上销售数据报表。`
- zh 长句真值：`三月十五号下午两点半，请把第三季度的营收报表发给我，重点看华东区的数据。另外，下周一的项目评审改到线上，链接我稍后发到群里。`
- en 真值：`The quick brown fox jumps over the lazy dog.`

### 2.1 实测结果表

| # | 夹具 | 后端/模型 | 输出（原文） | 与真值差异 | 耗时 |
|---|---|---|---|---|---|
| 1 | zh 短句 aiff | faster-whisper tiny (int8,CPU) | `今天的會議改到下午3點 請記得帶上銷售數據報表` | 繁体；数字写作 3 | 0.587s |
| 2 | zh 短句 **m4a** | faster-whisper tiny | `今天的會議改到下午3點 請記得帶上銷售數據報表` | 同上；**m4a 直接可读** | 0.505s |
| 3 | zh 短句 aiff | faster-whisper **small** | `今天的会议改到下午三点,请记得戴上销售数据报表。` | **简体+数字规范**，1 处同音错字（带→戴） | 2.704s |
| 4 | zh 长句 wav | faster-whisper tiny | `3月15號下午2點半 請把第三劑度的營收報表發給我 重點看華東區的數據 另外 下週一的項目評審改到現實 鏈接我稍後發到群理` | 繁体 + **3 处错字**（季→劑、线上→現實、群里→群理） | 0.669s |
| 5 | zh 长句 wav | faster-whisper **base** | `3月15日下午2點半,請把第三季度的營收報表發給我,…項目評審改道線上,鏈接我稍後發道群裡。` | 繁体 + 2 处错字（到→道 ×2） | 1.254s |
| 6 | zh 长句 wav | faster-whisper **small** | `3月15号下午2点半,请把第三季度的营收报表发给我,重点看华东区的数据。另外,下周一的项目评审改到线上,链接我稍后发到群里。` | **0 错（简体/标点/数字全对）** | 3.828s |
| 7 | en 短句 aiff | faster-whisper tiny / small | 均 `The quick brown fox jumps over the lazy dog.` | 0 错 | 0.337s / 2.071s |
| 8 | zh 短句 16k wav | **whisper.cpp tiny (ggml, Metal)** | `今天的會議改到下午3點請記得帶上銷售數據報表` | 繁体、无标点 | total 722ms / real 1.1s |
| 9 | en 16k wav | whisper.cpp tiny | ` The quick brown fox jumps over the lazy dog.` | 0 错 | — |

### 2.2 其他 API 实测

- `detect_language('asr_test_long.wav')` → `('zh', 0.9972)` ✅
- `transcribe_to_srt(...)` → 时间轴正确、分句合理 ✅
  ```
  1
  00:00:00,000 --> 00:00:08,199
  3月15号下午2点半,请把第三季度的营收报表发给我,重点看华东区的数据。

  2
  00:00:08,199 --> 00:00:14,300
  ```

## 3. 模型选型建议（由实测反推，非文档抄录）

1. **中文默认 `small`**：唯一 0 错配置；15s 内音频 CPU int8 约 2.4–3.8s，完全可交互。
2. `tiny`：快 5 倍但**系统性输出繁体 + 同音错字（长句 3 错）** ⇒ 仅适合"要极快、可接受繁体、后续人工/LLM 纠错"的场景。
3. `base`：**不划算**——比 tiny 慢一倍、仍繁体且仍有错字，精度不及 small。
4. `whisper.cpp`：本机已装且可用（Metal 加速，1s 内），但需 ggml 模型文件、繁体倾向与 tiny 同级；**结论=保底备选，主力用 faster-whisper small**。
5. 英文：tiny 即 0 错，`--lang en` 显式指定可省语言检测。

## 4. 微信语音：为什么没拿真机语音做夹具（环境阻塞点）

- 微信 4.x 容器 `~/Library/Containers/com.tencent.xinWeChat/.../xwechat_files/wxid_.../msg/` 下**只有 `attach / file / migrate / video`**；
- `attach/<hash>/<date>/` 子目录直方图（抽样 120 仓）：`Img 638 / Ann 45 / Rec 10`，**无 Voice 目录**；
- 全容器 `*.silk|*.aud|*.amr` 文件计数 = **0**；
- **关键坑 2**：该容器内 `find -type f` 计数 = **0**（macOS TCC 权限对 `find` 生效），而 `ls` 正常 → **排查微信目录必须用 `ls`/`ls -la`，用 `find` 会得到"空目录"假象**（本次首轮曾因此误判）。
- 结论：本机微信未在本机留存语音消息（或在加密 `db_storage` 中），**"一段微信语音"夹具不可得**——这是明确的环境阻塞点，不是能力缺陷。
- 替代方案：`say -v Tingting` 合成真实语音信号 + 已知真值（见 §2）。
- **局限（诚实标注）**：TTS 干净语音，无背景噪声/口音/远场/语速抖动；真实微信语音精度**预期低于**本表（建议后续用真实录音复测）。另：微信 `.silk` 格式 ffmpeg 不原生支持，真实链路需先过 silk-v3-decoder。

## 5. 交付物

1. **`memory/asr_cli.py`（37 行，核心复用函数 `to_wav` + `transcribe_any` 共 13 行）** —— 格式无关薄封装：非 wav 自动 ffmpeg 转 16k 单声道，支持 `--model/--lang/--srt`。真机回归 4 例全通过：
   - `asr_test_zh.m4a --srt cli_test2.srt` → `[zh 0.9979 | 2.377s] 今天的会议改到下午三点,请记得戴上销售数据报表。` + SRT 正常
   - `asr_test_long.aiff --model small` → 长句 0 错
   - `en.m4a --lang en` → `[en 1 | 1.262s] The quick brown fox jumps over the lazy dog.`
   - 负控：homebrew python3.13 调用 → 明确 `ModuleNotFoundError`（已写入封装注释）
2. 本报告 §6 = **L3 SOP 提案（待审，未落盘）**。

## 6. L3 SOP 提案（`memory/asr_sop.md`，待用户批准）

1. **解释器写死** `/opt/anaconda3/bin/python3`（faster-whisper 只在此环境）；homebrew python 会 `ModuleNotFoundError`。
2. **默认模型 `small`**；`tiny` 仅用于"极快+可接受繁体"；**不要用 base**。
3. 输入格式不必预处理：faster-whisper 可直吃 m4a/aac；需要 wav 时用 `ffmpeg -ar 16000 -ac 1`（`asr_cli.py` 已自动）。
4. 微信 `.silk` 语音：ffmpeg 不支持 → 先 silk-v3-decoder 转 wav；**排查微信目录用 `ls` 不用 `find`**（TCC 使 find 返回空）。
5. 长音频(>10min)或批量：`nohup` 后台 + 日志轮询（禁 sleep 阻塞，遵循 RULES#15）。
6. 隐私：推理全本地离线；仅首次下载模型走网络。
7. 纠错：中文 tiny/whisper.cpp 输出繁体属**已知系统性行为**，后接繁→简转换或 LLM 纠错可闭环。

## 7. 记忆更新提案（待审）

1. **L1 索引新增**（建议行）：`语音/音频: whisper_utils.py/asr_cli.py(faster-whisper, 解释器必须 /opt/anaconda3/bin/python3; 中文默认 small)`
2. **L2/global_mem 新增事实**：faster-whisper 仅存于 anaconda python；homebrew python3.13 无。
3. **修正表述**：TODO/报告中"完全没有任何音频/语音输入节点"→"节点已存在（whisper_utils.py），缺的是入口+SOP+选型依据（R339 已补）"。
4. **环境坑入库**：微信容器内 `find -type f` 恒为 0（TCC），须用 `ls`。

## 8. 零副作用 / 边界核验

- 唯一 memory 写入 = **新建** `memory/asr_cli.py`（未改任何既有文件；SOP 提案与记忆更新均**未落盘**，待批）。
- 夹具与日志全部落在 `temp/`：`asr_test_{zh,en,long}.{aiff,wav,m4a}`、`asr_run{1,2,3,4}.log`、`models/ggml-tiny.bin`。
- 未触碰业务仓；未修改 memory 散文。
- 未验证/残留：真实微信语音（.silk）链路、噪声/口音场景精度、>10min 长音频吞吐 —— 均已在 §4/§6 标注为待验证项。
