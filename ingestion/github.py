"""
Clone a public GitHub repository for ingestion.

Supports URLs like:
  https://github.com/owner/repo
  https://github.com/owner/repo/tree/main
  https://github.com/owner/repo/tree/feature-branch
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})

# owner/repo with optional /tree/<branch> (branch may contain slashes for rare cases)
_PATH_RE = re.compile(
    r"^/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)(?:\.git)?"
    r"(?:/tree/(?P<branch>.+))?(?:/.*)?$"
)


@dataclass(frozen=True)
class GitHubRepoRef:
    owner: str
    repo: str
    branch: str | None
    canonical_url: str


def parse_github_url(url: str) -> GitHubRepoRef:
    """Parse and validate a GitHub repository URL."""
    raw = url.strip()
    if not raw:
        raise ValueError("GitHub URL is empty.")

    if raw.startswith("git@github.com:"):
        # git@github.com:owner/repo.git
        path = "/" + raw.split(":", 1)[1].lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        owner, repo, branch = _parse_path(path)
        canonical = f"https://github.com/{owner}/{repo}"
        return GitHubRepoRef(owner, repo, branch, canonical)

    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.netloc.lower() not in _GITHUB_HOSTS:
        raise ValueError("Only github.com repository URLs are supported.")

    owner, repo, branch = _parse_path(parsed.path)
    canonical = f"https://github.com/{owner}/{repo}"
    if branch:
        canonical = f"{canonical}/tree/{branch}"
    return GitHubRepoRef(owner, repo, branch, canonical)


def _parse_path(path: str) -> tuple[str, str, str | None]:
    match = _PATH_RE.match(path)
    if not match:
        raise ValueError(
            "Invalid GitHub URL. Use https://github.com/owner/repo "
            "or https://github.com/owner/repo/tree/branch"
        )
    owner = match.group("owner")
    repo = match.group("repo")
    branch = match.group("branch")
    return owner, repo, branch


def _clone_dir_name(ref: GitHubRepoRef) -> str:
    if ref.branch:
        safe_branch = ref.branch.replace("/", "__")
        return f"{ref.owner}__{ref.repo}__{safe_branch}"
    return f"{ref.owner}__{ref.repo}"


def clone_github_repository(github_url: str, clone_root: str | Path) -> Path:
    """
    Shallow-clone a public GitHub repo into *clone_root* and return the local path.

    Re-cloning the same owner/repo[/branch] removes the previous checkout.
    """
    ref = parse_github_url(github_url)
    root = Path(clone_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    dest = root / _clone_dir_name(ref)
    if dest.exists():
        shutil.rmtree(dest)

    clone_url = f"https://github.com/{ref.owner}/{ref.repo}.git"
    cmd = ["git", "clone", "--depth", "1", clone_url, str(dest)]
    if ref.branch:
        cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref.branch,
            clone_url,
            str(dest),
        ]

    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "git is not installed. Install Git to ingest from GitHub."
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git clone failed").strip()
        raise RuntimeError(f"Failed to clone repository: {detail}")

    return dest
