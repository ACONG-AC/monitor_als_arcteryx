# ALS Arc'teryx 监控（价格 / 上新 / 库存）

监控 https://www.als.com/arc-teryx 集合页下的始祖鸟商品，发现变化后通过 **Discord** 发送通知，并将快照存入 `snapshot.json`（CI 自动提交）。

## 使用步骤

1. Fork 或新建仓库并加入下列文件。
2. 在仓库 **Settings → Secrets → Actions** 新建：
   - `DISCORD_WEBHOOK_URL`：你的 Discord Webhook。
3. 手动运行一次 *Actions → Monitor ALS Arc'teryx → Run workflow*，或等待定时任务（默认每 30 分钟）。
4. 运行成功后：
   - 你会收到 Discord 通知（如有变化）。
   - 仓库会生成或更新 `snapshot.json`。

> 若需本地跑：
```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python monitor_als_arcteryx.py
