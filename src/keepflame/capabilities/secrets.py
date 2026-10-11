"""Secret providers that never log or cache secret contents."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class SecretReadError(RuntimeError):
    pass


class FileSecretResolver:
    def read_text(self, reference: str) -> str:
        path = Path(reference)
        if not path.is_absolute():
            raise SecretReadError("secret file path must be absolute")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise SecretReadError(f"secret file is unreadable: {path}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SecretReadError(f"secret path is not a regular file: {path}")
            if info.st_uid != os.getuid():
                raise SecretReadError(f"secret file must belong to this user: {path}")
            if info.st_mode & 0o077:
                raise SecretReadError(
                    f"secret file must not be group or world accessible: {path}"
                )
            if info.st_nlink != 1:
                raise SecretReadError(f"secret file must not be hard-linked: {path}")
            raw = os.read(fd, info.st_size)
        except SecretReadError:
            raise
        except OSError as exc:
            raise SecretReadError(f"secret file is unreadable: {path}") from exc
        finally:
            os.close(fd)
        try:
            return raw.decode("utf-8")
        except UnicodeError as exc:
            raise SecretReadError(f"secret file is unreadable: {path}") from exc
