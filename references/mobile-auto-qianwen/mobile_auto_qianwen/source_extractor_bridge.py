"""Bridge module: invokes the Qianwen source extractor JS script.

This module is the integration layer between the Python mobile automation
(千问手机端) and the Node.js source extractor (千问 source extractor).

Pipeline:
    手机端捕获 share_url + linked_natural_question
        -> 本桥接模块校验参数
        -> 调用 node qianwen-source-extractor/run.js
        -> JS 脚本通过 CDP 访问分享页、提取来源、写回飞书
        -> 返回结构化结果供 Python 端记录与回写跟踪

设计要点：
    * 参数校验：share_url 必须为千问分享链接，natural_question 不得为空
    * 进程间通信：subprocess + 临时 JSON 输出文件，避免 stdout 截断
    * 重试机制：指数退避，可配置次数
    * 日志：写入 output_dir 下的 source-extractor.log
    * 原子性：JS 脚本内部使用 lark-cli 批量创建，失败时不写入半成品
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .time_utils import now_iso, stamp


# 默认配置：与项目根目录的 qianwen-source-extractor/run.js 对齐
DEFAULT_SCRIPT_RELATIVE = Path("qianwen-source-extractor", "run.js")
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_BASE = 2.0  # 指数退避基数（秒）

# 千问分享链接校验：https://www.qianwen.com/share/chat/<id>?biz_id=ai_qwen
QIANWEN_SHARE_URL_RE = re.compile(
    r"^https?://(www\.)?qianwen\.com/share/chat/[A-Za-z0-9]+(\?.*)?$",
    re.IGNORECASE,
)


class SourceExtractorError(RuntimeError):
    """Raised when the JS source extractor cannot complete successfully."""


@dataclass
class ExtractorOptions:
    """Runtime configuration for the JS source extractor."""

    script_path: str = ""  # 留空则自动定位到项目根的 qianwen-source-extractor/run.js
    cdp_url: str = DEFAULT_CDP_URL
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff_base: float = DEFAULT_RETRY_BACKOFF_BASE
    node_binary: str = "node"  # Windows 下通常已在 PATH
    extra_args: list[str] = field(default_factory=list)

    @classmethod
    def from_task_options(cls, task_options: dict) -> "ExtractorOptions":
        """从 task['options']['sourceExtractor'] 子字典构造。"""
        cfg = task_options.get("sourceExtractor") or {}
        return cls(
            script_path=cfg.get("scriptPath", ""),
            cdp_url=cfg.get("cdpUrl", DEFAULT_CDP_URL),
            timeout_seconds=int(cfg.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)),
            max_retries=int(cfg.get("maxRetries", DEFAULT_MAX_RETRIES)),
            retry_backoff_base=float(cfg.get("retryBackoffBase", DEFAULT_RETRY_BACKOFF_BASE)),
            node_binary=cfg.get("nodeBinary", "node"),
            extra_args=list(cfg.get("extraArgs", [])),
        )


def resolve_script_path(explicit: str = "") -> Path:
    """Locate the extractor run.js script from an explicit or default path."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise SourceExtractorError(f"Source extractor script not found: {p}")
        return p
    # 项目根 = mobile_auto_qianwen 的父目录
    here = Path(__file__).resolve().parent
    candidate = here.parent / DEFAULT_SCRIPT_RELATIVE
    if candidate.exists():
        return candidate
    # 兜底：当前工作目录
    candidate = Path.cwd() / DEFAULT_SCRIPT_RELATIVE
    if candidate.exists():
        return candidate
    raise SourceExtractorError(
        f"Could not locate qianwen-source-extractor/run.js. "
        f"Checked: {here.parent / DEFAULT_SCRIPT_RELATIVE} and {Path.cwd() / DEFAULT_SCRIPT_RELATIVE}. "
        f"Pass options.sourceExtractor.scriptPath explicitly."
    )


def validate_params(share_url: str, natural_question: str) -> None:
    """Validate the share URL and natural question before extraction."""
    if not share_url or not share_url.strip():
        raise SourceExtractorError("share_url is required (mobile side must capture the answer share link first)")
    if not natural_question or not natural_question.strip():
        raise SourceExtractorError("natural_question is required (linked natural question from Feishu)")
    if not QIANWEN_SHARE_URL_RE.match(share_url.strip()):
        raise SourceExtractorError(
            f"share_url does not look like a Qianwen share URL: {share_url[:80]!r}. "
            f"Expected: https://www.qianwen.com/share/chat/<id>"
        )


def _build_command(
    options: ExtractorOptions,
    script_path: Path,
    share_url: str,
    natural_question: str,
    base_token: str,
    table_id: str,
    output_file: Path,
) -> list[str]:
    """Build the node command used to invoke the extractor."""
    cmd = [
        options.node_binary,
        str(script_path),
        "--url",
        share_url,
        "--natural-question",
        natural_question,
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--cdp",
        options.cdp_url,
        "--output",
        str(output_file),
    ]
    cmd.extend(options.extra_args)
    return cmd


def _invoke_once(
    options: ExtractorOptions,
    script_path: Path,
    share_url: str,
    natural_question: str,
    base_token: str,
    table_id: str,
    output_file: Path,
    logger: logging.Logger,
    attempt: int,
) -> dict:
    """Invoke the JS extractor once and parse its output artifacts."""
    cmd = _build_command(options, script_path, share_url, natural_question, base_token, table_id, output_file)
    logger.info("Attempt %d: %s", attempt, " ".join(cmd[:6]) + " ...")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=options.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceExtractorError(
            f"JS source extractor timed out after {options.timeout_seconds}s (attempt {attempt})"
        ) from exc
    except FileNotFoundError as exc:
        raise SourceExtractorError(
            f"Node.js binary not found: {options.node_binary}. Install Node.js or set options.sourceExtractor.nodeBinary."
        ) from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    logger.info("Attempt %d exit code: %d", attempt, proc.returncode)
    if stdout:
        logger.info("Attempt %d stdout (tail):\n%s", attempt, _tail(stdout, 1200))
    if stderr:
        logger.warning("Attempt %d stderr (tail):\n%s", attempt, _tail(stderr, 1200))

    if proc.returncode != 0:
        raise SourceExtractorError(
            f"JS source extractor exited with code {proc.returncode} (attempt {attempt}). "
            f"stderr: {_tail(stderr, 400)}"
        )

    # JS 脚本通过 --output 写入提取结果 JSON；写回飞书的结果在 stdout
    result_payload: dict = {
        "ok": True,
        "exitCode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "outputFile": str(output_file),
        "extractedAt": now_iso(),
        "attempt": attempt,
    }

    if output_file.exists():
        try:
            extracted = json.loads(output_file.read_text(encoding="utf-8"))
            result_payload["extractedSources"] = extracted
            result_payload["sourceCount"] = int(extracted.get("count", 0)) if extracted.get("ok") else 0
            result_payload["extractOk"] = bool(extracted.get("ok"))
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse output file %s: %s", output_file, exc)
            result_payload["extractOk"] = False
            result_payload["extractError"] = f"output_file_parse_failed: {exc}"
    else:
        logger.warning("Output file not created by JS script: %s", output_file)
        result_payload["extractOk"] = False
        result_payload["extractError"] = "output_file_not_created"

    # 解析 stdout 中的写回结果（run.js 末尾会打印 "Write result: {...}"）
    write_result = _parse_write_result(stdout)
    if write_result is not None:
        result_payload["feishuWriteResult"] = write_result
        result_payload["feishuWriteOk"] = bool(write_result.get("ok"))
    else:
        result_payload["feishuWriteOk"] = None  # 未知（dry-run 或解析失败）

    return result_payload


def _tail(text: str, max_chars: int) -> str:
    """Keep only the tail of a long log string."""
    if len(text) <= max_chars:
        return text
    return "..." + text[-max_chars:]


_WRITE_RESULT_RE = re.compile(r"Write result:\s*(\{.*\})", re.DOTALL)


def _parse_write_result(stdout: str) -> dict | None:
    """Extract the JSON writeback payload from stdout."""
    match = _WRITE_RESULT_RE.search(stdout)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _get_logger(output_dir: str | Path) -> logging.Logger:
    """Create or reuse a logger for one extraction run."""
    logger = logging.getLogger(f"qianwen.source_extractor.{stamp()}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    log_path = Path(output_dir, "source-extractor.log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    except OSError:
        pass
    # 同时输出到根 logger（控制台）
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("[source-extractor] %(message)s"))
    logger.addHandler(console)
    return logger


def run_source_extractor(
    share_url: str,
    natural_question: str,
    base_token: str,
    table_id: str,
    output_dir: str | Path,
    options: ExtractorOptions | None = None,
) -> dict:
    """Run the full JS extraction flow with retries and logging."""
    options = options or ExtractorOptions()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = _get_logger(output_dir)

    # 1) 参数校验
    validate_params(share_url, natural_question)
    if not base_token:
        raise SourceExtractorError("base_token is required for Feishu source table writeback")
    if not table_id:
        raise SourceExtractorError("table_id is required for Feishu source table writeback")

    # 2) 定位脚本
    script_path = resolve_script_path(options.script_path)
    logger.info("Script: %s", script_path)
    logger.info("Share URL: %s", share_url)
    logger.info("Natural question: %s", natural_question)
    logger.info("Feishu base=%s table=%s", base_token, table_id)

    # 3) 带重试的调用
    attempts: list[dict] = []
    last_error: Exception | None = None
    output_file = output_dir / f"sources-{stamp()}.json"

    for attempt in range(1, options.max_retries + 2):  # max_retries + 1 次总尝试
        try:
            result = _invoke_once(
                options,
                script_path,
                share_url,
                natural_question,
                base_token,
                table_id,
                output_file,
                logger,
                attempt,
            )
            result["attempts"] = attempts
            result["status"] = "success"
            logger.info("Extraction succeeded on attempt %d (sources=%d)", attempt, result.get("sourceCount", 0))
            return result
        except SourceExtractorError as exc:
            last_error = exc
            attempts.append({
                "attempt": attempt,
                "error": str(exc),
                "timestamp": now_iso(),
            })
            logger.warning("Attempt %d failed: %s", attempt, exc)
            if attempt <= options.max_retries:
                backoff = options.retry_backoff_base ** attempt
                logger.info("Retrying in %.1fs...", backoff)
                time.sleep(backoff)

    # 所有重试均失败
    logger.error("All %d attempts failed. Last error: %s", len(attempts), last_error)
    return {
        "ok": False,
        "status": "failed",
        "error": str(last_error) if last_error else "unknown",
        "attempts": attempts,
        "extractedAt": now_iso(),
        "sourceCount": 0,
        "extractOk": False,
        "feishuWriteOk": False,
        "outputFile": str(output_file),
    }
