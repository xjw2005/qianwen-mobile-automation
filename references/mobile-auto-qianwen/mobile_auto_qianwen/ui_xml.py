import time
from pathlib import Path
from xml.etree import ElementTree

from .adb_client import AdbClient


def save_state(adb: AdbClient, output_dir: str | Path, label: str) -> dict:
    xml = adb.dump_xml()
    Path(output_dir, f"{label}.xml").write_text(xml, encoding="utf-8")
    nodes = collect_nodes(xml)
    return {"xml": xml, "nodes": nodes, "timestamp": time.time()}


def parse_bounds(bounds: str) -> dict[str, int] | None:
    import re
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    return {
        "left": left, "top": top, "right": right, "bottom": bottom,
        "centerX": (left + right) // 2, "centerY": (top + bottom) // 2,
    }


def collect_nodes(xml_text: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    nodes = []
    for elem in root.iter("node"):
        bounds = parse_bounds(elem.attrib.get("bounds", ""))
        nodes.append({
            "text": elem.attrib.get("text", ""),
            "resource_id": elem.attrib.get("resource-id", ""),
            "content_desc": elem.attrib.get("content-desc", ""),
            "class": elem.attrib.get("class", ""),
            "bounds": elem.attrib.get("bounds", ""),
            "clickable": elem.attrib.get("clickable", ""),
            "enabled": elem.attrib.get("enabled", ""),
            "parsedBounds": bounds,
        })
    return nodes


def find_nodes(nodes: list[dict], *, resource_id: str | None = None, text_contains: str | None = None) -> list[dict]:
    result = []
    for node in nodes:
        if resource_id is not None and node.get("resource_id") == resource_id:
            result.append(node)
        if text_contains is not None and text_contains in node.get("text", ""):
            result.append(node)
    return result


def visible_texts(nodes: list[dict]) -> list[str]:
    return [n.get("text", "").strip() for n in nodes if n.get("text", "").strip()]


def extract_urls_from_text(text: str) -> list[str]:
    import re
    return re.findall(r'https?://[^\s\]\)\"\'\>]+', text)
