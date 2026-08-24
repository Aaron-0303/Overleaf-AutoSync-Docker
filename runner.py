from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

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


# GitHub-like unified diff viewer: colored additions/deletions, hunk headers and line numbers.
_DIFF_CSS = r"""
.diff-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:14px 0 8px}.diff-toolbar-title{font-size:12px;font-weight:750;color:#344054}.diff-legend{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.diff-chip{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:4px 8px;font:700 10px/1 ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid}.diff-chip.add{background:#e6ffec;border-color:#abf2bc;color:#116329}.diff-chip.del{background:#ffebe9;border-color:#ffcecb;color:#82071e}.diff-chip.hunk{background:#ddf4ff;border-color:#b6e3ff;color:#0969da}.diff-view{border:1px solid #d8dee4;border-radius:12px;overflow:auto;background:#fff;box-shadow:inset 0 1px 0 rgba(27,31,36,.04)}.diff-row{display:grid;grid-template-columns:52px 52px 28px minmax(620px,1fr);min-width:752px;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#24292f}.diff-row+.diff-row{border-top:1px solid rgba(216,222,228,.35)}.diff-ln{padding:1px 8px;text-align:right;color:#8c959f;background:#f6f8fa;border-right:1px solid rgba(216,222,228,.65);user-select:none}.diff-mark{padding:1px 7px;text-align:center;font-weight:800;user-select:none}.diff-code-line{padding:1px 10px;white-space:pre;tab-size:4}.diff-row.context .diff-code-line{background:#fff}.diff-row.add{background:#e6ffec}.diff-row.add .diff-ln{background:#ccffd8;color:#57606a}.diff-row.add .diff-mark{color:#1a7f37;background:#ccffd8}.diff-row.add .diff-code-line{background:#e6ffec}.diff-row.del{background:#ffebe9}.diff-row.del .diff-ln{background:#ffd7d5;color:#57606a}.diff-row.del .diff-mark{color:#cf222e;background:#ffd7d5}.diff-row.del .diff-code-line{background:#ffebe9}.diff-row.hunk{background:#ddf4ff;color:#0550ae}.diff-row.hunk .diff-ln,.diff-row.hunk .diff-mark{background:#b6e3ff;color:#0969da}.diff-row.hunk .diff-code-line{background:#ddf4ff;font-weight:650}.diff-row.meta{color:#656d76;background:#f6f8fa}.diff-row.meta .diff-ln,.diff-row.meta .diff-mark,.diff-row.meta .diff-code-line{background:#f6f8fa}.diff-row.filemeta{color:#57606a;background:#f6f8fa}.diff-row.filemeta .diff-ln,.diff-row.filemeta .diff-mark,.diff-row.filemeta .diff-code-line{background:#f6f8fa;font-weight:650}.diff-row:hover .diff-code-line{box-shadow:inset 3px 0 0 rgba(9,105,218,.18)}@media(max-width:720px){.diff-toolbar{align-items:flex-start;flex-direction:column}.diff-row{grid-template-columns:42px 42px 24px minmax(560px,1fr);min-width:668px}.diff-ln{padding-left:4px;padding-right:6px}.diff-code-line{padding-left:8px}}
"""
base.CSS += _DIFF_CSS

_DIFF_PRE = '<pre class="code">{{ diff }}</pre>'
_DIFF_VIEW = r'''<div class="diff-toolbar"><div class="diff-toolbar-title">Unified Diff · 旧行号 / 新行号</div><div class="diff-legend"><span class="diff-chip add">+ 新增</span><span class="diff-chip del">− 删除</span><span class="diff-chip hunk">@@ 区块</span></div></div><div class="diff-view">{% for row in diff_rows %}<div class="diff-row {{ row.kind }}"><span class="diff-ln">{{ row.old_line if row.old_line is not none else '' }}</span><span class="diff-ln">{{ row.new_line if row.new_line is not none else '' }}</span><span class="diff-mark">{{ row.marker }}</span><span class="diff-code-line">{{ row.text }}</span></div>{% endfor %}</div>'''
base.VERSION_PAGE = base.VERSION_PAGE.replace(_DIFF_PRE, _DIFF_VIEW)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_unified_diff(diff: str) -> list[dict[str, Any]]:
    """Turn a unified diff into renderable rows with old/new line numbers."""
    rows: list[dict[str, Any]] = []
    old_line: int | None = None
    new_line: int | None = None

    def append(kind: str, text: str, marker: str = "", old: int | None = None, new: int | None = None) -> None:
        rows.append(
            {
                "kind": kind,
                "text": text,
                "marker": marker,
                "old_line": old,
                "new_line": new,
            }
        )

    for raw in diff.splitlines():
        if raw.startswith("@@"):
            match = _HUNK_RE.match(raw)
            if match:
                old_line = int(match.group(1))
                new_line = int(match.group(2))
            append("hunk", raw, "@@")
            continue

        if raw.startswith("diff --git") or raw.startswith("index ") or raw.startswith("new file mode ") or raw.startswith("deleted file mode ") or raw.startswith("similarity index ") or raw.startswith("rename from ") or raw.startswith("rename to "):
            append("meta", raw)
            continue

        if raw.startswith("--- ") or raw.startswith("+++ "):
            append("filemeta", raw)
            continue

        if raw.startswith("+"):
            current_new = new_line
            append("add", raw[1:], "+", new=current_new)
            if new_line is not None:
                new_line += 1
            continue

        if raw.startswith("-"):
            current_old = old_line
            append("del", raw[1:], "−", old=current_old)
            if old_line is not None:
                old_line += 1
            continue

        if raw.startswith(" "):
            current_old = old_line
            current_new = new_line
            append("context", raw[1:], "", old=current_old, new=current_new)
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
            continue

        if raw.startswith("\\ No newline at end of file"):
            append("meta", raw)
            continue

        append("meta", raw)

    return rows


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


# Replace the existing endpoint implementation so export success and rich diff rows can be shown inline.
def version_detail_with_export(project_id: str, sha: str):
    project, path, version = _version_context(project_id, sha)
    selected_file = request.args.get("file")
    diff = ""
    diff_rows: list[dict[str, Any]] = []
    if selected_file:
        try:
            diff = base.git_file_diff(path, version["sha"], selected_file)
            diff_rows = parse_unified_diff(diff)
        except ValueError:
            abort(404)

    return render_template_string(
        base.VERSION_PAGE,
        css=base.CSS,
        project=project,
        version=version,
        selected_file=selected_file,
        diff=diff,
        diff_rows=diff_rows,
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
