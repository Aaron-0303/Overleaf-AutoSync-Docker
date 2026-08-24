# Overleaf AutoSync Docker

一个面向 **自建 Overleaf Community Server / Overleaf Toolkit** 的 Docker 自动同步工具。它定时登录 Overleaf，将指定项目以 ZIP 源码形式下载到本地目录，并为每个项目自动维护独立 Git 历史。

> 设计目标：**Overleaf → 本地单向备份**。本项目不会把本地修改自动推回 Overleaf，因此不会产生双向同步冲突。

## 功能

- 多项目自动同步
- 默认每 5 分钟执行一次，可配置
- Overleaf 项目 ZIP → 普通 `.tex/.bib/figures` 目录
- 每个项目自动初始化独立 Git 仓库
- 只有内容变化时才创建 Git commit
- 删除 Overleaf 中已删除的文件，同时保留本地 `.git` 历史
- Web 状态面板与“立即同步”按钮
- `/api/status` 状态接口、`/api/sync` 手动触发接口
- Docker Healthcheck
- 登录密码仅通过环境变量传入，不写入项目配置
- ZIP 路径穿越保护与临时目录下载

## 目录效果

假设配置了两个项目：

```text
/data-12/M2023-WX/OverleafSync/
├── MAGIC-SLAM/
│   ├── .git/
│   ├── main.tex
│   ├── sections/
│   ├── figures/
│   └── refs.bib
└── ActiveSplat/
    ├── .git/
    └── ...
```

这份目录可以直接使用编辑器打开，也可以通过 `git log` 找回历史版本。

## 快速部署

### 1. 克隆

```bash
git clone https://github.com/Aaron-0303/Overleaf-AutoSync-Docker.git
cd Overleaf-AutoSync-Docker
```

### 2. 创建配置

```bash
cp .env.example .env
cp config.example.yml config.yml
```

编辑 `.env`：

```env
OVERLEAF_EMAIL=你的Overleaf邮箱
OVERLEAF_PASSWORD=你的Overleaf密码
SYNC_DIR=/data-12/M2023-WX/OverleafSync
CONFIG_FILE=./config.yml
WEB_PORT=30389
```

`.env` 已被 `.gitignore` 忽略，不要把真实密码提交到 GitHub。

### 3. 填项目 ID

打开一个 Overleaf 项目，例如：

```text
http://10.157.197.46:30388/project/67abcdef1234567890abcdef
```

其中：

```text
67abcdef1234567890abcdef
```

就是 Project ID。

编辑 `config.yml`：

```yaml
overleaf:
  base_url: "http://host.docker.internal:30388"
  email_env: "OVERLEAF_EMAIL"
  password_env: "OVERLEAF_PASSWORD"
  verify_tls: false
  timeout: 60

sync:
  interval_seconds: 300
  destination: "/backup"
  sync_on_start: true
  git:
    enabled: true
    user_name: "Overleaf AutoSync"
    user_email: "autosync@local"

projects:
  - id: "67abcdef1234567890abcdef"
    name: "MAGIC-SLAM"
  - id: "68abcdef1234567890abcdef"
    name: "ActiveSplat"
```

`name` 会作为本地文件夹名称，建议每个项目使用唯一名称。

### 4. 启动

```bash
docker compose up -d --build
```

查看：

```bash
docker compose ps
docker logs -f overleaf-autosync
```

Web 面板默认：

```text
http://<服务器IP>:30389/
```

在你的部署环境中可对应：

```text
http://10.157.197.46:30389/
```

## 为什么使用 `host.docker.internal`

如果 Overleaf 和 AutoSync 都运行在同一台 Linux 服务器，Overleaf 的 `30388` 是宿主机暴露端口。`docker-compose.yml` 中配置了：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

所以 AutoSync 容器可以通过：

```text
http://host.docker.internal:30388
```

访问宿主机的 Overleaf，而无需把 AutoSync 加入 Overleaf 内部 Docker network。

如果 Overleaf 在另一台服务器，直接把 `base_url` 改成对应地址即可。

## 同步逻辑

一次同步过程：

```text
登录 Overleaf
    ↓
/project/<id>/download/zip
    ↓
下载到临时目录
    ↓
安全解压
    ↓
镜像到 /backup/<project-name>
    ↓
保留 .git
    ↓
git add -A
    ↓
有变化 → git commit
无变化 → 不创建 commit
```

项目更新采用临时下载目录，不会把半个 ZIP 直接写到最终项目目录。

## Git 历史

进入任意项目目录：

```bash
cd /data-12/M2023-WX/OverleafSync/MAGIC-SLAM
git log --oneline --decorate
```

查看某个旧版本：

```bash
git show <commit>:main.tex
```

恢复误删文件：

```bash
git restore --source <commit> -- path/to/file.tex
```

注意：本地目录是 AutoSync 的**镜像目标**。直接在这里修改文件可能在下次同步时被 Overleaf 版本覆盖。如果需要本地编辑，建议先建立额外 clone 或关闭对应项目的自动同步。

## Web/API

### 状态

```bash
curl http://127.0.0.1:30389/api/status
```

### 立即同步全部项目

```bash
curl -X POST http://127.0.0.1:30389/api/sync
```

### 健康检查

```bash
curl http://127.0.0.1:30389/healthz
```

## 与灾难恢复备份的关系

本工具备份的是**可直接阅读和恢复的论文源码**，不能替代完整 Overleaf 服务器备份。

建议同时保留两层：

```text
Overleaf backups/
├── projects/      # 本项目生成，普通 TeX + Git 历史
└── disaster/      # MongoDB + /var/lib/overleaf + Redis 的服务器级备份
```

其中 MongoDB 与 `/var/lib/overleaf` 的服务器级备份仍然应该定期执行。

## 安全建议

- 不要提交 `.env`
- 不要把真实密码写进 `config.yml`
- 如果 AutoSync Web 面板需要暴露到公网，请在前面增加认证反向代理；当前面板默认没有登录认证
- HTTPS 自建证书环境可设置 `verify_tls: false`，公网正式环境建议保持 `true`
- 同步目录应放在空间充足、可靠的磁盘上

## 当前限制

- v1 需要手动填写 Project ID，不自动枚举账号下全部项目
- 单向 Overleaf → 本地，不执行本地 → Overleaf 推送
- 认证依赖 Community Server 标准 `/login` 页面和项目 ZIP 下载接口；如果你的 Overleaf 版本定制了认证流程，需要相应调整登录逻辑
- Web 面板目前用于内网管理，不带账户认证

## License

MIT
