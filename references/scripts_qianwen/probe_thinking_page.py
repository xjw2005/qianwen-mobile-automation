import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.app import (
    capture_thinking_content, click_view_all, dump_nodes, ensure_app,
    enter_thinking_mode, find_input_nodes, go_back, is_on_thinking_detail_page, swipe_up
)
from mobile_auto_qianwen.constants import DEFAULT_ADB
from mobile_auto_qianwen.ui_xml import save_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", default=DEFAULT_ADB)
    parser.add_argument("--serial", default=None)
    parser.add_argument("--output-dir", default="outputs/qianwen/thinking-probe")
    args = parser.parse_args()

    adb = AdbClient(adb=args.adb, serial=args.serial)
    adb.resolve_serial()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 确保千问在前台
    ensure_app(adb)
    save_state(adb, str(output_dir), "initial")

    # 尝试进入思考模式
    result = enter_thinking_mode(adb, str(output_dir))
    print("enter_thinking_mode:", result)

    # 检查是否有输入框
    nodes = dump_nodes(adb)
    input_nodes = find_input_nodes(nodes)
    print(f"input_nodes: {len(input_nodes)}")
    for n in input_nodes:
        print(f"  - {n.get('text')} bounds={n.get('bounds')}")

    # 如果当前有查看全部按钮，尝试点击
    view_all = click_view_all(adb, str(output_dir))
    print("click_view_all:", view_all)

    if view_all["ok"]:
        # 确认进入思考详情页
        nodes = dump_nodes(adb)
        print("is_on_thinking_detail_page:", is_on_thinking_detail_page(nodes))

        # 抓取思考内容
        capture = capture_thinking_content(adb, str(output_dir), max_scrolls=3)
        print("capture_thinking_content:", capture)

        # 返回
        go_back(adb)
        save_state(adb, str(output_dir), "after-back")
        print("Returned to previous page.")


if __name__ == "__main__":
    main()
