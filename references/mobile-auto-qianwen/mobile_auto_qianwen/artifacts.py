import time
from pathlib import Path

from .adb_client import AdbClient


def save_state(adb: AdbClient, output_dir: str | Path, label: str) -> dict:
    from .ui_xml import collect_nodes
    xml = adb.dump_xml()
    Path(output_dir, f"{label}.xml").write_text(xml, encoding="utf-8")
    nodes = collect_nodes(xml)
    return {"xml": xml, "nodes": nodes, "timestamp": time.time()}
