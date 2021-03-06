"""Interface for accessing the file system with automatic caching.

The idea is to cache the results of any file system state reads during
a single transaction. This has two main benefits:

* This avoids redundant syscalls, as we won't perform the same OS
  operations multiple times.

* This makes it easier to reason about concurrent FS updates, as different
  operations targeting the same paths can't report different state during
  a transaction.

Note that this only deals with reading state, not writing.

Properties maintained by the API:

* The contents of the file are always from the same or later time compared
  to the reported mtime of the file, even if mtime is queried after reading
  a file.

* Repeating an operation produces the same result as the first one during
  a transaction.

* Call flush() to start a new transaction (flush the caches).

The API is a bit limited. It's easy to add new cached operations, however.
You should perform all file system reads through the API to actually take
advantage of the benefits.
"""

import functools
import os
import stat
from typing import Dict, List, Optional, Tuple, TypeVar
from mypy.util import read_with_python_encoding


class FileSystemMetaCache:
    def __init__(self) -> None:
        self.stat = functools.lru_cache(maxsize=None)(self._stat)
        self.listdir = functools.lru_cache(maxsize=None)(self._listdir)
        # lru_cache doesn't handle exceptions, so we need special caches for them.
        self.stat_error_cache = {}  # type: Dict[str, Exception]
        self.listdir_error_cache = {}  # type: Dict[str, Exception]

    def flush(self) -> None:
        """Start another transaction and empty all caches."""
        self.stat.cache_clear()
        self.listdir.cache_clear()
        self.stat_error_cache.clear()
        self.listdir_error_cache.clear()

    def _stat(self, path: str) -> os.stat_result:
        if path in self.stat_error_cache:
            raise self.stat_error_cache[path]
        try:
            return os.stat(path)
        except Exception as err:
            self.stat_error_cache[path] = err
            raise

    def _listdir(self, path: str) -> List[str]:
        if path in self.listdir_error_cache:
            raise self.listdir_error_cache[path]
        try:
            return os.listdir(path)
        except Exception as err:
            self.listdir_error_cache[path] = err
            raise err

    def isfile(self, path: str) -> bool:
        try:
            st = self.stat(path)
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode)

    def isfile_case(self, path: str) -> bool:
        """Return whether path exists and is a file.

        On case-insensitive filesystems (like Mac or Windows) this returns
        False if the case of the path's last component does not exactly
        match the case found in the filesystem.
        TODO: We should maybe check the case for some directory components also,
        to avoid permitting wrongly-cased *packages*.
        """
        head, tail = os.path.split(path)
        if not tail:
            res = False
        else:
            try:
                names = self.listdir(head)
                res = tail in names and self.isfile(path)
            except OSError:
                res = False
        return res

    def isdir(self, path: str) -> bool:
        try:
            st = self.stat(path)
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode)

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except FileNotFoundError:
            return False
        return True


class FileSystemCache(FileSystemMetaCache):
    def __init__(self, pyversion: Tuple[int, int]) -> None:
        super().__init__()
        self.pyversion = pyversion
        self.flush()

    def flush(self) -> None:
        """Start another transaction and empty all caches."""
        super().flush()
        self.read_cache = {}  # type: Dict[str, str]
        self.read_error_cache = {}  # type: Dict[str, Exception]
        self.hash_cache = {}  # type: Dict[str, str]

    def read_with_python_encoding(self, path: str) -> str:
        if path in self.read_cache:
            return self.read_cache[path]
        if path in self.read_error_cache:
            raise self.read_error_cache[path]

        # Need to stat first so that the contents of file are from no
        # earlier instant than the mtime reported by self.stat().
        self.stat(path)

        try:
            data, md5hash = read_with_python_encoding(path, self.pyversion)
        except Exception as err:
            self.read_error_cache[path] = err
            raise
        self.read_cache[path] = data
        self.hash_cache[path] = md5hash
        return data

    def md5(self, path: str) -> str:
        if path not in self.hash_cache:
            self.read_with_python_encoding(path)
        return self.hash_cache[path]
