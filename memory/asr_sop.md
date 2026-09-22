# ASR 批量转写 SOP（本机 faster-whisper，2026-09-22 R353 实测）

## 入口与硬前置
- 解释器**必须写死** `/opt/anaconda3/bin/python3`（faster-whisper 1.2.1 只装在 anaconda，系统 python 会 ImportError）。
- 单文件：`/opt/anaconda3/bin/python3 memory/asr_cli.py <音频> [--model small] [--lang zh] [--srt out.srt]`
- 批量：`memory/whisper_utils.py` 的 `transcribe(path, model_size, language)` / `batch_transcribe(...)`；`compute_type=int8`、CPU 多线程。
- 支持 wav/aiff/m4a/mp3/mp4/flac/ogg（非 wav 自动 ffmpeg → 16k 单声道）。**微信 .silk 不认**，走 `tools/silk_to_text.py`。

## 实测性能（macOS 本机，model=small，int8）
- **≈4.4× 实时**：7612s（2.11h）音频 → 墙钟 **1735.5s（28.9 min）**。
- 长批处理**必须** `nohup ... > log 2>&1 &` 后台跑 + 后续 `ps -p`/`tail` 轮询（GA 工具 60/300s 超时会把前台长任务杀掉）。
- 断点续跑：逐条落 `<out>/asr/<stem>.json` + `progress.jsonl` + 已完成 skip（本次曾被前台重启掐死，靠此恢复）。

## 结果可信性判据（★幻觉识别）
- **无效判定**：`chars==0` 或（`conf<0.6` 且 `字数/时长 < 0.05`）⇒ 判「非语音/幻觉」，不得当内容引用。
- 已实测幻觉样本：53.8s 音频（`.MP3` 与 `.mov`）**输出完全相同的 7 字**、conf 0.51~0.54；508s 音频只出 37 字 conf 0.52。
- 本批 25 条结果里 **13 条零文本（52%）**、12 条出文本中 3 条为幻觉 ⇒ 无脑「转完就汇报」会有一半噪音。
- whisper 输出**繁简不稳**：同一模型短音频出繁体、长音频出简体；比对/引用前做繁转简或人工读。

## silero VAD 预筛（可选，不能单独定音轨有无）
- 26 条 7612s 全扫 **29.6s（≈257× 实时，成本仅 ASR 的 1.7%）**，`speech_ratio<0.03` 的 13 条占 2915s ⇒ 可用于**排序/抽样复核**省算力。
- ★**反例（must）**：`speech_ratio=1.000` 与 `0.541` 的两条 ASR **0 字**；另 `0.037~0.041` 的低占比条目反而输出了幻觉文本 ⇒ **禁止**用 VAD 单独判「有/无语音」，只能「低分 → 抽样复核」。

## 其它坑
- 源文件路径**勿凭直觉拼**（本次 `~/Downloads` 猜测致交叉校验失败一次）；路径来自 `tools/audio_asset_inventory.py --bases <CSV 目录>`，先拷副本（源可能被清理）。
- 去重口径：全字节 sha256（同一视频的 `.mp4`/`.MP3` 副本会重复转写，先判重）。
- macOS **无 `timeout` 命令**；需要超时用 python/`perl -e alarm` 或后台+轮询。
