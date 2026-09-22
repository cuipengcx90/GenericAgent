# R345 微信语音 `.silk` 解码 → wav → 转写 全链路（TODO#4，评审 5/10）

> 日期：2026-09-22　｜　任务：补齐 R339 的「微信语音夹具空缺」　｜　性质：**能力补齐（非泛采集）**
> 结论一句话：**端到端已打通，用公开真实 `.silk` 样本实证**（解码 → 24k mono wav → 转写文本），并交付一键复现脚本；
> 同时用三处独立证据确认「本机微信 4.x 容器内确实没有语音文件」这一侧的前提仍然成立。

---

## 1. 验收对照

| TODO 验收要求 | 结果 |
|---|---|
| ① 从公开仓获取**真实** `.silk` 样本并完成端到端 | ✅ 命中 `github.com/fishCoder/Silk_v3_decoder@d888a4b5`：`app/src/main/assets/{hello,test}.silk`（已钉 commit + sha256 双校验） |
| ② 给出解码前后文件大小 / 时长 / 转写文本 | ✅ 见下表（含 wav 头、时长与 PCM 字节数三方自洽） |
| ③ 无样本时给 3 处独立探测证据 + 一键脚本 | 样本已拿到；**仍额外给出**三处独立证据（§4），因为 TODO 的另一半是"本机有没有语音" |
| 红线：禁止泛采集与伪造样本 | ✅ 未扫描/未下载任何非样本数据；工具在头字节不符时**直接拒绝转换**（已负控实测） |

---

## 2. 实证数据（本机 macOS，2026-09-22）

| 样本 | .silk | 头魔数 | PCM | WAV | 时长 | 转写文本（faster-whisper tiny） |
|---|---|---|---|---|---|---|
| `hello.silk` | 3 615 B | `\x02#!SILK_V3` | 94 080 B | 94 124 B | **1960 ms** | **「你好」**（lang=zh, p=0.32） |
| `test.silk` | 2 517 B | `\x02#!SILK_V3` | 70 080 B | 70 124 B | **1460 ms** | **"It's up to you."**（lang=en, p=0.61） |

自洽校验（三条互相印证，非单点）：
1. `pcm_bytes / 2 / 24000` = 1.960 s / 1.460 s ⇔ 独立 API `pilk.get_duration()` = 1960 / 1460 ms ⇔ whisper 段边界 `0.0→2.0 s`；
2. wav 头 = 44 B 标准头（`RIFF…WAVEfmt` 16bit/mono/24000），`wav = pcm + 44`；
3. **语义一致性**：文件名 `hello` 的样本转出「你好」（而非乱码噪声）——解码链正确的强证据（错误解码会产出语义垃圾）。

转换链：`.silk --pilk.decode(pcm_rate=24000)--> PCM --wave--> WAV --whisper_utils.transcribe--> text`
（本机 ffmpeg **不带** silk 解码器，实测 `ffmpeg -decoders | grep -c silk` = **0**，故不走 ffmpeg。）

---

## 3. 一键复现（交付物）

```bash
# 从零复现（会自动下载样本、校验 sha256、解码、转写）
bash tools/silk_repro.sh /tmp/silk_repro tiny
```
* 脚本：`tools/silk_repro.sh`（耐久版，随 git 走；`temp/silk_fixture/repro.sh` 是同内容临时副本，因 `temp/` 不入 git）（钉死 commit `d888a4b51ed0840da4db6b26820acfb0df8c6cce`，sha256 前缀不符即 `exit 2`）
* 工具：`tools/silk_to_text.py`（可复用于将来拿到任意 `.silk`）
  * `decode a.silk [-o DIR]` → silk→pcm→wav，打印字节数/时长
  * `text a.wav|a.silk [-m tiny] [--lang zh]` → 转写（给 `.silk` 会自动先解码）
  * `batch DIR [-m tiny]` → 目录内全部 `.silk` 逐条转写（单文件失败不中断）
  * `probe DIR` → **只读**统计 `.silk` 数量/大小分布（不复制、不写入）
* 干净目录实测：`rc=0`，输出与 §2 基线**逐字段一致**（干净复现通过，非"本目录侥幸"）。
* 负控（纪律有效性）：对随机字节文件调用 `decode` → `拒绝/失败 -> …不是 silk 文件（头=6b7f0def…）`，`rc=3`，不产出任何 wav。

---

## 4. 三处独立证据：本机「没有微信语音文件」这半仍成立

| # | 证据类型 | 命令/方法 | 结果 |
|---|---|---|---|
| 1 | 目录结构 | `ls <容器>/xwechat_files/wxid_*/msg/` | 仅 `attach / file / migrate / video`；**无 `voice`/`audio` 目录** |
| 2 | 限时全量遍历 | Python `os.walk`（45 s deadline，避 TCC 挂起）统计 `*.silk/*.amr/*.slk/*.aud` | **0 个** |
| 3 | 命令行能力面 | `which silk_v3_decoder`（空）、`ffmpeg -decoders \| grep -ci silk`（**0**） | 无独立 silk CLI；解码只能靠 Python `pilk` |

## 5. 纠正 R339 的两条前提（避免后人误判）

* ❌ "本机无 silk 解码器" → ✅ 本机**早已装** `pilk 0.2.4`（含 C 扩展 `_pilk`，**decoder 与 encoder 都可用**，路径 `/opt/anaconda3/lib/python3.12/site-packages/pilk`）。R339 的"缺夹具"实为**缺样本**，不是缺解码器。
* ⚠️ `pilk.decode()` 的**返回值不可信**（实测返回 `2` / `1` ms）；取时长必须用 `pilk.get_duration()`。

---

## 6. 边界与未做（诚实声明）

* 未从**本机微信**解出语音（本机确无 `.silk`；4.x 消息库需密钥，本轮不碰）；
* 未做 silk 编码/回环（`pilk.SilkEncoder` 可用，但**自造样本不能当证据**，故未用于验收）；
* 转写用的是 `tiny` 模型（中文 1.96 s 短句 p=0.32，语义正确但置信度不高；需要更准时换 `small`/`medium`）；
* 未测 8k/16k 采样率的微信常见变体（本样本为 24k；`silk_to_text.py` 已参数化 `pcm_rate`，遇到变体需按时长自洽校验）。

## 7. 复用姿势（将来只要有 `.silk`）

```bash
python tools/silk_to_text.py probe <语音目录>          # 先只读摸底
python tools/silk_to_text.py batch <语音目录> -m small  # 再批量转写
```
