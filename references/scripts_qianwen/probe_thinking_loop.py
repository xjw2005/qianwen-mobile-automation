import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.app import (
    center,
    click_view_all,
    dump_nodes,
    extract_page_texts,
    find_input_nodes,
    find_view_all_button,
    swipe_up,
)
from mobile_auto_qianwen.constants import DEFAULT_ADB
from mobile_auto_qianwen.ocr import compact_text, ocr_screenshot
from mobile_auto_qianwen.ui_xml import collect_nodes, save_state, visible_texts


THINKING_TRIGGER_PATTERNS = (
    "已完成思考",
    "完成思考",
    "参考了",
    "参考",
    "展开全部",
)
STOP_TEXTS = {
    "夸克",
    "发消息...",
    "发消息或按住说话...",
    "内容由 Qwen-3.7 大模型生成",
    "千问高考",
    "美加墨",
    "新",
}
SOURCE_TITLE_SUFFIXES = (
    "新闻网",
    "39健康网",
    "母婴",
    "中国网",
    "中国报业网",
    "千龙网·中国首都网",
    "新京报",
)
THINKING_SECTION_TITLE_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]{2,18}$")


def looks_like_serial(value: str | None) -> bool:
    """Check whether a value looks like an adb serial instead of a file path."""
    if not value:
        return False
    if Path(value).exists():
        return False
    return ":" in value or value.startswith("emulator-")


def resolve_adb_and_serial(adb_arg: str | None, serial_arg: str | None) -> tuple[str, str | None]:
    """Resolve the adb binary and serial from CLI arguments."""
    adb = adb_arg or DEFAULT_ADB
    serial = serial_arg
    if looks_like_serial(adb) and not serial:
        serial = adb
        adb = DEFAULT_ADB
    if adb and Path(adb).exists():
        return adb, serial
    found = shutil.which(adb or "adb") or shutil.which("adb")
    return found or adb or "adb", serial


def ensure_serial_connected(adb_path: str, serial: str | None) -> None:
    """Connect to a network adb serial when needed."""
    if not serial or ":" not in serial:
        return
    result = subprocess.run([adb_path, "devices"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if serial in result.stdout:
        return
    connect = subprocess.run([adb_path, "connect", serial], capture_output=True, text=True, encoding="utf-8", errors="replace")
    message = (connect.stdout or connect.stderr or "").strip()
    if message and "connected" not in message.lower() and "already connected" not in message.lower():
        print(f"adb connect {serial} failed: {message}", file=sys.stderr)


def node_text(node: dict) -> str:
    """Return the combined visible text for a node."""
    return f"{node.get('text', '')}{node.get('content_desc', '')}".strip()


def containing_clickable_node(nodes: list[dict], child: dict) -> dict | None:
    """Find the smallest clickable ancestor that contains the child."""
    child_bounds = child.get("parsedBounds")
    if not child_bounds:
        return None
    containing = []
    for node in nodes:
        bounds = node.get("parsedBounds")
        if not bounds or node.get("clickable") != "true":
            continue
        if bounds["left"] <= child_bounds["centerX"] <= bounds["right"] and bounds["top"] <= child_bounds["centerY"] <= bounds["bottom"]:
            area = (bounds["right"] - bounds["left"]) * (bounds["bottom"] - bounds["top"])
            containing.append((area, node))
    if not containing:
        return None
    return sorted(containing, key=lambda item: item[0])[0][1]


def is_visible_bounds(node: dict) -> bool:
    """Check whether a node is within the visible screen bounds."""
    bounds = node.get("parsedBounds")
    if not bounds:
        return False
    if bounds["right"] <= bounds["left"] or bounds["bottom"] <= bounds["top"]:
        return False
    return 240 <= bounds["centerY"] <= 2260


def trigger_score(node: dict) -> int:
    """Score how likely a node is to expand completed thinking."""
    text = compact_text(node_text(node))
    score = 0
    if "已完成思考" in text and "参考" in text:
        score += 100
    elif "已完成思考" in text or "完成思考" in text:
        score += 80
    if "展开全部" in text:
        score += 70
    if "参考" in text and re.search(r"\d+篇", text):
        score += 60
    if node.get("clickable") == "true":
        score += 10
    bounds = node.get("parsedBounds") or {}
    center_y = bounds.get("centerY", 0)
    if center_y > 1500:
        score += 5
    return score


def find_thinking_trigger_by_ui(nodes: list[dict]) -> dict | None:
    """Find the thinking trigger using the XML tree."""
    candidates = []
    for node in nodes:
        text = compact_text(node_text(node))
        if not text or not is_visible_bounds(node):
            continue
        if not any(pattern in text for pattern in THINKING_TRIGGER_PATTERNS):
            continue
        if text in {"参考", "思考"}:
            continue
        target = containing_clickable_node(nodes, node) or node
        candidates.append((trigger_score(node), node, target))
    if not candidates:
        return None
    _, source, target = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    return {
        "source": "ui_tree",
        "text": node_text(source),
        "bounds": source.get("bounds", ""),
        "target": target,
    }


def tap_thinking_trigger_by_ocr(adb: AdbClient, output_dir: Path) -> dict:
    """Find and tap the trigger using OCR when XML is insufficient."""
    ocr = ocr_screenshot(adb, output_dir, "trigger-ocr")
    for line in ocr.get("lines", []):
        text = compact_text(line.get("text", ""))
        if "已完成思考" in text or "展开全部" in text or ("参考" in text and re.search(r"\d+篇", text)):
            x = int(line.get("centerX") or 540)
            y = int(line.get("centerY") or 1060)
            adb.tap(x, y)
            time.sleep(0.8)
            return {
                "ok": True,
                "source": "ocr_line",
                "tap": {"x": x, "y": y, "text": line.get("text", "")},
                "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")},
            }
    return {"ok": False, "source": "ocr_line", "error": "thinking_trigger_not_found_by_ocr", "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")}}


def tap_thinking_trigger(adb: AdbClient, output_dir: Path) -> dict:
    """Tap the best available thinking trigger from UI or OCR."""
    nodes = dump_nodes(adb)
    view_all = find_view_all_button(nodes)
    if view_all and is_visible_bounds(view_all):
        result = click_view_all(adb, str(output_dir))
        result["source"] = "view_all"
        return result

    trigger = find_thinking_trigger_by_ui(nodes)
    if trigger:
        target = trigger["target"]
        x, y = center(target)
        adb.tap(x, y)
        time.sleep(0.8)
        return {
            "ok": True,
            "source": trigger["source"],
            "tap": {"x": x, "y": y, "text": trigger["text"], "bounds": trigger["bounds"], "targetBounds": target.get("bounds", "")},
        }

    return tap_thinking_trigger_by_ocr(adb, output_dir)


def has_expanded_thinking(nodes: list[dict]) -> bool:
    """Check whether the thinking content is already expanded."""
    texts = [compact_text(text) for text in visible_texts(nodes)]
    joined = "\n".join(texts)
    if "搜索" in joined and "参考" in joined:
        return True
    if any("展开全部" in text for text in texts):
        return True
    if any("检索" in text and len(text) > 6 for text in texts) and any("已完成思考" in text for text in texts):
        return True
    return False


def ensure_thinking_expanded(adb: AdbClient, output_dir: Path, initial_nodes: list[dict]) -> tuple[dict, dict]:
    """Ensure the thinking content is expanded and return the latest state."""
    if has_expanded_thinking(initial_nodes):
        state = save_state(adb, output_dir, "already-expanded")
        return {"ok": True, "source": "already_expanded", "skipped": True}, state

    first_tap = tap_thinking_trigger(adb, output_dir)
    state = save_state(adb, output_dir, "after-trigger-tap")
    if has_expanded_thinking(state["nodes"]):
        return first_tap, state

    second_tap = tap_thinking_trigger(adb, output_dir)
    state = save_state(adb, output_dir, "after-trigger-retap")
    return {"ok": has_expanded_thinking(state["nodes"]), "source": "retap_if_collapsed", "firstTap": first_tap, "secondTap": second_tap}, state


def dedupe_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate strings while preserving their first-seen order."""
    seen = set()
    result = []
    for item in items:
        item = normalize_text(item)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_text(text: str) -> str:
    """Normalize whitespace in extracted text."""
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_answer_or_chrome_text(text: str, question: str | None = None) -> bool:
    """Check whether text is likely chrome or answer boilerplate."""
    stripped = text.strip()
    if not stripped or stripped in STOP_TEXTS:
        return True
    if question and stripped == question.strip():
        return True
    if stripped.startswith("为肠胃敏感") or stripped.startswith("基于这两项标准"):
        return True
    if stripped.startswith("1. 候选奶粉清单") or stripped.startswith("2. 其他选择"):
        return True
    return False


def is_source_or_search_artifact(text: str) -> bool:
    """Check whether text is an artifact from sources or search UI."""
    compact = compact_text(text)
    if not compact:
        return True
    if compact in {"已完成思考", "展开全部"}:
        return True
    if re.fullmatch(r"搜索\d+个关键词，参考\d+篇资料", compact):
        return True
    if re.fullmatch(r"参考\d+篇资料", compact):
        return True
    if len(text) <= 24 and " " in text and not any(punc in text for punc in "，。；："):
        return True
    if any(text.endswith(suffix) for suffix in SOURCE_TITLE_SUFFIXES):
        return True
    if any(marker in text for marker in (" — ", "--", "_")):
        return True
    if re.search(r"20\d{2}年\d{1,2}月", text):
        return True
    return False


def is_thinking_section_title(text: str) -> bool:
    """Check whether text looks like a short thinking-section title."""
    compact = compact_text(text)
    if not compact:
        return False
    if any(keyword in compact for keyword in ("检索", "比对", "筛选", "整合", "分析", "确认")):
        return bool(THINKING_SECTION_TITLE_RE.fullmatch(compact))
    return False


def is_reasoning_paragraph(text: str) -> bool:
    """Check whether text looks like a reasoning paragraph."""
    compact = compact_text(text)
    if len(compact) < 35:
        return False
    if is_source_or_search_artifact(text):
        return False
    reasoning_markers = (
        "需求",
        "需",
        "因为",
        "因",
        "旨在",
        "转为",
        "结合",
        "确保",
        "明确",
        "获取",
        "评估",
        "筛选",
        "整合",
        "判断",
        "标注",
        "基础",
        "信息",
    )
    return any(marker in compact for marker in reasoning_markers)


def clean_thinking_texts(texts: list[str]) -> list[str]:
    """Clean and merge thinking text fragments."""
    cleaned = []
    pending_title = ""
    for raw in texts:
        text = normalize_text(raw)
        if not text:
            continue
        if is_thinking_section_title(text):
            pending_title = text
            continue
        if is_reasoning_paragraph(text):
            if pending_title:
                cleaned.append(pending_title)
                pending_title = ""
            cleaned.append(text)
        elif is_source_or_search_artifact(text):
            continue
        else:
            pending_title = ""
    return dedupe_preserve_order(cleaned)


def extract_thinking_texts_from_ui_texts(texts: list[str], question: str | None = None) -> list[str]:
    """Extract ordered thinking text fragments from UI text lines."""
    meaningful = [normalize_text(text) for text in texts if normalize_text(text) and normalize_text(text) not in STOP_TEXTS]
    has_thinking_mark = any("已完成思考" in compact_text(text) or ("搜索" in compact_text(text) and "参考" in compact_text(text)) for text in meaningful)
    start_from_first_meaningful = bool(has_thinking_mark and meaningful and (not question or meaningful[0] != question.strip()))

    filtered = []
    started = start_from_first_meaningful
    for raw in meaningful:
        text = normalize_text(raw)
        compact = compact_text(text)
        if not text:
            continue
        if text == "思考":
            started = True
            continue
        if not started and ("已完成思考" in compact or "搜索" in compact and "参考" in compact):
            started = True
        if not started:
            continue
        if "已完成思考" in compact and "参考" in compact:
            break
        if question and text == question.strip():
            break
        if is_answer_or_chrome_text(text, question):
            continue
        if text == "展开全部":
            continue
        filtered.append(text)
        if text == "已完成思考":
            break
    return dedupe_preserve_order(filtered)


def capture_thinking_content(adb: AdbClient, output_dir: Path, max_scrolls: int, ocr_enabled: bool, question: str | None) -> dict:
    """Capture thinking content while scrolling and optionally applying OCR."""
    fragments = []
    snapshots = []
    seen = set()
    unchanged = 0

    for index in range(max_scrolls + 1):
        label = f"loop-capture-{index:02d}"
        xml = adb.dump_xml()
        xml_path = output_dir / f"{label}.xml"
        xml_path.write_text(xml, encoding="utf-8")
        nodes = collect_nodes(xml)
        ui_texts = extract_page_texts(xml)
        thinking_texts = extract_thinking_texts_from_ui_texts(ui_texts, question=question)

        ocr = {"ok": False, "text": "", "lines": [], "screenshot": "", "error": "ocr_disabled"}
        if ocr_enabled:
            ocr = ocr_screenshot(adb, output_dir, label)
            if not thinking_texts and ocr.get("text"):
                thinking_texts = extract_thinking_texts_from_ui_texts(ocr.get("text", "").splitlines(), question=question)

        new_items = [item for item in thinking_texts if item not in seen]
        if new_items:
            unchanged = 0
            seen.update(new_items)
        else:
            unchanged += 1
        fragments.extend(new_items)

        snapshots.append(
            {
                "index": index,
                "xml": str(xml_path),
                "screenshot": ocr.get("screenshot", ""),
                "uiTextCount": len(ui_texts),
                "thinkingTextCount": len(thinking_texts),
                "newTextCount": len(new_items),
                "ocrOk": ocr.get("ok", False),
                "ocrLineCount": len(ocr.get("lines", [])),
                "visibleTexts": visible_texts(nodes)[:40],
                "thinkingTexts": thinking_texts,
            }
        )

        if unchanged >= 2:
            break
        if index < max_scrolls:
            swipe_up(adb)

    raw_items = dedupe_preserve_order(fragments)
    clean_items = clean_thinking_texts(raw_items)
    raw_content = "\n\n".join(raw_items)
    content = "\n\n".join(clean_items)
    return {
        "status": "success" if content else "failed",
        "content": content,
        "contentLength": len(content),
        "cleanThinkingTexts": clean_items,
        "rawThinkingContent": raw_content,
        "rawThinkingLength": len(raw_content),
        "rawThinkingTexts": raw_items,
        "snapshots": snapshots,
    }


def infer_question(nodes: list[dict]) -> str:
    """Infer the current question from visible nodes."""
    candidates = []
    for text in visible_texts(nodes):
        stripped = text.strip()
        if len(stripped) < 8:
            continue
        if any(skip in stripped for skip in ("已完成思考", "参考了", "候选奶粉清单", "搜索")):
            continue
        if stripped.endswith(("？", "?", "。")) and len(stripped) < 120:
            candidates.append(stripped)
    return candidates[-1] if candidates else ""


def main() -> None:
    """CLI entry point for the thinking-loop probe."""
    parser = argparse.ArgumentParser(description="Click Qianwen completed-thinking entry and capture thinking content.")
    parser.add_argument("--adb", default=DEFAULT_ADB, help="Path to adb.exe. If a device serial is passed here, it is treated as --serial.")
    parser.add_argument("--serial", default=None, help="ADB device serial, for example 127.0.0.1:16416.")
    parser.add_argument("--output-dir", default="outputs/qianwen/thinking-loop-probe")
    parser.add_argument("--max-scrolls", type=int, default=4)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--question", default="", help="Optional question text used as the stop boundary.")
    args = parser.parse_args()

    adb_path, serial = resolve_adb_and_serial(args.adb, args.serial)
    ensure_serial_connected(adb_path, serial)
    adb = AdbClient(adb=adb_path, serial=serial)
    adb.resolve_serial()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    initial = save_state(adb, output_dir, "initial")
    question = args.question.strip() or infer_question(initial["nodes"])
    print("=== 千问思考闭环 Probe ===")
    print(f"设备: {adb.serial}")
    print(f"推断问题: {question or '(未识别)'}")
    print(f"初始输入框: {len(find_input_nodes(initial['nodes']))}")

    tap_result, after_tap = ensure_thinking_expanded(adb, output_dir, initial["nodes"])
    print(f"展开思考入口: {tap_result}")

    capture = capture_thinking_content(
        adb=adb,
        output_dir=output_dir,
        max_scrolls=max(0, args.max_scrolls),
        ocr_enabled=not args.no_ocr,
        question=question,
    )

    report = {
        "device": {"adb": adb_path, "serial": adb.serial},
        "question": question,
        "tap": tap_result,
        "afterTapVisibleTexts": visible_texts(after_tap["nodes"])[:60],
        "capture": capture,
    }
    report_path = output_dir / "thinking_loop_probe_report.json"
    content_path = output_dir / "thinking_loop_content.txt"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    content_path.write_text(capture.get("content", ""), encoding="utf-8")

    print(f"采集状态: {capture['status']}")
    print(f"内容长度: {capture['contentLength']}")
    print(f"采集屏数: {len(capture['snapshots'])}")
    for snapshot in capture["snapshots"]:
        print(
            f"  - #{snapshot['index']}: UI文本={snapshot['uiTextCount']}, "
            f"思考文本={snapshot['thinkingTextCount']}, 新文本={snapshot['newTextCount']}, OCR={snapshot['ocrOk']}"
        )
    print(f"内容文件: {content_path}")
    print(f"报告文件: {report_path}")


if __name__ == "__main__":
    main()
