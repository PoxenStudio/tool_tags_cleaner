"""
标签清理 —— MyBooks Toolbox 工具后端代码

用 `meta.guess_tags()` 里的一组固定规则（见该文件顶部 `RULES`，同时也是前端"清理规则"
面板展示的内容）遍历书库所有书籍，清掉广告/推广类无效标签。规则本身在 meta.py 里维护，
这个文件只管"怎么跑起来、怎么展示进度"。

耗时的遍历用 `AsyncService.register_service` 放到后台线程执行，进度通过
`job_store.JobStore`（工具自己的 `status.json`，见该文件顶部说明）落盘，
同时转发一份给 `self.api.tasks`，在宿主的后台任务面板里也能看到。

CoreAPI 命名空间速查（详见 mybooks/mybooks `webserver/toolbox/core_api.py`）：
    self.api.calibre   书库读写：all_book_ids / get_category_with_count / get_field_map /
                       search_ids / get_metadata / set_metadata
    self.api.tasks     后台任务：create_task / update_progress / complete_task
    self.api.storage   工具专属数据目录：get_work_dir（这里用来存 status.json）
"""
import json
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
# 按标签搜书时，一条 Calibre 搜索表达式里最多拼多少个标签
_SEARCH_TAG_BATCH_SIZE = 50
# 按标签搜书最多允许的批次数：要反复搜 5 批以上时，搜书本身比直接遍历书籍还慢
_MAX_SEARCH_ROUNDS = 5
# 遍历书籍时每批读取多少本书的 tags（批量读字段，避免逐本 get_metadata）
_FIELD_READ_BATCH_SIZE = 500


def _tag_search_expression(tags):
    terms = []
    for tag in tags:
        escaped = tag.replace("\\", "\\\\").replace('"', '\\"')
        terms.append('tags:"=' + escaped + '"')
    return " or ".join(terms)


def _max_rare_tag_count(book_count: int) -> int:
    """书库不足 100 本时最多只允许 1，避免小书库误删大量标签。"""
    return 1 if book_count < 100 else 3


def _clamp_rare_tag_count(value, book_count: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, _max_rare_tag_count(book_count)))


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

    @AsyncService.register_function
    def get_limits(self) -> dict:
        book_count = len(self.api.calibre.all_book_ids())
        return {
            "book_count": book_count,
            "max_rare_tag_count": _max_rare_tag_count(book_count),
        }

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
    def start(self, rare_tag_count: int = 0) -> dict:
        store = self._store()
        book_ids = self.api.calibre.all_book_ids()
        rare_tag_count = _clamp_rare_tag_count(rare_tag_count, len(book_ids))

        job = job_store.empty_stats()
        job.update({
            "status": job_store.STATUS_RUNNING,
            "total": len(book_ids),
            "target_total": len(book_ids),
            "rare_tag_count": rare_tag_count,
            "phase": "scanning" if rare_tag_count else "cleaning",
            "scan_total": 0,
            "cancel_requested": False,
            "error": None,
            "finished_at": None,
        })
        started, current = store.try_start(job)
        if not started:
            # 已经有一个任务在跑，直接把它的状态返回给前端展示，不重复启动
            return job_store.public_status(current)

        task_id = self.api.tasks.create_task(progress_data={
            "status": "starting", "total": len(book_ids),
            "rare_tag_count": rare_tag_count,
        })
        self._run(
            book_ids=book_ids, task_id=task_id, rare_tag_count=rare_tag_count
        )
        return job_store.public_status(store.read())

    @AsyncService.register_service
    def _run(self, book_ids, task_id, rare_tag_count=0):
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
                "phase": stats.get("phase"),
                "processed": stats["processed"],
                "total": total,
                "target_total": stats.get("target_total", total),
                "scanned": stats.get("scanned", 0),
                "scan_total": stats.get("scan_total", 0),
                "changed": stats["changed"],
            })

        try:
            # 1) 先从标签统计里整理出"需要删除的标签"，这一步不读任何书籍元数据。
            #    get_category_with_count() 由数据库一次统计完成（3000+ 个标签也只是一条
            #    SQL），比按标签一批批 search_ids() 去遍历书库快得多。
            categories = self.api.calibre.get_category_with_count("tag")
            tags_to_search = []      # 需要清理的标签原始名（命中规则 / 关联书籍数过少）
            rare_tag_names = set()   # 关联书籍数 <= rare_tag_count 的标签（去掉首尾空白）
            for category in categories:
                name = category["name"]
                count = category["count"]
                # count 为 0 的标签没有关联书籍，删了也改不到任何书，直接跳过
                if name is None or count <= 0:
                    continue
                name = str(name)
                normalized = name.strip()
                if not normalized:
                    continue
                if rare_tag_count and count <= rare_tag_count:
                    rare_tag_names.add(normalized)
                    tags_to_search.append(name)
                elif guess_tags([name]) != [name]:
                    tags_to_search.append(name)

            # 2) 决定处理范围。
            #    需要删除的标签不多时，按标签分批搜出关联书籍，只处理这些书；
            #    要搜 5 批以上时，搜书本身（每次都要扫一遍书库）比重点遍历还慢，
            #    直接改为遍历书籍列表——按批读取 tags 字段，只对真正要改的书读写元数据。
            tags_to_search = sorted(tags_to_search)
            search_rounds = (
                (len(tags_to_search) + _SEARCH_TAG_BATCH_SIZE - 1)
                // _SEARCH_TAG_BATCH_SIZE
            )
            use_search = 0 < search_rounds <= _MAX_SEARCH_ROUNDS
            stats["phase"] = "scanning" if rare_tag_count else "cleaning"

            if rare_tag_count:
                # 搜索阶段占前 50%；走遍历策略时没有搜索批次，扫描直接算作完成
                stats["scan_total"] = (
                    len(tags_to_search) if use_search else len(categories)
                )
                stats["scanned"] = 0
                # 先落一次盘，让前端马上拿到真实的扫描总量（而不是启动时的 0）
                flush(0, force=True)

            if not tags_to_search:
                # 没有任何需要清理的标签：整库都不会变化，直接结束
                targets = []
                stats["unchanged"] += total
            elif use_search:
                candidates = set()
                for start in range(
                    0, len(tags_to_search), _SEARCH_TAG_BATCH_SIZE
                ):
                    if store.is_cancel_requested():
                        stats["status"] = job_store.STATUS_CANCELLED
                        break
                    batch = tags_to_search[
                        start:start + _SEARCH_TAG_BATCH_SIZE
                    ]
                    candidates.update(self.api.calibre.search_ids(
                        _tag_search_expression(batch)
                    ))
                    stats["scanned"] = start + len(batch)
                    if rare_tag_count:
                        flush(int(stats["scanned"] * 50 / len(tags_to_search)))
                if stats.get("status") == job_store.STATUS_CANCELLED:
                    targets = []
                else:
                    targets = [
                        book_id for book_id in book_ids if book_id in candidates
                    ]
                    stats["unchanged"] += total - len(targets)
            else:
                if rare_tag_count:
                    stats["scanned"] = stats["scan_total"]
                targets = list(book_ids)

            if stats.get("status") != job_store.STATUS_CANCELLED:
                target_total = len(targets)
                progress_start = 50 if rare_tag_count else 0
                progress_span = 100 - progress_start
                stats["phase"] = "cleaning"
                stats["target_total"] = target_total
                stats["current_book"] = ""
                flush(progress_start, force=True)

                for chunk_start in range(0, target_total, _FIELD_READ_BATCH_SIZE):
                    if store.is_cancel_requested():
                        stats["status"] = job_store.STATUS_CANCELLED
                        break
                    chunk = targets[
                        chunk_start:chunk_start + _FIELD_READ_BATCH_SIZE
                    ]
                    # 一次读一整批书的 tags（内存缓存，不做元数据构造），
                    # 只有确实需要改的书才去 get_metadata + set_metadata
                    tag_map = self.api.calibre.get_field_map("tags", chunk)
                    for book_id in chunk:
                        if store.is_cancel_requested():
                            stats["status"] = job_store.STATUS_CANCELLED
                            break
                        try:
                            old_tags = [
                                str(tag) for tag in (tag_map.get(book_id) or ())
                            ]
                            kept_tags = (
                                [
                                    tag for tag in old_tags
                                    if tag.strip() not in rare_tag_names
                                ]
                                if rare_tag_names else old_tags
                            )
                            new_tags = guess_tags(kept_tags)
                            if new_tags != old_tags:
                                mi = self.api.calibre.get_metadata(book_id)
                                mi.set("tags", new_tags)
                                self.api.calibre.set_metadata(
                                    book_id, mi, force_changes=True
                                )
                                stats["changed"] += 1
                                stats["current_book"] = mi.title or str(book_id)
                            else:
                                stats["unchanged"] += 1
                        except Exception as err:
                            stats["errors"] += 1
                            samples = stats.get("error_samples") or []
                            if len(samples) < 5:
                                samples.append({
                                    "book_id": book_id, "error": str(err),
                                })
                            stats["error_samples"] = samples
                            logging.warning(
                                "[tags_cleaner] Failed to clean tags for book_id=%s: %s",
                                book_id, err,
                            )

                        stats["processed"] += 1
                        is_last = (
                            stats["processed"] == target_total
                            or stats["processed"] % _PROGRESS_EVERY_BOOKS == 0
                        )
                        progress = (
                            progress_start
                            + int(stats["processed"] * progress_span / target_total)
                            if target_total else 100
                        )
                        flush(progress, force=is_last)

                if not target_total and rare_tag_count:
                    flush(100, force=True)

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


class LimitsHandler(BaseHandler):
    @js
    @auth
    def get(self):
        return {"err": "ok", "data": TagsCleaner().get_limits()}


class StatusHandler(BaseHandler):
    @js
    @auth
    def get(self):
        return {"err": "ok", "data": TagsCleaner().get_status()}


class StartHandler(BaseHandler):
    @js
    @auth
    def post(self):
        try:
            body = json.loads(self.request.body or b"{}")
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
        except (TypeError, ValueError):
            return {"err": "params.invalid", "msg": _("请求体不是合法 JSON")}
        return {
            "err": "ok",
            "data": TagsCleaner().start(body.get("rare_tag_count", 0)),
        }


class CancelHandler(BaseHandler):
    @js
    @auth
    def post(self):
        try:
            data = TagsCleaner().cancel()
        except RuntimeError as err:
            return {"err": "cancel.invalid", "msg": str(err)}
        return {"err": "ok", "data": data}
