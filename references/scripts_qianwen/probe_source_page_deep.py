"""深度挖掘当前页面的 XML，递归遍历所有节点，检查所有属性。
前提：手机上已打开一个来源页面。
用法：python scripts_qianwen/probe_source_page_deep.py
"""
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_auto_qianwen.adb_client import AdbClient

OUTPUT_DIR = Path("results/snapshots/probe-source-deep").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

adb = AdbClient()
adb.resolve_serial()
print(f"设备: {adb.serial}")

XML = adb.dump_xml()
(OUTPUT_DIR / "source-page.xml").write_text(XML, encoding="utf-8")

root = ElementTree.fromstring(XML)
url_re = re.compile(r'https?://[^\s\]\)\"\'\>]+')
all_findings = []

def indent(depth):
    return "  " * depth

def probe_node(elem, depth=0):
    """递归挖掘节点，把一切有用信息挖出来"""
    tag = elem.tag
    attrs = elem.attrib

    # 收集所有属性中有文字/URL的
    text = attrs.get("text", "")
    desc = attrs.get("content-desc", "")
    rid = attrs.get("resource-id", "")
    cls = attrs.get("class", "")
    naf = attrs.get("NAF", "")
    bounds = attrs.get("bounds", "")
    clickable = attrs.get("clickable", "")
    long_clickable = attrs.get("long-clickable", "")
    scrollable = attrs.get("scrollable", "")
    focused = attrs.get("focused", "")
    focusable = attrs.get("focusable", "")

    combined = f"{text} | {desc}"
    urls = url_re.findall(combined)

    # 只打印有意义的节点
    interesting = bool(text) or bool(desc) or bool(rid) or urls or clickable == "true" or scrollable == "true" or focused == "true"

    if interesting:
        info = {
            "depth": depth,
            "class": cls,
            "rid": rid,
            "text": text[:200] if text else "",
            "desc": desc[:200] if desc else "",
            "bounds": bounds,
            "clickable": clickable,
            "long_clickable": long_clickable,
            "scrollable": scrollable,
            "focused": focused,
            "focusable": focusable,
            "naf": naf,
            "urls_in_node": urls,
        }
        all_findings.append(info)

        prefix = "⭐" if urls else ("🔗" if clickable == "true" else ("📜" if scrollable == "true" else "  "))
        print(f"{prefix}{indent(depth)}[{cls.split('.')[-1]}]", end="")
        if text:
            print(f" text='{text[:80]}'", end="")
        if desc:
            print(f" desc='{desc[:80]}'", end="")
        if rid:
            print(f" rid='{rid}'", end="")
        if clickable == "true":
            print(f" clickable", end="")
        if long_clickable == "true":
            print(f" long-clickable", end="")
        if scrollable == "true":
            print(f" scrollable", end="")
        if focused == "true":
            print(f" FOCUSED", end="")
        if urls:
            print(f" URLS={urls}", end="")
        print(f" {bounds}")

    # 递归子节点
    for child in elem:
        probe_node(child, depth + 1)

    # ⚠️ 关键：检查 WebView 内部是否有多层虚拟节点
    # 有的 WebView 会在同一层有很多同级的 View
    if "webview" in cls.lower() or "webkit" in cls.lower():
        print(f"{indent(depth)}  >>> 这是 WebView！可能还有隐藏内容 <<<")
        child_count = sum(1 for _ in elem)
        print(f"{indent(depth)}  直接子节点数: {child_count}")

print("=" * 70)
print("深度挖掘 XML 树（只显示有意义的节点）：")
print("=" * 70)
probe_node(root)

# 摘要
print("\n" + "=" * 70)
print("摘要:")
print("=" * 70)

total_nodes = sum(1 for _ in root.iter())
print(f"总节点数: {total_nodes}")
print(f"有意义的节点: {len(all_findings)}")

urls_found = set()
for f in all_findings:
    for u in f["urls_in_node"]:
        urls_found.add(u)
if urls_found:
    print(f"\n✅ 找到 {len(urls_found)} 个唯一 URL:")
    for u in urls_found:
        print(f"  {u}")
else:
    print("\n❌ 未找到任何 URL")

# 保存完整 JSON
with open(OUTPUT_DIR / "deep-probe.json", "w", encoding="utf-8") as fp:
    json.dump(all_findings, fp, ensure_ascii=False, indent=2)
print(f"\n完整结果: {OUTPUT_DIR / 'deep-probe.json'}")
