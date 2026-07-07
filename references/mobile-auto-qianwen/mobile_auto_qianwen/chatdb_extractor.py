"""Local chat.db capture path (default mode).

Qianwen 对「带货回答」刻意不渲染分享按钮 → 无 share_id → share/info 拿不到 answer/思考/来源，
更拿不到淘宝商品卡。但 App 把完整会话（含商品卡 / plan_cot / web 来源）落盘在本地 SQLite：

    /data/data/com.aliyun.tongyi/databases/chat.db

有 root 时直接 root 拉库解析即可，**不依赖分享按钮、不依赖 MITM、不依赖 Chrome CDP**，
且比 share/info 拿到的更多（带货 turn 在 share/info 里根本不存在）。

调用方（runner）先用 `check_root()` 判断当前设备能否走本模式；不能则回退到 link-only / 手机端捕获。

chat.db 结构（每个回答 turn = 一个 req_id，按 mime_type 拆成多行 chat_message）：
    signal/post        meta.ori_query                              用户问题（兜底）
    plan_cot/post      content                                     深度思考正文
    multi_load/iframe  content                                     回答 markdown（带 [(...)] 占位）
    multi_load/iframe  meta.multi_load[].content.cardData          淘宝商品卡（dinamicx）
    bar/iframe         meta.sources[].content.list[]               web 来源（格式A）
    chat_request.local_origin_input_text                           用户原话（首选 query 来源）
    chat_request.local_request_status == 'complete'                该 turn 是否生成完毕
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .adb_client import AdbClient, AdbError
from .constants import QIANWEN_PACKAGE

CHAT_DB_PATH = f"/data/data/{QIANWEN_PACKAGE}/databases/chat.db"
_REMOTE_TMP = "/sdcard/_qw_chatdb_pull.db"


# --------------------------------------------------------------------------- #
# root / pull
# --------------------------------------------------------------------------- #
def check_root(adb: AdbClient) -> bool:
    """Return True iff `su -c id` yields uid=0 on the device."""
    try:
        res = adb.command(["shell", "su -c id"], check=False)
    except AdbError:
        return False
    out = (res.stdout or "") + (res.stderr or "")
    return "uid=0" in out


def pull_chat_db(adb: AdbClient, dest: str | Path) -> str:
    """Root-copy chat.db to /sdcard then adb pull to `dest`. Raises AdbError on failure."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    adb.command(["shell", f"su -c 'cp {CHAT_DB_PATH} {_REMOTE_TMP}'"])
    pull = adb.command(["pull", _REMOTE_TMP, str(dest)], check=False)
    adb.command(["shell", f"rm {_REMOTE_TMP}"], check=False)
    if pull.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise AdbError(f"chat.db pull failed: {pull.stderr or pull.stdout}")
    return str(dest)


# --------------------------------------------------------------------------- #
# parsing (shared with the standalone qw_pull_extract.py tool)
# --------------------------------------------------------------------------- #
def _j(x):
    if isinstance(x, (dict, list)):
        return x
    if not x:
        return {}
    try:
        return json.loads(x)
    except (ValueError, TypeError):
        return {}


def _price_of(item: dict):
    """Displayed price lives in priceShow.price; `price` is the strike-through original."""
    ps = item.get("priceShow")
    if isinstance(ps, dict):
        return ps.get("price")
    if isinstance(ps, str) and ps:
        return ps
    p = item.get("price")
    return p.get("price") if isinstance(p, dict) else p


def _find_cate_title(card_data) -> str | None:
    box: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("cateTitle") and not box:
                box.append(o["cateTitle"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(card_data)
    return box[0] if box else None


def _extract_products(card_content: dict) -> list[dict]:
    """Walk cardData for any node with item_id+title. Handles taoassistant_single_product
    (cardData.data), _fold_product_feeds (cardData.data.items[]), and band (transItems)."""
    cd = _j(card_content.get("cardData", {}))
    cate = _find_cate_title(cd)
    out: list[dict] = []

    def is_item(o: dict) -> bool:
        keys = {k.lower() for k in o}
        return ("item_id" in keys or "itemid" in keys) and any("title" in k for k in keys)

    def walk(o):
        if isinstance(o, dict):
            if is_item(o):
                iid = o.get("item_id") or o.get("itemId")
                if iid:
                    shop = o.get("shopInfo")
                    out.append({
                        "cateTitle": cate,
                        "title": o.get("title") or o.get("listTwoLineTitle"),
                        "price": _price_of(o),
                        "sales": o.get("realSales"),
                        "itemId": str(iid),
                        "shop": shop.get("title") if isinstance(shop, dict) else None,
                        "isAd": o.get("isP4p"),
                        "image": o.get("pict_url") or o.get("pic_path"),
                        "cleanUrl": f"https://item.taobao.com/item.htm?id={iid}",
                        "originUrl": o.get("auctionURL") or o.get("detailBaseUrl"),
                    })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(cd)
    return out


def _norm_source(d: dict) -> dict | None:
    if not isinstance(d, dict):
        return None
    return {"title": d.get("title"), "url": d.get("url"), "snippet": (d.get("summary") or "")[:200]}


def _extract_turn(rows: list[sqlite3.Row]) -> dict:
    """rows = all chat_message rows for one req_id."""
    query = None
    answer_parts: list[str] = []
    thinking_parts: list[str] = []
    products: list[dict] = []
    sources: list[dict] = []
    videos: list[dict] = []

    for r in rows:
        mime = r["mime_type"] or ""
        content = r["content"] or ""
        meta = _j(r["meta_data"])
        if mime == "signal/post" and not query:
            query = meta.get("ori_query")
        elif mime.startswith("plan_cot") or "cot" in mime:
            if content.strip():
                thinking_parts.append(content)
        elif mime == "multi_load/iframe":
            if content.strip():
                answer_parts.append(content)
            for blk in meta.get("multi_load") or []:
                if not isinstance(blk, dict):
                    continue
                c = blk.get("content")
                t = blk.get("type")
                if isinstance(c, dict) and "product" in (c.get("cardType") or ""):
                    products += _extract_products(c)
                if t == "source_group_web" and isinstance(c, dict):
                    for g in c.get("list") or []:
                        for d in (g.get("content") or {}).get("list") or []:
                            s = _norm_source(d)
                            if s:
                                sources.append(s)
                if t == "video_note_list" and isinstance(c, dict):
                    for v in c.get("list") or []:
                        videos.append({"title": v.get("title"), "author": v.get("author"), "url": v.get("url")})
        elif mime == "bar/iframe":
            for s in meta.get("sources") or []:
                c = s.get("content") if isinstance(s, dict) else None
                if isinstance(c, dict):
                    for d in c.get("list") or []:
                        x = _norm_source(d)
                        if x:
                            sources.append(x)

    # dedup
    seen_src, srcs = set(), []
    for s in sources:
        u = s.get("url")
        if u and u in seen_src:
            continue
        seen_src.add(u)
        srcs.append(s)
    seen_p, prods = set(), []
    for p in products:
        if p["itemId"] in seen_p:
            continue
        seen_p.add(p["itemId"])
        prods.append(p)

    answer = max(answer_parts, key=len).strip() if answer_parts else ""
    thinking = "\n".join(thinking_parts).strip()
    mode = ("带货版" if prods and not thinking
            else "深度思考版" if thinking and not prods
            else "混合" if prods and thinking
            else "普通")
    return {"query": query, "mode": mode, "answer": answer, "thinking": thinking,
            "products": prods, "sources": srcs, "videos": videos}


def _load(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    turns: dict[str, list] = {}
    order: list[str] = []
    for r in cur.execute(
        "SELECT session_id, req_id, mime_type, content, meta_data FROM chat_message ORDER BY rowid"
    ):
        k = r["req_id"]
        if k not in turns:
            turns[k] = []
            order.append(k)
        turns[k].append(r)
    meta = {}
    for r in cur.execute(
        "SELECT req_id, local_request_status, local_origin_input_text, create_timestamp FROM chat_request"
    ):
        meta[r["req_id"]] = {
            "status": r["local_request_status"],
            "input": r["local_origin_input_text"],
            "ts": r["create_timestamp"] or 0,
        }
    con.close()
    return turns, order, meta


def extract_for_question(db_path: str, question: str) -> dict | None:
    """Find the turn matching `question` (by local_origin_input_text, else newest complete)
    and return its parsed content. Returns None if no matching turn exists yet."""
    turns, order, meta = _load(db_path)
    # 1) exact input-text match, newest first
    candidates = [k for k in order if (meta.get(k, {}).get("input") or "").strip() == (question or "").strip()]
    candidates.sort(key=lambda k: meta.get(k, {}).get("ts", 0))
    req = candidates[-1] if candidates else None
    if req is None:
        return None
    parsed = _extract_turn(turns[req])
    if not parsed["query"]:
        parsed["query"] = meta.get(req, {}).get("input")
    parsed["reqId"] = req
    parsed["status"] = meta.get(req, {}).get("status")
    return parsed


# --------------------------------------------------------------------------- #
# high-level capture (used by the runner)
# --------------------------------------------------------------------------- #
def capture_via_db(adb: AdbClient, question: str, output_dir: str,
                   poll_attempts: int = 6, poll_interval: float = 2.0) -> dict:
    """Pull chat.db and extract the just-asked turn. Polls until the turn is `complete`.

    Returns {status, answer, thinking, products, sources, videos, reqId, dbPath, error}.
    status: 'success' (answer found & complete) | 'partial' (answer but not complete) | 'failed'.
    """
    db_path = str(Path(output_dir) / "chat.db")
    last: dict | None = None
    err = None
    for attempt in range(poll_attempts):
        try:
            pull_chat_db(adb, db_path)
        except AdbError as exc:
            err = str(exc)
            time.sleep(poll_interval)
            continue
        parsed = extract_for_question(db_path, question)
        if parsed:
            last = parsed
            if parsed.get("status") == "complete" and parsed.get("answer"):
                parsed["dbStatus"] = "success"
                parsed["error"] = None
                return parsed
        time.sleep(poll_interval)
    if last:
        last["dbStatus"] = "partial" if last.get("answer") else "failed"
        last["error"] = None if last.get("answer") else "turn_incomplete"
        return last
    return {"dbStatus": "failed", "answer": "", "thinking": "", "products": [],
            "sources": [], "videos": [], "reqId": None, "error": err or "turn_not_found_in_db"}
