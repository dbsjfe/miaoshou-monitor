# 妙手ERP → 订单通知（邮件 + 微信）

两条链路互为兜底，**共用一份去重状态**，不会重复推送：

| 链路 | 位置 | 触发 | 作用 |
|------|------|------|------|
| **本机常驻版** | `local/` | 每 180 秒轮询 | 实时逐单通知（主通道） |
| **云端 Actions 版** | 仓库根目录 | 每 10 分钟 | **Mac 关机时接管**逐单通知 |

去重靠仓库里的两个状态文件（双方都读写）：

```
state/local_state.json   本机推完订单后通过 GitHub API 回写
state/cloud_state.json   云端推完订单后由 workflow 提交回仓库
```

加上「下单满 15 分钟云端才动手」的时间余量 → 本机在线时云端永不插话，
本机离线时云端接管，两边都不会重复。

推送通道按顺序自动降级：**邮件 → Server酱**。

> 私有说明（含通道选型踩坑、沙箱代理污染、单例陷阱等）见 [LOCAL.md](./LOCAL.md)。

## 部署步骤

### 1. 创建 GitHub 仓库

**建议 Private 私有仓库**——代码与文档任何人都能看，一旦把密钥写进 README / 代码就等于公开泄露。

⚠️ **但私有仓库有 Actions 计费限制**：GitHub 按「每个 job 向上取整到整分钟」计费
（官方原文：*GitHub rounds the minutes and partial minutes each job uses up to the nearest whole minute*），
所以一轮哪怕只跑 11 秒也算 1 分钟：

| cron 频率 | 每天轮数 | 折合计费分钟/月 | 私有仓库免费额度 2000 够吗 |
|---|---|---|---|
| `*/10`（当前） | 144 | ≈ 4300 | ❌ 超一倍 |
| `*/15` | 96 | ≈ 2900 | ❌ 仍超 |
| `*/30` | 48 | ≈ 1440 | ✅ 够 |

**Public 仓库的 Actions 完全免费无限**（标准 runner，不受分钟数限制）。

本项目**当前用 public**：代码里已经没有任何明文凭证，值全部走 Secrets。
如果你更在意隐私，把仓库转 Private 后请同步把 cron 改成 `*/30 * * * *`。

### 2. 设置 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret。

**不要把密钥写进任何提交的文件里**，只放 Secrets：

| Name | 说明 |
|------|------|
| `MIAOSHOU_APP_KEY` | 妙手 ERP 开放平台 AppKey |
| `MIAOSHOU_APP_SECRET` | 妙手 ERP 开放平台 AppSecret |
| `SMTP_HOST` | 如 `smtp.qq.com` |
| `SMTP_PORT` | 如 `465`（SSL） |
| `SMTP_USER` | 发件邮箱 |
| `SMTP_PASSWORD` | 邮箱 SMTP **授权码**（不是登录密码） |
| `SMTP_TO` | 收件邮箱 |
| `SERVERCHAN_SEND_KEY` | Server酱 SendKey（可选，兜底通道） |

批量写入可用 `local/set_gh_secrets.py`（读取本地 `config.json`，加密后写入）：

```bash
GH_TOKEN=<你的token> python local/set_gh_secrets.py dbsjfe/miaoshou-monitor
```

### 3. 推送代码

```bash
git init
git add .
git commit -m "init: 妙手ERP订单通知"
git branch -M main
git remote add origin https://github.com/你的用户名/仓库名.git
git push -u origin main
```

`config.json`、`orders_state.json`、`*.log` 已在 `.gitignore` 中，不会被提交。

### 4. 验证

仓库 → Actions → 选 `妙手ERP订单通知（云端接管）` → **Run workflow**。

**推荐先勾「只跑链路自检」**——它会发一条测试通知就结束（不查订单、不写状态），
用来确认 Secrets 配对了、消息真的送得出去。推送失败是静默的，值得先验一次。

不勾则跑正式流程，日志开头会打印当前模式：

- `进入「接管补推」模式` —— 本机已成功回写过状态，云端会补推漏单
- `降级为「日报」模式` —— 本机还没回写（token 未配），云端只发一条当日汇总，**不会重复推**

## 费用

按 GitHub 官方计费规则（每个 job 向上取整到整分钟）：

- **Public 仓库**：标准 runner 免费无限 —— 本项目当前的选择
- **Private 仓库**：免费额度 2000 分钟/月。`*/10` 频率约 4300 分钟/月会超，
  需把 cron 降到 `*/30`（≈1440 分钟/月）

## 本地测试

```bash
export MIAOSHOU_APP_KEY="你的AppKey"
export MIAOSHOU_APP_SECRET="你的AppSecret"
export SMTP_USER="你的邮箱"
export SMTP_PASSWORD="你的SMTP授权码"
export SMTP_TO="收件邮箱"
pip install requests
python run.py
```

手动跑一次对账（只打印不推送）：

```bash
python local/reconcile.py --cloud-fix --hours 24 --min-age-minutes 100000
```

## ⚠️ 安全须知

密钥一旦被提交进 Git，**即使后续删除也仍留在历史记录里**。如果曾经误提交：

1. 先去平台**重置密钥**（妙手后台重置 AppSecret、Server酱重置 SendKey）——这一步最关键
2. 再把仓库转为 Private / 清理文件中的明文
3. 之后一律只通过 Secrets 注入
