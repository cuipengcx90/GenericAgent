"""
场景6：价格/行情监控
监控 BTC/ETH 等价格波动，触发 Agent 分析并推飞书
启动：python agentmain.py --reflect reflect_scripts/06_price_alert.py
"""
import sys, os, json, urllib.request, time
sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu, Debounce

INTERVAL = 120   # 每2分钟检查一次（常驻模式下生效；定时任务每次独立进程调用则靠持久化文件累积历史）

# ===== 配置区 =====
WATCH_COINS   = ["bitcoin", "ethereum"]   # CoinGecko ID，免费无需 API key
ALERT_CHANGE  = 0.05    # 触发阈值：5% 波动（相对上一次持久化的采样点）
HISTORY_FILE  = os.path.join(os.path.dirname(__file__), '.price_history.json')
# ==================

_db = Debounce(cooldown=1800)          # 同一币种 30 分钟内不重复告警

def _load_history() -> dict:
    """跨进程持久化历史：定时任务每次独立起进程，内存dict无法累积，改用文件"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_history(history: dict):
    try:
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)
    except Exception as e:
        print(f"[价格历史保存失败] {e}")

def _fetch_prices() -> dict[str, float]:
    ids = ",".join(WATCH_COINS)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    req = urllib.request.Request(url, headers={"User-Agent": "GenericAgent/1.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return {coin: data[coin]["usd"] for coin in WATCH_COINS if coin in data}

def check():
    try:
        now = time.time()
        prices = _fetch_prices()
        alerts = []
        history = _load_history()   # {coin: [ts, price]}，跨进程持久化的"上一次"采样点

        for coin, price in prices.items():
            prev = history.get(coin)
            history[coin] = [now, price]   # 无论是否触发都更新为最新点，供下次比较

            if prev is None:
                continue   # 首次记录，无上一次可比
            prev_ts, prev_price = prev
            change = (price - prev_price) / prev_price

            if abs(change) >= ALERT_CHANGE and _db.should_fire(coin):
                direction = "📈 暴涨" if change > 0 else "📉 暴跌"
                elapsed_min = (now - prev_ts) / 60
                alerts.append(
                    f"{coin.upper()} {direction} {change*100:+.1f}%，"
                    f"当前价格 ${price:,.2f}（{elapsed_min:.0f}分钟前 ${prev_price:,.2f}）"
                )

        _save_history(history)   # 写回文件，供下次独立进程调用时读取

        if not alerts:
            return None

        desc = "\n".join(alerts)
        return (
            f"加密货币价格异动：\n{desc}\n\n"
            f"请分析：1) 可能的原因（近期新闻/市场情绪）"
            f" 2) 短期趋势判断 3) 给普通投资者的操作建议（保守派）"
        )
    except Exception as e:
        print(f"[价格监控失败] {e}")
        return None

def on_done(result: str):
    notify_feishu(f"💰 行情播报\n\n{result}")