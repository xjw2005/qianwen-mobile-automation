import re
import time
from pathlib import Path

from .adb_client import AdbClient
from .app import center, clear_focused_input, dump_nodes, extract_answer_share_link, extract_urls, find_input_nodes, read_clipboard, swipe_down, swipe_up, tap_thinking_reference_trigger
from .artifacts import save_state
from .ocr import compact_text, ocr_screenshot
from .ui_xml import collect_nodes, find_nodes


SOURCE_HINTS = ("来源", "参考", "引用", "资料", "相关", "链接")
IGNORE_TEXT = {"分享", "复制链接", "返回", "更多", "思考", "查看全部", "发消息...", "发消息或按住说话..."}
DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
NUMBERED_TITLE_RE = re.compile(r"^\s*\d+[.、·]\s*")


def scroll_panel_down(adb: AdbClient) -> None:
    swipe_down(adb)


def scroll_panel_up(adb: AdbClient) -> None:
    swipe_up(adb)


def scroll_to_source_list_top(adb: AdbClient, rounds: int = 6) -> None:
    for _ in range(rounds):
        scroll_panel_up(adb)


def extract_visible_source_items(nodes: list[dict]) -> list[dict]:
    items = []
    for node in nodes:
        bounds = node.get("parsedBounds")
        if not bounds:
            continue
        text = (node.get("text", "") or node.get("content_desc", "")).strip()
        if not text or text in IGNORE_TEXT:
            continue
        if len(text) < 6:
            continue
        if bounds["centerY"] < 260 or bounds["centerY"] > 1980:
            continue
        if node.get("clickable") == "true" or node.get("resource_id"):
            items.append({
                "title": text,
                "centerX": bounds["centerX"],
                "centerY": bounds["centerY"],
                "bounds": node.get("bounds", ""),
                "resourceId": node.get("resource_id", ""),
            })
    unique = []
    seen = set()
    for item in items:
        key = (item["title"], item["centerX"], item["centerY"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def extract_ocr_source_items(ocr: dict) -> list[dict]:
    items = []
    for line in ocr.get("lines", []):
        title = (line.get("text") or "").strip()
        compact = compact_text(title)
        if not title or len(compact) < 6:
            continue
        if any(skip in compact for skip in ("参考", "资料", "深度思考", "用户要求", "关键点", "需要严格控制")):
            continue
        y = int(line.get("centerY") or 0)
        if y < 360 or y > 2050:
            continue
        items.append({
            "title": title,
            "centerX": int(line.get("centerX") or 540),
            "centerY": y,
            "bounds": f"ocr:[{line.get('left', 0)},{line.get('top', 0)}][{line.get('right', 0)},{line.get('bottom', 0)}]",
            "resourceId": "ocr",
        })
    return items


def extract_reference_panel_items(nodes: list[dict]) -> list[dict]:
    text_nodes = []
    for node in nodes:
        text = (node.get("text") or "").strip()
        bounds = node.get("parsedBounds")
        if text and bounds and bounds["centerY"] > 500:
            text_nodes.append(node)
    items = []
    index = 0
    while index < len(text_nodes):
        node = text_nodes[index]
        title = node.get("text", "").strip()
        if not NUMBERED_TITLE_RE.match(title):
            index += 1
            continue
        domain = ""
        if index + 1 < len(text_nodes):
            next_text = text_nodes[index + 1].get("text", "").strip()
            if DOMAIN_RE.match(next_text):
                domain = next_text
        bounds = node["parsedBounds"]
        items.append({
            "title": title,
            "domain": domain,
            "url": f"https://{domain}" if domain else "",
            "centerX": bounds["centerX"],
            "centerY": bounds["centerY"],
            "bounds": node.get("bounds", ""),
            "resourceId": "reference_panel",
        })
        index += 1
    return items


def find_source_trigger(nodes: list[dict]) -> dict | None:
    candidates = []
    for node in nodes:
        text = f"{node.get('text', '')}{node.get('content_desc', '')}"
        if "完成思考" in text or "正在思考" in text:
            continue
        if any(hint in text for hint in SOURCE_HINTS) and node.get("clickable") == "true":
            bounds = node.get("parsedBounds")
            if bounds and 260 <= bounds["centerY"] <= 1980:
                candidates.append(node)
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item["parsedBounds"]["centerY"])[0]


def expand_references_if_needed(adb: AdbClient, nodes: list[dict], output_dir: str) -> dict:
    trigger = find_source_trigger(nodes)
    if not trigger:
        return {"expanded": False, "reason": "source_trigger_not_found", "items": []}

    x, y = center(trigger)
    adb.tap(x, y)
    time.sleep(0.5)
    expanded = save_state(adb, output_dir, "sources-after-trigger-tap")
    items = extract_visible_source_items(expanded["nodes"])
    if items:
        return {"expanded": True, "reason": "expanded_after_trigger", "items": items, "state": expanded}

    for _ in range(5):
        scroll_panel_up(adb)
    scrolled = save_state(adb, output_dir, "sources-after-scroll-top")
    items = extract_visible_source_items(scrolled["nodes"])
    return {"expanded": bool(items), "reason": "scrolled_to_top", "items": items, "state": scrolled}


def find_source_by_title_realtime(nodes: list[dict], target_title: str) -> dict | None:
    target = target_title.strip()
    current_items = extract_visible_source_items(nodes)
    for item in current_items:
        title = item["title"].strip()
        if title == target:
            return item
    for item in current_items:
        title = item["title"].strip()
        if target and title and (target in title or title in target):
            return item
    return None


def locate_source_realtime(adb: AdbClient, target_title: str, max_scroll_attempts: int = 20) -> dict | None:
    for _ in range(max_scroll_attempts + 1):
        nodes = dump_nodes(adb)
        item = find_source_by_title_realtime(nodes, target_title)
        if item:
            return item
        scroll_panel_down(adb)
    return None


def read_copied_urls(adb: AdbClient, output_dir: str, label: str) -> dict:
    direct_text = read_clipboard(adb)
    direct_urls = extract_urls(direct_text)
    if direct_urls:
        return {"urls": list(dict.fromkeys(direct_urls)), "state": None, "clear": {"verified": True}, "text": direct_text, "method": "direct_clipboard"}
    state = save_state(adb, output_dir, f"{label}-before-paste")
    input_nodes = find_input_nodes(state["nodes"])
    if not input_nodes:
        return {"urls": [], "state": state, "clear": {"verified": False}, "text": "", "error": "input_not_found", "method": "paste_input"}
    x, y = center(input_nodes[-1])
    adb.tap(x, y)
    time.sleep(0.15)
    clear_focused_input(adb, verify=False)
    adb.keyevent(279)
    time.sleep(0.3)
    pasted = save_state(adb, output_dir, f"{label}-after-paste")
    pasted_text = ""
    urls = []
    for node in find_input_nodes(pasted["nodes"]):
        text = node.get("text", "")
        if text:
            pasted_text = text
        urls.extend(extract_urls(text))
    clear_ok = clear_focused_input(adb, verify=True, fallback_chars=max(80, len(pasted_text) + 10 if pasted_text else 80))
    return {"urls": list(dict.fromkeys(urls)), "state": pasted, "clear": {"verified": clear_ok}, "text": pasted_text, "method": "paste_input"}


def collect_sources_across_scroll(adb: AdbClient, output_dir: str, max_rounds: int = 5) -> dict:
    seen_titles = set()
    collected_items = []
    no_new_count = 0
    for _ in range(max_rounds):
        nodes = dump_nodes(adb)
        new_count = 0
        for item in extract_visible_source_items(nodes):
            title = item["title"].strip()
            if title and title not in seen_titles:
                seen_titles.add(title)
                collected_items.append(item)
                new_count += 1
        if new_count:
            no_new_count = 0
        else:
            no_new_count += 1
            if no_new_count >= 2:
                break
        scroll_panel_down(adb)
    state = save_state(adb, output_dir, "sources-after-panel-scroll")
    return {"items": collected_items, "state": state}


def scroll_to_source_top_if_needed(adb: AdbClient, items: list[dict]) -> None:
    if items:
        scroll_to_source_list_top(adb)


def resolve_source_limit(source_limit, item_count: int) -> int:
    if isinstance(source_limit, str) and source_limit.lower() == "all":
        return item_count
    return min(int(source_limit), item_count)


def extract_sources(adb: AdbClient, options: dict, output_dir: str) -> dict:
    initial = save_state(adb, output_dir, "sources-initial")
    clear_focused_input(adb, verify=True)
    initial = save_state(adb, output_dir, "sources-after-input-clear")
    expand = expand_references_if_needed(adb, initial["nodes"], output_dir)
    items = expand["items"]
    limit = resolve_source_limit(options.get("sourceLimit", 5), len(items))
    sources = []
    if items:
        scroll_to_source_list_top(adb)
    for index, item in enumerate(items[:limit], start=1):
        realtime_item = locate_source_realtime(adb, item["title"], max_scroll_attempts=20)
        if not realtime_item:
            sources.append({
                "index": index,
                "title": item["title"],
                "url": "",
                "method": "share_copy_paste_read",
                "status": "failed",
                "error": "source_not_found_after_scroll",
                "debug": {},
            })
            continue
        source = {
            "index": index,
            "title": realtime_item["title"],
            "url": "",
            "method": "share_copy_paste_read",
            "status": "pending",
            "error": None,
            "debug": {"sourceTap": {"x": realtime_item["centerX"], "y": realtime_item["centerY"], "bounds": realtime_item["bounds"]}},
        }
        try:
            adb.tap(realtime_item["centerX"], realtime_item["centerY"])
            time.sleep(float(options.get("sourcePageWaitSeconds", 0.35)))
            source_page = save_state(adb, output_dir, f"source-{index}-page")
            share = extract_answer_share_link(adb, output_dir, max_scrolls=int(options.get("answerShareMaxScrolls", 8)))
            if share.get("url"):
                source.update({"url": share["url"], "status": "success"})
            else:
                source.update({"status": "failed", "error": share.get("error") or "no_url_after_share"})
            source["debug"].update({
                "sourcePageXml": source_page["xml"],
                "shareCapture": {key: value for key, value in share.items() if key != "clipboardText"},
            })
            adb.keyevent(4)
            time.sleep(0.25)
        except Exception as exc:
            source.update({"status": "failed", "error": str(exc)})
            adb.keyevent(4)
            time.sleep(0.25)
        sources.append(source)
    return {"sources": sources, "visibleSourceCount": len(items), "attemptedCount": limit, "referenceExpansion": {key: value for key, value in expand.items() if key not in {"items", "state"}}}


def extract_sources_from_thinking_detail(adb: AdbClient, options: dict, output_dir: str) -> dict:
    for _ in range(3):
        scroll_panel_down(adb)
    expand = tap_thinking_reference_trigger(adb, output_dir)
    state = save_state(adb, output_dir, "thinking-sources-after-trigger")
    items = extract_reference_panel_items(state["nodes"])
    ocr = ocr_screenshot(adb, output_dir, "thinking-sources-after-trigger")
    if not items:
        items = extract_ocr_source_items(ocr)
    limit = resolve_source_limit(options.get("sourceLimit", 5), len(items))
    sources = []
    from .ui_xml import visible_texts

    for index, item in enumerate(items[:limit], start=1):
        domain_url = item.get("url", "")
        source_tap = {"x": item["centerX"], "y": item["centerY"], "bounds": item["bounds"]}

        # —— 点击来源条目，进入内嵌来源页面 ——
        adb.tap(item["centerX"], item["centerY"])
        time.sleep(float(options.get("sourcePageWaitSeconds", 0.5)))

        # —— 尝试用分享复制拿完整 URL（和快速模式同一套逻辑）——
        share = extract_answer_share_link(adb, output_dir, max_scrolls=int(options.get("answerShareMaxScrolls", 4)))
        share_url = share.get("url", "")

        # —— 分享失败 → 从页面文本中提取 URL ——
        full_url = ""
        method = "thinking_source_page_share"
        if share_url and "qianwen.com" not in share_url:
            full_url = share_url
        else:
            source_page = save_state(adb, output_dir, f"thinking-source-{index}-page")
            all_text = "\n".join(visible_texts(source_page["nodes"]))
            page_urls = [u for u in extract_urls(all_text) if "qianwen.com" not in u]
            if page_urls:
                full_url = page_urls[0]
                method = "thinking_source_page_text_extract"

        # —— 都没拿到 → 回退到域名 ——
        if full_url:
            url = full_url
            status = "success"
        elif domain_url:
            url = domain_url
            method = "thinking_reference_panel_domain"
            status = "partial"
        else:
            url = ""
            method = "thinking_reference_panel_domain"
            status = "failed"

        sources.append({
            "index": index,
            "title": item["title"],
            "url": url,
            "method": method,
            "status": status,
            "error": None if url else ("full_url_not_found" if full_url else "domain_not_found"),
            "debug": {
                "sourceTap": source_tap,
                "domain": item.get("domain", ""),
            },
        })

        # —— 返回思考详情页 ——
        adb.keyevent(4)
        time.sleep(0.4)

        # —— 重新打开来源面板（为下一个来源做准备）——
        if index < limit:
            tap_thinking_reference_trigger(adb, output_dir)

    return {
        "sources": sources,
        "visibleSourceCount": len(items),
        "attemptedCount": limit,
        "referenceExpansion": {key: value for key, value in expand.items() if key != "ocr"},
        "ocr": {key: ocr.get(key) for key in ("ok", "screenshot", "error")},
    }
