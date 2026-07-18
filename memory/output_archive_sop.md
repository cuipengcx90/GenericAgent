# output 推送归档站 SOP

把 GenericAgent 每日推送产物(晨报/晚报/技术趋势榜/HG月刊/微信周报等)汇成一个可浏览可分享的归档站。

## 组件
- 生成器: `tools/build_output_archive.py` — 扫描 `output/*.md,*.txt`(日期命名), 解析类型(前缀映射 TYPES)→自包含单页 HTML→ `archive/index.html`。UI 浅色精致(跟随系统深色)+类型彩色徽章+日期分组卡片+杂志式正文+移动端"列表→点开→返回"单栏。
- launchd(gui/$(id -u), 都在 ~/Library/LaunchAgents):
  - `com.cuip.archive-server`: `python3 -m http.server 8899 --bind 0.0.0.0 --directory archive`, KeepAlive+RunAtLoad。
  - `com.cuip.archive-rebuild`: WatchPaths=`output/`, RunAtLoad, 每次新产物落盘自动重建。
- 去重私信: 生成器内 `maybe_push()` 按 md5(type:sort:title 拼接) 存 `archive/.push_state.json`, 仅内容变化才 `notify_feishu(text)` 私信推 ARCHIVE_URL, 失败不阻断生成。

## 关键坑(验证过)
- 归档目录必须在 `output/` **之外**(用 `archive/` 不用 `output/archive/`), 否则 rebuild 写产物又触发 WatchPaths→死循环。
- python 必须显式 `/opt/anaconda3/bin/python3`(plist 里 which 未必对)。
- 内嵌 JSON 前先确认报告正文无裸 `</script>`(会破坏 script 闭合); 用 json.loads 解析验证。
- **Clash TUN 陷阱**: 本机开 Clash TUN, 默认路由/socket 探到的是 `198.18.0.1` 假 IP。取真实 LAN IP 须解析 `ifconfig` 里 en0 的 192.168/10 网段(本机 en0=192.168.1.40, DHCP 可变, 已硬编码进生成器 ARCHIVE_URL, 换网需改)。
- file:// 链接手机打不开→必须 HTTP 服务。LAN 链接仅"同 WiFi + 本机开机"可达, 群成员/外网打不开(要公网分享须内网穿透如 Cloudflare Tunnel)。
- bash 里 `UID` 是只读变量不能赋值, 直接用 `$(id -u)`。

## 前端(2026-07 已重做为 Tailwind + shadcn 风)
- HTML_TEMPLATE 是 `HTML_TEMPLATE = r"""..."""`(约 211~561 行), 前 210 行是 Python 数据层(parse_name/to_html/build, 含中文 TYPES/WEEKDAY, **勿动**)。改前端只替换模板中间: 切在 `r"""` 与其后首个 `"""` 之间, Python 原样保留护中文。
- 占位符(重建后须 0 残留): `__DATA__`(items 数组), `__TYPES__`({key:{icon,label,color,count}}), `__GENTIME__`, `__TOTAL__`。item 字段: type/typeLabel/icon/color/sort(int YYYYMMDD)/date/wday/title/html(预渲染)/text。
- 手术方式: 新模板先写 /tmp 再 python 字符串 splice(index 定位 marker→切片重组), **模板内禁出现三连双引号**(会破坏 r""" 闭合)。
- 设计: Tailwind Play CDN + neutral 调 + 圆角卡片 + badge + 暗色 class 切换。默认近 30 天, 5 快捷段 + 自定义日历 range picker + 搜索 + 类型 chip + 主题切换(localStorage `arch-theme`, 渲染前 head 里先决定避免闪白)。
- **点击 null-bug 根因(踩过)**: 直接 `el.addEventListener` 绑到会被 renderCal() innerHTML 重建的节点上→重建后监听丢失/点击无反应。根治=**事件委托**: 在稳定父容器(pop)上 addEventListener, 用 `e.target.closest('[data-day]')` 分派, 日历怎么重绘点击都在。
- 实测验证(必做): 浏览器 reload 后 JS 断言 cardCount 随快捷段/搜索/区间变化、主题切换 classList.toggle('dark') 生效、无 console 报错。

## 命令
- 手动重建: `/opt/anaconda3/bin/python3 tools/build_output_archive.py`
- 加载: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cuip.archive-server.plist`
- 卸载/关服务: `launchctl bootout gui/$(id -u)/com.cuip.archive-server`
- 访问: `http://192.168.1.40:8899/`
