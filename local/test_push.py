#!/usr/bin/env python3
"""推送通道自检 / 文案预览：本地和云端都能跑。

为什么要单独有这个文件：
    推送失败是"静默"的 —— 通道密钥写错、授权码失效、通道被限流，进程都照跑不误。
    定期（或改动 secrets 之后）手动跑一次，才能确认"消息真的能送到"。

用法：
    # 1) 只发一条测试通知，确认通道通不通
    python test_push.py

    # 2) 拉最近 1 条真实订单，按正式格式推送 —— 用来预览文案长相
    python test_push.py --real
    python test_push.py --real --count 3   # 最近 3 条，走合并推送格式

云端：Actions → 妙手ERP订单通知（云端接管）→ Run workflow → 勾选「只跑链路自检」

只读订单，**不写** orders_state.json，不会影响去重名单（重复跑同一单也不会被记成已通知）。
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("test_push")

from push_channels import Notifier  # noqa: E402
from reconcile import build_cfg      # noqa: E402


def report_channels(cfg) -> Notifier:
    notifier = Notifier(cfg, {"notified": {}, "pending": {}})
    log.info("配置的通道顺序: %s", cfg["push"].get("channel_order", []))
    log.info("实际启用的通道: %s", list(notifier.channels.keys()))
    log.info("实际生效顺序  : %s", notifier.order)
    return notifier


def send_test(notifier) -> int:
    ok, via = notifier.send(
        "✅ 订单通知链路自检",
        "这是一条自检消息，用来确认订单通知真的能送达。\n\n"
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "如果你在邮箱或微信里看到了这条消息，说明通道正常。\n"
        "没收到就要查通道密钥/授权码了 —— 这类失败不会自己报错。")
    log.info("=" * 48)
    if ok:
        log.info("✅ 自检通过：经由通道「%s」送达", via)
        return 0
    log.error("❌ 自检失败：所有通道都没发出去，详情见上方日志")
    return 1


def send_real(cfg, notifier, count: int) -> int:
    """拉最近的真实订单，按正式推送格式发一条 —— 纯预览，不动状态文件"""
    from monitor import (TZ_SHANGHAI, fetch_all_orders, format_batch,  # noqa: E402
                         format_message, snapshot, title_new)

    since = datetime.now(TZ_SHANGHAI) - timedelta(hours=72)
    log.info("拉取 %s 之后的订单…", since.strftime("%m-%d %H:%M"))
    orders = fetch_all_orders(cfg["miaoshou"], cfg["monitor"], since)
    log.info("拉到 %d 条", len(orders))
    if not orders:
        log.error("❌ 最近 72 小时没有订单，换个时间再试，或去掉 --real 只做连通性自检")
        return 1

    # 按下单时间倒序，取最近的 N 单
    orders.sort(key=lambda o: o.get("orderInfo", {}).get("gmtOrderStart", ""),
                reverse=True)
    snaps = [snapshot(o) for o in orders[:max(1, count)]]

    if len(snaps) == 1:
        title = title_new(snaps[0], "（文案预览）")
        content = format_message(snaps[0])
    else:
        title = f"新订单 {len(snaps)} 单（合并推送·文案预览）"
        content = format_batch(snaps)

    log.info("=" * 48)
    log.info("标题: %s", title)
    log.info("-" * 48)
    for line in content.splitlines():
        log.info("  %s", line)
    log.info("=" * 48)

    ok, via = notifier.send(title, content)
    log.info("推送 %s | 经由通道: %s", "成功" if ok else "失败", via)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="推送链路自检 / 文案预览")
    ap.add_argument("--real", action="store_true",
                    help="拉真实订单并按正式格式推送（预览文案），不写状态文件")
    ap.add_argument("--count", type=int, default=1, help="--real 时推送几条（默认 1）")
    args = ap.parse_args()

    cfg = build_cfg()
    notifier = report_channels(cfg)
    if not notifier.channels:
        log.error("❌ 没有任何可用通道 —— 检查 config.json / Actions Secrets")
        return 1
    return send_real(cfg, notifier, args.count) if args.real else send_test(notifier)


if __name__ == "__main__":
    raise SystemExit(main())
