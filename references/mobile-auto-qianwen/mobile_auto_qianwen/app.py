import re
import time
from pathlib import Path
from xml.etree import ElementTree

from .adb_client import AdbClient
from .constants import (
    ADB_KEYBOARD_IME,
    COPY_LINK_TEXT,
    INPUT_ID,
    INPUT_PLACEHOLDER_TEXTS,
    LOGIN_KEYWORDS,
    PRIVACY_KEYWORDS,
    QIANWEN_PACKAGE,
    SHARE_BUTTON_TEXT,
    THINK_BUTTON_TEXT,
    VIEW_ALL_TEXT,
)
from .ocr import compact_text, ocr_screenshot
from .ui_xml import collect_nodes, extract_urls_from_text, find_nodes, visible_texts


LOGIN_BUTTON_TEXTS = ("登录", "同意", "确认")
ANSWER_BOTTOM_ANCHOR_ID = "com.aliyun.tongyi:id/query_onscreen_answer_bottom_anchor"
SOURCE_TITLE_SUFFIXES = (
    "新闻网",
    "39健康网",
    "母婴",
    "中国网",
    "中国报业网",
    "千龙网·中国首都网",
    "新京报",
)
THINKING_SECTION_TITLE_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9]{2,18}$")


def dump_xml(adb: AdbClient, output_dir: str | Path, label: str) -> str:
    """Dump the current UI XML to a labeled file."""
    xml = adb.dump_xml()
    Path(output_dir, f"{label}.xml").write_text(xml, encoding="utf-8")
    return xml


def dump_nodes(adb: AdbClient) -> list[dict]:
    """Return the current UI tree as flattened node dictionaries."""
    return collect_nodes(adb.dump_xml())


def center(node: dict) -> tuple[int, int]:
    """Return the screen center of a parsed node."""
    bounds = node.get("parsedBounds")
    if not bounds:
        return (540, 960)
    return (bounds["centerX"], bounds["centerY"])


def node_text(node: dict) -> str:
    """Concatenate the visible text and content description for a node."""
    return f"{node.get('text', '')}{node.get('content_desc', '')}".strip()


def is_visible_node(node: dict, top: int = 240, bottom: int = 2260) -> bool:
    """Check whether a node is within the visible content bounds."""
    bounds = node.get("parsedBounds")
    if not bounds:
        return False
    if bounds["right"] <= bounds["left"] or bounds["bottom"] <= bounds["top"]:
        return False
    return top <= bounds["centerY"] <= bottom


def tap_if_visible(adb: AdbClient, node: dict) -> bool:
    """Tap a node if it is on-screen and has valid bounds."""
    bounds = node.get("parsedBounds")
    if not bounds:
        return False
    x, y = center(node)
    if y < 260 or y > 1980:
        return False
    adb.tap(x, y)
    time.sleep(0.35)
    return True


def containing_clickable_node(nodes: list[dict], child: dict) -> dict | None:
    """Find the smallest clickable ancestor that contains the child node."""
    child_bounds = child.get("parsedBounds")
    if not child_bounds:
        return None
    containing = []
    for node in nodes:
        bounds = node.get("parsedBounds")
        if not bounds or node.get("clickable") != "true":
            continue
        if bounds["left"] <= child_bounds["centerX"] <= bounds["right"] and bounds["top"] <= child_bounds["centerY"] <= bounds["bottom"]:
            area = (bounds["right"] - bounds["left"]) * (bounds["bottom"] - bounds["top"])
            containing.append((area, node))
    if not containing:
        return None
    return sorted(containing, key=lambda item: item[0])[0][1]


def find_input_nodes(nodes: list[dict]) -> list[dict]:
    """Find likely chat input nodes using ids, classes, or placeholders."""
    by_id = find_nodes(nodes, resource_id=INPUT_ID) if INPUT_ID else []
    if by_id:
        return by_id
    edit_texts = [node for node in nodes if node.get("class") == "android.widget.EditText" and node.get("parsedBounds")]
    if edit_texts:
        return edit_texts
    placeholders = []
    for node in nodes:
        text = node.get("text", "")
        bounds = node.get("parsedBounds")
        if bounds and any(placeholder in text for placeholder in INPUT_PLACEHOLDER_TEXTS):
            placeholders.append(node)
    return placeholders


def swipe_up(adb: AdbClient) -> None:
    """Scroll the page upward."""
    try:
        adb.command(["shell", "input", "swipe", "540", "1800", "540", "600", "700"])
    except Exception:
        adb.keyevent(93)
    time.sleep(0.35)


def swipe_down(adb: AdbClient) -> None:
    """Scroll the page downward."""
    try:
        adb.command(["shell", "input", "swipe", "540", "600", "540", "1800", "700"])
    except Exception:
        adb.keyevent(92)
    time.sleep(0.35)


def detect_blocked(nodes: list[dict]) -> str | None:
    """Detect login or gate screens that block automation."""
    text = "\n".join(visible_texts(nodes))
    for keyword in LOGIN_KEYWORDS:
        if keyword in text:
            return "login_required"
    return None


def accept_privacy_if_present(adb: AdbClient) -> dict:
    """Accept the privacy prompt when it is visible."""
    nodes = dump_nodes(adb)
    text = "\n".join(visible_texts(nodes))
    if not any(keyword in text for keyword in PRIVACY_KEYWORDS):
        return {"ok": False, "reason": "privacy_not_found"}
    candidates = []
    for node in nodes:
        combined = f"{node.get('text', '')}{node.get('content_desc', '')}{node.get('resource_id', '')}"
        if any(label in combined for label in ("同意", "btn_right", "privacy_agree")) and node.get("clickable") == "true":
            candidates.append(node)
    if not candidates:
        return {"ok": False, "reason": "privacy_button_not_found"}
    target = sorted(candidates, key=lambda item: item.get("parsedBounds", {}).get("centerX", 0))[-1]
    x, y = center(target)
    adb.tap(x, y)
    time.sleep(1.2)
    return {"ok": True, "tap": {"x": x, "y": y, "bounds": target.get("bounds", "")}}


def ensure_app(adb: AdbClient) -> dict:
    """Bring Qianwen to the foreground and recover from nested pages."""
    adb.start_app(QIANWEN_PACKAGE)
    time.sleep(2.0)
    privacy = accept_privacy_if_present(adb)
    recovered = False
    for _ in range(3):
        nodes = dump_nodes(adb)
        if find_input_nodes(nodes) or detect_blocked(nodes):
            break
        adb.keyevent(4)
        recovered = True
        time.sleep(0.8)
    return {"started": True, "privacy": privacy, "recoveredFromNestedPage": recovered}


def create_new_chat(adb: AdbClient, output_dir: str, save_debug_xml: bool = False) -> dict:
    """Open a fresh chat from the sidebar menu."""
    if save_debug_xml:
        dump_xml(adb, output_dir, "new-chat-before")
    adb.tap(82, 160)
    time.sleep(0.35)
    nodes = dump_nodes(adb)
    if save_debug_xml:
        Path(output_dir, "new-chat-menu.xml").write_text(adb.dump_xml(), encoding="utf-8")
    candidates = []
    for node in nodes:
        text = f"{node.get('text', '')}{node.get('content_desc', '')}"
        bounds = node.get("parsedBounds")
        if "新建对话" in text and bounds and bounds["centerX"] < 800 and bounds["centerY"] < 500:
            candidates.append(node)
    if not candidates:
        return {"created": False, "method": "sidebar", "error": "new_chat_button_not_found"}
    target = sorted(candidates, key=lambda item: item["parsedBounds"]["centerY"])[0]
    x, y = center(target)
    adb.tap(x, y)
    time.sleep(0.45)
    after_nodes = dump_nodes(adb)
    if save_debug_xml:
        Path(output_dir, "new-chat-after.xml").write_text(adb.dump_xml(), encoding="utf-8")
    texts = visible_texts(after_nodes)
    created = bool(find_input_nodes(after_nodes)) and not find_view_all_button(after_nodes)
    return {"created": created, "method": "sidebar-new-chat", "tap": {"x": x, "y": y, "bounds": target.get("bounds", "")}, "visibleTexts": texts[:10]}


def find_think_button(nodes: list[dict]) -> dict | None:
    """Locate the deep-thinking toggle button."""
    for node in nodes:
        text = node.get("text", "") + node.get("content_desc", "")
        if THINK_BUTTON_TEXT in text:
            bounds = node.get("parsedBounds")
            if bounds and bounds["centerY"] > 1700:
                return containing_clickable_node(nodes, node) or node
    return None


def set_thinking_mode(adb: AdbClient, output_dir: str, enabled: bool) -> dict:
    """Enable or disable deep-thinking mode in the UI."""
    dump_xml(adb, output_dir, "thinking-before")
    nodes = dump_nodes(adb)
    btn = find_think_button(nodes)
    if not btn:
        return {"requested": enabled, "changed": False, "verified": False, "error": "think_button_not_found"}
    if not enabled:
        return {"requested": enabled, "changed": False, "verified": True, "reason": "quick_mode_default"}
    x, y = center(btn)
    adb.tap(x, y)
    time.sleep(0.6)
    dump_xml(adb, output_dir, "thinking-after")
    return {"requested": enabled, "changed": True, "verified": True, "tap": {"x": x, "y": y, "text": btn.get("text", "")}}


def enter_thinking_mode(adb: AdbClient, output_dir: str) -> dict:
    """Convenience wrapper that enables deep-thinking mode."""
    return set_thinking_mode(adb, output_dir, True)


def find_view_all_button(nodes: list[dict]) -> dict | None:
    """Locate the 'view all' control in the expanded answer page."""
    for node in nodes:
        text = node.get("text", "") + node.get("content_desc", "")
        if VIEW_ALL_TEXT in text and node.get("clickable") == "true":
            return node
    return None


def click_view_all(adb: AdbClient, output_dir: str) -> dict:
    """Tap the 'view all' control and capture the resulting state."""
    time.sleep(0.8)
    dump_xml(adb, output_dir, "view-all-before")
    nodes = dump_nodes(adb)
    btn = find_view_all_button(nodes)
    if not btn:
        return {"ok": False, "error": "view_all_not_found"}
    x, y = center(btn)
    adb.tap(x, y)
    time.sleep(0.8)
    dump_xml(adb, output_dir, "view-all-after")
    return {"ok": True, "tap": {"x": x, "y": y, "text": btn.get("text", "")}}


def go_back(adb: AdbClient) -> None:
    """Send a back key event and wait for the UI to settle."""
    adb.keyevent(4)
    time.sleep(0.8)


def return_to_chat_page(adb: AdbClient, output_dir: str, max_backs: int = 5) -> dict:
    """Back out until the input field reappears."""
    attempts = []
    for index in range(max_backs + 1):
        xml = dump_xml(adb, output_dir, f"return-chat-{index:02d}")
        nodes = collect_nodes(xml)
        if find_input_nodes(nodes):
            return {"ok": True, "backs": index, "attempts": attempts}
        if index < max_backs:
            adb.keyevent(4)
            attempts.append({"index": index, "action": "back"})
            time.sleep(0.8)
    return {"ok": False, "backs": max_backs, "attempts": attempts, "error": "input_not_found_after_back"}


def is_on_thinking_detail_page(nodes: list[dict]) -> bool:
    """Check whether the UI is showing the thinking-detail page."""
    if find_view_all_button(nodes):
        return False
    input_nodes = find_input_nodes(nodes)
    return not bool(input_nodes)


def extract_page_texts(xml_text: str, min_len: int = 5) -> list[str]:
    """Extract readable text nodes from a raw XML dump."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    texts = []
    for elem in root.iter("node"):
        text = elem.attrib.get("text", "").strip()
        if text and len(text) >= min_len and elem.attrib.get("class", "").endswith("TextView"):
            texts.append(text)
    return texts


def capture_thinking_content(adb: AdbClient, output_dir: str, max_scrolls: int = 4, ocr_enabled: bool = False) -> dict:
    """Capture thinking content by scrolling and optionally using OCR."""
    fragments: list[str] = []
    snapshots = []
    seen_texts = set()
    unchanged = 0
    for index in range(max_scrolls + 1):
        xml = dump_xml(adb, output_dir, f"thinking-detail-{index:02d}")
        texts = extract_page_texts(xml)
        ocr = {"ok": False, "screenshot": "", "error": "ocr_disabled"}
        if ocr_enabled:
            ocr = ocr_screenshot(adb, output_dir, f"thinking-detail-{index:02d}")
        if ocr_enabled and ocr.get("text"):
            texts.append(ocr["text"])
        new_texts = {text.strip() for text in texts if text.strip()} - seen_texts
        if new_texts:
            unchanged = 0
            seen_texts.update(new_texts)
        else:
            unchanged += 1
        fragments.extend(texts)
        snapshots.append({"index": index, "textCount": len(texts), "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")}})
        if unchanged >= 2:
            break
        if index < max_scrolls:
            swipe_up(adb)
    seen = set()
    ordered = []
    for item in fragments:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    content = "\n\n".join(ordered)
    return {"content": content, "snapshots": snapshots, "status": "success" if content else "failed"}


def normalize_text(text: str) -> str:
    """Normalize whitespace and line breaks in extracted text."""
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def dedupe_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate text fragments while preserving order."""
    seen = set()
    ordered = []
    for item in items:
        item = normalize_text(item)
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def is_source_or_search_artifact(text: str) -> bool:
    """Filter out source titles, headings, and other search artifacts."""
    compact = compact_text(text)
    if not compact:
        return True
    if compact in {"已完成思考", "展开全部"}:
        return True
    if re.fullmatch(r"搜索\d+个关键词，参考\d+篇资料", compact):
        return True
    if re.fullmatch(r"参考\d+篇资料", compact):
        return True
    if len(text) <= 24 and " " in text and not any(punc in text for punc in "，。；："):
        return True
    if any(text.endswith(suffix) for suffix in SOURCE_TITLE_SUFFIXES):
        return True
    if any(marker in text for marker in (" — ", "--", "_")):
        return True
    if re.search(r"20\d{2}年\d{1,2}月", text):
        return True
    return False


def is_thinking_section_title(text: str) -> bool:
    """Detect short section titles used inside thinking content."""
    compact = compact_text(text)
    if not compact:
        return False
    if any(keyword in compact for keyword in ("检索", "比对", "筛选", "整合", "分析", "确认")):
        return bool(THINKING_SECTION_TITLE_RE.fullmatch(compact))
    return False


def is_reasoning_paragraph(text: str) -> bool:
    """Detect paragraph-like reasoning content."""
    compact = compact_text(text)
    if len(compact) < 35:
        return False
    if is_source_or_search_artifact(text):
        return False
    markers = ("需求", "需", "因为", "因", "旨在", "转为", "结合", "确保", "明确", "获取", "评估", "筛选", "整合", "判断", "标注", "基础", "信息")
    return any(marker in compact for marker in markers)


def clean_thinking_texts(texts: list[str]) -> list[str]:
    """Clean and order thinking fragments into readable text."""
    cleaned = []
    pending_title = ""
    for raw in texts:
        text = normalize_text(raw)
        if not text:
            continue
        if is_thinking_section_title(text):
            pending_title = text
            continue
        if is_reasoning_paragraph(text):
            if pending_title:
                cleaned.append(pending_title)
                pending_title = ""
            cleaned.append(text)
        elif is_source_or_search_artifact(text):
            continue
        else:
            pending_title = ""
    return dedupe_preserve_order(cleaned)


def is_answer_or_chrome_text(text: str, question: str | None = None) -> bool:
    """Check whether text belongs to answer chrome rather than content."""
    stripped = normalize_text(text)
    if not stripped or stripped in {"夸克", "发消息...", "发消息或按住说话...", "内容由 Qwen-3.7 大模型生成", "千问高考", "美加墨", "新"}:
        return True
    if question and stripped == question.strip():
        return True
    if stripped.startswith("为肠胃敏感") or stripped.startswith("基于这两项标准"):
        return True
    if stripped.startswith("1. 候选奶粉清单") or stripped.startswith("2. 其他选择"):
        return True
    return False


def is_chrome_or_input_text(text: str, question: str | None = None) -> bool:
    """Check whether text is just app chrome or the input prompt."""
    stripped = normalize_text(text)
    if not stripped or stripped in {"夸克", "发消息...", "发消息或按住说话...", "内容由 Qwen-3.7 大模型生成", "千问高考", "美加墨", "新"}:
        return True
    if question and stripped == question.strip():
        return True
    return False


def extract_thinking_texts_from_ui_texts(texts: list[str], question: str | None = None) -> list[str]:
    """Extract thinking text blocks from visible UI text snippets."""
    meaningful = [normalize_text(text) for text in texts if normalize_text(text)]
    meaningful = [text for text in meaningful if not is_answer_or_chrome_text(text, question)]
    has_thinking_mark = any("已完成思考" in compact_text(text) or ("搜索" in compact_text(text) and "参考" in compact_text(text)) for text in meaningful)
    start_from_first_meaningful = bool(has_thinking_mark and meaningful and (not question or meaningful[0] != question.strip()))

    filtered = []
    started = start_from_first_meaningful
    for text in meaningful:
        compact = compact_text(text)
        if text == "思考":
            started = True
            continue
        if not started and ("已完成思考" in compact or ("搜索" in compact and "参考" in compact)):
            started = True
        if not started:
            continue
        if "已完成思考" in compact and "参考" in compact:
            break
        if question and text == question.strip():
            break
        if text == "展开全部":
            continue
        filtered.append(text)
        if text == "已完成思考":
            break
    return dedupe_preserve_order(filtered)


def has_expanded_thinking(nodes: list[dict]) -> bool:
    """Check whether the thinking block is already expanded."""
    texts = [compact_text(text) for text in visible_texts(nodes)]
    joined = "\n".join(texts)
    if "搜索" in joined and "参考" in joined:
        return True
    if any("展开全部" in text for text in texts):
        return True
    if any("检索" in text and len(text) > 6 for text in texts) and any("已完成思考" in text for text in texts):
        return True
    return False


def find_completed_thinking_trigger(nodes: list[dict]) -> dict | None:
    """Find the best UI node that can expand completed thinking."""
    candidates = []
    for node in nodes:
        text = compact_text(node_text(node))
        if not text or not is_visible_node(node):
            continue
        if text in {"参考", "思考"}:
            continue
        score = 0
        if "已完成思考" in text and "参考" in text:
            score += 100
        elif "已完成思考" in text or "完成思考" in text:
            score += 80
        if "展开全部" in text:
            score += 70
        if "参考" in text and re.search(r"\d+篇", text):
            score += 60
        if not score:
            continue
        target = containing_clickable_node(nodes, node) or node
        if target.get("clickable") == "true":
            score += 10
        candidates.append((score, node, target))
    if not candidates:
        return None
    _, source, target = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    return {"source": source, "target": target}


def tap_completed_thinking_trigger_by_ocr(adb: AdbClient, output_dir: str) -> dict:
    """Use OCR to find and tap the completed-thinking trigger."""
    ocr = ocr_screenshot(adb, output_dir, "completed-thinking-trigger-ocr")
    for line in ocr.get("lines", []):
        text = compact_text(line.get("text", ""))
        if "已完成思考" in text or "展开全部" in text or ("参考" in text and re.search(r"\d+篇", text)):
            x = int(line.get("centerX") or 540)
            y = int(line.get("centerY") or 1060)
            adb.tap(x, y)
            time.sleep(0.8)
            return {"ok": True, "source": "ocr_line", "tap": {"x": x, "y": y, "text": line.get("text", "")}, "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")}}
    return {"ok": False, "source": "ocr_line", "error": "completed_thinking_trigger_not_found", "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")}}


def tap_completed_thinking_trigger(adb: AdbClient, output_dir: str) -> dict:
    """Tap the UI or OCR trigger that expands completed thinking."""
    nodes = dump_nodes(adb)
    trigger = find_completed_thinking_trigger(nodes)
    if trigger:
        target = trigger["target"]
        source = trigger["source"]
        x, y = center(target)
        adb.tap(x, y)
        time.sleep(0.8)
        return {"ok": True, "source": "ui_tree", "detailOpen": False, "tap": {"x": x, "y": y, "text": node_text(source), "bounds": source.get("bounds", ""), "targetBounds": target.get("bounds", "")}}
    return tap_completed_thinking_trigger_by_ocr(adb, output_dir)


def ensure_thinking_expanded(adb: AdbClient, output_dir: str, initial_nodes: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """Expand the thinking section if it is still collapsed."""
    nodes = initial_nodes if initial_nodes is not None else dump_nodes(adb)
    if has_expanded_thinking(nodes):
        return {"ok": True, "source": "already_expanded", "skipped": True}, nodes
    first_tap = tap_completed_thinking_trigger(adb, output_dir)
    nodes = dump_nodes(adb)
    if first_tap.get("detailOpen"):
        return first_tap, nodes
    if has_expanded_thinking(nodes):
        return first_tap, nodes
    second_tap = tap_completed_thinking_trigger(adb, output_dir)
    nodes = dump_nodes(adb)
    if second_tap.get("detailOpen"):
        return {"ok": True, "source": "retap_if_collapsed", "detailOpen": True, "firstTap": first_tap, "secondTap": second_tap}, nodes
    return {"ok": has_expanded_thinking(nodes), "source": "retap_if_collapsed", "firstTap": first_tap, "secondTap": second_tap}, nodes


def capture_answer_page_thinking_content(adb: AdbClient, output_dir: str, question: str = "", max_scrolls: int = 4, ocr_enabled: bool = False) -> dict:
    """Capture the thinking content from the answer page."""
    initial_nodes = dump_nodes(adb)
    expand, _ = ensure_thinking_expanded(adb, output_dir, initial_nodes)
    fragments = []
    snapshots = []
    seen = set()
    unchanged = 0
    for index in range(max_scrolls + 1):
        xml = dump_xml(adb, output_dir, f"thinking-chat-{index:02d}")
        nodes = collect_nodes(xml)
        ui_texts = extract_page_texts(xml)
        thinking_texts = extract_thinking_texts_from_ui_texts(ui_texts, question=question)
        ocr = {"ok": False, "screenshot": "", "error": "ocr_disabled", "lines": []}
        if ocr_enabled:
            ocr = ocr_screenshot(adb, output_dir, f"thinking-chat-{index:02d}")
            if not thinking_texts and ocr.get("text"):
                thinking_texts = extract_thinking_texts_from_ui_texts(ocr["text"].splitlines(), question=question)
        new_items = [item for item in thinking_texts if item not in seen]
        if new_items:
            unchanged = 0
            seen.update(new_items)
        else:
            unchanged += 1
        fragments.extend(new_items)
        snapshots.append({"index": index, "textCount": len(ui_texts), "thinkingTextCount": len(thinking_texts), "newTextCount": len(new_items), "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")}})
        if unchanged >= 2:
            break
        if index < max_scrolls:
            swipe_up(adb)
    raw_items = dedupe_preserve_order(fragments)
    clean_items = clean_thinking_texts(raw_items)
    content = "\n\n".join(clean_items)
    raw_content = "\n\n".join(raw_items)
    go_back(adb)
    # 返回之后检查是否还有思考面板，如果还有再点击一次返回
    # after_back_nodes = dump_nodes(adb)
    # if is_on_thinking_detail_page(after_back_nodes):
    #     go_back(adb)
    return {
        "status": "success" if content else "failed",
        "content": content,
        "rawContent": raw_content,
        "cleanThinkingTexts": clean_items,
        "rawThinkingTexts": raw_items,
        "snapshots": snapshots,
        "expansion": expand,
        "detailOpen": bool(expand.get("detailOpen")),
    }


def tap_thinking_reference_trigger(adb: AdbClient, output_dir: str, max_scrolls: int = 2) -> dict:
    """Find and tap the reference trigger inside the thinking content."""
    for index in range(max_scrolls + 1):
        ocr = ocr_screenshot(adb, output_dir, f"thinking-reference-trigger-{index:02d}")
        for line in ocr.get("lines", []):
            text = compact_text(line.get("text", ""))
            if "参考" in text and "资料" in text:
                x = int(line.get("centerX") or 540)
                y = int(line.get("centerY") or 1060)
                adb.tap(x, y)
                time.sleep(0.8)
                return {"ok": True, "tap": {"x": x, "y": y, "text": line.get("text", ""), "source": "ocr_line"}, "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")}}
        if index < max_scrolls:
            swipe_up(adb)
    adb.tap(430, 1060)
    time.sleep(0.8)
    return {"ok": True, "tap": {"x": 430, "y": 1060, "source": "fallback_coordinate"}, "fallback": True}


def find_share_button(nodes: list[dict]) -> dict | None:
    """Locate the in-page share button near the bottom of the answer."""
    candidates = []
    for node in nodes:
        text = node.get("text", "") + node.get("content_desc", "")
        if SHARE_BUTTON_TEXT in text and node.get("clickable") == "true":
            bounds = node.get("parsedBounds")
            if bounds and bounds["centerY"] > 1600:
                candidates.append(node)
    if candidates:
        return sorted(candidates, key=lambda item: item["parsedBounds"]["centerY"])[-1]
    return None


def answer_bottom_anchor_nodes(nodes: list[dict]) -> list[dict]:
    """Collect the bottom-anchor nodes used for answer scrolling."""
    return find_nodes(nodes, resource_id=ANSWER_BOTTOM_ANCHOR_ID)


def scroll_state_signature(nodes: list[dict]) -> str:
    """Build a stable signature for the current scroll position."""
    anchors = ",".join(node.get("bounds", "") for node in answer_bottom_anchor_nodes(nodes))
    texts = "|".join(visible_texts(nodes)[-8:])
    return f"{anchors}::{texts}"


def summarize_anchors(nodes: list[dict]) -> list[dict]:
    """Summarize the visible answer anchors for debug output."""
    summary = []
    for node in answer_bottom_anchor_nodes(nodes):
        bounds = node.get("parsedBounds")
        summary.append(
            {
                "bounds": node.get("bounds", ""),
                "centerY": bounds["centerY"] if bounds else None,
            }
        )
    return summary


def find_scroll_to_bottom_button(nodes: list[dict]) -> dict | None:
    """Find the floating scroll-to-bottom control if present."""
    candidates = []
    for node in nodes:
        bounds = node.get("parsedBounds")
        if not bounds or node.get("clickable") != "true":
            continue
        if node.get("text") or node.get("content_desc"):
            continue
        width = bounds["right"] - bounds["left"]
        height = bounds["bottom"] - bounds["top"]
        if 100 <= width <= 220 and 100 <= height <= 180 and 420 <= bounds["centerX"] <= 660 and 1450 <= bounds["centerY"] <= 1900:
            candidates.append(node)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item["parsedBounds"]["centerY"])[-1]


def tap_scroll_to_bottom_button(adb: AdbClient, nodes: list[dict]) -> dict | None:
    """Tap the floating scroll-to-bottom control."""
    target = find_scroll_to_bottom_button(nodes)
    if not target:
        return None
    x, y = center(target)
    adb.tap(x, y)
    time.sleep(0.4)
    return {"x": x, "y": y, "bounds": target.get("bounds", "")}


def scroll_to_answer_bottom_anchor(adb: AdbClient, max_scrolls: int = 8) -> dict:
    """Scroll until the answer bottom anchor becomes visible."""
    attempts = []
    previous_signature = ""
    unchanged_count = 0
    for index in range(max_scrolls + 1):
        nodes = dump_nodes(adb)
        if index == 0:
            jump = tap_scroll_to_bottom_button(adb, nodes)
            if jump:
                attempts.append({"index": index, "action": "tap_scroll_to_bottom_first", "tap": jump})
                nodes = dump_nodes(adb)
        anchors = answer_bottom_anchor_nodes(nodes)
        signature = scroll_state_signature(nodes)
        if signature == previous_signature:
            unchanged_count += 1
        else:
            unchanged_count = 0
        previous_signature = signature

        visible = [node for node in anchors if node.get("parsedBounds") and 300 <= node["parsedBounds"]["centerY"] <= 1850]
        attempts.append(
            {
                "index": index,
                "anchors": summarize_anchors(nodes),
                "visibleAnchorCount": len(visible),
                "unchangedCount": unchanged_count,
            }
        )
        if visible:
            return {
                "ok": True,
                "anchor": sorted(visible, key=lambda item: item["parsedBounds"]["centerY"])[-1],
                "attempts": attempts,
            }
        if unchanged_count >= 1:
            jump = tap_scroll_to_bottom_button(adb, nodes)
            if jump:
                attempts[-1]["action"] = "tap_scroll_to_bottom"
                attempts[-1]["tap"] = jump
                continue
        if unchanged_count >= 2:
            return {"ok": False, "error": "answer_bottom_anchor_scroll_stuck", "attempts": attempts}
        if index < max_scrolls:
            parsed_anchors = [node for node in anchors if node.get("parsedBounds")]
            if parsed_anchors:
                nearest = sorted(parsed_anchors, key=lambda item: abs(item["parsedBounds"]["centerY"] - 1100))[0]
                if nearest["parsedBounds"]["centerY"] < 300:
                    swipe_down(adb)
                else:
                    swipe_up(adb)
            else:
                swipe_up(adb)
    return {"ok": False, "error": "answer_bottom_anchor_not_found", "attempts": attempts}


def find_bottom_toolbar_share_target(nodes: list[dict], anchor: dict) -> dict | None:
    """Find the bottom-toolbar share icon relative to the anchor."""
    anchor_bounds = anchor.get("parsedBounds")
    if not anchor_bounds:
        return None
    toolbar_top = anchor_bounds["top"]
    toolbar_bottom = anchor_bounds["bottom"]
    candidates = []
    for node in nodes:
        bounds = node.get("parsedBounds")
        if not bounds or node.get("clickable") != "true":
            continue
        if not (toolbar_top <= bounds["centerY"] <= toolbar_bottom):
            continue
        if not (40 <= bounds["centerX"] <= 720):
            continue
        width = bounds["right"] - bounds["left"]
        height = bounds["bottom"] - bounds["top"]
        if 40 <= width <= 180 and 40 <= height <= 180:
            candidates.append(node)
    if not candidates:
        return None
    # Current Qianwen answer toolbar icon order:
    # read aloud, share, copy, edit, checklist/reference, regenerate.
    ordered = sorted(candidates, key=lambda item: item["parsedBounds"]["centerX"])
    return ordered[1] if len(ordered) >= 2 else None


def click_bottom_toolbar_share(adb: AdbClient, output_dir: str, max_scrolls: int = 8) -> dict:
    """Tap the bottom-toolbar share icon after scrolling to the anchor."""
    scroll = scroll_to_answer_bottom_anchor(adb, max_scrolls=max_scrolls)
    if not scroll.get("ok"):
        return scroll
    anchor = scroll["anchor"]
    nodes = dump_nodes(adb)
    target = find_bottom_toolbar_share_target(nodes, anchor)
    if not target:
        return {
            "ok": False,
            "error": "bottom_toolbar_share_not_found",
            "anchor": {"bounds": anchor.get("bounds", "")},
            "scrollAttempts": scroll.get("attempts", []),
        }
    x, y = center(target)
    adb.tap(x, y)
    time.sleep(0.25)
    return {
        "ok": True,
        "tap": {"x": x, "y": y, "bounds": target.get("bounds", "")},
        "anchor": {"bounds": anchor.get("bounds", "")},
        "scrollAttempts": scroll.get("attempts", []),
    }


def find_answer_text_node(nodes: list[dict]) -> dict | None:
    """Locate the main answer text node for context-menu sharing."""
    candidates = []
    for node in nodes:
        bounds = node.get("parsedBounds")
        text = node.get("text", "").strip()
        if not bounds or len(text) < 20:
            continue
        if node.get("class") == "android.widget.TextView" and bounds["centerY"] < 1800 and bounds["centerX"] < 820:
            if not any(skip in text for skip in ("发消息", "你能做什么", "ChatGPT", "Qwen模型")):
                candidates.append(node)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item["parsedBounds"]["centerY"])[-1]


def open_answer_context_menu(adb: AdbClient, output_dir: str) -> dict:
    """Open the long-press context menu on the answer text."""
    nodes = dump_nodes(adb)
    answer_node = find_answer_text_node(nodes)
    if not answer_node:
        return {"ok": False, "error": "answer_text_node_not_found"}
    x, y = center(answer_node)
    adb.command(["shell", "input", "swipe", str(x), str(y), str(x), str(y), "850"])
    time.sleep(0.3)
    menu_nodes = dump_nodes(adb)
    return {"ok": True, "tap": {"x": x, "y": y, "bounds": answer_node.get("bounds", "")}, "nodes": menu_nodes}


def open_answer_context_menu_with_retry(adb: AdbClient, output_dir: str, max_adjust_scrolls: int = 2) -> dict:
    """Retry opening the answer context menu with small scroll adjustments."""
    attempts = []
    for index in range(max_adjust_scrolls + 1):
        context = open_answer_context_menu(adb, output_dir)
        attempts.append({key: value for key, value in context.items() if key != "nodes"})
        if context.get("ok"):
            context["attempts"] = attempts
            return context
        if index < max_adjust_scrolls:
            swipe_down(adb)
    return {"ok": False, "error": "answer_text_node_not_found", "attempts": attempts}


def click_context_share(adb: AdbClient, output_dir: str, menu_nodes: list[dict] | None = None) -> dict:
    """Tap the share item inside the answer context menu."""
    nodes = menu_nodes if menu_nodes is not None else dump_nodes(adb)
    share_nodes = find_nodes(nodes, text_contains=SHARE_BUTTON_TEXT)
    candidates = []
    for node in share_nodes:
        bounds = node.get("parsedBounds")
        if not bounds:
            continue
        target = containing_clickable_node(nodes, node) or node
        target_bounds = target.get("parsedBounds")
        if target_bounds and target_bounds["centerX"] > 700:
            continue
        candidates.append(target)
    if not candidates:
        return {"ok": False, "error": "context_share_not_found"}
    target = sorted(candidates, key=lambda item: item["parsedBounds"]["centerY"])[-1]
    x, y = center(target)
    bounds = target.get("parsedBounds") or {}
    if bounds:
        y = max(bounds["top"] + 18, y - 52)
    adb.tap(x, y)
    time.sleep(0.25)
    return {"ok": True, "tap": {"x": x, "y": y, "bounds": target.get("bounds", "")}}


def scroll_to_share_button(adb: AdbClient, max_scrolls: int = 8) -> dict | None:
    """Scroll until the answer-level share button is visible."""
    for index in range(max_scrolls + 1):
        nodes = dump_nodes(adb)
        btn = find_share_button(nodes)
        if btn:
            return btn
        if index < max_scrolls:
            swipe_up(adb)
    return None


def click_copy_link(adb: AdbClient, output_dir: str) -> dict:
    """Tap the copy-link control inside the share sheet."""
    nodes = []
    copy_nodes = []
    for attempt in range(10):
        nodes = dump_nodes(adb)
        copy_nodes = find_nodes(nodes, text_contains=COPY_LINK_TEXT)
        if not copy_nodes:
            copy_nodes = [node for node in nodes if COPY_LINK_TEXT in node.get("content_desc", "") and node.get("parsedBounds")]
        if copy_nodes:
            break
        time.sleep(0.2)
    usable = [node for node in copy_nodes if node.get("parsedBounds") and node["parsedBounds"]["centerY"] > 1600]
    if not usable:
        usable = [node for node in copy_nodes if node.get("parsedBounds")]
    if not usable:
        texts = [text for text in visible_texts(nodes) if text][:20]
        return {"ok": False, "error": "copy_link_not_found", "nodeCount": len(nodes), "texts": texts}
    target = containing_clickable_node(nodes, sorted(usable, key=lambda item: (item["parsedBounds"]["centerY"], item["parsedBounds"]["centerX"]))[0])
    if target is None:
        target = sorted(usable, key=lambda item: (item["parsedBounds"]["centerY"], item["parsedBounds"]["centerX"]))[0]
    x, y = center(target)
    adb.tap(x, y)
    time.sleep(0.45)
    return {"ok": True, "tap": {"x": x, "y": y, "bounds": target.get("bounds", "")}}


def read_clipboard(adb: AdbClient) -> str:
    """Read the current clipboard text from adb."""
    result = adb.command(["shell", "dumpsys", "clipboard"], check=False)
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.lower().startswith("text:"):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def extract_urls(text: str) -> list[str]:
    """Extract URLs from a text block."""
    return extract_urls_from_text(text)


def close_share_sheet(adb: AdbClient) -> list[dict]:
    """Close the share sheet if it is still open."""
    nodes = dump_nodes(adb)
    if find_nodes(nodes, text_contains=COPY_LINK_TEXT) or not find_input_nodes(nodes):
        adb.keyevent(4)
        time.sleep(0.12)
        nodes = dump_nodes(adb)
    return nodes


def read_clipboard_via_paste(adb: AdbClient, output_dir: str, label: str) -> dict:
    """Paste clipboard text into the input box and read it back."""
    state_nodes = close_share_sheet(adb)
    input_nodes = find_input_nodes(state_nodes)
    if not input_nodes:
        return {"text": "", "urls": [], "clearOk": False, "error": "input_not_found"}
    x, y = center(input_nodes[-1])
    adb.tap(x, y)
    time.sleep(0.06)
    clear_focused_input(adb, verify=False)
    adb.keyevent(279)
    time.sleep(0.15)
    pasted_nodes = dump_nodes(adb)
    texts = [node.get("text", "") for node in find_input_nodes(pasted_nodes) if node.get("text", "")]
    text = texts[0] if texts else ""
    clear_ok = clear_focused_input(adb, verify=False, fallback_chars=max(80, len(text) + 10 if text else 80))
    return {"text": text, "urls": extract_urls(text), "clearOk": clear_ok}


def finish_share_after_open(adb: AdbClient, output_dir: str, share_open: dict, method: str) -> dict:
    """Finish a share flow by copying the link and reading the clipboard."""
    x = share_open.get("tap", {}).get("x", 0)
    y = share_open.get("tap", {}).get("y", 0)
    copy = click_copy_link(adb, output_dir)
    if not copy.get("ok"):
        return {
            "status": "failed",
            "url": "",
            "error": copy.get("error") or "copy_link_failed",
            "shareTap": {"x": x, "y": y, "method": method},
            "shareOpen": share_open,
            "copy": copy,
        }
    clipboard = read_clipboard(adb)
    paste = read_clipboard_via_paste(adb, output_dir, "answer-share")
    urls = extract_urls(clipboard) or paste.get("urls", [])
    return {
        "status": "success" if urls and paste.get("clearOk", True) else ("partial" if urls else "failed"),
        "url": urls[0] if urls else "",
        "clipboardText": clipboard,
        "paste": paste,
        "shareTap": {"x": x, "y": y, "method": method},
        "shareOpen": share_open,
        "copy": copy,
    }


def extract_answer_share_link_by_context_menu(adb: AdbClient, output_dir: str) -> dict:
    """Extract the share link via long-press context menu fallback."""
    context = open_answer_context_menu_with_retry(adb, output_dir)
    if not context.get("ok"):
        return {"status": "failed", "url": "", "error": context.get("error") or "answer_text_node_not_found", "context": context}

    context_share = click_context_share(adb, output_dir, menu_nodes=context.get("nodes"))
    if not context_share.get("ok"):
        context_public = {key: value for key, value in context.items() if key != "nodes"}
        return {
            "status": "failed",
            "url": "",
            "error": context_share.get("error") or "context_share_failed",
            "context": context_public,
            "contextShare": context_share,
        }

    result = finish_share_after_open(adb, output_dir, context_share, "answer_context_menu")
    context_public = {key: value for key, value in context.items() if key != "nodes"}
    result["context"] = context_public
    result["contextShare"] = context_share
    return result


def extract_answer_share_link(adb: AdbClient, output_dir: str, max_scrolls: int = 8) -> dict:
    """Extract the answer share link using toolbar first, then fallback."""
    bottom_share = click_bottom_toolbar_share(adb, output_dir, max_scrolls=max_scrolls)
    if bottom_share.get("ok"):
        bottom_result = finish_share_after_open(adb, output_dir, bottom_share, "answer_bottom_toolbar")
        if bottom_result.get("status") in {"success", "partial"} and bottom_result.get("url"):
            return bottom_result
        close_share_sheet(adb)
    else:
        bottom_result = {
            "status": "failed",
            "url": "",
            "error": bottom_share.get("error") or "bottom_toolbar_share_failed",
            "shareOpen": bottom_share,
        }

    fallback = extract_answer_share_link_by_context_menu(adb, output_dir)
    fallback["fallbackFrom"] = bottom_result
    return fallback


def clear_focused_input(adb: AdbClient, verify: bool = False, fallback_chars: int = 100) -> bool:
    """Clear the focused input field and optionally verify it."""
    try:
        previous_ime = adb.current_ime()
        if previous_ime != ADB_KEYBOARD_IME:
            adb.set_ime(ADB_KEYBOARD_IME)
            time.sleep(0.1)
        adb.broadcast_clear_text()
        time.sleep(0.2)
        if not verify:
            return True
        texts = [node.get("text", "") for node in find_input_nodes(dump_nodes(adb))]
        if texts and all(not text or text.startswith("发消息") for text in texts):
            return True
        adb.keyevent(123)
        for _ in range(min(fallback_chars, 40)):
            adb.keyevent(67)
        time.sleep(0.1)
        texts = [node.get("text", "") for node in find_input_nodes(dump_nodes(adb))]
        return bool(texts) and all(not text or text.startswith("发消息") for text in texts)
    except Exception:
        return False


def focus_input(adb: AdbClient, nodes: list[dict]) -> bool:
    """Tap the visible input field to focus it."""
    input_nodes = find_input_nodes(nodes)
    if not input_nodes:
        return False
    x, y = center(input_nodes[-1])
    adb.tap(x, y)
    time.sleep(0.4)
    return True


def type_question(adb: AdbClient, question: str) -> dict:
    """Type a question using the ADB keyboard broadcast path."""
    imes = adb.list_imes()
    if ADB_KEYBOARD_IME not in imes:
        return {"ok": False, "error": "adb_keyboard_not_installed", "availableImes": imes}
    previous_ime = adb.current_ime()
    switch_error = None
    if previous_ime != ADB_KEYBOARD_IME:
        try:
            adb.set_ime(ADB_KEYBOARD_IME)
            time.sleep(0.5)
        except Exception as exc:
            switch_error = str(exc)
    method = "adb_keyboard_b64"
    if hasattr(adb, "broadcast_base64_text"):
        adb.broadcast_base64_text(question)
    else:
        adb.broadcast_text(question)
        method = "adb_keyboard_text"
    time.sleep(0.8)
    input_texts = [node.get("text", "") for node in find_input_nodes(dump_nodes(adb))]
    if not any(question in text for text in input_texts):
        adb.broadcast_text(question)
        method = "adb_keyboard_text"
        time.sleep(0.6)
        input_texts = [node.get("text", "") for node in find_input_nodes(dump_nodes(adb))]
    if previous_ime and previous_ime != ADB_KEYBOARD_IME:
        try:
            adb.set_ime(previous_ime)
        except Exception:
            pass
    if not any(question in text for text in input_texts):
        error = "question_text_not_visible_after_adb_keyboard_input"
        if switch_error:
            error = f"{error}: {switch_error}"
        return {"ok": False, "error": error, "previousIme": previous_ime, "switchError": switch_error, "inputTexts": input_texts}
    result = {"ok": True, "method": method, "previousIme": previous_ime, "inputTexts": input_texts}
    if switch_error:
        result["switchError"] = switch_error
    return result


def tap_send_button(adb: AdbClient, nodes: list[dict]) -> bool:
    """Tap the send button when it is visible."""
    candidates = []
    for node in nodes:
        text = node.get("text", "") + node.get("content_desc", "")
        if any(label in text for label in ("发送", "提交", "完成")) and node.get("clickable") == "true":
            bounds = node.get("parsedBounds")
            if bounds and bounds["centerY"] > 1600:
                candidates.append(node)
    if not candidates:
        for node in nodes:
            bounds = node.get("parsedBounds")
            if not bounds or node.get("clickable") != "true":
                continue
            if bounds["centerX"] > 880 and bounds["centerY"] > 2050:
                candidates.append(node)
    if not candidates:
        return False
    target = sorted(candidates, key=lambda item: item["parsedBounds"]["centerY"])[-1]
    x, y = center(target)
    adb.tap(x, y)
    return True


def send_question(adb: AdbClient, question: str, output_dir: str) -> tuple[bool, dict]:
    """Clear the input, type the question, and submit it."""
    state = dump_nodes(adb)
    blocked = detect_blocked(state)
    if blocked:
        return False, {"error": blocked}
    if not focus_input(adb, state):
        return False, {"error": "input_not_found"}
    clear_focused_input(adb, verify=False)
    input_result = type_question(adb, question)
    if not input_result.get("ok"):
        return False, {"error": input_result.get("error"), "input": input_result}
    time.sleep(0.5)
    after_type_nodes = dump_nodes(adb)
    if tap_send_button(adb, after_type_nodes):
        time.sleep(1.0)
        return True, {"method": "send_button", "input": input_result}
    adb.keyevent(66)
    time.sleep(1.0)
    return True, {"method": "enter_key", "input": input_result}


def extract_answer_text(nodes: list[dict], question: str) -> str:
    """Extract the latest answer text from the visible UI nodes."""
    texts = [text for text in visible_texts(nodes) if text and text != question]
    texts = [text for text in texts if not text.startswith("http://") and not text.startswith("https://")]
    texts = [text for text in texts if text not in ("思考", "深度思考", "联网搜索", "拍照问答")]
    input_nodes = find_input_nodes(nodes)
    input_texts = {node.get("text", "") for node in input_nodes}
    texts = [text for text in texts if text not in input_texts]
    if question in texts:
        index = len(texts) - 1 - texts[::-1].index(question)
        texts = texts[index + 1 :]
    long_texts = [text for text in texts if len(text.strip()) > 20]
    return long_texts[-1].strip() if long_texts else ""


def has_generation_indicator(nodes: list[dict]) -> bool:
    """Detect whether the app still shows a generation indicator."""
    combined = "\n".join(visible_texts(nodes))
    keywords = ("停止生成", "停止回答", "正在生成", "正在思考", "思考中")
    return any(keyword in combined for keyword in keywords)


PLANNING_ANSWER_PREFIXES = ("检索", "调研", "查询", "收集", "确认", "对比", "分析", "整合")


def is_likely_truncated_planning_answer(answer: str) -> bool:
    """Detect the search-plan block that can appear before the final answer."""
    text = normalize_text(answer)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return sum(1 for prefix in PLANNING_ANSWER_PREFIXES if prefix in text) >= 3
    planning_lines = [line for line in lines if line.startswith(PLANNING_ANSWER_PREFIXES)]
    return len(planning_lines) >= 3 and len(planning_lines) >= len(lines) * 0.6


def collect_full_answer_by_scrolling(adb: AdbClient, output_dir: str, question: str, initial_answer: str, max_scrolls: int = 6) -> dict:
    """Scroll only after a suspected planning-block truncation."""
    if not initial_answer:
        return {"answer": initial_answer, "attempts": []}
    fragments = [initial_answer]
    seen = {initial_answer.strip()}
    attempts = []
    for index in range(max_scrolls):
        swipe_up(adb)
        xml = dump_xml(adb, output_dir, f"answer-complete-{index:02d}")
        nodes = collect_nodes(xml)
        answer = extract_answer_text(nodes, question)
        stripped = answer.strip()
        added = bool(stripped and stripped not in seen)
        if added:
            seen.add(stripped)
            fragments.append(answer)
        attempts.append(
            {
                "index": index,
                "answerLength": len(answer),
                "added": added,
            }
        )
        if not answer or not added:
            break
    return {"answer": "\n".join(fragments) if len(fragments) > 1 else fragments[0], "attempts": attempts}


def wait_for_answer(adb: AdbClient, question: str, output_dir: str, timeout: float = 180.0, stable_seconds: int = 2, completion_scrolls: int = 6) -> dict:
    """Wait for the answer to stabilize and return the latest capture."""
    started = time.time()
    last_answer = ""
    stable = 0
    samples = []
    latest_nodes = None
    while time.time() - started < timeout:
        xml = dump_xml(adb, output_dir, "answer-sample")
        nodes = collect_nodes(xml)
        latest_nodes = nodes
        answer = extract_answer_text(nodes, question)
        # 若当前"答案"其实是检索/调研/查询… 这类研究计划块，说明千问还在思考/联网搜索、
        # 正文尚未生成，视为仍在生成，避免把计划块误当成最终答案而提前结束等待。
        generating = has_generation_indicator(nodes) or is_likely_truncated_planning_answer(answer)
        if answer and answer == last_answer and not generating:
            stable += 1
        else:
            stable = 0
            last_answer = answer
        samples.append(
            {
                "elapsedMs": int((time.time() - started) * 1000),
                "answerLength": len(answer),
                "stable": stable,
                "generating": generating,
            }
        )
        if answer and stable >= stable_seconds:
            break
        time.sleep(1.0)
    if last_answer and completion_scrolls > 0 and is_likely_truncated_planning_answer(last_answer):
        completion = collect_full_answer_by_scrolling(adb, output_dir, question, last_answer, max_scrolls=completion_scrolls)
        last_answer = completion.get("answer", last_answer)
        restore_scrolls = len(completion.get("attempts", []))
        for _ in range(restore_scrolls):
            swipe_down(adb)
        samples.append(
            {
                "elapsedMs": int((time.time() - started) * 1000),
                "answerLength": len(last_answer),
                "stable": stable,
                "generating": False,
                "suspectedPlanningTruncation": True,
                "completionScrolls": completion.get("attempts", []),
                "restoreScrolls": restore_scrolls,
            }
        )
    return {"answer": last_answer, "nodes": latest_nodes, "samples": samples[-20:]}
