# Overleaf AutoSync Docker

一个面向 **自建 Overleaf Community Server / Overleaf Toolkit** 的 Docker 自动同步工具。

只需要提供 Overleaf 地址、邮箱和密码，AutoSync 会自动登录并发现该账号可访问的全部项目。随后在 Web 面板中勾选需要备份的论文即可，无需手动查找或填写 Project ID。

> 设计目标：**Overleaf → 本地单向备份**。不会把本地修改自动推回 Overleaf，因此不会产生双向同步冲突。

## 功能

- 自动登录自建 Overleaf
- **自动发现账号可访问的全部项目**
- Web 面板展示项目名称、权限、更新时间、归档/回收站状态
- 在 Web 中勾选“是否备份”，不需要手填 Project ID
- 备份选择持久化，容器重启/重建后仍保留
- 默认每 5 分钟重新发现项目并同步已启用项目
- Overleaf 项目 ZIP → 普通 `.tex/.bib/figures` 目录
- 每个项目自动维护独立 Git 仓库
- 只有内容发生变化时才创建 Git commit
- Overleaf 删除文件后，本地镜像同步删除，但旧内容仍可从 Git 历史恢复
- “刷新项目”“立即同步”“保存备份选择”操作
- `/api/projects`、`/api/status`、`/api/selection`、`/api/sync`、`/api/refresh`
- Docker Healthcheck
- 登录密码只从环境变量读取
- ZIP 路径穿越保护

## 工作方式

```text
Overleaf 账号
    │
    ├─ 登录 /login
    │
    ├─ 自动读取 /api/project
    │        ↓
    │   获取全部项目
    │        ↓
    │   Web 面板勾选
    │        ↓
    └─ 已启用项目
             ↓
      下载项目 ZIP
             ↓
      安全解压 / 镜像
             ↓
      /backup/<项目名>
             ↓
         Git commit
```

## 本地目录效果

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

每个项目都是普通文件夹，可以直接用 VS Code 等编辑器打开，也可以通过 `git log` 找回历史版本。

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
STATE_DIR=./state
CONFIG_FILE=./config.yml
WEB_PORT=30389
TZ=Asia/Shanghai
LOG_LEVEL=INFO
```

`.env` 已被 `.gitignore` 忽略。不要把真实密码提交到 GitHub。

### 3. 配置 Overleaf 地址

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
  state_path: "/state/selection.json"
  sync_on_start: true
  git:
    enabled: true
    user_name: "Overleaf AutoSync"
    user_email: "autosync@local"
```

**这里不需要任何 Project ID。**

### 4. 启动

```bash
docker compose up -d --build
```

查看：

```bash
docker compose ps
docker logs -f overleaf-autosync
```

### 5. 在 Web 中选择论文

浏览器访问：

```text
http://<服务器IP>:30389/
```

例如：

```text
http://10.157.197.46:30389/
```

程序会自动显示账号中的项目列表。

在页面中：

1. 勾选需要备份的项目；
2. 点击 **保存备份选择**；
3. 新启用项目会立即开始第一次同步；
4. 后续按照 `interval_seconds` 周期自动同步。

页面还支持：

- 搜索项目名称 / Project ID
- 全选当前可见项目
- 取消当前可见项目
- 刷新 Overleaf 项目列表
- 查看同步状态和错误
- 对单个已启用项目立即同步

## 项目发现

AutoSync 使用登录后的 Overleaf 项目列表接口获取当前账号可以访问的项目，因此可以发现：

- 自己拥有的项目
- 被邀请协作的项目
- 只读项目
- 已归档项目
- 回收站项目

Web 面板会显示相应状态，由你决定是否备份。

如果在 Overleaf 中新建了一篇论文，不需要修改配置。下一个同步周期会自动发现，也可以直接点击 Web 中的 **刷新项目**。

## 备份选择如何保存

Web 中的选择不会写入 `config.yml`，而是保存在：

```text
/state/selection.json
```

Docker Compose 默认映射为：

```text
./state/selection.json
```

可通过 `.env` 修改：

```env
STATE_DIR=/data-12/M2023-WX/OverleafAutoSyncState
```

取消某个项目的备份只会停止后续自动同步，**不会删除已经存在的本地项目目录，也不会删除 `.git` 历史**。

## 为什么使用 `host.docker.internal`

如果 Overleaf 和 AutoSync 在同一台 Linux 服务器，Overleaf 的 `30388` 通常是宿主机暴露端口。

`docker-compose.yml` 已配置：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

因此 AutoSync 可以直接使用：

```text
http://host.docker.internal:30388
```

如果 Overleaf 在另一台服务器，把 `base_url` 改成实际地址即可。

## 自动同步逻辑

每个周期执行：

```text
登录 Overleaf
    ↓
刷新全部项目
    ↓
读取 Web 保存的备份选择
    ↓
逐个下载已启用项目
    ↓
临时目录安全解压
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

项目下载和解压先在临时目录完成，不会把半个 ZIP 直接写进最终论文目录。

## 项目重名

如果两个 Overleaf 项目名称相同，AutoSync 会自动给后选择的项目增加 Project ID 前缀片段，例如：

```text
Paper/
Paper__67ab12cd/
```

Web 选择状态会记住项目对应的本地目录，因此以后取消再重新启用，也会继续使用原目录。

## Git 历史

进入任意项目：

```bash
cd /data-12/M2023-WX/OverleafSync/MAGIC-SLAM
git log --oneline --decorate
```

查看旧版本：

```bash
git show <commit>:main.tex
```

恢复误删文件：

```bash
git restore --source <commit> -- path/to/file.tex
```

注意：同步目录是 **Overleaf 的本地镜像目标**。直接修改其中内容可能在下一次同步时被 Overleaf 版本覆盖。如果需要长期本地编辑，建议对该目录另外 `git clone` 一份，或者先在 Web 中关闭该项目的自动备份。

## Web / API

### 获取全部项目和备份状态

```bash
curl http://127.0.0.1:30389/api/projects
```

### 刷新项目列表

```bash
curl -X POST http://127.0.0.1:30389/api/refresh
```

### 设置需要备份的项目

```bash
curl -X POST http://127.0.0.1:30389/api/selection \
  -H 'Content-Type: application/json' \
  -d '{"project_ids":["PROJECT_ID_1","PROJECT_ID_2"]}'
```

### 立即同步全部已启用项目

```bash
curl -X POST http://127.0.0.1:30389/api/sync
```

### 健康检查

```bash
curl http://127.0.0.1:30389/healthz
```

## 从 v1 升级

早期版本要求：

```yaml
projects:
  - id: "..."
    name: "..."
```

新版本不再需要该部分。

为了兼容已有部署，如果旧 `config.yml` 中仍有 `projects:`，这些项目会被当作初始启用项目导入到新的 `selection.json`。确认 Web 面板正常后即可删除旧配置中的 `projects:`。

## 与灾难恢复备份的关系

本工具备份的是 **可直接阅读和恢复的论文源码**，不能替代完整 Overleaf 服务器备份。

建议同时保留：

```text
Overleaf backups/
├── projects/      # AutoSync：普通 TeX + Git 历史
└── disaster/      # MongoDB + /var/lib/overleaf + Redis
```

## 安全建议

- 不要提交 `.env`
- 不要把密码写进 `config.yml`
- Web 面板默认没有登录认证，建议仅在可信内网使用
- 如果需要暴露公网，请在前面增加带认证的 HTTPS 反向代理
- 自签名 HTTPS 可设置 `verify_tls: false`；正式环境建议开启证书校验
- 同步目录应放在空间充足、可靠的磁盘上
- `state` 目录不含 Overleaf 密码，但包含项目 ID 和本地目录映射

## 当前限制

- 单向 Overleaf → 本地，不执行本地 → Overleaf 推送
- 认证依赖 Community Server 标准登录流程
- Web 面板自身目前不带账号认证
- 不会自动删除已取消备份的本地论文目录，这是为了避免误删数据

## License

MIT
