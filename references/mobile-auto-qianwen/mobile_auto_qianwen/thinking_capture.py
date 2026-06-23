import json
import time
from pathlib import Path

from .adb_client import AdbClient
from .artifacts import save_state
from .ui_xml import find_nodes, visible_texts


THINKING_PAGE_TITLE = "思考内容"


def is_generating(nodes: list[dict]) -> bool:
    combined = "\n".join(visible_texts(nodes))
    keywords = ("正在生成", "正在回答", "思考中", "推理中")
    return any(kw in combined for kw in keywords)


def wait_for_thinking_complete(adb: AdbClient, output_dir: str, timeout: float = 180.0, stable_seconds: int = 3) -> dict:
    """等待AI思考完成（分享按钮出现或'查看全部'出现且稳定）"""
    started = time.time()
    stable = 0
    samples = []
    while time.time() - started < timeout:
        state = save_state(adb, output_dir, "thinking-wait")
        nodes = state["nodes"]
        if is_generating(nodes):
            stable = 0
        else:
            # 检查是否有分享按钮或查看全部 -> 说明回答已生成
            has_share = any(
                "分享" in (n.get("text", "") + n.get("content_desc", ""))
                for n in nodes
            )
            has_view_all = any(
                "查看全部" in (n.get("text", "") + n.get("content_desc", ""))
                for n in nodes
            )
            if has_share or has_view_all:
                stable += 1
            else:
                stable = 0
        samples.append({"elapsedMs": int((time.time() - started) * 1000), "stable": stable, "generating": is_generating(nodes)})
        if stable >= stable_seconds:
            return {"ok": True, "samples": samples}
        time.sleep(1.0)
    return {"ok": False, "error": "timeout", "samples": samples}
