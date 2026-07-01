"""
Virtuals Protocol 打新监控脚本
================================
条件: 代币上线 ≤ 2小时 AND 持有人数 ≥ 10
告警: 终端输出 + Telegram 机器人推送 + 飞书 Webhook 推送

依赖安装:
    pip install requests

运行:
    python virtuals_monitor.py
"""

import requests
import time
from datetime import datetime, timezone

# 整点汇报时间窗口：19:00 ~ 23:30
SUMMARY_START_HOUR = 19
SUMMARY_END_HOUR   = 23
SUMMARY_END_MINUTE = 30

# ============================================================
#  配置从系统环境变量读取，无需修改此文件
#  环境变量对应关系：
#    TelegramBotAPI   → Telegram Bot Token
#    TelegramChatID   → Telegram Chat ID
#    Feishubot        → 飞书 Webhook 地址
#    FeishubotMY      → 飞书签名密钥
# ============================================================

import os

TELEGRAM_BOT_TOKEN = os.environ.get("TelegramBotAPI", "")
TELEGRAM_CHAT_ID   = os.environ.get("TelegramChatID", "")
FEISHU_WEBHOOK     = os.environ.get("Feishubot", "")
FEISHU_SECRET      = os.environ.get("FeishubotMY", "")

# ── 告警条件 ──────────────────────────────────────────────────
MAX_HOURS    = 2     # 上线时间不超过 N 小时
MIN_HOLDERS  = 10    # 持有人数至少 N 人

# ── 刷新间隔（秒），建议 30~60 ────────────────────────────────
POLL_INTERVAL = 30

# ============================================================

API_BASE = "https://api.virtuals.io/api"
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://app.virtuals.io/",
}

# key: agent_id, value: 上次推送时的持有人里程碑（10的倍数）
alerted_milestones: dict = {}

# 记录已发过整点汇报的小时（避免重复发送）
last_summary_hour: int = -1

# 本小时内的统计数据
hour_stats: dict = {
    "triggered": [],   # 本小时触发过的代币列表
    "total_seen": 0,   # 本小时扫描到的代币总数
}


# ─── Virtuals API ────────────────────────────────────────────

def fetch_new_agents(page_size: int = 50) -> list:
    params = {
        "filters[status][$eq]": "1",
        "sort[0]": "createdAt:desc",
        "pagination[pageSize]": page_size,
        "pagination[page]": 1,
        "populate[0]": "image",
    }
    try:
        resp = requests.get(f"{API_BASE}/virtuals", headers=HEADERS,
                            params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except requests.RequestException as e:
        print(f"[{now()}] ⚠️  API 请求失败: {e}")
        return []


# 设为 True 可打印第一条原始数据，用于排查字段名
DEBUG_FIELDS = False
_debug_printed = False

# 缓存 VIRTUAL 价格，每5分钟刷新一次
_virtual_price_cache = {"price": 0.0, "ts": 0}

def get_virtual_price() -> float:
    """获取 VIRTUAL 代币的实时美元价格，缓存5分钟。"""
    now_ts = time.time()
    if now_ts - _virtual_price_cache["ts"] < 300 and _virtual_price_cache["price"] > 0:
        return _virtual_price_cache["price"]
    try:
        # Virtuals 官方价格接口
        resp = requests.get(
            "https://api.virtuals.io/api/virtuals/stats",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            price = float(data.get("data", {}).get("virtualPrice") or
                          data.get("virtualPrice") or 0)
            if price > 0:
                _virtual_price_cache["price"] = price
                _virtual_price_cache["ts"]    = now_ts
                return price
    except Exception:
        pass
    # 备用：从 CoinGecko 获取
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=virtual-protocol&vs_currencies=usd",
            timeout=10
        )
        if resp.status_code == 200:
            price = float(resp.json().get("virtual-protocol", {}).get("usd", 0))
            if price > 0:
                _virtual_price_cache["price"] = price
                _virtual_price_cache["ts"]    = now_ts
                return price
    except Exception:
        pass
    # 两个接口都失败，返回上次缓存值（哪怕过期）
    return _virtual_price_cache["price"] or 0.0


def parse_agent(agent: dict):
    global _debug_printed
    try:
        attr        = agent.get("attributes", agent)

        # 调试模式：只打印价值相关字段
        if DEBUG_FIELDS and not _debug_printed:
            print("\n[DEBUG] 价值相关字段:")
            keywords = ["mcap", "fdv", "value", "price", "cap", "usd", "market", "virtual", "liquidity", "tvl"]
            for k, v in attr.items():
                if any(kw in k.lower() for kw in keywords):
                    print(f"  {k}: {v}")
            print()
            _debug_printed = True

        name        = attr.get("name", "Unknown")
        symbol      = attr.get("symbol", "???")
        created_raw = attr.get("launchedAt") or attr.get("createdAt", "")
        holders     = int(attr.get("holderCount") or 0)
        # mcapInVirtual 单位是 VIRTUAL 数量，乘以实时价格换算为美元
        mcap_virtual = float(attr.get("mcapInVirtual") or attr.get("fdvInVirtual") or 0)
        mcap         = mcap_virtual * get_virtual_price()
        liquidity   = float(attr.get("liquidityUsd") or 0)
        token_addr  = attr.get("preToken") or attr.get("tokenAddress") or ""
        # numeric_id 用于拼接 Virtuals 官网链接（/prototypes/{id}）
        numeric_id  = attr.get("id") or agent.get("id") or ""
        agent_id    = str(numeric_id) or attr.get("uid", "")

        if created_raw:
            created_dt  = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            hours_since = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        else:
            hours_since = 999

        return {
            "id": agent_id, "name": name, "symbol": symbol,
            "hours_since": hours_since, "holders": holders,
            "mcap": mcap, "liquidity": liquidity,
            "token_addr": token_addr, "numeric_id": str(numeric_id),
        }
    except Exception as e:
        print(f"[{now()}] ⚠️  解析失败: {e}")
        return None


# ─── 条件判断 ────────────────────────────────────────────────

# 上限持有人数，超过后不再提醒
MAX_HOLDERS = 100

def current_milestone(holders: int) -> int:
    """计算当前持有人数对应的里程碑（10的整数倍，上限100）。"""
    if holders < MIN_HOLDERS:
        return 0
    if holders >= MAX_HOLDERS:
        return MAX_HOLDERS  # 100以上不推送
    return (holders // 10) * 10

def should_alert(agent: dict) -> bool:
    """判断是否需要推送：在2小时内，且达到新的10人里程碑，且未超过100人。"""
    if agent["hours_since"] > MAX_HOURS:
        return False
    if agent["holders"] >= MAX_HOLDERS:
        return False
    milestone = current_milestone(agent["holders"])
    if milestone == 0:
        return False
    last = alerted_milestones.get(agent["id"], 0)
    return milestone > last


# ─── 格式化工具 ──────────────────────────────────────────────

def fmt_time(h: float) -> str:
    return f"{int(h*60)} 分钟" if h < 1 else f"{h:.1f} 小时"

def fmt_mcap(m: float) -> str:
    """FDV 已换算为美元。"""
    if m >= 1_000_000: return f"${m/1_000_000:.2f}M"
    if m >= 1_000:     return f"${m/1_000:.1f}K"
    return f"${m:.0f}"

def fmt_usd(m: float) -> str:
    """流动性单位为美元。"""
    if m >= 1_000_000: return f"${m/1_000_000:.2f}M"
    if m >= 1_000:     return f"${m/1_000:.1f}K"
    return f"${m:.0f}"

def fmt_link(agent: dict) -> str:
    """统一使用 app.virtuals.io/virtuals/{numeric_id} 格式。"""
    if agent.get("numeric_id"):
        return f"https://app.virtuals.io/virtuals/{agent['numeric_id']}"
    return "https://app.virtuals.io/prototypes"

def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ─── Telegram 推送 ───────────────────────────────────────────

def send_telegram(agent: dict) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        print(f"[{now()}] ⚠️  Telegram 未配置，跳过推送")
        return False

    addr = agent["token_addr"] or "暂无"
    text = (
        f"🚨 *Virtuals 打新告警*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🤖 代币: *{agent['name']}* (${agent['symbol']})\n"
        f"⏱ 上线时间: {fmt_time(agent['hours_since'])}\n"
        f"👥 持有人数: {agent['holders']} 人\n"
        f"📈 FDV: {fmt_mcap(agent['mcap'])}\n"
        f"💧 流动性: {fmt_usd(agent['liquidity'])}\n"
        f"📋 合约地址: `{addr}`\n"
        f"━━━━━━━━━━━━━━\n"
        f"[在 Virtuals 查看]({fmt_link(agent)})"
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=10
        )
        if resp.status_code == 200:
            print(f"[{now()}] ✅ Telegram 推送成功: {agent['name']}")
            return True
        print(f"[{now()}] ❌ Telegram 失败 {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.RequestException as e:
        print(f"[{now()}] ❌ Telegram 网络错误: {e}")
        return False


# ─── 飞书推送 ────────────────────────────────────────────────

def _feishu_sign(timestamp: str) -> str:
    """生成飞书签名（仅在开启签名校验时使用）。"""
    import hmac, hashlib, base64
    key = f"{timestamp}\n{FEISHU_SECRET}".encode("utf-8")
    sign = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(sign).decode("utf-8")


def send_feishu(agent: dict) -> bool:
    if not FEISHU_WEBHOOK:
        print(f"[{now()}] ⚠️  飞书未配置，跳过推送")
        return False

    link     = fmt_link(agent)
    addr     = agent["token_addr"] or "暂无"
    dex_link = (f"https://dexscreener.com/base/{agent['token_addr']}"
                if agent["token_addr"] else "")

    action_buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "Virtuals 页面 →"},
            "type": "primary",
            "url": link
        }
    ]
    if dex_link:
        action_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "DEX Screener →"},
            "type": "default",
            "url": dex_link
        })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🚨 Virtuals 打新告警"},
            "template": "red"
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md",
                                 "content": f"**🤖 代币**\n{agent['name']} (${agent['symbol']})"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md",
                                 "content": f"**📈 FDV**\n{fmt_mcap(agent['mcap'])}"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md",
                                 "content": f"**💧 流动性**\n{fmt_usd(agent['liquidity'])}"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md",
                                 "content": f"**⏱ 上线时间**\n{fmt_time(agent['hours_since'])}"}
                    },
                    {
                        "is_short": True,
                        "text": {"tag": "lark_md",
                                 "content": f"**👥 持有人数**\n{agent['holders']} 人"}
                    },
                ]
            },
            {
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"**📋 合约地址**\n`{addr}`"}
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": action_buttons
            }
        ]
    }

    payload = {"msg_type": "interactive", "card": card}

    # 如果配置了签名校验，加入签名
    if FEISHU_SECRET:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"]      = _feishu_sign(ts)

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0 or data.get("StatusCode") == 0:
            print(f"[{now()}] ✅ 飞书推送成功: {agent['name']}")
            return True
        print(f"[{now()}] ❌ 飞书推送失败: {data}")
        return False
    except requests.RequestException as e:
        print(f"[{now()}] ❌ 飞书网络错误: {e}")
        return False


# ─── 主循环 ──────────────────────────────────────────────────

def print_banner():
    tg = "✅ 已配置" if TELEGRAM_BOT_TOKEN else "⚠️  未配置（环境变量 TelegramBotAPI 未设置）"
    fs = "✅ 已配置" if FEISHU_WEBHOOK     else "⚠️  未配置（环境变量 Feishubot 未设置）"
    print("=" * 55)
    print("  Virtuals Protocol 打新监控")
    print("=" * 55)
    print(f"  条件1 : 上线时间 ≤ {MAX_HOURS} 小时")
    print(f"  条件2 : 持有人数每增加 10 人提醒一次")
    print(f"  范围  : {MIN_HOLDERS} 人 → {MAX_HOLDERS} 人（超过后静默）")
    print(f"  刷新  : 每 {POLL_INTERVAL} 秒")
    print(f"  Telegram : {tg}")
    print(f"  飞书     : {fs}")
    print("=" * 55)
    print()


def in_summary_window() -> bool:
    """当前时间是否在 19:00~23:30 的整点汇报窗口内。"""
    t = datetime.now()
    if t.hour < SUMMARY_START_HOUR:
        return False
    if t.hour > SUMMARY_END_HOUR:
        return False
    if t.hour == SUMMARY_END_HOUR and t.minute > SUMMARY_END_MINUTE:
        return False
    return True


def send_hourly_summary():
    """发送整点汇报到 Telegram 和飞书。"""
    t        = datetime.now()
    hour_str = t.strftime("%H:00")
    triggered = hour_stats["triggered"]
    total     = hour_stats["total_seen"]
    count     = len(triggered)

    # ── Telegram ──────────────────────────────────────────────
    if TELEGRAM_BOT_TOKEN:
        if count > 0:
            token_lines = "\n".join(
                f"  • {a['name']} (${a['symbol']}) — {a['holders']} 人"
                for a in triggered[-10:]  # 最多显示10条
            )
            body = f"共 {count} 个代币触发条件：\n{token_lines}"
        else:
            body = "本小时无代币触发告警条件。"

        text = (
            f"🕐 *{hour_str} 整点汇报*\n"
            f"━━━━━━━━━━━━━━\n"
            f"📊 扫描代币数: {total}\n"
            f"🔔 触发告警数: {count}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{body}\n"
            f"━━━━━━━━━━━━━━\n"
            f"✅ 监控程序运行正常"
        )
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                      "parse_mode": "Markdown"},
                timeout=10
            )
            if resp.status_code == 200:
                print(f"[{now()}] ✅ Telegram 整点汇报已发送")
            else:
                print(f"[{now()}] ❌ Telegram 汇报失败: {resp.text[:100]}")
        except Exception as e:
            print(f"[{now()}] ❌ Telegram 汇报错误: {e}")

    # ── 飞书 ──────────────────────────────────────────────────
    if FEISHU_WEBHOOK:
        if count > 0:
            token_lines = "\n".join(
                f"• {a['name']} (${a['symbol']}) — {a['holders']} 人"
                for a in triggered[-10:]
            )
            detail_text = f"**触发代币**\n{token_lines}"
        else:
            detail_text = "本小时无代币触发告警条件。"

        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"🕐 {hour_str} 整点汇报"},
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md",
                                     "content": f"**📊 扫描代币数**\n{total}"}
                        },
                        {
                            "is_short": True,
                            "text": {"tag": "lark_md",
                                     "content": f"**🔔 触发告警数**\n{count}"}
                        },
                    ]
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": detail_text}
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "✅ 监控程序运行正常"}
                }
            ]
        }
        payload = {"msg_type": "interactive", "card": card}
        if FEISHU_SECRET:
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"]      = _feishu_sign(ts)
        try:
            resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
            data = resp.json()
            if data.get("code") == 0 or data.get("StatusCode") == 0:
                print(f"[{now()}] ✅ 飞书整点汇报已发送")
            else:
                print(f"[{now()}] ❌ 飞书汇报失败: {data}")
        except Exception as e:
            print(f"[{now()}] ❌ 飞书汇报错误: {e}")

    # 重置本小时统计
    hour_stats["triggered"] = []
    hour_stats["total_seen"] = 0


def run():
    print_banner()
    cycle = 0

    global last_summary_hour

    while True:
        cycle += 1
        print(f"[{now()}] 🔄 第 {cycle} 次刷新...")

        # ── 整点汇报检查 ──────────────────────────────────────
        t = datetime.now()
        if in_summary_window() and t.minute < 2:
            if t.hour != last_summary_hour:
                last_summary_hour = t.hour
                print(f"[{now()}] 📊 整点汇报发送中...")
                send_hourly_summary()

        # ── 拉取数据 ──────────────────────────────────────────
        agents_raw = fetch_new_agents()
        if not agents_raw:
            print(f"[{now()}]    未获取到数据，{POLL_INTERVAL}s 后重试\n")
            time.sleep(POLL_INTERVAL)
            continue

        triggered_this_round = []
        hour_stats["total_seen"] += len(agents_raw)

        for raw in agents_raw:
            agent = parse_agent(raw)
            if not agent:
                continue
            if agent["hours_since"] > MAX_HOURS + 0.5:
                continue

            alert = should_alert(agent)
            milestone = current_milestone(agent["holders"])
            if agent["holders"] >= MAX_HOLDERS:
                flag = "  🔕  "  # 超过100人，静默
            elif alert:
                flag = f"🔔 {milestone}人"
            else:
                flag = "   -   "
            print(f"  {flag:<8}  {agent['name']:<20} ${agent['symbol']:<8} "
                  f"上线 {fmt_time(agent['hours_since']):<10} 持有人 {agent['holders']}")

            if alert:
                alerted_milestones[agent["id"]] = milestone
                triggered_this_round.append(agent)
                # 记入本小时统计
                if not any(a["id"] == agent["id"] for a in hour_stats["triggered"]):
                    hour_stats["triggered"].append(agent)

        if triggered_this_round:
            print(f"\n[{now()}] ⚡ 发现 {len(triggered_this_round)} 个新代币满足条件，发送推送...")
            for agent in triggered_this_round:
                send_telegram(agent)
                send_feishu(agent)
            print()
        else:
            print(f"[{now()}]    本轮无新告警")

        print(f"[{now()}]    等待 {POLL_INTERVAL}s...\n")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n\n监控已停止。")
