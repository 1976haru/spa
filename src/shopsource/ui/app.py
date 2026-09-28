from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from shopsource.classifier import classify_store
from shopsource.db import connect, init_db, upsert_store
from shopsource.exporter import export_store
from shopsource.importer import import_spark
from shopsource.connectors.spark_center import capability as spark_center_capability
from shopsource.paths import STORE_DIR
from shopsource.stats import master_summary, store_summary


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ShopSource Studio v0.1")
        self.geometry("1050x680")
        self.minsize(900, 600)
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

    def _run(self, fn):
        self.progress.start(12)
        self.msg.set("Working...")
        def task():
            try:
                result = fn()
                self.after(0, lambda: self._done(result))
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
