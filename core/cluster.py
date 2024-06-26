import asyncio
import base64
from enum import Enum
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import aiofiles
import aiohttp
from typing import Any, Optional, Type
import socketio
from tqdm import tqdm
from core.config import Config
from core import certificate, dashboard, system, unit
from core import timer as Timer
from core.timer import Task
import pyzstd as zstd
import core.utils as utils
import core.stats as stats
import core.web as web
from core.logger import logger
import plugins
from core.i18n import locale
import aiowebdav.client as webdav3_client
import aiowebdav.exceptions as webdav3_exceptions

from core.const import *

from core.api import (
    File,
    BMCLAPIFile,
    FileCheckType,
    StatsCache,
    Storage,
    get_hash,
)


class TokenManager:
    def __init__(self) -> None:
        self.token = None
        self.token_expires: float = 0

    async def fetchToken(self):
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT}, base_url=BASE_URL
        ) as session:
            logger.info(locale.t("cluster.info.token.fetching"))
            try:
                async with session.get(
                    "/openbmclapi-agent/challenge", params={"clusterId": CLUSTER_ID}
                ) as req:
                    req.raise_for_status()
                    challenge: str = (await req.json())["challenge"]

                signature = hmac.new(
                    CLUSTER_SECERT.encode("utf-8"), digestmod=hashlib.sha256
                )
                signature.update(challenge.encode())
                signature = signature.hexdigest()

                data = {
                    "clusterId": CLUSTER_ID,
                    "challenge": challenge,
                    "signature": signature,
                }

                async with session.post("/openbmclapi-agent/token", json=data) as req:
                    req.raise_for_status()
                    content: dict[str, Any] = await req.json()
                    self.token = content["token"]
                    Timer.delay(
                        self.fetchToken, delay=float(content["ttl"]) / 1000.0 - 600
                    )
                    self.token_expires = content["ttl"] / 1000.0 - 600 + time.time()
                    tll = utils.format_time(content["ttl"] / 1000.0)
                    logger.success(locale.t("cluster.success.token.fetched", tll=tll))

            except aiohttp.ClientError as e:
                logger.error(
                    locale.t("cluster.error.token.failed", delay=RECONNECT_DELAY, e=e)
                )
                await asyncio.sleep(RECONNECT_DELAY)
                return await self.fetchToken()

    async def getToken(self) -> str:
        if not self.token:
            await self.fetchToken()
        return self.token or ""


class ParseFileList:
    async def __call__(self, data) -> list[BMCLAPIFile]:
        self.data = io.BytesIO(data)
        self.files = []
        with tqdm(
            total=self.read_long(),
            desc=locale.t("cluster.tqdm.desc.parsing_file_list"),
            unit_scale=True,
            unit=locale.t("cluster.tqdm.unit.file"),
        ) as pbar:
            for _ in range(pbar.total):
                self.files.append(
                    BMCLAPIFile(
                        self.read_string(),
                        self.read_string(),
                        self.read_long(),
                        self.read_long(),
                    )
                )
                pbar.update(1)
        return self.files

    def read_long(self):
        b = ord(self.data.read(1))
        n = b & 0x7F
        shift = 7
        while (b & 0x80) != 0:
            b = ord(self.data.read(1))
            n |= (b & 0x7F) << shift
            shift += 7
        return (n >> 1) ^ -(n & 1)

    def read_string(self):
        return self.data.read(self.read_long()).decode("utf-8")


class FileDownloader:
    def __init__(self) -> None:
        self.files = []
        self.queues: asyncio.Queue[BMCLAPIFile] = asyncio.Queue()
        self.last_modified: int = 0

    async def get_files(self) -> list[BMCLAPIFile]:
        async with aiohttp.ClientSession(
            base_url=BASE_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {await token.getToken()}",
            },
        ) as session:
            logger.debug(locale.t("cluster.debug.get_files.created_session"))
            async with session.get(
                "/openbmclapi/files",
                data={
                    "responseType": "buffer",
                    "cache": "",
                    "lastModified": self.last_modified,
                },
            ) as req:
                logger.debug(
                    locale.t(
                        "cluster.debug.get_files.response_status", status=req.status
                    )
                )
                if req.status == 204:
                    return []
                if req.status != 200:
                    try:
                        req.raise_for_status()
                    except:
                        logger.error(traceback.format_exc())
                    return []
                logger.info(locale.t("cluster.success.get_files.requested_filelist"))
                files = await ParseFileList()(zstd.decompress(await req.read()))
                self.last_modified = max(
                    self.last_modified, *(file.mtime for file in files)
                )
                modified = utils.parse_time_to_gmt(self.last_modified / 1000)
                logger.info(
                    locale.t("cluster.info.get_files.modified_time", time=modified)
                )
                if DEBUG:
                    self.files = files
                return files

    async def _download(self, pbar: tqdm, session: aiohttp.ClientSession):
        while not self.queues.empty() and storages.available:
            file = await self.queues.get()
            hash = get_hash(file.hash)
            size = 0
            filepath = Path("./cache/download/" + file.hash[:2] + "/" + file.hash)
            if filepath.exists() and filepath.stat().st_size == size:
                await self._mount_files(file)
            try:
                async with session.get(file.path) as resp:
                    filepath.parent.mkdir(exist_ok=True, parents=True)
                    async with aiofiles.open(filepath, "wb") as w:
                        while data := await resp.content.read(IO_BUFFER):
                            if not data:
                                break
                            byte = len(data)
                            size += byte
                            pbar.update(byte)
                            # pbar.set_postfix_str(file.hash.ljust(40))
                            await w.write(data)
                            hash.update(data)
                if file.hash != hash.hexdigest():
                    filepath.unlink(True)
                    raise EOFError
                await self._mount_files(file)
            except:
                pbar.update(-size)
                await self.queues.put(file)
        await session.close()

    async def _mount_files(self, file: BMCLAPIFile):
        buf = io.BytesIO()
        async with aiofiles.open(
            f"./cache/download/{file.hash[:2]}/{file.hash}", "rb"
        ) as r:
            buf = io.BytesIO(await r.read())
        for storage in storages.get_storages():
            result = -1
            try:
                result = await storage.write(file.hash, buf)
            except:
                logger.error(traceback.format_exc())
            if result != file.size:
                hash = file.hash
                file_size = unit.format_bytes(file.size)
                target_size = unit.format_bytes(result)
                logger.error(
                    locale.t(
                        "cluster.error.mount_files.failed_to_copy",
                        hash=hash,
                        file=file_size,
                        target=target_size,
                    )
                )

        try:
            os.remove(f"./cache/download/{file.hash[:2]}/{file.hash}")
        except:
            ...

    async def download(self, miss: list[BMCLAPIFile]):
        if not storages.available:
            logger.error("No storages.")
            return
        with tqdm(
            desc=locale.t("cluster.tqdm.desc.download"),
            unit="b",
            unit_divisor=1024,
            total=sum((file.size for file in miss)),
            unit_scale=True,
        ) as pbar:
            await dashboard.set_status_by_tqdm(
                "下载文件中", pbar
            )
            for file in miss:
                await self.queues.put(file)
            timers = []
            for _ in range(0, MAX_DOWNLOAD, 32):
                for __ in range(32):
                    timers.append(
                        self._download(
                            pbar,
                            aiohttp.ClientSession(
                                BASE_URL,
                                headers={
                                    "User-Agent": USER_AGENT,
                                    "Authorization": f"Bearer {await token.getToken()}",
                                },
                            ),
                        ),
                    )
            await asyncio.gather(*timers)
            # pbar.set_postfix_str(" " * 40)
        logger.success(locale.t("cluster.info.download.finished"))


class FileCheck:
    def __init__(self, downloader: FileDownloader) -> None:
        self.checked = False
        self.downloader = downloader
        self.check_type = FileCheckType.EXISTS
        if FILECHECK == "size":
            self.check_type = FileCheckType.SIZE
        elif FILECHECK == "hash":
            self.check_type = FileCheckType.HASH
        self.files = []
        self.pbar: Optional[tqdm] = None
        self.check_files_timer: Optional[Task] = None
        logger.info(
            locale.t("cluster.info.check_files.check_type", type=self.check_type.name)
        )

    def start_task(self):
        if self.check_files_timer:
            self.check_files_timer.block()
        self.check_files_timer = Timer.repeat(self.__call__, delay=1800, interval=1800)

    async def __call__(
        self,
    ) -> Any:
        if not self.checked:
            await dashboard.set_status("拉取最新文件列表")
        files = await self.downloader.get_files()
        sorted(files, key=lambda x: x.hash)
        if not self.checked:
            await dashboard.set_status("正在检查缺失文件")
        if not files:
            logger.warn(locale.t("cluster.warn.check_files.skipped"))
            self.start_task()
            return
        with tqdm(
            total=len(files) * len(storages.get_storages()),
            unit=locale.t("cluster.tqdm.unit.file"),
            unit_scale=True,
        ) as pbar:
            self.pbar = pbar
            self.files = files
            await dashboard.set_status_by_tqdm("文件完整性", pbar)
            pbar.set_description(locale.t("cluster.tqdm.desc.check_files"))

            miss_storage: list[list[BMCLAPIFile]] = await asyncio.gather(
                *[
                    self.check_missing_files(storage)
                    for storage in storages.get_storages()
                ]
            )
            missing_files_by_storage: dict[Storage, set[BMCLAPIFile]] = {}
            total_missing_bytes = 0

            for storage, missing_files in zip(storages.get_storages(), miss_storage):
                missing_files_by_storage[storage] = set(missing_files)
                total_missing_bytes += sum(
                    (file.size for file in missing_files_by_storage[storage])
                )

            self.pbar = None
        if not self.checked:
            self.checked = True
            more_files = {storage: [] for storage in storages.get_storages()}
            prefix_files = {
                prefix: []
                for prefix in (prefix.to_bytes(1, "big").hex() for prefix in range(256))
            }
            prefix_hash = {
                prefix: []
                for prefix in (prefix.to_bytes(1, "big").hex() for prefix in range(256))
            }

            for file in files:
                prefix_files[file.hash[:2]].append(file)
                prefix_hash[file.hash[:2]].append(file.hash)
            for more, more_storage in more_files.items():
                for prefix, filelist in prefix_files.items():
                    size = await more.get_files_size(prefix)
                    if size != sum((file.size for file in filelist)):
                        for file in await more.get_files(prefix):
                            if file in prefix_hash[prefix]:
                                continue
                            more_storage.append(file)
            more_total = sum(len(storage) for storage in more_files.values())
            if more_total != 0:
                with tqdm(
                    total=more_total,
                    desc=locale.t("cluster.tqdm.desc.delete_old_files"),
                    unit=locale.t("cluster.tqdm.unit.file"),
                    unit_scale=True,
                ) as pbar:
                    await dashboard.set_status_by_tqdm("删除旧文件中", pbar)
                    for storage, filelist in more_files.items():
                        removed = await storage.removes(filelist)
                        if removed != (total := len(filelist)):
                            logger.warn(
                                locale.t(
                                    "cluster.warn.check_files.failed",
                                    cur=removed,
                                    total=total,
                                )
                            )
                        pbar.update(total)
            if total_missing_bytes != 0 and len(miss_storage) >= 2:
                with tqdm(
                    total=total_missing_bytes,
                    desc=locale.t(
                        "cluster.tqdm.desc.copying_files_from_local_storages"
                    ),
                    unit="B",
                    unit_divisor=1024,
                    unit_scale=True,
                ) as pbar:
                    await dashboard.set_status_by_tqdm("复制缺失文件中", pbar)
                    for storage, files in missing_files_by_storage.items():
                        for file in files:
                            for other_storage in storages.get_storages():
                                if other_storage == storage:
                                    continue
                                if (
                                    await other_storage.exists(file.hash)
                                    and await other_storage.get_size(file.hash)
                                    == file.size
                                ):
                                    size = await storage.write(
                                        file.hash,
                                        (await other_storage.get(file.hash)).get_data(),
                                    )
                                    if size == -1:
                                        hash = file.hash
                                        file_size = unit.format_bytes(file.size)
                                        target_size = unit.format_bytes(size)
                                        logger.warn(
                                            locale.t(
                                                "cluster.error.check_files.failed_to_copy",
                                                hash=hash,
                                                file=file_size,
                                                target=target_size,
                                            )
                                        )
                                    else:
                                        missing_files_by_storage[storage].remove(file)
                                        pbar.update(size)
        miss = set().union(*missing_files_by_storage.values())
        if not miss:
            file_count = len(files) * len(storages.get_storages())
            file_size = unit.format_bytes(
                sum(file.size for file in files) * len(storages.get_storages())
            )
            logger.success(
                locale.t(
                    "cluster.success.check.finished", count=file_count, size=file_size
                )
            )
        else:
            logger.info(
                locale.t(
                    "cluster.info.check_files.missing",
                    count=unit.format_number(len(miss)),
                )
            )
            await self.downloader.download(list(miss))
        if os.path.exists("./cache/download"):
            paths = []
            dir = []
            for root, dirs, files in os.walk("./cache/download"):
                for file in files:
                    paths.append(os.path.join(root, file))
                if dirs:
                    for d in dirs:
                        dir.append(d)
            with tqdm(
                desc=locale.t("cluster.tqdm.desc.cleaning_cache"),
                total=len(paths) + len(dir),
                unit=locale.t("cluster.tqdm.unit.file"),
                unit_scale=True,
            ) as pbar:
                await dashboard.set_status_by_tqdm("清理缓存文件中", pbar)
                if paths:
                    for path in paths:
                        os.remove(path)
                        pbar.update(1)
                if dir:
                    for d in dir:
                        os.removedirs(f"./cache/download/{d}")
                        pbar.update(1)
        self.start_task()

    async def _exists(self, file: BMCLAPIFile, storage: Storage):
        return await storage.exists(file.hash)

    async def _size(self, file: BMCLAPIFile, storage: Storage):
        return (
            await storage.exists(file.hash)
            and await storage.get_size(file.hash) == file.size
        )

    async def _hash(self, file: BMCLAPIFile, storage: Storage):
        return (
            await storage.exists(file.hash)
            and await storage.get_hash(file.hash) == file.hash
        )

    async def check_missing_files(self, storage: Storage):
        if not self.pbar:
            raise
        miss = []
        handler = None
        if self.check_type == FileCheckType.EXISTS:
            handler = self._exists
        if self.check_type == FileCheckType.SIZE:
            handler = self._size
        if self.check_type == FileCheckType.HASH:
            handler = self._hash
        if handler is None:
            raise KeyError(f"HandlerNotFound: {self.check_type}")
        for file in self.files:
            if not await handler(file, storage):
                miss.append(file)
            self.pbar.update(1)
            await asyncio.sleep(0)
        return miss


class FileStorage(Storage):
    def __init__(self, name: str, dir: Path, width: int) -> None:
        super().__init__(name, width)
        self.dir = dir
        if self.dir.is_file():
            raise FileExistsError(f"Cannot copy file: '{self.dir}': Is a file.")
        self.dir.mkdir(exist_ok=True, parents=True)
        self.cache: dict[str, File] = {}
        self.timer = Timer.repeat(
            self.clear_cache, delay=CHECK_CACHE, interval=CHECK_CACHE
        )

    async def get(self, hash: str, offset: int = 0) -> File:
        if hash in self.cache:
            file = self.cache[hash]
            file.last_access = time.time()
            file.cache = True
            return file
        path = Path(str(self.dir) + f"/{hash[:2]}/{hash}")
        buf = io.BytesIO()
        async with aiofiles.open(path, "rb") as r:
            while data := await r.read(IO_BUFFER):
                buf.write(data)
        file = File(path, hash, buf.tell(), time.time(), time.time())
        file.set_data(buf.getbuffer())
        self.cache[hash] = file
        file.cache = False
        return file

    async def exists(self, hash: str) -> bool:
        return os.path.exists(str(self.dir) + f"/{hash[:2]}/{hash}")

    async def get_size(self, hash: str) -> int:
        return os.path.getsize(str(self.dir) + f"/{hash[:2]}/{hash}")

    async def copy(self, origin: Path, hash: str):
        Path(str(self.dir) + f"/{hash[:2]}/{hash}").parent.mkdir(
            exist_ok=True, parents=True
        )
        async with (
            aiofiles.open(str(self.dir) + f"/{hash[:2]}/{hash}", "wb") as w,
            aiofiles.open(origin, "rb") as r,
        ):
            await w.write(await r.read())
            return origin.stat().st_size

    async def write(self, hash: str, io: io.BytesIO) -> int:
        Path(str(self.dir) + f"/{hash[:2]}/{hash}").parent.mkdir(
            exist_ok=True, parents=True
        )
        async with aiofiles.open(str(self.dir) + f"/{hash[:2]}/{hash}", "wb") as w:
            await w.write(io.getbuffer())
            return len(io.getbuffer())

    async def get_hash(self, hash: str) -> str:
        h = get_hash(hash)
        async with aiofiles.open(str(self.dir) + f"/{hash[:2]}/{hash}", "rb") as r:
            while data := await r.read(Config.get("advanced.io_buffer")):
                if not data:
                    break
                h.update(data)
                await asyncio.sleep(0.001)
        return h.hexdigest()

    async def clear_cache(self):
        size: int = 0
        old_keys: list[str] = []
        old_size: int = 0
        file: File
        key: str
        for key, file in sorted(
            self.cache.items(), key=lambda x: x[1].last_access, reverse=True
        ):
            if size <= CACHE_BUFFER and file.last_access + CACHE_TIME >= time.time():
                continue
            old_keys.append(key)
            old_size += file.size
        if not old_keys:
            return
        for key in old_keys:
            self.cache.pop(key)
        logger.info(
            locale.t(
                "cluster.info.clear_cache.count",
                count=unit.format_number(len(old_keys)),
                size=unit.format_bytes(old_size),
            )
        )

    async def get_files(self, dir: str) -> list[str]:
        files = []
        if os.path.exists(str(self.dir) + f"/{dir}"):
            with os.scandir(str(self.dir) + f"/{dir}") as session:
                for file in session:
                    files.append(file.name)
        return files

    async def removes(self, hashs: list[str]) -> int:
        success = 0
        for hash in hashs:
            file = str(self.dir) + f"/{hash[:2]}/{hash}"
            if os.path.exists(file):
                os.remove(file)
                success += 1
        return success

    async def get_files_size(self, dir: str) -> int:
        size = 0
        if os.path.exists(str(self.dir) + f"/{dir}"):
            with os.scandir(str(self.dir) + f"/{dir}") as session:
                for file in session:
                    size += file.stat().st_size
        return size

    async def get_cache_stats(self) -> StatsCache:
        stat = StatsCache()
        for file in self.cache.values():
            stat.total += 1
            stat.bytes += file.size
        return stat


class WebDav(Storage):
    def __init__(
        self,
        name: str,
        width: int,
        username: str,
        password: str,
        hostname: str,
        endpoint: str,
    ) -> None:
        super().__init__(name, width)
        self.username = username
        self.password = password
        self.hostname = hostname
        self.endpoint = endpoint
        self.files: dict[str, File] = {}
        self.dirs: list[str] = []
        self.fetch: bool = False
        self.cache: dict[str, File] = {}
        self.empty = File("", "", 0)
        self.lock = None
        self.session = webdav3_client.Client(
            {
                "webdav_login": self.username,
                "webdav_password": self.password,
                "webdav_hostname": self.hostname,
                "User-Agent": USER_AGENT,
            }
        )
        Timer.delay(self._list_all)
        Timer.repeat(self._keepalive, interval=60)

    async def _keepalive(self):
        try:
            hostname = self.hostname
            endpoint = self.endpoint
            if not self.disabled:
                logger.success(
                    locale.t(
                        "cluster.success.webdav.keepalive",
                        hostname=hostname,
                        endpoint=endpoint,
                    )
                )
            else:
                storages.enable(self)
                logger.success(
                    locale.t(
                        "cluster.success.webdav.enabled",
                        hostname=hostname,
                        endpoint=endpoint,
                    )
                )
                await self._list_all()
        except webdav3_exceptions.NoConnection:
            if not self.disabled:
                logger.warn(
                    locale.t(
                        "cluster.warn.webdav.no_connection",
                        hostname=hostname,
                        endpoint=endpoint,
                    )
                )
            storages.disable(self)
            self.fetch = False
        except:
            logger.error(traceback.format_exc())

    async def _execute(self, target):
        try:
            return await target
        except webdav3_exceptions.NoConnection as e:
            hostname = self.hostname
            endpoint = self.endpoint
            logger.warn(
                locale.t(
                    "cluster.warn.webdav.no_connection",
                    hostname=hostname,
                    endpoint=endpoint,
                )
            )
            storages.disable(self)
            self.fetch = False
            raise e
        except Exception as e:
            raise e

    def _endpoint(self, file: str):
        return f"{self.endpoint}/{file.removeprefix('/')}"

    async def _mkdir(self, dirs: str):
        if await self._execute(self.session.check(dirs)):
            return
        d = ""
        for dir in dirs.split("/"):
            d += dir
            await self._execute(self.session.mkdir(d))
            d += "/"

    async def _list_all(self, force=False):
        if self.fetch and not force:
            return
        if not self.fetch:
            self.lock = asyncio.get_running_loop().create_future()
        self.fetch = True
        try:
            await self._mkdir(self.endpoint)
            dirs = (await self._execute(self.session.list(self.endpoint)))[1:]
            with tqdm(
                total=len(dirs),
                desc=f"[WebDav List Files <endpoint: '{self.endpoint}'>]",
            ) as pbar:
                await dashboard.set_status_by_tqdm("正在获取 WebDav 文件列表中", pbar)
                for dir in (await self._execute(self.session.list(self.endpoint)))[1:]:
                    pbar.update(1)
                    files: dict[str, File] = {}
                    for file in (
                        await self._execute(
                            self.session.list(
                                self._endpoint(
                                    dir,
                                ),
                                get_info=True,
                            )
                        )
                    )[1:]:
                        files[file["name"]] = File(
                            file["path"].removeprefix(f"/dav/{self.endpoint}/"),
                            file["name"],
                            int(file["size"]),
                        )
                        await asyncio.sleep(0)
                    for remove in set(
                        file for file in self.files.keys() if file.startswith(dir)
                    ) - set(files.keys()):
                        self.files.pop(remove)
                    self.files.update(files)
                    if dir not in self.dirs:
                        self.dirs.append(dir)
        except:
            logger.error(traceback.format_exc())
        if self.lock is not None:
            self.lock.cancel()
            self.lock = None
        return self.files

    async def _wait_lock(self):
        while self.lock:
            try:
                await asyncio.wait_for(self.lock, timeout=1)
            except:
                ...

    async def get(self, file: str, offset: int = 0) -> File:
        if file in self.cache and self.cache[file].expiry - 10 > time.time():
            self.cache[file].cache = True
            self.cache[file].last_hit = time.time()
            return self.cache[file]
        try:
            async with aiohttp.ClientSession(
                auth=aiohttp.BasicAuth(self.username, self.password)
            ) as session:
                async with session.get(
                    self.hostname + self._endpoint(file[:2] + "/" + file),
                    allow_redirects=False,
                ) as resp:
                    logger.debug(resp.status, resp.closed)
                    f = File(
                        file,
                        file,
                        size=int(resp.headers.get("Content-Length", 0)),
                    )
                    f.headers = {}
                    for field in (
                        "ETag",
                        "Last-Modified",
                        "Content-Length",
                        "Content-Range",
                    ):
                        if field not in resp.headers:
                            continue
                        f.headers[field] = resp.headers.get(field)
                    if resp.status == 200:
                        f.set_data(await resp.read())
                        f.expiry = time.time() + CACHE_TIME
                    elif resp.status // 100 == 3:
                        f.path = resp.headers.get("Location")
                        f.expiry = time.time() + utils.parse_cache_control(
                            resp.headers.get("Cache-Control", "")
                        )
                    self.cache[file] = f
            return self.cache[file]
        except Exception as e:
            storages.disable(self)

    async def exists(self, hash: str) -> bool:
        await self._wait_lock()
        if not self.fetch:
            self.fetch = True
            await self._list_all()
        return hash in self.files

    async def get_size(self, hash: str) -> int:
        await self._wait_lock()
        return self.files.get(hash, self.empty).size

    async def write(self, hash: str, io: io.BytesIO) -> int:
        path = self._endpoint(f"{hash[:2]}/{hash}")
        await self._mkdir(self._endpoint(f"{hash[:2]}"))
        await self._execute(self.session.upload_to(io.getbuffer(), path))
        self.files[hash] = File(path, hash, len(io.getbuffer()))
        return self.files[hash].size

    async def get_files(self, dir: str) -> list[str]:
        await self._wait_lock()
        return list((hash for hash in self.files.keys() if hash.startswith(dir)))

    async def get_hash(self, hash: str) -> str:
        h = get_hash(hash)
        async for data in await self._execute(
            self.session.download_iter(self._endpoint(f"{hash[:2]}/{hash}"))
        ):
            h.update(data)
        return h.hexdigest()

    async def get_files_size(self, dir: str) -> int:
        await self._wait_lock()
        return sum(
            (file.size for hash, file in self.files.items() if hash.startswith(dir))
        )

    async def removes(self, hashs: list[str]) -> int:
        success = 0
        for hash in hashs:
            await self._execute(
                self.session.clean(self._endpoint(f"{hash[:2]}/{hash}"))
            )
            success += 1
        return success

    async def get_cache_stats(self) -> StatsCache:
        stat = StatsCache()
        for file in self.cache.values():
            stat.total += 1
            stat.bytes += file.size
        return stat


class TypeStorage(Enum):
    FILE = "file"
    WEBDAV = "webdav"


class StorageManager:
    def __init__(self) -> None:
        self._storages: list[Storage] = []
        self._interface_storage: dict[TypeStorage, Type[Storage]] = {}
        self._storage_stats: dict[Storage, stats.StorageStats] = {}
        self.available_width = False
        self.available = False
        self.storage_widths: dict[Storage, int] = {}
        self.storage_cur: int = 0

    def enable(self, storage: Storage):
        storage.disabled = False
        if not self.available and not cluster.connected:
            self.available = True
            Timer.delay(cluster.start)

    def disable(self, storage: Storage):
        storage.disabled = True
        if self.available and not self.get_storages():
            self.available = False
            Timer.delay(cluster.disable)

    def add_storage(self, storage):
        self._storages.append(storage)
        type = "Unknown"
        key = time.time()
        if isinstance(storage, FileStorage):
            type = "File"
            key = storage.dir
        elif isinstance(storage, WebDav):
            type = "Webdav"
            key = storage.endpoint
        self._storage_stats[storage] = stats.get_storage(f"{type}_{key}")
        self.available = True
        if storage.width != -1:
            self.available_width = True
            self.storage_widths[storage] = 0

    def remove_storage(self, storage):
        self._storages.remove(storage)

    def add_interface(self, type: TypeStorage, storage: Type[Storage]):
        self._interface_storage[type] = storage

    def create_storage(self, type: TypeStorage, *args, **kwargs):
        self.add_storage(self._interface_storage[type](*args, **kwargs))

    def get_all_storages(self):
        return [storage for storage in self._storages if not storage.disabled]

    def get_storages(self):
        return [storage for storage in self._storages if not storage.disabled]

    def get_available_storages(self):
        return [
            storage
            for storage in self._storages
            if not storage.disabled and storage.width != -1
        ]

    def get_storage_stats(self):
        return self._storage_stats

    def get_storage_width(self):
        keys = list(self.storage_widths.keys())
        storage: Storage = keys[self.storage_cur]
        if self.storage_widths[storage] >= storage.width:
            self.storage_widths[storage] = 0
            self.storage_cur += 1
            if self.storage_cur >= len(keys):
                self.storage_cur = 0
                storage = keys[self.storage_cur]
        self.storage_widths[storage] += 1
        return storage

    async def get(self, hash: str, offset: int) -> Optional[File]:
        first_storage = self.get_storage_width()
        storage = first_storage
        exists: bool = False
        while not (exists := await storage.exists(hash)):
            storage = self.get_storage_width()
            if storage == first_storage:
                break
        if not exists:
            return None
        file = await storage.get(hash, offset)
        self._storage_stats[storage].hit(file, offset)
        return file

    def get_storage_stat(self, storage):
        return self._storage_stats[storage]


class Cluster:
    def __init__(self) -> None:
        self.connected = False
        self.sio = socketio.AsyncClient()
        self.sio.on("message", self._message)
        self.sio.on("exception", self._exception)
        self.stats_storage: Optional[stats.SyncStorage] = None
        self.downloader = FileDownloader()
        self.file_check = FileCheck(self.downloader)
        self._enable_timer: Optional[Task] = None
        self.keepaliving = False
        self.keepaliveTimer: Optional[Task] = None
        self.keepaliveTimeoutTimer: Optional[Task] = None
        self._cur_storages: list[stats.SyncStorage] = []
        self._retry = 0
        self.cert_valid: float = 0

    def _message(self, message):
        logger.info(locale.t("cluster.info.cluster.remote_message", message=message))
        if "信任度过低" in message:
            self.trusted = False

    def _exception(self, message):
        logger.error(locale.t("cluster.error.cluster.remote_message", message=message))
            Timer.delay(self.retry)

    async def emit(self, channel, data=None):
        await self.sio.emit(
            channel, data, callback=lambda x: Timer.delay(self.message, (channel, x))
        )

    async def connect(self):
        if not self.sio.connected:
            try:
                await self.sio.connect(
                    BASE_URL,
                    auth={"token": await token.getToken()},
                    transports=["websocket"],
                )
                await self.cert()
                return True
            except:
                logger.warn(locale.t("cluster.warn.cluster.failed_to_connect"))
                return False
        return True

    async def init(self):
        if not await self.connect():
            return
        await self.start()

    async def start(self):
        if len(storages.get_storages()) == 0:
            logger.warn(locale.t("cluster.warn.cluster.no_storage"))
            return
        await self.start_storage()

    async def start_storage(self):
        if len(storages.get_storages()) == 0:
            if self.connected:
                self.disable()
            logger.warn(locale.t("cluster.warn.cluster.no_storage"))
            return
        start = time.time()
        await self.file_check()
        t = "%.2f" % start
        logger.success(locale.t("cluster.success.cluster.finished_file_check", time=t))
        if not self.connected:
            await self.enable()

    async def cert(self):
        if self.cert_valid - 600 > time.time():
            return
        await self.emit("request-cert")

    async def enable(self):
        if self.connected:
            logger.debug(locale.t("cluster.debug.cluster.blocked"))
            return
        if not ENABLE or not storages.available:
            logger.warn(locale.t("cluster.warn.cluster.disabled"))
            return
        self.connected = True
        if self._enable_timer is not None:
            self._enable_timer.block()
        self._enable_timer = Timer.delay(self.retry, delay=ENABLE_TIMEOUT)
        if not await self.connect():
            return
        await self._enable()

    async def retry(self):
        if RECONNECT_RETRY != -1 and self._retry >= RECONNECT_RETRY:
            logger.error(
                locale.t(
                    "cluster.error.cluster.reached_maximum_retry_count",
                    count=RECONNECT_RETRY,
                )
            )
            return
        if self.connected:
            await self.disable()
            self.connected = False
        self._retry += 1
        logger.info(locale.t("cluster.info.cluster.retry", t=RECONNECT_DELAY))
        await asyncio.sleep(RECONNECT_DELAY)
        await self.enable()

    async def _enable(self):
        if not ENABLE or not storages.available:
            logger.warn(locale.t("cluster.warn.cluster.disabled"))
            return
        storage_str = {"file": 0, "webdav": 0}
        self.trusted = True
        for storage in storages.get_storages():
            if isinstance(storage, FileStorage):
                storage_str["file"] += 1
            elif isinstance(storage, WebDav):
                storage_str["webdav"] += 1
        logger.info(
            locale.t(
                "cluster.info.cluster.storage_count",
                total=len(storages.get_storages()),
                local=storage_str["file"],
                webdav=storage_str["webdav"],
            )
        )
        await self.emit(
            "enable",
            {
                "host": PUBLIC_HOST,
                "port": PUBLIC_PORT or PORT,
                "version": API_VERSION,
                "byoc": BYOC,
                "noFastEnable": False,
                "flavor": {
                    "runtime": f"python/{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} {VERSION}",
                    "storage": "+".join(
                        sorted((key for key, value in storage_str.items() if value))
                    ),
                },
            },
        )
        await dashboard.set_status("巡检中")

    async def message(self, type, data: list[Any]):
        if len(data) == 1:
            data.append(None)
        err, ack = data
        if type != "request-cert":
            logger.debug(type, data)
        if type == "request-cert":
            err, ack = data
            if err:
                logger.error(locale.t("cluster.error.cert.failed", ack=ack))
                return
            self.cert_valid = utils.parse_iso_time(ack["expires"])
            logger.success(locale.t("cluster.success.cert.requested"))
            certificate.load_text(ack["cert"], ack["key"])
        elif type == "enable":
            err, ack = data
            if self._enable_timer is not None:
                self._enable_timer.block()
                self._enable_timer = None
            if err:
                logger.error(
                    locale.t(
                        "cluster.error.cluster.failed_to_start_service",
                        e=err["message"],
                    )
                )
                await self.retry()
                return
            self._retry = 0
            self.connected = True
            logger.success(
                locale.t("cluster.success.cluster.connected_to_center_server")
            )
            logger.info(
                locale.t(
                    "cluster.info.cluster.hosting",
                    id=CLUSTER_ID,
                    port=PUBLIC_PORT or PORT,
                )
            )
            await self.start_keepalive()
            await dashboard.set_status(
                "正常工作" + ("" if self.trusted else "（节点信任度过低）")
            )
        elif type == "keep-alive":
            if err:
                logger.error(locale.t("cluster.error.cluster.keepalive_failed"))
                await self.retry()
                return
            if not ack:
                await self.emit("disable")
                logger.warn(locale.t("cluster.warn.cluster.kicked"))
                return
            storage_data = {"hits": 0, "bytes": 0}
            for storage in self._cur_storages:
                storage.object.add_last_hits(storage.sync_hits)
                storage.object.add_last_bytes(storage.sync_bytes)
                storage_data["hits"] += storage.sync_hits
                storage_data["bytes"] += storage.sync_bytes
            keepalive_time = utils.parse_iso_time(ack)
            hits = unit.format_number(storage_data["hits"])
            bytes = unit.format_bytes(storage_data["bytes"])
            storage_count = len(self._cur_storages)
            ping = int((time.time() - keepalive_time.timestamp()) * 1000)
            logger.success(
                locale.t(
                    "cluster.success.keepalive",
                    hits=hits,
                    bytes=bytes,
                    count=storage_count,
                    ping=ping,
                )
            )
            self._cur_storages = []

    async def start_keepalive(self, delay: int = 0):
        if self.keepaliveTimer is not None:
            self.keepaliveTimer.block()
        if self.keepaliveTimeoutTimer is not None:
            self.keepaliveTimeoutTimer.block()
        self.keepaliveTimer = Timer.delay(self._keepalive, delay=delay)
        self.keepaliveTimeoutTimer = Timer.delay(
            self._keepalive_timeout, delay=delay + KEEPALIVE_TIMEOUT
        )
        self.keepaliving = True

    async def _keepalive_timeout(self):
        if self.keepaliveTimer is not None:
            self.keepaliveTimer.block()
        if self.keepaliveTimeoutTimer is not None:
            self.keepaliveTimeoutTimer.block()
        self.keepaliving = False
        logger.warn(locale.t("cluster.error.cluster.keepalive_failed"))
        await self.retry()

    async def _keepalive(self):
        self._cur_storages = stats.get_offset_storages().copy()
        data = {"hits": 0, "bytes": 0}
        for storage in self._cur_storages:
            data["hits"] += storage.sync_hits
            data["bytes"] += storage.sync_bytes
        await self.emit(
            "keep-alive",
            {"time": int(time.time() * 1000), **data},
        )
        self.keepaliving = False
        logger.debug(locale.t("cluster.info.cluster.next_keepalive"))
        await self.start_keepalive(60)

    async def disable(self):
        if self.sio.connected and self.connected:
            await self.emit("disable")
            logger.info(locale.t("cluster.info.cluster.disconnecting"))
        if self.sio.connected and token.token_expires <= time.time():
            await self.sio.disconnect()
        await dashboard.set_status("已下线")

    async def get_cache_stats(self) -> StatsCache:
        stat = StatsCache()
        for storage in storages.get_storages():
            t = await storage.get_cache_stats()
            stat.total += t.total
            stat.bytes += t.bytes
        return stat


token = TokenManager()
cluster: Optional[Cluster] = None
last_status: str = "-"
storages = StorageManager()
github_api = "https://api.github.com"
download_url = ""


async def check_update():
    global fetched_version
    fetched_version = "Unknown"
    async with aiohttp.ClientSession(base_url=github_api) as session:
        logger.info(locale.t("cluster.info.check_update.checking"))
        try:
            async with session.get(
                "/repos/TTB-Network/python-openbmclapi/releases/latest"
            ) as req:
                req.raise_for_status()
                data = await req.json()
                fetched_version = data["tag_name"]
            if fetched_version != VERSION:
                logger.success(
                    locale.t(
                        "cluster.success.check_update.new_version",
                        latest=fetched_version,
                    )
                )
                await dashboard.trigger("version")
            else:
                logger.info(locale.t("cluster.info.check_update.already_up_to_date"))
        except aiohttp.ClientError as e:
            logger.error(locale.t("cluster.error.check_update.failed", e=e))
    Timer.delay(check_update, delay=3600)


async def init():
    global cluster
    cluster = Cluster()
    system.init()
    plugins.load_plugins()
    for plugin in plugins.get_plugins():
        await plugin.init()
        await plugin.enable()
    for storage in STORAGES:
        if storage.type == "file":
            storages.add_storage(
                FileStorage(storage.name, Path(storage.path), storage.width)
            )
        elif storage.type == "webdav":
            storages.add_storage(
                WebDav(
                    storage.name,
                    storage.width,
                    storage.kwargs["username"],
                    storage.kwargs["password"],
                    storage.kwargs["endpoint"],
                    storage.path,
                )
            )
    Timer.delay(cluster.init)
    app = web.app
    if DEBUG:
        logger.debug("Currently in developer mode")

        @app.get("/files")
        async def _():
            files = sorted(cluster.downloader.files, key=lambda x: x.hash)
            for file in files:
                yield f'<a href="/dev_download/{file.hash}" target="_blank">{file}</a></br>'.encode()

        @app.get("/dev_download/{hash}")
        async def _(hash: str):
            cur_time = int(time.time() * 1000.0) + 600
            e = utils.base36_encode(cur_time)
            s = hashlib.sha1()
            s.update(CLUSTER_SECERT.encode("utf-8"))
            s.update(hash.encode("utf-8"))
            s.update(e.encode("utf-8"))
            return web.RedirectResponse(
                f"/download/{hash}?s={base64.urlsafe_b64encode(s.digest()).decode().strip('=')}&e={e}"
            )

    @app.get("/measure/{size}")
    async def _(request: web.Request, size: int, config: web.ResponseConfiguration):
        if not SIGN_SKIP and not utils.check_sign(
            request.get_url(),
            CLUSTER_SECERT,
            request.get_url_params().get("s") or "",
            request.get_url_params().get("e") or "",
        ):
            yield web.Response(status_code=403)
            return
        config.length = size * 1024 * 1024
        for _ in range(size):
            yield b"\x00" * 1024 * 1024
        return

    @app.get("/download/{hash}", access_logs=False)
    async def _(request: web.Request, hash: str):
        if (
            not SIGN_SKIP
            and not utils.check_sign(
                hash,
                CLUSTER_SECERT,
                request.get_url_params().get("s") or "",
                request.get_url_params().get("e") or "",
            )
            or not cluster
        ):
            return web.Response(status_code=403)
        if not storages.available_width:
            return web.Response(status_code=503)
        start_bytes = 0
        range_str = await request.get_headers("range", "")
        range_match = re.search(r"bytes=(\d+)-(\d+)", range_str, re.S) or re.search(
            r"bytes=(\d+)-", range_str, re.S
        )
        if range_match:
            start_bytes = int(range_match.group(1)) if range_match else 0
        name = {}
        if request.get_url_params().get("name"):
            name["Content-Disposition"] = (
                f"attachment; filename={request.get_url_params().get('name')}"
            )
        data = await storages.get(hash, start_bytes)
        if not data:
            return web.Response(status_code=404)
        if data.is_url() and isinstance(data.get_path(), str):
            return web.RedirectResponse(str(data.get_path())).set_headers(name)
        return web.Response(
            data.get_data().getbuffer(), headers=data.headers or {}
        ).set_headers(name)

    cache = io.BytesIO()

    @app.get("/files")
    async def _(request: web.Request):
        if len(cache.getbuffer()) != 0:
            return cache.getbuffer()
        total = 0
        buf = utils.DataOutputStream()
        for root, dirs, files in os.walk("./bmclapi"):
            for file in files:
                total += 1
                buf.writeString(file)
                buf.writeVarInt(os.path.getsize(os.path.join(root, file)))
        data = utils.DataOutputStream()
        data.writeVarInt(total)
        data.write(buf.io.getvalue())
        cache.write(zstd.compress(data.io.getbuffer()))
        return cache

    @app.get("/sync_download/{hash}")
    async def _(request: web.Request, hash: str):
        return Path(f"./bmclapi/{hash[:2]}/{hash}")

    dir = Path("./bmclapi_dashboard/")
    dir.mkdir(exist_ok=True, parents=True)
    app.mount_resource(web.Resource("/", dir, show_dir=False))

    @app.get("/pages/{name}/{sub}")
    @app.get("/pages/{name}")
    async def _(request: web.Request, name: str, sub: str = ""):
        return Path(f"./bmclapi_dashboard/index.html")

    @app.websocket("/pages/{name}/{sub}")
    @app.websocket("/pages/{name}")
    async def _(request: web.Request, ws: web.WebSocket):
        dashboard.websockets.append(ws)
        auth_cookie = (await request.get_cookies()).get("auth") or None
        auth = dashboard.token_isvaild(auth_cookie.value if auth_cookie else None)
        if not auth:
            await ws.send(dashboard.to_bytes(0, "auth", None).io.getbuffer())
        else:
            await ws.send(dashboard.to_bytes(0, "auth", DASHBOARD_USERNAME).io.getbuffer())
        async for raw_data in ws:
            if isinstance(raw_data, str):
                continue
            if isinstance(raw_data, io.BytesIO):
                raw_data = raw_data.getvalue()
            input = utils.DataInputStream(raw_data)
            key = input.readVarInt()
            type = input.readString()
            data = dashboard.deserialize(input)
            await ws.send(
                dashboard.to_bytes(
                    key, type, await dashboard.process(type, data)
                ).io.getbuffer()
            )
        dashboard.websockets.remove(ws)
    @app.get("/auth")
    async def _(request: web.Request):
        auth = (await request.get_headers("Authorization")).split(" ", 1)[1]
        try:
            info = json.loads(base64.b64decode(auth))
        except:
            return web.Response(status_code=401)
        if (
            info["username"] != DASHBOARD_USERNAME
            or info["password"] != DASHBOARD_PASSWORD
        ):
            return web.Response(status_code=401)
        token = dashboard.generate_token(request)
        return web.Response(
            DASHBOARD_USERNAME,
            cookies=[web.Cookie("auth", token.value, expires=int(time.time() + 86400))],
        )

    @app.post("/api/{name}")
    async def _(request: web.Request, name: str):
        if name == "auth":
            auth_cookie = (await request.get_cookies()).get("auth") or None
            auth = dashboard.token_isvaild(auth_cookie.value if auth_cookie else None)
            if not auth:
                return None
            else:
                return DASHBOARD_USERNAME
        data = {"content": ""}
        try:
            data = (await request.json()) or {}
            if "content" not in data:
                data = {"content": ""}
        except:
            ...
        return await dashboard.process(name, data.get("content"))

    app.redirect("/", "/pages/")
    await check_update()


async def close():
    global cluster
    for plugin in plugins.get_enable_plugins():
        await plugin.disable()
    if cluster:
        await cluster.disable()
