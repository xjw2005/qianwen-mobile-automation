"""快速单独测试 tap_thinking_reference_trigger。
前提：手机上已打开千问 → 思考详情页（点击过"查看全部"）。
用法：python scripts_qianwen/test_thinking_reference_trigger.py
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.app import tap_thinking_reference_trigger, swipe_down, swipe_up

OUTPUT_DIR = Path("results/snapshots/test-trigger").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

adb = AdbClient()
adb.resolve_serial()
print(f"设备: {adb.serial}")

# 先点一下屏幕确保焦点（避免 OCR 截图时屏幕休眠）
adb.tap(540, 960)

result = tap_thinking_reference_trigger(adb, str(OUTPUT_DIR), max_scrolls=2)
print(json.dumps(result, ensure_ascii=False, indent=2))

if result.get("fallback"):
    print("\n⚠️  走了兜底坐标 (430, 1060)，OCR 没找到'参考资料'")
elif result.get("ok"):
    print(f"\n✅ OCR 识别到'参考资料'，点击了 ({result['tap']['x']}, {result['tap']['y']})")
