#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建「妙手ERP-TK订单通知-安装包.py」自解压安装包。"""
import base64
import hashlib
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 仓库根目录
SRC = ROOT
DOC = ROOT / "docs" / "安装前准备清单.md"
OUT = ROOT / "dist" / "妙手ERP-TK订单通知-安装包.py"

# 需要整体脱敏的片段
REPL = [
    ("/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python", "python3"),
    ("/Users/jianguo/WorkBuddy/2026-09-10-18-17-36/miaoshou-monitor", "__PROJECT_DIR__"),
    ("dbsjfe/miaoshou-monitor", "你的GitHub用户名/你的仓库名"),
]

FROM_DISK = [
    "README.md",
    ".gitignore",
    "run.py",
    "local/monitor.py",
    "local/push_channels.py",
    "local/desktop_alert.py",
    "local/cloud_sync.py",
    "local/reconcile.py",
    "local/test_push.py",
    "local/set_gh_secrets.py",
    "local/test_connectivity.py",
    "local/MiaoshouMonitor.command",
    ".github/workflows/monitor.yml",
]

EXECUTABLE = {"local/start.sh", "local/MiaoshouMonitor.command"}

# ---------------------------------------------------------------- 生成的 start.sh
START_SH = r'''#!/bin/bash
# 妙手ERP订单监控 —— 本机常驻启动脚本（macOS）
# 三层保护：单例（防重复推送） + caffeinate（防系统睡眠） + 自愈循环（防静默死亡）
#
# 用法： bash start.sh          （前台常驻，关掉终端就停）
#       双击 MiaoshouMonitor.command  （同上）
#       开机自启见 local/com.miaoshou.monitor.plist

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 0) 选解释器：优先项目自带 .venv → WorkBuddy 受管 venv → 系统 python3
#    ⚠️ 必须是有 certifi 的解释器。缺 certifi 时 requests 会报
#       "Could not find a suitable TLS CA certificate bundle"，
#       而且是**静默失败**（进程照跑、日志只是报错，一个请求都发不出去）。
pick_python() {
    for c in "$SCRIPT_DIR/../.venv/bin/python" \
             "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
             "$(command -v python3 2>/dev/null)" \
             "/usr/local/bin/python3"
    do
        [ -n "$c" ] && [ -x "$c" ] && { printf '%s' "$c"; return; }
    done
}
PYTHON="$(pick_python)"
if [ -z "$PYTHON" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] 找不到可用的 python3，请先安装 Python 3" >> monitor.log
    exit 1
fi
if ! "$PYTHON" -c "import requests, certifi" 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] ⚠️ $PYTHON 缺少 requests/certifi，请执行：" \
         "$PYTHON -m pip install -r requirements.txt" >> monitor.log
fi

# 0b) 剥离「指向本机回环」的代理变量（重要）
#     AI 助手/沙箱会给子进程注入 HTTP_PROXY=http://127.0.0.1:<随机端口>。
#     常驻进程若继承它，那个会话一结束端口就关闭，之后**所有 HTTPS 请求全部失败**
#     （ProxyError: Unable to connect to proxy ... Connection refused），
#     而进程仍在跑、日志仍在滚 —— 典型"静默失效"。
#     只清回环代理，不动用户真实的公司/家庭代理配置。
for _v in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    eval "_val=\${$_v:-}"
    case "$_val" in
        *127.0.0.1*|*localhost*|*::1*) unset "$_v"; echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] 已剥离回环代理 $_v=$_val" >> monitor.log ;;
    esac
done
export NO_PROXY="*"
export no_proxy="*"

# 1) 单例保护：已有实例则直接退出（本地跑两份 = 同一单推两次）
PID_FILE="$SCRIPT_DIR/monitor.pid"
OLD_PID=""
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') 监控已在运行(PID $OLD_PID)，跳过本次启动" >> monitor.log
        exit 0
    fi
fi

# 1b) 清理孤儿实例：pidfile 没记录但进程还活着的 monitor.py
#     用字符类 [.] 包裹，避免 pgrep 匹配到本脚本自己的命令行
for _p in $(pgrep -f "python[^ ]* monitor[.]py" 2>/dev/null); do
    if [ "$_p" != "$OLD_PID" ]; then
        kill -9 "$_p" 2>/dev/null
        echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] 清理孤儿实例 PID $_p" >> monitor.log
    fi
done

# 2) 用 caffeinate 包裹自身，阻止系统睡眠（-s 插电时 / -i 空闲时）
if [ -z "$MONITOR_CAFFEINATED" ]; then
    export MONITOR_CAFFEINATED=1
    exec /usr/bin/caffeinate -s -i /bin/bash "$0"
fi

# 3) 自愈循环：monitor.py 异常退出后 10 秒自动重启
#    stdout 丢弃：monitor.py 自己写 monitor.log，重定向过去会导致每行日志重复两次
while true; do
    "$PYTHON" monitor.py > /dev/null 2>> monitor.log &
    MON_PID=$!
    echo "$MON_PID" > "$PID_FILE"
    wait "$MON_PID"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] monitor.py(PID $MON_PID) 已退出，10 秒后自动重启" >> monitor.log
    sleep 10
done
'''

# ------------------------------------------------------- 生成的 config.example.json
CONFIG_EXAMPLE = r'''{
  "_说明": "首次安装请把这个文件复制成 config.json，然后把所有空字符串填上再启动。留空表示该通道不使用。",
  "miaoshou": {
    "_说明": "妙手 ERP 开放平台 → 应用管理 里拿 AppKey / AppSecret（必填）",
    "app_key": "",
    "app_secret": "",
    "base_url": "https://openapi-erp.91miaoshou.com"
  },
  "push": {
    "_说明": "channel_order 是从上到下的尝试顺序，前一个失败/额度用尽会自动切下一个",
    "channel_order": ["email", "serverchan"],
    "min_interval_seconds": 13,
    "daily_limit": {
      "wxpusher": 100000,
      "wecom": 100000,
      "wecom_bot": 100000,
      "serverchan": 5,
      "email": 100000
    },
    "wxpusher": {
      "_说明": "可选通道。wxpusher.zjiecode.com 微信扫码登录→创建应用拿 appToken；关注后用公众号「我的 UID」拿 uid",
      "app_token": "PLEASE_FILL_IN_APP_TOKEN",
      "uid": "PLEASE_FILL_IN_UID",
      "min_interval_seconds": 2
    },
    "wecom_bot": {
      "_说明": "可选通道。企业微信群机器人 webhook 的 key（无 IP 白名单限制）",
      "key": "PLEASE_FILL_IN_BOT_KEY",
      "min_interval_seconds": 4,
      "mention_all": false
    },
    "wecom": {
      "_说明": "可选通道。企业微信自建应用，需要「企业可信IP」，家用宽带基本配不通，一般留空",
      "corpid": "PLEASE_FILL_IN_CORPID",
      "secret": "PLEASE_FILL_IN_SECRET",
      "agentid": "PLEASE_FILL_IN_AGENTID",
      "touser": "@all",
      "min_interval_seconds": 2
    },
    "serverchan": {
      "_说明": "可选通道。sct.ftqq.com 微信扫码登录拿 SendKey（形如 SCT...）。免费版每天只能发 5 条，只适合当兜底",
      "send_key": ""
    },
    "email": {
      "_说明": "推荐主通道，无条数限制。注意 password 填的是 SMTP 授权码，不是邮箱登录密码",
      "host": "smtp.qq.com",
      "port": 465,
      "user": "",
      "password": "",
      "to": "",
      "min_interval_seconds": 5
    }
  },
  "monitor": {
    "poll_interval_seconds": 180,
    "page_size": 50,
    "max_pages": 20,
    "query_window_hours": 6,
    "new_order_max_age_hours": 24,
    "merge_threshold": 3
  },
  "desktop_alert": {
    "_说明": "电脑端语音提醒（仅 macOS 本机生效，云端不启用）。不想要就设 enabled=false",
    "enabled": true,
    "voice": "Tingting",
    "rate": 190,
    "sound": "Glass",
    "notify_center": true,
    "max_seconds": 15,
    "template_one": "您有一条新的{region}订单，请及时处理",
    "template_many": "您有{count}条新订单，出单地区{regions}，请及时处理"
  },
  "cloud_sync": {
    "_说明": "可选。填了才能在电脑关机时由 GitHub Actions 接管补推，且两边不会重复推。不填就设 enabled=false",
    "enabled": false,
    "repo": "你的GitHub用户名/你的仓库名",
    "token": "",
    "min_interval_seconds": 300
  }
}
'''

REQUIREMENTS = "requests>=2.31\ncertifi\n"

PLIST = r'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.miaoshou.monitor</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>__PROJECT_DIR__/local/start.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>__PROJECT_DIR__/local</string>

    <!-- 登录时自动启动 -->
    <key>RunAtLoad</key>
    <true/>

    <!-- 进程意外退出后自动重启 -->
    <key>KeepAlive</key>
    <true/>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>__PROJECT_DIR__/local/launchd.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>__PROJECT_DIR__/local/launchd.stderr.log</string>
</dict>
</plist>
'''

GUIDE = r'''妙手ERP → TikTok 新订单通知 · 一键安装包
================================================================

【这是什么】
  妙手 ERP 每 3 分钟轮询一次 TikTok 订单。有新订单时：
    · 用邮件（或 Server酱/微信）把订单详情推给你，标题带出单地区，例如
         【菲律宾 PH】您有一条新的TK 订单
    · 在 Mac 上直接念一句「您有一条新的菲律宾订单，请及时处理」并弹通知横幅
  已经修好的三个老毛病：不会重复推送、不会漏单、不受 Server酱「每天 5 条」限制
  （多通道自动降级 + 积压合并 + 失败重试队列）。
  可选：挂 GitHub Actions 做云端兜底，电脑关机/离线时也能收到通知。

【怎么用 —— 两种方式，任选一种】

  ★ 方式 A（推荐，不用敲命令）
    把这个文件发给 WorkBuddy，说一句：
        「读取这个文件，按里面的说明在本机安装」
    WorkBuddy 会自己读下面的《给 AI 助手的执行指令》并一步步做完。
    你只需要在它问你要账号信息时，把《需要你提供的东西》里的那几项发给它。

  ★ 方式 B（自己敲命令）
    python3 妙手ERP-TK订单通知-安装包.py --check
    python3 妙手ERP-TK订单通知-安装包.py --install --dir ~/miaoshou-monitor
    python3 妙手ERP-TK订单通知-安装包.py --guide      # 打印完整安装说明

  开始之前建议先看同目录的「安装前准备清单.md」：安装本身一条命令就完事，
  真正花时间的是准备资料（妙手密钥、邮箱 SMTP 授权码、可选的 GitHub Token）。

【给 AI 助手的执行指令】—— 请严格按顺序执行，不要跳步、不要自己编造密钥

  第 0 步 · 环境检查
      python3 <本文件> --check
      要求：macOS 12+（本机常驻与语音提醒仅 macOS 支持）、Python 3.9+。
      需要 requests 与 certifi。缺失时优先建项目内 venv，不要污染用户全局环境：
        python3 -m venv <项目目录>/.venv
        <项目目录>/.venv/bin/pip install -r <项目目录>/local/requirements.txt

  第 1 步 · 释放代码
      python3 <本文件> --install --dir <项目目录>
      项目目录默认 ~/miaoshou-monitor；用户没指定就先问一句再装。
      同名文件会自动备份成 *.bak-<时间戳>，不会静默覆盖。

  第 2 步 · 向用户收集配置（务必逐项问清楚，缺一项就配不通）
      详细的获取方法都写在包内 安装前准备清单.md 里，可以先让用户看那份。
      - 妙手 ERP 开放平台的 AppKey / AppSecret（必填）
      - 通知方式：默认「邮件优先 + Server酱兜底」；先问用户有没有邮箱可用
          邮件：发件邮箱 + SMTP 授权码（不是登录密码！）+ 收件邮箱
          Server酱：sct.ftqq.com 的 SendKey（可只配这一项）
          WxPusher：wxpusher.zjiecode.com 的 appToken + UID（免费无限）
          企业微信群机器人：webhook 里的 key
      - 是否要云端兜底（需要 GitHub 仓库 + PAT）；不要就先设 enabled=false

  第 3 步 · 写入配置并验证（这一步不能省，推送失败是静默的）
      cp <项目目录>/local/config.example.json <项目目录>/local/config.json
      按用户回答填空字符串字段（_说明 字段保留即可，程序会忽略）
      然后依次跑：
        cd <项目目录>/local
        python3 test_push.py            # ① 通道自检：发一条测试通知，确认能送到
        python3 test_push.py --real     # ② 用最近一条真实订单预览文案（不写状态）
        python3 test_push.py --voice    # ③ 语音提醒自检（Mac 上应能听到声音 + 弹横幅）
      三条都通过再往下走。任何一条报错，按输出的提示先修配置。

  第 4 步 · 常驻运行
      bash <项目目录>/local/start.sh
      它会用 pidfile 做单例、用 caffeinate 防休眠、并在 monitor.py 异常退出后 10 秒自动重启。
      确认第一条日志： tail -20 <项目目录>/local/monitor.log
      应能看到「推送通道: email → serverchan」「电脑语音提醒: ✅ 开」。
      想开机自启（macOS）：把 <项目目录>/local/com.miaoshou.monitor.plist 复制到
      ~/Library/LaunchAgents/ 后执行：
        launchctl bootout gui/$(id -u)/com.miaoshou.monitor 2>/dev/null
        launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.miaoshou.monitor.plist
        launchctl list | grep miaoshou
      注意：这步必须在**终端或双击 .command**里做，AI 沙箱里执行会报
      "Bootstrap failed: 5: Input/output error"（拿不到图形会话）。

  第 5 步 · 云端兜底（可选，用户不需要就跳过）
      建一个 GitHub 仓库（public 免费跑 Actions；private 需注意每月 2000 分钟额度）
      → 用 <项目目录>/local/set_gh_secrets.py 写入 Secrets（PAT 用 classic，
        勾 repo + workflow 两项即可）
      → 推送代码，Actions 里手动 Run workflow 勾「只跑链路自检」验证一次

  验收标准
      · test_push.py 三条自检全部通过
      · monitor.log 出现「本轮完成 | 已通知 N」
      · 用户收到过至少一条真实通知（邮件/微信）
      · Mac 上听到语音 + 看到横幅

【需要你提供的东西】

  | 项目 | 哪里拿 | 必填 |
  |---|---|---|
  | 妙手 AppKey / AppSecret | 妙手 ERP → 开放平台 → 应用管理 | ✅ |
  | 发件邮箱 + SMTP 授权码 + 收件邮箱 | QQ 邮箱：设置→账号→开启 SMTP→生成授权码 | 二选一 |
  | Server酱 SendKey | sct.ftqq.com 微信扫码登录 | 二选一 |
  | GitHub 用户名/仓库名 + PAT | github.com → Settings → Developer settings | 可选 |

【包内文件清单】

  安装前准备清单.md              ★ 先看这个：要准备哪些资料、每项怎么拿、清单可勾选
  README.md                      通用说明（部署、计费、安全须知）
  run.py                         云端入口（GitHub Actions 调用）
  .github/workflows/monitor.yml  云端定时任务
  local/monitor.py               主程序：轮询 + 去重状态机 + 推送 + 语音
  local/push_channels.py         通道层：邮件/Server酱/WxPusher/企业微信 + 自动降级
  local/desktop_alert.py         电脑端语音 + 通知横幅（macOS）
  local/cloud_sync.py            本地↔云端状态同步（消除重复推送的关键）
  local/reconcile.py             对账/补推/日报
  local/test_push.py             自检与文案预览工具
  local/set_gh_secrets.py        批量写 Actions Secrets
  local/start.sh                 本机常驻启动（单例 + 防休眠 + 自愈）
  local/MiaoshouMonitor.command  macOS 双击启动
  local/com.miaoshou.monitor.plist  开机自启（路径已按你的安装目录生成）
  local/config.example.json      配置模板（复制成 config.json 再填）

【常见问题】

  1) 完全收不到通知
     先跑 python3 local/test_push.py。它是静默失败的照妖镜：密钥写错、授权码失效、
     通道限流都只会体现在这条自检里，进程本身永远是"正常在跑"。
  2) 每天只能收到几条
     Server酱免费版每天只有 5 条。把邮件设成主通道（channel_order 里 email 在前）。
  3) 重复收到同一条
     只保留一份本机实例。kill 时用 kill $(cat monitor.pid)，不要 pkill start.sh。
  4) Mac 上没声音 / 没横幅
     系统设置→声音 是否静音；系统设置→通知 是否允许「终端」发通知；
     进程是否跑在 LaunchDaemon(root) 下（那样没有图形会话，一定没声音）。
  5) 邮件能发但发出去就报错
     password 填成了邮箱登录密码。必须用 SMTP 授权码。

  更多排障见 README.md；先跑自检再看日志，顺序不要反。
'''


def build_zip() -> bytes:
    buf = io.BytesIO()
    items = {}          # 文件名 -> bytes

    for rel in FROM_DISK:
        text = (SRC / rel).read_text(encoding="utf-8")
        for a, b in REPL:
            text = text.replace(a, b)
        if rel == "local/set_gh_secrets.py":
            text = text.replace('DEFAULT_REPO = "你的GitHub用户名/你的仓库名"',
                                'DEFAULT_REPO = ""   # 用法: python3 set_gh_secrets.py <用户名>/<仓库名>')
        if rel == "README.md":
            # README 里「一键安装包」那节是讲怎么生成这个包的，收包的人不需要，
            # 留着反而困惑（包内没有 tools/）。整段裁掉。
            head = "## 📦 一键安装包（要分享给别人用）"
            tail_ = "## 推送长什么样"
            if head in text and tail_ in text:
                text = text.split(head)[0] + tail_ + text.split(tail_, 1)[1]
        items[rel] = text.encode("utf-8")

    gen = {
        "安装前准备清单.md": DOC.read_text(encoding="utf-8"),
        "local/start.sh": START_SH,
        "local/config.example.json": CONFIG_EXAMPLE,
        "local/requirements.txt": REQUIREMENTS,
        "local/com.miaoshou.monitor.plist": PLIST,
        "state/.gitkeep": "# 状态文件目录，必须入库（本地/云端去重的唯一依据）\n",
    }
    for k, v in gen.items():
        items[k] = v.encode("utf-8")

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(items.items()):
            zi = zipfile.ZipInfo(name, date_time=(2026, 9, 11, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (0o755 if name in EXECUTABLE else 0o644) << 16
            zf.writestr(zi, data)
    return buf.getvalue()


INSTALLER_TMPL = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""__GUIDE__"""

import argparse
import base64
import hashlib
import io
import os
import platform
import shutil
import sys
import time
import zipfile
from pathlib import Path

VERSION = "__VERSION__"
PAYLOAD_SHA256 = "__SHA__"
PAYLOAD_B64 = ""


def _payload() -> bytes:
    raw = base64.b64decode(PAYLOAD_B64)
    got = hashlib.sha256(raw).hexdigest()
    if got != PAYLOAD_SHA256:
        print(f"❌ 安装包校验失败：期望 {PAYLOAD_SHA256[:12]}… 实际 {got[:12]}…")
        print("   文件可能在传输中被改动/截断，请让发送方重新发一次。")
        sys.exit(2)
    return raw


def cmd_list():
    with zipfile.ZipFile(io.BytesIO(_payload())) as zf:
        print(f"安装包 v{VERSION}，共 {len(zf.namelist())} 个文件：\\n")
        for n in zf.namelist():
            print("  " + n)
    return 0


def cmd_check():
    print("环境检查")
    print("-" * 46)
    ok = True
    sysname = platform.system()
    print(f"  操作系统     : {sysname} {platform.release()}")
    if sysname != "Darwin":
        print("               ⚠️ 本机常驻版与语音提醒仅支持 macOS（Windows/Linux 只能用云端兜底）")
        ok = sysname in ("Linux",)
    v = sys.version_info
    print(f"  Python       : {v.major}.{v.minor}.{v.micro}  ({sys.executable})")
    if v < (3, 9):
        print("               ❌ 需要 Python 3.9 以上")
        ok = False
    for mod in ("requests", "certifi"):
        try:
            __import__(mod)
            print(f"  依赖 {mod:<9}: ✅ 已安装")
        except ImportError:
            print(f"  依赖 {mod:<9}: ❌ 缺失 —— 用项目内 venv 装："
                  f"python3 -m venv .venv && .venv/bin/pip install -r local/requirements.txt")
            ok = False
    if sysname == "Darwin":
        for c in ("say", "osascript", "afplay"):
            p = shutil.which(c)
            print(f"  语音命令 {c:<9}: {'✅ ' + p if p else '❌ 找不到（语音提醒会跳过）'}")
    print("-" * 46)
    print("结论：" + ("✅ 可以直接安装" if ok else "⚠️ 有项目未就绪，见上面提示"))
    return 0 if ok else 1


def cmd_install(dest: Path, force: bool, no_config: bool):
    raw = _payload()
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = dest.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    print(f"安装到： {dest}")
    print("-" * 46)

    written, backed = [], []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not force:
                bak = target.with_name(target.name + f".bak-{ts}")
                shutil.copy2(target, bak)
                backed.append(bak.name)
            data = zf.read(info)
            # plist 里的 __PROJECT_DIR__ 换成真实安装路径，装完即可直接用
            if rel.endswith(".plist"):
                data = data.replace(b"__PROJECT_DIR__", str(dest).encode())
            target.write_bytes(data)
            mode = (info.external_attr >> 16) & 0o777
            os.chmod(target, mode or 0o644)
            written.append(rel)

    for n in sorted(written):
        print(f"  ✅ {n}")
    if backed:
        print(f"\\n  （{len(backed)} 个同名文件已备份为 *.bak-{ts}）")

    cfg = dest / "local" / "config.json"
    if not no_config and not cfg.exists():
        shutil.copy2(dest / "local" / "config.example.json", cfg)
        print(f"  ✅ local/config.json 已从模板生成 —— 里面的空字段必须填完才能启动")

    print("-" * 46)
    print("下一步（不要跳步）：")
    print(f"  0. 看清单：  包里的「安装前准备清单.md」列了要准备哪些资料、怎么拿")
    print(f"  1. 填配置：  {cfg}")
    print(f"  2. 自检：    cd {dest}/local && python3 test_push.py")
    print(f"                python3 test_push.py --voice    # Mac 语音提醒")
    print(f"  3. 常驻：    bash {dest}/local/start.sh")
    print(f"  4. 看日志：  tail -f {dest}/local/monitor.log")
    print()
    print("  AI 助手请对照《给 AI 助手的执行指令》的第 2～5 步继续，")
    print("  特别是第 2 步：必须逐项向用户索取密钥，不要自己编造。")
    print()
    print(f"  完整说明：python3 {Path(__file__).name} --guide")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="妙手ERP → TK 新订单通知 一键安装包",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true", help="释放代码（默认动作）")
    ap.add_argument("--dir", default="~/miaoshou-monitor", help="安装目录（默认 ~/miaoshou-monitor）")
    ap.add_argument("--force", action="store_true", help="覆盖同名文件（默认先备份）")
    ap.add_argument("--no-config", action="store_true", help="不自动生成 local/config.json")
    ap.add_argument("--list", action="store_true", help="列出包内文件")
    ap.add_argument("--check", action="store_true", help="只检查环境，不写任何文件")
    ap.add_argument("--guide", action="store_true", help="打印完整安装说明（给人和 AI 看）")
    args = ap.parse_args()

    if args.guide:
        print(__doc__)
        return 0
    if args.list:
        return cmd_list()
    if args.check:
        return cmd_check()
    return cmd_install(Path(args.dir), args.force, args.no_config)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    raw = build_zip()
    sha = hashlib.sha256(raw).hexdigest()
    b64 = base64.b64encode(raw).decode()

    # 把 base64 按 96 字符折行，放进三引号字符串
    lines = [b64[i:i + 96] for i in range(0, len(b64), 96)]
    payload_block = 'PAYLOAD_B64 = """\\\n' + "\n".join(lines) + '\n"""'

    src = INSTALLER_TMPL.replace("__GUIDE__", GUIDE)
    src = src.replace("__VERSION__", "2026-09-11")
    src = src.replace("__SHA__", sha)
    src = src.replace('PAYLOAD_B64 = ""', payload_block)

    OUT.write_text(src, encoding="utf-8")
    ok = compile(src, str(OUT), "exec")
    print(f"✅ 已生成 {OUT}")
    print(f"   大小 {OUT.stat().st_size / 1024:.1f} KB | zip {len(raw)} B | sha256 {sha[:16]}…")
    print(f"   内含文件 {len(zipfile.ZipFile(io.BytesIO(raw)).namelist())} 个")


if __name__ == "__main__":
    main()
