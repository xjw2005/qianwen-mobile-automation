"""全面检测来源页面的文本内容：
1. OCR 截图 → 看完整页面上有什么文字
2. 长按 WebView → 看能不能触发选中/复制/在浏览器打开等菜单
3. 读剪贴板 → 看长按后有没有自动复制内容
4. 如果页面可滚动，滚动后再次 OCR

前提：手机上已打开一个来源页面。
用法：python scripts_qianwen/probe_source_page_text.py
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.ocr import ocr_screenshot, compact_text
from mobile_auto_qianwen.ui_xml import collect_nodes, visible_texts, find_nodes
from mobile_auto_qianwen.app import read_clipboard, swipe_up, swipe_down

OUTPUT_DIR = Path("results/snapshots/probe-source-text").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

adb = AdbClient()
adb.resolve_serial()
print(f"设备: {adb.serial}\n")

url_re = re.compile(r'https?://[^\s\]\)\"\'\>]+')
domain_re = re.compile(r'\b[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z]{2,}\b')

# ──── 1. XML 标题 ────
xml = adb.dump_xml()
nodes = collect_nodes(xml)
title_nodes = find_nodes(nodes, resource_id="com.aliyun.tongyi:id/tv_title")
title = title_nodes[0].get("text", "") if title_nodes else ""
print(f"📌 标题栏: {title[:120]}")

# ──── 2. OCR 整个页面 ────
print("\n" + "=" * 70)
print("📷 OCR 整个页面")
print("=" * 70)
ocr_result = ocr_screenshot(adb, str(OUTPUT_DIR), "source-full-page")
urls_from_ocr = []
all_ocr_texts = []

for i, line in enumerate(ocr_result.get("lines", [])):
    text = line.get("text", "").strip()
    if text:
        compact = compact_text(text)
        all_ocr_texts.append(compact)
        display = text[:150] + "..." if len(text) > 150 else text
        print(f"[{i:3d}] y={line.get('centerY',0):4d} {display}")
        urls_from_ocr.extend(url_re.findall(text))

print(f"\nOCR 总行数: {len(all_ocr_texts)}")
if urls_from_ocr:
    print(f"✅ OCR 找到 URL: {urls_from_ocr}")
else:
    print("❌ OCR 未找到完整 URL")

# 检查域名
domains_found = set()
for text in all_ocr_texts:
    domains_found.update(domain_re.findall(text))
if domains_found:
    print(f"🔗 OCR 找到可能的域名: {sorted(domains_found)}")

# ──── 3. 长按 WebView ────
print("\n" + "=" * 70)
print("🖐 长按 WebView → 检测上下文菜单")
print("=" * 70)
webview_node = None
for n in nodes:
    cls = n.get("class", "")
    if "webkit" in cls.lower() or "webview" in cls.lower():
        bounds = n.get("parsedBounds")
        if bounds:
            webview_node = n
            break

if webview_node:
    bounds = webview_node["parsedBounds"]
    cx, cy = bounds["centerX"], bounds["centerY"]
    # 长按 WebView 中间位置
    adb.command(["shell", "input", "swipe", str(cx), str(cy), str(cx), str(cy), "1500"])
    time.sleep(1.0)
    after_menu_xml = adb.dump_xml()
    (OUTPUT_DIR / "after-longpress.xml").write_text(after_menu_xml, encoding="utf-8")
    menu_nodes = collect_nodes(after_menu_xml)
    menu_texts = visible_texts(menu_nodes)
    print(f"长按后可见文本: {menu_texts[:30]}")
    
    # 检查有没有"复制"/"全选"/"在浏览器打开"等菜单项
    menu_keywords = ["复制", "全选", "在浏览器", "打开", "分享", "选择", "拷贝"]
    found_menu = [t for t in menu_texts if any(kw in t for kw in menu_keywords)]
    if found_menu:
        print(f"✅ 检测到菜单: {found_menu}")
    else:
        print("❌ 长按未弹出任何可用菜单")

# ──── 4. 读剪贴板 ────
print("\n" + "=" * 70)
print("📋 剪贴板内容")
print("=" * 70)
clipboard = read_clipboard(adb)
if clipboard:
    print(f"剪贴板: {clipboard[:500]}")
    cb_urls = url_re.findall(clipboard)
    if cb_urls:
        print(f"✅ 剪贴板中有 URL: {cb_urls}")
else:
    print("❌ 剪贴板为空")

# ──── 5. 尝试滚动页面再 OCR ────
print("\n" + "=" * 70)
print("📜 滚动页面后再次 OCR")
print("=" * 70)
for scroll_i in range(3):
    swipe_up(adb)
    time.sleep(0.3)
scroll_ocr = ocr_screenshot(adb, str(OUTPUT_DIR), "source-scrolled")
new_texts = []
for line in scroll_ocr.get("lines", []):
    text = line.get("text", "").strip()
    if text:
        compact = compact_text(text)
        if compact not in all_ocr_texts:
            new_texts.append(compact)

if new_texts:
    print(f"滚动后发现 {len(new_texts)} 行新文本:")
    for t in new_texts[:15]:
        display = t[:150] + "..." if len(t) > 150 else t
        print(f"  {display}")
else:
    print("❌ 滚动后无新文本（页面可能不可滚动或已到底）")

# ──── 6. 汇总 ────
print("\n" + "=" * 70)
print("📊 汇总")
print("=" * 70)
print(f"XML 标题: {title[:80]}")
print(f"OCR 发现 URL: {len(urls_from_ocr)} 个 → {urls_from_ocr}")
print(f"OCR 发现域名: {sorted(domains_found)}")
print(f"OCR 总文本行: {len(all_ocr_texts)}")
print(f"长按菜单: {'有' if found_menu else '无'}")
print(f"剪贴板: {'有' if clipboard else '空'}")
print(f"滚后新文本: {len(new_texts)} 行")
print(f"\n截图保存: {OUTPUT_DIR}")
