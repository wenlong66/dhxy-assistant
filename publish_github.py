"""Publish this reviewed source snapshot as a NEW public GitHub repository.

Requires Python 3.11+, Git and GitHub CLI. Uses local gh browser authentication.
Never accepts a token argument, overwrites an existing repository, or force-pushes.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = (
    ".gitignore", ".gitattributes", "README.md", "DESIGN.md", "CHANGELOG.md",
    "PUBLISH.md", "app.py", "quiz_core.py", "publish_github.py",
    "examples/dali_question.json", "tests/test_quiz.py", "TEST_RESULTS.txt",
    "demo_output.json",
)
DESCRIPTION = "DHXY mobile personal assistant: local quiz bank and OpenAI answer core; MaaFramework integration planned."


class PublishError(RuntimeError):
    pass


def run(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args, cwd=ROOT, check=False, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode:
        # Do not echo credential-helper output or entire remote error bodies.
        raise PublishError(f"Command failed ({result.returncode}): {args[0]} {args[1] if len(args) > 1 else ''}. No forced retry.")
    return result


def check_sources() -> None:
    patterns = [
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{25,}"),
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
        re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ]
    for relative in FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise PublishError(f"Missing or unsafe publish file: {relative}")
        text = path.read_text(encoding="utf-8-sig")
        if any(pattern.search(text) for pattern in patterns):
            raise PublishError(f"Possible secret in {relative}; stopped without printing it.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="wenlong66", help="Must match the authenticated personal account")
    parser.add_argument("--repo", default="dhxy-assistant", help="New public repository name")
    parser.add_argument("--check-only", action="store_true", help="Local source scan and tests only; no GitHub access")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise PublishError("Python 3.11+ is required.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", args.owner):
        raise PublishError("Invalid personal account name.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", args.repo):
        raise PublishError("Invalid repository name.")
    check_sources()
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    if args.check_only:
        print("Local checks passed. No GitHub operation or paid API call was made.")
        return 0
    for command in ("git", "gh"):
        if not shutil.which(command):
            raise PublishError(f"Install {command} first; then reopen the terminal. See PUBLISH.md.")
    # Never accidentally add files to a parent project or rewrite existing history.
    if (ROOT / ".git").exists() or run("git", "rev-parse", "--show-toplevel", capture=True, check=False).returncode == 0:
        raise PublishError("This directory is already inside a Git repository. Use a fresh extraction; nothing was overwritten.")

    print(f"Target: {args.owner}/{args.repo} (PUBLIC).", flush=True)
    if run("gh", "auth", "status", "--hostname", "github.com", capture=True, check=False).returncode:
        print("Complete the GitHub browser sign-in on your own computer. Do not paste a token into chat.", flush=True)
        run("gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web")
    account = json.loads(run("gh", "api", "--hostname", "github.com", "user", capture=True).stdout)
    if account.get("login", "").lower() != args.owner.lower():
        raise PublishError(f"Wrong signed-in account: {account.get('login')}. Expected {args.owner}; stopped before creating a repository.")
    login = account["login"]
    full_name = f"{login}/{args.repo}"

    # Refuse existing repositories. A failed lookup is not assumed to mean 404;
    # the separate create command below must succeed before any push is possible.
    lookup = run("gh", "repo", "view", full_name, "--json", "nameWithOwner", capture=True, check=False)
    if lookup.returncode == 0:
        raise PublishError(f"{full_name} already exists. No changes were made to it. Choose a different --repo name.")

    run("git", "init", "-b", "main")
    run("git", "config", "--local", "user.name", login)
    run("git", "config", "--local", "user.email", f"{account['id']}+{login}@users.noreply.github.com")
    # Local repository credential helper only; no token is stored in this project.
    run("git", "config", "--local", "credential.helper", "")
    run("git", "config", "--local", "--add", "credential.helper", "!gh auth git-credential")
    run("git", "add", "--", *FILES)
    staged = set(run("git", "diff", "--cached", "--name-only", capture=True).stdout.splitlines())
    if staged != set(FILES):
        raise PublishError("Staged files differ from the reviewed allowlist. Stopped before repository creation.")
    run("git", "commit", "-m", "feat: initialize DHXY quiz core with local question bank and OpenAI integration")
    local_sha = run("git", "rev-parse", "HEAD", capture=True).stdout.strip()

    # --public is intentional and authorized by the user. Never use force-push.
    run("gh", "repo", "create", full_name, "--public", "--description", DESCRIPTION)
    info = json.loads(run("gh", "repo", "view", full_name, "--json", "nameWithOwner,isPrivate,url", capture=True).stdout)
    if info.get("isPrivate") is not False or info.get("nameWithOwner", "").lower() != full_name.lower():
        raise PublishError("Repository identity/visibility check failed; no code was pushed.")
    run("git", "remote", "add", "origin", info["url"] + ".git")
    run("git", "push", "--set-upstream", "origin", "main")
    remote = run("git", "ls-remote", "--heads", "origin", "main", capture=True).stdout.split()
    if not remote or remote[0] != local_sha:
        raise PublishError("Cannot verify the remote commit; inspect GitHub before retrying.")
    result = {"repository": info["url"], "visibility": "public", "branch": "main", "commit": local_sha}
    (ROOT / "publication-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Verified: public repository created and initial commit pushed.")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PublishError, OSError, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        print("No force-push or automatic destructive cleanup was attempted. See PUBLISH.md.", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Cancelled. Inspect local/remote state before retrying.", file=sys.stderr)
        raise SystemExit(130)
