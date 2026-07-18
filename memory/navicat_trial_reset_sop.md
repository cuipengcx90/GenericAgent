# Navicat Premium 试用期重置 SOP (macOS)

目标：清除试用期计时痕迹，重置 14 天试用。经 2026-07-02 验证通过。

## 关键前置
- 重置脚本：`/Users/cuip/develop/WorkSpaces/idea/git/navicat_for_mac_reset/reset_navicat.sh`
- 试用痕迹存放位置（两处，缺一不可）：
  1. `~/Library/Preferences/com.navicat.NavicatPremium.plist`
  2. `~/Library/Application Support/PremiumSoft CyberTech/Navicat CC/Navicat Premium/` 目录下的隐藏文件 `.<32位大写hex>`（本机为 `.9472C1EE62E0D3AD3DFFDA1133C78691`，重置后会被同名重生成，属正常）
- 同目录的 `info.plist`/`usage.db`/`Profiles`/`Logs` 不动，删了会丢连接配置。

## 执行步骤
1. **必须先彻底关闭 Navicat**（否则进程会回写文件，重置失效）：
   ```bash
   osascript -e 'quit app "Navicat Premium"'; sleep 1; pkill -i "Navicat Premium"
   pgrep -fl -i navicat   # 确认无残留
   ```
2. **执行重置脚本**（脚本内部就是删上述两个文件）：
   ```bash
   bash /Users/cuip/develop/WorkSpaces/idea/git/navicat_for_mac_reset/reset_navicat.sh
   ```
3. **收尾打开策略（重要）**：
   - 重置前先记录 Navicat 是否在运行（`pgrep -i navicat`）。
   - 验证生效：`ls -a` 该目录，隐藏 hash 文件时间戳更新为当前即成功；若手动执行想确认，可 `open -a "Navicat Premium"` 触发重生成后再关。
   - **仅当重置前 Navicat 本来在运行时，才 `open -a "Navicat Premium"` 恢复；本来没开就保持关闭，不要自动打开**（避免打扰用户）。

## 典型坑
- **必须用 `bash` 不用 `sh`**：脚本用了 `[[ =~ ]]` 和 `BASH_REMATCH`，macOS `/bin/sh` 的 posix 模式下匹配可能失效导致隐藏文件删不掉。用户口述的 `sh reset_navicat.sh` 不可靠。
- 不需要 `sudo`（文件都在用户 `~/Library` 下，普通权限即可 rm）。
- 验证生效口径：重置后重新打开，`ls -a` 该目录看到 `.<32hex>` 文件时间戳更新为当前，即成功。
