"""
场景6：价格/行情监控
监控 BTC/ETH 等价格波动，触发 Agent 分析并推飞书
启动：python agentmain.py --reflect reflect_scripts/06_price_alert.py
"""
import sys, json, urllib.request, time
sys.path.insert(0, '/Users/cuip/projects/GenericAgent/reflect_scripts')
from reflect_common import notify_feishu, Debounce

INTERVAL = 120   # 每2分钟检查一次

# ===== 配置区 =====
WATCH_COINS   = ["bitcoin", "ethereum"]   # CoinGecko ID，免费无需 API key
ALERT_CHANGE  = 0.05    # 触发阈值：5% 波动
WINDOW_SECS   = 600     # 统计窗口：10分钟内的涨跌幅
# ==================

_price_history: dict[str, list] = {}   # {coin: [(timestamp, price), ...]}
_db = Debounce(cooldown=1800)          # 同一币种 30 分钟内不重复告警

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

        for coin, price in prices.items():
            hist = _price_history.setdefault(coin, [])
            hist.append((now, price))
            # 清理窗口外的历史
            _price_history[coin] = [(t, p) for t, p in hist if now - t <= WINDOW_SECS]

            if len(_price_history[coin]) < 2:
                continue
            oldest_price = _price_history[coin][0][1]
            change = (price - oldest_price) / oldest_price

            if abs(change) >= ALERT_CHANGE and _db.should_fire(coin):
                direction = "📈 暴涨" if change > 0 else "📉 暴跌"
                alerts.append(
                    f"{coin.upper()} {direction} {change*100:+.1f}%，"
                    f"当前价格 ${price:,.2f}（{WINDOW_SECS//60}分钟前 ${oldest_price:,.2f}）"
                )

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