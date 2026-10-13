# R583 只读审核:L站「每帖只加载前 20 楼」是否真是问题

审核时间 2026-10-13 15:00–15:20 (Asia/Shanghai)。**只读**:未改任何源码、未触发任何浏览动作。
被审对象 = 另一会话 2026-10-13 10:55–10:58 的结论与建议(原报告见 temp/user_prompt_80985_*.md)。

## 0. 裁决摘要

**主结论成立,且比原报告更严重。** 原报告只指出「只加载 20 楼」这一个症状;
本次核验另发现**同批(R579b)落地的三处独立缺陷**,其中两处会**反向影响已交付的记账口径**:

| 项 | 原报告 | 本次核验 |
|---|---|---|
| DOM 恒 ≤20、canAppendMore=true、滚到底不追加 | 真瓶颈(建议改 ROUTE_JS) | ✅ 证实,且 daemon 源码零处引用 canAppendMore(见 §2) |
| 楼层探针 FLOOR_PROBE_JS | 未提及 | ❌ **JS 语法错误,恒失败**(见 §3.1) |
| 日志 posts 字段来源 | 未提及 | ❌ 690/699 行 posts=None,posts_src 无一条 probe(见 §3.2) |
| 楼层记账 floors_credit_of | 未提及 | ❌ 回退到候选估算,**高估**实际读到楼数(见 §4) |

简言之:**「只加载20楼」是真问题;但当前账本上的楼层数不但没有被这个瓶颈压住,
反而因为探针坏了、记账回退到候选,被系统性高估了。**

## 1. 「20 楼」结论复核(成立)

### 1.1 原报告证据 vs 本次独立复测

原报告 2026-10-13 10:55–10:58 读 `_onscreen`/DOM/视口:

| 话题 | 原报告读数 |
|---|---|
| 2987481 | _onscreen=20、DOM=20、视口内=4,滚到底 26s 内 6 次采样恒 20 |
| 2996869 | 共 25 楼,只加载 20,canAppendMore=true,loadingBelow=false |
| 其余 tab(40/85 楼) | 各只加载 20,canAppendMore=true |

本次在 daemon 正在使用的两个 linux.do tab 上重放只读探针
(`postStream.posts.length` / `canAppendMore` / `loadingBelow` / DOM `.topic-post` 数),
结果为(字段原文照抄):

```
2109127035 {"dom":20,"min":1,"max":20,"loaded":20,"canAppendMore":true,"loadingBelow":false,
             "total":25,"topic_id":3016944,"scrollY":4714,"docH":5492,"winH":779,"vis":"hidden"}
2109127055 {"dom":6,"min":1,"max":7,"loaded":20,"canAppendMore":true,"loadingBelow":false,
             "total":...,"vis":"hidden"}
```

两条独立采样均复现:**canAppendMore=true 表示「还有下一批可加载」,但 loadingBelow=false
表示没有任何在途加载请求;tab 处于 hidden。** 原报告口径成立。

### 1.2 为什么「滚到底」也不加载

daemon 的浏览脚本 ROUTE_JS(`tools/linuxdo_browse_daemon.py:1706` 起)全程只调用
`window.scrollBy(...)`(第 1772、1776、1793 行三处),**没有任何一处读取 postStream.canAppendMore,
也没有任何一处主动触发加载**(全文件 grep `canAppendMore` = 0 命中)。

Discourse 的 PostStream 是**分页惰性加载**:初载只渲染首批(本机实测=20 楼),
触底时由 Discourse 自身的 scroll 监听调 `postStream.appendMore()` 拉下一批。
在 hidden 的后台 tab 里该监听不触发,于是 DOM 永远停在 20。
⇒ 原报告判断「瓶颈是加载上限、非滑动速度」**正确**,且「滑太快没跟上」的旧推测被否决也正确。

### 1.3 后果:高楼层加权策略确实被削平

daemon 侧 `FLOOR_CAP=64`、`FLOOR_BIAS=0.8`、`BIG_DEEP_*` 等「优先选高楼层帖」的选帖/分档参数
(R578/R581 一批)都以「帖子真实楼层数」为前提。若每帖最多只入账 20 楼,
则 40/85/90 楼的帖与 20 楼的帖在**实际入账**上无差别 ⇒ 该策略收益被截断。
这一点原报告说对了。

### 1.4 唯一未证部分(原报告自称的)

原报告承认「为什么后台标签页不加载下一批」只有推测、未读源。本次同样**未读 Discourse 源码**,
但用运行时证据把推测坐实了一半:postStream 原型上确实存在 `appendMore`(见 §5 API 探针),
即「缺的是触发调用,不是能力」;至于是浏览器对 hidden tab 停 scroll/timer 的机制,
仍属未读源的推断,报告中保留为「未验证」。

## 2. 缺陷一:ROUTE_JS 从不检查/触发续载(证实)

```
$ grep -n canAppendMore tools/linuxdo_browse_daemon.py
(无输出,0 命中)
```

ROUTE_JS 的三处滚动(1772/1776/1793)均为 `window.scrollBy`,末尾直接
`return JSON.stringify({how,title,...,posts:document.querySelectorAll('.topic-post').length})`
(第 1777 行)——即**返回值里的 posts 就是当前 DOM 楼层数(≤20)**,不做任何续载。
⇒ 原报告 §建议「在 ROUTE_JS 滚到底后检查 canAppendMore,为真就触发加载下一批」
在**方向上正确**;实现细节本报告 §5 给出更稳妥的写法。

## 3. 缺陷二:楼层探针 FLOOR_PROBE_JS 语法错误,恒失败(R579b 引入,原报告未发现)

### 3.1 语法错误(独立复现)

`tools/linuxdo_browse_daemon.py:1920`:

```
FLOOR_PROBE_JS = ("(()=>{try{return JSON.stringify({posts:document.querySelectorAll('.topic-post').length})}"
                  "}catch(e){return JSON.stringify({posts:null})}})()")
```

实测该常量字符串为:

```
(()=>{try{return JSON.stringify({posts:document.querySelectorAll('.topic-post').length})}}catch(e){...}})()
```

注意 `...length})}` 之后**紧跟 `}catch`**:这里多出一个 `}`。
大括号计数 `{` × 5、`}` × 6;`node --check` 直接判语法错误:

```
$ python3 -c "import sys;sys.path.insert(0,'tools');import linuxdo_browse_daemon as D;  open('/tmp/p.js','w').write(D.FLOOR_PROBE_JS)"
$ node --check /tmp/p.js
/tmp/p.js:1
(()=>{try{return JSON.stringify({posts:document.querySelectorAll('.topic-post').length})}}catch(e)...
                                                                                            ^
SyntaxError: Missing catch or finally after try
rc=1
```

在真实 linux.do tab 上直接下发该探针,返回体原文:

```
FLOOR_PROBE 2109127055 {"ok": false, "error": "{'message': 'Missing catch or finally after try', 'name': 'SyntaxError', ...}"}
```

⇒ **探针从未成功执行过一次**,`res["posts_src"]` 永远不会是 "probe"。

### 3.2 自测为何没拦住(41/41 假绿)

daemon 自测(第 3714 行)对该常量只做**字符串子串**检查:

```
if "topic-post" not in FLOOR_PROBE_JS or "_floor_probe(session)" not in _insp.getsource(browse_one):
    fails.append(...)
```

只验「字面里有没有 'topic-post'」,**从不校验这段 JS 能否解析** ⇒ 语法坏掉仍 41/41 全绿。
(与 RULE 16「审测试全绿先验测试本身是否真执行」同型:子串断言≠能力验证。)

## 4. 缺陷三:记账回退到候选,系统性高估楼层

### 4.1 记账函数

`floors_credit_of`(2255 行,节选):

```
def floors_credit_of(res, cand):
    ...  # 优先取 res["posts"](脚本实测 DOM 楼数,受 20 上限压制)
    return floors_credit({"posts": got})   # 缺失时回退候选楼层
```

`floors_credit(cand)` 取候选 `cand["posts"]`(服务器给的**总**楼层数),上限 `FLOOR_CREDIT_CAP=64`。
由于探针坏了(§3),`res["posts"]` 只在 ROUTE_JS 正常返回那 9 次里才有值,
其余全部走**回退**:按候选总楼层数(可达 64)记账,**而实际只读到 ≤20 楼**。

### 4.2 日志实测分布(browse_log.jsonl,只读聚合)

(下列为 2026-10-13 15:14 实时聚合快照;browse_log 随 daemon 运行增长)

```
DONE 总行数: 699
posts 非空行: 167   (all-time max = 20,>20 的行 = 0)
posts_src='probe' 行: 0
posts_src='res'   行: 9     (03:27/09:05/09:30/10:48/11:00/11:02/11:04/11:05/15:34,值 4/20/20/6/5/3/5/8/5)
posts_src=None(旧格式/无来源)行: 690
2026-10-13 DONE: 89,其中 posts 有值仅 9,fc>20 的行 63(fc 最高 64)
```

⇒ 今日绝大多数 DONE 的 `posts=None`,`fc`(计入楼层)来自候选回退,
**记账值 > 真实读到楼数**。原报告说「9.6 楼/话题、约 20 楼/帖是上限」,
但 state 里 `daily_floors['2026-10-13']=3090`(读数时刻 15:14;该键随 daemon 运行持续增长,复核时已 3156),按「真实读到楼层 ≤20/帖」重算约 **1499**
(89 行中 63 行 fc>20 全部属回退虚高)——**同一现象,两个方向相反的偏移并存**:
选帖按候选高楼挑、实际只读到 20,而账本又按候选高楼记 ⇒ 账本既不是「真实浏览」也不是「保守估计」。

## 5. 续载能力探针(为「建议」提供更稳的落地依据)

在 daemon 使用的 tab 上只读枚举 `postStream` 原型方法(不调用、不改状态):

```
2109127055 {"canAppendMore":true,
  "hasAppendMore":"function",
  "proto":["loading","notLoading","firstPostNotLoaded","lastPostNotLoaded","canAppendMore",
           "canPrependMore","hasLoadedData","loadedAllPosts","loadedPosts","appendMore",
           "prependMore","appendPost","findLoadedPost","loadPostByPostNumber",
           "loadNearestPostToDate","loadPost","triggerNewPostInStream",...]}
```

⇒ 页面**本来就具备** `appendMore()` 能力,只是没人调用。
因此比原报告「再滚一次等 Discourse 自己触发」更直接的做法是:
**滚到底后,`while (postStream.canAppendMore && 未达上限) { await postStream.appendMore(); await 短暂等待; }`**。

### 5.1 建议(仅建议,本次未改任何源码)

1. **修 FLOOR_PROBE_JS 语法**(去掉多余 `}`):`...length})}` → `...length})`。
2. **给 daemon 自测加「JS 常量可解析」断言**:把 ROUTE_JS / FLOOR_PROBE_JS / 各 *_JS 常量
   交给 `node --check`(或 python `esprima`)校验,杜绝此类假绿。
3. **ROUTE_JS 滚到底后补续载循环**(按 §5 写法),循环到 `canAppendMore=false` 或达到
   `FLOOR_CAP`/预算上限;并在返回值里把 DOM 楼数、`postStream.posts.length`、
   `canAppendMore` 一并回传,便于日志核验。
4. **记账口径收紧**:`floors_credit_of` 在拿不到实测楼数时,回退值应**下限截到 20**
   (当前落到候选 64 是纯虚高),或明确标注「估算」并在看板区分实测/估算两列。
5. 留意:`appendMore` 在 hidden tab 是否真能完成网络加载,需**真机 A/B**(可见 vs hidden)
   验证后再上线;本次只读,未验证其副作用与风控影响。

## 6. 本次审核方法(可复现)

- 进程:daemon `python3 -u tools/linuxdo_browse_daemon.py watch`(PID 8032,14:48 起),含 R579b(HEAD c03bb21)。
- 源码:grep + sed 直读 daemon;`node --check` 校验 JS 常量;python 聚合 `browse_log.jsonl`。
- 运行时:复用 daemon 自己的 `run_js(session_id, js)` 只读下发,**不点击、不导航、不改任何状态**。
- 未改任何文件(仅新增本报告);未执行任何浏览/回复动作。

## 7. 未验证 / 边界

- 「后台 tab 不触发 Discourse 续载」的**浏览器机制**仍属推断(未读 Discourse 源码)。
- `appendMore()` 在 hidden tab 能否真正拉回数据:未做真机实验。
- 原报告话题 id(2987481/2996869)与本次实测话题 id 不同,属不同现场,结论一致即互证。
- 楼层高估的幅度按「真实 ≤20/帖」粗算;若某些帖确实不滚也加载了 >20 楼(未见证据),该值会略变。

## 8. 结论

**是的,是问题。** 「每帖只加载 20 楼」这一主结论成立且已被独立复现;
并且本次另查出**该结论在 R579b 同批遗留的三处缺陷**(探针语法坏、自测假绿、记账虚高),
使「只加载 20 楼」这个瓶颈在**账本上被掩盖**——账面楼层涨得比真实读到的高。
建议按 §5.1 修,且任何改动都须先经用户批准(改的是自身源码)并在真实 tab 上 A/B 验证。
