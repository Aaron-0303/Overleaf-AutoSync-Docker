from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, redirect, render_template_string, request, url_for

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
    last_check: str | None = None
    last_sync: str | None = None
    last_change: str | None = None
    last_commit: str | None = None
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
    cfg["sync"].setdefault("history_limit", 50)

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


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def ensure_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        run_git(["init"], path)
    run_git(["config", "user.name", "Overleaf AutoSync"], path)
    run_git(["config", "user.email", "autosync@local"], path)


def git_has_head(path: Path) -> bool:
    if not (path / ".git").exists():
        return False
    return run_git(["rev-parse", "--verify", "HEAD"], path, check=False).returncode == 0


def git_worktree_changes(path: Path) -> list[dict[str, str]]:
    if not (path / ".git").exists():
        return []
    result = run_git(["status", "--porcelain=v1", "--untracked-files=all"], path)
    changes: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip() or "M"
        file_path = line[3:]
        if " -> " in file_path:
            file_path = file_path.split(" -> ", 1)[1]
        changes.append({"status": code, "path": file_path})
    return changes


def git_commit_changes(path: Path, message: str) -> tuple[str | None, list[dict[str, str]]]:
    ensure_git_repo(path)
    changes = git_worktree_changes(path)
    if not changes:
        head = run_git(["rev-parse", "HEAD"], path, check=False)
        return (head.stdout.strip() or None, [])
    run_git(["add", "-A"], path)
    staged = run_git(["diff", "--cached", "--quiet"], path, check=False)
    if staged.returncode == 0:
        head = run_git(["rev-parse", "HEAD"], path, check=False)
        return (head.stdout.strip() or None, [])
    if staged.returncode != 1:
        raise RuntimeError(staged.stderr.strip() or "git diff failed")
    run_git(["commit", "-m", message], path)
    sha = run_git(["rev-parse", "HEAD"], path).stdout.strip()
    return sha, changes


def git_version_count(path: Path) -> int:
    if not git_has_head(path):
        return 0
    result = run_git(["rev-list", "--count", "HEAD"], path)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def git_latest(path: Path) -> dict[str, str] | None:
    if not git_has_head(path):
        return None
    result = run_git(["log", "-1", "--format=%H%x1f%h%x1f%cI%x1f%s"], path)
    parts = result.stdout.strip().split("\x1f", 3)
    if len(parts) != 4:
        return None
    return {"sha": parts[0], "short": parts[1], "time": parts[2], "message": parts[3]}


def git_commit_files(path: Path, sha: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        return []
    status_result = run_git(
        ["show", "--format=", "--name-status", "--no-renames", sha], path, check=False
    )
    if status_result.returncode != 0:
        return []
    num_result = run_git(
        ["show", "--format=", "--numstat", "--no-renames", sha], path, check=False
    )
    stats: dict[str, tuple[str, str]] = {}
    for line in num_result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            stats[parts[2]] = (parts[0], parts[1])

    files: list[dict[str, Any]] = []
    labels = {"A": "新增", "M": "修改", "D": "删除", "T": "类型变化"}
    for line in status_result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        file_path = parts[-1]
        added, deleted = stats.get(file_path, ("-", "-"))
        files.append(
            {
                "status": status,
                "status_label": labels.get(status, status),
                "path": file_path,
                "added": added,
                "deleted": deleted,
            }
        )
    return files


def git_versions(path: Path, limit: int) -> list[dict[str, Any]]:
    if not git_has_head(path):
        return []
    result = run_git(
        ["log", f"-n{max(1, min(limit, 200))}", "--format=%H%x1f%h%x1f%cI%x1f%s"], path
    )
    versions: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f", 3)
        if len(parts) != 4:
            continue
        files = git_commit_files(path, parts[0])
        add_total = sum(int(f["added"]) for f in files if str(f["added"]).isdigit())
        del_total = sum(int(f["deleted"]) for f in files if str(f["deleted"]).isdigit())
        versions.append(
            {
                "sha": parts[0],
                "short": parts[1],
                "time": parts[2],
                "message": parts[3],
                "files": files,
                "file_count": len(files),
                "added": add_total,
                "deleted": del_total,
            }
        )
    return versions


def git_file_diff(path: Path, sha: str, file_path: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise ValueError("invalid commit")
    files = {item["path"] for item in git_commit_files(path, sha)}
    if file_path not in files:
        raise ValueError("file not in commit")
    result = run_git(
        ["show", "--format=", "--no-ext-diff", "--unified=3", sha, "--", file_path],
        path,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("cannot read diff")
    text = result.stdout
    if len(text) > 200_000:
        text = text[:200_000] + "\n\n[diff 过长，已截断]"
    return text


class OverleafClient:
    def __init__(self, cfg: dict[str, Any]):
        self.base_url = cfg["base_url"].rstrip("/") + "/"
        self.verify = bool(cfg.get("verify_tls", True))
        self.timeout = int(cfg.get("timeout", 60))
        self.email = env_required(cfg.get("email_env", "OVERLEAF_EMAIL"))
        self.password = env_required(cfg.get("password_env", "OVERLEAF_PASSWORD"))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Overleaf-AutoSync-Docker/4.0"})
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

    def _load_project_page(self) -> None:
        project_url = self._url("/project")
        response = self.session.get(
            project_url, timeout=self.timeout, verify=self.verify, allow_redirects=True
        )
        response.raise_for_status()
        if response.url.rstrip("/").endswith("/login"):
            raise RuntimeError("Overleaf login failed; check email/password and base_url")
        token = self._extract_csrf(response.text)
        if not token:
            raise RuntimeError("Could not find Overleaf CSRF token on /project")
        self.csrf_token = token

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
        headers = {
            "X-Csrf-Token": str(self.csrf_token),
            "Accept": "application/json",
            "Referer": self._url("/project"),
        }
        body = {"sort": {"by": "lastUpdated", "order": "desc"}}
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
        projects = response.json().get("projects")
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
            if "text/html" in response.headers.get("Content-Type", "") and response.url.rstrip("/").endswith("/login"):
                raise RuntimeError("Overleaf session expired or authentication failed")
            with destination.open("wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        if destination.stat().st_size == 0:
            raise RuntimeError(f"Downloaded empty ZIP for project {project_id}")


def safe_extract(zip_path: Path, output_dir: Path) -> None:
    root = output_dir.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"Unsafe path in ZIP: {member.filename}")
        archive.extractall(output_dir)


def same_file(a: Path, b: Path) -> bool:
    if not b.exists() or not b.is_file() or a.stat().st_size != b.stat().st_size:
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
                return {str(k): dict(v) for k, v in projects.items() if isinstance(v, dict)}
        except Exception as exc:
            LOG.error("Failed to read state %s: %s", self.state_path, exc)
        return {}

    def _save_registry_locked(self) -> None:
        payload = {"version": 2, "projects": self.registry}
        fd, tmp_name = tempfile.mkstemp(prefix="selection-", suffix=".json", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_name, self.state_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _allocate_local_name_locked(self, name: str, project_id: str) -> str:
        base = safe_local_name(name, project_id)
        used = {
            str(entry.get("local_name"))
            for pid, entry in self.registry.items()
            if pid != project_id and entry.get("local_name")
        }
        candidate = base
        if candidate in used or ((self.destination / candidate).exists() and project_id not in self.registry):
            candidate = f"{base}__{project_id[:8]}"
        n = 2
        initial = candidate
        while candidate in used:
            candidate = f"{initial}-{n}"
            n += 1
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
                last_check=entry.get("last_check"),
                last_sync=entry.get("last_sync"),
                last_change=entry.get("last_change"),
                last_commit=entry.get("last_commit"),
                last_error=entry.get("last_error"),
            )
            self.states[project_id] = state
            self.project_locks[project_id] = threading.Lock()
        else:
            state.name = name
            state.local_name = local_name
        return state

    def _persist_state_locked(self, project_id: str, state: ProjectState) -> None:
        entry = self.registry[project_id]
        entry.update(
            {
                "last_check": state.last_check,
                "last_sync": state.last_sync,
                "last_change": state.last_change,
                "last_commit": state.last_commit,
                "last_error": state.last_error,
            }
        )
        self._save_registry_locked()

    def _apply_catalog(self, projects: list[dict[str, Any]]) -> None:
        changed = False
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
                    changed = True
                if entry.get("enabled"):
                    self._ensure_state(project_id, new_name)
            if changed:
                self._save_registry_locked()
            self.last_refresh = utc_now()
            self.discovery_error = None

    def selected_ids(self) -> list[str]:
        with self.meta_lock:
            return [pid for pid, entry in self.registry.items() if entry.get("enabled")]

    def set_selection(self, project_ids: list[str]) -> None:
        requested = {str(pid) for pid in project_ids}
        with self.meta_lock:
            unknown = requested - set(self.catalog)
            if unknown:
                raise ValueError(f"Unknown project id(s): {', '.join(sorted(unknown))}")
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
                        "local_name": self._allocate_local_name_locked(name, project_id),
                    }
                    self.registry[project_id] = entry
                else:
                    entry["enabled"] = True
                    entry["name"] = name
                    if not entry.get("local_name"):
                        entry["local_name"] = self._allocate_local_name_locked(name, project_id)
                self._ensure_state(project_id, name)
            self._save_registry_locked()

    def project_path(self, project_id: str) -> Path | None:
        with self.meta_lock:
            entry = self.registry.get(project_id)
            if not entry or not entry.get("local_name"):
                return None
            return self.destination / str(entry["local_name"])

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
                name = str((project or {}).get("name") or entry.get("name") or project_id)
                local_name = entry.get("local_name")
                path = self.destination / str(local_name) if local_name else None
                status = state.status if state else ("off" if not selected else "waiting")
                if selected and project is None:
                    status = "unavailable"
                latest = git_latest(path) if path and path.exists() else None
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
                        "last_check": state.last_check if state else entry.get("last_check"),
                        "last_sync": state.last_sync if state else entry.get("last_sync"),
                        "last_change": state.last_change if state else entry.get("last_change"),
                        "last_error": state.last_error if state else entry.get("last_error"),
                        "local_path": str(path) if path else None,
                        "version_count": git_version_count(path) if path and path.exists() else 0,
                        "latest_version": latest,
                    }
                )
            return rows

    def refresh_projects(self) -> None:
        try:
            client = OverleafClient(self.cfg["overleaf"])
            client.login()
            self._apply_catalog(client.list_projects())
            LOG.info("Discovered %d Overleaf projects", len(self.catalog))
        except Exception as exc:
            with self.meta_lock:
                self.discovery_error = str(exc)
            LOG.exception("Project discovery failed")
            raise

    def _sync_project_with_client(self, project_id: str, client: OverleafClient, force: bool = False) -> None:
        with self.meta_lock:
            entry = self.registry.get(project_id)
            project = self.catalog.get(project_id)
            if not entry or not entry.get("enabled"):
                raise RuntimeError("Project is not enabled for backup")
            if not project:
                state = self._ensure_state(project_id, str(entry.get("name") or project_id))
                state.status = "unavailable"
                state.last_error = "Project is no longer visible to this Overleaf account"
                self._persist_state_locked(project_id, state)
                return
            state = self._ensure_state(project_id, str(project["name"]))
            lock = self.project_locks[project_id]

        if not lock.acquire(blocking=False):
            return
        try:
            state.status = "checking"
            state.last_error = None
            state.last_check = utc_now()
            local_path = self.destination / state.local_name
            remote_updated = str(project.get("lastUpdated") or "")
            previous_remote = str(entry.get("remote_last_updated") or "")
            local_clean = bool(local_path.exists() and git_has_head(local_path) and not git_worktree_changes(local_path))

            if not force and remote_updated and remote_updated == previous_remote and local_clean:
                state.status = "ok"
                with self.meta_lock:
                    self._persist_state_locked(project_id, state)
                LOG.info("Checked %s: unchanged, skip download", state.name)
                return

            state.status = "syncing"
            ensure_git_repo(local_path)
            with tempfile.TemporaryDirectory(prefix="overleaf-autosync-") as tmp:
                tmp_path = Path(tmp)
                zip_path = tmp_path / "project.zip"
                extracted = tmp_path / "project"
                extracted.mkdir()
                client.download_project_zip(project_id, zip_path)
                safe_extract(zip_path, extracted)
                mirror_tree(extracted, local_path)

            now = utc_now()
            message = f"AutoSync {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            sha, changes = git_commit_changes(local_path, message)
            state.last_sync = now
            state.last_commit = sha
            if changes:
                state.last_change = now
                LOG.info("Backed up %s: %d changed files", state.name, len(changes))
            else:
                LOG.info("Checked %s: downloaded but source content unchanged", state.name)
            state.status = "ok"
            with self.meta_lock:
                entry["remote_last_updated"] = remote_updated
                self._persist_state_locked(project_id, state)
        except Exception as exc:
            state.status = "error"
            state.last_error = str(exc)
            with self.meta_lock:
                self._persist_state_locked(project_id, state)
            LOG.exception("Sync failed for %s (%s)", state.name, project_id)
        finally:
            lock.release()

    def sync_project(self, project_id: str, force: bool = True) -> None:
        try:
            client = OverleafClient(self.cfg["overleaf"])
            client.login()
            self._apply_catalog(client.list_projects())
            self._sync_project_with_client(project_id, client, force=force)
        except Exception as exc:
            LOG.exception("Manual sync failed for %s: %s", project_id, exc)

    def sync_all(self) -> None:
        if not self.sync_all_lock.acquire(blocking=False):
            return
        try:
            client = OverleafClient(self.cfg["overleaf"])
            client.login()
            self._apply_catalog(client.list_projects())
            for project_id in self.selected_ids():
                self._sync_project_with_client(project_id, client, force=False)
        except Exception as exc:
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


CSS = """
:root{--bg:#f6f8fb;--card:#fff;--text:#152033;--muted:#6b7280;--line:#e7eaf0;--brand:#1769e0;--brand2:#0e4fb4;--green:#0c7a4b;--greenbg:#eafaf2;--red:#b42318;--redbg:#fff0ee;--amber:#a15c00;--amberbg:#fff7e6;--bluebg:#edf5ff}*{box-sizing:border-box}body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}a{color:inherit;text-decoration:none}.shell{max-width:1320px;margin:auto;padding:28px 22px 72px}.nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}.brand{display:flex;align-items:center;gap:12px}.logo{width:38px;height:38px;border-radius:12px;background:linear-gradient(135deg,var(--brand),#63a4ff);display:grid;place-items:center;color:white;font-weight:900}.brand h1{font-size:20px;margin:0}.brand p{margin:2px 0 0;color:var(--muted);font-size:12px}.btn{border:0;border-radius:10px;padding:10px 14px;font-weight:700;cursor:pointer;background:#182230;color:white}.btn:hover{opacity:.92}.btn.secondary{background:white;color:#344054;border:1px solid var(--line)}.btn.small{padding:7px 10px;font-size:12px}.hero{background:linear-gradient(135deg,#ffffff,#f2f7ff);border:1px solid #dfe8f7;border-radius:20px;padding:22px;display:flex;justify-content:space-between;gap:20px;align-items:center;box-shadow:0 8px 30px rgba(23,105,224,.06);margin-bottom:18px}.hero h2{margin:0;font-size:26px}.hero p{margin:6px 0 0;color:var(--muted)}.stats{display:flex;gap:10px;flex-wrap:wrap}.stat{min-width:108px;background:white;border:1px solid var(--line);border-radius:14px;padding:12px}.stat b{font-size:20px;display:block}.stat span{font-size:11px;color:var(--muted)}.toolbar{display:flex;gap:10px;align-items:center;margin:16px 0}.search{flex:1;border:1px solid var(--line);border-radius:11px;background:white;padding:11px 13px;font:inherit}.notice,.warning{border-radius:12px;padding:11px 13px;margin:12px 0;font-size:13px}.notice{background:var(--greenbg);color:var(--green);border:1px solid #bcebd3}.warning{background:var(--redbg);color:var(--red);border:1px solid #ffd2cc}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}.card{background:white;border:1px solid var(--line);border-radius:16px;padding:17px;box-shadow:0 5px 18px rgba(16,24,40,.035);transition:.16s}.card:hover{transform:translateY(-1px);box-shadow:0 9px 24px rgba(16,24,40,.07)}.cardtop{display:flex;justify-content:space-between;gap:12px}.title{font-weight:780;font-size:16px}.meta{font-size:11px;color:var(--muted);margin-top:4px}.badge{display:inline-flex;align-items:center;border-radius:999px;padding:4px 8px;font-size:10px;font-weight:750;border:1px solid var(--line);white-space:nowrap}.ok{color:var(--green);background:var(--greenbg)}.checking,.syncing{color:var(--brand2);background:var(--bluebg)}.error,.unavailable{color:var(--red);background:var(--redbg)}.waiting{color:var(--amber);background:var(--amberbg)}.off{color:#667085;background:#f7f8fa}.row{display:flex;justify-content:space-between;gap:12px;align-items:center}.divider{height:1px;background:var(--line);margin:14px 0}.facts{display:grid;grid-template-columns:1fr 1fr;gap:10px}.fact span{display:block;color:var(--muted);font-size:10px}.fact b{font-size:12px}.tag{display:inline-block;background:#f2f4f7;color:#475467;border-radius:6px;padding:3px 6px;font-size:10px;margin-right:4px}.savebar{position:sticky;bottom:16px;margin-top:16px;background:rgba(255,255,255,.94);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:14px;padding:12px 14px;display:flex;justify-content:space-between;align-items:center;box-shadow:0 12px 35px rgba(16,24,40,.10)}.project-head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin:18px 0}.project-head h2{font-size:28px;margin:0}.crumb{font-size:12px;color:var(--muted)}.panel{background:white;border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:15px}.timeline{position:relative;margin-left:8px}.version{display:grid;grid-template-columns:20px 1fr;gap:12px;padding-bottom:20px}.dot{width:11px;height:11px;border-radius:50%;background:var(--brand);margin-top:6px;box-shadow:0 0 0 5px #edf5ff}.versionbody{border:1px solid var(--line);border-radius:14px;padding:14px}.change-list{margin-top:10px;display:grid;gap:6px}.change{display:flex;justify-content:space-between;gap:12px;padding:7px 9px;background:#fafbfc;border-radius:8px;font-size:12px}.file{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}.plus{color:#067647}.minus{color:#b42318}.code{background:#111827;color:#e5e7eb;border-radius:12px;padding:14px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre}.back{color:var(--brand);font-weight:700;font-size:13px}.empty{text-align:center;padding:48px 15px;color:var(--muted)}@media(max-width:720px){.shell{padding:18px 12px 55px}.hero,.project-head{align-items:flex-start;flex-direction:column}.grid{grid-template-columns:1fr}.stats{width:100%}.stat{flex:1}.hide-mobile{display:none}}
"""

DASHBOARD = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Overleaf AutoSync</title><style>{{ css }}</style></head><body><div class="shell">
<div class="nav"><div class="brand"><div class="logo">O</div><div><h1>Overleaf AutoSync</h1><p>自动发现 · 变化检测 · Git 版本历史</p></div></div><form method="post" action="{{ url_for('refresh_projects') }}"><button class="btn secondary">刷新项目</button></form></div>
<div class="hero"><div><h2>论文备份中心</h2><p>定时检查 Overleaf；只有源码真正变化时才生成新版本。</p></div><div class="stats"><div class="stat"><b>{{ discovered }}</b><span>发现项目</span></div><div class="stat"><b>{{ selected }}</b><span>已启用备份</span></div><div class="stat"><b>{{ versions }}</b><span>累计版本</span></div></div></div>
{% if saved %}<div class="notice">备份选择已保存，新启用的项目会立即执行一次检查。</div>{% endif %}{% if error %}<div class="warning"><b>项目发现失败：</b>{{ error }}</div>{% endif %}
<div class="toolbar"><input id="search" class="search" placeholder="搜索论文名称或 Project ID…" oninput="filterCards()"><button class="btn secondary" type="button" onclick="toggleVisible(true)">全选可见</button><button class="btn secondary" type="button" onclick="toggleVisible(false)">取消可见</button></div>
<form method="post" action="{{ url_for('save_selection') }}" id="selection-form"><div class="grid">
{% for p in projects %}<article class="card project-card" data-search="{{ (p.name ~ ' ' ~ p.project_id)|lower }}"><div class="cardtop"><div><a class="title" href="{{ url_for('project_detail', project_id=p.project_id) }}">{{ p.name }}</a><div class="meta"><code>{{ p.project_id }}</code></div></div><span class="badge {{ p.status }}">{{ p.status }}</span></div><div style="margin-top:9px">{% if p.archived %}<span class="tag">已归档</span>{% endif %}{% if p.trashed %}<span class="tag">回收站</span>{% endif %}<span class="tag">{{ p.access_level or '—' }}</span></div><div class="divider"></div><div class="facts"><div class="fact"><span>历史版本</span><b>{{ p.version_count }}</b></div><div class="fact"><span>最近变化</span><b>{{ p.last_change or '尚无' }}</b></div><div class="fact"><span>最近检查</span><b>{{ p.last_check or '尚无' }}</b></div><div class="fact"><span>最新版本</span><b>{% if p.latest_version %}{{ p.latest_version.short }}{% else %}—{% endif %}</b></div></div>{% if p.last_error %}<div class="warning" style="margin-bottom:0">{{ p.last_error }}</div>{% endif %}<div class="divider"></div><div class="row"><label><input class="project-check" type="checkbox" name="project_ids" value="{{ p.project_id }}" {% if p.selected %}checked{% endif %}> 自动备份</label><a class="back" href="{{ url_for('project_detail', project_id=p.project_id) }}">查看版本 →</a></div></article>{% endfor %}
{% if not projects %}<div class="empty">尚未发现项目，请检查 Overleaf 地址与账号。</div>{% endif %}</div><div class="savebar"><div><b>备份策略</b><div class="meta">没变化只检查，不下载、不产生版本；取消勾选不会删除历史。</div></div><button class="btn" type="submit">保存备份选择</button></div></form></div>
<script>function filterCards(){const q=document.getElementById('search').value.trim().toLowerCase();document.querySelectorAll('.project-card').forEach(c=>c.style.display=c.dataset.search.includes(q)?'':'none')}function toggleVisible(v){document.querySelectorAll('.project-card').forEach(c=>{if(c.style.display!=='none'){const x=c.querySelector('.project-check');if(x)x.checked=v}})}</script></body></html>
"""

PROJECT_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ project.name }} · AutoSync</title><style>{{ css }}</style></head><body><div class="shell"><div class="nav"><div class="brand"><div class="logo">O</div><div><h1>Overleaf AutoSync</h1><p>版本历史</p></div></div><a class="btn secondary" href="{{ url_for('index') }}">返回项目</a></div><div class="crumb">项目 / {{ project.name }}</div><div class="project-head"><div><h2>{{ project.name }}</h2><div class="meta"><code>{{ project.project_id }}</code> · {{ project.local_path or '尚未创建本地目录' }}</div></div><div class="row"><span class="badge {{ project.status }}">{{ project.status }}</span>{% if project.selected %}<form method="post" action="{{ url_for('sync_one', project_id=project.project_id) }}"><button class="btn">立即检查</button></form>{% endif %}</div></div>
<div class="panel"><div class="facts"><div class="fact"><span>版本数</span><b>{{ project.version_count }}</b></div><div class="fact"><span>最近检查</span><b>{{ project.last_check or '—' }}</b></div><div class="fact"><span>最近同步</span><b>{{ project.last_sync or '—' }}</b></div><div class="fact"><span>最近变化</span><b>{{ project.last_change or '—' }}</b></div></div>{% if project.last_error %}<div class="warning">{{ project.last_error }}</div>{% endif %}</div>
<div class="panel"><div class="row"><div><b>备份版本</b><div class="meta">每次源码发生实际变化才会出现一个新版本。</div></div><span class="tag">显示最近 {{ limit }} 个</span></div></div>
{% if versions %}<div class="timeline">{% for v in versions %}<div class="version"><div class="dot"></div><div class="versionbody"><div class="row"><div><b>{{ v.time }}</b><div class="meta"><code>{{ v.short }}</code> · {{ v.message }}</div></div><a class="back" href="{{ url_for('version_detail', project_id=project.project_id, sha=v.sha) }}">查看详情 →</a></div><div class="meta" style="margin-top:8px">{{ v.file_count }} 个文件 · <span class="plus">+{{ v.added }}</span> · <span class="minus">-{{ v.deleted }}</span></div><div class="change-list">{% for f in v.files[:5] %}<div class="change"><span><span class="tag">{{ f.status_label }}</span><span class="file">{{ f.path }}</span></span><span><span class="plus">+{{ f.added }}</span> <span class="minus">-{{ f.deleted }}</span></span></div>{% endfor %}{% if v.file_count > 5 %}<div class="meta">还有 {{ v.file_count - 5 }} 个文件…</div>{% endif %}</div></div></div>{% endfor %}</div>{% else %}<div class="panel empty">还没有版本。启用备份后，第一次同步会创建初始版本。</div>{% endif %}</div></body></html>
"""

VERSION_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ version.short }} · {{ project.name }}</title><style>{{ css }}</style></head><body><div class="shell"><div class="nav"><div class="brand"><div class="logo">O</div><div><h1>{{ project.name }}</h1><p>版本 {{ version.short }}</p></div></div><a class="btn secondary" href="{{ url_for('project_detail', project_id=project.project_id) }}">返回版本历史</a></div><div class="panel"><div class="row"><div><b>{{ version.time }}</b><div class="meta"><code>{{ version.sha }}</code> · {{ version.message }}</div></div><div><span class="plus">+{{ version.added }}</span>&nbsp;&nbsp;<span class="minus">-{{ version.deleted }}</span></div></div></div><div class="panel"><b>修改文件</b><div class="change-list">{% for f in version.files %}<a class="change" href="{{ url_for('version_detail', project_id=project.project_id, sha=version.sha, file=f.path) }}"><span><span class="tag">{{ f.status_label }}</span><span class="file">{{ f.path }}</span></span><span><span class="plus">+{{ f.added }}</span> <span class="minus">-{{ f.deleted }}</span></span></a>{% endfor %}</div></div>{% if selected_file %}<div class="panel"><div class="row"><div><b>{{ selected_file }}</b><div class="meta">该文件在此版本中的 diff</div></div></div><pre class="code">{{ diff }}</pre></div>{% else %}<div class="panel empty">点击上面的文件即可查看具体修改内容。</div>{% endif %}</div></body></html>
"""

cfg = load_config()
service = SyncService(cfg)
app = Flask(__name__)


@app.get("/")
def index():
    rows = service.rows()
    return render_template_string(
        DASHBOARD,
        css=CSS,
        projects=rows,
        discovered=len(service.catalog),
        selected=sum(1 for p in rows if p["selected"]),
        versions=sum(int(p["version_count"]) for p in rows),
        error=service.discovery_error,
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


@app.get("/project/<project_id>")
def project_detail(project_id: str):
    project = next((p for p in service.rows() if p["project_id"] == project_id), None)
    if not project:
        abort(404)
    path = service.project_path(project_id)
    limit = int(cfg["sync"].get("history_limit", 50))
    versions = git_versions(path, limit) if path and path.exists() else []
    return render_template_string(
        PROJECT_PAGE, css=CSS, project=project, versions=versions, limit=limit
    )


@app.get("/project/<project_id>/version/<sha>")
def version_detail(project_id: str, sha: str):
    project = next((p for p in service.rows() if p["project_id"] == project_id), None)
    path = service.project_path(project_id)
    if not project or not path or not path.exists():
        abort(404)
    versions = git_versions(path, 200)
    version = next((v for v in versions if v["sha"].startswith(sha)), None)
    if not version:
        abort(404)
    selected_file = request.args.get("file")
    diff = ""
    if selected_file:
        try:
            diff = git_file_diff(path, version["sha"], selected_file)
        except ValueError:
            abort(404)
    return render_template_string(
        VERSION_PAGE,
        css=CSS,
        project=project,
        version=version,
        selected_file=selected_file,
        diff=diff,
    )


@app.post("/sync/<project_id>")
def sync_one(project_id: str):
    if project_id not in service.selected_ids():
        return "project is not enabled for backup", 409
    threading.Thread(target=service.sync_project, args=(project_id, True), daemon=True).start()
    return redirect(url_for("project_detail", project_id=project_id))


@app.get("/api/projects")
def api_projects():
    return jsonify(service.rows())


@app.post("/api/refresh")
def api_refresh():
    try:
        service.refresh_projects()
        return jsonify({"ok": True, "projects": service.rows()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


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
    return jsonify({"ok": True})


@app.post("/api/sync")
def api_sync_all():
    threading.Thread(target=service.sync_all, daemon=True).start()
    return jsonify({"accepted": True}), 202


@app.get("/api/project/<project_id>/versions")
def api_versions(project_id: str):
    path = service.project_path(project_id)
    if not path or not path.exists():
        return jsonify([])
    return jsonify(git_versions(path, int(cfg["sync"].get("history_limit", 50))))


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
    threading.Thread(target=service.scheduler_loop, name="autosync-scheduler", daemon=True).start()


if __name__ == "__main__":
    start_scheduler()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)
