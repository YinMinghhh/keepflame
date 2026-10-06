"""Secret providers that never log or cache secret contents."""

from __future__ import annotations

from pathlib import Path


class SecretReadError(RuntimeError):
    pass


class FileSecretResolver:
    def read_text(self, reference: str) -> str:
        path = Path(reference)
        if not path.is_absolute():
            raise SecretReadError("secret file path must be absolute")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SecretReadError(f"secret file is unreadable: {path}") from exc

