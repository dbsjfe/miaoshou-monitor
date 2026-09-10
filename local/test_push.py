#!/usr/bin/env python3
"""推送通道自检：本地和云端都能跑，发一条测试通知并报告实际生效的通道。

为什么要单独有这个文件：
    推送失败是"静默"的 —— 通道密钥写错、授权码失效、通道被限流，进程都照跑不误。
    定期（或改动 secrets 之后）手动跑一次，才能确认"消息真的能送到"。

用法：
    本地：/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python test_push.py
    云端：Actions → 妙手ERP订单通知（云端接管）→ Run workflow → 勾选「只跑链路自检」

只读配置，不读订单，不改状态文件。
"""
import logging
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("test_push")

from push_channels import Notifier  # noqa: E402
from reconcile import build_cfg      # noqa: E402


def main() -> int:
    cfg = build_cfg()
    order = cfg["push"].get("channel_order", [])
    notifier = Notifier(cfg, {"notified": {}, "pending": {}})

    log.info("配置的通道顺序: %s", order)
    log.info("实际启用的通道: %s", list(notifier.channels.keys()))
    log.info("实际生效顺序  : %s", notifier.order)
    if not notifier.channels:
        log.error("❌ 没有任何可用通道 —— 检查 config.json / Actions Secrets")
        return 1

    ok, via = notifier.send(
        "✅ 订单通知链路自检",
        "这是一条自检消息，用来确认订单通知真的能送达。\n\n"
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"本次实际生效通道: {order[0] if order else '未知'}\n\n"
        "如果你在邮箱或微信里看到了这条消息，说明通道正常。\n"
        "没收到就要查通道密钥/授权码了 —— 这类失败不会自己报错。")

    log.info("=" * 48)
    if ok:
        log.info("✅ 自检通过：经由通道「%s」送达", via)
        return 0
    log.error("❌ 自检失败：所有通道都没发出去，详情见上方日志")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
