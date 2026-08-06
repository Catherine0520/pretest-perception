# 云端部署指南

部署后获得一个公网URL，任何人打开浏览器就能参与预测试。

## 第1步：推送到 GitHub（5分钟）

1. 打开 https://github.com/new
2. Repository name 填 `pretest-perception`（随便填）
3. **不要**勾选 "Add a README file"（我们已有代码）
4. 点 "Create repository"
5. 在终端运行以下命令（把 `YOUR_USERNAME` 换成你的 GitHub 用户名）：

```bash
cd /Users/catherine/Desktop/风险管理/deploy/pretest

git remote add origin https://github.com/YOUR_USERNAME/pretest-perception.git
git branch -M main
git push -u origin main
```

## 第2步：部署到 Render（3分钟）

1. 打开 https://render.com
2. 点 "Get Started" → 用 GitHub 账号登录
3. 点 "New +" → "Web Service"
4. 搜索并选择 `pretest-perception` 仓库
5. 配置：
   - **Name**: `pretest-perception`（随意）
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python server.py`
   - **Free Instance Type**: 选 Free
6. 点 "Create Web Service"

等待 2-3 分钟部署完成。Render 会给你一个 URL 如：
```
https://pretest-perception.onrender.com
```

## 第3步：测试

浏览器打开那个 URL，走一遍完整流程确保正常。

## 发给参与者

把 URL 发给参与者。他们用手机或电脑浏览器打开即可。

## 获取数据

部署后所有评分数据保存在 Render 服务器上。预测试结束后告诉我，我帮你把数据导出来。或者你可以在 Render 的 "Shell" 标签中运行：

```bash
cat data/pretest_*.csv
```

## 注意事项

- **免费额度**：Render 免费版每月 750 小时，够用一整月
- **冷启动**：免费版 15 分钟无访问会休眠，下次打开需等 30-60 秒唤醒
- **数据持久性**：免费版数据在重新部署时会丢失，建议每人完成后即时保存
- **停止**：预测试完成后在 Render 后台点 "Suspend" 停止服务
