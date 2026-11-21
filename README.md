# My Proxy Hub · 私人模块化反代面板

专为 **SillyTavern（酒馆）** 与 **Kelivo** 手机端自用设计的轻量级私人 API 反代与管理面板：
- 零浏览器 Cookie 抓包，密钥和日志全部只存在你自己的 VPS `./data/hub.db` 中。
- 插件式 `providers/` 架构：自带 **Google AI Studio 官方多 Key 轮询** + **万能 OpenAI `/v1` 兼容插槽**（可直接接管本机 `5678` 端口的 Kiro 反代、DeepSeek、NVIDIA NIM 等）。
- 单文件手机端网页界面（`static/index.html`），可以直接在手机 GitHub App 里边看边改 UI 配色与布局。
- 独立跑在空闲的 `3000` 端口，完全不碰 `8000` 端口的酒馆本体。

## 一、上传到 GitHub 私有仓库
1. 在 GitHub 新建一个 **Private（私有）** 仓库（例如 `my-proxy-hub`）。
2. 解压本压缩包，将里面的全部文件上传到该私有仓库根目录。

## 二、在 AWS VPS（Termius）首次部署
1. 拉取私有仓库（把下面链接换成你的仓库地址和 GitHub Token）：
```bash
git clone https://你的GitHub用户名:你的GitHub_PAT@github.com/你的GitHub用户名/my-proxy-hub.git ~/my-proxy-hub && cd ~/my-proxy-hub
```
2. 修改默认面板密码（可选，把 `你的强密码` 换成你想设的密码）：
```bash
sed -i 's/change_me_123456/你的强密码/g' docker-compose.yml
```
3. 启动容器（映射 `3000` 端口）：
```bash
docker compose up -d --build
```
4. 验证运行状态（看到 `{"status":"ok","providers":2}` 即成功）：
```bash
curl -s http://127.0.0.1:3000/health
```

> 记得在 AWS 安全组（Security Group）放行 `3000` 端口（或通过你现有的 Nginx / Cloudflare Tunnel 反代到 `127.0.0.1:3000`），然后用手机浏览器打开 `http://你的公网IP:3000` 即可登录面板！

## 三、以后在手机 GitHub App 改完 UI 或加了新插件，如何一键更新？
在 Termius 里执行这一行即可拉取最新代码并重启（你的 `./data` 数据库和密钥不会丢）：
```bash
cd ~/my-proxy-hub && git pull && docker compose up -d --build
```

## 四、如何添加你自己的新反代模块？
1. 在 GitHub 打开 `providers/_custom_template.py`，复制一份改名为 `providers/你的新模块名.py`（去掉开头的下划线 `_`）。
2. 修改里面的 `provider_type`、`label`、`fields` 和请求转发逻辑。
3. 回到 Termius 执行上面那行一键更新命令，网页「添加渠道」下拉框里就会自动出现你的新模块！
