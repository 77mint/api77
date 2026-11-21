# api77 · 私人模块化反代面板

轻量级自托管 API 反代与网页管理面板，对外提供标准 **OpenAI `/v1`** 兼容接口，**所有支持自定义 OpenAI 接口的前端通用**，特别适配：

- **酒馆（[SillyTavern](https://github.com/sillytavern/SillyTavern)）**
- **TT 酒馆（[TauriTavern](https://github.com/Darkatse/TauriTavern)）**
- **各类小手机**（HTML 网页前端，如免费机、开源机、自建小手机等）
- 以及 Kelivo 等其他支持自定义 `/v1` 接口的聊天客户端

## 核心特点

- **全前端通用，一处配置到处用**：对外统一输出标准 `/v1/models` 和 `/v1/chat/completions`（支持流式输出），切前端、换模型都不用反复改配置。
- **插件式反代架构（`providers/`）**：想接什么反代就加什么模块。内置 **Google AI Studio 官方多 Key 轮询**（免 Cookie）和 **万能 OpenAI `/v1` 兼容插槽**（可接管本机其他反代或任意第三方 `/v1` 接口），附带 `_custom_template.py` 插件模板。
- **手机端直看日志与报错**：单文件网页控制面板（`static/index.html`），手机浏览器直接开关渠道、测速连通、看 Token 统计与完整报错详情，不用翻终端日志，也方便直接在手机 GitHub 里边看边改 UI。
- **隐私与轻量**：不抓包、不上传明文 Cookie，所有 API Key 与日志只存在你自己 VPS 的本地 `./data/hub.db` 中；默认运行在 `3000` 端口，不干扰 `8000` 端口的酒馆本体。

## 前端接入方法（酒馆 / TT 酒馆 / 小手机通用）

部署启动后，用手机浏览器打开面板 `http://你的服务器IP:3000` 登录，在「概览」页一键复制下面两项填进前端的 **OpenAI 兼容 / 自定义反代** 设置即可：

- **API 地址（Base URL）**：`http://你的服务器IP:3000/v1`（若配置了域名或反代则填 `https://你的域名/v1`）
- **API 密钥（API Key）**：面板「概览」页显示的对外总密钥（以 `sk-hub-` 开头）

## AWS VPS 首次部署（Termius 单行复制）

1. 拉取仓库到独立目录（不碰酒馆目录）：
```bash
git clone https://github.com/77mint/api77.git ~/api77 && cd ~/api77
```
*(如果已将仓库设为 Private，请把链接换成 `https://77mint:你的GitHub_PAT@github.com/77mint/api77.git`)*

2. 修改首次启动的默认面板密码（把 `你的强密码` 换成你想设的密码）：
```bash
sed -i 's/change_me_123456/你的强密码/g' docker-compose.yml
```

3. 构建并后台启动（占用 `3000` 端口）：
```bash
docker compose up -d --build
```

4. 验证服务是否跑通（返回 `{"status":"ok","providers":2}` 即成功）：
```bash
curl -s http://127.0.0.1:3000/health
```

> 别忘了在 AWS 安全组放行 `3000` 端口（或用现有 Nginx / Cloudflare Tunnel 转发到 `127.0.0.1:3000`）。

## 手机改完 UI 或新增反代模块后，如何一键更新

在 GitHub 改完 `static/index.html` 或往 `providers/` 放进新插件后，到 Termius 执行这一行即可更新重启（本地 `./data` 中的密钥和日志不会丢失）：

```bash
cd ~/api77 && git pull && docker compose up -d --build
```

## 目录说明

- `main.py` / `db.py`：核心转发服务与本地 SQLite 日志/配置存储
- `static/index.html`：单文件网页管理面板（改配色、改布局直接改这个文件）
- `providers/`：反代插件目录（新增不带下划线开头的 `.py` 文件后重启即自动加载）
- `Dockerfile` / `docker-compose.yml`：容器化部署配置
