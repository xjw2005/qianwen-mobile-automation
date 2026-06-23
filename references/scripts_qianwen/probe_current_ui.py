import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.app import find_input_nodes, find_think_button
from mobile_auto_qianwen.constants import DEFAULT_ADB, QIANWEN_PACKAGE, VIEW_ALL_TEXT, SHARE_BUTTON_TEXT, COPY_LINK_TEXT
from mobile_auto_qianwen.ui_xml import collect_nodes, find_nodes, visible_texts, save_state


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

    # Backward-compatible convenience: allow `--adb 127.0.0.1:16417`.
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


def analyze_page(nodes: list[dict]) -> dict:
    """Summarize the key visible controls on the current page."""
    return {
        "input_nodes": len(find_input_nodes(nodes)),
        "think_buttons": [find_think_button(nodes)] if find_think_button(nodes) else [],
        "view_all_buttons": [n for n in nodes if VIEW_ALL_TEXT in (n.get("text","")+n.get("content_desc","")) and n.get("clickable")=="true"],
        "share_buttons": [n for n in nodes if SHARE_BUTTON_TEXT in (n.get("text","")+n.get("content_desc","")) and n.get("clickable")=="true"],
        "copy_link_buttons": [n for n in nodes if COPY_LINK_TEXT in (n.get("text","")+n.get("content_desc","")) and n.get("clickable")=="true"],
        "visible_texts": visible_texts(nodes)[:20],
    }


def main():
    """CLI entry point for the current-UI probe."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", default=DEFAULT_ADB, help="Path to adb.exe. If a device serial is passed here, it is treated as --serial.")
    parser.add_argument("--serial", default=None, help="ADB device serial, for example 127.0.0.1:16417.")
    parser.add_argument("--output-dir", default="outputs/qianwen/probe")
    args = parser.parse_args()

    adb_path, serial = resolve_adb_and_serial(args.adb, args.serial)
    ensure_serial_connected(adb_path, serial)
    adb = AdbClient(adb=adb_path, serial=serial)
    adb.resolve_serial()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    state = save_state(adb, str(output_dir), "probe")
    nodes = state["nodes"]
    analysis = analyze_page(nodes)

    report_path = output_dir / "probe_report.json"
    import json
    report_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("=== 千问页面探测报告 ===")
    print(f"输入框节点: {analysis['input_nodes']}")
    print(f"思考按钮: {len(analysis['think_buttons'])}")
    for btn in analysis['think_buttons']:
        print(f"  - text={btn.get('text')}, desc={btn.get('content_desc')}, bounds={btn.get('bounds')}, clickable={btn.get('clickable')}")
    print(f"查看全部按钮: {len(analysis['view_all_buttons'])}")
    for btn in analysis['view_all_buttons']:
        print(f"  - text={btn.get('text')}, desc={btn.get('content_desc')}, bounds={btn.get('bounds')}")
    print(f"分享按钮: {len(analysis['share_buttons'])}")
    for btn in analysis['share_buttons']:
        print(f"  - text={btn.get('text')}, desc={btn.get('content_desc')}, bounds={btn.get('bounds')}")
    print(f"复制链接按钮: {len(analysis['copy_link_buttons'])}")
    for btn in analysis['copy_link_buttons']:
        print(f"  - text={btn.get('text')}, desc={btn.get('content_desc')}, bounds={btn.get('bounds')}")
    print(f"前20个可见文本: {analysis['visible_texts']}")
    print(f"报告已保存: {report_path}")


if __name__ == "__main__":
    main()
