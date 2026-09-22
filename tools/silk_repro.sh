#!/usr/bin/env bash
# repro.sh — 一键复现「微信语音 .silk → wav → 文字」全链路（R345 / TODO#4 交付物）
#
# 真实样本来源（公开仓，钉死 commit，非自造）：
#   repo   : github.com/fishCoder/Silk_v3_decoder
#   commit : d888a4b51ed0840da4db6b26820acfb0df8c6cce
#   path   : app/src/main/assets/{hello,test}.silk
# 校验：下载后比对 sha256 前缀，不符即中止（防样本被替换/污染）
#
# 用法：bash tools/silk_repro.sh [WORKDIR] [MODEL]   # 默认 WORKDIR=<GA>/temp/silk_fixture, MODEL=tiny
#       （原 temp/silk_fixture/repro.sh 为同一脚本的临时副本；temp/ 不入 git，此份才是耐久版）
# 产物：WORKDIR/{hello,test}.silk、WORKDIR/out/{hello,test}.{pcm,wav}、逐行 text 输出
set -euo pipefail

WORKDIR="${1:-$GA/temp/silk_fixture}"
MODEL="${2:-tiny}"
REPO="fishCoder/Silk_v3_decoder"
COMMIT="d888a4b51ed0840da4db6b26820acfb0df8c6cce"
GA="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 本脚本位于 <GA>/tools/
PY="${PY:-python}"

# 实测 sha256 前缀（16 hex）；注意：macOS 自带 bash 3.2 不支持 `declare -A`，故用 case 而非关联数组
expect_sha() {
  case "$1" in
    hello.silk) echo 27442a99adab555a ;;
    test.silk)  echo b43ab9df5bee48a2 ;;
    *) echo "" ;;
  esac
}

mkdir -p "$WORKDIR"
echo "== 1/3 拉取真实样本 (${REPO}@${COMMIT:0:8}) =="
for n in hello.silk test.silk; do
  url="https://raw.githubusercontent.com/${REPO}/${COMMIT}/app/src/main/assets/${n}"
  [ -s "$WORKDIR/$n" ] || curl -sL --max-time 45 -o "$WORKDIR/$n" "$url"
  got=$(shasum -a 256 "$WORKDIR/$n" | cut -c1-16)
  want=$(expect_sha "$n")
  if [ "$got" != "$want" ]; then echo "!! sha256 不符: $n 期望 $want 实得 $got"; exit 2; fi
  echo "   $n  $(wc -c < "$WORKDIR/$n" | tr -d ' ')B  sha256:${got}  ✅"
done

echo "== 2/3 解码 .silk -> wav (pilk) =="
"$PY" "$GA/tools/silk_to_text.py" decode "$WORKDIR/hello.silk" "$WORKDIR/test.silk" -o "$WORKDIR/out"

echo "== 3/3 转写 (faster-whisper / $MODEL) =="
"$PY" "$GA/tools/silk_to_text.py" text "$WORKDIR/out/hello.wav" "$WORKDIR/out/test.wav" -m "$MODEL"

echo
echo "预期结果（2026-09-22 本机实测基线）:"
echo "   hello.silk 3615B -> wav 94124B | 1960ms | zh | \"你好\""
echo "   test.silk  2517B -> wav 70124B | 1460ms | en | \"It's up to you.\""
