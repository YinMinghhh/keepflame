"""FileSecretResolver rejects world-readable or foreign secret files."""

from pathlib import Path

import pytest

from keepflame.capabilities.secrets import FileSecretResolver, SecretReadError
from tests.helpers import write_secret


def test_mode_0600_secret_file_is_readable(tmp_path: Path) -> None:
    path = write_secret(tmp_path / "ok.secret", "token-value\n")
    assert FileSecretResolver().read_text(str(path)) == "token-value\n"


@pytest.mark.parametrize("mode", [0o644, 0o664, 0o666, 0o640, 0o604])
def test_group_or_world_accessible_secret_files_are_rejected(tmp_path: Path, mode: int) -> None:
    path = tmp_path / f"wide-{mode:o}.secret"
    path.write_text("must-not-leak\n")
    path.chmod(mode)
    with pytest.raises(SecretReadError, match="must not be group or world accessible") as caught:
        FileSecretResolver().read_text(str(path))
    assert "must-not-leak" not in str(caught.value)
    assert str(path) in str(caught.value)


def test_relative_secret_path_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    write_secret(tmp_path / "rel.secret", "token\n")
    with pytest.raises(SecretReadError, match="must be absolute"):
        FileSecretResolver().read_text("rel.secret")


def test_missing_secret_file_is_rejected_without_echoing_a_fabricated_value(tmp_path: Path) -> None:
    missing = tmp_path / "absent.secret"
    with pytest.raises(SecretReadError, match="unreadable") as caught:
        FileSecretResolver().read_text(str(missing))
    assert "fabricated-secret" not in str(caught.value)


def test_symlink_secret_path_is_rejected(tmp_path: Path) -> None:
    target = write_secret(tmp_path / "real.secret", "token\n")
    link = tmp_path / "link.secret"
    link.symlink_to(target)
    with pytest.raises(SecretReadError) as caught:
        FileSecretResolver().read_text(str(link))
    assert "token" not in str(caught.value)
