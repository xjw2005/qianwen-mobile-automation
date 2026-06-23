import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.app import dump_nodes, extract_answer_share_link, find_share_button
from mobile_auto_qianwen.constants import DEFAULT_ADB
from mobile_auto_qianwen.ui_xml import save_state


def main():
    """CLI entry point for the answer-share probe."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", default=DEFAULT_ADB)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--output-dir", default="outputs/qianwen/share-probe")
    args = parser.parse_args()

    adb = AdbClient(adb=args.adb, serial=args.serial)
    adb.resolve_serial()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    save_state(adb, str(output_dir), "initial")

    # 先打印当前页是否有老的文本分享按钮；真正测试直接调用 extract_answer_share_link，
    # 当前实现会优先滚到底部点回答工具栏分享，失败后再走长按回答兜底。
    nodes = dump_nodes(adb)
    btn = find_share_button(nodes)
    print("find_share_button:", btn)

    result = extract_answer_share_link(adb, str(output_dir), max_scrolls=8)
    print("extract_answer_share_link:", json.dumps({
        "status": result.get("status"),
        "url": result.get("url"),
        "error": result.get("error"),
        "paste": result.get("paste"),
        "context": result.get("context"),
        "contextShare": result.get("contextShare"),
        "copy": result.get("copy"),
        "shareOpen": result.get("shareOpen"),
        "fallbackFrom": result.get("fallbackFrom"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
