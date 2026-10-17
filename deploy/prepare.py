#!/usr/bin/env python3
"""Offline release and Mac deployment preparation; never install or start anything."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from zoneinfo import ZoneInfo


class PreparationError(ValueError):
    """A bounded preparation step was refused; messages never contain source data."""


SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
ROOT_FILES = {
    ".dockerignore", ".gitignore", "AGENTS.md", "README.md", "Dockerfile",
    "compose.yaml", "pyproject.toml", "uv.lock",
}
DEPLOY_FILES = {
    "prepare.py", "README.md", "keepflame.server.toml.example",
    "com.keepflame.runner.plist.example",
}
BLOCKED_COMPONENTS = {
    ".git", ".env", ".venv", "venv", "__pycache__", ".pytest_cache",
    "profiles", "profile", "cookies", "credentials", "private", "accounts",
    "state", "logs", "cache", "backups", "runtime", "var", "secrets",
}
OUTPUT_BLOCKED_COMPONENTS = BLOCKED_COMPONENTS - {"private", "var", "cache", "runtime"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _commit(value: str) -> str:
    if not SHA.fullmatch(value):
        raise PreparationError("commit must be a complete lowercase 40-character SHA")
    return value


def _absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in str(path)):
        raise PreparationError("paths must be absolute, without traversal or control characters")
    return path


def _no_symlink_ancestors(path: Path) -> None:
    for candidate in reversed((path, *path.parents)):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PreparationError("symlink paths are not accepted")


def _safe_source_path(name: str) -> None:
    path = PurePosixPath(name)
    if (not name or len(name) > 200 or not re.fullmatch(r"[A-Za-z0-9._/-]+", name)
            or path.is_absolute() or ".." in path.parts or str(path) != name):
        raise PreparationError("unsafe tracked path")
    if any(part.lower() in BLOCKED_COMPONENTS or part.lower().startswith(".env")
           for part in path.parts):
        raise PreparationError("runtime or credential path is not distributable")
    if name in ROOT_FILES:
        return
    if path.parts[0] in {"src", "tests"} and path.suffix in {".py", ".md"}:
        return
    if path.parts[0] == "config" and len(path.parts) == 2:
        if path.name == "README.md" or path.name.endswith(".example.toml"):
            return
    if path.parts[0] == "deploy" and len(path.parts) == 2 and path.name in DEPLOY_FILES:
        return
    raise PreparationError("tracked path is outside the reviewed source allowlist")


def _git(repo: Path, *args: str) -> bytes:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise PreparationError("git is not installed in the system executable path")
    environment = {
        "PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        completed = subprocess.run(
            [executable, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
             "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreparationError("local git inspection failed") from exc
    if completed.returncode:
        raise PreparationError("local git inspection refused the repository or commit")
    return completed.stdout


def _clean_head(repo: Path, commit: str) -> None:
    if _git(repo, "rev-parse", "--verify", "HEAD").decode().strip() != commit:
        raise PreparationError("the frozen commit must equal the current HEAD")
    # Reject unsafe staged paths before git status might hash their working copy.
    for record in _git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        try:
            attributes, name = record.split(b"\t", 1)
            mode, _object_id, stage = attributes.split()
            _safe_source_path(name.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise PreparationError("unsafe Git index metadata") from exc
        if mode not in {b"100644", b"100755"} or stage != b"0":
            raise PreparationError("release rejects links or unresolved index entries")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=all"):
        raise PreparationError("release requires a clean tracked and untracked working tree")


def snapshot(repo: Path, commit: str) -> tuple[str, list[tuple[str, bytes]]]:
    """Read approved immutable Git blobs, never account files in the checkout."""
    _commit(commit)
    _absolute(repo)
    _no_symlink_ancestors(repo)
    if _git(repo, "rev-parse", "--show-toplevel").decode().strip() != str(repo):
        raise PreparationError("repo must name the canonical repository root")
    if _git(repo, "rev-parse", "--verify", "HEAD").decode().strip() != commit:
        raise PreparationError("the frozen commit must equal the current HEAD")
    records = _git(repo, "ls-tree", "-r", "-z", "--full-tree", "-l", commit).split(b"\0")
    approved: list[tuple[str, str, int]] = []
    for record in records:
        if not record:
            continue
        try:
            attributes, encoded_name = record.split(b"\t", 1)
            mode, kind, object_id, encoded_size = attributes.split()
            name = encoded_name.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise PreparationError("invalid Git tree metadata") from exc
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise PreparationError("links, submodules and non-regular tracked entries are forbidden")
        _safe_source_path(name)
        try:
            size = int(encoded_size)
        except ValueError as exc:
            raise PreparationError("invalid Git blob size") from exc
        if size < 0 or size > MAX_FILE:
            raise PreparationError("tracked source exceeds the per-file size limit")
        approved.append((name, object_id.decode("ascii"), size))
    if len(approved) > 2000 or sum(item[2] for item in approved) > MAX_TOTAL:
        raise PreparationError("tracked source exceeds the release size limit")
    if not {"pyproject.toml", "uv.lock", "src/keepflame/__main__.py"} <= {x[0] for x in approved}:
        raise PreparationError("release must contain package metadata, lockfile and application source")
    _clean_head(repo, commit)
    files = []
    for name, object_id, size in sorted(approved):
        content = _git(repo, "cat-file", "blob", object_id)
        if len(content) != size or b"\0" in content:
            raise PreparationError("release accepts bounded text source only")
        try:
            content.decode("utf-8")
        except UnicodeError as exc:
            raise PreparationError("release source must be UTF-8 text") from exc
        if re.search(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----", content):
            raise PreparationError("private key material is forbidden in a release")
        files.append((name, content))
    _clean_head(repo, commit)
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    return tree, files


def _json(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def make_bundle(commit: str, tree: str, files: list[tuple[str, bytes]]) -> dict[str, bytes]:
    _commit(commit)
    _commit(tree)
    buffer = io.BytesIO()
    seen = set()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in sorted(files):
            _safe_source_path(name)
            if name in seen:
                raise PreparationError("duplicate release file")
            seen.add(name)
            info = tarfile.TarInfo(f"{commit}/{name}")
            info.mode = 0o644
            info.mtime = info.uid = info.gid = 0
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    archive_name = f"keepflame-{commit}.tar"
    archive_content = buffer.getvalue()
    manifest = _json({
        "schema": 1, "commit": commit, "tree": tree,
        "archive": {"name": archive_name, "bytes": len(archive_content), "sha256": digest(archive_content)},
        "files": [{"path": name, "bytes": len(content), "sha256": digest(content), "mode": "0644"}
                  for name, content in sorted(files)],
    })
    checksums = f"{digest(archive_content)}  {archive_name}\n{digest(manifest)}  release.json\n".encode()
    return {archive_name: archive_content, "release.json": manifest, "SHA256SUMS": checksums}


def write_new_directory(out: Path, files: dict[str, bytes], *, forbidden: tuple[Path, ...] = ()) -> None:
    """Only a new, explicitly chosen artifact directory; never overwrite or clean up."""
    _absolute(out)
    _no_symlink_ancestors(out)
    if any(out.is_relative_to(path) or path.is_relative_to(out) for path in forbidden):
        raise PreparationError("artifact output must be outside source and runtime directories")
    if any(part.lower() in OUTPUT_BLOCKED_COMPONENTS for part in out.parts):
        raise PreparationError("artifact output cannot be a runtime or credential path")
    lowered = "/".join(part.lower() for part in out.parts)
    if any(segment in lowered for segment in (
        "/library/application support/keepflame", "/library/logs/keepflame",
        "/library/launchagents", "/applications/keepflame",
    )):
        raise PreparationError("artifacts must not be written into an installation or runtime directory")
    if not out.parent.is_dir() or out.exists():
        raise PreparationError("artifact output needs an existing parent and must not already exist")
    if any(Path(name).name != name or name in {".", ".."} for name in files):
        raise PreparationError("invalid artifact filename")
    out.mkdir(mode=0o700)
    for name, content in files.items():
        descriptor = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)


def _read_artifact(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        raise PreparationError("artifact must be a bounded regular file with no links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise PreparationError("artifact exceeds its size limit")
    return content


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError("duplicate manifest member")
        result[key] = value
    return result


def verify_bundle(bundle: Path) -> dict[str, object]:
    """Verify without extracting, importing application code or touching runtime paths."""
    _absolute(bundle)
    _no_symlink_ancestors(bundle)
    try:
        manifest_bytes = _read_artifact(bundle / "release.json", MAX_FILE)
        manifest = json.loads(manifest_bytes, object_pairs_hook=_unique_object)
        commit = _commit(manifest["commit"])
        _commit(manifest["tree"])
        archive_name = f"keepflame-{commit}.tar"
        if type(manifest["schema"]) is not int or manifest["schema"] != 1 or manifest["archive"]["name"] != archive_name:
            raise PreparationError("unsupported release manifest")
        archive_bytes = _read_artifact(bundle / archive_name, MAX_TOTAL + 4 * 1024 * 1024)
        if (manifest["archive"]["sha256"] != digest(archive_bytes)
                or manifest["archive"]["bytes"] != len(archive_bytes)):
            raise PreparationError("release archive digest or size mismatch")
        expected_checksums = f"{digest(archive_bytes)}  {archive_name}\n{digest(manifest_bytes)}  release.json\n".encode()
        if _read_artifact(bundle / "SHA256SUMS", 4096) != expected_checksums:
            raise PreparationError("release checksum manifest mismatch")
        expected = {}
        if not isinstance(manifest["files"], list) or not 1 <= len(manifest["files"]) <= 2000:
            raise PreparationError("invalid manifest file count")
        for item in manifest["files"]:
            name = item["path"]
            _safe_source_path(name)
            if name in expected or item["mode"] != "0644":
                raise PreparationError("duplicate or unsupported manifest file")
            if (type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_FILE
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise PreparationError("invalid manifest file size or digest")
            expected[name] = item
        if (not {"pyproject.toml", "uv.lock", "src/keepflame/__main__.py"} <= set(expected)
                or sum(item["bytes"] for item in expected.values()) > MAX_TOTAL):
            raise PreparationError("release must contain a bounded complete package")
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
            for member in archive:
                if not member.name.startswith(commit + "/") or not member.isreg() or member.linkname:
                    raise PreparationError("release contains a link or unexpected archive path")
                name = member.name[len(commit) + 1:]
                _safe_source_path(name)
                if name not in expected or name in seen or not 0 <= member.size <= MAX_FILE:
                    raise PreparationError("release contains duplicate or unexpected files")
                item = expected[name]
                if member.mode != 0o644 or member.size != item["bytes"]:
                    raise PreparationError("release member size or permission mismatch")
                stream = archive.extractfile(member)
                if stream is None or digest(stream.read(MAX_FILE + 1)) != item["sha256"]:
                    raise PreparationError("release member digest mismatch")
                seen.add(name)
        if seen != set(expected):
            raise PreparationError("release is missing a manifest file")
    except (KeyError, TypeError, ValueError, OSError, tarfile.TarError) as exc:
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError("release artifacts are missing or malformed") from exc
    return {"verified": True, "commit": commit, "tree": manifest["tree"], "files": len(seen),
            "archive_sha256": digest(archive_bytes), "extracted": False}


def layout(server_home: Path, commit: str) -> dict[str, Path]:
    _absolute(server_home)
    _commit(commit)
    if server_home == Path("/"):
        raise PreparationError("server-home must identify an individual non-root user's home")
    release = server_home / "Applications/KeepFlame/releases" / commit
    runtime = server_home / "Library/Application Support/KeepFlame"
    return {
        "release": release, "venv": release / ".venv", "runtime": runtime,
        "config": runtime / "config.toml", "state": runtime / "state",
        "database": runtime / "state/keepflame.sqlite3", "profiles": runtime / "profiles",
        "profile": runtime / "profiles/joinquant-primary",
        "logs": server_home / "Library/Logs/KeepFlame",
        "launchagent": server_home / "Library/LaunchAgents/com.keepflame.runner.plist",
    }


def render_plan(server_home: Path, commit: str) -> tuple[dict[str, object], dict[str, bytes]]:
    paths = layout(server_home, commit)
    arguments = [str(paths["venv"] / "bin/python"), "-I", "-B", "-m", "keepflame", "run",
                 "--config", str(paths["config"])]
    agent = {
        "Label": "com.keepflame.runner", "Disabled": True, "RunAtLoad": False,
        "KeepAlive": True, "ThrottleInterval": 300, "ExitTimeOut": 240, "Umask": 0o077,
        "LimitLoadToSessionType": "Aqua", "ProgramArguments": arguments,
        "WorkingDirectory": str(paths["release"]),
        "EnvironmentVariables": {"TZ": "Asia/Shanghai", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": str(paths["logs"] / "runner.out.jsonl"),
        "StandardErrorPath": str(paths["logs"] / "runner.err.jsonl"),
    }
    # JSON strings form valid TOML basic strings for these control-free paths.
    quote = lambda path: json.dumps(str(path), ensure_ascii=False)
    config = [
        "# Server-only candidate: every task is disabled; no credentials are included.",
        "# Recreate the venv and enroll/login locally on the server after authorization.",
        "[storage]", f"path = {quote(paths['database'])}", "",
    ]
    for flame_id, task_name in (("joinquant-primary", "checkin"), ("joinquant-reading-primary", "reading")):
        if task_name == "checkin":
            timing = [
                "# Each day draws one uniform start in [09:00, 11:00), kept across restarts.",
                '# A fixed `first_check = "09:00"` (instead of the window keys) still works.',
                'first_window_start = "09:00"', 'first_window_end = "11:00"',
                "# FAILED runs retry after 30, 60, 120 minutes, then stop for the day with one alert.",
                "retry_minutes = [30, 60, 120]",
            ]
        else:
            timing = [
                "# Drawn independently, but never earlier than 5 minutes after check-in that day.",
                'first_window_start = "09:00"', 'first_window_end = "11:00"',
                'after_flame = "joinquant-primary"', "min_gap_minutes = 5",
                "retry_minutes = [30, 60, 120]",
            ]
        config.extend([
            "[[flames]]", f'id = "{flame_id}"', 'keeper = "joinquant"', 'account_id = "primary"',
            "enabled = false", "", "[flames.schedule]", 'timezone = "Asia/Shanghai"',
            *timing, "recheck_minutes = 60", 'reminder_at = "20:00"',
            'stop_at = "23:00"', "", "[flames.settings]", 'mode = "browser"',
            f'task = "{task_name}"', f"browser_profile_dir = {quote(paths['profile'])}",
            "browser_headless = false",
        ])
        if task_name == "reading":
            config.extend([
                "# Articles are chosen by anonymous read-only discovery; no fixed list is needed.",
                'reading_discovery = true',
                'reading_discovery_max_requests = 30',
                '# reading_wait_seconds = 50',
                '# reading_scroll = true',
            ])
        config.append("")
    config.extend([
        "# Email delivery exists and stays off. This candidate is not a live mailbox.",
        "[notify.email]",
        "enabled = false",
        'host = "smtp.example.invalid"',
        "port = 587",
        'security = "starttls"',
        'username = "keepflame@example.invalid"',
        f"password_file = {quote(paths['runtime'] / 'secrets/smtp.password')}",
        'from = "keepflame@example.invalid"',
        'to = ["operator@example.invalid"]',
        "timeout_seconds = 15",
        "",
        "# Slack delivery exists and stays off. This candidate holds no real webhook address.",
        "[notify.slack]",
        "enabled = false",
        f"webhook_file = {quote(paths['runtime'] / 'secrets/slack-webhook.url')}",
        "timeout_seconds = 15",
        "",
    ])
    report = {"dry_run": True, "installed": False, "tasks_enabled": False, "execution_enabled": False,
              "timezone": "Asia/Shanghai", "commit": commit, "paths": {k: str(v) for k, v in paths.items()},
              "program_arguments": arguments, "automatic_restart": False,
              "unverified": ["server dependencies", "GUI login", "normal network access", "platform permission",
                             "server-only enrollment/login", "reading qualification and claim", "live scheduler recovery"]}
    return report, {"deployment-plan.json": _json(report),
                    "keepflame.server.toml.example": ("\n".join(config) + "\n").encode(),
                    "com.keepflame.runner.plist.example": plistlib.dumps(agent, sort_keys=True)}


def _metadata(path: Path, *, private: bool, directory: bool) -> dict[str, object]:
    """One lstat only: never list profiles, follow links or open their contents."""
    try:
        _no_symlink_ancestors(path.parent)
        item = path.lstat()
    except FileNotFoundError:
        return {"present": False, "ready": False, "reason": "missing"}
    except (OSError, PreparationError):
        return {"present": None, "ready": False, "reason": "unreadable_or_symlink_parent"}
    regular = stat.S_ISDIR(item.st_mode) if directory else stat.S_ISREG(item.st_mode)
    permissions = stat.S_IMODE(item.st_mode)
    ready = (regular and item.st_uid == os.getuid() and not (permissions & (0o077 if private else 0o022))
             and (directory or item.st_nlink == 1))
    return {"present": True, "ready": ready, "kind": "directory" if stat.S_ISDIR(item.st_mode) else
            "file" if stat.S_ISREG(item.st_mode) else "link_or_special", "mode": f"{permissions:04o}",
            "owned_by_current_user": item.st_uid == os.getuid()}


def preflight(server_home: Path, commit: str, *, chrome: Path | None = None) -> dict[str, object]:
    """Local metadata and installed-package metadata only; no processes or network."""
    paths = layout(server_home, commit)
    chrome = chrome or Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    _absolute(chrome)
    dependencies = {}
    for package in ("playwright", "Pillow", "numpy"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    try:
        ZoneInfo("Asia/Shanghai")
        timezone_available = True
    except (KeyError, ValueError):
        timezone_available = False
    try:
        console_user = Path("/dev/console").stat().st_uid == os.getuid()
    except OSError:
        console_user = False
    checks: dict[str, object] = {
        "macos": platform.system() == "Darwin", "non_root": os.geteuid() != 0,
        "home_matches_current_user": server_home == Path.home(),
        "python_at_least_3_13": sys.version_info >= (3, 13), "python_version": platform.python_version(),
        "running_from_release_venv": Path(sys.prefix) == paths["venv"],
        "package_versions": dependencies, "timezone_available": timezone_available,
        "console_owned_by_current_user": console_user,
    }
    metadata = {}
    for name in ("release", "runtime", "state", "profiles", "profile", "logs", "config", "database"):
        metadata[name] = _metadata(paths[name], private=name != "release", directory=name not in {"config", "database"})
    try:
        _no_symlink_ancestors(chrome)
        info = chrome.lstat()
        chrome_present = stat.S_ISREG(info.st_mode) and bool(info.st_mode & 0o111)
    except (OSError, PreparationError):
        chrome_present = False
    checks["chrome_executable_present"] = chrome_present
    return {"dry_run": True, "runtime_started": False, "runtime_contents_read": False,
            "ready_for_live_execution": False, "checks": checks, "path_metadata": metadata,
            "manual_checks_required": ["lockfile-installed versions", "GUI usability after lock/sleep/restart",
                                       "profile enrollment and local login", "network and platform permission",
                                       "one approved live trial", "tasks and execution remain disabled"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    release = commands.add_parser("release", help="inspect or write a clean frozen source release")
    release.add_argument("--repo", type=Path, default=Path.cwd())
    release.add_argument("--commit", required=True)
    plan = commands.add_parser("plan", help="render candidates without installing")
    check = commands.add_parser("preflight", help="inspect local metadata; never run keepflame health")
    for command in (plan, check):
        command.add_argument("--server-home", type=Path, required=True)
        command.add_argument("--commit", required=True)
    check.add_argument("--chrome", type=Path)
    for command in (release, plan):
        command.add_argument("--write", action="store_true", help="write only to a new artifact directory")
        command.add_argument("--out", type=Path)
    verify = commands.add_parser("verify", help="verify release digests and archive members without extraction")
    verify.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"release", "plan"} and bool(args.write) != bool(args.out):
            raise PreparationError("--write and --out must be supplied together")
        if args.command == "release":
            tree, files = snapshot(args.repo, args.commit)
            artifacts = make_bundle(args.commit, tree, files)
            report = {"dry_run": not args.write, "commit": args.commit, "tree": tree,
                      "files": len(files), "source_bytes": sum(len(content) for _, content in files),
                      "archive_sha256": digest(artifacts[f"keepflame-{args.commit}.tar"])}
            if args.write:
                write_new_directory(args.out, artifacts, forbidden=(args.repo,))
                report["out"] = str(args.out)
        elif args.command == "verify":
            report = verify_bundle(args.bundle)
        elif args.command == "plan":
            report, artifacts = render_plan(args.server_home, args.commit)
            if args.write:
                paths = layout(args.server_home, args.commit)
                write_new_directory(args.out, artifacts, forbidden=(paths["runtime"], paths["release"].parents[1],
                                                                     paths["logs"], paths["launchagent"].parent))
                report["artifacts_written"] = True
                report["out"] = str(args.out)
        else:
            report = preflight(args.server_home, args.commit, chrome=args.chrome)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (PreparationError, OSError) as exc:
        message = str(exc) if isinstance(exc, PreparationError) else "local preparation could not complete"
        print(json.dumps({"error": message, "installed": False, "runtime_started": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
