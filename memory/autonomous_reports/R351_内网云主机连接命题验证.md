# R351 · 命题验证：本机是否连接过用户自建内网 / 云主机（只读）

- 日期：2026-09-22
- 来源：TODO#11（环境 | 评审 6/10 收敛为可证伪命题）
- 约束：**全程只读**，未执行任何登录 / 连接 / 探测包（无 ping/ssh/curl 到目标主机；`Tailscale status`、`gcloud config list`、`docker context ls` 均为本地只读查询）
- 边界：密钥类文件只列名与大小，未读取内容（`~/.aliyun/config.json`、gcloud `credentials.db` 等）

## 0. 命题与结论

**命题**：本机是否连接过用户自建内网 / 云主机？
**结论**：**成立（YES）**。不是"有痕迹"级别的弱证据，而是三层独立来源互相印证：

1. SSH 已知主机（`~/.ssh/known_hosts`）含 6 个私网/云 IP + 1 个 Tailscale MagicDNS 域名；
2. Shell 历史（`~/.zsh_history`）含**实际执行过的 ssh/隧道命令**，含用 GCP 专用密钥与通过 Tailscale IP 做端口转发；
3. 浏览器历史（Safari）独立确证其中一台的主机:端口访问记录，与 R344 的"6 次访问"完全吻合。

同时如实记录**当前未连接**项：Tailscale 隧道 `stopped`；kubectl 无任何 kubeconfig；docker 无远端 context。

---

## 1. 逐项证据（命令 + 原始输出片段）

### 1.1 SSH 已知主机（这是"连接过"的直接凭据）

命令：`awk '{print NR": "$1}' ~/.ssh/known_hosts`

原始输出（30 行全量，仅主机名/IP）：

```
1-3: 192.168.42.1      4-6: 10.211.55.4      7: 10.211.55.5      8-10: 10.211.55.6
11-13: 192.168.1.86    14: 192.168.1.163    15: codeup.aliyun.com  16-18: github.com
19-21: 195.26.252.222  22: 100.114.166.15   23-25: 100.112.252.36
26: 35.203.144.122     27: openclaw-server.tailf82c5b.ts.net
28-30: 47.116.119.182
```

文件元数据：`-rw-------@ 1 cuip staff 7572 Jul 10 12:39 ~/.ssh/known_hosts`（另有 `known_hosts.old` 6828B / May 9 11:22）
⇒ 明文存储（**非 hashed**），因此上表是完整的"确实 ssh 过"清单。

**关键交叉发现**：`100.112.252.36`（23-25 行）与 `openclaw-server.tailf82c5b.ts.net`（27 行）记录的是**同一把 ed25519 公钥**
（`AAAAC3NzaC1lZDI1NTE5AAAAINueb3MBCfj21R3d65PTThMQMLEjl0adC0mL2vlwVVJS`）⇒ 二者是同一台主机的 Tailscale IP 与 MagicDNS 名。
而 `100.114.166.15` 用的是另一把公钥（`...AAAAIFmit4mr/...`）⇒ **另一台独立主机**。

### 1.2 SSH 实际使用史（命令级，非仅 know_hosts）

命令：`grep -aE "ssh |tailscale|docker -H|kubectl" ~/.zsh_history | sed 's/^:[^;]*;//' | tail -25`

原始输出节选：

```
ssh 10.211.55.4 / parallels@10.211.55.5 / cuip@10.211.55.6      # Parallels 虚拟机
ssh Smyze@192.168.1.8 / smyze@192.168.1.86 / smyze@192.168.1.163  # 局域网设备
ssh smyze@192.168.42.1
ssh root@121.40.75.178                                          # 阿里云段
ssh fr3t_39586553@ftp.tttttttttt.top                            # 自建 FTP
ssh -L 8085:127.0.0.1:8085 root@63.205.203.249 -p 22            # 云主机本地端口转发
ssh cuipe1604@195.26.252.222
ssh root@47.116.119.182                                         # 阿里云段
ssh -N -L 18789:127.0.0.1:18789 cuip@100.112.252.36             # ★经 Tailscale IP 转发到本机 18789
ssh -i ~/.ssh/google_compute_engine -o ConnectTimeout=10 cuip@100.112.252.36  # ★用 GCP 专用密钥登录
autossh -M 0 -N -R 2221:127.0.0.1:22 root@8.209.231.194         # ★反向隧道(本机 22 → 远端 2221)
tailscale / tailscale serve status                              # Tailscale CLI 曾使用
```

推断链（逐条可核）：
- `-i ~/.ssh/google_compute_engine` → 该密钥是 **GCP 实例自动生成的专属密钥**（`~/.ssh/google_compute_engine.pub` 585B / Feb 13 2026），且已知主机含 `35.203.144.122`（GCP 段）⇒ `100.112.252.36` 是**用户的 GCP 云主机**，其 GCP 内网/Tailscale 侧另有 `35.203.144.122`；
- `root@47.116.119.182`、`root@121.40.75.178`（47.116.x/121.40.x = 阿里云）＋ ACR 私有镜像（见 1.4）⇒ 存在**阿里云侧主机**；
- `autossh -R` ⇒ 曾搭建**反向隧道**用于从公网回访本机（典型"内网穿透"用法）；
- `10.211.55.4/5/6`（Parallels 默认网段）＋ `bridge100-102` 在线 ⇒ **本机内网 = Parallels 虚拟机群**。

### 1.3 浏览器交叉确证（独立第三源）

命令（只读副本 + `mode=ro` 查询，遵循"必须连 -wal/-shm"规则）：

```python
shutil.copy2("~/Library/Safari/History.db", "/tmp/hist_Safari.db")   # 同法拷 -wal/-shm
sqlite3 "file:/tmp/hist_Safari.db?mode=ro" → select url,visit_count from history_items where url like '%<ip>%'
```

原始结果：

```
[Safari] 总记录=29
   100.114.166.15: 2 条 | 例: http://100.114.166.15:18080/ (visits=6)
   192.168.42.1:   1 条 | 例: http://www.192.168.42.1:8010/ (visits=1)
[Chrome] 总记录=12449
   100.114.166.15: 0 条 ; 100.112.252.36: 0 条 ; tailf82c5b: 0 条
```

⇒ `100.114.166.15` 上跑着 **HTTP 服务（:18080）**，Safari 访问 6 次（与 R344 记录一致）；Chrome 侧 0 命中说明走的是 Safari。
⇒ `100.112.252.36`、`openclaw-server.tailf82c5b.ts.net` 在浏览器 0 命中 ⇒ 那台是**纯 SSH/隧道用途**，不是网页服务。

### 1.4 五项工具的「存在 / 未连接 / 未验证」三态

| # | 项目 | 状态 | 证据命令 | 原始输出片段 |
|---|---|---|---|---|
| 1 | docker / OrbStack | **存在且正在运行（本地）**；**未连接任何远端 docker 主机** | `docker version --format ...`；`docker ps -a`；`docker context ls`；`ls -l ~/.orbstack/run/docker.sock` | `29.4.0 client=29.4.0`；`trendradar/cliproxyapi/cpab-server/drawio... Up 2 hours`；`images: 30`；contexts 仅 `default`(unix:///var/run/docker.sock) 与 `orbstack`(unix:///Users/cuip/.orbstack/run/docker.sock) |
| 2 | kubectl + ~/.kube | **二进制存在 / 无配置 ⇒ 未连接** | `which kubectl`；`ls -la ~/.kube`；`find ~/.kube -maxdepth 2 -type f` | `/usr/local/bin/kubectl`；`total 0`（仅 `.`/`..`）；无任何文件 ⇒ 从未连过集群 |
| 3 | ~/.aliyun（阿里云凭据面） | **CLI 与凭据面均存在；内容未读（需授权）**；云资源使用有旁证 | `ls -la ~/.aliyun`；`grep -a aliyun ~/.zsh_history` | `-rw------- 412 Aug 3 12:48 config.json` + `plugins/` + `flow-cli-prefix/`；历史含 `docker pull bucket-registry.cn-shanghai.cr.aliyuncs.com/smyze/bar-op-server`、`git clone git@codeup.aliyun.com:62b67c74.../bar_smyze_op.git` |
| 4 | gcloud | **存在且**近期在用（09-20~09-22 均有日志）；已配置账号/项目；**凭据仍在有效会话** | `ls /opt/homebrew/share/google-cloud-sdk`；`gcloud config list --format=...`；`grep -a gcloud ~/.zsh_history`；`ls ~/.config/gcloud/logs` | `cuipengcx@gmail.com  cpgoagent-11013`；`logs/: 2026.09.20 / 2026.09.21 / 2026.09.22`；历史 `gcloud run services list --filter="metadata.name=cliproxyapi"`；本次调用日志见下（**注意：该命令并非纯本地，会读 GCP API**） |
| 5 | tailscale | **已安装 + 进程常驻，但当前未连接（stopped）**；历史曾用 | `/Applications/Tailscale.app/Contents/MacOS/Tailscale status`；`pgrep -l -i tailscale`；`ps -Ao comm` | `Tailscale is stopped.`（rc=1）；`72302 io.tailscale.ip`；`/Library/SystemExtensions/.../io.tailscale.ipn.macsys.network-extension` |

补充：`which aliyun` → `/opt/homebrew/bin/aliyun`；`which tailscale` → 空（macOS 版 CLI 在 app bundle 内，未软链到 PATH）。

### 1.4b ⚠️ 更正：`gcloud config list` 不是纯本地操作（本轮唯一的网络出站）

原以为 `gcloud config list` 只读本地配置，**实测会触达 GCP API**。命令：

```
CLOUDSDK_CORE_DISABLE_PROMPTS=1 /opt/homebrew/share/google-cloud-sdk/bin/gcloud config list \
  --format='value(core.account,core.project,compute.region,compute.zone)'
```

`~/.config/gcloud/logs/2026.09.22/15.32.31.276795.log` 原始片段：

```
15:32:31,362 DEBUG urllib3.connectionpool Starting new HTTPS connection (1): cloudresourcemanager.googleapis.com:443
15:32:44,823 DEBUG urllib3.connectionpool https://cloudresourcemanager.googleapis.com:443 "GET /v3/effectiveTags?alt=json&pageSize=100&p..."
15:32:44,851 INFO  root  Unable to get environment tag for project [cpgoagent-11013]: PERMISSION_DENIED: Cloud Resource Manager API
                      has not been used in project cpgoagent-11013 before or it is disabled.
15:32:44,853 INFO  ___FILE_ONLY___ cuipengcx@gmail.com	cpgoagent-11013
```

两点结论（都属"未计划但已发生"的如实披露）：
1. 这是一次**对 Google API 的只读 GET**（`effectiveTags`），并非纯本地读取 —— 报告原文的"不发网络请求"说法**作废**；
2. 该请求**未报认证错误**，而是 API 未启用/权限不足 ⇒ 说明本机 gcloud 凭据仍处于**有效授权会话**（补充旁证：`access_tokens.db` mtime = 09-22 14:26）。

对命题的意义：GCP 侧不只是"有配置文件"，而是**凭据可用、账号在线**。



命令：`/sbin/ifconfig -a`、`/usr/sbin/netstat -rn -f inet`

```
en0: inet 192.168.1.22 netmask 0xffffff00        bridge0/bridge100-102: UP（Parallels 虚拟机桥）
默认路由: default 192.168.1.1 en0
（无 utun*、无 100.64/10 CGNAT 地址）
```

⇒ 与 1.4#5 一致：**此刻 Tailscale 隧道不存在**，本机只在内网 192.168.1.0/24 + Parallels 桥接网段上。

---

## 2. 交叉确证表（同一事实 ≥2 个独立来源）

| 事实 | 来源 A | 来源 B | 来源 C | 判定 |
|---|---|---|---|---|
| 曾 ssh 到 100.112.252.36 | known_hosts 23-25 行 | zsh_history `ssh -i google_compute_engine cuip@100.112.252.36` | zsh_history `ssh -N -L 18789 cuip@100.112.252.36` | ✅ 三重 |
| 100.112.252.36 与 openclaw-server.tailf82c5b.ts.net 同机 | known_hosts 公钥逐字节相同 | 二者同属 100.64/10 CGNAT（Tailscale 网段） | shell 历史曾用过 Tailscale CLI | ✅ |
| 该机是 GCP 主机 | 登录用 GCP 专用密钥 `google_compute_engine` | known_hosts 另有 GCP 段 IP `35.203.144.122` | `~/.config/gcloud` 项目 `cpgoagent-11013` 在近 3 日活跃 | ✅ |
| 100.114.166.15 是另一台、且对外提供 Web 服务 | known_hosts 独立公钥 | Safari `http://100.114.166.15:18080/` visits=6 | 100.64/10 ⇒ Tailscale 节点 | ✅ |
| 存在阿里云侧主机/资源 | known_hosts `47.116.119.182` ×3 | zsh_history `ssh root@47.116.119.182`、`root@121.40.75.178` | ACR 私有镜像 + codeup 仓库（用户私有命名空间 `smyze`） | ✅ |
| 本机内网 = Parallels 虚拟机群 | known_hosts `10.211.55.4/5/6` | shell 历史 `ssh parallels@10.211.55.x` | `bridge100-102` 在线 + `/Applications/Parallels Desktop.app` | ✅ |
| 当前 Tailscale **未连接** | `Tailscale status` = "Tailscale is stopped." | `ifconfig` 无 utun/100.64 地址 | `netstat` 无隧道默认路由 | ✅ |
| docker 仅本地、无远端主机 | `docker context ls` 仅 default+orbstack | `~/.docker/contexts/meta/*/meta.json` 仅 orbstack(unix socket) | 本地 13 容器 Up / 30 镜像 | ✅ |

---

## 3. 三态结论（不粉饰）

**已连接过（有实证）**
- Tailscale 网络：`100.112.252.36`（= openclaw-server）、`100.114.166.15`，均经 100.64/10 CGNAT；
- 云主机：GCP（`35.203.144.122` / Cloud Run 服务 `cliproxyapi` / 项目 cpgoagent-11013）、阿里云（`47.116.119.182`、`121.40.75.178`、ACR `cn-shanghai` 私有仓库）、另有 `195.26.252.222`、`63.205.203.249`、`8.209.231.194`（autossh 反向隧道目标）、`ftp.tttttttttt.top`（自建 FTP）；
- 局域网/内网：`192.168.1.86`、`192.168.1.163`、`192.168.42.1`、Parallels `10.211.55.4/5/6`。

**当前未连接（如实记录，不得写成"已盘点完毕"）**
- Tailscale：客户端进程在跑（pid 72302）但隧道 `stopped`、无 CGNAT 地址 ⇒ 现在**无法**触达两台 100.x 主机；
- kubectl：无 kubeconfig ⇒ **不清楚**是否存在 K8s 集群，本机从未配置过；
- docker：无远端 context ⇒ 未通过本机 docker 管理远端 docker 主机；
- 现状无活跃 ssh 会话/隧道：`ps -Ao pid,command | grep '[s]sh '` 空。

**未验证（需授权或需连接，本轮明令禁止）**
- `~/.aliyun/config.json` 的 profile/权限范围（存在凭据面但内容未读）；
- `~/.config/gcloud/credentials.db` / `access_tokens.db` 的授权范围（仅读了 `active_config` 与 `core.account/project`）；
- 各主机当前是否在线、服务是否可用（**未**做任何连通性探测）；
- `100.114.166.15:18080` 上具体是什么服务（仅从 URL 推断为 Web 服务）；
- autossh 当前是否仍在使用（`which autossh` 为空 ⇒ 该工具现已不在 PATH，历史用过）。

---

## 4. "要真盘点需用户授权什么"

1. **启动 Tailscale（登录 tailnet）** —— 否则 `100.112.252.36` / `100.114.166.15` 不可达，无法核对"现在还有哪些节点在网"。授权后本机可产出 `tailscale status` 全节点表（含每个节点的 OS/在线态）。
2. **是否允许读 `~/.aliyun/config.json`（凭据面）** —— 目前只确认"文件存在 412B / Aug 3"，未读内容。授权后可列 profile 名与 endpoint（**仍不建议输出 AccessKey 明文**）；进一步"盘点云资源"需要调用 `aliyun ecs DescribeInstances` 等网络请求，属"连接尝试"，需单独授权。
3. **GCP 侧只读盘点** —— 已有本地配置（account `cuipengcx@gmail.com` / project `cpgoagent-11013`）。若允许执行 `gcloud compute instances list`、`gcloud run services list`（这会**联网**并使用现有凭据），可产出 GCP 资源清单；本轮未执行。
4. **是否允许 SSH 登录各云主机做主机内盘点** —— 这属明确禁止项，故本轮完全未做；如需主机清单，需要你逐台授权（或提供只读命令输出）。
5. **Parallels / 局域网设备清单** —— 若要"内网盘点"，需要授权读取 Parallels 配置（`prlctl list`）或对 192.168.1.0/24 做扫描（后者属主动探测，需明确同意）。

---

## 5. 复现命令包（全部只读）

```bash
awk '{print NR": "$1}' ~/.ssh/known_hosts
grep -aE "ssh |tailscale|docker -H|kubectl" ~/.zsh_history | sed 's/^:[^;]*;//' | tail -25
python3 -c "import shutil,sqlite3;shutil.copy2('$HOME/Library/Safari/History.db','/tmp/h.db');\
c=sqlite3.connect('file:/tmp/h.db?mode=ro',uri=True);print(c.execute(\"select url,visit_count from history_items where url like '%100.114%'\").fetchall())"
/Applications/Tailscale.app/Contents/MacOS/Tailscale status     # 只读状态查询
pgrep -l -i tailscale; ps -Ao comm | grep -i tailscale
docker version --format '{{.Server.Version}}'; docker ps -a --format '{{.Names}}|{{.Status}}'; docker context ls
ls -la ~/.kube ~/.aliyun; ls /opt/homebrew/share/google-cloud-sdk
CLOUDSDK_CORE_DISABLE_PROMPTS=1 /opt/homebrew/share/google-cloud-sdk/bin/gcloud config list \
  --format='value(core.account,core.project)'
/sbin/ifconfig -a | grep -E '^[a-z0-9]+:|inet '; /usr/sbin/netstat -rn -f inet | awk '$1=="default"'
```

## 6. 本轮边界声明

- 未执行任何会触达目标主机的动作（无 ssh / ping / nc / curl 到上述 IP 或域名）；
- **本轮唯一的对外网络出站 = `gcloud config list`**（见 §1.4b）：它触发了 1 次只读 `GET cloudresourcemanager.googleapis.com/v3/effectiveTags`，返回 PERMISSION_DENIED。属于计划外的出站，如实披露；该调用不改变任何远端状态、不涉及认证操作；
- 其余命令（`Tailscale status`、`docker version/context ls/ps`、`ls`、`grep`、`sqlite3` 只读查询）均为本地操作；**未对任何目标主机做连通性/登录尝试**（无 ssh/ping/nc/curl 到 §1 清单中的 IP 或域名）；
- 未读取任何私钥或凭据内容，仅列出文件名、大小与修改时间。
