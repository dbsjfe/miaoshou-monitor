#!/usr/bin/env python3
"""
电脑端语音提醒（macOS 本机）
============================
为什么要有这个模块：
    邮件 / Server酱 都是"远程"提醒 —— 你人离开手机、或者微信消息被折叠，
    一单来了你可能十几分钟后才发现。这个模块在**来单的同一台 Mac 上**直接
    念一句「您有一条新的马来订单，请及时处理」，同时弹一条通知中心横幅，
    眼耳双保险，坐在电脑前就不会漏。

零第三方依赖，只用 macOS 自带命令：
    say       文字转语音（Tingting = 中文女声，系统自带）
    osascript 通知中心横幅（可带提示音 sound name）
    afplay    兜底提示音（通知中心不可用时）

设计要点 / 踩坑记录：
    1. 必须跑在用户的 GUI(Aqua) 会话里。start.sh 从终端启动、或 launchd
       LaunchAgent 启动都在 Aqua 会话内，say 有声音、横幅能弹；
       若是 LaunchDaemon(root) 则**静默无声** —— 这是最常见的"配置好了没反应"。
    2. 沙箱环境（AI 会话）里 say 可能被静音或直接失败。这是环境限制而非配置
       错误，自检会把它明确报出来，别当成 bug 查半天。
    3. say 是**同步阻塞**的（念完才返回）。故这里一律用 Popen 分离进程 + 
       start_new_session，绝不 wait()，避免一次播报卡住 180 秒的轮询循环。
    4. 一次轮询可能推送多单，**合并成一句**播报（"您有 3 条新订单"），
       否则多句话重叠在一起谁也听不清。
"""

import logging
import platform
import shutil
import subprocess

log = logging.getLogger("alert")

# 出单地区取不到时的兜底读音（按平台名念，总比念"未知"强）
PLATFORM_TTS = {"tiktok": "TikTok", "shopee": "Shopee", "lazada": "Lazada"}

DEFAULT_ONE = "您有一条新的{region}订单，请及时处理"
DEFAULT_MANY = "您有{count}条新订单，出单地区{regions}，请及时处理"


class DesktopAlert:
    """来单语音播报器。构造即自检，不可用时全部方法降级为空操作。"""

    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.voice = str(cfg.get("voice", "Tingting") or "")
        self.rate = int(cfg.get("rate", 190))
        self.sound = str(cfg.get("sound", "Glass") or "")
        self.notify_center = bool(cfg.get("notify_center", True))
        self.t_one = str(cfg.get("template_one", DEFAULT_ONE))
        self.t_many = str(cfg.get("template_many", DEFAULT_MANY))
        self.max_seconds = float(cfg.get("max_seconds", 15))

        self._say = shutil.which("say") or ""
        self._osascript = shutil.which("osascript") or ""
        self._afplay = shutil.which("afplay") or ""
        self._procs: list = []          # 分离出去的 say/osascript 进程，用于回收
        self._checked = False
        self.ok = False
        self.reason = "未自检"

    # ---------------- 自检 ----------------
    def check(self) -> tuple[bool, str]:
        """返回 (是否可用, 原因)。原因字符串可直接展示给用户排障。"""
        if not self.enabled:
            return self._set(False, "未启用（config.json → desktop_alert.enabled = false）")
        if platform.system() != "Darwin":
            return self._set(False, f"非 macOS（当前 {platform.system()}），本模块跳过")
        if not self._say:
            return self._set(False, "找不到系统命令 say")
        if not shutil.which("afplay") and not self._osascript:
            return self._set(False, "系统缺少 osascript/afplay，无法弹窗或放提示音")

        self._resolve_voice()
        return self._set(True, f"语音 {self.voice or '系统默认'}，速率 {self.rate}"
                               f"{'，含通知横幅' if self.notify_center else ''}")

    def _set(self, ok: bool, reason: str) -> tuple[bool, str]:
        self.ok, self.reason, self._checked = ok, reason, True
        log.log(logging.INFO if ok else logging.WARNING,
                f"[语音] {'✅ 可用: ' if ok else '⏭️ 已跳过: '}{reason}")
        return ok, reason

    def _resolve_voice(self):
        """确认配置的声音存在；不存在则退回系统默认（不报错，只提醒）"""
        if not self.voice:
            return
        try:
            out = subprocess.run([self._say, "-v", "?"], capture_output=True,
                                 text=True, timeout=10).stdout
            names = {ln.split()[0] for ln in out.splitlines() if ln.strip()}
        except Exception as e:
            log.warning(f"[语音] 读取声音列表失败（{e}），改用系统默认音色")
            self.voice = ""
            return
        if self.voice not in names:
            log.warning(f"[语音] 声音「{self.voice}」不存在，改用系统默认音色")
            self.voice = ""

    # ---------------- 播报 ----------------
    def announce(self, snaps: list) -> bool:
        """把本轮推送成功的订单快照合成一句话念出来。snaps 为空则什么都不做。"""
        if not snaps:
            return False
        if not self._checked:
            self.check()
        if not self.ok:
            return False

        speech = self._speech_text(snaps)
        banner = self._banner_text(snaps)
        if self.notify_center:
            self._notify(banner)
        self._speak(speech)
        log.info(f"[语音] 🔊 播报: {speech}")
        return True

    def _region(self, s: dict) -> str:
        """单条订单的地区读音：优先中文名（马来/菲律宾），退回站点码，再退回平台名"""
        name = str(s.get("site_name") or "").strip()
        site = str(s.get("site") or "").strip()
        if name:
            return name
        if site:
            return site
        return PLATFORM_TTS.get(str(s.get("platform", "")).lower(),
                               str(s.get("platform") or "TikTok"))

    def _regions(self, snaps: list) -> list:
        out = []
        for s in snaps:
            r = self._region(s)
            if r and r not in out:
                out.append(r)
        return out or ["TikTok"]

    def _speech_text(self, snaps: list) -> str:
        n = len(snaps)
        if n == 1:
            s = snaps[0]
            return self.t_one.format(region=self._region(s), count=1,
                                     amount=f"{s.get('amount', 0):.0f}",
                                     currency=str(s.get("currency", "")),
                                     shop=str(s.get("shop", "")))
        return self.t_many.format(count=n, regions="、".join(self._regions(snaps)),
                                  region=self._region(snaps[0]),
                                  amount="", currency="",
                                  shop=str(snaps[0].get("shop", "")))

    def _banner_text(self, snaps: list) -> str:
        n = len(snaps)
        if n == 1:
            s = snaps[0]
            return (f"出单地区 {self._region(s)} · "
                    f"{s.get('amount', 0):.2f} {s.get('currency', '')}\n"
                    f"{s.get('sn', '')}")
        total = sum(s.get("amount", 0) for s in snaps)
        cur = snaps[0].get("currency", "")
        return (f"共 {n} 单 · 合计 {total:.2f} {cur}\n"
                f"地区 {'、'.join(self._regions(snaps))}")

    # ---------------- 底层调用 ----------------
    def _spawn(self, argv: list):
        """分离进程执行，绝不阻塞轮询循环；顺手回收已结束的子进程防僵尸"""
        self._procs = [p for p in self._procs if p.poll() is None]
        try:
            self._procs.append(subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True))
        except Exception as e:
            log.warning(f"[语音] 执行 {argv[0]} 失败: {e}")

    def _speak(self, text: str):
        if not text:
            return
        argv = [self._say]
        if self.voice:
            argv += ["-v", self.voice]
        argv += ["-r", str(self.rate), text]
        self._spawn(argv)

    def _notify(self, body: str):
        if not self._osascript:
            if self._afplay and self.sound:
                self._spawn([self._afplay,
                             f"/System/Library/Sounds/{self.sound}.aiff"])
            return
        title = "🛒 新的 TK 订单"
        parts = [f'display notification "{_as_str(body)}"',
                 f'with title "{_as_str(title)}"']
        if self.sound:
            parts.append(f'sound name "{_as_str(self.sound)}"')
        self._spawn([self._osascript, "-e", " ".join(parts)])


def _as_str(s: str) -> str:
    """转义成 AppleScript 字符串字面量内部安全的形式（转义反斜杠与引号）"""
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "  ")


# ---------------- 命令行自检 ----------------
def _main():
    import json
    import sys
    import time
    from pathlib import Path

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cfg_file = Path(__file__).resolve().parent / "config.json"
    cfg = {}
    if cfg_file.exists():
        with open(cfg_file, encoding="utf-8") as f:
            cfg = json.load(f).get("desktop_alert", {})

    alert = DesktopAlert(cfg)
    ok, reason = alert.check()
    print(f"\n语音提醒自检: {'✅ 可用' if ok else '❌ 不可用'} —— {reason}\n")
    if not ok:
        return 1

    demo = [{"sn": "SELFTEST-0001", "site": "MY", "site_name": "马来",
             "platform": "tiktok", "amount": 123.45, "currency": "MYR",
             "shop": "SoftTots MY"}]
    print("即将播报单条示例：", alert._speech_text(demo))
    alert.announce(demo)
    time.sleep(6)
    demo2 = demo + [{"sn": "SELFTEST-0002", "site": "PH", "site_name": "菲律宾",
                     "platform": "tiktok", "amount": 394.0, "currency": "PHP",
                     "shop": "SoftTots PH"}]
    print("即将播报多条示例：", alert._speech_text(demo2))
    alert.announce(demo2)
    print("\n若上面两句话你听到了、横幅也弹了，说明语音提醒已生效。")
    print("没声音 → 检查：① 系统设置→声音 未静音 ② 音量 ③ 是否在 LaunchDaemon 下运行\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
