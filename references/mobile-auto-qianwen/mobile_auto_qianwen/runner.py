import argparse
import json
import logging
import time
import traceback
from pathlib import Path

from .adb_client import AdbClient
from .app import (
    capture_answer_page_thinking_content,
    capture_thinking_content,
    click_view_all,
    create_new_chat,
    detect_blocked,
    ensure_app,
    enter_thinking_mode,
    extract_answer_share_link,
    return_to_chat_page,
    send_question,
    wait_for_answer,
)
from .artifacts import save_state
from .feishu_base import build_task_from_feishu, build_feishu_writeback_rows_for_result, planned_writeback, write_feishu_result
from .result_writer import write_result
from .source_extractor_bridge import ExtractorOptions, run_source_extractor
from .task_schema import load_task, normalize_task, summarize_task
from .time_utils import now_iso, stamp


def _setup_run_logger(output_path: str) -> logging.Logger:
    """为整次任务创建一个 run logger，写入 output 同级目录。"""
    logger = logging.getLogger("qianwen.runner")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    log_path = Path(output_path).with_suffix(".log")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    except OSError:
        pass
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("[runner] %(message)s"))
    logger.addHandler(console)
    return logger


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the Qianwen runner."""
    parser = argparse.ArgumentParser(description="Run Qianwen mobile automation through Python + adb.exe.")
    parser.add_argument("--task")
    parser.add_argument("--adb")
    parser.add_argument("--serial", "--device", dest="serial", help="Android adb serial / device id.")
    parser.add_argument("--output", help="Override the result JSON path.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--writeback", action="store_true", help="Create Feishu answer/source records after each successful or partial result.")
    parser.add_argument("--mark-collected", action="store_true", help="With --writeback, set source Feishu rows 是否本次采集 to 否 after successful answer writeback.")
    parser.add_argument("--collect-account", help="Override the 采集账号 field written to Feishu answer rows.")
    parser.add_argument("--base-url", help="Feishu Base URL containing /base/{baseToken}?table=...&view=...")
    parser.add_argument("--base-token")
    parser.add_argument("--table-id")
    parser.add_argument("--view-id")
    parser.add_argument("--base-start", type=int, help="1-based start row in Feishu Base, inclusive.")
    parser.add_argument("--base-end", type=int, help="1-based end row in Feishu Base, inclusive.")
    parser.add_argument("--base-limit", type=int, default=50)
    parser.add_argument("--source-limit", type=int, default=2, help="How many sources to collect per question.")
    parser.add_argument("--platform", default="千问")
    parser.add_argument("--lark-cli", default="lark-cli")
    parser.add_argument("--force-quick", action="store_true")
    parser.add_argument("--debug", action="store_true")
    # —— JS 来源提取器集成 ——
    parser.add_argument(
        "--extract-sources",
        action="store_true",
        help="After capturing the answer share link, invoke the qianwen-source-extractor JS script to extract sources and write them to Feishu.",
    )
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="Chrome DevTools Protocol endpoint for the JS extractor.")
    parser.add_argument("--extractor-script", default="", help="Path to qianwen-source-extractor/run.js. Auto-located if omitted.")
    parser.add_argument("--extractor-timeout", type=int, default=120, help="Per-attempt timeout (seconds) for the JS extractor.")
    parser.add_argument("--extractor-retries", type=int, default=2, help="Max retries for the JS extractor on failure.")
    parser.add_argument("--source-base-token", default="", help="Feishu base_token for the source table. Defaults to the input base_token.")
    parser.add_argument("--source-table-id", default="", help="Feishu table_id for the source table. Defaults to the built-in Qianwen source table.")
    return parser.parse_args()


def question_artifact_dir(output: str, session_name: str, index: int) -> str:
    """Create the artifact directory for a single question."""
    base = Path(output).parent / "snapshots" / f"{session_name}-{index}-{stamp()}"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def failed_writeback_result(exc: Exception, session: dict, result: dict) -> dict:
    """Format a failed Feishu writeback attempt for result storage."""
    error_text = str(exc)
    parsed_error = None
    try:
        parsed_error = json.loads(error_text)
    except json.JSONDecodeError:
        parsed_error = None
    return {
        "finishedAt": now_iso(),
        "status": "failed",
        "errorType": exc.__class__.__name__,
        "error": parsed_error or error_text,
        "traceback": traceback.format_exc(),
        "sourceRecordId": (session.get("meta") or {}).get("feishuRecordId"),
        "question": result.get("question", ""),
        "resultStatus": result.get("status", ""),
        "answerCount": 0,
        "sourceCount": 0,
    }


def run_question(
    adb: AdbClient,
    task: dict,
    session_name: str,
    question_item: dict,
    index: int,
    ensure_result: dict | None = None,
    source_extractor_context: dict | None = None,
    session_meta: dict | None = None,
    run_logger: logging.Logger | None = None,
) -> dict:
    """Run one Qianwen question through answer, thinking, share, and source capture."""
    question = question_item["text"]
    output_dir = question_artifact_dir(task["output"], session_name, index)
    asked_at = now_iso()
    debug = {"notes": [], "artifactsDir": output_dir}
    timing = {}
    _t0 = time.monotonic()
    if run_logger:
        run_logger.info("[%s#%d] start: %s", session_name, index, question[:60])
    try:
        debug["ensureApp"] = ensure_result or {"started": False, "method": "task_level_reuse"}
        timing["ensureApp"] = 0
        _ts = time.monotonic()
        if question_item.get("newChat"):
            new_chat = create_new_chat(adb, output_dir, save_debug_xml=bool(task["options"].get("saveDebugXml", False)))
        else:
            new_chat = {"created": False, "method": "skipped", "requested": False}
        timing["newChat"] = int((time.monotonic() - _ts) * 1000)
        debug["newChat"] = {"requested": question_item.get("newChat"), "result": new_chat}
        if question_item.get("newChat") and not new_chat.get("created"):
            return {
                "index": index,
                "question": question,
                "askedAt": asked_at,
                "finishedAt": now_iso(),
                "answer": "",
                "sources": [],
                "status": "failed",
                "error": new_chat.get("error") or "new_chat_failed",
                "debug": {**debug, "timing": timing},
            }
        _ts = time.monotonic()
        initial = save_state(adb, output_dir, "question-initial")
        blocked = detect_blocked(initial["nodes"])
        timing["checkBlocked"] = int((time.monotonic() - _ts) * 1000)
        if blocked:
            return {
                "index": index,
                "question": question,
                "askedAt": asked_at,
                "finishedAt": now_iso(),
                "answer": "",
                "sources": [],
                "status": "blocked",
                "error": blocked,
                "debug": {**debug, "initialXml": initial["xml"], "screenshot": initial.get("screenshot"), "timing": timing},
            }

        requested_thinking = True if question_item.get("thinking") is None else question_item.get("thinking")
        _ts = time.monotonic()
        thinking = enter_thinking_mode(adb, output_dir) if requested_thinking else {"requested": False, "changed": False, "verified": True}
        timing["enterThinking"] = int((time.monotonic() - _ts) * 1000)
        debug["thinking"] = thinking

        _ts = time.monotonic()
        sent, send_debug = send_question(adb, question, output_dir)
        timing["sendQuestion"] = int((time.monotonic() - _ts) * 1000)
        debug["send"] = send_debug
        if not sent:
            return {
                "index": index,
                "question": question,
                "askedAt": asked_at,
                "finishedAt": now_iso(),
                "answer": "",
                "sources": [],
                "status": "failed",
                "error": send_debug.get("error") or "send_failed",
                "debug": {**debug, "timing": timing},
            }

        _ts = time.monotonic()
        answer_result = wait_for_answer(adb, question, output_dir, timeout=180.0, stable_seconds=int(task["options"].get("waitStableSeconds", 5)))
        timing["waitForAnswer"] = int((time.monotonic() - _ts) * 1000)
        answer = answer_result.get("answer", "")
        debug["answerSamples"] = answer_result["samples"]
        if answer_result.get("nodes"):
            debug["answerXmlNodeCount"] = len(answer_result["nodes"])

        _ts_expert = time.monotonic()
        # Source extraction is intentionally disabled for the current Feishu path.
        # Thinking-detail capture still runs when deep-thinking mode is requested,
        # because Feishu writeback has a dedicated "深度思考" field.
        sources = []
        expert_answer = {"status": "success" if answer else "failed", "thinking": "", "answer": answer_result.get("answer", "")}
        detail_open = False
        try:
            if requested_thinking:
                _t_view = time.monotonic()
                view_all = click_view_all(adb, output_dir)
                timing["clickViewAll"] = int((time.monotonic() - _t_view) * 1000)
                debug["clickViewAll"] = view_all
                _t_cap = time.monotonic()
                if view_all.get("ok"):
                    detail_open = True
                    thinking_capture = capture_thinking_content(
                        adb,
                        output_dir,
                        max_scrolls=int(task["options"].get("expertAnswerMaxScrolls", 5)),
                        ocr_enabled=bool(task["options"].get("thinkingCaptureOcr", False)),
                    )
                    timing["captureThinkingContent"] = int((time.monotonic() - _t_cap) * 1000)
                    expert_answer["thinking"] = thinking_capture.get("content", "")
                    debug["thinkingCapture"] = {
                        "status": thinking_capture.get("status"),
                        "method": "view_all_detail",
                        "contentLength": len(thinking_capture.get("content", "")),
                        "snapshots": thinking_capture.get("snapshots", []),
                    }
                else:
                    thinking_capture = capture_answer_page_thinking_content(
                        adb,
                        output_dir,
                        question=question,
                        max_scrolls=int(task["options"].get("expertAnswerMaxScrolls", 5)),
                        ocr_enabled=bool(task["options"].get("thinkingCaptureOcr", False)),
                    )
                    timing["captureThinkingContent"] = int((time.monotonic() - _t_cap) * 1000)
                    expert_answer["thinking"] = thinking_capture.get("content", "")
                    debug["thinkingCapture"] = {
                        "status": thinking_capture.get("status"),
                        "method": "completed_thinking_answer_page",
                        "expansion": thinking_capture.get("expansion"),
                        "contentLength": len(thinking_capture.get("content", "")),
                        "rawContentLength": len(thinking_capture.get("rawContent", "")),
                        "snapshots": thinking_capture.get("snapshots", []),
                    }
        except Exception as exc:
            debug["notes"].append(f"thinking_capture_failed:{exc}")
            debug["thinkingCaptureError"] = str(exc)
        finally:
            if detail_open:
                _t_back = time.monotonic()
                debug["returnToChat"] = return_to_chat_page(adb, output_dir)
                timing["returnToChat"] = int((time.monotonic() - _t_back) * 1000)
        timing["expertAnswerBlock"] = int((time.monotonic() - _ts_expert) * 1000)

        debug["sourceExtraction"] = {"visibleSourceCount": 0, "attemptedCount": 0, "note": "handled_by_js_extractor"}

        share_result = {"status": "skipped", "url": "", "error": "answer_not_found"}
        if answer:
            _ts_share = time.monotonic()
            try:
                share_result = extract_answer_share_link(adb, output_dir)
            except Exception as exc:
                debug["notes"].append(f"answer_share_failed:{exc}")
                debug["answerShareError"] = str(exc)
            timing["answerShare"] = int((time.monotonic() - _ts_share) * 1000)
        debug["answerShare"] = {key: value for key, value in share_result.items() if key != "clipboardText"}

        # —— JS 来源提取器集成 ——
        # 手机端完成 share_link 捕获后，将 share_link + linked_natural_question
        # 传递给 qianwen-source-extractor JS 脚本，由其访问分享页、提取来源、写回飞书。
        sources: list[dict] = []
        source_extraction = {"status": "skipped", "reason": "not_enabled"}
        share_url = share_result.get("url", "")
        extractor_enabled = bool(source_extractor_context and source_extractor_context.get("enabled"))
        if extractor_enabled and share_url:
            meta = (session_meta or {}) if session_meta else {}
            natural_question = meta.get("linkedNaturalQuestion") or meta.get("naturalQuestion") or question
            base_token = source_extractor_context.get("baseToken", "")
            table_id = source_extractor_context.get("tableId", "")
            extractor_options = source_extractor_context.get("options") or ExtractorOptions()
            if run_logger:
                run_logger.info("[%s#%d] invoking JS source extractor: url=%s nq=%s", session_name, index, share_url[:60], natural_question[:40])
            _ts_ext = time.monotonic()
            try:
                extraction_result = run_source_extractor(
                    share_url=share_url,
                    natural_question=natural_question,
                    base_token=base_token,
                    table_id=table_id,
                    output_dir=output_dir,
                    options=extractor_options,
                )
                timing["jsSourceExtraction"] = int((time.monotonic() - _ts_ext) * 1000)
                source_extraction = {
                    "status": extraction_result.get("status", "failed"),
                    "ok": bool(extraction_result.get("ok")),
                    "sourceCount": int(extraction_result.get("sourceCount", 0)),
                    "extractOk": extraction_result.get("extractOk"),
                    "feishuWriteOk": extraction_result.get("feishuWriteOk"),
                    "attempts": extraction_result.get("attempts", []),
                    "outputFile": extraction_result.get("outputFile", ""),
                    "error": extraction_result.get("error"),
                }
                # 将提取到的来源摘要回填到 result.sources（仅用于结果记录，飞书写回由 JS 完成）
                extracted = extraction_result.get("extractedSources") or {}
                for src in extracted.get("sources", []):
                    sources.append({
                        "index": src.get("index"),
                        "title": src.get("title", ""),
                        "url": src.get("url", ""),
                        "platform": src.get("platform", ""),
                        "method": "js_extractor",
                        "status": "success" if src.get("url") else "failed",
                    })
                if run_logger:
                    run_logger.info("[%s#%d] JS extractor done: status=%s sources=%d", session_name, index, source_extraction["status"], source_extraction["sourceCount"])
            except Exception as exc:
                timing["jsSourceExtraction"] = int((time.monotonic() - _ts_ext) * 1000)
                source_extraction = {"status": "failed", "ok": False, "error": str(exc), "sourceCount": 0}
                debug["notes"].append(f"js_source_extractor_failed:{exc}")
                if run_logger:
                    run_logger.error("[%s#%d] JS extractor failed: %s", session_name, index, exc)
        elif extractor_enabled and not share_url:
            source_extraction = {"status": "skipped", "reason": "no_share_url"}
            if run_logger:
                run_logger.warning("[%s#%d] JS extractor skipped: no share URL", session_name, index)
        debug["sourceExtraction"] = source_extraction

        # 来源条件：JS 提取成功即视为满足；未启用时恒满足（保持原行为）
        all_sources_ok = True if not extractor_enabled else (source_extraction.get("status") == "success")
        share_ok = share_result.get("status") == "success" and bool(share_result.get("url"))
        expert_ok = expert_answer.get("status") == "success"
        if answer and expert_ok and all_sources_ok and share_ok:
            status = "success"
        elif answer:
            status = "partial"
        else:
            status = "failed"

        timing["total"] = int((time.monotonic() - _t0) * 1000)
        if run_logger:
            run_logger.info("[%s#%d] done: status=%s answer_len=%d sources=%d share_ok=%s", session_name, index, status, len(answer), len(sources), share_ok)
        return {
            "index": index,
            "question": question,
            "askedAt": asked_at,
            "finishedAt": now_iso(),
            "answer": answer,
            "thinkingContent": expert_answer["thinking"],
            "sources": sources,
            "answerShareUrl": share_result.get("url", ""),
            "sourceExtraction": source_extraction,
            "status": status,
            "error": None if answer else "answer_not_found",
            "debug": {**debug, "timing": timing},
        }
    except Exception as exc:
        timing["total"] = int((time.monotonic() - _t0) * 1000)
        return {
            "index": index,
            "question": question,
            "askedAt": asked_at,
            "finishedAt": now_iso(),
            "answer": "",
            "sources": [],
            "status": "failed",
            "error": str(exc),
            "debug": {**debug, "timing": timing},
        }


def run_task(task: dict, writeback_context: dict | None = None, source_extractor_context: dict | None = None) -> str:
    """Execute all sessions in a task and persist the aggregate result."""
    device = task["device"]
    adb = AdbClient(device.get("adb"), device.get("serial"))
    adb.resolve_serial()
    run_logger = _setup_run_logger(task["output"])
    run_logger.info("=== Task started: %s ===", task.get("taskName", "qianwen-mobile-run"))
    if source_extractor_context and source_extractor_context.get("enabled"):
        run_logger.info("JS source extractor enabled: base=%s table=%s", source_extractor_context.get("baseToken"), source_extractor_context.get("tableId"))
    aggregate = {
        "taskName": task.get("taskName", "qianwen-mobile-run"),
        "mode": task.get("mode", "separate"),
        "startedAt": now_iso(),
        "sessions": [],
    }
    output = task["output"]
    writebacks = []
    _ts = time.monotonic()
    ensure_result = ensure_app(adb)
    task_ensure_ms = int((time.monotonic() - _ts) * 1000)
    for session_index, session in enumerate(task["sessions"], start=1):
        session_out = {"sessionName": session["sessionName"], "newChat": session["newChat"], "thinking": session["thinking"], "meta": session.get("meta", {}), "results": []}
        for question_index, question in enumerate(session["questions"], start=1):
            result = run_question(
                adb,
                task,
                session["sessionName"],
                question,
                question_index,
                ensure_result=ensure_result if session_index == 1 and question_index == 1 else None,
                source_extractor_context=source_extractor_context,
                session_meta=session.get("meta", {}),
                run_logger=run_logger,
            )
            result["debug"]["sessionIndex"] = session_index
            result["debug"].setdefault("timing", {})["taskEnsureApp"] = task_ensure_ms if session_index == 1 and question_index == 1 else 0
            if writeback_context and writeback_context.get("enabled"):
                try:
                    result["writeback"] = write_feishu_result(writeback_context, session, result)
                except Exception as exc:
                    result["writeback"] = failed_writeback_result(exc, session, result)
                    result["debug"].setdefault("notes", []).append("feishu_writeback_failed")
                writebacks.append(result["writeback"])
            session_out["results"].append(result)
            partial = {**aggregate, "sessions": [*aggregate["sessions"], session_out], "finishedAt": now_iso()}
            write_result(output, partial)
            interval_ms = int(task["options"].get("intervalMs", 0))
            if interval_ms:
                time.sleep(interval_ms / 1000)
        aggregate["sessions"].append(session_out)
    if writeback_context and writeback_context.get("enabled"):
        aggregate["writeback"] = {
            "finishedAt": now_iso(),
            "mode": "per-result",
            "answerTableId": writeback_context.get("answerTableId"),
            "sourceTableId": writeback_context.get("sourceTableId"),
            "inputTableId": writeback_context.get("base", {}).get("tableId"),
            "markCollected": bool(writeback_context.get("markCollected")),
            "answerCount": sum(item.get("answerCount", 0) for item in writebacks),
            "sourceCount": sum(item.get("sourceCount", 0) for item in writebacks),
            "results": writebacks,
        }
    write_result(output, aggregate, finished=True)
    run_logger.info("=== Task finished: %s ===", task.get("taskName", "qianwen-mobile-run"))
    return output


def validate_args(args: argparse.Namespace) -> None:
    """Validate the CLI arguments before starting a run."""
    has_base = bool(args.base_url or args.base_token or args.table_id)
    if args.task and has_base:
        raise ValueError("Use either --task or Feishu Base flags, not both.")
    if not args.task and not has_base:
        raise ValueError("Provide --task or Feishu Base flags.")
    if args.base_start is not None and args.base_start < 1:
        raise ValueError("--base-start must be an integer >= 1.")
    if args.base_end is not None and args.base_end < 1:
        raise ValueError("--base-end must be an integer >= 1.")
    if args.base_start is not None and args.base_end is not None and args.base_end < args.base_start:
        raise ValueError("--base-end must be greater than or equal to --base-start.")
    if not isinstance(args.base_limit, int) or args.base_limit < 1 or args.base_limit > 350:
        raise ValueError("--base-limit must be an integer from 1 to 350.")


def force_quick_mode(task: dict) -> None:
    """Force every session and question in a task into quick mode."""
    task["thinking"] = False
    for session in task.get("sessions", []):
        session["thinking"] = False
        if isinstance(session.get("meta"), dict):
            session["meta"]["thinking"] = False
        for question in session.get("questions", []):
            if isinstance(question, dict):
                question["thinking"] = False


def main() -> None:
    """CLI entry point for the Qianwen runner."""
    args = parse_args()
    validate_args(args)
    loaded = {"task": load_task(args.task), "taskPath": str(Path(args.task).resolve()), "source": "task-json"} if args.task else build_task_from_feishu(args)
    task = normalize_task(loaded["task"]) if not args.task else loaded["task"]
    if args.force_quick:
        force_quick_mode(task)
    if args.adb:
        task["device"]["adb"] = args.adb
    if args.serial:
        task["device"]["serial"] = args.serial
    if args.output:
        task["output"] = args.output
    if args.collect_account:
        task.setdefault("options", {})["collectAccount"] = args.collect_account
    if args.source_limit:
        task.setdefault("options", {})["sourceLimit"] = args.source_limit
    if args.debug:
        task.setdefault("options", {}).setdefault("debug", {})["enabled"] = True
    if args.extract_sources:
        task.setdefault("options", {}).setdefault("sourceExtractor", {})["enabled"] = True
        if args.cdp_url:
            task["options"]["sourceExtractor"]["cdpUrl"] = args.cdp_url
        if args.extractor_script:
            task["options"]["sourceExtractor"]["scriptPath"] = args.extractor_script
        task["options"]["sourceExtractor"]["timeoutSeconds"] = args.extractor_timeout
        task["options"]["sourceExtractor"]["maxRetries"] = args.extractor_retries
    if args.dry_run:
        print(json.dumps(
            {
                "dryRun": True,
                "taskPath": loaded.get("taskPath"),
                "source": loaded.get("source"),
                "base": loaded.get("base"),
                "summary": summarize_task(task),
                "generatedTask": loaded.get("task") if loaded.get("source") == "feishu-base" else None,
                "skipped": loaded.get("skipped"),
                "plannedWriteback": planned_writeback(task, args.writeback, args.mark_collected) if loaded.get("source") == "feishu-base" else None,
                "sourceExtractor": task.get("options", {}).get("sourceExtractor"),
            },
            ensure_ascii=False,
            indent=2,
        ))
        return
    if not task["sessions"]:
        raise ValueError("No Feishu rows selected. Check 是否本次采集.")
    writeback_context = None
    if loaded.get("source") == "feishu-base":
        writeback_context = {
            "enabled": args.writeback,
            "base": loaded["base"],
            "markCollected": args.mark_collected,
            "collectAccount": args.collect_account or task.get("options", {}).get("collectAccount"),
            "larkCli": args.lark_cli,
            "dryRun": args.dry_run,
            "answerTableId": planned_writeback(task, True)["answerTableId"],
            "sourceTableId": planned_writeback(task, True)["sourceTableId"],
        }
    # —— 构建 JS 来源提取器上下文 ——
    # 触发条件：CLI --extract-sources 或 task.options.sourceExtractor.enabled
    task_extractor_cfg = task.get("options", {}).get("sourceExtractor") or {}
    extractor_enabled = args.extract_sources or bool(task_extractor_cfg.get("enabled"))
    source_extractor_context = None
    if extractor_enabled:
        from .feishu_base import FEISHU_SOURCE_TABLE_ID
        base_token = args.source_base_token or (loaded.get("base") or {}).get("baseToken", "")
        table_id = args.source_table_id or FEISHU_SOURCE_TABLE_ID
        # CLI 参数优先，其次 task JSON 中的配置，最后默认值
        extractor_options = ExtractorOptions(
            script_path=args.extractor_script or task_extractor_cfg.get("scriptPath", ""),
            cdp_url=args.cdp_url or task_extractor_cfg.get("cdpUrl", "http://127.0.0.1:9222"),
            timeout_seconds=args.extractor_timeout or int(task_extractor_cfg.get("timeoutSeconds", 120)),
            max_retries=args.extractor_retries or int(task_extractor_cfg.get("maxRetries", 2)),
            retry_backoff_base=float(task_extractor_cfg.get("retryBackoffBase", 2.0)),
        )
        source_extractor_context = {
            "enabled": True,
            "baseToken": base_token,
            "tableId": table_id,
            "options": extractor_options,
        }
    output = run_task(task, writeback_context, source_extractor_context)
    print(json.dumps({"status": "finished", "output": output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
