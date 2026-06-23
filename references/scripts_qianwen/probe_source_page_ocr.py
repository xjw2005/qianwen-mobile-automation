"""OCR 截图测试来源页面，看能不能找到 URL。
前提：手机上已打开一个来源页面。
用法：python scripts_qianwen/probe_source_page_ocr.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.ocr import ocr_screenshot

OUTPUT_DIR = Path("results/snapshots/probe-source-ocr").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

adb = AdbClient()
adb.resolve_serial()
print(f"设备: {adb.serial}")

# OCR 截图
result = ocr_screenshot(adb, str(OUTPUT_DIR), "source-page-ocr")
print(f"OCR ok: {result['ok']}")
print(f"截图: {result['screenshot']}")

# 打印所有识别到的文字行
print("\n" + "=" * 60)
print("OCR 识别的所有文字行:")
print("=" * 60)
url_re = re.compile(r'https?://[^\s\]\)\"\'\>]+')
found_urls = []

for i, line in enumerate(result.get("lines", [])):
    text = line.get("text", "").strip()
    if text:
        display = text[:150] + "..." if len(text) > 150 else text
        print(f"[{i:3d}] {display}")
        urls = url_re.findall(text)
        found_urls.extend(urls)

print("\n" + "=" * 60)
if found_urls:
    print(f"✅ OCR 找到 {len(found_urls)} 个 URL:")
    for u in found_urls:
        print(f"  {u}")
else:
    print("❌ OCR 也未找到 URL")
    print("\n完整 OCR 文本:")
    print(result.get("text", "")[:2000])
