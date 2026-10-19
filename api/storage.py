"""Where model versions and directory snapshots are kept: a local folder, or a private Azure Blob
container read and written with the managed identity (DefaultAzureCredential). Names are relative,
'/'-separated paths under the store's root."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Protocol

_BLOB_URL = re.compile(r"^https://(?P<account>[a-z0-9]{3,24})\.blob\.core\.windows\.net/(?P<container>[a-z0-9-]{3,63})"
                       r"(?:/(?P<prefix>.*))?$")


class StoreError(RuntimeError):
    """The store could not be read or written."""


class Store(Protocol):
    location: str

    def list(self, prefix: str = "") -> list[str]: ...

    def read(self, name: str) -> bytes: ...

    def write(self, name: str, data: bytes) -> None: ...

    def delete(self, name: str) -> None: ...

    def download(self, name: str, dest: Path) -> None: ...


def _clean(name: str) -> str:
    """A relative name with no '..', drive or leading '/', so nothing outside the store is reached."""
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts or any(p in ("..", "") or ":" in p for p in parts) or name.startswith(("/", "\\")):
        raise StoreError(f"Not a store path: {name!r}")
    return "/".join(parts)


class LocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.location = str(self.root)

    def _path(self, name: str) -> Path:
        return self.root.joinpath(*_clean(name).split("/"))

    def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root
        try:
            if not base.is_dir():
                return []
            return sorted(p.relative_to(self.root).as_posix() for p in base.iterdir() if p.is_file())
        except OSError as e:
            raise StoreError(f"Cannot list {self.location}: {type(e).__name__}") from None

    def read(self, name: str) -> bytes:
        try:
            return self._path(name).read_bytes()
        except OSError as e:
            raise StoreError(f"Cannot read {name} from {self.location}: {type(e).__name__}") from None

    def write(self, name: str, data: bytes) -> None:
        path = self._path(name)
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, path)  # readers never see a half-written file
        except OSError as e:
            raise StoreError(f"Cannot write {name} to {self.location}: {type(e).__name__}") from None

    def delete(self, name: str) -> None:
        try:
            self._path(name).unlink(missing_ok=True)
        except OSError as e:
            raise StoreError(f"Cannot delete {name} from {self.location}: {type(e).__name__}") from None

    def download(self, name: str, dest: Path) -> None:
        data = self.read(name)
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        except OSError as e:
            raise StoreError(f"Cannot write {dest.name}: {type(e).__name__}") from None


class BlobStore:
    """A Blob container, optionally under a path prefix: https://<account>.blob.core.windows.net/<container>[/<path>].
    Signs in with DefaultAzureCredential, which is the managed identity in Azure and the developer's
    az login locally. container_client is injectable for tests."""

    def __init__(self, url: str, container_client=None):
        m = _BLOB_URL.match(url.rstrip("/"))
        if not m:
            raise StoreError("A Blob store URL looks like https://<account>.blob.core.windows.net/<container>[/<path>]")
        self.location = url.rstrip("/")
        self.prefix = (m.group("prefix") or "").strip("/")
        if container_client is None:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import ContainerClient
            container_client = ContainerClient(f"https://{m.group('account')}.blob.core.windows.net",
                                               m.group("container"), credential=DefaultAzureCredential())
        self.client = container_client

    def _name(self, name: str) -> str:
        name = _clean(name)
        return f"{self.prefix}/{name}" if self.prefix else name

    def _relative(self, blob_name: str) -> str:
        return blob_name[len(self.prefix) + 1:] if self.prefix else blob_name

    def list(self, prefix: str = "") -> list[str]:
        start = self._name(prefix) + "/" if prefix else (self.prefix + "/" if self.prefix else "")
        try:
            names = [b.name for b in self.client.list_blobs(name_starts_with=start or None)]
        except Exception as e:  # azure.core errors carry request details; report the type only
            raise StoreError(f"Cannot list {self.location}: {type(e).__name__}") from None
        # Direct children only, as LocalStore.list() returns
        return sorted(self._relative(n) for n in names if "/" not in n[len(start):])

    def read(self, name: str) -> bytes:
        try:
            return self.client.download_blob(self._name(name)).readall()
        except Exception as e:
            raise StoreError(f"Cannot read {name} from {self.location}: {type(e).__name__}") from None

    def write(self, name: str, data: bytes) -> None:
        try:
            self.client.upload_blob(self._name(name), data, overwrite=True)
        except Exception as e:
            raise StoreError(f"Cannot write {name} to {self.location}: {type(e).__name__}") from None

    def delete(self, name: str) -> None:
        try:
            self.client.delete_blob(self._name(name))
        except Exception as e:
            raise StoreError(f"Cannot delete {name} from {self.location}: {type(e).__name__}") from None

    def download(self, name: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            with tmp.open("wb") as f:
                self.client.download_blob(self._name(name)).readinto(f)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise StoreError(f"Cannot download {name} from {self.location}: {type(e).__name__}") from None
        os.replace(tmp, dest)


def open_store(location: str) -> Store:
    """A BlobStore for an https:// Blob URL, else a LocalStore for a folder."""
    if location.startswith("https://"):
        return BlobStore(location)
    if "://" in location:
        raise StoreError("A store is a local folder or an https:// Blob container URL")
    return LocalStore(Path(location))
