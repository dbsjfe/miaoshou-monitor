# 妙手ERP → 微信 新订单提醒

GitHub Actions 每 5 分钟自动拉取妙手 ERP 最新订单，新订单通过 Server酱 推送到微信。

## 部署步骤

### 1. 创建 GitHub 仓库

在 GitHub 新建一个仓库（建议 **Private** 私有仓库，API 密钥不会泄露）。

### 2. 设置 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret，逐个添加：

| Name | Value |
|------|-------|
| `MIAOSHOU_APP_KEY` | `ak_96f186e5f28e40d0914b562568f9ea60` |
| `MIAOSHOU_APP_SECRET` | `e3fe998819bd4e15a24b7a3d915bc5c68477663cbbce4652908f54461cb52061` |
| `SERVERCHAN_SEND_KEY` | `SCT389742T1B6UL3R5apFywv2aWzUCTp6Z` |

### 3. 推送代码

```bash
cd miaoshou-actions
git init
git add .
git commit -m "init: 妙手ERP订单监控"
git branch -M main
git remote add origin https://github.com/你的用户名/仓库名.git
git push -u origin main
```

### 4. 验证

推送后 Actions 会自动开始每 5 分钟运行。也可以到仓库 → Actions → 手动触发 `workflow_dispatch` 立即测试。

## 费用

- **Public 仓库**: 完全免费，无限使用
- **Private 仓库**: 免费额度 2000 分钟/月，每5分钟跑一次约 8640 分钟/月，如果超了可以改成每10分钟

## 本地测试

```bash
export MIAOSHOU_APP_KEY="你的AppKey"
export MIAOSHOU_APP_SECRET="你的AppSecret"
export SERVERCHAN_SEND_KEY="你的SendKey"
pip install requests
python run.py
```
