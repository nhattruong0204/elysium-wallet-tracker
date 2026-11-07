"""Private local SQLite file guard shared by PAPER and authentication stores."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from .paper import TradeRefused


class PrivateSqlite:
    def __init__(self, path: str):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        parent = self.path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode):
            raise TradeRefused("Paper journal directory cannot be a symlink")
        self._directory_identity = (parent.st_dev, parent.st_ino)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise TradeRefused("Paper journal directory must be owned by this user with mode 0700")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            self._identity = (st.st_dev, st.st_ino)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) & 0o077
            ):
                raise TradeRefused("Paper journal must be a private regular file with mode 0600")
        finally:
            os.close(fd)

    def _check_private_path(self) -> None:
        try:
            directory = self.path.parent.lstat()
            file = self.path.lstat()
            if (
                not stat.S_ISDIR(directory.st_mode)
                or directory.st_uid != os.getuid()
                or stat.S_IMODE(directory.st_mode) & 0o077
                or (directory.st_dev, directory.st_ino) != self._directory_identity
                or not stat.S_ISREG(file.st_mode)
                or file.st_uid != os.getuid()
                or stat.S_IMODE(file.st_mode) & 0o077
                or file.st_nlink != 1
                or (file.st_dev, file.st_ino) != self._identity
            ):
                raise TradeRefused("Private paper journal path or permissions changed")
        except OSError:
            raise TradeRefused("Private paper journal unavailable") from None

    @contextmanager
    def _connect(self):
        self._check_private_path()
        conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=2)
        try:
            self._check_private_path()
            conn.execute("PRAGMA trusted_schema=OFF")
            with conn:
                yield conn
        finally:
            conn.close()
