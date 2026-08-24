# Overleaf AutoSync Docker

面向 **自建 Overleaf Community Server / Overleaf Toolkit** 的论文自动备份工具。

只需要提供 Overleaf 地址、邮箱和密码，AutoSync 会自动登录并发现该账号可访问的全部项目。你在 Web 页面中勾选哪些项目需要备份即可，不需要手填 Project ID。

## 当前功能

- 自动登录自建 Overleaf
- 自动发现账号可访问的全部项目
- Web 页面选择哪些项目需要备份
- 默认每 5 分钟检查一次项目变化
- **远端没有变化时只检查，不下载、不创建 Git commit**
- 远端 `lastUpdated` 变化时才下载项目 ZIP 做实际文件比对
- 源码实际发生变化时才创建一个新的本地 Git 版本
- 每个项目独立维护 `.git` 历史，不会 push 到 GitHub
- 项目详情页查看所有备份版本
- 每个版本显示修改了哪些文件、增加/删除多少行
- 点击文件可查看该版本的具体 diff
- 自动处理新增、修改和删除文件
- 取消备份不会删除已有本地文件和历史版本
- 项目重名时自动增加 Project ID 短后缀避免覆盖
- 国内构建默认使用阿里云 Debian 镜像和清华 PyPI

## 备份机制

```text
定时检查 Overleaf
        ↓
获取项目 lastUpdated
        ↓
与上次成功同步时间戳比较
        ↓
┌──────────────────────────────┐
│ 没变化 + 本地 Git 干净      │
│ → 跳过下载，不产生新版本    │
└──────────────────────────────┘
        ↓ 有变化 / 本地异常
下载项目 ZIP 到临时目录
        ↓
安全解压并镜像到本地项目目录
        ↓
Git 检查实际文件变化
        ↓
┌──────────────────────────────┐
│ 实际源码没变                 │
│ → 不 commit                  │
├──────────────────────────────┤
│ 实际源码有变化               │
│ → git add -A + git commit    │
└──────────────────────────────┘
```

所以即使设置为每 5 分钟检查，也不会每 5 分钟生成一个空版本。

## 本地目录

例如：

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

Git 只用于本地版本历史，不配置 remote，也不会自动上传任何论文内容。

## Web 页面

默认地址：

```text
http://<服务器IP>:30389/
```

例如：

```text
http://10.157.197.46:30389/
```

首页显示：

- 项目名称、Project ID、权限
- 是否启用自动备份
- 当前状态：`checking / syncing / ok / error / off`
- 历史版本数量
- 最近检查时间
- 最近同步时间
- 最近实际变化时间
- 最新 Git commit

点击项目进入详情页，可以看到按时间排列的备份版本：

```text
2026-08-24 11:20
commit 8ca21f4
3 个文件发生变化
+42 / -11

M sections/method.tex
A figures/framework.pdf
D figures/old.png
```

再点击某个版本，可以查看全部变化文件；点击具体文件可以查看文本 diff。

## 快速部署

```bash
git clone https://github.com/Aaron-0303/Overleaf-AutoSync-Docker.git
cd Overleaf-AutoSync-Docker
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

默认 `config.yml`：

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
  history_limit: 50
```

启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker logs -f overleaf-autosync
```

## 国内构建源

默认：

```env
DEBIAN_MIRROR=https://mirrors.aliyun.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
PYTHON_IMAGE=python:3.12-slim
```

这些都可以在 `.env` 中覆盖。

## 备份选择状态

你在 Web 中勾选哪些项目，会保存到：

```text
/state/selection.json
```

Docker 默认映射为宿主机：

```text
./state/selection.json
```

容器重启或重新 build 不会丢失选择状态。

## 关于 Git

这里的 Git 不是为了把论文同步到 GitHub，而是作为本地版本数据库：

- 只有内容实际变化时才 commit
- 可以查看每次修改了哪些文件
- 可以查看文本 diff
- 可以恢复误删或错误修改
- 每个项目完全独立

如果从曾经的“无 Git 版本”升级，第一次检测到项目后会在现有项目目录中初始化 `.git`，并以当前 Overleaf 内容创建第一个基线版本。以前已经存在的 `.git` 会继续沿用。

## API

```text
GET  /api/projects
POST /api/refresh
POST /api/selection
POST /api/sync
GET  /api/project/<project_id>/versions
GET  /healthz
```

## 注意

- 这是 **Overleaf → 本地** 的单向备份，本地修改不会推回 Overleaf。
- 如果手动修改备份目录，下次检查会发现本地仓库不干净并重新从 Overleaf 校正。
- Web 面板默认没有单独登录认证，建议只在可信内网使用。
- 本工具不能替代完整的 Overleaf 灾难恢复备份；MongoDB、`/var/lib/overleaf` 和 Redis 仍建议单独备份。

## License

MIT
