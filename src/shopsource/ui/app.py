from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from shopsource.classifier import classify_store
from shopsource.db import connect, init_db, upsert_store
from shopsource.exporter import export_store
from shopsource.importer import import_spark
from shopsource.connectors.spark_center import capability as spark_center_capability
from shopsource.connectors.spark_center_package import (
    SparkCenterPackageResult,
    SparkCenterPackageService,
    list_packages,
    mark_package,
)
from shopsource.connectors.spark_handoff import SparkHandoffConnector, SparkHandoffResult
from shopsource.paths import EXPORT_DIR, STORE_DIR
from shopsource.stats import master_summary, store_summary


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ShopSource Studio v0.1")
        self.geometry("1150x780")
        self.minsize(1000, 680)
        self.latest_package = None
        init_db()
        self._build()
        self._load_store_profiles()
        self.refresh()

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Store").pack(side="left")
        self.store_var = tk.StringVar(value="001")
        self.store_combo = ttk.Combobox(top, textvariable=self.store_var, width=28, state="readonly")
        self.store_combo.pack(side="left", padx=6)
        self.store_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(top, text="Spark ZIP/Storage Import", command=self.import_source).pack(side="left", padx=4)
        ttk.Button(top, text="Classify", command=self.classify).pack(side="left", padx=4)
        ttk.Button(top, text="Export CSV", command=lambda: self.export("csv")).pack(side="left", padx=4)
        ttk.Button(top, text="Export JSON", command=lambda: self.export("json")).pack(side="left", padx=4)
        ttk.Button(top, text="Spark Desktop Handoff...", command=self.spark_handoff).pack(side="left", padx=4)
        cap = spark_center_capability()
        self.spark_button = ttk.Button(top, text=f"Spark Center: {cap.status}", state="disabled")
        self.spark_button.pack(side="left", padx=4)

        status = ttk.LabelFrame(self, text="MASTER / Store Summary", padding=10)
        status.pack(fill="x", padx=10, pady=(0, 8))
        self.summary_text = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.summary_text, justify="left").pack(anchor="w")

        filters = ttk.Frame(self, padding=(10, 0))
        filters.pack(fill="x")
        ttk.Label(filters, text="Status filter").pack(side="left")
        self.status_var = tk.StringVar(value="ALL")
        self.status_combo = ttk.Combobox(filters, textvariable=self.status_var, width=20, state="readonly",
                                         values=["ALL","PRIMARY","RESERVE_A","RESERVE_B","RESERVE_C","LOW_RESERVE","HIGH_RESERVE","REVIEW","RESTRICTED","ARCHIVED"])
        self.status_combo.pack(side="left", padx=6)
        self.status_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_table())
        ttk.Button(filters, text="Refresh", command=self.refresh).pack(side="left", padx=4)

        package = ttk.LabelFrame(self, text="Spark Center manual upload package", padding=10)
        package.pack(fill="x", padx=10, pady=8)
        ttk.Label(package, text="Product count").grid(row=0, column=0, sticky="w")
        self.package_limit_var = tk.StringVar(value="50")
        ttk.Entry(package, textvariable=self.package_limit_var, width=8).grid(
            row=0, column=1, padx=6, sticky="w"
        )
        ttk.Button(
            package,
            text="Spark Center 업로드 폴더 만들기",
            command=self.create_spark_center_package,
        ).grid(row=0, column=2, padx=6, sticky="w")
        self.open_package_button = ttk.Button(
            package, text="폴더 열기", command=self.open_latest_package, state="disabled"
        )
        self.open_package_button.grid(row=0, column=3, padx=6)
        self.mark_uploaded_button = ttk.Button(
            package, text="업로드 완료 표시", command=self.mark_latest_uploaded, state="disabled"
        )
        self.mark_uploaded_button.grid(row=0, column=4, padx=6)
        self.package_info_var = tk.StringVar(value="Recent package: none")
        ttk.Label(
            package, textvariable=self.package_info_var, justify="left", wraplength=1050
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

        cols = ("asin","price","title","fit","price_status","risk","final")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=20)
        widths = {"asin":105,"price":70,"title":460,"fit":65,"price_status":115,"risk":100,"final":120}
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=8)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.msg = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.msg).pack(side="left", padx=10)

    def _load_store_profiles(self):
        profiles = []
        for p in sorted(STORE_DIR.glob("*.json")):
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
                upsert_store(obj)
                profiles.append((obj["store_id"], obj["store_name"]))
            except Exception:
                continue
        values = [f"{sid} | {name}" for sid, name in profiles]
        self.store_combo["values"] = values
        if values:
            self.store_var.set(values[0])

    def store_id(self) -> str:
        return self.store_var.get().split("|", 1)[0].strip() or "001"

    def _run(self, fn, on_done=None):
        self.progress.start(12)
        self.msg.set("Working...")
        def task():
            try:
                result = fn()
                self.after(0, lambda: (on_done or self._done)(result))
            except Exception as exc:
                self.after(0, lambda: self._error(exc))
        threading.Thread(target=task, daemon=True).start()

    def _done(self, result):
        self.progress.stop()
        self.msg.set(str(result)[:180])
        self.refresh()

    def _error(self, exc):
        self.progress.stop()
        self.msg.set("Error")
        messagebox.showerror("ShopSource Studio", str(exc))

    def import_source(self):
        path = filedialog.askopenfilename(title="Select Spark storage.zip", filetypes=[("ZIP", "*.zip"), ("All files", "*.*")])
        if not path:
            path = filedialog.askdirectory(title="Or select Spark storage directory")
        if not path:
            return
        self._run(lambda: import_spark(path))

    def classify(self):
        self._run(lambda: classify_store(self.store_id()))

    def export(self, fmt):
        sid = self.store_id()
        status = self.status_var.get()
        statuses = None if status == "ALL" else [status]
        ext = ".json" if fmt == "json" else ".csv"
        out = filedialog.asksaveasfilename(defaultextension=ext, filetypes=[(fmt.upper(), f"*{ext}")], initialfile=f"{sid}_products{ext}")
        if not out:
            return
        try:
            p = export_store(sid, fmt, statuses, out)
            messagebox.showinfo("Export complete", str(p))
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def spark_handoff(self):
        status = self.status_var.get()
        statuses = ["PRIMARY"] if status == "ALL" else [status]
        appdata = os.environ.get("APPDATA")
        spark_datasets = Path(appdata) / "spark" / "storage" / "datasets" if appdata else None
        default_jobs = EXPORT_DIR / "spark_handoff" / "jobs"
        initial = spark_datasets if spark_datasets and spark_datasets.is_dir() else default_jobs
        selected = filedialog.askdirectory(
            title="Select parent folder for the new Spark job",
            initialdir=str(initial),
            mustexist=False,
        )
        if not selected:
            return
        connector = SparkHandoffConnector()
        self._run(
            lambda: connector.export(
                store_id=self.store_id(), statuses=statuses, out_root=selected
            ),
            self._handoff_done,
        )

    def _handoff_done(self, result: SparkHandoffResult):
        self.progress.stop()
        self.msg.set(f"Spark handoff {result.validation_status}: {result.product_count} products")
        messagebox.showinfo(
            "Spark Handoff complete",
            f"Job ID: {result.job_id}\nProducts: {result.product_count}\n"
            f"Folder: {result.folder}\nValidation: {result.validation_status}",
        )
        self.refresh()

    def create_spark_center_package(self):
        try:
            limit = int(self.package_limit_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid product count", "Product count must be a whole number.")
            return
        if limit < 1:
            messagebox.showerror("Invalid product count", "Product count must be at least 1.")
            return
        status = self.status_var.get()
        statuses = ["PRIMARY"] if status == "ALL" else [status]
        service = SparkCenterPackageService()
        self._run(
            lambda: service.create(
                store_id=self.store_id(), statuses=statuses, limit=limit
            ),
            self._package_done,
        )

    def _package_done(self, result: SparkCenterPackageResult):
        self.progress.stop()
        self.latest_package = result.to_dict()
        self._show_latest_package()
        self.msg.set(f"Spark Center package {result.validation_status}: {result.product_count} products")
        messagebox.showinfo(
            "Spark Center upload folder ready",
            f"Store: {result.store_id} | {result.store_name}\n"
            f"Package ID: {result.package_id}\nProducts: {result.product_count}\n"
            f"Validation: {result.validation_status}\n\n"
            f"Spark Center에는 아래 폴더만 업로드하세요:\n{result.folder}",
        )
        self.refresh()

    def refresh_package_info(self):
        try:
            packages = list_packages(self.store_id(), 1)
            self.latest_package = packages[0] if packages else None
        except Exception:
            self.latest_package = None
        self._show_latest_package()

    def _show_latest_package(self):
        package = self.latest_package
        if not package:
            self.package_info_var.set("Recent package: none")
            self.open_package_button.configure(state="disabled")
            self.mark_uploaded_button.configure(state="disabled")
            return
        package_id = package.get("package_id", "")
        folder = package.get("folder") or package.get("output_path", "")
        status = package.get("package_status", "CREATED")
        validation = package.get("validation_status", "")
        count = package.get("product_count", 0)
        self.package_info_var.set(
            f"Recent: {package_id} | {count} products | Validation {validation} | Status {status}\n"
            f"Spark Center에는 이 폴더만 업로드: {folder}"
        )
        self.open_package_button.configure(state="normal" if folder else "disabled")
        self.mark_uploaded_button.configure(
            state="normal" if status == "CREATED" else "disabled"
        )

    def open_latest_package(self):
        package = self.latest_package or {}
        folder = Path(package.get("folder") or package.get("output_path", ""))
        if not folder.is_dir():
            messagebox.showerror("Folder unavailable", f"Package folder not found:\n{folder}")
            return
        os.startfile(str(folder))

    def mark_latest_uploaded(self):
        package = self.latest_package or {}
        package_id = package.get("package_id")
        if not package_id:
            return
        if not messagebox.askyesno(
            "Mark uploaded",
            "이 표시는 사용자가 Spark Center에 폴더를 업로드했다는 수동 기록입니다.\n"
            "포털 또는 Shopify 성공 검증을 의미하지 않습니다. 계속할까요?",
        ):
            return
        self._run(
            lambda: mark_package(package_id, "UPLOADED", "Marked uploaded in GUI"),
            self._package_marked,
        )

    def _package_marked(self, result):
        self.progress.stop()
        self.msg.set(f"Package marked {result['package_status']}")
        self.refresh_package_info()
        messagebox.showinfo(
            "Package status updated",
            "사용자 수동 업로드 기록을 저장했습니다. 포털/Shopify 성공 검증은 별도입니다.",
        )

    def refresh(self):
        try:
            master = master_summary()
            store = store_summary(self.store_id())
            self.summary_text.set(
                f"MASTER unique products: {master['unique_products']:,} | occurrences: {master['occurrences']:,} | repeats: {master['duplicates_or_repeats']:,} | Spark jobs: {master['spark_jobs']}\n"
                f"Store {self.store_id()}: {store['total']:,} classified | " + ", ".join(f"{k} {v:,}" for k, v in store['counts'].items())
            )
        except Exception as exc:
            self.summary_text.set(str(exc))
        self.refresh_table()
        self.refresh_package_info()

    def refresh_table(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        sid = self.store_id()
        status = self.status_var.get()
        where = "d.store_id=?"
        params = [sid]
        if status != "ALL":
            where += " AND d.final_status=?"
            params.append(status)
        sql = f"""
        SELECT p.asin,p.price,p.title,d.fit_score,d.price_status,d.risk_status,d.final_status
        FROM store_product_decisions d JOIN products p ON p.id=d.product_id
        WHERE {where} ORDER BY d.final_status,p.price LIMIT 500
        """
        try:
            with connect() as con:
                rows = con.execute(sql, params).fetchall()
            for r in rows:
                self.tree.insert("", "end", values=(r["asin"], r["price"], r["title"], r["fit_score"], r["price_status"], r["risk_status"], r["final_status"]))
        except Exception:
            pass


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
