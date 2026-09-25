# R405 网络分层诊断单入口 tools/net_doctor.py

- 完成时刻: 2026-09-25 15:27:16 +0800
- 任务来源: `temp/TODO.txt` 第 9 行（第11批·三路评审 7.00，能力类）
- 交付物: `tools/net_doctor.py`（713 tools/net_doctor.py 行 / 33298 tools/net_doctor.py 字节，sha256 `958d0933f4d27e5454b11a5f6b87ff950d03fb329babe37c9e08c887f0ff808d`）+ 网络漂移台账 `temp/net_doctor_history.jsonl`
- 证据包（本轮全部原始输出的单文件留档）: `temp/net_doctor_evidence_R405.txt`（下文 E1–E8 均从该文件逐字复制）
- 性质: 只读诊断工具。除自己的历史文件外不写任何路径；不自动改网络/代理配置，不改任何 memory 文件

## 0. 结论

把 RULES#14 里散落的网络排查动作（Clash 控制面探活 / 假 IP 识别 / 目标站探活 / 跨网旁证）收成**一个确定性的只读单入口**，
并按「本机链路 → 代理控制面 → 出口基线 → 目标站」四层给证据链，最终归到三类故障之一（外加一个出口不可达分支）：

| 判定码 | 语义 | 触发条件（要点） |
|---|---|---|
| `LOCAL_DOWN` | 本机断网 | L1 链路任一环节失败（路由/接口/网关 TCP/DNS） |
| `PROXY_NODE_BAD` | 代理节点坏（出口层） | Clash 控制面可达，但当前节点 delay 探测失败 |
| `SITE_FAILURE` | 站点故障（目标侧） | 基线站全通 + 仅个别目标站 TCP/TLS/HTTP 类失败 |
| `EGRESS_BAD` | 出口层不可达 | 本机在线但基线站全部打不通 |
| `OK` | 未发现异常 | 基线可达且无目标站失败 |

**关键设计判据（本轮的真价值点）**：`FAKE_IP`（解析到 198.18.0.0/15 等 TUN 假 IP）与 `DNS_FAIL` 被**显式排除**在「站点故障」判据之外 —
`SITE_FAILURE_CLASSES` 只含 TCP/TLS/HTTP 层失败码（见 `tools/net_doctor.py:346`），
这正是 haisou 那次把 `peer 198.18.0.112` 误读成「站点故障/网络异常」的复现点。

## 1. 交付物与规模（机器取值，见 E1）

```
### E1 工具规模
     713 tools/net_doctor.py
   33298 tools/net_doctor.py
958d0933f4d27e5454b11a5f6b87ff950d03fb329babe37c9e08c887f0ff808d  tools/net_doctor.py
```

## 2. 结构与判定实现位置（行号由 grep 当场取得，非手写）

| 角色 | 位置 | 说明 |
|---|---|---|
| 目标站分类 | `tools/net_doctor.py:317` | 单目标站：DNS → 假 IP 判定 → TCP/TLS/HTTP 分类 |
| 站点故障判据集 | `tools/net_doctor.py:346` | 仅 `TCP_TIMEOUT/TCP_FAIL/TLS_FAIL/HTTP_ERROR_STATUS`；**不含** `FAKE_IP`/`DNS_FAIL` |
| 三段式判定 | `tools/net_doctor.py:364` | 顺序：LOCAL_DOWN → PROXY_NODE_BAD → SITE_FAILURE → EGRESS_BAD → OK |
| ↳ 各分支返回点 | L374 / L387 / L404 / L416 | 每条都带 `evidence` + `next_steps`（下一步命令） |
| 自测 | `tools/net_doctor.py:577` | 13 项：负控/正控/合成判定/端到端夹具 |
| CLI | `tools/net_doctor.py:664` | `--target`(可多次) / `--timeout` / `--json` / `--selftest` / `--no-history` / `--history-file` / `--clash-sock` / `--quiet` |

L1 链路探测全部走**绝对路径**（`/sbin/route`、`/usr/sbin/ifconfig` 之类），原因是本轮实测环境 PATH 不含系统 sbin 目录（见 E7）。

## 3. 验收对照（TODO 第 9 行五条）

| # | 验收要求 | 结论 | 证据 |
|---|---|---|---|
| ① | 真跑并贴原始输出（≥1 可达站 + 1 不可达站，均记时间戳） | 达标 | E3/E4：`=== net_doctor @ 2026-09-25T15:26:43+08:00 ===`；可达 `https://www.baidu.com/ ⇒ 分类=OK (可达)`；不可达 `https://nonexistent-1790321165.invalid/ ⇒ 分类=DNS_FAIL`；每次运行头/尾都打印探测时刻，并在判定下方写明「结论仅代表本次探测时刻」 |
| ② | 负控：注入不可达域名与 198.18.x 假 IP 须正确归类（不得报「站点故障」） | 达标 | E2 selftest：`negctl1 … DNS_FAIL`、`negctl2 … FAKE_IP`、`negctl3 仅含假IP/不可达域名时 判定!=站点故障`、`negctl4 站点故障判据不含 FAKE_IP/DNS_FAIL 类`；真机同轮 E4：四目标混合时判定=SITE_FAILURE，但**归因只落在真 TLS 失败的目标上**，DNS_FAIL/FAKE_IP 两目标未参与该判据 |
| ③ | 三段式判定 = 本机断网 / 代理节点坏 / 站点故障，每段给证据链与下一步命令 | 达标 | E2 `synth 断网 ⇒ LOCAL_DOWN`/`synth 节点坏 ⇒ PROXY_NODE_BAD`/`synth 站点故障 ⇒ SITE_FAILURE`（合成输入，检查判定映射）+ `synth 基线全败 ⇒ EGRESS_BAD`；E4 真机 SITE_FAILURE 段落逐条打印 `证据:` 与 `下一步:`（4 条命令，含 RULES#14 的 r.jina.ai 跨网旁证与「peer 落 198.18/15 时不得据此断言」） |
| ④ | 无 Clash 时降级只跑链路+目标站 | 达标 | E5：本机当前无控制面（`/tmp/verge/verge-mihomo.sock` 与 `/tmp/mihomo.sock` 均不存在；pgrep 只有 Clash Verge 的**特权服务 helper**，无 mihomo/clash 主进程/API）⇒ 输出 `[L2 代理层] 降级：未发现 Clash/mihomo 控制面`，且**本轮所有真跑都是走这条降级路径**；控制面可达的分支由 e2e 夹具覆盖（E2 两条 `e2e Clash 分支`，含 `/version` 字段形状校验） |
| ⑤ | 固定探测矩阵 + 结果落历史文件（禁标永久结论） | 达标 | 固定矩阵 = 3 个基线站（gstatic/baidu/apple success）+ 运行时 `--target` 目标站；历史 E6：`temp/net_doctor_history.jsonl` 现有 4 行（每行 JSON：ts/verdict/link_ok/tun_hint/clash_api/clash_node/clash_delay_ok/baselines/targets）；运行头写明「结论仅代表本次探测时刻；网络状态会漂移，禁止标为永久结论」 |

自测总计：`--- 13/13 passed ---  rc=0`（E2）。

## 4. 原始证据（逐字，均取自 `temp/net_doctor_evidence_R405.txt`）

### E1 工具规模与哈希
```
### E1 工具规模
     713 tools/net_doctor.py
   33298 tools/net_doctor.py
958d0933f4d27e5454b11a5f6b87ff950d03fb329babe37c9e08c887f0ff808d  tools/net_doctor.py
```
### E2 `--selftest` 全量输出（13 项）
```
### E2 selftest 原始输出
=== net_doctor --selftest @ 2026-09-25T15:26:43+08:00 ===
  [PASS] negctl1 不可达域名 ⇒ DNS_FAIL  (https://nonexistent-1790321201.invalid/ -> DNS_FAIL)
  [PASS] negctl2 198.18.x 假 IP ⇒ FAKE_IP  (http://198.18.0.112/ -> FAKE_IP)
  [PASS] posctl 可达站 ⇒ OK  (https://www.baidu.com/ -> OK)
  [PASS] negctl3 仅含假IP/不可达域名时 判定!=站点故障  (实际判定=未发现异常 (OK))
  [PASS] negctl4 站点故障判据不含 FAKE_IP/DNS_FAIL 类  (SITE_FAILURE_CLASSES=['HTTP_ERROR_STATUS', 'TCP_FAIL', 'TCP_TIMEOUT', 'TLS_FAIL'])
  [PASS] synth 断网 ⇒ LOCAL_DOWN  (实际=LOCAL_DOWN steps=4 ev=1)
  [PASS] synth 节点坏 ⇒ PROXY_NODE_BAD  (实际=PROXY_NODE_BAD steps=3 ev=2)
  [PASS] synth 站点故障 ⇒ SITE_FAILURE  (实际=SITE_FAILURE steps=4 ev=1)
  [PASS] synth 基线全败 ⇒ EGRESS_BAD  (实际=EGRESS_BAD steps=3 ev=2)
  [PASS] e2e Clash 分支(夹具 delay_fail)  (delay 504 ⇒ 判定=PROXY_NODE_BAD delay_ok=False; 调用路径=['/configs', '/proxies', '/proxies/node-1/delay', '/version'])
  [PASS] e2e 夹具接口形状: /version 应含 version 字段  ()
  [PASS] e2e Clash 分支(夹具 delay_ok)  (delay ok ⇒ 判定=OK; 调用路径=['/configs', '/proxies', '/proxies/node-1/delay', '/version'])
  [PASS] e2e 夹具接口形状: /version 应含 version 字段  ()
--- 13/13 passed ---
rc=0
```
### E3 真跑：固定探测矩阵（默认，含时间戳）
```
### E3 真跑：固定探测矩阵（默认，含时间戳）
2026-09-25 15:26:43
  === net_doctor @ 2026-09-25T15:26:43+08:00 ===
  （结论仅代表本次探测时刻；网络状态会漂移，禁止标为永久结论）
  
  [L1 本机链路] link_ok=True  TUN 提示=False
    默认路由: ok=True gateway=192.168.3.1 iface=en0 (cmd=/sbin/route)
    接口地址: ok=True iface=en0 ips=['192.168.3.55']
    网关 TCP: ok=True 
    DNS: ok=True host=www.apple.com ips=['39.91.187.101', '39.91.187.97'] ms=1 
  
  [L2 代理层] 降级：未发现 Clash/mihomo 控制面（未开 TUN 或服务未拉起）
    （降级路径只跑链路 + 目标站，不臆测代理状态）
  
  [L3 出口基线]
    https://www.gstatic.com/generate_204               ok=True  status=204  peer=2408:80f1:1b0:109::1002 ms=142 
    https://www.baidu.com/                             ok=True  status=200  peer=2408:873d:22:18cb:0:ff:b037:e6d8 ms=102 
    https://www.apple.com/library/test/success.html    ok=True  status=200  peer=39.91.187.101    ms=111 
  
  [判定] 未发现异常 (OK)
    证据: 基线 3/3 可达
  
  [历史] 已追加 /Users/cuip/projects/GenericAgent/temp/net_doctor_history.jsonl
rc=0
```
### E4 三段式实景对照（同一轮，四目标混合）
```
### E4 三段式实景对照（同一轮，四目标）
2026-09-25 15:26:47
  === net_doctor @ 2026-09-25T15:26:47+08:00 ===
  （结论仅代表本次探测时刻；网络状态会漂移，禁止标为永久结论）
  
  [L1 本机链路] link_ok=True  TUN 提示=False
    默认路由: ok=True gateway=192.168.3.1 iface=en0 (cmd=/sbin/route)
    接口地址: ok=True iface=en0 ips=['192.168.3.55']
    网关 TCP: ok=True 
    DNS: ok=True host=www.apple.com ips=['39.91.187.101', '39.91.187.97'] ms=2 
  
  [L2 代理层] 降级：未发现 Clash/mihomo 控制面（未开 TUN 或服务未拉起）
    （降级路径只跑链路 + 目标站，不臆测代理状态）
  
  [L3 出口基线]
    https://www.gstatic.com/generate_204               ok=True  status=204  peer=2408:80f1:1b0:109::1002 ms=149 
    https://www.baidu.com/                             ok=True  status=200  peer=2408:873d:22:18cb:0:ff:b037:e6d8 ms=117 
    https://www.apple.com/library/test/success.html    ok=True  status=200  peer=39.91.187.101    ms=118 
  
  [L4 目标站]
    https://www.baidu.com/
      dns=True ips=['153.3.238.127', '153.3.238.28', '2408:873d:22:18cb:0:ff:b037:e6d8', '2408:873d:22:1a91:0:ff:b013:cf81']
      http ok=True status=200 peer=2408:873d:22:18cb:0:ff:b037:e6d8 ms=153 
      ⇒ 分类=OK (可达)
    https://nonexistent-1790321165.invalid/
      dns=False ips=None
      http ok=False status=None peer=None ms=30 gaierror
      ⇒ 分类=DNS_FAIL (域名不可解析（DNS 层，勿判站点故障）)
    http://198.18.0.112/
      dns=True ips=['198.18.0.112']
      http skipped(fake_ip) 未发起连接
      ⇒ 分类=FAKE_IP (解析到 TUN 假 IP（无判定价值，勿判站点故障）)
    https://expired.badssl.com/
      dns=True ips=['104.154.89.105']
      http ok=False status=None peer=None ms=586 SSLCertVerificationError
      ⇒ 分类=TLS_FAIL (TLS 握手失败（SSL 截断/证书问题）)
  
  [判定] 站点故障（目标侧） (SITE_FAILURE)
    证据: https://expired.badssl.com/ 失败(TLS_FAIL: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1000)) 而基线 https://www.gstatic.com/generate_204=204 正常
    下一步:
      curl -s https://r.jina.ai/<目标URL>   # 跨网旁证（RULES#14）
      # 若目标是同一站点的多个路径都失败、且其它站点正常 ⇒ 偏站点故障
      # 但 peer 或解析结果落在 198.18.0.0/15 时不得据此断言（TUN 假 IP）
      # 换一个出口/节点再复测一次，避免把出口策略误判成站点故障
  
  [历史] 已追加 /Users/cuip/projects/GenericAgent/temp/net_doctor_history.jsonl
rc=0
```
### E5 降级路径与控制面存在性探测
```
### E5 降级路径证明：本机控制面存在性探测
ls: /tmp/verge/verge-mihomo.sock: No such file or directory
ls: /tmp/mihomo.sock: No such file or directory
342 /Library/PrivilegedHelperTools/io.github.clash-verge-rev.clash-verge-rev.service.bundle/Contents/MacOS/clash-verge-service
pgrep_rc=0
[L2 代理层] 降级：未发现 Clash/mihomo 控制面（未开 TUN 或服务未拉起）
  （降级路径只跑链路 + 目标站，不臆测代理状态）
```
### E6 历史文件（网络漂移台账）
```
### E6 历史文件（网络漂移台账）
       4 temp/net_doctor_history.jsonl
--- 最近 2 行 ---
{"ts": "2026-09-25T15:26:47+08:00", "verdict": "SITE_FAILURE", "link_ok": true, "tun_hint": false, "clash_api": false, "clash_node": null, "clash_delay_ok": null, "baselines": [{"url": "https://www.gstatic.com/generate_204", "ok": true, "status": 204, "ms": 149, "error_class": null}, {"url": "https://www.baidu.com/", "ok": true, "status": 200, "ms": 117, "error_class": null}, {"url": "https://www.apple.com/library/test/success.html", "ok": true, "status": 200, "ms": 118, "error_class": null}], "targets": [{"url": "https://www.baidu.com/", "class": "OK", "peer": "2408:873d:22:18cb:0:ff:b037:e6d8", "status": 200}, {"url": "https://nonexistent-1790321165.invalid/", "class": "DNS_FAIL", "peer": null, "status": null}, {"url": "http://198.18.0.112/", "class": "FAKE_IP", "peer": null, "status": null}, {"url": "https://expired.badssl.com/", "class": "TLS_FAIL", "peer": null, "status": null}]}
{"ts": "2026-09-25T15:26:48+08:00", "verdict": "OK", "link_ok": true, "tun_hint": false, "clash_api": false, "clash_node": null, "clash_delay_ok": null, "baselines": [{"url": "https://www.gstatic.com/generate_204", "ok": true, "status": 204, "ms": 155, "error_class": null}, {"url": "https://www.baidu.com/", "ok": true, "status": 200, "ms": 123, "error_class": null}, {"url": "https://www.apple.com/library/test/success.html", "ok": true, "status": 200, "ms": 125, "error_class": null}], "targets": [{"url": "http://198.18.0.112/", "class": "FAKE_IP", "peer": null, "status": null}]}
```
### E7 PATH 与本机 sbin 现实（为何全用绝对路径）
```
### E7 PATH 证据（工具全走绝对路径的原因）
PATH=/opt/anaconda3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
route      command -v: (NOT FOUND)
ifconfig   command -v: (NOT FOUND)
ls: /usr/sbin/ifconfig: No such file or directory
-r-xr-xr-x  1 root  wheel  153472 Aug 23  2024 /sbin/route
```
### E8 只读性自证
```
### E8 只读性自证（工具不写别的文件）
-rw-r--r--  1 cuip  staff   2753 Sep 25 15:26 temp/net_doctor_history.jsonl
-rw-r--r--  1 cuip  staff  33298 Sep 25 15:26 tools/net_doctor.py
```

## 5. 本轮顺带得到的本机事实（可复用）

1. **`route`/`ifconfig` 在当前执行环境 PATH 下找不到**：`PATH=/opt/anaconda3/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`，`command -v route/ifconfig` 均 NOT FOUND，`/usr/sbin/ifconfig` 在本机根本不存在（只有 `/sbin/route`，root:wheel 0755）。⇒ 任何网络类脚本/子进程探测**必须走绝对路径**，否则会把「找不到命令」误判成「本机断网」。
2. **本机当前无 mihomo 控制面**：两个候选 unix socket 都不存在；`pgrep -fl "mihomo|clash"` 只命中 Clash Verge 的 privileged helper（system service bundle），不是控制面 API。⇒ 需要控制面能力时（如 RULES#14 的 `proxies/<node>/delay`）先判 socket 存在性与 `/version`，别硬编码单一路径。
3. **假 IP 现象可在本机复现**：`http://198.18.0.112/` 解析成功但落在 198.18/15（Clash TUN 段），工具分类 `FAKE_IP` 且**不发起连接**（E4 里显示 `http skipped(fake_ip) 未发起连接`）。这正是 haisou 报告里 `peer 198.18.0.112` 的口径：它是 TUN 假 IP，不构成站点或链路证据。
4. 真站点故障正控可用 `https://expired.badssl.com/`（证书过期 ⇒ `TLS_FAIL`）：本轮真机触发 `SITE_FAILURE`，且判定同时打印对照依据（基线 gstatic=204 正常）。

## 6. 边界与未验清单（明确不假装）

- **Clash 控制面真机分支未验证**：本机当下没有可用的 mihomo API socket，`PROXY_NODE_BAD` 只有合成输入（synth）+ 假 mihomo unix socket 夹具（e2e，2 例，含 `/version` 形状校验）两级证据，**没有真实 Clash 实例**的端到端证据。夹具替身已校核接口形状（端点路径与必需字段），但仍非真机。
- **TUN/假 IP 识别限于常见段**：`198.18.0.0/15` 与接口名启发式；若用户 Clash 配置改了 fake-ip 段或走别的 TUN，需要扩表。
- **不自动修复**：只给下一步命令，不动系统代理/不重启服务（破坏性动作一律留人）。
- **历史文件住 `temp/`（gitignored）**：与 L1「temp 耐久耦合」同一缺口（属既有问题，不在本轮扩面）。
- 判定是**快照**：单一时刻结论，跨时段要对比历史文件而不是改报告结论。

## 7. 待批项（未执行，见台账 `temp/pending_decisions/DECISIONS_R405_net_doctor.md`）

| ID | 动作 | 可逆 |
|---|---|---|
| R405A | 在 `memory/global_mem_insight.txt`(L1) 增一行网络诊断索引（指向 `tools/net_doctor.py`） | 可逆 |
| R405B | 在 RULES#14 文本末尾追加一句「起步命令 `python3 tools/net_doctor.py --target <URL>`」 | 可逆 |

## 8. 复现命令

```bash
cd ~/projects/GenericAgent
python3 tools/net_doctor.py --selftest --no-history
python3 tools/net_doctor.py                                   # 固定矩阵，写历史
python3 tools/net_doctor.py --target https://www.baidu.com/ \
        --target https://nonexistent-1790321165.invalid/ \
        --target http://198.18.0.112/ --target https://expired.badssl.com/
python3 tools/net_doctor.py --target <URL> --json             # 机读
tail -1 temp/net_doctor_history.jsonl                          # 漂移台账
```
