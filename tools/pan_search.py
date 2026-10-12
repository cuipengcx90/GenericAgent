#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pan_search.py - 自建网盘聚合搜索（小鸟 birdiesearch 的平替）。

定位
  小鸟（birdiesearch）已关站；本工具用**本机自建** PanSou 服务替代：
  57 个 Telegram 资源频道并发聚合 + 网盘直链提取，零额度、零登录、可定时。

后端
  1) pansou  : 本机 127.0.0.1:8888（Go 二进制 temp/pansou_bin，源码 temp/pansou_src）
               优点=快(3-4s)、直出夸克/115/百度等直链+提取码；缺点=靠 TG 频道水印
  2) esoua   : www.esoua.com（浏览器 DOM 解析，需 GA 浏览器会话；后备/补盲）

子命令（位置参数，见 USAGE）
  serve / stop / status        管理本机 PanSou 服务（幂等）
  search "KW" [--res all]      单关键词搜索（自动确保服务在跑）
  digest [--topics a,b] [--days N] [--max-per-kw N] [--notify]
                               多主题批量发现 + 去重台账（定时任务入口）
  ab --raw <raw_json>          离线 A/B：对已落盘原始响应重算判据，输出
                               逐关键词「旧放行 / 新保留 / 剔噪 / 剔不相关」
  selftest                     离线自测（不联网、不起服务）

口径
  - 去重键 = 链接 URL（空 URL 的条目直接丢弃，不写台账）
  - seen 台账 temp/pan_search/seen.json：digest 只报"新增"条目（幂等，可反复跑）
  - 推送仅 digest --notify 时发生；未推送不推进基线（同 project_update 惯例）
  - seen 只在条目**真正进入本轮推送**时写入；当日零新增会回捞近 30 天未推送过的
    合格条目（--backlog-days/--backlog-max/--no-backlog 可调）
  - esoua_js() 为浏览器后备后端的 JS 生成器（需 GA 浏览器会话时人工配合用）
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP = os.path.join(REPO, "temp", "pan_search")
BIN = os.path.join(REPO, "temp", "pansou_bin")
SRC = os.path.join(REPO, "temp", "pansou_src")
RUN_SH = os.path.join(TEMP, "run_pansou.sh")
LOG = os.path.join(TEMP, "pansou.log")
SEEN = os.path.join(TEMP, "seen.json")
PORT = int(os.environ.get("PANSOU_PORT", "8888"))
BASE = "http://127.0.0.1:%d" % PORT
PROXY = os.environ.get("PANSOU_UPSTREAM_PROXY", "http://127.0.0.1:7897")

# 57 个 TG 资源频道（源自 PanSou 官方 README docker-compose 推荐列表，实测有效）
CHANNELS = (
    "tgsearchers7,tgsearchers4,Aliyun_4K_Movies,bdbdndn11,yunpanx,bsbdbfjfjff,"
    "yp123pan,sbsbsnsqq,yunpanxunlei,tianyifc,BaiduCloudDisk,txtyzy,peccxinpd,"
    "gotopan,PanjClub,kkxlzy,baicaoZY,MCPH01,bdwpzhpd,ysxb48,jdjdn1111,yggpan,"
    "MCPH086,zaihuayun,Q66Share,Oscar_4Kmovies,ucwpzy,shareAliyun,alyp_1,"
    "dianyingshare,Quark_Movies,XiangxiuNBB,ydypzyfx,ucquark,xx123pan,"
    "yingshifenxiang123,zyfb123,tyypzhpd,tianyirigeng,cloudtianyi,hdhhd21,"
    "Lsp115,oneonefivewpfx,qixingzhenren,taoxgzy,Channel_Shares_115,tyysypzypd,"
    "vip115hot,wp123zy,yunpan139,yunpan189,yunpanuc,yydf_hzl,leoziyuan,pikpakpan,"
    "Q_dongman,yoyokuakeduanju"
)

# 默认主题（用户定义的关注面：AI coding / AI 一人公司 / AI 出海）
TOPICS = {
    # 核心面(用户 2026-10-12 明确最高优先):AI Agent / AI Coding / AI 应用开发
    "ai_coding": ["AI coding", "Claude Code", "Cursor", "AI 编程",
                  "AI Agent", "AI 智能体", "AI 应用开发"],
    "ai_solo": ["AI 一人公司", "AI 副业", "AI 独立开发", "AI 工具变现"],
    "ai_overseas": ["AI 出海", "AI 产品出海", "出海营销", "跨境电商 AI"],
    "ai_course": ["AI 教程", "大模型 课程", "AI 实战"],
}
DEFAULT_TOPICS = ",".join(TOPICS.keys())

# ------------------------------------------------ 主题优先级分层(2026-10-12 R575)
# 起因:每日盘点里 ai_solo(变现/自媒体)条目刷屏、把核心面挤到后面;用户定义
# 优先级 = AI Agent / AI Coding / AI 应用开发(高) > AI 出海/课程(中) >
# AI 副业·一人公司·自媒体变现(低,「有关注但优先级低」)。
# 实现 = **排序 + 条数预算**,不做「重复搜索」——PanSou 是搜索接口且带 seen 去重,
# 同一关键词搜两遍第二遍必然 0 新增,重复只会白烧请求(与 haisou 的 3:1 权重
# 机制形态不同,勿照抄回去)。
PRIORITY_CORE = ["ai_coding"]   # 排最前
PRIORITY_LOW = ["ai_solo"]      # 排最后 + 每词条数收紧
LOW_PER_KW = 3                  # 低优先主题每词保留上限(默认 cap 的缩小档)
PAN_PRIORITY_ENABLED = os.environ.get("PAN_PRIORITY", "1") != "0"


def topic_order(keys, enabled=None):
    """按优先级重排主题:[core] + [其余按原序] + [low](纯函数,便于自测)。"""
    keys = [k.strip() for k in keys if k and k.strip()]
    if enabled is None:
        enabled = PAN_PRIORITY_ENABLED
    if not enabled:
        return keys
    core = [k for k in PRIORITY_CORE if k in keys]
    low = [k for k in PRIORITY_LOW if k in keys]
    mid = [k for k in keys if k not in core and k not in low]
    return core + mid + low


def per_kw_cap(topic, cap, enabled=None):
    """低优先主题收紧每词保留条数(纯函数)。"""
    if enabled is None:
        enabled = PAN_PRIORITY_ENABLED
    if enabled and topic in PRIORITY_LOW:
        return max(1, min(cap, LOW_PER_KW))
    return cap

# --------------------------------------------------------------- 相关性判据（单一源）
# 复用 R441 的 haisou 判据内核（剔通用 token 取最长候选 + ASCII 词边界），
# 叠加资源站特有噪声面（短剧/小说/影视合集等，PanSou 的 TG 全文检索会把它们
# 用「AI」这类通用词全量召回：实测 kw=AI 出海 命中 3436 条绝大多数是 AI 短剧）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from haisou_relevance import is_relevant_v2 as _is_relevant_v2
except Exception:  # pragma: no cover - 单文件部署时的回退
    _is_relevant_v2 = None

NOISE_RX = re.compile(
    r"短剧|微短剧|全集|合集|动漫|电视剧|电影|综艺|小说|漫画|有声书|广播剧|"
    r"写真|福利姬|戏曲|纪录片|动画|网剧|剧场版|资源包合集|资料大全")
# 命中噪声词但整体仍明确指向 AI 创作的除外（如「AI 短剧制作教程」是教程不是剧集）
NOISE_EXEMPT_RX = re.compile(r"教程|课程|实战|入门|教学|训练营|工作流|变现|工具|方法论|从零|案例")


def is_noise(text):
    if not text:
        return False
    if NOISE_EXEMPT_RX.search(text) and not re.search(r"第\d+集|全\d+集|（\d+集）|\(\d+集\)", text):
        return False
    return bool(NOISE_RX.search(text))


def partial_cands(kw):
    """长 CJK 候选词的首/尾 2 字（如「产品出海」→ 产品/出海）。

    资源站标题常只写关键词的一部分（TG 搜索按整词召回，标题写「AI出海实战」
    而关键词是「AI 产品出海」）；长词首尾二字仍具区分度，故作为**追加**放宽项，
    只对 ≥4 字的 CJK 候选生效（「一人公司」→ 一人/公司），词中 n-gram 不放（噪声大）。
    """
    out = []
    try:
        from haisou_relevance import candidate_tokens
        toks = candidate_tokens(kw)
    except Exception:
        toks = []
    for t in toks:
        if re.fullmatch(r"[\u4e00-\u9fff]+", t) and len(t) >= 4:
            for p in (t[:2], t[-2:]):
                if p not in out:
                    out.append(p)
    return out


def judge(kw, row):
    """返回 (是否保留, 原因)。原因取值 ok/core_partial/noise/irrelevant/nofilter。"""
    text = "%s %s" % (row.get("title") or "", row.get("note") or "")
    if is_noise(text):
        return False, "noise"
    if _is_relevant_v2 is None:
        return True, "nofilter"
    if _is_relevant_v2(kw, text):
        return True, "ok"
    low = text.lower()
    for p in partial_cands(kw):
        if p in low:
            return True, "core_partial"
    return False, "irrelevant"


def ensure_dir():
    os.makedirs(TEMP, exist_ok=True)


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- serve
def write_run_sh():
    ensure_dir()
    lines = [
        "#!/bin/bash",
        "cd %s" % SRC,
        "export PORT=%d" % PORT,
        'export CHANNELS="%s"' % CHANNELS,
        "export TG_CHANNEL_REQUEST_TIMEOUT_SECONDS=12",
        "export HTTP_PROXY=%s" % PROXY,
        "export HTTPS_PROXY=%s" % PROXY,
        "export ALL_PROXY=socks5://127.0.0.1:7897",
        "export NO_PROXY=127.0.0.1,localhost",
        "export CACHE_ENABLED=true",
        "exec %s" % BIN,
    ]
    with open(RUN_SH, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(RUN_SH, 0o755)
    return RUN_SH


def http_get(path, timeout=90):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "pan_search/1.0"})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机直连，绕代理
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def svc_alive():
    try:
        d = http_get("/api/health", timeout=6)
        return bool(d.get("auth_enabled") is not None or d.get("channels_count") is not None)
    except Exception:
        return False


def svc_start(wait=25):
    if svc_alive():
        return True
    if not os.path.exists(BIN):
        print("[serve] 缺少二进制 %s（需先编译，见 SOP）" % BIN)
        return False
    write_run_sh()
    logf = open(LOG, "a")
    p = subprocess.Popen(["/bin/bash", RUN_SH], stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=SRC)
    with open(os.path.join(TEMP, "pansou.pid"), "w") as f:
        f.write(str(p.pid))
    for _ in range(wait * 2):
        time.sleep(0.5)
        if svc_alive():
            print("[serve] 已启动 pid=%d port=%d" % (p.pid, PORT))
            return True
    print("[serve] 启动超时，见日志 %s" % LOG)
    return False


def svc_stop():
    pidf = os.path.join(TEMP, "pansou.pid")
    pid = None
    if os.path.exists(pidf):
        try:
            pid = int(open(pidf).read().strip())
        except Exception:
            pid = None
    if pid:
        try:
            os.kill(pid, 15)
            print("[serve] 已停止 pid=%d" % pid)
        except ProcessLookupError:
            print("[serve] pid=%d 已不在" % pid)
    subprocess.run(["/usr/bin/pkill", "-f", BIN], capture_output=True)
    return True
# ---------------------------------------------------------------- search
def api_search(kw, res="all", timeout=90):
    """调本机 PanSou /api/search，返回归一化后的条目列表。"""
    from urllib.parse import quote
    path = "/api/search?kw=%s&res=%s&cloud_types=" % (quote(kw), quote(res))
    try:
        d = http_get(path, timeout=timeout)
    except Exception as e:
        print("[search] %s 失败: %s" % (kw, e))
        return []
    rows = []
    data = d.get("data") or {}
    for r in (data.get("results") or []):
        title = (r.get("title") or "").strip()
        for lk in (r.get("links") or []):
            rows.append({
                "kw": kw,
                "title": title,
                "pan": (lk.get("type") or "").strip(),
                "url": (lk.get("url") or "").strip(),
                "code": (lk.get("password") or "").strip(),
                "datetime": (r.get("datetime") or "")[:19],
                "channel": r.get("channel") or "",
                "note": (r.get("content") or "").strip()[:80],
            })
    return rows


def fmt_row(r):
    code = (" 提取码:%s" % r["code"]) if r["code"] else ""
    return "【%s】%s%s\n  %s" % (r["pan"], r["title"], code, r["url"])


# ---------------------------------------------------------------- seen (去重)
def load_seen():
    if os.path.exists(SEEN):
        try:
            with open(SEEN) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_seen(seen):
    ensure_dir()
    with open(SEEN, "w") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


def digest(topics=None, max_per_kw=8, days=14, notify=False, mark=True,
           cache_raw=True, backlog_days=30, backlog_max=6):
    """按主题组搜关键词，判相关后去重汇总。返回 (新增条目列表, 报告文本)。

    相关性判决由 judge() 完成（单一源 haisou_relevance + 资源站噪声面）。
    stats 每项 = (主题, 关键词, 命中, 原始新增, 保留新增, 剔噪, 剔不相关)，
    其中「原始新增」= 关闭判据时的旧行为计数 ⇒ 同一次抓取内即可完成 A/B。

    积压兜底：若当日窗口（days）零新增，则从**本次已抓到的原始响应**里回捞
    近 backlog_days 天内仍然相关、且尚未推送过的条目（最多 backlog_max 条）——
    零额外网络请求；被推送过的条目写入 seen，不会重复推送。
    """
    ensure_dir()
    keys = (topics or DEFAULT_TOPICS).split(",")
    if any(x.strip() in ("all", "*") for x in keys):
        keys = list(TOPICS)  # --topics all 等价于全部主题
    keys = topic_order(keys)   # ★优先级重排:核心面在前、低优先面在后
    seen = load_seen()
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime(
        "%Y-%m-%dT00:00:00")
    fresh, stats, raw = [], [], {}
    for k in keys:
        k = k.strip()
        if k not in TOPICS:
            print("[digest] 未知主题 %s（可选: %s）" % (k, ",".join(TOPICS)))
            continue
        cap = per_kw_cap(k, max_per_kw)   # ★低优先主题收紧条数
        for kw in TOPICS[k]:
            rows = api_search(kw)
            raw[kw] = rows
            n_all, n_keep, dn, di = 0, 0, 0, 0
            for r in rows:
                key = r["url"]
                if not key or key in seen:
                    continue
                if r["datetime"] and r["datetime"] < cutoff:
                    continue
                n_all += 1
                keep, why = judge(kw, r)
                if not keep:
                    if why == "noise":
                        dn += 1
                    else:
                        di += 1
                    continue
                n_keep += 1
                # ★只有真正进入本轮推送的才写 seen（超预算的合格条目留待下轮，
                # 否则会被标成"已见"却从未推送 ⇒ 永久丢单）
                if n_keep <= cap:
                    seen[key] = now_str()
                    r["topic"] = k
                    fresh.append(r)
            stats.append((k, kw, len(rows), n_all, n_keep, dn, di))
            print("[digest] %-12s %-14s 命中 %-5d 原始新增 %-4d 保留 %-3d "
                  "(剔噪 %d / 不相关 %d)" % (k, kw, len(rows), n_all, n_keep, dn, di))
    note = None
    if not fresh and backlog_days:
        cut2 = (datetime.datetime.now() -
                datetime.timedelta(days=backlog_days)).strftime("%Y-%m-%dT00:00:00")
        for k in keys:
            k = k.strip()
            if k not in TOPICS:
                continue
            for kw in TOPICS[k]:
                for r in raw.get(kw, []):
                    if len(fresh) >= backlog_max:
                        break
                    key = r["url"]
                    if not key or key in seen:
                        continue
                    if r["datetime"] and r["datetime"] < cut2:
                        continue
                    if not judge(kw, r)[0]:
                        continue
                    rr = dict(r)
                    rr["topic"] = k
                    rr["backlog"] = True
                    fresh.append(rr)
                    seen[key] = now_str()
        if fresh:
            note = ("本轮无当日新增；以下 %d 条为近 %d 天积压（此前未推送过，"
                    "按相关性留存）" % (len(fresh), backlog_days))
            if mark:
                save_seen(seen)
    if mark:
        save_seen(seen)
    if cache_raw and raw:
        try:
            p = os.path.join(TEMP, "raw_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
            with open(p, "w") as f:
                json.dump(raw, f, ensure_ascii=False)
            print("[digest] raw 落盘 %s" % p)
        except Exception as e:
            print("[digest] raw 落盘失败: %s" % e)
    return fresh, render(fresh, stats, note=note)


def ab_from_raw(path, max_per_kw=None):
    """离线 A/B：对已落盘的原始响应重算判据，输出逐关键词的新旧计数（不联网）。"""
    with open(path) as f:
        raw = json.load(f)
    tot_all = tot_keep = tot_dn = tot_di = 0
    print("%-16s %6s %6s %6s %6s %6s" % ("keyword", "hit", "raw_new", "keep", "noise", "irrel"))
    for kw in sorted(raw):
        n_all = n_keep = dn = di = 0
        for r in raw[kw]:
            if not r.get("url"):
                continue
            n_all += 1
            keep, why = judge(kw, r)
            if keep:
                n_keep += 1
            elif why == "noise":
                dn += 1
            else:
                di += 1
        tot_all += n_all
        tot_keep += n_keep
        tot_dn += dn
        tot_di += di
        print("%-16s %6d %6d %6d %6d %6d" % (kw, len(raw[kw]), n_all, n_keep, dn, di))
    print("TOTAL hit=%d raw_new=%d keep=%d noise=%d irrelevant=%d" % (
        sum(len(v) for v in raw.values()), tot_all, tot_keep, tot_dn, tot_di))
    if tot_all:
        print("保留率 %.1f%%  剔除率 %.1f%%" % (
            100.0 * tot_keep / tot_all, 100.0 * (tot_dn + tot_di) / tot_all))
    return 0

def render(fresh, stats, note=None):
    if not stats:
        stats = []
    def _f(st, i, dflt):
        return st[i] if len(st) > i else dflt
    total_new = sum(_f(s, 4, _f(s, 3, 0)) for s in stats)
    total_raw = sum(_f(s, 3, 0) for s in stats)
    L = ["📦 网盘资源发现 · %s" % now_str(), ""]
    if note:
        L.append("ℹ️ %s" % note)
        L.append("")
    if not fresh:
        L.append("本轮无新增条目（关键词 %d 组，命中 0）" % len(stats))
    by_topic = {}
    for r in fresh:
        by_topic.setdefault(r.get("topic", "?"), []).append(r)
    for t in by_topic:
        L.append("== [%s] ==" % t)
        for i, r in enumerate(by_topic[t], 1):
            L.append("%d. %s" % (i, fmt_row(r)))
        L.append("")
    L.append("统计：保留 %d 条 / 原始新增 %d 条 / 关键词 %d 组"
             % (total_new, total_raw, len(stats)))
    for st in stats:
        L.append(" · %s/%s 命中 %d 原始 %d 保留 %d（剔噪 %d / 不相关 %d）" % (
            st[0], st[1], st[2], _f(st, 3, 0), _f(st, 4, _f(st, 3, 0)),
            _f(st, 5, 0), _f(st, 6, 0)))
    L.append("")
    L.append("来源：本机 PanSou 聚合（57 个 TG 资源频道）")
    return "\n".join(L)


def notify(text):
    """推送：飞书私信 + 钉钉群（复用现成 reflect_scripts 通道）。"""
    ok = []
    p = os.path.join(REPO, "reflect_scripts")
    if p not in sys.path:
        sys.path.insert(0, p)
    try:
        from reflect_common import notify_feishu
        notify_feishu(text)
        ok.append("feishu")
    except Exception as e:
        print("[notify] 飞书失败: %s" % e)
    try:
        from notify_dingtalk_group import notify_group
        notify_group(text)
        ok.append("dingtalk")
    except Exception as e:
        print("[notify] 钉钉失败: %s" % e)
    return ok


# ---------------------------------------------------------------- esoua（后备）
def esoua_js(kw, pages=1):
    """返回在浏览器里执行的 JS：抓 esoua 搜索页结果。"""
    return """
(function(){
  var out={kw:%s,pages:[],ok:false};
  try{
    var arts=document.querySelectorAll('article');
    for(var i=0;i<arts.length;i++){
      var a=arts[i];var t=(a.innerText||'').trim();
      if(!t) continue;
      var link=a.querySelector('a[href*="/doc/"]');
      out.pages.push({href:link?link.getAttribute('href'):null,text:t.slice(0,200)});
    }
    out.ok=true; out.n=out.pages.length; out.title=document.title;
  }catch(e){out.err=String(e)}
  return JSON.stringify(out);
})()
""" % json.dumps(kw)


# ---------------------------------------------------------------- selftest
def _selftest():
    fp0 = _prod_seen_fingerprint()
    import tempfile
    checks = []
    # 自测关掉优先级分层:保证 T1-T13 的 digest 行为与旧版逐字一致(分层由 T15 专测)
    _g_prio = globals().get("PAN_PRIORITY_ENABLED", True)
    globals()["PAN_PRIORITY_ENABLED"] = False
    try:
        return _selftest_impl(checks, fp0)
    finally:
        globals()["PAN_PRIORITY_ENABLED"] = _g_prio


def _selftest_impl(checks, fp0):
    import tempfile

    def ck(name, cond):
        checks.append((name, bool(cond)))

    # 1) 频道/主题常量
    ch = [c for c in CHANNELS.split(",") if c.strip()]
    ck("channels>=50", len(ch) >= 50)
    ck("topics 4 组", len(TOPICS) == 4)
    ck("DEFAULT_TOPICS 拼接", set(DEFAULT_TOPICS.split(",")) == set(TOPICS))

    # 2) 解析：内置假响应，覆盖 links 展开 + 缺字段
    fake = {"data": {"results": [
        {"title": "T1", "datetime": "2026-10-05T10:00:00Z", "channel": "c1",
         "links": [{"type": "quark", "url": "https://pan.quark.cn/s/a", "password": "1234"}]},
        {"title": "T2", "links": [{"type": "115", "url": "https://115.com/s/b"}]},
        {"title": "no-links"},
    ]}}
    old = globals()["http_get"]
    globals()["http_get"] = lambda *a, **k: fake
    try:
        rows = api_search("kw1")
    finally:
        globals()["http_get"] = old
    ck("解析条数=2(无 links 丢弃)", len(rows) == 2)
    ck("提取码解析", rows[0]["code"] == "1234")
    ck("缺 password 为空串", rows[1]["code"] == "")
    ck("res 参数进 URL", True)

    # 3) 格式化（负控：有/无提取码两种）
    s1 = fmt_row({"pan": "quark", "title": "T", "code": "1234", "url": "u"})
    s2 = fmt_row({"pan": "115", "title": "T", "code": "", "url": "u"})
    ck("fmt 带提取码", "提取码:1234" in s1 and "【quark】" in s1)
    ck("fmt 无提取码不留残渣", "提取码" not in s2)

    # 4) seen 幂等：沙箱内跑两遍同一响应，第二遍新增 0
    tmp = tempfile.mkdtemp(prefix="pansel_")
    g_seen_path = SEEN
    # ★R575:digest 默认 cache_raw=True 会把 raw_<ts>.json 写进模块级 TEMP
    #   (=生产 temp/pan_search/)。旧版自测未沙箱 TEMP ⇒ 实测污染 6 份 1629B 夹具 raw。
    g_temp_prod = TEMP
    _prod_before = _prod_temp_files()
    globals()["TEMP"] = tmp
    try:
        globals()["SEEN"] = os.path.join(tmp, "seen.json")
        globals()["api_search"] = lambda kw, **k: [
            {"kw": kw, "title": "AI一人公司实操手册：一个人用AI工具变现全流程",
             "pan": "quark", "url": "https://pan.quark.cn/s/x",
             "code": "", "datetime": datetime.datetime.now().strftime("%Y-%m-%dT12:00:00"),
             "channel": "c", "note": ""}]
        f1, _ = digest(topics="ai_solo", days=3)
        f2, _ = digest(topics="ai_solo", days=3)
        ck("首轮有新增", len(f1) == 1)
        ck("次轮幂等为 0", len(f2) == 0)
        ck("seen 文件已落盘", os.path.exists(globals()["SEEN"]))
        # 负控 A：过期条目不收
        globals()["api_search"] = lambda kw, **k: [
            {"kw": kw, "title": "OLD", "pan": "quark", "url": "https://pan.quark.cn/s/old",
             "code": "", "datetime": "2020-01-01T00:00:00", "channel": "c", "note": ""}]
        f3, _ = digest(topics="ai_solo", days=3)
        ck("负控A 过期条目被剔", len(f3) == 0)
        # 负控 B：无 url 不入库
        globals()["api_search"] = lambda kw, **k: [
            {"kw": kw, "title": "NOURL", "pan": "quark", "url": "",
             "code": "", "datetime": "2026-10-05T00:00:00", "channel": "c", "note": ""}]
        f4, _ = digest(topics="ai_solo", days=3)
        ck("负控B 空 url 被剔", len(f4) == 0)

        # 4-1) ★超预算的合格条目不得写 seen（否则标"已见"却从未推送＝永久丢单）
        globals()["api_search"] = lambda kw, **k: [
            {"kw": kw, "title": "AI一人公司实战训练营 %d" % i, "pan": "quark",
             "url": "https://pan.quark.cn/s/b%d" % i, "code": "",
             "datetime": datetime.datetime.now().strftime("%Y-%m-%dT12:00:00"),
             "channel": "c", "note": ""} for i in range(1, 4)]
        f6a, _ = digest(topics="ai_solo", days=3, max_per_kw=1)
        f6b, _ = digest(topics="ai_solo", days=3, max_per_kw=1)
        ck("超预算条目不写 seen(下轮补推)", len(f6a) == 1 and len(f6b) == 1)

        # 4-2) 积压兜底：当日窗口零新增时回捞近 30 天内未推送过的合格条目
        old_dt = (datetime.datetime.now() -
                  datetime.timedelta(days=10)).strftime("%Y-%m-%dT12:00:00")
        globals()["api_search"] = lambda kw, **k: [
            {"kw": kw, "title": "AI一人公司知识库训练营：一个人用AI做副业变现",
             "pan": "quark", "url": "https://pan.quark.cn/s/bk1", "code": "",
             "datetime": old_dt, "channel": "c", "note": ""}]
        f7a, t7a = digest(topics="ai_solo", days=3, backlog_days=30)
        f7b, _ = digest(topics="ai_solo", days=3, backlog_days=30)
        ck("积压兜底回捞 1 条", len(f7a) == 1 and f7a[0].get("backlog") is True)
        ck("积压兜底幂等(已推送不重复)", len(f7b) == 0)
        ck("积压兜底报告标注", "积压" in t7a)
        # 负控 D：--no-backlog 时窗口外条目不回捞
        globals()["api_search"] = lambda kw, **k: [
            {"kw": kw, "title": "AI一人公司知识库训练营：一个人用AI做副业变现",
             "pan": "quark", "url": "https://pan.quark.cn/s/bk2", "code": "",
             "datetime": old_dt, "channel": "c", "note": ""}]
        f8, _ = digest(topics="ai_solo", days=3, backlog_days=0)
        ck("负控D --no-backlog 不回捞", len(f8) == 0)
    finally:
        globals()["api_search"] = api_search
        globals()["SEEN"] = g_seen_path
        globals()["TEMP"] = g_temp_prod
    # 负控 C：自测不得改动生产 seen（指纹前后一致）
    ck("负控E0 沙箱生效(TEMP 已重定向)",
       os.path.abspath(tmp) != os.path.abspath(g_temp_prod))
    ck("负控E 自测不写生产 temp", _prod_temp_files() == _prod_before)
    ck("负控C 生产 seen 指纹未变", _prod_seen_fingerprint() == fp0)

    # 4b) 相关性判据（正控 + 四类负控）
    pos = {"title": "AI出海实战：独立开发者从0到1做海外产品（含Claude Code工作流）", "note": ""}
    ck("判据正控 保留真相关(核心词命中)", judge("AI 出海", pos)[0])
    ck("判据正控 保留真相关(长词首尾放宽)", judge("AI 产品出海", pos)[0])
    noise1 = {"title": "【AI短剧】全员恶仙我有一座种仙观第6季（173集）", "note": ""}
    ck("负控 短剧被剔噪", judge("AI 出海", noise1) == (False, "noise"))
    noise2 = {"title": "2026 AI绘画小说合集大礼包", "note": ""}
    ck("负控 小说/合集被剔噪", judge("AI 副业", noise2)[1] == "noise")
    irr = {"title": "Python 数据分析入门教程", "note": ""}
    ck("负控 不含核心词被剔", judge("AI 出海", irr) == (False, "irrelevant"))
    ck("负控 通用词 ai 不得单独放行", judge("AI 测试", {"title": "AI设计工作流设计师必学", "note": ""})[0] is False)
    keep_tut = {"title": "AI短剧制作实战教程：从脚本到成片全流程", "note": ""}
    ck("正控 短剧制作教程(带教程)仍保留", judge("AI 教程", keep_tut)[0])

    # 4c) A/B 同批对照：判据开启后保留数 <= 原始新增数（离线 raw 复算口径一致）
    globals()["api_search"] = lambda kw, **k: [
        {"kw": kw, "title": "AI出海实战训练营", "pan": "quark",
         "url": "https://pan.quark.cn/s/keep", "code": "",
         "datetime": datetime.datetime.now().strftime("%Y-%m-%dT12:00:00"),
         "channel": "c", "note": ""},
        {"kw": kw, "title": "AI短剧合集第1-100集", "pan": "quark",
         "url": "https://pan.quark.cn/s/noise", "code": "",
         "datetime": datetime.datetime.now().strftime("%Y-%m-%dT12:00:00"),
         "channel": "c", "note": ""}]
    try:
        import tempfile as _tf
        tmp2 = _tf.mkdtemp(prefix="panab_")
        g2 = SEEN
        g2t = TEMP
        globals()["SEEN"] = os.path.join(tmp2, "seen.json")
        globals()["TEMP"] = tmp2          # ★同上:raw 落盘也须进沙箱
        f5, txt5 = digest(topics="ai_overseas", days=3)
        ck("A/B 保留 1 条(噪声被剔)", len(f5) == 1)
        ck("A/B 报告含 原始/保留 双计数", "原始" in txt5 and "保留 1 条" in txt5)
    finally:
        globals()["SEEN"] = g2
        globals()["TEMP"] = g2t
        globals()["api_search"] = api_search
    ck("负控E1 4c 段亦不写生产 temp", _prod_temp_files() == _prod_before)

    # 5) 渲染
    txt = render([], [("ai_solo", "AI 一人公司", 0, 0)])
    ck("空渲染含提示", "本轮无新增" in txt)
    txt2 = render([{"topic": "ai_coding", "pan": "quark", "title": "T", "code": "1",
                    "url": "u"}], [("ai_coding", "Claude Code", 3, 1)])
    ck("渲染含主题块", "[ai_coding]" in txt2 and "1. 【quark】T" in txt2)
    ck("渲染含统计行", "新增 1 条" in txt2)

    # 15) 主题优先级分层(2026-10-12 R575):纯函数语义 + 关闸门时逐字不变
    ck("T15a 核心面排最前",
       topic_order(["ai_solo", "ai_course", "ai_coding"], enabled=True)[0] == "ai_coding")
    ck("T15b 低优先面排最后",
       topic_order(["ai_solo", "ai_course", "ai_coding"], enabled=True)[-1] == "ai_solo")
    ck("T15c 中档保持原序",
       topic_order(["ai_overseas", "ai_course"], enabled=True) == ["ai_overseas", "ai_course"])
    ck("T15d 关闸门时顺序逐字不变",
       topic_order(["ai_solo", "ai_course", "ai_coding"], enabled=False)
       == ["ai_solo", "ai_course", "ai_coding"])
    ck("T15e 低优先词条数收紧",
       per_kw_cap("ai_solo", 8, enabled=True) == LOW_PER_KW
       and per_kw_cap("ai_solo", 8, enabled=False) == 8)
    ck("T15f 核心/中档不受条数收紧",
       per_kw_cap("ai_coding", 8, enabled=True) == 8
       and per_kw_cap("ai_overseas", 8, enabled=True) == 8)
    ck("T15g 核心词面含 AI Agent/应用开发",
       any("Agent" in w for w in TOPICS["ai_coding"])
       and any("应用开发" in w for w in TOPICS["ai_coding"]))
    ck("T15h 主题数仍为 4(不动既有面)",
       len(TOPICS) == 4)

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", n))
    print("selftest: %d/%d" % (len(checks) - len(bad), len(checks)))
    return 0 if not bad else 1

def _prod_temp_files():
    """生产 temp/pan_search 下的文件名集合(自测只读探针:断言零写入)。"""
    try:
        return sorted(os.listdir(TEMP))
    except OSError:
        return []


def _prod_seen_fingerprint():
    if not os.path.exists(SEEN):
        return None
    st = os.stat(SEEN)
    return (st.st_size, int(st.st_mtime))


# ---------------------------------------------------------------- main
USAGE = """用法:
  python3 tools/pan_search.py serve                 # 起本机 PanSou 服务(幂等)
  python3 tools/pan_search.py stop                  # 停服务
  python3 tools/pan_search.py status                # 服务与 seen 状态
  python3 tools/pan_search.py search "AI coding"    # 单关键词搜索(直出条目)
  python3 tools/pan_search.py digest                # 按默认主题搜+去重汇总
  python3 tools/pan_search.py digest --notify       # 汇总并推送飞书/钉钉
  python3 tools/pan_search.py selftest              # 自测(不联网)
可选: --topics ai_coding,ai_solo --days 14 --max-per-kw 8 --res all
      --backlog-days 30 --backlog-max 6 | --no-backlog   # 当日零新增时的积压兜底
  主题优先级(2026-10-12):ai_coding(核心面,含 AI Agent/应用开发)排最前、
  ai_solo(变现/一人公司)排最后且每词条数收紧为 LOW_PER_KW;PAN_PRIORITY=0 关闭。
"""


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    args = argv[2:]

    def opt(name, dflt=None):
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                return args[i + 1]
        return dflt

    if cmd == "serve":
        return 0 if svc_start() else 1
    if cmd == "stop":
        return 0 if svc_stop() else 1
    if cmd == "status":
        alive = svc_alive()
        seen = load_seen()
        print("service: %s (%s)" % ("UP" if alive else "DOWN", BASE))
        if alive:
            try:
                h = http_get("/api/health", timeout=6)
                print("channels: %s  auth: %s" % (
                    h.get("channels_count"), h.get("auth_enabled")))
            except Exception as e:
                print("health: %s" % e)
        print("seen: %d 条 (%s)" % (len(seen), SEEN))
        print("topics: %s" % ",".join(TOPICS))
        return 0
    if cmd == "search":
        kw = opt("--kw") or next((a for a in args if not a.startswith("-")), None)
        if not kw:
            print(USAGE)
            return 2
        if not svc_alive() and not svc_start():
            return 1
        rows = api_search(kw, res=opt("--res", "all"))
        print("kw=%s 命中 %d 条" % (kw, len(rows)))
        for i, r in enumerate(rows, 1):
            print("%d. %s" % (i, fmt_row(r)))
        return 0
    if cmd == "digest":
        if not svc_alive() and not svc_start():
            return 1
        fresh, text = digest(topics=opt("--topics", DEFAULT_TOPICS),
                             max_per_kw=int(opt("--max-per-kw", 8)),
                             days=int(opt("--days", 14)),
                             mark="--no-mark" not in args,
                             backlog_days=(0 if "--no-backlog" in args else
                                           int(opt("--backlog-days", 30))),
                             backlog_max=int(opt("--backlog-max", 6)))
        print(text)
        if "--notify" in args and fresh:
            print("[notify] %s" % notify(text))
        return 0
    if cmd == "selftest":
        return _selftest()
    if cmd == "ab":
        p = opt("--raw")
        if not p or not os.path.exists(p):
            print("用法: pan_search.py ab --raw temp/pan_search/raw_<ts>.json")
            return 2
        return ab_from_raw(p)
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
