# SOP: oc2a(opencode2api) 免费模型 IP 限流 429 的出口轮换治理
> 适用: 自建网关 `opencode2api` 走 Zen **免费模型（匿名通道）** 时命中 429/网络错误即失败、客户端直接看到报错。
> 实测环境: macOS 本机, 2026-10-15；细节实测表见 `temp/oc2a_egress/NOTES.md`。

## 1. 机制（为什么单出口必然失败）
- 免费模型只走**匿名通道**（上游 `public` 凭证）；网关对**每个可用代理最多尝试一次**
  ⇒ `"proxies": ["direct"]` 时全局只有 1 个匿名节点，429 后无出口可换，直接透传客户端
  （日志特征 `"legs":"anon:429:...ms"`、`"outcome":"client_error"`、`attempts:1`）。
- 所以 **出口数 = 可换 IP 的机会数**；给网关多喂几个 `socks5h://` 出口即可显著降失败率。

## 2. 拓扑（两实例并存，互不干扰）
```
客户端/GA → oc2a 网关 127.0.0.1:18080 --(proxies: direct + N×socks5h)--> 本地出口 mihomo
                                                                  ↘ 机场订阅节点(每端口=1 节点=1 出口 IP)
Clash Verge 主实例(自己的 TUN/7897)  ←→  另开的出口 mihomo 只监听 127.0.0.1、**不开 TUN、不改系统路由**
```

## 3. 操作步骤
1. **摘节点**：`yaml.safe_load(<Verge profile yaml>)["proxies"]`（clash-verge-rev profiles 目录下的订阅文件自带明文凭据）；
   过滤 `剩余流量/到期/官网` 之类伪节点，按 `(server,port)` 去重。
2. **生成出口配置**：每个节点复制成 `proxies: [{name: egNN, ...原样}]`，再配 `listeners: [{name: egNN, type: socks, port: 18090+NN, listen: 127.0.0.1, proxy: egNN}]`
   + `rules: ["MATCH,DIRECT"]`；写到 `temp/oc2a_egress/config.yaml`（gitignored，含凭据，勿入库）。
3. **拉起并保活**：先跑通后固化为 LaunchAgent（`com.cuip.mihomo.oc2a.egress`，`KeepAlive=true`，日志落在 `temp/oc2a_egress/mihomo.{out,err}.log`）。
4. **实测出口**：`curl -sS -m 6 -x socks5h://127.0.0.1:1809N https://api.ipify.org`（只保留出口 IP 互不相同的端口；
   先串行 + 长超时，别用高并发 xargs 判死活，hy2 节点会因并发出现假 FAIL）。
5. **写回网关**：`/Users/cuip/opencode2api/config.json` 的 `"proxies"` 改成 `["direct", "socks5h://127.0.0.1:1809A", ...]`
   （**改前备份** `config.json.bak-<ts>-before-proxies`；只动 proxies 键，其余键一字不改）。
6. **重启**：`launchctl kickstart -k gui/501/com.cuip.opencode2api`。
7. **验收（必须拿到日志证据）**：发一次真实请求，`grep request_routed <网关日志>` 应见
   `"legs":"anon:429:...ms anon:200:...ms"`（换出口重试成功）；改前失败态是 `attempts:1` + `outcome:"client_error"`。

## 4. 坑与判据
- `listeners` 端口必须与写回网关的 `socks5h://127.0.0.1:<port>` **一一对应**；漏配端口 = 该出口恒失败、白占一次尝试。
- 出口节点会**抖动**（同一端口时通时断）⇒ 复测要用长超时串行；判"死"需两次以上 FAIL。
- 出口 IP 必须**互不相同**：同 IP 多端口只是重复喂同一个被限流 IP。
- `hysteria2` 类节点依赖 UDP 出网；`*lv6*` 节点可能只有 IPv6 出口，本机无 v6 时必 FAIL。
- 不要把出口实例改成 TUN/系统代理（会与 Clash Verge 抢路由）；本 SOP 只开本地 socks 端口。

## 5. 回滚
1) `cp /Users/cuip/opencode2api/config.json.bak-<ts>-before-proxies <config.json>` → `launchctl kickstart -k gui/501/com.cuip.opencode2api`；
2) 停出口实例：`launchctl bootout gui/501 ~/Library/LaunchAgents/com.cuip.mihomo.oc2a.egress.plist`。

## 6. 边界（不要当成已解决）
- 机场节点是**共享 IP**，只是把"1 次机会"变成"多次换 IP 机会"，非独立 IP、非新配额。
- Zen 免费模型**仅匿名通道**免费；想要 Key 级轮换需付费 credit（`zen_keys` 非空才有 Key 轮换）。
- 机场订阅**到期即出口全废**（当时套餐到期 2026-10-17）⇒ 到期后需重摘 profile 重跑本 SOP。
- 未处理：`first_event_timeout_seconds=0` 导致的**长挂死**（曾见 `duration_ms` 477617 ≈ 8 分钟）另案处理。
- 参考：`temp/oc2a_egress/NOTES.md`（端口↔节点↔出口 IP↔握手延迟实测表）。
