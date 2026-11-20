"""
EPUB文件精简 —— MyBooks Toolbox 工具后端代码

流程：
1. 按书名在书库中搜索含 EPUB 格式的书籍（只返回有 EPUB 文件的）；
2. 选定一本书后，调用 Calibre 的 ``ebook-polish -i -u -U``：
     -i  无损压缩图片
     -u  清理未被使用的 CSS 规则
     -U  升级书籍内部结构（如 EPUB 2 → EPUB 3）
3. 把生成文件与原文件比较体积：只有确实变小时才允许下载或写回书库，
   否则直接丢弃生成文件并把结果告诉前端。

写回书库走 ``self.api.calibre.add_format``（Calibre 会自行失效缓存）；下载模式下
生成文件保留在工具工作目录，由 DownloadHandler 读取后返回给浏览器。
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from typing import Optional

from webserver.handlers.base import BaseHandler, js, is_admin
from webserver.i18n import _
from webserver.services import AsyncService
from webserver.toolbox.base_tool import BaseTool

POLISH_CMD = "ebook-polish"
# 与需求一致：压缩图片、清理未使用 CSS、升级内部结构
POLISH_OPTIONS = ["-i", "-u", "-U"]
# 单本书的打磨超时（秒）；大画册类 EPUB 处理图片可能较慢
POLISH_TIMEOUT = 10 * 60

MAX_SEARCH_RESULTS = 50
# 临时任务目录保留 7 天，之后在工具下次启动/首次使用时清理
WORK_RETENTION_SECONDS = 7 * 24 * 60 * 60
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _calibre_literal(value: str) -> str:
    """把用户输入转义成可以安全放进 Calibre 搜索表达式的字符串字面量。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class EpubMinify(BaseTool):
    # 后台任务面板里显示的任务名称（会经过 i18n 处理）
    service_item_name = "EPUB文件精简"

    # job_id -> 任务状态。只保留最近若干条，避免长期运行后无限增长
    _jobs: dict = {}
    _jobs_order: list = []
    _jobs_lock = threading.Lock()
    _MAX_JOBS = 20

    # 进程内只执行一次临时目录清理
    _cleanup_lock = threading.Lock()
    _cleanup_done = False

    @staticmethod
    def info():
        return {
            "tool_id": "epub_minify",
            "name": "EPUB文件精简",
            "description": "按书名搜索书库中的 EPUB 书籍，调用 ebook-polish 清理未使用 CSS、"
                           "压缩图片并升级内部结构；只有体积变小时才下载或替换原文件。",
            "revision": "0.1.0",
            "author": "Horky",
            "publish_date": "2026-11-20",
            "repo_url": "https://github.com/PoxenStudio/tool_epub_minify",
        }

    # ------------------------------------------------------------------
    # 任务状态（内存，随进程重启丢失；单本书任务足够用）
    # ------------------------------------------------------------------

    @classmethod
    def _put_job(cls, job_id: str, data: dict) -> None:
        with cls._jobs_lock:
            cls._jobs[job_id] = data
            if job_id not in cls._jobs_order:
                cls._jobs_order.append(job_id)
            while len(cls._jobs_order) > cls._MAX_JOBS:
                oldest = cls._jobs_order.pop(0)
                cls._jobs.pop(oldest, None)

    @classmethod
    def _update_job(cls, job_id: str, **changes) -> None:
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            if job is not None:
                job.update(changes)

    @classmethod
    def _get_job(cls, job_id: str) -> Optional[dict]:
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            return dict(job) if job else None

    @classmethod
    def _cleanup_old_work_dirs(cls, api) -> None:
        """首次使用时删除超过 7 天的任务目录（每个进程只执行一次）。"""
        with cls._cleanup_lock:
            if cls._cleanup_done:
                return
            cls._cleanup_done = True
        try:
            root = api.storage.get_work_dir("")
            now = time.time()
            removed = 0
            for name in os.listdir(root):
                if not _JOB_ID_RE.match(name):
                    continue
                path = os.path.join(root, name)
                try:
                    if not os.path.isdir(path):
                        continue
                    if now - os.path.getmtime(path) <= WORK_RETENTION_SECONDS:
                        continue
                except OSError:
                    continue
                api.storage.cleanup_work_dir(path)
                removed += 1
            if removed:
                logging.info("[epub_minify] 已清理 %d 个超过 7 天的临时目录", removed)
        except Exception as err:  # noqa: BLE001
            logging.warning("[epub_minify] 清理临时目录失败：%s", err)

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    @AsyncService.register_function
    def search(self, query: str) -> list:
        """按书名搜索含 EPUB 的书籍。query 为空时返回书库中的 EPUB 书籍。"""
        self._cleanup_old_work_dirs(self.api)

        query = (query or "").strip()
        expr = "format:epub"
        if query:
            expr = 'title:"%s" and format:epub' % _calibre_literal(query)

        books = self.api.calibre.search_books(
            expr, max_results=MAX_SEARCH_RESULTS, include_comments=False
        )
        results = []
        for book in books:
            book_id = book.get("id")
            if not isinstance(book_id, int):
                continue
            path = self.api.calibre.format_abspath(book_id, "EPUB")
            if not path or not os.path.exists(path):
                # 搜索表达式理论上已经过滤掉没有 EPUB 的书，这里再挡一次
                # （文件可能被外部删除、书库缓存未刷新）
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            results.append({
                "book_id": book_id,
                "title": book.get("title") or "",
                "author": book.get("author") or ", ".join(book.get("authors") or []),
                "authors": book.get("authors") or [],
                "size": size,
                "cover": book.get("thumb") or book.get("img") or "",
            })
        return results

    # ------------------------------------------------------------------
    # 开始处理
    # ------------------------------------------------------------------

    @AsyncService.register_function
    def start(self, book_id: int, mode: str) -> dict:
        """受理一次精简请求并投递到后台线程。mode: replace | download。"""
        self._cleanup_old_work_dirs(self.api)

        if mode not in ("replace", "download"):
            raise ValueError(_("不支持的处理方式"))

        book = self.api.calibre.get_data_as_dict([book_id])
        if not book:
            raise RuntimeError(_("书籍不存在：ID=%d") % book_id)
        book = book[0]
        formats = [f.upper() for f in (book.get("available_formats") or [])]
        if "EPUB" not in formats:
            raise RuntimeError(_("该书籍没有 EPUB 格式"))

        source_path = self.api.calibre.format_abspath(book_id, "EPUB")
        if not source_path or not os.path.exists(source_path):
            raise RuntimeError(_("找不到 EPUB 文件，可能已被移除"))

        title = book.get("title") or "book-%d" % book_id
        job_id = uuid.uuid4().hex
        task_id = self.api.tasks.create_task(progress_data={
            "book_id": book_id, "title": title, "mode": mode,
        })

        self._put_job(job_id, {
            "job_id": job_id,
            "status": "running",
            "stage": "queued",
            "progress": 0,
            "book_id": book_id,
            "title": title,
            "mode": mode,
            "old_size": 0,
            "new_size": 0,
            "saved": 0,
            "saved_percent": 0,
            "replaced": False,
            "download_ready": False,
            "error": "",
        })

        # register_service 会把这次调用投递到后台线程执行
        self._run(job_id, book_id, mode, title, source_path, task_id)
        return {"job_id": job_id, "task_id": task_id}

    @AsyncService.register_service
    def _run(self, job_id: str, book_id: int, mode: str, title: str,
             source_path: str, task_id: int) -> None:
        output_path = ""
        try:
            self._update_progress(job_id, task_id, 5, "locating")

            try:
                old_size = os.path.getsize(source_path)
            except OSError as err:
                raise RuntimeError(_("读取原文件失败：%s") % err)

            work_dir = self.api.storage.get_work_dir("job-" + job_id)
            output_path = os.path.join(work_dir, "minified.epub")
            log_path = os.path.join(work_dir, "polish.log")

            self._update_progress(job_id, task_id, 15, "polishing")
            self._polish(source_path, output_path, log_path)

            self._update_progress(job_id, task_id, 85, "comparing")

            if not os.path.exists(output_path):
                raise RuntimeError(_("ebook-polish 没有生成输出文件"))
            new_size = os.path.getsize(output_path)

            if new_size >= old_size:
                # 没有变小：不做任何替换/下载，清掉生成文件
                self._remove(output_path)
                self._finish(job_id, task_id, "no_improvement", {
                    "old_size": old_size,
                    "new_size": new_size,
                })
                logging.info(
                    "[epub_minify] book_id=%s 未变小（%s -> %s），已跳过",
                    book_id, old_size, new_size,
                )
                return

            saved = old_size - new_size
            percent = round(saved * 100.0 / old_size, 1) if old_size else 0.0

            if mode == "download":
                self._finish(job_id, task_id, "completed", {
                    "old_size": old_size,
                    "new_size": new_size,
                    "saved": saved,
                    "saved_percent": percent,
                    "download_ready": True,
                    "stage": "done",
                })
                logging.info(
                    "[epub_minify] book_id=%s 精简完成（下载）：%s -> %s",
                    book_id, old_size, new_size,
                )
                return

            self._update_progress(job_id, task_id, 95, "saving")
            self.api.calibre.add_format(book_id, "EPUB", output_path)
            self._remove(output_path)
            self._finish(job_id, task_id, "completed", {
                "old_size": old_size,
                "new_size": new_size,
                "saved": saved,
                "saved_percent": percent,
                "replaced": True,
                "stage": "done",
            })
            logging.info(
                "[epub_minify] book_id=%s 已替换（%s -> %s）",
                book_id, old_size, new_size,
            )

        except Exception as err:  # noqa: BLE001
            logging.error("[epub_minify] 精简失败 book_id=%s: %s", book_id, err)
            if output_path:
                self._remove(output_path)
            self._update_job(
                job_id, status="failed", stage="failed", progress=100,
                error=str(err),
            )
            try:
                self.api.tasks.complete_task(task_id, error_message=str(err))
            except Exception as task_err:  # noqa: BLE001
                logging.warning("[epub_minify] 标记任务失败时出错：%s", task_err)
            return

    def _polish(self, source_path: str, output_path: str, log_path: str) -> None:
        """调用 ebook-polish；输出写入 log 文件，失败时把日志尾部带进异常。"""
        env = os.environ.copy()
        # 与宿主 ConverterService 保持一致：容器里 Chromium/QtWebEngine 需要关闭沙箱
        env.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
        env.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-ubuntu")

        args = [POLISH_CMD, *POLISH_OPTIONS, source_path, output_path]
        logging.info("[epub_minify] CMD: %s", " ".join(args))
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            try:
                completed = subprocess.run(
                    args, stdout=log, stderr=subprocess.STDOUT,
                    env=env, timeout=POLISH_TIMEOUT, check=False,
                )
            except FileNotFoundError as err:
                raise RuntimeError(_("未找到 ebook-polish 命令")) from err
            except subprocess.TimeoutExpired as err:
                raise RuntimeError(
                    _("ebook-polish 处理超时（%d 秒）") % POLISH_TIMEOUT
                ) from err

        if completed.returncode == 0 and os.path.exists(output_path):
            return

        tail = ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as log:
                tail = log.read()[-800:].strip()
        except OSError:
            pass
        logging.error(
            "[epub_minify] ebook-polish 失败（exit=%s）：%s",
            completed.returncode, tail,
        )
        message = _("ebook-polish 执行失败（退出码 %s）") % completed.returncode
        if tail:
            message = "%s：%s" % (message, tail[-300:])
        raise RuntimeError(message)

    def _update_progress(self, job_id: str, task_id: int, progress: int, stage: str) -> None:
        self._update_job(job_id, progress=progress, stage=stage)
        try:
            job = self._get_job(job_id) or {}
            self.api.tasks.update_progress(task_id, progress, {
                "book_id": job.get("book_id"),
                "title": job.get("title"),
                "stage": stage,
            })
        except Exception as err:  # noqa: BLE001
            logging.warning("[epub_minify] 更新任务进度失败：%s", err)

    def _finish(self, job_id: str, task_id: int, status: str, extra: dict) -> None:
        changes = {
            "status": status,
            "progress": 100,
            "stage": extra.pop("stage", "done"),
        }
        changes.update(extra)
        self._update_job(job_id, **changes)
        try:
            job = self._get_job(job_id) or {}
            self.api.tasks.update_progress(task_id, 100, {
                "book_id": job.get("book_id"),
                "title": job.get("title"),
                "stage": changes["stage"],
                "status": status,
            })
            self.api.tasks.complete_task(task_id)
        except Exception as err:  # noqa: BLE001
            logging.warning("[epub_minify] 结束任务失败：%s", err)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as err:
            logging.warning("[epub_minify] 删除临时文件失败 %s: %s", path, err)

    # ------------------------------------------------------------------
    # 下载用：取任务产物路径（只接受 job_id，不接受前端传路径）
    # ------------------------------------------------------------------

    def download_file(self, job_id: str):
        job = self._get_job(job_id)
        if not job:
            raise RuntimeError(_("任务不存在或已过期"))
        if job.get("status") != "completed" or not job.get("download_ready"):
            raise RuntimeError(_("该任务没有可下载的文件"))
        work_dir = self.api.storage.get_work_dir("job-" + job_id)
        path = os.path.join(work_dir, "minified.epub")
        if not os.path.exists(path):
            raise RuntimeError(_("文件已过期，请重新处理"))
        return job.get("title") or "book", path


# ---------------------------------------------------------------------------
# HTTP handlers（manifest.json 的 api_routes 声明）


class SearchHandler(BaseHandler):
    """GET /search?q=..."""

    @js
    @is_admin
    def get(self):
        query = self.get_argument("q", "")
        try:
            data = EpubMinify().search(query)
        except Exception as err:  # noqa: BLE001
            logging.error("[epub_minify] 搜索失败: %s", err)
            return {"err": "search.failed", "msg": _("搜索书籍失败：%s") % err}
        return {"err": "ok", "data": data}


class StartHandler(BaseHandler):
    """POST /start  {"book_id": 1, "mode": "replace"|"download"}"""

    @js
    @is_admin
    def post(self):
        try:
            body = json.loads(self.request.body or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except (TypeError, ValueError):
            return {"err": "params.invalid", "msg": _("请求体不是合法 JSON")}

        book_id = body.get("book_id")
        mode = body.get("mode")
        if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
            return {"err": "params.invalid", "msg": _("书籍 ID 不合法")}
        if mode not in ("replace", "download"):
            return {"err": "params.invalid", "msg": _("处理方式不合法")}

        try:
            data = EpubMinify().start(book_id, mode)
        except ValueError as err:
            return {"err": "params.invalid", "msg": str(err)}
        except RuntimeError as err:
            return {"err": "start.failed", "msg": str(err)}
        except Exception as err:  # noqa: BLE001
            logging.error("[epub_minify] 启动任务失败: %s", err)
            return {"err": "start.failed", "msg": _("启动任务失败：%s") % err}
        return {"err": "ok", "data": data}


class StatusHandler(BaseHandler):
    """GET /status?job_id=..."""

    @js
    @is_admin
    def get(self):
        job_id = self.get_argument("job_id", "")
        if not _JOB_ID_RE.match(job_id or ""):
            return {"err": "params.invalid", "msg": _("任务 ID 不合法")}
        job = EpubMinify._get_job(job_id)
        if not job:
            return {"err": "status.not_found", "msg": _("任务不存在或已过期")}
        return {"err": "ok", "data": job}


class DownloadHandler(BaseHandler):
    """GET /download?job_id=... —— 返回精简后的 EPUB 文件。"""

    @is_admin
    def get(self):
        from urllib.parse import quote

        job_id = self.get_argument("job_id", "")
        if not _JOB_ID_RE.match(job_id or ""):
            self.set_status(400)
            self.write("Invalid job_id")
            return

        try:
            title, path = EpubMinify().download_file(job_id)
        except RuntimeError as err:
            self.set_status(404)
            self.write(str(err))
            return

        filename = "%s_minified.epub" % title
        # 去掉路径分隔符等会破坏响应头的字符
        filename = re.sub(r'[\\/:*?"<>|\r\n]+', "_", filename).strip() or "minified.epub"
        ascii_name = "minified.epub"
        self.set_header("Content-Type", "application/epub+zip")
        self.set_header(
            "Content-Disposition",
            "attachment; filename=\"%s\"; filename*=UTF-8''%s"
            % (ascii_name, quote(filename)),
        )
        self.set_header("Content-Length", str(os.path.getsize(path)))
        with open(path, "rb") as f:
            # 分块写出，避免把几十 MB 的文件一次性读进内存
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.write(chunk)
