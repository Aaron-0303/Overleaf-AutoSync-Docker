from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
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
    local_name: str
    status: str = "waiting"
    last_sync: str | None = None
    last_change: str | None = None
    last_error: str | None = None


class ConfigError(RuntimeError):
    pass


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"Config file not found: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if not cfg.get("overleaf", {}).get("base_url"):
        raise ConfigError("overleaf.base_url is required")

    cfg.setdefault("sync", {})
    cfg["sync"].setdefault("interval_seconds", 300)
    cfg["sync"].setdefault("destination", "/backup")
    cfg["sync"].setdefault("state_path", "/state/selection.json")
    cfg["sync"].setdefault("sync_on_start", True)

    cfg["overleaf"].setdefault("verify_tls", True)
    cfg["overleaf"].setdefault("timeout", 60)
    return cfg


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Environment variable {name} is required")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_local_name(name: str, project_id: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or cleaned in {".", ".."}:
        cleaned = f"project-{project_id[:8]}"
    return cleaned[:120]


class OverleafClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base_url = cfg["base_url"].rstrip("/") + "/"
        self.verify = bool(cfg.get("verify_tls", True))
        self.timeout = int(cfg.get("timeout", 60))
        self.email = env_required(cfg.get("email_env", "OVERLEAF_EMAIL"))
        self.password = env_required(cfg.get("password_env", "OVERLEAF_PASSWORD"))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Overleaf-AutoSync-Docker/3.0"})
        self.csrf_token: str | None = None

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    @staticmethod
    def _extract_csrf(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")

        csrf_input = soup.find("input", attrs={"name": "_csrf"})
        if csrf_input and csrf_input.get("value"):
            return str(csrf_input.get("value"))

        csrf_meta = soup.find("meta", attrs={"name": "ol-csrfToken"})
        if csrf_meta and csrf_meta.get("content"):
            return str(csrf_meta.get("content"))

        return None

    def _load_project_page(self) -> str:
        project_url = self._url("/project")
        response = self.session.get(
            project_url,
            timeout=self.timeout,
            verify=self.verify,
            allow_redirects=True,
        )
        response.raise_for_status()

        if response.url.rstrip("/").endswith("/login"):
            raise RuntimeError("Overleaf login failed; check email/password and base_url")

        token = self._extract_csrf(response.text)
        if not token:
            raise RuntimeError("Could not find Overleaf CSRF token on /project")

        self.csrf_token = token
        return response.text

    def login(self) -> None:
        login_url = self._url("/login")
        page = self.session.get(login_url, timeout=self.timeout, verify=self.verify)
        page.raise_for_status()

        csrf = self._extract_csrf(page.text)
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
        self._load_project_page()

    def list_projects(self) -> list[dict[str, Any]]:
        if not self.csrf_token:
            self._load_project_page()

        api_url = self._url("/api/project")
        body = {"sort": {"by": "lastUpdated", "order": "desc"}}
        headers = {
            "X-Csrf-Token": str(self.csrf_token),
            "Accept": "application/json",
            "Referer": self._url("/project"),
        }

        response = self.session.post(
            api_url,
            json=body,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify,
        )

        if response.status_code == 403:
            self._load_project_page()
            headers["X-Csrf-Token"] = str(self.csrf_token)
            response = self.session.post(
                api_url,
                json=body,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify,
            )

        response.raise_for_status()
        payload = response.json()
        projects = payload.get("projects")
        if not isinstance(projects, list):
            raise RuntimeError("Unexpected response from Overleaf /api/project")

        result: list[dict[str, Any]] = []
        for project in projects:
            project_id = str(project.get("id") or project.get("_id") or "").strip()
            if not project_id:
                continue

            result.append(
                {
                    "id": project_id,
                    "name": str(project.get("name") or project_id),
                    "lastUpdated": project.get("lastUpdated"),
                    "accessLevel": project.get("accessLevel"),
                    "archived": bool(project.get("archived", False)),
                    "trashed": bool(project.get("trashed", False)),
                }
            )

        return result

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
    """Mirror source into destination.

    Existing .git directories from older releases are intentionally preserved,
    but this application no longer creates, reads or updates Git repositories.
    """
    destination.mkdir(parents=True, exist_ok=True)
    changed = False

    source_files = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    source_dirs = {p.relative_to(source) for p in source.rglob("*") if p.is_dir()}

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


class SyncService:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.destination = Path(cfg["sync"]["destination"]).resolve()
        self.destination.mkdir(parents=True, exist_ok=True)

        self.state_path = Path(cfg["sync"]["state_path"]).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        self.meta_lock = threading.RLock()
        self.sync_all_lock = threading.Lock()
        self.catalog: dict[str, dict[str, Any]] = {}
        self.catalog_order: list[str] = []
        self.registry: dict[str, dict[str, Any]] = self._load_registry()
        self.states: dict[str, ProjectState] = {}
        self.project_locks: dict[str, threading.Lock] = {}
        self.last_refresh: str | None = None
        self.discovery_error: str | None = None

        self._seed_legacy_projects()

        for project_id, entry in self.registry.items():
            if entry.get("enabled"):
                self._ensure_state(project_id, str(entry.get("name") or project_id))

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        if not self.state_path.exists():
            return {}

        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            projects = payload.get("projects", {})
            if isinstance(projects, dict):
                return {
                    str(k): dict(v)
                    for k, v in projects.items()
                    if isinstance(v, dict)
                }
        except Exception as exc:  # noqa: BLE001
            LOG.error("Failed to read selection state %s: %s", self.state_path, exc)

        return {}

    def _save_registry_locked(self) -> None:
        payload = {"version": 1, "projects": self.registry}
        fd, tmp_name = tempfile.mkstemp(
            prefix="selection-",
            suffix=".json",
            dir=self.state_path.parent,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")

            os.replace(tmp_name, self.state_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _seed_legacy_projects(self) -> None:
        legacy = self.cfg.get("projects") or []
        changed = False

        with self.meta_lock:
            for project in legacy:
                project_id = str(project.get("id") or "").strip()
                if not project_id or project_id == "REPLACE_WITH_PROJECT_ID":
                    continue
                if project_id in self.registry:
                    continue

                name = str(project.get("name") or project_id)
                self.registry[project_id] = {
                    "enabled": True,
                    "name": name,
                    "local_name": self._allocate_local_name_locked(name, project_id),
                }
                changed = True

            if changed:
                self._save_registry_locked()

    def _allocate_local_name_locked(self, name: str, project_id: str) -> str:
        base = safe_local_name(name, project_id)
        used = {
            str(entry.get("local_name"))
            for pid, entry in self.registry.items()
            if pid != project_id and entry.get("local_name")
        }

        candidate = base
        if candidate in used or (
            (self.destination / candidate).exists() and project_id not in self.registry
        ):
            candidate = f"{base}__{project_id[:8]}"

        serial = 2
        original = candidate
        while candidate in used:
            candidate = f"{original}-{serial}"
            serial += 1

        return candidate

    def _ensure_state(self, project_id: str, name: str) -> ProjectState:
        entry = self.registry[project_id]
        local_name = str(entry["local_name"])
        state = self.states.get(project_id)

        if state is None:
            state = ProjectState(
                project_id=project_id,
                name=name,
                local_name=local_name,
            )
            self.states[project_id] = state
            self.project_locks[project_id] = threading.Lock()
        else:
            state.name = name
            state.local_name = local_name

        return state

    def _apply_catalog(self, projects: list[dict[str, Any]]) -> None:
        changed_registry = False

        with self.meta_lock:
            self.catalog = {str(p["id"]): p for p in projects}
            self.catalog_order = [str(p["id"]) for p in projects]

            for project_id, entry in self.registry.items():
                project = self.catalog.get(project_id)
                if not project:
                    continue

                new_name = str(project["name"])
                if entry.get("name") != new_name:
                    entry["name"] = new_name
                    changed_registry = True

                if entry.get("enabled"):
                    self._ensure_state(project_id, new_name)

            if changed_registry:
                self._save_registry_locked()

            self.last_refresh = utc_now()
            self.discovery_error = None

    def selected_ids(self) -> list[str]:
        with self.meta_lock:
            return [
                pid
                for pid, entry in self.registry.items()
                if entry.get("enabled")
            ]

    def set_selection(self, project_ids: list[str]) -> None:
        requested = {str(pid) for pid in project_ids}

        with self.meta_lock:
            unknown = requested - set(self.catalog)
            if unknown:
                raise ValueError(
                    f"Unknown project id(s): {', '.join(sorted(unknown))}"
                )

            for project_id, entry in self.registry.items():
                entry["enabled"] = project_id in requested

            for project_id in requested:
                project = self.catalog[project_id]
                name = str(project["name"])
                entry = self.registry.get(project_id)

                if entry is None:
                    entry = {
                        "enabled": True,
                        "name": name,
                        "local_name": self._allocate_local_name_locked(
                            name,
                            project_id,
                        ),
                    }
                    self.registry[project_id] = entry
                else:
                    entry["enabled"] = True
                    entry["name"] = name
                    if not entry.get("local_name"):
                        entry["local_name"] = self._allocate_local_name_locked(
                            name,
                            project_id,
                        )

                self._ensure_state(project_id, name)

            self._save_registry_locked()

    def rows(self) -> list[dict[str, Any]]:
        with self.meta_lock:
            ids = list(self.catalog_order)
            ids.extend(pid for pid in self.registry if pid not in self.catalog)

            rows: list[dict[str, Any]] = []
            for project_id in ids:
                project = self.catalog.get(project_id)
                entry = self.registry.get(project_id, {})
                selected = bool(entry.get("enabled"))
                state = self.states.get(project_id)

                name = str(
                    (project or {}).get("name")
                    or entry.get("name")
                    or project_id
                )

                local_name = entry.get("local_name")
                status = state.status if state else ("off" if not selected else "waiting")

                if selected and project is None:
                    status = "unavailable"

                rows.append(
                    {
                        "project_id": project_id,
                        "name": name,
                        "selected": selected,
                        "status": status,
                        "access_level": (project or {}).get("accessLevel"),
                        "archived": bool((project or {}).get("archived", False)),
                        "trashed": bool((project or {}).get("trashed", False)),
                        "last_updated": (project or {}).get("lastUpdated"),
                        "last_sync": state.last_sync if state else None,
                        "last_change": state.last_change if state else None,
                        "last_error": state.last_error if state else None,
                        "local_path": (
                            str(self.destination / str(local_name))
                            if local_name
                            else None
                        ),
                    }
                )

            return rows

    def refresh_projects(self) -> None:
        try:
            client = OverleafClient(self.cfg["overleaf"])
            client.login()
            self._apply_catalog(client.list_projects())
            LOG.info("Discovered %d Overleaf projects", len(self.catalog))
        except Exception as exc:  # noqa: BLE001
            with self.meta_lock:
                self.discovery_error = str(exc)
            LOG.exception("Project discovery failed")
            raise

    def _sync_project_with_client(
        self,
        project_id: str,
        client: OverleafClient,
    ) -> None:
        with self.meta_lock:
            entry = self.registry.get(project_id)
            project = self.catalog.get(project_id)

            if not entry or not entry.get("enabled"):
                raise RuntimeError("Project is not enabled for backup")

            if not project:
                state = self._ensure_state(
                    project_id,
                    str(entry.get("name") or project_id),
                )
                state.status = "unavailable"
                state.last_error = (
                    "Project is no longer visible to this Overleaf account"
                )
                return

            state = self._ensure_state(project_id, str(project["name"]))
            lock = self.project_locks[project_id]

        if not lock.acquire(blocking=False):
            LOG.info("Project %s is already syncing", project_id)
            return

        state.status = "syncing"
        state.last_error = None

        try:
            with tempfile.TemporaryDirectory(prefix="overleaf-autosync-") as tmp:
                tmp_path = Path(tmp)
                zip_path = tmp_path / "project.zip"
                extracted = tmp_path / "project"
                extracted.mkdir()

                client.download_project_zip(project_id, zip_path)
                safe_extract(zip_path, extracted)

                local_path = self.destination / state.local_name
                content_changed = mirror_tree(extracted, local_path)

            now = utc_now()
            state.last_sync = now
            if content_changed:
                state.last_change = now
            state.status = "ok"

            LOG.info(
                "Synced %s (%s), changed=%s",
                state.name,
                project_id,
                content_changed,
            )
        except Exception as exc:  # noqa: BLE001
            state.status = "error"
            state.last_error = str(exc)
            LOG.exception("Sync failed for %s (%s)", state.name, project_id)
        finally:
            lock.release()

    def sync_project(self, project_id: str) -> None:
        try:
            client = OverleafClient(self.cfg["overleaf"])
            client.login()
            self._apply_catalog(client.list_projects())
            self._sync_project_with_client(project_id, client)
        except Exception as exc:  # noqa: BLE001
            with self.meta_lock:
                state = self.states.get(project_id)
                if state:
                    state.status = "error"
                    state.last_error = str(exc)
            LOG.exception("Manual sync failed for %s", project_id)

    def sync_all(self) -> None:
        if not self.sync_all_lock.acquire(blocking=False):
            LOG.info("A full sync is already running")
            return

        try:
            client = OverleafClient(self.cfg["overleaf"])
            client.login()
            self._apply_catalog(client.list_projects())

            for project_id in self.selected_ids():
                self._sync_project_with_client(project_id, client)
        except Exception as exc:  # noqa: BLE001
            with self.meta_lock:
                self.discovery_error = str(exc)
            LOG.exception("Full sync/project discovery failed")
        finally:
            self.sync_all_lock.release()

    def scheduler_loop(self) -> None:
        interval = max(30, int(self.cfg["sync"].get("interval_seconds", 300)))

        if bool(self.cfg["sync"].get("sync_on_start", True)):
            self.sync_all()
        else:
            try:
                self.refresh_projects()
            except Exception:
                pass

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
    :root { color-scheme:light; --bg:#f4f7fb; --card:#fff; --text:#172033; --muted:#667085; --line:#e4e7ec; --green:#067647; --green-bg:#ecfdf3; --red:#b42318; --red-bg:#fef3f2; --blue:#175cd3; --blue-bg:#eff8ff; --amber:#b54708; --amber-bg:#fffaeb; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    main { max-width:1280px; margin:0 auto; padding:34px 20px 70px; }
    h1 { margin:0; font-size:30px; letter-spacing:-.035em; }
    .sub { color:var(--muted); margin-top:7px; }
    .top { display:flex; justify-content:space-between; align-items:flex-end; gap:20px; margin-bottom:20px; }
    .stats { display:flex; gap:9px; flex-wrap:wrap; }
    .pill,.badge { border:1px solid var(--line); background:white; border-radius:999px; padding:6px 10px; font-size:12px; font-weight:700; white-space:nowrap; }
    .panel { background:var(--card); border:1px solid var(--line); border-radius:16px; box-shadow:0 6px 22px rgba(16,24,40,.045); overflow:hidden; }
    .toolbar { display:flex; gap:10px; align-items:center; padding:14px; border-bottom:1px solid var(--line); flex-wrap:wrap; }
    input[type=search] { flex:1; min-width:220px; border:1px solid var(--line); border-radius:10px; padding:10px 12px; font:inherit; }
    button { border:0; border-radius:10px; padding:10px 13px; font-weight:700; cursor:pointer; background:#182230; color:white; }
    button.secondary { color:#344054; background:white; border:1px solid var(--line); }
    button.small { padding:7px 10px; font-size:12px; }
    button:disabled { opacity:.42; cursor:not-allowed; }
    .savebar { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:14px; border-top:1px solid var(--line); background:#fcfcfd; }
    table { width:100%; border-collapse:collapse; }
    th,td { padding:12px 14px; border-bottom:1px solid #f0f1f3; text-align:left; vertical-align:middle; font-size:13px; }
    th { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em; background:#fcfcfd; }
    tr:last-child td { border-bottom:0; }
    .name { font-weight:750; font-size:14px; }
    .meta { color:var(--muted); font-size:11px; margin-top:4px; }
    code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; }
    .ok { color:var(--green); background:var(--green-bg); border-color:#abefc6; }
    .error,.unavailable { color:var(--red); background:var(--red-bg); border-color:#fecdca; }
    .syncing { color:var(--blue); background:var(--blue-bg); border-color:#b2ddff; }
    .waiting { color:var(--amber); background:var(--amber-bg); border-color:#fedf89; }
    .off { color:#667085; background:#f9fafb; }
    .tag { display:inline-flex; margin-right:5px; border-radius:6px; padding:3px 6px; font-size:10px; background:#f2f4f7; color:#475467; }
    .warning { margin-bottom:14px; color:var(--red); background:var(--red-bg); border:1px solid #fecdca; border-radius:12px; padding:11px 13px; font-size:13px; }
    .notice { margin-bottom:14px; color:var(--green); background:var(--green-bg); border:1px solid #abefc6; border-radius:12px; padding:11px 13px; font-size:13px; }
    .path { max-width:310px; word-break:break-all; color:#475467; }
    .empty { text-align:center; color:var(--muted); padding:45px 20px; }
    @media(max-width:800px) { .top{align-items:flex-start;flex-direction:column}.hide-mobile{display:none} main{padding:22px 10px 50px} th,td{padding:10px 8px}.path{max-width:160px} }
  </style>
</head>
<body>
<main>
  <div class="top">
    <div>
      <h1>Overleaf AutoSync</h1>
      <div class="sub">自动发现 Overleaf 项目 · 选择项目后定时镜像到本地文件夹</div>
    </div>
    <div class="stats">
      <span class="pill">发现 {{ discovered_count }} 个项目</span>
      <span class="pill">已启用 {{ selected_count }} 个</span>
      <span class="pill">上次刷新 {{ last_refresh or '尚未成功' }}</span>
    </div>
  </div>

  {% if saved %}<div class="notice">备份选择已保存。新启用的项目会立即开始同步。</div>{% endif %}
  {% if discovery_error %}<div class="warning"><strong>项目发现失败：</strong>{{ discovery_error }}</div>{% endif %}

  <div class="panel">
    <div class="toolbar">
      <input id="search" type="search" placeholder="搜索论文名称或 Project ID…" oninput="filterRows()">
      <button class="secondary" type="button" onclick="toggleVisible(true)">全选可见</button>
      <button class="secondary" type="button" onclick="toggleVisible(false)">取消可见</button>
      <form method="post" action="{{ url_for('refresh_projects') }}" style="margin:0"><button class="secondary" type="submit">刷新项目</button></form>
    </div>

    <form method="post" action="{{ url_for('save_selection') }}" id="selection-form">
      {% if projects %}
      <div style="overflow-x:auto">
      <table>
        <thead><tr><th>备份</th><th>项目</th><th class="hide-mobile">权限</th><th>状态</th><th class="hide-mobile">本地目录</th><th></th></tr></thead>
        <tbody>
        {% for p in projects %}
          <tr class="project-row" data-search="{{ (p.name ~ ' ' ~ p.project_id)|lower }}">
            <td><input class="project-check" type="checkbox" name="project_ids" value="{{ p.project_id }}" {% if p.selected %}checked{% endif %}></td>
            <td>
              <div class="name">{{ p.name }}</div>
              <div class="meta"><code>{{ p.project_id }}</code>{% if p.last_updated %} · {{ p.last_updated }}{% endif %}</div>
              <div style="margin-top:5px">{% if p.archived %}<span class="tag">已归档</span>{% endif %}{% if p.trashed %}<span class="tag">回收站</span>{% endif %}</div>
              {% if p.last_error %}<div class="meta" style="color:var(--red)">{{ p.last_error }}</div>{% endif %}
            </td>
            <td class="hide-mobile">{{ p.access_level or '—' }}</td>
            <td><span class="badge {{ p.status }}">{{ p.status }}</span>{% if p.last_sync %}<div class="meta">{{ p.last_sync }}</div>{% endif %}</td>
            <td class="path hide-mobile">{% if p.local_path %}<code>{{ p.local_path }}</code>{% else %}启用后创建{% endif %}</td>
            <td><button class="small secondary" type="submit" formaction="{{ url_for('sync_one', project_id=p.project_id) }}" formmethod="post" {% if not p.selected %}disabled title="先勾选并保存"{% endif %}>立即同步</button></td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      </div>
      {% else %}
        <div class="empty">尚未发现项目。请确认 Overleaf 账号、密码和服务器地址后点击“刷新项目”。</div>
      {% endif %}
      <div class="savebar">
        <div class="sub" style="margin:0">取消备份不会删除已经同步到本地的文件。</div>
        <button type="submit">保存备份选择</button>
      </div>
    </form>
  </div>
</main>
<script>
function filterRows(){
  const q=document.getElementById('search').value.trim().toLowerCase();
  document.querySelectorAll('.project-row').forEach(r=>{r.style.display=r.dataset.search.includes(q)?'':'none'});
}
function toggleVisible(value){
  document.querySelectorAll('.project-row').forEach(r=>{
    if(r.style.display!=='none'){const c=r.querySelector('.project-check'); if(c)c.checked=value;}
  });
}
</script>
</body>
</html>
"""


cfg = load_config()
service = SyncService(cfg)
app = Flask(__name__)


@app.get("/")
def index():
    rows = service.rows()
    return render_template_string(
        PAGE,
        projects=rows,
        discovered_count=len(service.catalog),
        selected_count=sum(1 for p in rows if p["selected"]),
        last_refresh=service.last_refresh,
        discovery_error=service.discovery_error,
        saved=request.args.get("saved") == "1",
    )


@app.post("/refresh")
def refresh_projects():
    try:
        service.refresh_projects()
    except Exception:
        pass
    return redirect(url_for("index"))


@app.post("/projects/selection")
def save_selection():
    service.set_selection(request.form.getlist("project_ids"))
    threading.Thread(target=service.sync_all, daemon=True).start()
    return redirect(url_for("index", saved="1"))


@app.post("/sync/<project_id>")
def sync_one(project_id: str):
    if project_id not in service.selected_ids():
        return "project is not enabled for backup", 409

    threading.Thread(
        target=service.sync_project,
        args=(project_id,),
        daemon=True,
    ).start()
    return redirect(url_for("index"))


@app.post("/api/refresh")
def api_refresh():
    try:
        service.refresh_projects()
        return jsonify({"ok": True, "projects": service.rows()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.get("/api/projects")
def api_projects():
    return jsonify(service.rows())


@app.post("/api/selection")
def api_selection():
    payload = request.get_json(silent=True) or {}
    project_ids = payload.get("project_ids", [])

    if not isinstance(project_ids, list):
        return jsonify({"error": "project_ids must be a list"}), 400

    try:
        service.set_selection([str(pid) for pid in project_ids])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    threading.Thread(target=service.sync_all, daemon=True).start()
    return jsonify({"ok": True, "projects": service.rows()})


@app.post("/api/sync")
def api_sync_all():
    threading.Thread(target=service.sync_all, daemon=True).start()
    return jsonify({"accepted": True}), 202


@app.get("/api/status")
def api_status():
    return jsonify(service.rows())


@app.get("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "discovered_projects": len(service.catalog),
            "selected_projects": len(service.selected_ids()),
            "last_refresh": service.last_refresh,
            "discovery_error": service.discovery_error,
        }
    )


def start_scheduler() -> None:
    thread = threading.Thread(
        target=service.scheduler_loop,
        name="autosync-scheduler",
        daemon=True,
    )
    thread.start()


if __name__ == "__main__":
    start_scheduler()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        threaded=True,
    )
