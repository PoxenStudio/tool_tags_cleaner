"""
job_store —— 标签清理工具的持久任务状态。

跟 `tool_translation/backend/job_store.py` 是同一个思路的精简版：`BackgroundService`
的任务记录只在进程内存里，重启/刷新页面后读不回，所以这里在工具自己的数据目录下维护一份
`status.json`，作为"当前/最近一次清理"的唯一权威状态；`self.api.tasks.*` 只是把同一份进度
顺带展示在宿主的后台任务面板里，不作为判断依据。

跟翻译工具的区别：这里没有产出文件（清理结果直接写回 Calibre 元数据），所以不需要
`jobs/<job_id>/` 这层目录，`status.json` 单文件即可。

写入用临时文件 + `os.replace` 保证原子性；同一时间只允许一个清理任务在跑，用
`try_start()` 做原子的"检查 + 创建"。取消是协作式的：`request_cancel()` 只置一个进程内
`threading.Event`，真正生效要等处理循环下一次检查它——不是立即打断当前正在写的那本书。
"""
import json
import logging
import os
import tempfile
import threading
import time
from typing import Optional

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# 运行中任务如果长时间没有任何进度更新，视为已失联（例如进程被杀）
STALE_RUNNING_SECONDS = 2 * 3600

_STATUS_FILENAME = "status.json"


class JobStore:
    """按工具数据目录缓存的单例；同一个工具在同一个进程里只应该有一份状态。"""

    _instances = {}
    _instances_lock = threading.Lock()

    def __new__(cls, data_dir: str):
        with cls._instances_lock:
            inst = cls._instances.get(data_dir)
            if inst is None:
                inst = super().__new__(cls)
                inst._initialized = False
                cls._instances[data_dir] = inst
            return inst

    def __init__(self, data_dir: str):
        if self._initialized:
            return
        self._data_dir = data_dir
        self._status_path = os.path.join(data_dir, _STATUS_FILENAME)
        self._lock = threading.Lock()
        # 进程内取消信号，不落盘：跟着当前这一个"运行中任务"走，try_start() 开新任务时重置。
        self._cancel_event = threading.Event()
        os.makedirs(data_dir, exist_ok=True)
        self._interrupt_stale_running_job()
        self._initialized = True

    def _interrupt_stale_running_job(self) -> None:
        """只在这个数据目录第一次被访问时跑一次，效果上等同于"进程重启后的第一次检查"。
        如果这时 status.json 还是 running，说明是上一个进程遗留的、不可能再推进的任务，
        直接改判为 failed，这样它就不会一直挡住 `is_running()`/`try_start()`。"""
        job = self.read()
        if not job or job.get("status") != STATUS_RUNNING:
            return
        logging.warning("[tags_cleaner] Previous job was still 'running' when the process "
                         "(re)started; marking it failed")
        job["status"] = STATUS_FAILED
        job["error"] = "服务重启，清理任务已中断"
        job["updated_at"] = time.time()
        job["finished_at"] = time.time()
        self._write_atomic(job)

    # 读写

    def read(self) -> Optional[dict]:
        if not os.path.exists(self._status_path):
            return None
        try:
            with open(self._status_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as err:
            logging.warning("[tags_cleaner] Failed to read status.json: %s", err)
            return None

    def _write_atomic(self, data: dict) -> None:
        fd, tmp_path = tempfile.mkstemp(prefix=".status-", dir=self._data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._status_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    def is_running(self) -> bool:
        job = self.read()
        return bool(job and job.get("status") == STATUS_RUNNING)

    def try_start(self, job: dict):
        """原子"检查是否有运行中任务 + 写入新任务"。

        返回 `(started, job)`：`started=False` 时 `job` 是已有的运行中任务（前端应直接
        展示它，而不是报错），`started=True` 时 `job` 就是刚写入的新任务。
        """
        with self._lock:
            current = self.read()
            if current and current.get("status") == STATUS_RUNNING and not _is_stale(current):
                return False, current
            now = time.time()
            job.setdefault("started_at", now)
            job["updated_at"] = now
            self._cancel_event.clear()
            self._write_atomic(job)
            return True, job

    def request_cancel(self) -> bool:
        """请求取消当前运行中的任务，返回 False 说明当前没有运行中的任务可取消。"""
        with self._lock:
            current = self.read()
            if not current or current.get("status") != STATUS_RUNNING:
                return False
            self._cancel_event.set()
            current["cancel_requested"] = True
            current["updated_at"] = time.time()
            self._write_atomic(current)
            return True

    def is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def update(self, **fields) -> None:
        with self._lock:
            current = self.read()
            if not current:
                return
            current.update(fields)
            current["updated_at"] = time.time()
            self._write_atomic(current)


def _is_stale(job: dict) -> bool:
    updated_at = job.get("updated_at") or job.get("started_at") or 0
    return (time.time() - updated_at) > STALE_RUNNING_SECONDS


def empty_stats() -> dict:
    return {
        "total": 0,
        "processed": 0,
        "changed": 0,
        "unchanged": 0,
        "errors": 0,
        "error_samples": [],
        "current_book": "",
    }


def public_status(job: Optional[dict]) -> dict:
    """job 对前端的展示形态。"""
    if not job:
        return {"status": STATUS_IDLE}
    fields = (
        "status", "total", "processed", "changed", "unchanged", "errors",
        "error_samples", "current_book", "cancel_requested", "error",
        "started_at", "updated_at", "finished_at",
    )
    return {k: job.get(k) for k in fields if k in job}
