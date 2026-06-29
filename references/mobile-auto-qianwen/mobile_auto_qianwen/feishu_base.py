import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .time_utils import now_iso, stamp


FEISHU_ANSWER_TABLE_ID = "tblaV1deA4L9hzze"
FEISHU_SOURCE_TABLE_ID = "tblF1LsniY1BnOt3"
DEFAULT_PLATFORM = "千问移动端"
DEFAULT_COLLECT_ACCOUNT = "18870501682"
QUESTION_ID_FIELD = "问题ID"
LEGACY_QUESTION_ID_FIELD = "关联自然问句"
INPUT_QUESTION_ID_FIELDS = (QUESTION_ID_FIELD, LEGACY_QUESTION_ID_FIELD)
QUESTION_TEXT_FIELDS = ("问题文本", "问题")
THINKING_FIELD = "是否开启深度思考"
COLLECT_NOW_FIELD = "是否本次采集"
AI_PLATFORM_FIELD = "AI平台"
ANSWER_WRITEBACK_FIELDS = ["采集账号", "问题文本", QUESTION_ID_FIELD, "是否开启深度思考", "AI回答", "深度思考", "是否触发联网", "对话链接", AI_PLATFORM_FIELD]
SOURCE_WRITEBACK_FIELDS = ["来源标题", "来源URL", "引用来源类型", "引用来源平台", QUESTION_ID_FIELD]
ANSWER_PRELUDE_PREFIXES = ("检索", "对比", "调研", "查询", "分析", "收集", "整合")
ANSWER_START_MARKERS = (
    "适度水解奶粉主要",
    "肠胃敏感宝宝",
    "给肠胃",
    "综合",
)


class FeishuError(RuntimeError):
    """Raised when Feishu CLI access or task assembly fails."""


def clean_text(value) -> str:
    """Normalize whitespace and repeated blank lines."""
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", str(value or "").replace("\u00a0", " "))).strip()


def normalize_ai_platform(value) -> str:
    """Map legacy platform labels to Feishu select options."""
    text = clean_text(value)
    aliases = {
        "豆包": "豆包移动端",
        "doubao": "豆包移动端",
        "千问": "千问移动端",
        "qianwen": "千问移动端",
        "DeepSeek": "DeepSeek移动端",
        "deepseek": "DeepSeek移动端",
    }
    return aliases.get(text, text or DEFAULT_PLATFORM)


def clean_thinking_for_writeback(value) -> str:
    """Trim boilerplate from captured thinking content before writeback."""
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\d+\s*[：:]\s*\d+\s*深度思考\s*", "", text)
    first_real = text.find("这是关于")
    if 0 < first_real < 80:
        text = text[first_real:]
    return clean_text(text)


def clean_answer_for_writeback(value) -> str:
    """Trim answer preludes and normalize the final answer text."""
    text = clean_text(value)
    if not text:
        return ""
    if text.startswith(ANSWER_PRELUDE_PREFIXES):
        starts = [index for marker in ANSWER_START_MARKERS if (index := text.find(marker, 10)) > 0]
        if starts:
            text = text[min(starts):]
    return clean_text(text)


def is_yes(value) -> bool:
    """Interpret common yes-like values as booleans."""
    return value is True or value == "是" or (isinstance(value, list) and "是" in value)


def first_row_value(row_map: dict, names: tuple[str, ...], default=None):
    """Return the first non-empty value from a Feishu row map."""
    for name in names:
        if name in row_map:
            value = row_map.get(name)
            if value not in (None, ""):
                return value
    return default


def parse_base_location(args) -> dict:
    """Read Feishu Base location parameters from CLI arguments."""
    base_token = getattr(args, "base_token", None)
    table_id = getattr(args, "table_id", None)
    view_id = getattr(args, "view_id", None)
    base_url = getattr(args, "base_url", None)
    if base_url:
        parsed = urlparse(base_url)
        match = re.search(r"/base/([^/?#]+)", parsed.path)
        if not match:
            raise FeishuError("Feishu Base URL must contain /base/{baseToken}.")
        query = parse_qs(parsed.query)
        base_token = base_token or match.group(1)
        table_id = table_id or (query.get("table") or [None])[0]
        view_id = view_id or (query.get("view") or [None])[0]
    if not base_token or not table_id:
        raise FeishuError("Feishu Base mode requires --base-url or both --base-token and --table-id.")
    return {"baseToken": base_token, "tableId": table_id, "viewId": view_id}


def run_json_command(command: str, args: list[str]) -> dict:
    """Run a Feishu CLI command and parse its JSON response."""
    try:
        proc = subprocess.run([command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise FeishuError(f"{command} not found. Install lark-cli or pass --lark-cli with the executable path.") from exc
    output = (proc.stdout or proc.stderr or "").strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise FeishuError(f"{command} returned non-JSON output: {output or exc}") from exc
    if proc.returncode != 0 or not payload.get("ok"):
        raise FeishuError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def run_json_command_with_payload(command: str, args: list[str], payload: dict) -> dict:
    """Run a JSON command with its payload stored in a temp file."""
    temp_path = None
    try:
        payload_dir = Path(".lark-payloads")
        payload_dir.mkdir(exist_ok=True)
        temp_path = payload_dir / f"qianwen-{stamp()}.json"
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return run_json_command(command, [*args, "--json", f"@{temp_path.as_posix()}", "--format", "json"])
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)


def parse_base_row_range(args) -> tuple[int, int]:
    """Compute the inclusive Feishu row range to read."""
    base_start = getattr(args, "base_start", None)
    base_end = getattr(args, "base_end", None)
    base_limit = getattr(args, "base_limit", None)

    if base_start is None and base_end is None:
        return 1, max(1, int(base_limit or 1))

    if base_start is None:
        base_start = 1
    if base_end is None:
        base_end = base_start + max(1, int(base_limit or 1)) - 1
    if base_start < 1:
        raise FeishuError("--base-start must be >= 1.")
    if base_end < base_start:
        raise FeishuError("--base-end must be greater than or equal to --base-start.")
    return int(base_start), int(base_end)


def list_feishu_records_page(base: dict, offset: int, limit: int, lark_cli: str = "lark-cli") -> dict:
    """Fetch one page of records from a Feishu Base table."""
    args = [
        "base",
        "+record-list",
        "--base-token",
        base["baseToken"],
        "--table-id",
        base["tableId"],
    ]
    if base.get("viewId"):
        args.extend(["--view-id", base["viewId"]])
    for field_name in [*QUESTION_TEXT_FIELDS, *INPUT_QUESTION_ID_FIELDS, THINKING_FIELD, COLLECT_NOW_FIELD]:
        args.extend(["--field-id", field_name])
    args.extend([
        "--offset",
        str(offset),
        "--limit",
        str(limit),
        "--format",
        "json",
    ])
    return run_json_command(lark_cli, args).get("data", {})


def list_feishu_records(base: dict, start_row: int, end_row: int, lark_cli: str = "lark-cli") -> dict:
    """Fetch a contiguous row range from Feishu Base."""
    if end_row < start_row:
        return {"data": [], "record_id_list": []}

    remaining = end_row - start_row + 1
    offset = start_row - 1
    rows: list = []
    record_ids: list[str] = []
    fields: list[str] = []

    while remaining > 0:
        page = list_feishu_records_page(base, offset, min(remaining, 200), lark_cli)
        page_rows = page.get("data") or []
        page_record_ids = page.get("record_id_list") or []
        if not fields:
            fields = page.get("fields") or []
        if not page_rows:
            break
        rows.extend(page_rows)
        record_ids.extend(page_record_ids[: len(page_rows)])
        fetched = len(page_rows)
        remaining -= fetched
        offset += fetched
        if fetched < 200:
            break

    return {"data": rows, "record_id_list": record_ids, "fields": fields}


def build_task_from_feishu(args) -> dict:
    """Transform selected Feishu rows into a runnable task."""
    base = parse_base_location(args)
    base_start, base_end = parse_base_row_range(args)
    records = list_feishu_records(base, base_start, base_end, args.lark_cli)
    rows = records.get("data") or []
    record_ids = records.get("record_id_list") or []
    fields = records.get("fields") or []
    sessions = []
    skipped = []
    needs_thinking = False
    platform = normalize_ai_platform(args.platform or DEFAULT_PLATFORM)

    for index, row in enumerate(rows):
        row_map = dict(zip(fields, row)) if fields else {}
        if row_map:
            question = first_row_value(row_map, QUESTION_TEXT_FIELDS)
            linked_natural_question = first_row_value(row_map, INPUT_QUESTION_ID_FIELDS)
            thinking = row_map.get(THINKING_FIELD)
            collect_now = row_map.get(COLLECT_NOW_FIELD, "是")
        else:
            padded = [*row, None, None, None]
            question, linked_natural_question, thinking, collect_now = padded[:4]
            if collect_now is None:
                collect_now = "是"
        record_id = record_ids[index] if index < len(record_ids) else None
        question_text = clean_text(question)
        should_collect = is_yes(collect_now)
        if not question_text or not should_collect:
            skipped.append({
                "recordId": record_id,
                "question": question_text,
                "collectNow": collect_now,
                "reason": "empty-question" if not question_text else "not-selected",
            })
            continue
        row_thinking = False if getattr(args, "force_quick", False) else is_yes(thinking)
        if row_thinking:
            needs_thinking = True
        sessions.append({
            "sessionName": f"feishu-{record_id or index + 1}",
            "newChat": True,
            "thinking": row_thinking,
            "questions": [question_text],
            "meta": {
                "feishuRecordId": record_id,
                "baseToken": base["baseToken"],
                "tableId": base["tableId"],
                "viewId": base.get("viewId"),
                "naturalQuestion": question_text,
                "linkedNaturalQuestion": clean_text(linked_natural_question),
                "fullQuestion": question_text,
                "thinking": row_thinking,
                "platform": platform,
            },
        })

    task = {
        "taskName": "qianwen-feishu-base-run",
        "mode": "separate",
        "thinking": bool(needs_thinking),
        "sessions": sessions,
        "options": {
            "sourceLimit": 2,
            "waitStableSeconds": 1,
            "intervalMs": 0,
            "timeoutMs": 180000,
            "expertAnswerTopScrolls": 4,
            "expertAnswerMaxScrolls": 4,
            "thinkingCaptureOcr": True,
            "answerShareMaxScrolls": 8,
            "answerShareWaitSeconds": 0.3,
            "sourcePageWaitSeconds": 0.3,
            "sourceShareWaitSeconds": 0.15,
            "debug": {
                "screenshots": False,
                "currentFocus": False,
            },
        },
        "output": f"results/qianwen-feishu-base-run-{stamp()}.json",
    }
    return {"task": task, "taskPath": None, "base": {**base, "rowStart": base_start, "rowEnd": base_end, "rowRange": [base_start, base_end]}, "skipped": skipped, "source": "feishu-base"}


def create_feishu_records(base: dict, table_id: str, fields: list[str], rows: list[list], lark_cli: str = "lark-cli", dry_run: bool = False) -> dict:
    """Create one or more records in Feishu."""
    if not rows:
        return {"skipped": True, "reason": "no-rows", "tableId": table_id, "count": 0}
    args = [
        "base",
        "+record-batch-create",
        "--base-token",
        base["baseToken"],
        "--table-id",
        table_id,
    ]
    if dry_run:
        args.append("--dry-run")
    return run_json_command_with_payload(lark_cli, args, {"fields": fields, "rows": rows})


def update_feishu_task_rows(base: dict, record_ids: list[str], lark_cli: str = "lark-cli", dry_run: bool = False) -> dict:
    """Mark source task rows as no longer selected for collection."""
    if not record_ids:
        return {"skipped": True, "reason": "no-record-ids", "count": 0}
    args = [
        "base",
        "+record-batch-update",
        "--base-token",
        base["baseToken"],
        "--table-id",
        base["tableId"],
    ]
    if dry_run:
        args.append("--dry-run")
    return run_json_command_with_payload(lark_cli, args, {"record_id_list": record_ids, "patch": {"是否本次采集": "否"}})


def build_feishu_writeback_rows_for_result(source_session: dict, result: dict, collect_account: str | None = None) -> dict:
    """Build Feishu answer rows for a completed question result."""
    answer_rows = []
    source_rows = []
    if result.get("status") not in {"success", "partial"}:
        return {"answerRows": answer_rows, "sourceRows": source_rows}
    cleaned_answer = clean_answer_for_writeback(result.get("answer", ""))
    if not cleaned_answer:
        return {"answerRows": answer_rows, "sourceRows": source_rows}
    meta = source_session.get("meta") or {}
    questions = source_session.get("questions") or []
    first_question = questions[0].get("text") if questions and isinstance(questions[0], dict) else (questions[0] if questions else "")
    natural_question = clean_text(meta.get("naturalQuestion")) or clean_text(first_question)
    linked_natural_question = clean_text(meta.get("linkedNaturalQuestion"))
    platform = normalize_ai_platform(meta.get("platform") or DEFAULT_PLATFORM)
    # 是否触发联网：只要本次打开了思考面板就填“是”
    # 分支 A：点击“查看全部”进入思考详情页（debug.clickViewAll.ok）
    # 分支 B：答案页内联展开思考（debug.thinkingCapture.expansion.ok）
    debug = result.get("debug") or {}
    thinking_capture = debug.get("thinkingCapture") or {}
    expansion = thinking_capture.get("expansion") or {}
    opened_via_view_all = bool((debug.get("clickViewAll") or {}).get("ok"))
    opened_inline = bool(expansion.get("ok"))
    thinking_panel_opened = opened_via_view_all or opened_inline
    answer_rows.append([
        clean_text(collect_account or DEFAULT_COLLECT_ACCOUNT),
        natural_question,
        linked_natural_question,
        "是" if (source_session.get("thinking") or meta.get("thinking")) else "否",
        cleaned_answer,
        clean_thinking_for_writeback(result.get("thinkingContent")),
        "是" if thinking_panel_opened else "否",
        clean_text(result.get("answerShareUrl")),
        platform,
    ])
    return {"answerRows": answer_rows, "sourceRows": source_rows}


def write_feishu_result(writeback_context: dict, source_session: dict, result: dict) -> dict:
    """Write answer rows and summarize source extraction for auditing."""
    rows = build_feishu_writeback_rows_for_result(source_session, result, writeback_context.get("collectAccount"))
    base = writeback_context["base"]
    lark_cli = writeback_context.get("larkCli", "lark-cli")
    dry_run = bool(writeback_context.get("dryRun"))
    answer_table_id = writeback_context.get("answerTableId") or FEISHU_ANSWER_TABLE_ID
    source_table_id = writeback_context.get("sourceTableId") or FEISHU_SOURCE_TABLE_ID
    answer_result = create_feishu_records(base, answer_table_id, ANSWER_WRITEBACK_FIELDS, rows["answerRows"], lark_cli, dry_run)
    # 来源记录由 qianwen-source-extractor JS 脚本直接写入飞书来源表，
    # Python 端不再重复写源；这里仅记录 JS 提取结果以便审计与追踪。
    js_extraction = result.get("sourceExtraction") or {}
    js_source_count = int(js_extraction.get("sourceCount", 0))
    js_write_ok = js_extraction.get("feishuWriteOk")
    if js_extraction.get("status") == "success":
        source_result = {
            "skipped": False,
            "reason": "written_by_js_extractor",
            "tableId": source_table_id,
            "count": js_source_count,
            "jsStatus": js_extraction.get("status"),
            "jsWriteOk": js_write_ok,
            "jsAttempts": js_extraction.get("attempts"),
        }
    elif js_extraction.get("status") == "skipped":
        source_result = {
            "skipped": True,
            "reason": js_extraction.get("reason", "js_extractor_skipped"),
            "tableId": source_table_id,
            "count": 0,
            "jsStatus": js_extraction.get("status"),
        }
    else:
        source_result = {
            "skipped": True,
            "reason": "js_extractor_failed",
            "tableId": source_table_id,
            "count": 0,
            "jsStatus": js_extraction.get("status"),
            "jsError": js_extraction.get("error"),
            "jsAttempts": js_extraction.get("attempts"),
        }
    source_record_id = (source_session.get("meta") or {}).get("feishuRecordId")
    if writeback_context.get("markCollected") and rows["answerRows"] and source_record_id:
        source_update_result = update_feishu_task_rows(base, [source_record_id], lark_cli, dry_run)
    else:
        source_update_result = {"skipped": True, "reason": "no-source-record-id-or-answer-row" if writeback_context.get("markCollected") else "mark-collected-disabled"}
    return {
        "finishedAt": now_iso(),
        "answerTableId": answer_table_id,
        "sourceTableId": source_table_id,
        "inputTableId": base["tableId"],
        "sourceRecordId": source_record_id,
        "markCollected": bool(writeback_context.get("markCollected")),
        "answerCount": len(rows["answerRows"]),
        "sourceCount": js_source_count,
        "answerResult": answer_result,
        "sourceResult": source_result,
        "sourceUpdateResult": source_update_result,
    }


def planned_writeback(task: dict, enabled: bool, mark_collected: bool = False, answer_table_id: str = "", source_table_id: str = "") -> dict:
    """Describe the writeback work that would happen for this task."""
    record_ids = [
        session.get("meta", {}).get("feishuRecordId")
        for session in task.get("sessions", [])
        if session.get("meta", {}).get("feishuRecordId")
    ]
    extractor_cfg = task.get("options", {}).get("sourceExtractor") or {}
    extractor_enabled = bool(extractor_cfg.get("enabled"))
    answer_table = (answer_table_id or "").strip() or FEISHU_ANSWER_TABLE_ID
    source_table = (source_table_id or "").strip() or FEISHU_SOURCE_TABLE_ID
    return {
        "enabled": bool(enabled),
        "action": "create Qianwen answer records; source records written by JS extractor" if extractor_enabled else "create Qianwen answer records only; source writeback is disabled",
        "answerTableId": answer_table,
        "sourceTableId": source_table if extractor_enabled else None,
        "markCollected": bool(mark_collected),
        "markCollectedField": "是否本次采集",
        "markCollectedValue": "否",
        "recordIds": record_ids,
        "answerFields": ANSWER_WRITEBACK_FIELDS,
        "sourceFields": SOURCE_WRITEBACK_FIELDS if extractor_enabled else [],
        "sourceWriter": "qianwen-source-extractor (JS)" if extractor_enabled else None,
    }
