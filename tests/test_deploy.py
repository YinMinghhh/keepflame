"""Offline deployment artifacts must never inspect or start a real runtime."""

import builtins
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tarfile
import tomllib

import pytest


SPEC = importlib.util.spec_from_file_location(
    "keepflame_deployment_prepare", Path(__file__).resolve().parents[1] / "deploy/prepare.py"
)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)
COMMIT = "1" * 40


def git(repo, *arguments):
    result = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.name=Synthetic Test", "-c", "user.email=synthetic@example.invalid",
         "-C", str(repo), *arguments],
        env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return result.stdout.decode().strip()


def commit_all(repo):
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthetic source")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "src/keepflame").mkdir(parents=True)
    (repo / "src/keepflame/__main__.py").write_text('print("synthetic")\n')
    (repo / "pyproject.toml").write_text('[project]\nname="synthetic"\nversion="0.0.0"\n')
    (repo / "uv.lock").write_text("version = 1\n")
    (repo / "README.md").write_text("Synthetic release only.\n")
    return repo, commit_all(repo)


def write_bundle(tmp_path, repository):
    repo, commit = repository
    tree, files = prepare.snapshot(repo, commit)
    out = tmp_path / "release-artifacts"
    prepare.write_new_directory(out, prepare.make_bundle(commit, tree, files), forbidden=(repo,))
    return out


def test_release_is_deterministic_clean_source_with_complete_hashes(tmp_path, repository):
    repo, commit = repository
    tree, files = prepare.snapshot(repo, commit)
    first = prepare.make_bundle(commit, tree, files)
    assert first == prepare.make_bundle(commit, tree, files)
    out = tmp_path / "artifacts"
    prepare.write_new_directory(out, first, forbidden=(repo,))
    result = prepare.verify_bundle(out)
    assert result["verified"] and result["commit"] == commit and not result["extracted"]
    assert result["files"] == 4
    assert out.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in out.iterdir())
    with tarfile.open(out / f"keepflame-{commit}.tar") as archive:
        assert {x.name for x in archive} == {f"{commit}/{name}" for name, _ in files}
        assert all(x.isreg() and x.mode == 0o644 for x in archive)


def test_default_release_does_not_write(tmp_path, repository, capsys):
    repo, commit = repository
    before = sorted(tmp_path.iterdir())
    assert prepare.main(["release", "--repo", str(repo), "--commit", commit]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert sorted(tmp_path.iterdir()) == before


def test_release_accepts_its_own_reviewed_tools_and_test_source(repository):
    repo, _ = repository
    stage = Path(__file__).resolve().parents[1]
    (repo / "deploy").mkdir()
    (repo / "tests").mkdir()
    for name in prepare.DEPLOY_FILES:
        (repo / "deploy" / name).write_bytes((stage / "deploy" / name).read_bytes())
    (repo / "tests/test_deploy.py").write_bytes(Path(__file__).read_bytes())
    commit = commit_all(repo)
    _, files = prepare.snapshot(repo, commit)
    assert {"deploy/prepare.py", "tests/test_deploy.py"} <= {name for name, _ in files}


@pytest.mark.parametrize("dirty_name", ["README.md", "untracked-account-note.txt"])
def test_release_refuses_any_dirty_worktree_without_exposing_contents(repository, dirty_name):
    repo, commit = repository
    (repo / dirty_name).write_text("SYNTHETIC-PRIVATE-SENTINEL")
    with pytest.raises(prepare.PreparationError, match="clean") as result:
        prepare.snapshot(repo, commit)
    assert "SYNTHETIC" not in str(result.value) and dirty_name not in str(result.value)


def test_release_reads_git_blobs_not_ignored_runtime_material(repository):
    repo, _ = repository
    (repo / ".gitignore").write_text("/.env\n/profiles/\n")
    commit = commit_all(repo)
    (repo / ".env").write_text("SYNTHETIC-PRIVATE-SENTINEL")
    (repo / "profiles").mkdir()
    (repo / "profiles/Cookies").write_bytes(b"SYNTHETIC-PRIVATE-SENTINEL")
    tree, files = prepare.snapshot(repo, commit)
    assert "SYNTHETIC-PRIVATE-SENTINEL" not in repr(files)
    assert not any("profile" in name or name == ".env" for name, _ in files)
    assert tree


@pytest.mark.parametrize("name", [
    ".env", "config/live.toml", "src/keepflame/profiles/credentials.py",
    "tests/Cookies", "tests/private/fixture.py", "src/key.pem", "evidence/session.json",
])
def test_release_rejects_sensitive_or_unreviewed_tracked_paths(repository, name):
    repo, _ = repository
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SYNTHETIC-PRIVATE-SENTINEL")
    commit = commit_all(repo)
    with pytest.raises(prepare.PreparationError) as result:
        prepare.snapshot(repo, commit)
    assert "SYNTHETIC" not in str(result.value)


def test_release_rejects_tracked_symlink(repository):
    repo, _ = repository
    (repo / "src/link.py").symlink_to("keepflame/__main__.py")
    commit = commit_all(repo)
    with pytest.raises(prepare.PreparationError, match="links"):
        prepare.snapshot(repo, commit)


def test_release_rejects_submodule_metadata(repository):
    repo, commit = repository
    git(repo, "update-index", "--add", "--cacheinfo", "160000", commit, "src/submodule")
    git(repo, "commit", "-qm", "synthetic gitlink")
    with pytest.raises(prepare.PreparationError, match="submodules"):
        prepare.snapshot(repo, git(repo, "rev-parse", "HEAD"))


def test_staged_sensitive_path_rejected_before_status_or_blob_read(repository, monkeypatch):
    repo, commit = repository
    (repo / ".env").write_text("SYNTHETIC-PRIVATE-SENTINEL")
    git(repo, "add", ".env")
    calls = []
    original = prepare._git

    def observed(source, *args):
        calls.append(args[0])
        return original(source, *args)

    monkeypatch.setattr(prepare, "_git", observed)
    with pytest.raises(prepare.PreparationError):
        prepare.snapshot(repo, commit)
    assert "status" not in calls and "cat-file" not in calls


def test_release_rejects_private_key_even_in_source(repository):
    repo, _ = repository
    (repo / "src/example.py").write_text("-----BEGIN " + "PRIVATE KEY-----\nsynthetic\n")
    commit = commit_all(repo)
    with pytest.raises(prepare.PreparationError, match="private key"):
        prepare.snapshot(repo, commit)


def test_release_requires_exact_current_commit(repository):
    repo, commit = repository
    with pytest.raises(prepare.PreparationError, match="complete"):
        prepare.snapshot(repo, commit[:7])
    (repo / "README.md").write_text("Changed synthetic source.\n")
    commit_all(repo)
    with pytest.raises(prepare.PreparationError, match="HEAD"):
        prepare.snapshot(repo, commit)


def test_release_rechecks_clean_head_after_reading(repository, monkeypatch):
    repo, commit = repository
    original = prepare._git
    touched = False

    def changed(source, *args):
        nonlocal touched
        result = original(source, *args)
        if args[0] == "cat-file" and not touched:
            (repo / "README.md").write_text("Changed during freeze.\n")
            touched = True
        return result

    monkeypatch.setattr(prepare, "_git", changed)
    with pytest.raises(prepare.PreparationError, match="clean"):
        prepare.snapshot(repo, commit)


def test_artifact_output_refuses_existing_directory_and_source(tmp_path, repository):
    repo, _ = repository
    existing = tmp_path / "existing-evidence"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("preserve")
    with pytest.raises(prepare.PreparationError):
        prepare.write_new_directory(existing, {"keep.txt": b"replace"})
    assert sentinel.read_text() == "preserve"
    with pytest.raises(prepare.PreparationError):
        prepare.write_new_directory(repo / "new-artifacts", {"a": b"b"}, forbidden=(repo,))
    assert not (repo / "new-artifacts").exists()


def test_artifact_output_rejects_symlink_parent_and_runtime_path(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    for out in (link / "new", tmp_path / "profiles"):
        with pytest.raises(prepare.PreparationError):
            prepare.write_new_directory(out, {"safe.txt": b"safe"})
    assert not list(real.iterdir())


def test_artifact_output_refuses_installation_locations_even_for_release(tmp_path):
    install_root = tmp_path / "synthetic-home/Library/Application Support/KeepFlame"
    install_root.mkdir(parents=True)
    with pytest.raises(prepare.PreparationError, match="installation"):
        prepare.write_new_directory(install_root / "new-bundle", {"safe.txt": b"safe"})
    assert not list(install_root.iterdir())


def test_verifier_rejects_modified_archive(tmp_path, repository):
    bundle = write_bundle(tmp_path, repository)
    archive = next(bundle.glob("*.tar"))
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(prepare.PreparationError, match="digest or size"):
        prepare.verify_bundle(bundle)


def resign_archive(bundle, content):
    manifest = json.loads((bundle / "release.json").read_text())
    archive = bundle / manifest["archive"]["name"]
    archive.write_bytes(content)
    manifest["archive"]["sha256"] = prepare.digest(content)
    manifest["archive"]["bytes"] = len(content)
    manifest_bytes = prepare._json(manifest)
    (bundle / "release.json").write_bytes(manifest_bytes)
    (bundle / "SHA256SUMS").write_text(
        f"{prepare.digest(content)}  {archive.name}\n{prepare.digest(manifest_bytes)}  release.json\n"
    )


@pytest.mark.parametrize("mutation", ["symlink", "traversal", "duplicate"])
def test_verifier_rejects_unsafe_tar_even_with_matching_manifest_hash(tmp_path, repository, mutation):
    bundle = write_bundle(tmp_path, repository)
    commit = repository[1]
    source = next(bundle.glob("*.tar")).read_bytes()
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as original:
        with tarfile.open(fileobj=out, mode="w") as archive:
            for member in original:
                archive.addfile(member, original.extractfile(member))
            extra = tarfile.TarInfo(f"{commit}/README.md")
            if mutation == "symlink":
                extra.type, extra.linkname = tarfile.SYMTYPE, "/SYNTHETIC-NOT-READ"
            elif mutation == "traversal":
                extra.name = f"{commit}/../escape.py"
            archive.addfile(extra)
    resign_archive(bundle, out.getvalue())
    with pytest.raises(prepare.PreparationError):
        prepare.verify_bundle(bundle)
    assert not (tmp_path / "escape.py").exists()


def test_verify_never_follows_artifact_symlink(tmp_path, repository):
    bundle = write_bundle(tmp_path, repository)
    manifest = bundle / "release.json"
    saved = tmp_path / "saved.json"
    manifest.rename(saved)
    manifest.symlink_to(saved)
    with pytest.raises(prepare.PreparationError, match="regular file"):
        prepare.verify_bundle(bundle)


def test_plan_has_disabled_distinct_tasks_and_version_pinned_dry_run_agent(tmp_path):
    server_home = tmp_path / "synthetic-home"
    report, files = prepare.render_plan(server_home, COMMIT)
    config = tomllib.loads(files["keepflame.server.toml.example"].decode())
    assert {x["settings"]["task"] for x in config["flames"]} == {"checkin", "reading"}
    assert all(not x["enabled"] and x["schedule"]["timezone"] == "Asia/Shanghai" for x in config["flames"])
    assert "article_url" not in config["flames"][1]["settings"]
    assert "cookie" not in config["flames"][0]["settings"]
    agent = plistlib.loads(files["com.keepflame.runner.plist.example"])
    assert agent["Disabled"] and not agent["RunAtLoad"] and agent["KeepAlive"]
    assert agent["Umask"] == 0o077 and agent["LimitLoadToSessionType"] == "Aqua"
    # Allow the 120-second reading window, 60-second action budget and browser cleanup.
    assert agent["ExitTimeOut"] == 240
    assert "--execute" not in agent["ProgramArguments"]
    assert "run-once" not in agent["ProgramArguments"]
    assert not {"Sockets", "MachServices", "StartCalendarInterval", "StartInterval"} & agent.keys()
    assert COMMIT in agent["ProgramArguments"][0]
    assert report["dry_run"] and not report["installed"] and not report["tasks_enabled"]
    assert not server_home.exists()


def test_default_plan_writes_nothing_and_refuses_runtime_output(tmp_path, capsys):
    server_home = tmp_path / "synthetic-home"
    assert prepare.main(["plan", "--server-home", str(server_home), "--commit", COMMIT]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert not server_home.exists()
    runtime = prepare.layout(server_home, COMMIT)["runtime"]
    runtime.mkdir(parents=True)
    assert prepare.main(["plan", "--server-home", str(server_home), "--commit", COMMIT,
                         "--write", "--out", str(runtime / "candidate")]) == 1
    assert not (runtime / "candidate").exists()


def test_plan_quotes_paths_without_shell_commands(tmp_path):
    server_home = tmp_path / 'synthetic $() "quoted" home'
    _, files = prepare.render_plan(server_home, COMMIT)
    config = tomllib.loads(files["keepflame.server.toml.example"].decode())
    agent = plistlib.loads(files["com.keepflame.runner.plist.example"])
    assert config["storage"]["path"].startswith(str(server_home))
    assert agent["ProgramArguments"][0].startswith(str(server_home))
    for value in (Path("relative"), Path("/safe/../escape"), Path("/safe\nunsafe")):
        with pytest.raises(prepare.PreparationError):
            prepare.render_plan(value, COMMIT)


def test_checked_in_examples_match_generator_and_remain_disabled():
    _, expected = prepare.render_plan(Path("/Users/keepflame"), "0" * 40)
    root = Path(__file__).resolve().parents[1] / "deploy"
    for name in ("keepflame.server.toml.example", "com.keepflame.runner.plist.example"):
        assert (root / name).read_bytes() == expected[name]


def test_preflight_only_metadata_never_reads_runtime_or_starts_process(tmp_path, monkeypatch):
    server_home = tmp_path / "synthetic-home"
    paths = prepare.layout(server_home, COMMIT)
    for name in ("release", "runtime", "state", "profiles", "profile", "logs"):
        paths[name].mkdir(mode=0o700, parents=True, exist_ok=True)
        paths[name].chmod(0o700)
    for name in ("config", "database"):
        paths[name].write_bytes(b"SYNTHETIC-PRIVATE-SENTINEL")
        paths[name].chmod(0o600)
    cookie_file = paths["profile"] / "Cookies"
    cookie_file.write_bytes(b"SYNTHETIC-PRIVATE-SENTINEL")
    chrome = tmp_path / "synthetic-chrome"
    chrome.write_text("synthetic, must never execute")
    chrome.chmod(0o700)
    before = {name: hashlib.sha256(paths[name].read_bytes()).hexdigest() for name in ("config", "database")}
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("side effect")
        raise AssertionError("preflight must use metadata only")

    with monkeypatch.context() as patch:
        patch.setattr(prepare.subprocess, "run", forbidden)
        patch.setattr(prepare.importlib.metadata, "version", lambda name: "synthetic-installed")
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "open", forbidden)
        patch.setattr(Path, "iterdir", forbidden)
        result = prepare.preflight(server_home, COMMIT, chrome=chrome)
    assert not calls and not result["runtime_started"] and not result["runtime_contents_read"]
    assert result["checks"]["chrome_executable_present"]
    assert all(result["path_metadata"][name]["ready"] for name in ("runtime", "state", "profile", "config", "database"))
    assert not result["ready_for_live_execution"]
    assert before == {name: hashlib.sha256(paths[name].read_bytes()).hexdigest() for name in before}
    assert cookie_file.read_bytes() == b"SYNTHETIC-PRIVATE-SENTINEL"


def test_preflight_flags_symlink_and_broad_permissions_without_following(tmp_path, monkeypatch):
    server_home = tmp_path / "synthetic-home"
    paths = prepare.layout(server_home, COMMIT)
    paths["runtime"].mkdir(parents=True, mode=0o700)
    paths["state"].mkdir(mode=0o755)
    paths["profile"].parent.mkdir(mode=0o700)
    paths["profile"].symlink_to(tmp_path / "unread-secret-profile")
    monkeypatch.setattr(prepare.importlib.metadata, "version", lambda name: "synthetic-installed")
    result = prepare.preflight(server_home, COMMIT, chrome=tmp_path / "missing-chrome")
    assert not result["path_metadata"]["state"]["ready"]
    assert not result["path_metadata"]["profile"]["ready"]
    assert result["path_metadata"]["profile"]["kind"] == "link_or_special"
    assert not result["checks"]["chrome_executable_present"]
    assert not (tmp_path / "unread-secret-profile").exists()
