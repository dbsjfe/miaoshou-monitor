#!/usr/bin/env python3
"""
云端入口（GitHub Actions）
=========================
每 10 分钟被 cron 唤起一次，干两件事之一：

  A. **接管补推**（`--cloud-fix`）：Mac 关机/离线时，本机推不了，
     由云端把订单补推出去。去重靠仓库里的两个状态文件：
        state/local_state.json  本机推完回写
        state/cloud_state.json  本函数推完回写
     再叠一层"下单满 15 分钟才动手"的余量，保证本地在线时零撞车。

  B. **日报兜底**（`--report`）：还没配状态同步时（`state/local_state.json`
     不存在），无法可靠去重，就只发一条当日汇总，绝不逐单推 —— 宁可少推，
     也不能重复推。

选 A 还是 B 是自动判断的：只要本机成功回写过一次状态，云端就进入接管模式。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL_STATE = ROOT / "state" / "local_state.json"
RECONCILE = ROOT / "local" / "reconcile.py"

if LOCAL_STATE.exists():
    mode = ["--cloud-fix", "--hours", "24"]
    print(f"[run.py] 检测到 {LOCAL_STATE.name}，进入「接管补推」模式")
else:
    mode = ["--report", "--hours", "24"]
    print(f"[run.py] 未检测到 {LOCAL_STATE.name}（本机尚未回写状态），"
          f"降级为「日报」模式 —— 无法可靠去重时绝不逐单推")

sys.exit(subprocess.call([sys.executable, str(RECONCILE), *mode]))
