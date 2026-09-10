#!/usr/bin/env python3
"""
妙手ERP 新订单提醒 —— 微信推送
=================================
定时轮询妙手ERP开放平台「批量获取包裹列表」接口，
发现新订单后通过Server酱推送到微信。
"""

import hmac
import hashlib
import json
import time
import os
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------- 配置 ----------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / "orders_state.json"
LOG_FILE = SCRIPT_DIR / "monitor.log"

TZ_SHANGHAI = timezone(timedelta(hours=8))

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("miaoshou")


def load_config():
    """加载配置文件"""
    if not CONFIG_FILE.exists():
        log.error("配置文件 config.json 不存在")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    """加载已通知的订单记录"""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"notified_order_ids": [], "last_check_time": None}


def save_state(state):
    """保存订单状态"""
    # 只保留最近 1000 条已通知订单ID，防止文件过大
    state["notified_order_ids"] = state["notified_order_ids"][-1000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def generate_sign(app_secret, path, timestamp, app_key, body_json=""):
    """
    生成 HMAC-SHA256 签名
    签名公式: HmacSHA256(appSecret, appSecret + path + timestamp + appKey + bodyJson + appSecret)
    """
    content = app_secret + path + str(timestamp) + app_key
    if body_json:
        content += body_json
    content += app_secret

    sign = hmac.new(
        app_secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return sign


def call_miaoshou_api(config, path, body=None):
    """调用妙手ERP API"""
    app_key = config["miaoshou"]["app_key"]
    app_secret = config["miaoshou"]["app_secret"]
    base_url = config["miaoshou"]["base_url"]

    timestamp = int(time.time())
    body_json = json.dumps(body, ensure_ascii=False) if body else ""
    sign = generate_sign(app_secret, path, timestamp, app_key, body_json)

    headers = {
        "x-app-key": app_key,
        "x-timestamp": str(timestamp),
        "x-sign": sign,
        "Content-Type": "application/json",
    }

    url = base_url + path
    log.debug(f"请求: POST {url}")
    log.debug(f"Headers: x-app-key={app_key}, x-timestamp={timestamp}")
    log.debug(f"Body: {body_json}")

    try:
        resp = requests.post(url, headers=headers, data=body_json, timeout=30)
        log.debug(f"响应状态码: {resp.status_code}")
        return resp.json()
    except requests.RequestException as e:
        log.error(f"API 请求失败: {e}")
        return None
    except json.JSONDecodeError:
        log.error(f"响应解析失败: {resp.text[:500]}")
        return None


def fetch_recent_orders(config, state):
    """获取最近的订单列表，返回 (api_result, from_time)"""
    path = "/open/v1/order/package/fetch/search_package_list"

    body = {
        "page": 1,
        "pageSize": config["monitor"]["page_size"],
    }

    # 用妙手修改时间过滤（注意：该过滤作用于包裹级修改时间，
    # 旧订单被物流/状态同步时也会返回，需配合下单时间判断新旧）
    if state.get("last_check_time"):
        last_time = datetime.fromisoformat(state["last_check_time"])
        from_time = max(
            last_time - timedelta(minutes=config["monitor"]["lookback_minutes"]),
            last_time - timedelta(hours=24)
        )
    else:
        # 首次运行，拉取最近 30 分钟
        from_time = datetime.now(TZ_SHANGHAI) - timedelta(minutes=30)

    body["gmtModifiedFrom"] = from_time.strftime("%Y-%m-%d %H:%M:%S")
    log.info(f"查询参数: {json.dumps(body, ensure_ascii=False)}")
    result = call_miaoshou_api(config, path, body)

    return result, from_time


def extract_orders(api_result):
    """从API响应中提取订单列表"""
    if not api_result:
        return []

    code = api_result.get("code") or api_result.get("result", "")
    if code != "success":
        log.error(f"API 返回错误: code={code}, message={api_result.get('message')}")
        return []

    data = api_result.get("data")
    if not data:
        return []

    # data 可能是 dict (含 orderPackageList) 或 list
    all_orders = []
    if isinstance(data, dict):
        all_orders = data.get("orderPackageList", [])
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                all_orders.extend(item.get("orderPackageList", []))

    return all_orders


def format_order_message(order):
    """将订单信息格式化为推送消息"""
    order_info = order.get("orderInfo", {})
    items = order.get("items", [])
    consignee = order.get("consigneeInfo", {})

    platform = order.get("platformName", order.get("platform", "未知"))
    shop = order.get("shopName", "未知店铺")
    order_sn = order_info.get("platformOrderSn", "无")
    status = order_info.get("appOrderStatusText", order_info.get("appOrderStatus", "未知"))
    amount = order_info.get("orderAmount", 0)
    currency = order_info.get("currency", "")

    country = consignee.get("countryName", consignee.get("country", ""))

    # 商品摘要
    item_lines = []
    for item in items[:5]:  # 最多显示5个
        title = item.get("title", "未知商品")
        qty = item.get("quantity", 0)
        price = item.get("discountedPrice", item.get("originalPrice", 0))
        item_lines.append(f"  - {title} ×{qty}  {price:.2f} {currency}")

    if len(items) > 5:
        item_lines.append(f"  ... 共 {len(items)} 件商品")

    msg = f"""平台: {platform} | 店铺: {shop}
订单号: {order_sn}
状态: {status} | 金额: {amount:.2f} {currency}
国家: {country}

商品:
{chr(10).join(item_lines)}
---
⏰ {datetime.now(TZ_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')}"""

    return msg


def push_to_wechat(config, title, content):
    """通过Server酱推送到微信"""
    send_key = config["serverchan"]["send_key"]
    url = f"https://sctapi.ftqq.com/{send_key}.send"

    try:
        resp = requests.post(url, data={
            "title": title,
            "desp": content,
        }, timeout=15)

        result = resp.json()
        if result.get("code") == 0:
            log.info(f"微信推送成功: {title}")
            return True
        else:
            log.error(f"微信推送失败: {result}")
            return False
    except Exception as e:
        log.error(f"微信推送异常: {e}")
        return False


def run_once(config, state):
    """执行一次轮询检查"""
    log.info("=" * 50)
    log.info("开始轮询妙手ERP...")

    api_result, from_time = fetch_recent_orders(config, state)
    orders = extract_orders(api_result)

    if not orders:
        log.info("���获取到订单数据")
        state["last_check_time"] = datetime.now(TZ_SHANGHAI).isoformat()
        save_state(state)
        return

    log.info(f"获取到 {len(orders)} 条包裹记录")

    # 检测新订单
    notified_ids = set(state.get("notified_order_ids", []))
    new_orders = []

    for order in orders:
        order_info = order.get("orderInfo", {})
        op_order_id = str(order_info.get("opOrderId", ""))

        if not op_order_id or op_order_id in notified_ids:
            continue

        # 只推送"下单时间在本次查询起点之后"的真新订单；
        # 旧订单（仅因物流/状态同步触发包裹修改时间更新）只记录不推送，避免误报旧单
        gmt_start = order_info.get("gmtOrderStart", "")
        is_new = True
        try:
            start_dt = datetime.strptime(gmt_start, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_SHANGHAI)
            is_new = start_dt >= from_time
        except Exception:
            pass

        if not is_new:
            notified_ids.add(op_order_id)
            log.info(f"旧订单(下单 {gmt_start})仅记录不推送: op={op_order_id}")
            continue

        new_orders.append(order)

    if new_orders:
        log.info(f"发现 {len(new_orders)} 个新订单！")

        for order in new_orders:
            order_info = order.get("orderInfo", {})
            op_order_id = str(order_info.get("opOrderId", ""))
            order_sn = order_info.get("platformOrderSn", "未知")

            msg = format_order_message(order)
            platform = order.get("platformName", order.get("platform", "未知"))
            platform_short = {"tiktok": "TK", "shopee": "SP", "lazada": "LZ"}.get(
                platform.lower(), platform)
            title = f"您有一条新的{platform_short} 订单"

            if push_to_wechat(config, title, msg):
                notified_ids.add(op_order_id)
                # 避免发送太快
                time.sleep(1)

    else:
        log.info(f"所有 {len(orders)} 条订单均已通知过")

    # 更新状态
    state["notified_order_ids"] = list(notified_ids)
    state["last_check_time"] = datetime.now(TZ_SHANGHAI).isoformat()
    save_state(state)
    log.info("本轮检查完成")


def main():
    config = load_config()
    state = load_state()

    poll_interval = config["monitor"]["poll_interval_seconds"]

    log.info(f"妙手ERP订单监控启动, 轮询间隔: {poll_interval}秒")
    log.info(f"AppKey: {config['miaoshou']['app_key'][:10]}...")

    try:
        while True:
            try:
                run_once(config, state)
            except Exception as e:
                log.exception(f"轮询出错: {e}")
                # 出错后发送一条告警
                try:
                    push_to_wechat(
                        config,
                        "⚠️ 妙手订单监控异常",
                        f"轮询过程中出现错误:\n```\n{str(e)[:500]}\n```\n请检查日志文件。"
                    )
                except:
                    pass

            # 等待下一轮
            next_run = datetime.now(TZ_SHANGHAI) + timedelta(seconds=poll_interval)
            log.info(f"下一轮检查: {next_run.strftime('%H:%M:%S')}")
            time.sleep(poll_interval)

    except KeyboardInterrupt:
        log.info("监控已手动停止")
        save_state(state)


if __name__ == "__main__":
    main()
