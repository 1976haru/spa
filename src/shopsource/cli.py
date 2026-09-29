from __future__ import annotations

import argparse
import json
from pathlib import Path

from .classifier import classify_store, clear_manual_override, manual_override
from .connectors.spark_handoff import SparkHandoffConnector
from .connectors.spark_center_package import (
    SparkCenterPackageService,
    list_packages,
    mark_package,
)
from .db import init_db, upsert_store
from .exporter import export_store
from .importer import import_amazon_source, import_spark
from .stats import master_summary, store_summary
from .schema_probe import probe_schema
from .sourcing.engine import SourcingEngine


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

    p = sub.add_parser("import-amazon-source", help="Import product JSON from the local source inbox")
    p.add_argument("source", nargs="?", help="JSON file/folder (default: source/amazon/inbox)")
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

    p = sub.add_parser("clear-override")
    p.add_argument("--store", required=True)
    p.add_argument("--asin", required=True)

    p = sub.add_parser("probe-schema")
    p.add_argument("source", help="Read-only JSON, folder, or ZIP input")

    p = sub.add_parser("spark-handoff", help="Create a verified Spark datasets job folder")
    p.add_argument("--store", required=True)
    p.add_argument("--status", action="append", dest="statuses")
    p.add_argument("--limit", type=int)
    p.add_argument("--asin", action="append", dest="asins")
    p.add_argument("--out-root", help="Parent folder where the new Spark job folder is created")
    p.add_argument("--job-id")
    p.add_argument("--allow-restricted", action="store_true")

    p = sub.add_parser("spark-center-package", help="Create a local Spark Center upload package")
    p.add_argument("--store", required=True)
    p.add_argument("--status", action="append", dest="statuses")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--asin", action="append", dest="asins")
    p.add_argument("--out-root", help="Package root (default: project exports/spark_center)")
    p.add_argument("--package-id")
    p.add_argument("--allow-restricted", action="store_true")

    p = sub.add_parser("package-list", help="List recent Spark Center manual packages")
    p.add_argument("--store")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("package-mark", help="Manually update a Spark Center package status")
    p.add_argument("--package-id", required=True)
    p.add_argument("--status", required=True, choices=["CREATED", "UPLOADED", "FAILED", "ARCHIVED"])
    p.add_argument("--note", default="")

    p = sub.add_parser("export")
    p.add_argument("--store", required=True)
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--status", action="append", dest="statuses")
    p.add_argument("--out")

    p = sub.add_parser("source-auto", help="Preview or run provider-based candidate sourcing")
    p.add_argument("--store", required=True)
    p.add_argument("--target", type=int, default=5)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-tokens", type=int, default=1000)
    p.add_argument("--min-token-reserve", type=int, default=100)

    p = sub.add_parser("source-status", help="Show automated sourcing run status")
    p.add_argument("--run", required=True)

    p = sub.add_parser("source-resume", help="Resume a paused automated sourcing run")
    p.add_argument("--run", required=True)

    p = sub.add_parser("source-cancel", help="Cancel an automated sourcing run")
    p.add_argument("--run", required=True)

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
    elif args.cmd == "import-amazon-source":
        result = import_amazon_source(args.source, db, args.allow_reimport)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "classify":
        print(json.dumps(classify_store(args.store, db), ensure_ascii=False, indent=2))
    elif args.cmd == "summary":
        result = store_summary(args.store, db) if args.store else master_summary(db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "override":
        manual_override(args.store, args.asin, args.status, args.memo, db)
        print(json.dumps({"ok": True, "store": args.store, "asin": args.asin, "status": args.status.upper()}, ensure_ascii=False))
    elif args.cmd == "clear-override":
        clear_manual_override(args.store, args.asin, db)
        print(json.dumps({"ok": True, "store": args.store, "asin": args.asin, "manual_override": False}, ensure_ascii=False))
    elif args.cmd == "probe-schema":
        print(json.dumps(probe_schema(args.source), ensure_ascii=False, indent=2))
    elif args.cmd == "spark-handoff":
        result = SparkHandoffConnector().export(
            store_id=args.store,
            statuses=args.statuses,
            limit=args.limit,
            asins=args.asins,
            out_root=args.out_root,
            job_id=args.job_id,
            db=db,
            allow_restricted=args.allow_restricted,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif args.cmd == "spark-center-package":
        result = SparkCenterPackageService().create(
            store_id=args.store,
            statuses=args.statuses,
            limit=args.limit,
            asins=args.asins,
            out_root=args.out_root,
            package_id=args.package_id,
            db=db,
            allow_restricted=args.allow_restricted,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif args.cmd == "package-list":
        print(json.dumps(list_packages(args.store, args.limit, db), ensure_ascii=False, indent=2))
    elif args.cmd == "package-mark":
        print(json.dumps(
            mark_package(args.package_id, args.status, args.note, db),
            ensure_ascii=False,
            indent=2,
        ))
    elif args.cmd == "export":
        target = export_store(args.store, args.format, args.statuses, args.out, db)
        print(target)
    elif args.cmd == "source-auto":
        engine = SourcingEngine()
        result = (engine.preview(args.store, args.target, db) if args.dry_run else
                  engine.run(args.store, args.target, db=db,
                             max_tokens_per_run=args.max_tokens,
                             min_tokens_reserve=args.min_token_reserve))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "source-status":
        print(json.dumps(SourcingEngine.status(args.run, db), ensure_ascii=False, indent=2))
    elif args.cmd == "source-resume":
        print(json.dumps(SourcingEngine().resume(args.run, db), ensure_ascii=False, indent=2))
    elif args.cmd == "source-cancel":
        print(json.dumps(SourcingEngine.cancel(args.run, db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
