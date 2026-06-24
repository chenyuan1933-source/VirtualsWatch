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

# ============================================================
#  ★ 在这里填写你的配置 ★
# ============================================================

# ── Telegram ─────────────────────────────────────────────────
# 1. 搜索 @BotFather → /newbot → 获取 Token
# 2. 搜索 @userinfobot → 获取你的 Chat ID
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID_HERE"

# ── 飞书 Webhook ──────────────────────────────────────────────
# 获取方法（见下方说明）:
# 1. 飞书群 → 右上角「设置」→「群机器人」→「添加机器人」→「自定义机器人」
# 2. 复制 Webhook 地址，格式:
#    https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# 3. 如果设置了「签名校验」，把密钥填入 FEISHU_SECRET；否则留空 ""
FEISHU_WEBHOOK = ""
FEISHU_SECRET  = ""   # 签名密钥，没开启就留空

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

alerted_ids: set = set()


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


def parse_agent(agent: dict):
    try:
        attr        = agent.get("attributes", agent)
        name        = attr.get("name", "Unknown")
        symbol      = attr.get("symbol") or attr.get("tokenSymbol", "???")
        created_raw = attr.get("createdAt") or attr.get("tokenCreatedAt", "")
        holders     = int(attr.get("holderCount") or attr.get("holders") or 0)
        mcap        = float(attr.get("marketCap") or attr.get("mcap") or 0)
        token_addr  = attr.get("tokenAddress", "")
        agent_id    = agent.get("id") or attr.get("id", "")

        if created_raw:
            created_dt  = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            hours_since = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
        else:
            hours_since = 999

        return {
            "id": agent_id, "name": name, "symbol": symbol,
            "hours_since": hours_since, "holders": holders,
            "mcap": mcap, "token_addr": token_addr,
        }
    except Exception as e:
        print(f"[{now()}] ⚠️  解析失败: {e}")
        return None


# ─── 条件判断 ────────────────────────────────────────────────

def is_triggered(agent: dict) -> bool:
    return agent["hours_since"] <= MAX_HOURS and agent["holders"] >= MIN_HOLDERS


# ─── 格式化工具 ──────────────────────────────────────────────

def fmt_time(h: float) -> str:
    return f"{int(h*60)} 分钟" if h < 1 else f"{h:.1f} 小时"

def fmt_mcap(m: float) -> str:
    if m >= 1_000_000: return f"${m/1_000_000:.2f}M"
    if m >= 1_000:     return f"${m/1_000:.1f}K"
    return f"${m:.0f}"

def fmt_link(agent: dict) -> str:
    return (f"https://app.virtuals.io/virtuals/{agent['token_addr']}"
            if agent["token_addr"] else "https://app.virtuals.io/prototypes")

def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ─── Telegram 推送 ───────────────────────────────────────────

def send_telegram(agent: dict) -> bool:
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        return True

    text = (
        f"🚨 *Virtuals 打新告警*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🤖 代币: *{agent['name']}* (${agent['symbol']})\n"
        f"⏱ 上线时间: {fmt_time(agent['hours_since'])}\n"
        f"👥 持有人数: {agent['holders']} 人\n"
        f"💰 市值: {fmt_mcap(agent['mcap'])}\n"
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
    if FEISHU_WEBHOOK == "YOUR_FEISHU_WEBHOOK_HERE":
        return True

    link = fmt_link(agent)

    # 使用「卡片消息」，排版更美观
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
                                 "content": f"**💰 市值**\n{fmt_mcap(agent['mcap'])}"}
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
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "在 Virtuals 查看 →"},
                    "type": "primary",
                    "url": link
                }]
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
    tg = "✅ 已配置" if TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN_HERE" else "⚠️  未配置"
    fs = "✅ 已配置" if FEISHU_WEBHOOK   != "YOUR_FEISHU_WEBHOOK_HERE" else "⚠️  未配置"
    print("=" * 55)
    print("  Virtuals Protocol 打新监控")
    print("=" * 55)
    print(f"  条件1 : 上线时间 ≤ {MAX_HOURS} 小时")
    print(f"  条件2 : 持有人数 ≥ {MIN_HOLDERS} 人")
    print(f"  刷新  : 每 {POLL_INTERVAL} 秒")
    print(f"  Telegram : {tg}")
    print(f"  飞书     : {fs}")
    print("=" * 55)
    print()


def run():
    print_banner()
    cycle = 0

    while True:
        cycle += 1
        print(f"[{now()}] 🔄 第 {cycle} 次刷新...")

        agents_raw = fetch_new_agents()
        if not agents_raw:
            print(f"[{now()}]    未获取到数据，{POLL_INTERVAL}s 后重试\n")
            time.sleep(POLL_INTERVAL)
            continue

        triggered_this_round = []

        for raw in agents_raw:
            agent = parse_agent(raw)
            if not agent:
                continue
            if agent["hours_since"] > MAX_HOURS + 0.5:
                continue

            triggered = is_triggered(agent)
            flag = "🔔 触发" if triggered else "   -   "
            print(f"  {flag}  {agent['name']:<20} ${agent['symbol']:<8} "
                  f"上线 {fmt_time(agent['hours_since']):<10} 持有人 {agent['holders']}")

            if triggered and agent["id"] not in alerted_ids:
                alerted_ids.add(agent["id"])
                triggered_this_round.append(agent)

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