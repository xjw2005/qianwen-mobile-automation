import argparse
import json
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
    is_on_thinking_detail_page,
    swipe_up,
    tap_thinking_reference_trigger,
)
from mobile_auto_qianwen.constants import DEFAULT_ADB
from mobile_auto_qianwen.ocr import ocr_screenshot
from mobile_auto_qianwen.ui_xml import collect_nodes, save_state, visible_texts


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


def dedupe_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate strings while preserving order."""
    seen = set()
    result = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def summarize_nodes(nodes: list[dict]) -> dict:
    """Summarize the current page state for console output."""
    view_all = find_view_all_button(nodes)
    input_nodes = find_input_nodes(nodes)
    return {
        "nodeCount": len(nodes),
        "inputNodeCount": len(input_nodes),
        "hasViewAll": bool(view_all),
        "viewAll": {
            "text": view_all.get("text", ""),
            "contentDesc": view_all.get("content_desc", ""),
            "bounds": view_all.get("bounds", ""),
            "tap": {"x": center(view_all)[0], "y": center(view_all)[1]},
        }
        if view_all
        else None,
        "isThinkingDetailPage": is_on_thinking_detail_page(nodes),
        "visibleTexts": visible_texts(nodes)[:40],
    }


def capture_current_thinking(adb: AdbClient, output_dir: Path, max_scrolls: int, ocr_enabled: bool) -> dict:
    """Capture thinking content while scrolling and optionally applying OCR."""
    fragments = []
    snapshots = []
    seen_texts = set()
    unchanged = 0

    for index in range(max_scrolls + 1):
        label = f"thinking-probe-{index:02d}"
        xml = adb.dump_xml()
        (output_dir / f"{label}.xml").write_text(xml, encoding="utf-8")
        nodes = collect_nodes(xml)
        ui_texts = extract_page_texts(xml)
        ocr = {"ok": False, "text": "", "lines": [], "screenshot": "", "error": "ocr_disabled"}
        if ocr_enabled:
            ocr = ocr_screenshot(adb, output_dir, label)

        page_texts = list(ui_texts)
        if ocr_enabled and ocr.get("text"):
            page_texts.append(ocr["text"])

        new_texts = {text.strip() for text in page_texts if text.strip()} - seen_texts
        if new_texts:
            unchanged = 0
            seen_texts.update(new_texts)
        else:
            unchanged += 1

        fragments.extend(page_texts)
        snapshots.append(
            {
                "index": index,
                "xml": str(output_dir / f"{label}.xml"),
                "screenshot": ocr.get("screenshot", ""),
                "uiTextCount": len(ui_texts),
                "ocrOk": ocr.get("ok", False),
                "ocrLineCount": len(ocr.get("lines", [])),
                "ocrError": ocr.get("error", ""),
                "newTextCount": len(new_texts),
                "unchanged": unchanged,
                "uiTexts": ui_texts[:30],
                "ocrText": ocr.get("text", ""),
                "visibleTexts": visible_texts(nodes)[:30],
            }
        )

        if unchanged >= 2:
            break
        if index < max_scrolls:
            swipe_up(adb)

    content = "\n\n".join(dedupe_preserve_order(fragments))
    return {
        "status": "success" if content else "failed",
        "content": content,
        "contentLength": len(content),
        "snapshots": snapshots,
    }


def main() -> None:
    """CLI entry point for the thinking-capture probe."""
    parser = argparse.ArgumentParser(description="Probe Qianwen thinking content with UI XML and OCR.")
    parser.add_argument("--adb", default=DEFAULT_ADB, help="Path to adb.exe. If a device serial is passed here, it is treated as --serial.")
    parser.add_argument("--serial", default=None, help="ADB device serial, for example 127.0.0.1:16416.")
    parser.add_argument("--output-dir", default="outputs/qianwen/thinking-capture-probe")
    parser.add_argument("--max-scrolls", type=int, default=4)
    parser.add_argument("--no-ocr", action="store_true", help="Disable OCR and only use UI XML text.")
    parser.add_argument("--no-click-view-all", action="store_true", help="Do not click 查看全部 even if it is visible.")
    parser.add_argument("--tap-reference-trigger", action="store_true", help="After content capture, OCR-search and tap the 参考资料 trigger.")
    args = parser.parse_args()

    adb_path, serial = resolve_adb_and_serial(args.adb, args.serial)
    ensure_serial_connected(adb_path, serial)
    adb = AdbClient(adb=adb_path, serial=serial)
    adb.resolve_serial()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    initial = save_state(adb, output_dir, "initial")
    initial_summary = summarize_nodes(initial["nodes"])
    print("=== 千问思考内容识别 Probe ===")
    print(f"设备: {adb.serial}")
    print(f"初始节点: {initial_summary['nodeCount']}")
    print(f"输入框: {initial_summary['inputNodeCount']}")
    print(f"查看全部: {initial_summary['hasViewAll']}")
    print(f"疑似思考详情页: {initial_summary['isThinkingDetailPage']}")

    view_all_result = {"ok": False, "skipped": True}
    if initial_summary["hasViewAll"] and not args.no_click_view_all:
        view_all_result = click_view_all(adb, str(output_dir))
        print(f"点击查看全部: {view_all_result}")
        time.sleep(0.5)
    elif initial_summary["hasViewAll"]:
        print("跳过点击查看全部: --no-click-view-all")

    before_capture = save_state(adb, output_dir, "before-capture")
    before_summary = summarize_nodes(before_capture["nodes"])
    capture = capture_current_thinking(
        adb=adb,
        output_dir=output_dir,
        max_scrolls=max(0, args.max_scrolls),
        ocr_enabled=not args.no_ocr,
    )

    reference_trigger = None
    if args.tap_reference_trigger:
        reference_trigger = tap_thinking_reference_trigger(adb, str(output_dir))
        save_state(adb, output_dir, "after-reference-trigger")
        print(f"点击参考资料入口: {reference_trigger}")

    report = {
        "device": {"adb": adb_path, "serial": adb.serial},
        "options": {
            "maxScrolls": args.max_scrolls,
            "ocrEnabled": not args.no_ocr,
            "clickViewAll": not args.no_click_view_all,
            "tapReferenceTrigger": args.tap_reference_trigger,
        },
        "initial": initial_summary,
        "viewAllClick": view_all_result,
        "beforeCapture": before_summary,
        "capture": capture,
        "referenceTrigger": reference_trigger,
    }

    report_path = output_dir / "thinking_capture_probe_report.json"
    content_path = output_dir / "thinking_capture_content.txt"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    content_path.write_text(capture.get("content", ""), encoding="utf-8")

    print(f"采集状态: {capture['status']}")
    print(f"内容长度: {capture['contentLength']}")
    print(f"采集屏数: {len(capture['snapshots'])}")
    for item in capture["snapshots"]:
        print(
            f"  - #{item['index']}: UI文本={item['uiTextCount']}, "
            f"OCR={item['ocrOk']}, OCR行={item['ocrLineCount']}, 新文本={item['newTextCount']}"
        )
    print(f"内容文件: {content_path}")
    print(f"报告文件: {report_path}")


if __name__ == "__main__":
    main()
