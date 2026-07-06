#!/usr/bin/env python3
"""Standalone ad-hoc tool: root-pull Qianwen chat.db and print recent turns
(answer / 深度思考 / 来源 / 淘宝商品卡 with cleaned links).

Thin CLI over mobile_auto_qianwen.chatdb_extractor — the runner and this tool share
the exact same parser, so there is no field drift. Not part of the runner; use it to
inspect a device or a pulled db by hand.

Examples:
    # pull from a rooted device and list new turns since a baseline db
    python qw_pull_extract.py --serial 127.0.0.1:16416 --out cur.db --baseline base.db

    # just parse an already-pulled db
    python qw_pull_extract.py --db cur.db

    # extract one turn by its exact question text
    python qw_pull_extract.py --db cur.db --question "没有效果比较好，然后性价比也比较高的这个成长奶粉。"
"""
import argparse
import sqlite3
import sys
from pathlib import Path

# allow running from this directory: `python qw_pull_extract.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mobile_auto_qianwen.adb_client import AdbClient  # noqa: E402
from mobile_auto_qianwen.chatdb_extractor import (  # noqa: E402
    _extract_turn, _load, check_root, extract_for_question, pull_chat_db,
)


def _print_turn(t: dict) -> None:
    print(f"  [{t.get('mode')}] status={t.get('status')} "
          f"answer={len(t.get('answer', ''))}字 思考={len(t.get('thinking', ''))}字 "
          f"来源={len(t.get('sources', []))} 商品={len(t.get('products', []))}")
    for p in t.get("products", []):
        ad = "⚠广告位" if str(p.get("isAd")).lower() == "true" else "自然位"
        print(f"    * {p.get('title')}  ¥{p.get('price')}  {p.get('sales')}  [{ad}]")
        print(f"      {p.get('cleanUrl')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="Device serial; when set, pull chat.db before parsing.")
    ap.add_argument("--adb", help="adb.exe path (optional).")
    ap.add_argument("--out", default="chat.db", help="Where to pull the db to (with --serial).")
    ap.add_argument("--db", help="Parse an existing db file instead of pulling.")
    ap.add_argument("--baseline", help="Baseline db; only show turns whose req_id is NOT in it.")
    ap.add_argument("--question", help="Extract the single turn matching this exact question text.")
    args = ap.parse_args()

    if args.serial:
        adb = AdbClient(args.adb or AdbClient().adb, args.serial)
        if not check_root(adb):
            print(f"[!] {args.serial} is NOT rooted — chat.db mode unavailable.", file=sys.stderr)
            sys.exit(2)
        db_path = pull_chat_db(adb, args.out)
    elif args.db:
        db_path = args.db
    else:
        ap.error("provide --serial (to pull) or --db (to parse an existing file)")

    if args.question:
        t = extract_for_question(db_path, args.question)
        if not t:
            print("turn not found for that question")
            sys.exit(1)
        print(f"Q: {args.question}")
        _print_turn(t)
        return

    turns, order, meta = _load(db_path)
    base = set()
    if args.baseline and Path(args.baseline).exists():
        bc = sqlite3.connect(args.baseline)
        base = {r[0] for r in bc.execute("SELECT req_id FROM chat_request")}
        bc.close()
    new = [k for k in order if k not in base]
    print(f"NEW turns: {len(new)} (of {len(order)} total)")
    for k in new:
        t = _extract_turn(turns[k])
        q = t.get("query") or meta.get(k, {}).get("input")
        print(f"\n=== req={k[:12]}  Q: {q}")
        _print_turn(t)


if __name__ == "__main__":
    main()
