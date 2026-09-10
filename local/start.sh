#!/bin/bash
# 妙手ERP订单监控 —— 本机常驻启动脚本
# 三层保护：单例（防重复推送） + caffeinate（防系统睡眠） + 自愈循环（防静默死亡）

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 注意：必须用 venv 里的解释器。受管 python 3.13.12 本体缺 certifi，
# 会导致 OSError: Could not find a suitable TLS CA certificate bundle，所有 HTTPS 请求失败。
PYTHON="/Users/jianguo/.workbuddy/binaries/python/envs/default/bin/python"

cd "$SCRIPT_DIR"

# 0) 剥离「指向本机回环」的代理变量（重要，2026-09-10 踩坑）
#    WorkBuddy/AI 会话会给子进程注入 HTTP_PROXY=http://127.0.0.1:<随机端口> 的沙箱代理。
#    常驻进程若继承它，会话一结束端口就关闭，之后**所有 HTTPS 请求全部失败**
#    （ProxyError: Unable to connect to proxy ... Connection refused），
#    而进程仍在跑、日志仍在滚 —— 典型的"静默失效"，和 certifi 那次症状相同。
#    只清回环代理，不动用户真实的公司/家庭代理配置。
for _v in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
    eval "_val=\${$_v:-}"
    case "$_val" in
        *127.0.0.1*|*localhost*|*::1*) unset "$_v"; echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] 已剥离回环代理 $_v=$_val" >> monitor.log ;;
    esac
done
export NO_PROXY="*"
export no_proxy="*"

# 1) 单例保护：已有实例则直接退出
# （重要：本地与云端曾各跑一份，是"重复通知"的主要来源）
# 用 pidfile 而非 pgrep：pgrep -f 会把"调用者的命令行"也算进去，导致误判为已在运行
PID_FILE="$SCRIPT_DIR/monitor.pid"
OLD_PID=""
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') 监控已在运行(PID $OLD_PID)，跳过本次启动" >> monitor.log
        exit 0
    fi
fi

# 1b) 清理孤儿实例：pidfile 里没记录、但进程还活着的 monitor.py
#     （场景：手动 kill 时旧循环抢先把 python 拉起来，造成两份并存 → 重复推送）
#     用字符类 [.] 包裹，避免 pgrep 匹配到本脚本自己的命令行
for _p in $(pgrep -f "envs/default/bin/python monitor[.]py" 2>/dev/null); do
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

# 3) 自愈循环：monitor.py 异常退出后 10 秒自动重启。
#    历史教训：进程曾"静默死亡"（日志停更 19 分钟无人察觉），本循环兜住这类情况。
#    stdout 丢到 /dev/null：monitor.py 已用 FileHandler 写 monitor.log，
#    若再把 stdout 重定向到同一文件，会导致每行日志重复两次。
while true; do
    "$PYTHON" monitor.py > /dev/null 2>> monitor.log &
    MON_PID=$!
    echo "$MON_PID" > "$PID_FILE"
    wait "$MON_PID"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] monitor.py(PID $MON_PID) 已退出，10 秒后自动重启" >> monitor.log
    sleep 10
done
