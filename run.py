#!/usr/bin/env python3
"""
妙手ERP 新订单提醒 — GitHub Actions 版
========================================
由 GitHub Actions 每 5 分钟触发一次，拉取妙手最近订单，
对比已通知列表，新订单推送到微信（Server酱）。
"""

import hmac
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------- 配置（从环境变量读取）----------
APP_KEY = os.environ["MIAOSHOU_APP_KEY"]
APP_SECRET = os.environ["MIAOSHOU_APP_SECRET"]
SEND_KEY = os.environ["SERVERCHAN_SEND_KEY"]
BASE_URL = "https://openapi-erp.91miaoshou.com"

CACHE_STATE_FILE = Path("/tmp/miaoshou_orders_state.json")
TZ_SHANGHAI = timezone(timedelta(hours=8))
LOOKBACK_MINUTES = 8  # 每次拉取最近 N 分钟的订单（略大于轮询间隔）


# ---------- 签名 ----------
def generate_sign(app_secret, path, timestamp, app_key, body_json=""):
    content = app_secret + path + str(timestamp) + app_key
    if body_json:
        content += body_json
    content += app_secret
    return hmac.new(
        app_secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ---------- API 调用 ----------
def call_miaoshou_api(path, body=None):
    timestamp = int(datetime.now().timestamp())
    body_json = json.dumps(body, ensure_ascii=False) if body else ""
    sign = generate_sign(APP_SECRET, path, timestamp, APP_KEY, body_json)

    headers = {
        "x-app-key": APP_KEY,
        "x-timestamp": str(timestamp),
        "x-sign": sign,
        "Content-Type": "application/json",
    }

    url = BASE_URL + path
    print(f"[API] POST {url}  body={body_json[:200]}")
    try:
        resp = requests.post(url, headers=headers, data=body_json, timeout=30)
        print(f"[API] 状态码: {resp.status_code}")
        return resp.json()
    except requests.RequestException as e:
        print(f"[API] 请求失败: {e}")
        return None
    except json.JSONDecodeError:
        print(f"[API] 响应解析失败: {resp.text[:300]}")
        return None


# ---------- 状态持久化 ----------
def load_state():
    if CACHE_STATE_FILE.exists():
        with open(CACHE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"notified_ids": [], "last_check": None}


def save_state(state):
    # 只保留最近 2000 条
    state["notified_ids"] = state["notified_ids"][-2000:]
    with open(CACHE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- 订单拉取 ----------
def fetch_recent_orders():
    path = "/open/v1/order/package/fetch/search_package_list"
    from_time = (datetime.now(TZ_SHANGHAI) - timedelta(minutes=LOOKBACK_MINUTES))
    body = {
        "page": 1,
        "pageSize": 50,
        "gmtModifiedFrom": from_time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"[轮询] 查询 {from_time.strftime('%H:%M:%S')} 之后的订单")
    result = call_miaoshou_api(path, body)

    if not result:
        return []

    code = result.get("code") or result.get("result", "")
    if code != "success":
        print(f"[轮询] API 返回非 success: code={code}, msg={result.get('message')}")
        return []

    data = result.get("data")
    if not data:
        return []

    all_orders = []
    if isinstance(data, dict):
        all_orders = data.get("orderPackageList", [])
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                all_orders.extend(item.get("orderPackageList", []))

    return all_orders


# ---------- 订单格式化 ----------
def format_order(order):
    order_info = order.get("orderInfo", {})
    items = order.get("items", [])
    consignee = order.get("consigneeInfo", {})

    platform = order.get("platformName", order.get("platform", "未知"))
    shop = order.get("shopName", "未知店铺")
    order_sn = order_info.get("platformOrderSn", "无")
    status = order_info.get("appOrderStatusText",
                             order_info.get("appOrderStatus", "未知"))
    amount = order_info.get("orderAmount", 0)
    currency = order_info.get("currency", "")
    country = consignee.get("countryName", consignee.get("country", ""))

    item_lines = []
    for item in items[:5]:
        title = item.get("title", "未知商品")
        qty = item.get("quantity", 0)
        price = item.get("discountedPrice", item.get("originalPrice", 0))
        item_lines.append(f"  - {title} ×{qty}  {price:.2f} {currency}")

    if len(items) > 5:
        item_lines.append(f"  ... 共 {len(items)} 件商品")

    return (
        f"平台: {platform} | 店铺: {shop}\n"
        f"订单号: {order_sn}\n"
        f"状态: {status} | 金额: {amount:.2f} {currency}\n"
        f"国家: {country}\n\n"
        f"商品:\n"
        + "\n".join(item_lines) +
        f"\n\n⏰ {datetime.now(TZ_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}"
    )


# ---------- 微信推送 ----------
def push_wechat(title, content):
    url = f"https://sctapi.ftqq.com/{SEND_KEY}.send"
    try:
        resp = requests.post(url, data={"title": title, "desp": content}, timeout=15)
        result = resp.json()
        ok = result.get("code") == 0
        status = "✅" if ok else f"❌ {result.get('message', '')}"
        print(f"[推送] {status}  title={title}")
        return ok
    except Exception as e:
        print(f"[推送] 异常: {e}")
        return False


# ---------- 主逻辑 ----------
def main():
    print("=" * 50)
    print(f"妙手ERP订单监控 (GitHub Actions)")
    print(f"AppKey: {APP_KEY[:12]}...")
    now = datetime.now(TZ_SHANGHAI)
    print(f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    state = load_state()
    print(f"[状态] 已有 {len(state['notified_ids'])} 条已通知记录")

    orders = fetch_recent_orders()
    print(f"[结果] 获取到 {len(orders)} 条包裹记录")

    if not orders:
        print("无订单，本轮结束")
        state["last_check"] = now.isoformat()
        save_state(state)
        return

    notified_set = set(state["notified_ids"])
    new_count = 0

    for order in orders:
        order_info = order.get("orderInfo", {})
        op_order_id = str(order_info.get("opOrderId", ""))

        if not op_order_id or op_order_id in notified_set:
            continue

        platform = order.get("platformName", order.get("platform", "未知"))
        platform_short = {"tiktok": "TK", "shopee": "SP", "lazada": "LZ"}.get(
            platform.lower(), platform)
        msg = format_order(order)
        title = f"您有一条新的{platform_short} 订单"

        if push_wechat(title, msg):
            notified_set.add(op_order_id)
            new_count += 1

    # 更新状态
    state["notified_ids"] = list(notified_set)
    state["last_check"] = now.isoformat()
    save_state(state)

    print(f"[总结] 本轮发现 {new_count} 条新订单，总已通知 {len(state['notified_ids'])} 条")


if __name__ == "__main__":
    main()
