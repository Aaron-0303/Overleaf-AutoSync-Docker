from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from flask import abort, redirect, render_template_string, request, url_for

import app as base


# Add export actions without duplicating the main application implementation.
_PROJECT_LINK = "<a class=\"back\" href=\"{{ url_for('version_detail', project_id=project.project_id, sha=v.sha) }}\">查看详情 →</a>"
_PROJECT_ACTIONS = "<div class=\"row\" style=\"gap:8px\"><form method=\"post\" action=\"{{ url_for('export_version', project_id=project.project_id, sha=v.sha) }}\"><button class=\"btn secondary small\" type=\"submit\">打包 ZIP</button></form>" + _PROJECT_LINK + "</div>"
base.PROJECT_PAGE = base.PROJECT_PAGE.replace(_PROJECT_LINK, _PROJECT_ACTIONS)

_VERSION_BACK = "<a class=\"btn secondary\" href=\"{{ url_for('project_detail', project_id=project.project_id) }}\">返回版本历史</a>"
_VERSION_ACTIONS = "<div class=\"row\" style=\"gap:8px\"><form method=\"post\" action=\"{{ url_for('export_version', project_id=project.project_id, sha=version.sha) }}\"><button class=\"btn\" type=\"submit\">打包此版本</button></form>" + _VERSION_BACK + "</div>"
base.VERSION_PAGE = base.VERSION_PAGE.replace(_VERSION_BACK, _VERSION_ACTIONS)

_VERSION_NOTICE_ANCHOR = "</div><div class=\"panel\"><div class=\"row\"><div><b>{{ version.time }}</b>"
_VERSION_NOTICE = "</div>{% if exported %}<div class=\"notice\"><b>已打包：</b><span class=\"file\">{{ exported }}</span></div>{% endif %}{% if export_error %}<div class=\"warning\"><b>打包失败：</b>{{ export_error }}</div>{% endif %}<div class=\"panel\"><div class=\"row\"><div><b>{{ version.time }}</b>"
base.VERSION_PAGE = base.VERSION_PAGE.replace(_VERSION_NOTICE_ANCHOR, _VERSION_NOTICE)


def resolve_history_commit(path: Path, sha: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        raise ValueError("invalid commit")

    result = base.run_git(
        ["rev-parse", "--verify", f"{sha}^{{commit}}"],
        path,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("commit not found")

    resolved = result.stdout.strip()
    ancestor = base.run_git(
        ["merge-base", "--is-ancestor", resolved, "HEAD"],
        path,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError("commit is not in project history")
    return resolved


def commit_stamp(path: Path, sha: str) -> str:
    result = base.run_git(["show", "-s", "--format=%cI", sha], path)
    value = result.stdout.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y%m%d-%H%M%S")
    except ValueError:
        return "unknown-time"


def export_commit_zip(path: Path, sha: str) -> tuple[Path, str]:
    resolved = resolve_history_commit(path, sha)
    stamp = commit_stamp(path, resolved)
    filename = f"{path.name}__{stamp}__{resolved[:8]}.zip"
    output = path.parent / filename

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}-export-",
        suffix=".zip.tmp",
        dir=path.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        result = base.run_git(
            [
                "archive",
                "--format=zip",
                f"--prefix={path.name}/",
                f"--output={temp_path}",
                resolved,
            ],
            path,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git archive failed")
        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise RuntimeError("generated archive is empty")
        os.replace(temp_path, output)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    host_root = os.getenv("SYNC_DIR_HOST", "").strip()
    if host_root:
        display_path = str(Path(host_root) / filename)
    else:
        display_path = str(output)
    return output, display_path


def _version_context(project_id: str, sha: str):
    project = next(
        (p for p in base.service.rows() if p["project_id"] == project_id),
        None,
    )
    path = base.service.project_path(project_id)
    if not project or not path or not path.exists():
        abort(404)

    versions = base.git_versions(path, 200)
    version = next((v for v in versions if v["sha"].startswith(sha)), None)
    if not version:
        abort(404)
    return project, path, version


# Replace the existing endpoint implementation so export success can be shown inline.
def version_detail_with_export(project_id: str, sha: str):
    project, path, version = _version_context(project_id, sha)
    selected_file = request.args.get("file")
    diff = ""
    if selected_file:
        try:
            diff = base.git_file_diff(path, version["sha"], selected_file)
        except ValueError:
            abort(404)

    return render_template_string(
        base.VERSION_PAGE,
        css=base.CSS,
        project=project,
        version=version,
        selected_file=selected_file,
        diff=diff,
        exported=request.args.get("exported"),
        export_error=request.args.get("export_error"),
    )


base.app.view_functions["version_detail"] = version_detail_with_export


@base.app.post("/project/<project_id>/version/<sha>/export")
def export_version(project_id: str, sha: str):
    try:
        project, path, version = _version_context(project_id, sha)
        _output, display_path = export_commit_zip(path, version["sha"])
        base.LOG.info(
            "Exported project %s version %s to %s",
            project["name"],
            version["short"],
            display_path,
        )
        return redirect(
            url_for(
                "version_detail",
                project_id=project_id,
                sha=version["sha"],
                exported=display_path,
            )
        )
    except (ValueError, RuntimeError) as exc:
        return redirect(
            url_for(
                "version_detail",
                project_id=project_id,
                sha=sha,
                export_error=str(exc),
            )
        )


@base.app.post("/api/project/<project_id>/version/<sha>/export")
def api_export_version(project_id: str, sha: str):
    try:
        _project, path, version = _version_context(project_id, sha)
        output, display_path = export_commit_zip(path, version["sha"])
        return {
            "ok": True,
            "sha": version["sha"],
            "file": output.name,
            "path": display_path,
            "size": output.stat().st_size,
        }
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}, 400


if __name__ == "__main__":
    base.start_scheduler()
    base.app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        threaded=True,
    )
