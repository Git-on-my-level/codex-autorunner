import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from .utils import atomic_write, ensure_executable, read_json


class GitHubError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_dumps(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int = 30,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitHubError(f"Missing binary: {args[0]}", status_code=500) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(
            f"Command timed out: {' '.join(args)}", status_code=504
        ) from exc

    if check and proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise GitHubError(
            f"Command failed: {' '.join(args)}: {detail}", status_code=400
        )
    return proc


def _tail_lines(text: str, *, max_lines: int = 60, max_chars: int = 6000) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = raw.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        return tail[-max_chars:]
    return tail


def _sanitize_cmd(args: list[str]) -> str:
    # Best-effort sanitization: redact obvious tokens if ever present.
    redacted: list[str] = []
    for a in args:
        if any(
            k in a.lower() for k in ("token", "apikey", "api_key", "password", "secret")
        ):
            redacted.append("<redacted>")
        else:
            redacted.append(a)
    return " ".join(redacted)


def _get_nested(d: Any, *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _build_sync_agent_prompt(
    *,
    repo_root: Path,
    branch: str,
    issue_num: Optional[int],
) -> str:
    issue_hint = f"issue #{issue_num}" if issue_num else "the linked issue (if any)"
    return f"""You are syncing the local git branch to the remote to prepare for a GitHub PR.

Repository: {repo_root}
Branch: {branch}
Context: {issue_hint}

Rules (safety):
- Do NOT discard changes. Do NOT run destructive commands like `git reset --hard`, `git clean -fdx`, or delete files indiscriminately.
- Do NOT force-push.
- Prefer minimal, safe changes that preserve intent.

Tasks:
1) If there is a Makefile or standard tooling, run formatting/lint/tests best-effort. Prefer (in this order) `make fmt`, `make format`, `make lint`, `make test` when targets exist.
2) Check `git status`. If there are unstaged/uncommitted changes and committing is appropriate, stage and commit them.
   - Use a descriptive commit message based on the diff; include the issue number if available.
3) Push the current branch to `origin`.
   - Ensure upstream is set (e.g., `git push -u origin {branch}`).
4) If push is rejected (non-fast-forward/remote updated), do a safe `git pull --rebase`.
   - If there are rebase conflicts, resolve them by editing files to incorporate both sides correctly.
   - Continue the rebase (`git rebase --continue`) until it completes.
   - Re-run formatting if needed after conflict resolution.
   - Retry push.
5) Do not stop until the branch is successfully pushed.

When finished, print a short summary of what you did.
"""


def _run_codex_sync_agent(
    *,
    repo_root: Path,
    raw_config: dict,
    prompt: str,
) -> None:
    codex_cfg = raw_config.get("codex") if isinstance(raw_config, dict) else None
    codex_cfg = codex_cfg if isinstance(codex_cfg, dict) else {}
    binary = str(codex_cfg.get("binary") or "codex")
    base_args = codex_cfg.get("args") if isinstance(codex_cfg.get("args"), list) else []

    # Strip any existing --model flags from base args to avoid ambiguity; this flow
    # deliberately uses the configured "small" model (or no model when unset).
    cleaned_args: list[str] = []
    skip_next = False
    for a in [str(x) for x in base_args]:
        if skip_next:
            skip_next = False
            continue
        if a == "--model":
            skip_next = True
            continue
        cleaned_args.append(a)

    # Use the "small" model for this use-case when configured; if unset/null, omit --model.
    models = _get_nested(raw_config, "codex", "models", default=None)
    if isinstance(models, dict) and "small" in models:
        model_small = models.get("small")
    else:
        model_small = "gpt-5.1-codex-mini"
    model_flag: list[str] = ["--model", str(model_small)] if model_small else []

    cmd = [binary, *model_flag, *cleaned_args, prompt]

    github_cfg = raw_config.get("github") if isinstance(raw_config, dict) else None
    github_cfg = github_cfg if isinstance(github_cfg, dict) else {}
    timeout_seconds = int(github_cfg.get("sync_agent_timeout_seconds", 1800))

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitHubError(f"Missing binary: {binary}", status_code=500) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitHubError(
            f"Codex sync agent timed out after {timeout_seconds}s: {_sanitize_cmd(cmd[:-1])}",
            status_code=504,
        ) from exc

    if proc.returncode != 0:
        stdout_tail = _tail_lines(proc.stdout or "")
        stderr_tail = _tail_lines(proc.stderr or "")
        detail = stderr_tail or stdout_tail or f"exit {proc.returncode}"
        raise GitHubError(
            "Codex sync agent failed.\n"
            f"cmd: {_sanitize_cmd(cmd[:-1])}\n"
            f"detail:\n{detail}",
            status_code=400,
        )


@dataclass
class RepoInfo:
    name_with_owner: str
    url: str
    default_branch: Optional[str] = None


def _parse_repo_info(payload: dict) -> RepoInfo:
    name = payload.get("nameWithOwner") or ""
    url = payload.get("url") or ""
    default_ref = payload.get("defaultBranchRef") or {}
    default_branch = default_ref.get("name") if isinstance(default_ref, dict) else None
    if not name or not url:
        raise GitHubError("Unable to determine GitHub repo (missing nameWithOwner/url)")
    return RepoInfo(
        name_with_owner=str(name), url=str(url), default_branch=default_branch
    )


ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<num>\d+)(?:[/?#].*)?$"
)


def parse_issue_input(issue: str) -> Tuple[Optional[str], int]:
    """
    Returns (repo_slug_or_none, issue_number).
    Accepts:
      - "123"
      - "https://github.com/org/repo/issues/123"
    """
    raw = (issue or "").strip()
    if not raw:
        raise GitHubError("issue is required", status_code=400)
    if raw.isdigit():
        return None, int(raw)
    m = ISSUE_URL_RE.match(raw)
    if not m:
        raise GitHubError(
            "Invalid issue reference (expected issue number or GitHub issue URL)"
        )
    slug = f"{m.group('owner')}/{m.group('repo')}"
    return slug, int(m.group("num"))


class GitHubService:
    def __init__(self, repo_root: Path, raw_config: Optional[dict] = None):
        self.repo_root = repo_root
        self.raw_config = raw_config or {}
        self.github_path = repo_root / ".codex-autorunner" / "github.json"

    # ── persistence ────────────────────────────────────────────────────────────
    def read_link_state(self) -> dict:
        return read_json(self.github_path) or {}

    def write_link_state(self, data: dict) -> dict:
        payload = dict(data)
        payload.setdefault("updatedAtMs", _now_ms())
        atomic_write(self.github_path, _json_dumps(payload))
        return payload

    # ── capability/status ──────────────────────────────────────────────────────
    def gh_available(self) -> bool:
        return ensure_executable("gh")

    def gh_authenticated(self) -> bool:
        if not self.gh_available():
            return False
        proc = _run(
            ["gh", "auth", "status"],
            cwd=self.repo_root,
            check=False,
            timeout_seconds=10,
        )
        return proc.returncode == 0

    def repo_info(self) -> RepoInfo:
        proc = _run(
            ["gh", "repo", "view", "--json", "nameWithOwner,url,defaultBranchRef"],
            cwd=self.repo_root,
            check=True,
            timeout_seconds=15,
        )
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise GitHubError(
                "Unable to parse gh repo view output", status_code=500
            ) from exc
        return _parse_repo_info(payload)

    def current_branch(self, *, cwd: Optional[Path] = None) -> str:
        proc = _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or self.repo_root,
            check=True,
        )
        return (proc.stdout or "").strip() or "HEAD"

    def is_clean(self, *, cwd: Optional[Path] = None) -> bool:
        proc = _run(
            ["git", "status", "--porcelain"], cwd=cwd or self.repo_root, check=True
        )
        return not bool((proc.stdout or "").strip())

    def pr_for_branch(
        self, *, branch: str, cwd: Optional[Path] = None
    ) -> Optional[dict]:
        cwd = cwd or self.repo_root
        proc = _run(
            [
                "gh",
                "pr",
                "view",
                "--json",
                "number,url,state,isDraft,title,headRefName,baseRefName",
            ],
            cwd=cwd,
            check=False,
            timeout_seconds=15,
        )
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout or "{}") or None
            except json.JSONDecodeError:
                return None
        proc2 = _run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--limit",
                "1",
                "--json",
                "number,url,state,isDraft,title,headRefName,baseRefName",
            ],
            cwd=cwd,
            check=False,
            timeout_seconds=15,
        )
        if proc2.returncode != 0:
            return None
        try:
            arr = json.loads(proc2.stdout or "[]") or []
        except json.JSONDecodeError:
            return None
        return arr[0] if arr else None

    def issue_view(self, *, number: int, cwd: Optional[Path] = None) -> dict:
        proc = _run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--json",
                "number,url,title,body,state",
            ],
            cwd=cwd or self.repo_root,
            check=True,
            timeout_seconds=20,
        )
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise GitHubError(
                "Unable to parse gh issue view output", status_code=500
            ) from exc

    def validate_issue_same_repo(self, issue_ref: str) -> int:
        repo = self.repo_info()
        slug_from_input, num = parse_issue_input(issue_ref)
        if slug_from_input and slug_from_input.lower() != repo.name_with_owner.lower():
            raise GitHubError(
                f"Issue must be in this repo ({repo.name_with_owner}); got {slug_from_input}",
                status_code=400,
            )
        return num

    # ── high-level operations ──────────────────────────────────────────────────
    def status_payload(self) -> dict:
        link = self.read_link_state()
        gh_ok = self.gh_available()
        authed = self.gh_authenticated() if gh_ok else False
        repo: Optional[RepoInfo] = None
        if authed:
            try:
                repo = self.repo_info()
            except Exception:
                repo = None
        branch = self.current_branch()
        clean = self.is_clean()
        is_worktree = (self.repo_root / ".git").is_file()
        pr = None
        if authed:
            pr = self.pr_for_branch(branch=branch) or None
        payload = {
            "gh": {"available": gh_ok, "authenticated": authed},
            "repo": (
                {
                    "nameWithOwner": repo.name_with_owner,
                    "url": repo.url,
                    "defaultBranch": repo.default_branch,
                }
                if repo
                else None
            ),
            "git": {"branch": branch, "clean": clean, "is_worktree": is_worktree},
            "link": link or {},
            "pr": pr,
        }
        if pr and pr.get("url"):
            url = pr["url"]
            payload["pr_links"] = {
                "url": url,
                "files": f"{url}/files",
                "checks": f"{url}/checks",
            }
        return payload

    def link_issue(self, issue_ref: str) -> dict:
        number = self.validate_issue_same_repo(issue_ref)
        issue = self.issue_view(number=number)
        repo = self.repo_info()
        state = self.read_link_state()
        state["repo"] = {"nameWithOwner": repo.name_with_owner, "url": repo.url}
        state["issue"] = {
            "number": issue.get("number"),
            "url": issue.get("url"),
            "title": issue.get("title"),
            "state": issue.get("state"),
        }
        state["updatedAtMs"] = _now_ms()
        return self.write_link_state(state)

    def sync_pr(
        self,
        *,
        draft: bool = True,
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> dict:
        if not self.gh_available():
            raise GitHubError("GitHub CLI (gh) not available", status_code=500)
        if not self.gh_authenticated():
            raise GitHubError(
                "GitHub CLI not authenticated (run `gh auth login`)", status_code=401
            )

        repo = self.repo_info()
        base = repo.default_branch or "main"
        state = self.read_link_state() or {}
        issue_num = ((state.get("issue") or {}) or {}).get("number")
        head_branch = self.current_branch()
        cwd = self.repo_root
        meta = {"mode": "current"}
        # Decide commit behavior
        github_cfg = (
            (self.raw_config.get("github") or {})
            if isinstance(self.raw_config, dict)
            else {}
        )
        commit_mode = str(github_cfg.get("sync_commit_mode", "auto")).lower()
        if commit_mode not in ("none", "auto", "always"):
            commit_mode = "auto"

        dirty = not self.is_clean(cwd=cwd)
        if commit_mode in ("always", "auto") and dirty:
            # Commit/push is handled by the sync agent below.
            pass
        if commit_mode == "none" and dirty:
            raise GitHubError(
                "Uncommitted changes present; commit them before syncing PR.",
                status_code=409,
            )

        # Agentic sync (format/lint/test, commit if needed, push; resolve rebase conflicts if any)
        prompt = _build_sync_agent_prompt(
            repo_root=self.repo_root, branch=head_branch, issue_num=issue_num
        )
        _run_codex_sync_agent(
            repo_root=self.repo_root, raw_config=self.raw_config, prompt=prompt
        )

        # Find/create PR
        pr = self.pr_for_branch(branch=head_branch, cwd=cwd)
        if not pr:
            args = ["gh", "pr", "create", "--base", base]
            if draft:
                args.append("--draft")
            if title:
                args += ["--title", title]
            if body:
                args += ["--body", body]
            else:
                args.append("--fill")
            proc = _run(args, cwd=cwd, check=True, timeout_seconds=60)
            # gh pr create returns URL on stdout typically
            url = (
                (proc.stdout or "").strip().splitlines()[-1].strip()
                if proc.stdout
                else ""
            )
            pr = {
                "url": url,
                "state": "OPEN",
                "isDraft": bool(draft),
                "headRefName": head_branch,
                "baseRefName": base,
            }
        pr_url = pr.get("url") if isinstance(pr, dict) else None

        state["repo"] = {"nameWithOwner": repo.name_with_owner, "url": repo.url}
        state["baseBranch"] = base
        state["headBranch"] = head_branch
        if pr_url:
            state["pr"] = {
                "number": pr.get("number"),
                "url": pr_url,
                "state": pr.get("state"),
                "isDraft": pr.get("isDraft"),
                "title": pr.get("title"),
                "headRefName": pr.get("headRefName") or head_branch,
                "baseRefName": pr.get("baseRefName") or base,
            }
        state["updatedAtMs"] = _now_ms()
        self.write_link_state(state)

        out = {
            "status": "ok",
            "repo": repo.name_with_owner,
            "mode": "current",
            "meta": meta,
            "pr": pr,
        }
        if pr_url:
            out["links"] = {
                "url": pr_url,
                "files": f"{pr_url}/files",
                "checks": f"{pr_url}/checks",
            }
        return out
