# eia_weekly_sop — EIA 能源价格 + 汇率周报（定时任务）

## 任务面
- 任务：`sche_tasks/eia_weekly.json`（每周 08:30，prompt=跑一条命令并把摘要写进报告路径）
- 入口：`python /Users/cuip/projects/GenericAgent/temp/eia_supply_chain_digest.py`（**脚本住 gitignored temp/**，改动务必留备份）
- 报告：`sche_tasks/done/<日期>_<时间>_eia_weekly.md`（同属 gitignored，收尾无需 git 提交）
- 脚本只打印，无推送逻辑。

## 取数三层（任一成功即用，绝不杜撰）
1. EIA v2 API：需 `EIA_API_KEY` / `EIA_KEY`（env 或 keychain）→ **本机无此 key**（2026-09-27 核：keychain 仅 haisou/typesafe）。
2. **免密钥 HTML 兜底**（2026-09-27 新增，实际走这条）：`https://www.eia.gov/petroleum/gasdiesel/`
   - HTTP 200、无需登录/UA 伪装即可取（只有 API 端点无 key 报 403）。
   - 解析口径：扁平化 HTML（剥标签+压空白）后，在标题 `U.S. Regular Gasoline Prices*(dollars per gallon)` / `U.S. On-Highway Diesel Fuel Prices*(dollars per gallon)` 之后抓 `U.S.` 行的 6 个数 = 最近三周价 + 2 年前 + 1 年前 + 周变化；取第 3 个为最新、第 6 个为周变化；`Gasoline Release Date: … Next Release Date: …` 给发布/下次发布日。
   - 页面是**周更**（周二发布，下周同一时间可看到新周价）。
3. 两者都失败 ⇒ 输出 `[降级] EIA 数据不可用: <原因>`（原语义不变）。

汇率：`frankfurter.app` 优先，回退 `open.er-api.com`（USD→CNY/EUR/JPY/SGD）。

## 坑
- **keychain 导入会打印表头污染摘要**（digest stdout 多一行）⇒ 导入须 `contextlib.redirect_stdout` 包裹。
- API 端点是 `api.eia.gov/v2/petroleum/pri/spt/data/`，无 key = HTTP 403（不是"网络不通"）。
- HTML 解析依赖页面结构；改版会解析失败并**回落降级**，需人工从报告里发现 `[降级]` 复发。
- 报告里的数值口径：`$/GAL`，全国 `U.S.` 行；PADD 分区行不纳入。
- 回滚（回到无兜底版）：`cp temp/eia_supply_chain_digest.py.bak_20260927_0830 temp/eia_supply_chain_digest.py`
  （改前原版 = 3646 字节；改后含兜底版 = 8653 字节）
- 历史拟合：27 份历史报告中 **13 份降级（48%）**，且 08-20 起连续 6 份全降级——早期能出数是因当时脚本**内嵌 key**（该 key 已随脚本删除丢失）。

## 未决
- 未申请 EIA API key（属凭据获取，需用户决定）；未改任务 JSON。
