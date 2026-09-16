"""
标签清理 —— MyBooks Toolbox 工具后端代码

用 `meta.guess_tags()` 里的一组固定规则（见该文件顶部 `RULES`，同时也是前端"清理规则"
面板展示的内容）遍历书库所有书籍，清掉广告/推广类无效标签。规则本身在 meta.py 里维护，
这个文件只管"怎么跑起来、怎么展示进度"。

耗时的遍历用 `AsyncService.register_service` 放到后台线程执行，进度通过
`job_store.JobStore`（工具自己的 `status.json`，见该文件顶部说明）落盘，
同时转发一份给 `self.api.tasks`，在宿主的后台任务面板里也能看到。

CoreAPI 命名空间速查（详见 mybooks/mybooks `webserver/toolbox/core_api.py`）：
    self.api.calibre   书库读写：all_book_ids / get_metadata / set_metadata
    self.api.tasks     后台任务：create_task / update_progress / complete_task
    self.api.storage   工具专属数据目录：get_work_dir（这里用来存 status.json）
"""
import logging
import time

from webserver.i18n import _
from webserver.services import AsyncService
from webserver.handlers.base import BaseHandler, js, auth
from webserver.toolbox.base_tool import BaseTool

from . import job_store
from .job_store import JobStore
from .meta import RULES, guess_tags

# 每处理这么多本书，或者过了这么多秒，落一次盘/推一次任务面板进度——避免逐本书都写文件
_PROGRESS_EVERY_BOOKS = 5
_PROGRESS_EVERY_SECONDS = 1.0


class TagsCleaner(BaseTool):
    # 后台任务面板里显示的任务名称（会经过 i18n 处理）
    service_item_name = "标签清理"

    @staticmethod
    def info():
        return {
            "tool_id": "tags_cleaner",
            "name": "标签清理",
            "description": "使用基础规则清理书库中无效的标签",
            "revision": "0.1.0",
            "author": "PoxenStudio",
            "publish_date": "2026-09-16",
            "repo_url": "https://github.com/PoxenStudio/tool_tags_cleaner",
        }

    def _store(self) -> JobStore:
        return JobStore(self.api.storage.get_work_dir())

    # -- 规则展示 --

    @AsyncService.register_function
    def get_rules(self) -> list:
        return RULES

    # -- 任务状态 --

    @AsyncService.register_function
    def get_status(self) -> dict:
        return job_store.public_status(self._store().read())

    @AsyncService.register_function
    def cancel(self) -> dict:
        if not self._store().request_cancel():
            raise RuntimeError(_("没有可取消的运行中任务"))
        return job_store.public_status(self._store().read())

    # -- 启动任务 --

    @AsyncService.register_function
    def start(self) -> dict:
        store = self._store()
        book_ids = self.api.calibre.all_book_ids()

        job = job_store.empty_stats()
        job.update({
            "status": job_store.STATUS_RUNNING,
            "total": len(book_ids),
            "cancel_requested": False,
            "error": None,
            "finished_at": None,
        })
        started, current = store.try_start(job)
        if not started:
            # 已经有一个任务在跑，直接把它的状态返回给前端展示，不重复启动
            return job_store.public_status(current)

        task_id = self.api.tasks.create_task(
            progress_data={"status": "starting", "total": len(book_ids)}
        )
        self._run(book_ids=book_ids, task_id=task_id)
        return job_store.public_status(store.read())

    @AsyncService.register_service
    def _run(self, book_ids, task_id):
        # `register_service` 把这一整个调用挪到后台线程执行；是否允许"入队"由
        # start() 里的 try_start() 原子把关，所以任何时候最多只有一次清理在跑。
        store = self._store()
        stats = store.read() or job_store.empty_stats()
        total = len(book_ids)
        last_flush = 0.0

        def flush(progress: int, force: bool = False):
            nonlocal last_flush
            now = time.time()
            if not force and (now - last_flush) < _PROGRESS_EVERY_SECONDS:
                return
            last_flush = now
            store.update(**stats)
            self.api.tasks.update_progress(task_id, progress, progress_data={
                "processed": stats["processed"], "total": total, "changed": stats["changed"],
            })

        try:
            for idx, book_id in enumerate(book_ids, 1):
                if store.is_cancel_requested():
                    stats["status"] = job_store.STATUS_CANCELLED
                    break
                try:
                    mi = self.api.calibre.get_metadata(book_id)
                    old_tags = list(mi.tags or [])
                    new_tags = guess_tags(old_tags)
                    if new_tags != old_tags:
                        mi.set("tags", new_tags)
                        self.api.calibre.set_metadata(book_id, mi, force_changes=True)
                        stats["changed"] += 1
                    else:
                        stats["unchanged"] += 1
                    stats["current_book"] = mi.title or str(book_id)
                except Exception as err:
                    stats["errors"] += 1
                    samples = stats.get("error_samples") or []
                    if len(samples) < 5:
                        samples.append({"book_id": book_id, "error": str(err)})
                    stats["error_samples"] = samples
                    logging.warning(
                        "[tags_cleaner] Failed to clean tags for book_id=%s: %s", book_id, err
                    )

                stats["processed"] = idx
                is_last = idx == total or idx % _PROGRESS_EVERY_BOOKS == 0
                flush(int(idx * 100 / total) if total else 100, force=is_last)

            if stats.get("status") != job_store.STATUS_CANCELLED:
                stats["status"] = job_store.STATUS_COMPLETED
            stats["finished_at"] = time.time()
            store.update(**stats)
            self.api.tasks.complete_task(task_id)
        except Exception as err:
            logging.error("[tags_cleaner] Cleaning job failed: %s", err)
            stats["status"] = job_store.STATUS_FAILED
            stats["error"] = str(err)
            stats["finished_at"] = time.time()
            store.update(**stats)
            self.api.tasks.complete_task(task_id, error_message=str(err))


# ---------------------------------------------------------------------------
# HTTP handlers（manifest.json 的 api_routes 声明，见该文件）


class RulesHandler(BaseHandler):
    @js
    @auth
    def get(self):
        return {"err": "ok", "data": TagsCleaner().get_rules()}


class StatusHandler(BaseHandler):
    @js
    @auth
    def get(self):
        return {"err": "ok", "data": TagsCleaner().get_status()}


class StartHandler(BaseHandler):
    @js
    @auth
    def post(self):
        return {"err": "ok", "data": TagsCleaner().start()}


class CancelHandler(BaseHandler):
    @js
    @auth
    def post(self):
        try:
            data = TagsCleaner().cancel()
        except RuntimeError as err:
            return {"err": "cancel.invalid", "msg": str(err)}
        return {"err": "ok", "data": data}
