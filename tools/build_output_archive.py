#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地推送归档站生成器
扫描 output/ 下所有按日期命名的推送产物，生成一个自包含的单页 HTML 归档站。
- 自包含：所有内容内嵌，离线可看、可整份文件分享
- 功能：全文搜索 + 类型筛选 + 按日期倒序浏览 + 移动端适配
用法：python3 tools/build_output_archive.py
产物：<ROOT>/archive/index.html   (放在 output 之外，避免 WatchPaths 自触发)
"""
import os, re, json, html, datetime, glob

ROOT = "/Users/cuip/projects/GenericAgent"
OUT_DIR = os.path.join(ROOT, "output")
ARCHIVE_DIR = os.path.join(ROOT, "archive")
import sys as _sys
_sys.path.insert(0, os.path.join(ROOT, "tools"))
from lan_ip import archive_url as _archive_url  # 家庭 WiFi DHCP，IP 每天漂，动态探测当天真实 LAN IP
ARCHIVE_URL = _archive_url()
STATE_FILE = os.path.join(ARCHIVE_DIR, ".push_state.json")  # 去重状态：仅内容变化时才私信

# 类型定义：前缀 -> (图标, 中文标签, 主题色)
TYPES = {
    "tech_trends":         ("📊", "技术趋势榜",     "#3b82f6"),
    "trendradar_morning":  ("🌅", "IT情报晨报",     "#f59e0b"),
    "trendradar_evening":  ("🌙", "IT情报晚报",     "#8b5cf6"),
    "hellogithub_monthly": ("📦", "HelloGitHub月报", "#10b981"),
    "wechat_weekly":       ("💬", "微信信号周报",   "#06b6d4"),
    "github_trending":     ("📈", "GitHub趋势",     "#ef4444"),
}

URL_RE = re.compile(r'(https?://[^\s<>"\)）]+)')
MDLINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')
ARCHIVE_URL_RE = re.compile(r'http://192\.168\.\d+\.\d+:8899/')
WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def normalize_archive_links(raw: str) -> str:
    """归档页渲染时把历史推送正文里的旧局域网归档链接替换成当天动态 URL。"""
    return ARCHIVE_URL_RE.sub(ARCHIVE_URL, raw)


def parse_name(fn):
    """从文件名解析 (type_key, sortkey(YYYYMMDD数字), 显示日期, weekday)。无法解析返回 None。"""
    base = re.sub(r'\.(md|txt)$', '', fn)
    tkey = None
    for k in sorted(TYPES, key=len, reverse=True):
        if base == k or base.startswith(k + "_"):
            tkey = k
            break
    if not tkey:
        return None
    rest = base[len(tkey):].lstrip("_")
    if not rest:
        return None

    def wd(y, mo, d):
        try:
            return WEEKDAY[datetime.date(int(y), int(mo), int(d)).weekday()]
        except Exception:
            return ""

    m = re.match(r'(\d{4})-(\d{2})-(\d{2})$', rest) or re.match(r'(\d{4})(\d{2})(\d{2})$', rest)
    if m:
        y, mo, d = m.groups()
        return tkey, int(f"{y}{mo}{d}"), f"{y}-{mo}-{d}", wd(y, mo, d)
    m = re.match(r'(\d{4})-(\d{2})$', rest)
    if m:
        y, mo = m.groups()
        return tkey, int(f"{y}{mo}31"), f"{y}-{mo}", ""
    m = re.match(r'(\d{4})_W(\d{1,2})$', rest)
    if m:
        y, w = int(m.group(1)), int(m.group(2))
        try:
            dt = datetime.date.fromisocalendar(y, w, 1)
            return tkey, int(dt.strftime("%Y%m%d")), f"{y} 第{w}周", ""
        except ValueError:
            return tkey, int(f"{y}0101"), f"{y} 第{w}周", ""
    return None


def _inline(s):
    """行内：转义 -> markdown链接 -> 裸链接 -> 粗体 -> 行内代码。"""
    s = html.escape(s)
    # markdown 链接 [text](url) — 注意此时 [] () 已被转义？html.escape 不转义 []()，只转义 &<>"'
    s = MDLINK_RE.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
    s = URL_RE.sub(lambda m: (m.group(0) if '"' in m.group(0) else f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>'), s)
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    return s


def to_html(text):
    """轻量 markdown/纯文本 -> 安全 HTML。支持标题/无序列表/分隔线/粗体/链接/代码。"""
    out, buf_ul = [], []

    def flush_ul():
        if buf_ul:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in buf_ul) + "</ul>")
            buf_ul.clear()

    for line in text.splitlines():
        raw = line.rstrip()
        # 标题
        hm = re.match(r'(#{1,4})\s+(.*)', raw)
        if hm:
            flush_ul()
            lvl = min(len(hm.group(1)) + 1, 6)
            out.append(f'<h{lvl}>{_inline(hm.group(2))}</h{lvl}>')
            continue
        # 分隔线
        if re.match(r'^\s*([-*_])\1{2,}\s*$', raw):
            flush_ul()
            out.append("<hr>")
            continue
        # 无序列表
        lm = re.match(r'^\s*[-*•]\s+(.*)', raw)
        if lm:
            buf_ul.append(_inline(lm.group(1)))
            continue
        flush_ul()
        if raw.strip() == "":
            out.append('<div class="sp"></div>')
        else:
            out.append(f'<p>{_inline(raw)}</p>')
    flush_ul()
    return "\n".join(out)


def first_title(text, fallback):
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        # 去掉行首装饰符
        s = re.sub(r'^[\*\-•\s]+', '', s)
        if s:
            return s[:80]
    return fallback


def build():
    items = []
    for path in glob.glob(os.path.join(OUT_DIR, "*.md")) + glob.glob(os.path.join(OUT_DIR, "*.txt")):
        fn = os.path.basename(path)
        parsed = parse_name(fn)
        if not parsed:
            continue
        tkey, sortkey, disp, wday = parsed
        try:
            with open(path, encoding="utf-8") as f:
                raw = normalize_archive_links(f.read())
        except Exception:
            continue
        icon, label, color = TYPES[tkey]
        items.append({
            "type": tkey, "typeLabel": label, "icon": icon, "color": color,
            "sort": sortkey, "date": disp, "wday": wday,
            "title": first_title(raw, f"{label} {disp}"),
            "html": to_html(raw), "text": raw,
        })
    items.sort(key=lambda x: (x["sort"], x["type"]), reverse=True)

    type_counts = {}
    for it in items:
        type_counts[it["type"]] = type_counts.get(it["type"], 0) + 1

    data_json = json.dumps(items, ensure_ascii=False)
    types_json = json.dumps(
        {k: {"icon": v[0], "label": v[1], "color": v[2], "count": type_counts.get(k, 0)}
         for k, v in TYPES.items() if type_counts.get(k, 0)},
        ensure_ascii=False)
    gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    html_out = (HTML_TEMPLATE
                .replace("__DATA__", data_json)
                .replace("__TYPES__", types_json)
                .replace("__GENTIME__", gen_time)
                .replace("__TOTAL__", str(len(items))))
    out_path = os.path.join(ARCHIVE_DIR, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"OK 归档站已生成: {out_path}")
    print(f"   共 {len(items)} 篇  |  " + ", ".join(f"{k}x{v}" for k, v in type_counts.items()))
    print(f"   浏览: file://{out_path}")
    # 静默重建：不再单独推"归档已更新"私信（链接已默认追加进每日推送正文）
    # maybe_push(items, type_counts)
    return out_path


def maybe_push(items, type_counts):
    """内容变化时给飞书私信推一条带访问链接的通知（哈希去重，避免同内容重复推）。"""
    import hashlib
    sig = hashlib.md5(
        "|".join(f"{it['type']}:{it['sort']}:{it['title']}" for it in items).encode("utf-8")
    ).hexdigest()
    try:
        prev = json.load(open(STATE_FILE, encoding="utf-8")).get("sig") if os.path.exists(STATE_FILE) else None
    except Exception:
        prev = None
    if sig == prev:
        print("   [推送] 内容未变化，跳过私信")
        return
    latest = items[0] if items else None
    head = f"{latest['icon']} {latest['title']}" if latest else "推送归档更新"
    text = (f"📚 归档站已更新（共 {len(items)} 篇）\n"
            f"最新：{head}\n"
            f"点此浏览全部推送：{ARCHIVE_URL}\n"
            f"（需连家中 WiFi，本机开机时可访问）")
    try:
        import sys
        sys.path.insert(0, os.path.join(ROOT, "reflect_scripts"))
        from reflect_common import notify_feishu
        notify_feishu(text)
        json.dump({"sig": sig}, open(STATE_FILE, "w", encoding="utf-8"))
        print("   [推送] 已私信飞书")
    except Exception as e:
        print(f"   [推送] 失败（不影响归档生成）：{e}")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN" class="">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>推送归档</title>
<script>
  // 主题在渲染前决定，避免闪白
  try { if (localStorage.getItem('arch-theme') === 'dark') document.documentElement.classList.add('dark'); } catch (e) {}
</script>
<script src="https://cdn.tailwindcss.com"></script>
<script>
  tailwind.config = {
    darkMode: 'class',
    theme: { extend: { fontFamily: { sans: ['ui-sans-serif','-apple-system','BlinkMacSystemFont','Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei','sans-serif'] } } }
  };
</script>
<style>
  html { -webkit-text-size-adjust: 100%; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: rgba(120,120,130,.3); border-radius: 8px; }
  /* 正文排版 */
  .prose { font-size: 14.5px; line-height: 1.72; color: rgb(63 63 70); word-break: break-word; }
  .dark .prose { color: rgb(212 212 216); }
  .prose h2,.prose h3,.prose h4,.prose h5,.prose h6 { font-weight: 600; margin: 1.1em 0 .4em; line-height: 1.4; color: rgb(24 24 27); }
  .dark .prose h2,.dark .prose h3,.dark .prose h4 { color: rgb(244 244 245); }
  .prose h2 { font-size: 1.12em; } .prose h3 { font-size: 1.05em; } .prose h4 { font-size: 1em; }
  .prose p { margin: .5em 0; }
  .prose ul { margin: .5em 0; padding-left: 1.25em; list-style: disc; }
  .prose li { margin: .25em 0; }
  .prose a { color: rgb(37 99 235); text-decoration: none; border-bottom: 1px solid rgba(37,99,235,.35); }
  .dark .prose a { color: rgb(96 165 250); }
  .prose code { background: rgba(120,120,130,.14); padding: .1em .4em; border-radius: 6px; font-size: .9em; font-family: ui-monospace,SFMono-Regular,Menlo,monospace; }
  .prose strong { font-weight: 650; color: rgb(24 24 27); }
  .dark .prose strong { color: rgb(250 250 250); }
  .prose hr { border: 0; border-top: 1px solid rgba(120,120,130,.2); margin: 1em 0; }
  .prose .sp { height: .4em; }
  details > summary { list-style: none; cursor: pointer; }
  details > summary::-webkit-details-marker { display: none; }
  details[open] .chev { transform: rotate(180deg); }
  .chev { transition: transform .2s ease; }
  .fade-in { animation: fi .25s ease both; }
  @keyframes fi { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
</style>
</head>
<body class="bg-neutral-50 text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100 font-sans antialiased min-h-screen">

<!-- 顶栏 -->
<header class="sticky top-0 z-30 backdrop-blur-xl bg-white/80 dark:bg-neutral-950/80 border-b border-neutral-200 dark:border-neutral-800">
  <div class="max-w-4xl mx-auto px-4 py-3">
    <div class="flex items-center gap-3">
      <div class="flex items-center gap-2.5 flex-1 min-w-0">
        <div class="w-9 h-9 rounded-xl bg-neutral-900 dark:bg-white flex items-center justify-center text-white dark:text-neutral-900 text-lg shrink-0">&#128238;</div>
        <div class="min-w-0">
          <h1 class="text-[15px] font-semibold leading-tight truncate">推送归档</h1>
          <p class="text-[11px] text-neutral-500 leading-tight">共 __TOTAL__ 篇 · 更新于 __GENTIME__</p>
        </div>
      </div>
      <button id="themeBtn" class="w-9 h-9 rounded-lg border border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800 flex items-center justify-center transition shrink-0" title="切换主题">
        <span class="dark:hidden">&#127769;</span><span class="hidden dark:inline">&#9728;&#65039;</span>
      </button>
    </div>

    <!-- 搜索 -->
    <div class="mt-3 relative">
      <svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-neutral-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
      <input id="q" type="search" placeholder="搜索标题或正文内容…" autocomplete="off"
        class="w-full h-10 pl-9 pr-9 rounded-xl bg-neutral-100 dark:bg-neutral-900 border border-transparent focus:border-neutral-300 dark:focus:border-neutral-700 focus:bg-white dark:focus:bg-neutral-950 outline-none text-sm transition">
      <button id="qclear" class="hidden absolute right-2.5 top-1/2 -translate-y-1/2 w-6 h-6 rounded-md text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-200 hover:bg-neutral-200 dark:hover:bg-neutral-800 flex items-center justify-center text-lg leading-none">&times;</button>
    </div>

    <!-- 筛选行 -->
    <div class="mt-2.5 flex items-center gap-2 flex-wrap">
      <!-- 日期区间按钮 -->
      <div class="relative">
        <button id="dateBtn" class="h-8 px-3 rounded-lg border border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800 flex items-center gap-1.5 text-[13px] font-medium transition">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
          <span id="dateLabel">近 30 天</span>
          <svg class="w-3 h-3 text-neutral-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>
        </button>
        <!-- 弹出面板 -->
        <div id="datePop" class="hidden absolute left-0 mt-2 w-[300px] rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 shadow-xl shadow-black/10 dark:shadow-black/40 p-3 z-40">
          <div class="flex flex-wrap gap-1.5 mb-3">
            <button data-preset="all"  class="preset h-7 px-2.5 rounded-md text-xs border border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800">全部</button>
            <button data-preset="7"    class="preset h-7 px-2.5 rounded-md text-xs border border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800">近 7 天</button>
            <button data-preset="30"   class="preset h-7 px-2.5 rounded-md text-xs border border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800">近 30 天</button>
            <button data-preset="90"   class="preset h-7 px-2.5 rounded-md text-xs border border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800">近 90 天</button>
            <button data-preset="year" class="preset h-7 px-2.5 rounded-md text-xs border border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800">今年</button>
          </div>
          <div id="cal"></div>
          <div class="mt-3 flex items-center justify-between">
            <span id="rangeText" class="text-[11px] text-neutral-500"></span>
            <button id="dateDone" class="h-7 px-3 rounded-md text-xs font-medium bg-neutral-900 text-white dark:bg-white dark:text-neutral-900 hover:opacity-90">完成</button>
          </div>
        </div>
      </div>
      <!-- 类型 chips 容器 -->
      <div id="chips" class="flex items-center gap-1.5 flex-wrap"></div>
    </div>
  </div>
</header>

<!-- 列表 -->
<main class="max-w-4xl mx-auto px-4 py-5">
  <div id="stat" class="text-[12px] text-neutral-500 mb-3"></div>
  <div id="list" class="space-y-3"></div>
  <div id="empty" class="hidden text-center py-20 text-neutral-400">
    <div class="text-4xl mb-2">&#128269;</div>
    <p class="text-sm">没有匹配的内容</p>
  </div>
</main>

<footer class="max-w-4xl mx-auto px-4 pb-10 pt-2 text-center text-[11px] text-neutral-400">
  推送内容自动归档 · 每日自动更新
</footer>

<script>
const DATA = __DATA__;
const TYPES = __TYPES__;

const $ = id => document.getElementById(id);
const esc = s => (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

/* ---------- 状态 ---------- */
let activeTypes = new Set();
let range = { start: null, end: null };   // int YYYYMMDD
let search = '';

/* ---------- 日期工具 ---------- */
function toInt(d){ return d.getFullYear()*10000 + (d.getMonth()+1)*100 + d.getDate(); }
function todayInt(){ return toInt(new Date()); }
function daysAgoInt(n){ const d=new Date(); d.setDate(d.getDate()-n); return toInt(d); }
function fmtInt(iv){ if(!iv) return ''; const y=Math.floor(iv/10000), m=Math.floor(iv/100)%100, d=iv%100; return y+'-'+String(m).padStart(2,'0')+'-'+String(d).padStart(2,'0'); }

/* ---------- 类型 chips ---------- */
function renderChips(){
  const box = $('chips'); box.innerHTML='';
  Object.keys(TYPES).forEach(k => {
    const t = TYPES[k];
    const on = activeTypes.has(k);
    const b = document.createElement('button');
    b.className = 'h-8 px-2.5 rounded-lg text-[13px] font-medium border transition flex items-center gap-1 '
      + (on ? 'text-white border-transparent' : 'border-neutral-200 dark:border-neutral-700 hover:bg-neutral-100 dark:hover:bg-neutral-800');
    if(on) b.style.background = t.color;
    b.innerHTML = '<span>'+t.icon+'</span><span>'+esc(t.label)+'</span><span class="opacity-60 text-[11px]">'+t.count+'</span>';
    b.onclick = () => { on ? activeTypes.delete(k) : activeTypes.add(k); renderChips(); render(); };
    box.appendChild(b);
  });
}

/* ---------- 日历 ---------- */
let viewY, viewM;
(function(){ const d=new Date(); viewY=d.getFullYear(); viewM=d.getMonth(); })();

function renderCal(){
  const wd = ['一','二','三','四','五','六','日'];
  let h = '<div class="flex items-center justify-between mb-2">'
    + '<button data-cal="prev" class="w-7 h-7 rounded-md hover:bg-neutral-100 dark:hover:bg-neutral-800 flex items-center justify-center text-neutral-500">&#8249;</button>'
    + '<span class="text-[13px] font-semibold">'+viewY+' 年 '+(viewM+1)+' 月</span>'
    + '<button data-cal="next" class="w-7 h-7 rounded-md hover:bg-neutral-100 dark:hover:bg-neutral-800 flex items-center justify-center text-neutral-500">&#8250;</button></div>';
  h += '<div class="grid grid-cols-7 gap-0.5 text-center">';
  for(const d of wd) h += '<div class="text-[11px] text-neutral-400 py-1">'+d+'</div>';
  const first = new Date(viewY, viewM, 1);
  const lead = (first.getDay()+6)%7;              // 周一为首列
  const days = new Date(viewY, viewM+1, 0).getDate();
  const tdy = todayInt();
  for(let i=0;i<lead;i++) h += '<div></div>';
  for(let d=1; d<=days; d++){
    const iv = viewY*10000 + (viewM+1)*100 + d;
    let cls = 'h-8 text-[13px] rounded-md flex items-center justify-center cursor-pointer transition ';
    const isEnd = iv===range.start || iv===range.end;
    const inR = range.start && range.end && iv>=range.start && iv<=range.end;
    if(isEnd) cls += 'bg-neutral-900 text-white dark:bg-white dark:text-neutral-900 font-semibold ';
    else if(inR) cls += 'bg-neutral-200/70 dark:bg-neutral-700/60 ';
    else cls += 'hover:bg-neutral-100 dark:hover:bg-neutral-800 ';
    if(iv===tdy && !isEnd) cls += 'ring-1 ring-inset ring-neutral-400 ';
    h += '<button class="'+cls+'" data-day="'+iv+'">'+d+'</button>';
  }
  h += '</div>';
  $('cal').innerHTML = h;
  $('rangeText').textContent = range.start ? (fmtInt(range.start) + (range.end ? ' ~ ' + fmtInt(range.end) : ' ~ …')) : '点击选择起止日期';
}

/* ---------- 日期标签 ---------- */
let presetLabel = '近 30 天';
function updateDateLabel(){
  if(presetLabel) { $('dateLabel').textContent = presetLabel; return; }
  if(!range.start && !range.end){ $('dateLabel').textContent = '全部时间'; return; }
  $('dateLabel').textContent = fmtInt(range.start) + ' ~ ' + fmtInt(range.end || range.start);
}

function applyPreset(p){
  presetLabel = { all:'全部时间', '7':'近 7 天', '30':'近 30 天', '90':'近 90 天', year:'今年' }[p];
  if(p==='all'){ range={start:null,end:null}; }
  else if(p==='year'){ range={start:new Date().getFullYear()*10000+101, end:todayInt()}; }
  else { range={start:daysAgoInt(+p-1), end:todayInt()}; }
  renderCal(); updateDateLabel(); render();
}

/* ---------- 渲染列表 ---------- */
function render(){
  const list = $('list'); list.innerHTML='';
  const q = search.trim().toLowerCase();
  const items = DATA.filter(it => {
    if(activeTypes.size && !activeTypes.has(it.type)) return false;
    if(range.start && it.sort < range.start) return false;
    if(range.end && it.sort > range.end) return false;
    if(q && !(it.title.toLowerCase().includes(q) || (it.text||'').toLowerCase().includes(q))) return false;
    return true;
  });

  $('stat').textContent = '显示 ' + items.length + ' / ' + DATA.length + ' 篇';
  $('empty').classList.toggle('hidden', items.length>0);

  let lastDate = '';
  items.forEach(it => {
    if(it.date !== lastDate){
      lastDate = it.date;
      const dh = document.createElement('div');
      dh.className = 'flex items-center gap-2 pt-2 first:pt-0';
      dh.innerHTML = '<span class="text-[12px] font-semibold text-neutral-500">'+esc(it.date)+'</span>'
        + '<span class="text-[11px] text-neutral-400">周'+esc(it.wday)+'</span>'
        + '<div class="flex-1 h-px bg-neutral-200 dark:bg-neutral-800"></div>';
      list.appendChild(dh);
    }
    const c = TYPES[it.type] ? TYPES[it.type].color : '#71717a';
    const card = document.createElement('article');
    card.className = 'fade-in rounded-2xl border border-neutral-200 dark:border-neutral-800 bg-white dark:bg-neutral-900 overflow-hidden';
    card.innerHTML =
      '<details>'
      + '<summary class="flex items-start gap-3 p-4 select-none">'
      +   '<span class="w-9 h-9 rounded-xl flex items-center justify-center text-lg shrink-0" style="background:'+c+'1a">'+it.icon+'</span>'
      +   '<div class="flex-1 min-w-0">'
      +     '<div class="flex items-center gap-1.5 mb-0.5">'
      +       '<span class="text-[11px] font-medium px-1.5 py-0.5 rounded" style="color:'+c+';background:'+c+'14">'+esc(it.typeLabel)+'</span>'
      +     '</div>'
      +     '<h3 class="text-[14.5px] font-semibold leading-snug">'+esc(it.title)+'</h3>'
      +   '</div>'
      +   '<svg class="chev w-4 h-4 text-neutral-400 mt-1 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>'
      + '</summary>'
      + '<div class="px-4 pb-4 pt-1 border-t border-neutral-100 dark:border-neutral-800"><div class="prose pt-3">'+it.html+'</div></div>'
      + '</details>';
    list.appendChild(card);
  });
}

/* ---------- 事件 ---------- */
// 主题
$('themeBtn').onclick = () => {
  const dark = document.documentElement.classList.toggle('dark');
  try { localStorage.setItem('arch-theme', dark ? 'dark' : 'light'); } catch(e){}
};
// 搜索
$('q').addEventListener('input', e => {
  search = e.target.value;
  $('qclear').classList.toggle('hidden', !search);
  render();
});
$('qclear').onclick = () => { $('q').value=''; search=''; $('qclear').classList.add('hidden'); render(); $('q').focus(); };

// 日期弹层：整层事件委托，点击永远有效
$('dateBtn').onclick = e => { e.stopPropagation(); $('datePop').classList.toggle('hidden'); };
$('datePop').addEventListener('click', e => {
  e.stopPropagation();
  const t = e.target.closest('[data-cal],[data-day],[data-preset]');
  if(!t){ if(e.target.id==='dateDone') $('datePop').classList.add('hidden'); return; }
  if(t.dataset.cal==='prev'){ viewM--; if(viewM<0){viewM=11;viewY--;} renderCal(); return; }
  if(t.dataset.cal==='next'){ viewM++; if(viewM>11){viewM=0;viewY++;} renderCal(); return; }
  if(t.dataset.preset){ applyPreset(t.dataset.preset); return; }
  if(t.dataset.day){
    const iv = +t.dataset.day;
    presetLabel = '';
    if(!range.start || range.end){ range.start=iv; range.end=null; }
    else if(iv < range.start){ range.end=range.start; range.start=iv; }
    else { range.end=iv; }
    renderCal(); updateDateLabel(); render();
    return;
  }
});
// 点击外部关闭弹层
document.addEventListener('click', e => {
  if(!$('datePop').classList.contains('hidden') && !$('datePop').contains(e.target) && e.target!==$('dateBtn'))
    $('datePop').classList.add('hidden');
});

/* ---------- 初始化 ---------- */
renderChips();
applyPreset('30');   // 默认近 30 天
renderCal();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    build()
