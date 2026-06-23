"""测试当前界面的 XML，看能不能找到 URL。
前提：手机上已经打开了一个来源页面。
用法：python scripts_qianwen/probe_source_page_urls.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mobile_auto_qianwen.adb_client import AdbClient
from mobile_auto_qianwen.ui_xml import collect_nodes, visible_texts, find_nodes

OUTPUT_DIR = Path("results/snapshots/probe-source-urls").resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

adb = AdbClient()
adb.resolve_serial()
print(f"设备: {adb.serial}")

# dump XML
xml = adb.dump_xml()
(OUTPUT_DIR / "source-page.xml").write_text(xml, encoding="utf-8")
nodes = collect_nodes(xml)
print(f"节点总数: {len(nodes)}")

# 收集所有可见文本
texts = visible_texts(nodes)
print(f"可见文本行数: {len(texts)}")

# 打印所有文本（截断）
print("\n" + "=" * 60)
print("所有可见文本:")
print("=" * 60)
for i, t in enumerate(texts):
    display = t[:120] + "..." if len(t) > 120 else t
    print(f"[{i:3d}] {display}")

# 查找 URL
url_re = re.compile(r'https?://[^\s\]\)\"\'\>]+')
all_text = "\n".join(texts)
urls = url_re.findall(all_text)
print("\n" + "=" * 60)
if urls:
    print(f"找到 {len(urls)} 个 URL:")
    for u in urls:
        print(f"  {u}")
else:
    print("❌ 未找到任何 URL")

# 查找 clickable 元素
clickable = [n for n in nodes if n.get("clickable") == "true"]
print(f"\n可点击元素: {len(clickable)}")
for n in clickable[:20]:
    text = n.get("text", "") + n.get("content_desc", "")
    text = text.strip()[:80]
    bounds = n.get("bounds", "")
    print(f"  [{n.get('class','?')}] text={text!r} bounds={bounds}")

# 查找 resource-id
with_id = [n for n in nodes if n.get("resource_id")]
print(f"\n有 resource-id 的元素: {len(with_id)}")
rids = set(n["resource_id"] for n in with_id)
for rid in sorted(rids):
    print(f"  {rid}")

print(f"\nXML 已保存: {OUTPUT_DIR / 'source-page.xml'}")
