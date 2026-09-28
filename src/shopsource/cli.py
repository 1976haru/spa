from __future__ import annotations

import argparse
import json
from pathlib import Path

from .classifier import classify_store, manual_override
from .db import init_db, upsert_store
from .exporter import export_store
from .importer import import_spark
from .stats import master_summary, store_summary


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="shopsource", description="ShopSource Studio v0.1")
    parser.add_argument("--db", default=None, help="SQLite DB path (default: data/shopsource.sqlite3)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")

    p = sub.add_parser("add-store")
    p.add_argument("profile")

    p = sub.add_parser("import-spark")
    p.add_argument("source", help="Spark storage directory or storage.zip")
    p.add_argument("--allow-reimport", action="store_true")

    p = sub.add_parser("classify")
    p.add_argument("--store", required=True)

    p = sub.add_parser("summary")
    p.add_argument("--store")

    p = sub.add_parser("override")
    p.add_argument("--store", required=True)
    p.add_argument("--asin", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--memo", default="")

    p = sub.add_parser("export")
    p.add_argument("--store", required=True)
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--status", action="append", dest="statuses")
    p.add_argument("--out")

    args = parser.parse_args(argv)
    db = args.db

    if args.cmd == "init-db":
        print(init_db(db))
    elif args.cmd == "add-store":
        init_db(db)
        profile = load_json(args.profile)
        upsert_store(profile, db)
        print(json.dumps({"added": profile["store_id"], "name": profile["store_name"]}, ensure_ascii=False))
    elif args.cmd == "import-spark":
        result = import_spark(args.source, db, args.allow_reimport)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "classify":
        print(json.dumps(classify_store(args.store, db), ensure_ascii=False, indent=2))
    elif args.cmd == "summary":
        result = store_summary(args.store, db) if args.store else master_summary(db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "override":
        manual_override(args.store, args.asin, args.status, args.memo, db)
        print(json.dumps({"ok": True, "store": args.store, "asin": args.asin, "status": args.status.upper()}, ensure_ascii=False))
    elif args.cmd == "export":
        target = export_store(args.store, args.format, args.statuses, args.out, db)
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
