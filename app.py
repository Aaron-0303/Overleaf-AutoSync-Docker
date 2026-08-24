from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from flask import Flask, jsonify, redirect, render_template_string, request, url_for


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
LOG = logging.getLogger("overleaf-autosync")

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config.yml"))


@dataclass
class ProjectState:
    project_id: str
    name: str
    status: str = "waiting"
    last_sync: str | None = None
    last_change: str | None = None
    last_error: str | None = None
    local_path: str | None = None


class ConfigError(RuntimeError):
    pass


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"Config file not found: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if not cfg.get("overleaf", {}).get("base_url"):
        raise ConfigError("overleaf.base_url is required")
    if not cfg.get("projects"):
        raise ConfigError("At least one project is required in projects")

    cfg.setdefault("sync", {})
    cfg["sync"].setdefault("interval_seconds", 300)
    cfg["sync"].setdefault("destination", "/backup")
    cfg["sync"].setdefault("sync_on_start", True)
    cfg["sync"].setdefault("git", {})
    cfg["sync"]["git"].setdefault("enabled", True)
    cfg["sync"]["git"].setdefault("user_name", "Overleaf AutoSync")
    cfg["sync"]["git"].setdefault("user_email", "autosync@local")

    cfg["overleaf"].setdefault("verify_tls", True)
    cfg["overleaf"].setdefault("timeout", 60)
    return cfg


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Environment variable {name} is required")
    return value


class OverleafClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base_url = cfg["base_url"].rstrip("/") + "/"
        self.verify = bool(cfg.get("verify_tls", True))
        self.timeout = int(cfg.get("timeout", 60))
        self.email = env_required(cfg.get("email_env", "OVERLEAF_EMAIL"))
        self.password = env_required(cfg.get("password_env", "OVERLEAF_PASSWORD"))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Overleaf-AutoSync-Docker/1.0"})

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def login(self) -> None:
        login_url = self._url("/login")
        page = self.session.get(login_url, timeout=self.timeout, verify=self.verify)
        page.raise_for_status()

        soup = BeautifulSoup(page.text, "html.parser")
        csrf = None
        csrf_input = soup.find("input", attrs={"name": "_csrf"})
        if csrf_input:
            csrf = csrf_input.get("value")
        if not csrf:
            csrf_meta = soup.find("meta", attrs={"name": "ol-csrfToken"})
            if csrf_meta:
                csrf = csrf_meta.get("content")
        if not csrf:
            raise RuntimeError("Could not find Overleaf CSRF token on /login")

        response = self.session.post(
            login_url,
            data={"_csrf": csrf, "email": self.email, "password": self.password},
            headers={"Referer": login_url},
            timeout=self.timeout,
            verify=self.verify,
            allow_redirects=True,
        )
        response.raise_for_status()

        # Community Server redirects successful logins away from /login.
        # Some versions return JSON instead, so also accept an authenticated session cookie.
        still_login = response.url.rstrip("/").endswith("/login")
        has_session_cookie = any(
            "session" in cookie.name.lower() or "sharelatex" in cookie.name.lower()
            for cookie in self.session.cookies
        )
        if still_login and not has_session_cookie:
            text = response.text.lower()
            if "invalid" in text or "incorrect" in text or "login" in text:
                raise RuntimeError("Overleaf login failed; check email/password and base_url")

    def download_project_zip(self, project_id: str, destination: Path) -> None:
        url = self._url(f"/project/{project_id}/download/zip")
        with self.session.get(
            url,
            stream=True,
            timeout=self.timeout,
            verify=self.verify,
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type and response.url.rstrip("/").endswith("/login"):
                raise RuntimeError("Overleaf session expired or authentication failed")
            with destination.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

        if destination.stat().st_size == 0:
            raise RuntimeError(f"Downloaded empty ZIP for project {project_id}")


def safe_extract(zip_path: Path, output_dir: Path) -> None:
    output_root = output_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if output_root != target and output_root not in target.parents:
                raise RuntimeError(f"Unsafe path in ZIP: {member.filename}")
        archive.extractall(output_dir)


def same_file(a: Path, b: Path) -> bool:
    if not b.exists() or not b.is_file():
        return False
    if a.stat().st_size != b.stat().st_size:
        return False
    with a.open("rb") as fa, b.open("rb") as fb:
        while True:
            ca = fa.read(1024 * 1024)
            cb = fb.read(1024 * 1024)
            if ca != cb:
                return False
            if not ca:
                return True


def mirror_tree(source: Path, destination: Path) -> bool:
    """Mirror source into destination while preserving destination/.git."""
    destination.mkdir(parents=True, exist_ok=True)
    changed = False

    source_files = {
        p.relative_to(source)
        for p in source.rglob("*")
        if p.is_file()
    }
    source_dirs = {
        p.relative_to(source)
        for p in source.rglob("*")
        if p.is_dir()
    }

    for dst in sorted(destination.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = dst.relative_to(destination)
        if rel.parts and rel.parts[0] == ".git":
            continue
        if dst.is_file() and rel not in source_files:
            dst.unlink()
            changed = True
        elif dst.is_dir() and rel not in source_dirs:
            try:
                dst.rmdir()
                changed = True
            except OSError:
                pass

    for rel in sorted(source_dirs):
        (destination / rel).mkdir(parents=True, exist_ok=True)

    for rel in sorted(source_files):
        src = source / rel
        dst = destination / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not same_file(src, dst):
            shutil.copy2(src, dst)
            changed = True

    return changed


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def git_commit_if_needed(path: Path, git_cfg: dict[str, Any]) -> bool:
    if not git_cfg.get("enabled", True):
        return False
    if not (path / ".git").exists():
        run_git(["init"], path)
    run_git(["config", "user.name", str(git_cfg.get("user_name", "Overleaf AutoSync"))], path)
    run_git(["config", "user.email", str(git_cfg.get("user_email", "autosync@local"))], path)
    run_git(["add", "-A"], path)
    diff = run_git(["diff", "--cached", "--quiet"], path, check=False)
    if diff.returncode == 0:
        return False
    if diff.returncode != 1:
        raise RuntimeError(diff.stderr.strip() or "git diff failed")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    run_git(["commit", "-m", f"autosync: {stamp}"], path)
    return True


class SyncService:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.destination = Path(cfg["sync"]["destination"]).resolve()
        self.destination.mkdir(parents=True, exist_ok=True)
        self.states: dict[str, ProjectState] = {}
        self.locks: dict[str, threading.Lock] = {}
        for p in cfg["projects"]:
            project_id = str(p["id"]).strip()
            name = str(p.get("name") or project_id).strip()
            local_path = str(self.destination / name)
            self.states[project_id] = ProjectState(project_id, name, local_path=local_path)
            self.locks[project_id] = threading.Lock()

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(state) for state in self.states.values()]

    def sync_project(self, project_id: str) -> None:
        if project_id not in self.states:
            raise KeyError(project_id)
        lock = self.locks[project_id]
        if not lock.acquire(blocking=False):
            LOG.info("Project %s is already syncing", project_id)
            return

        state = self.states[project_id]
        state.status = "syncing"
        state.last_error = None
        try:
            client = OverleafClient(self.cfg["overleaf"])
            client.login()
            with tempfile.TemporaryDirectory(prefix="overleaf-autosync-") as tmp:
                tmp_path = Path(tmp)
                zip_path = tmp_path / "project.zip"
                extracted = tmp_path / "project"
                extracted.mkdir()
                client.download_project_zip(project_id, zip_path)
                safe_extract(zip_path, extracted)
                local_path = Path(state.local_path or self.destination / state.name)
                content_changed = mirror_tree(extracted, local_path)
                commit_created = git_commit_if_needed(local_path, self.cfg["sync"]["git"])

            now = datetime.now(timezone.utc).isoformat()
            state.last_sync = now
            if content_changed or commit_created:
                state.last_change = now
            state.status = "ok"
            LOG.info("Synced %s (%s), changed=%s", state.name, project_id, content_changed)
        except Exception as exc:  # noqa: BLE001
            state.status = "error"
            state.last_error = str(exc)
            LOG.exception("Sync failed for %s (%s)", state.name, project_id)
        finally:
            lock.release()

    def sync_all(self) -> None:
        for project_id in self.states:
            self.sync_project(project_id)

    def scheduler_loop(self) -> None:
        interval = max(30, int(self.cfg["sync"].get("interval_seconds", 300)))
        if bool(self.cfg["sync"].get("sync_on_start", True)):
            self.sync_all()
        while True:
            time.sleep(interval)
            self.sync_all()


PAGE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Overleaf AutoSync</title>
  <style>
    :root { color-scheme: light; --bg:#f5f7fb; --card:#fff; --text:#172033; --muted:#6b7280; --line:#e5e7eb; --ok:#15803d; --bad:#b91c1c; --sync:#1d4ed8; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    main { max-width:1100px; margin:0 auto; padding:40px 22px 70px; }
    header { display:flex; justify-content:space-between; align-items:end; gap:20px; margin-bottom:24px; }
    h1 { margin:0; font-size:30px; letter-spacing:-.03em; }
    .sub { color:var(--muted); margin-top:7px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:16px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:20px; box-shadow:0 5px 18px rgba(15,23,42,.04); }
    .row { display:flex; justify-content:space-between; gap:12px; align-items:center; }
    .name { font-weight:700; font-size:18px; word-break:break-word; }
    .badge { border-radius:999px; padding:5px 10px; font-size:12px; font-weight:700; background:#eef2ff; }
    .ok { color:var(--ok); background:#ecfdf3; } .error { color:var(--bad); background:#fef2f2; } .syncing { color:var(--sync); background:#eff6ff; }
    dl { display:grid; grid-template-columns:88px 1fr; gap:9px 12px; font-size:13px; margin:18px 0; }
    dt { color:var(--muted); } dd { margin:0; word-break:break-all; }
    .err { color:var(--bad); background:#fff7f7; border:1px solid #fee2e2; border-radius:10px; padding:10px; font-size:12px; margin-bottom:12px; }
    button { width:100%; border:0; border-radius:10px; padding:10px 14px; font-weight:700; cursor:pointer; background:#172033; color:white; }
    button:hover { opacity:.9; }
    code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
    @media (max-width:600px) { main{padding:24px 14px 50px} header{align-items:start; flex-direction:column} }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>Overleaf AutoSync</h1><div class="sub">单向论文源码备份 · 自动 Git 历史</div></div>
    <div class="sub">刷新页面查看最新状态</div>
  </header>
  <section class="grid">
  {% for p in projects %}
    <article class="card">
      <div class="row"><div class="name">{{ p.name }}</div><span class="badge {{ p.status }}">{{ p.status }}</span></div>
      <dl>
        <dt>Project ID</dt><dd><code>{{ p.project_id }}</code></dd>
        <dt>本地目录</dt><dd><code>{{ p.local_path }}</code></dd>
        <dt>上次同步</dt><dd>{{ p.last_sync or '尚未同步' }}</dd>
        <dt>上次变化</dt><dd>{{ p.last_change or '—' }}</dd>
      </dl>
      {% if p.last_error %}<div class="err">{{ p.last_error }}</div>{% endif %}
      <form method="post" action="{{ url_for('sync_one', project_id=p.project_id) }}"><button type="submit">立即同步</button></form>
    </article>
  {% endfor %}
  </section>
</main>
</body>
</html>
"""


cfg = load_config()
service = SyncService(cfg)
app = Flask(__name__)


@app.get("/")
def index():
    return render_template_string(PAGE, projects=service.snapshot())


@app.post("/sync/<project_id>")
def sync_one(project_id: str):
    if project_id not in service.states:
        return "unknown project", 404
    threading.Thread(target=service.sync_project, args=(project_id,), daemon=True).start()
    return redirect(url_for("index"))


@app.post("/api/sync")
def api_sync_all():
    threading.Thread(target=service.sync_all, daemon=True).start()
    return jsonify({"accepted": True}), 202


@app.get("/api/status")
def api_status():
    return jsonify(service.snapshot())


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "projects": len(service.states)})


def start_scheduler() -> None:
    thread = threading.Thread(target=service.scheduler_loop, name="autosync-scheduler", daemon=True)
    thread.start()


if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)
