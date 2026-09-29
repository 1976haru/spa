from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from shopsource.classifier import classify_store
from shopsource.db import init_db, upsert_store
from shopsource.exporter import export_store
from shopsource.importer import import_amazon_source, import_spark
from shopsource.connectors.spark_center_package import (
    SparkCenterPackageResult,
    SparkCenterPackageService,
    list_packages,
    mark_package,
)
from shopsource.connectors.spark_handoff import SparkHandoffConnector, SparkHandoffResult
from shopsource.paths import AMAZON_INBOX_DIR, EXPORT_DIR, STORE_DIR, ensure_dirs
from shopsource.stats import master_summary, store_summary
from shopsource.sourcing.engine import SourcingEngine, new_run_id
from shopsource.sourcing.providers.keepa import KeepaProvider


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ShopSource Studio v0.1")
        self.geometry("980x650")
        self.minsize(900, 600)
        self.latest_package = None
        self.current_sourcing_run = None
        self.keepa_session_key = None
        init_db()
        self._build()
        self._load_store_profiles()
        self.refresh()

    def _build(self):
        menu = tk.Menu(self)
        advanced = tk.Menu(menu, tearoff=False)
        advanced.add_command(label="현재 Store 다시 분류", command=self.classify)
        advanced.add_command(label="Spark Storage/ZIP 가져오기...", command=self.import_source)
        advanced.add_separator()
        advanced.add_command(label="CSV 내보내기...", command=lambda: self.export("csv"))
        advanced.add_command(label="JSON 내보내기...", command=lambda: self.export("json"))
        advanced.add_command(label="Spark Desktop 호환 폴더...", command=self.spark_handoff)
        menu.add_cascade(label="고급", menu=advanced)
        self.configure(menu=menu)

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="ShopSource Studio", font=("Segoe UI", 18, "bold")).pack(anchor="w")

        store = ttk.Frame(body, padding=(0, 16, 0, 8))
        store.pack(fill="x")
        ttk.Label(store, text="Store:", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.store_var = tk.StringVar(value="001")
        self.store_combo = ttk.Combobox(store, textvariable=self.store_var, width=34, state="readonly")
        self.store_combo.pack(side="left", padx=6)
        self.store_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        source = ttk.LabelFrame(body, text="소싱 상품", padding=12)
        source.pack(fill="x", pady=(0, 10))
        ttk.Button(source, text="소싱 폴더 열기", command=self.open_source_folder).pack(side="left", padx=(0, 8))
        ttk.Button(source, text="소싱 상품 가져오기", command=self.import_amazon_inbox).pack(side="left")

        automatic = ttk.LabelFrame(body, text="자동 소싱 (Keepa)", padding=12)
        automatic.pack(fill="x", pady=(0, 10))
        ttk.Button(automatic, text="API 설정", command=self.configure_keepa).grid(row=0, column=0, padx=(0, 8))
        ttk.Label(automatic, text="Target candidates:").grid(row=0, column=1)
        self.auto_target_var = tk.StringVar(value="5")
        ttk.Entry(automatic, textvariable=self.auto_target_var, width=7).grid(row=0, column=2, padx=6)
        ttk.Button(automatic, text="소싱 미리보기", command=self.preview_sourcing).grid(row=0, column=3, padx=4)
        ttk.Button(automatic, text="자동 소싱 시작", command=self.start_sourcing).grid(row=0, column=4, padx=4)
        ttk.Button(automatic, text="일시정지", command=self.pause_sourcing).grid(row=0, column=5, padx=4)
        ttk.Button(automatic, text="계속", command=self.resume_sourcing).grid(row=0, column=6, padx=4)
        ttk.Button(automatic, text="취소", command=self.cancel_sourcing).grid(row=0, column=7, padx=4)
        self.sourcing_info_var = tk.StringVar(value="Status: IDLE")
        ttk.Label(automatic, textvariable=self.sourcing_info_var, wraplength=760).grid(
            row=1, column=0, columnspan=8, sticky="w", pady=(8, 0)
        )

        summary = ttk.Frame(body, padding=(4, 8))
        summary.pack(fill="x")
        self.summary_text = tk.StringVar(value="")
        ttk.Label(summary, textvariable=self.summary_text, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.empty_master_text = tk.StringVar(value="")
        ttk.Label(summary, textvariable=self.empty_master_text, justify="left", foreground="#8a4b08").pack(
            anchor="w", pady=(8, 0)
        )

        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=14)

        package = ttk.LabelFrame(body, text="Spark Center 업로드 package", padding=12)
        package.pack(fill="x")
        ttk.Label(package, text="Status:").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="PRIMARY")
        self.status_combo = ttk.Combobox(package, textvariable=self.status_var, width=18, state="readonly",
                                         values=["ALL","PRIMARY","RESERVE_A","RESERVE_B","RESERVE_C","LOW_RESERVE","HIGH_RESERVE","REVIEW","RESTRICTED","ARCHIVED"])
        self.status_combo.grid(row=0, column=1, padx=(6, 20), sticky="w")
        ttk.Label(package, text="Product count:").grid(row=0, column=2, sticky="w")
        self.package_limit_var = tk.StringVar(value="50")
        ttk.Entry(package, textvariable=self.package_limit_var, width=8).grid(
            row=0, column=3, padx=6, sticky="w"
        )
        self.create_package_button = ttk.Button(
            package,
            text="Spark Center 업로드 폴더 만들기",
            command=self.create_spark_center_package,
        )
        self.create_package_button.grid(row=1, column=0, columnspan=2, padx=(0, 8), pady=(12, 0), sticky="w")
        self.open_package_button = ttk.Button(
            package, text="폴더 열기", command=self.open_latest_package, state="disabled"
        )
        self.open_package_button.grid(row=1, column=2, padx=6, pady=(12, 0))
        self.mark_uploaded_button = ttk.Button(
            package, text="업로드 완료 표시", command=self.mark_latest_uploaded, state="disabled"
        )
        self.mark_uploaded_button.grid(row=1, column=3, padx=6, pady=(12, 0))
        self.package_info_var = tk.StringVar(value="Recent package: none")
        ttk.Label(
            package, textvariable=self.package_info_var, justify="left", wraplength=1000
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(12, 0))

        bottom = ttk.Frame(body, padding=(0, 18, 0, 0))
        bottom.pack(fill="x", side="bottom")
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

    def open_source_folder(self):
        ensure_dirs()
        os.startfile(str(AMAZON_INBOX_DIR))

    def import_amazon_inbox(self):
        self._run(self._import_and_classify_current_store, self._source_import_done)

    def _import_and_classify_current_store(self):
        result = import_amazon_source()
        classification = classify_store(self.store_id())
        return {"import": result, "classification": classification}

    def _source_import_done(self, result):
        self.progress.stop()
        imported = result["import"]
        master = master_summary()
        self.msg.set(f"소싱 가져오기 완료: MASTER {master['unique_products']:,}")
        messagebox.showinfo(
            "소싱 상품 가져오기 완료",
            f"신규 MASTER: {imported['inserted']:,}\n"
            f"갱신: {imported['updated']:,}\n"
            f"중복: {imported['duplicates']:,}\n"
            f"오류: {imported['invalid']:,}\n"
            f"MASTER 전체: {master['unique_products']:,}\n\n"
            f"{self.store_var.get()} 자동 분류 완료\n\n"
            f"원본 폴더:\n{imported['source_path']}",
        )
        self.refresh()

    def classify(self):
        self._run(lambda: classify_store(self.store_id()))

    def configure_keepa(self):
        key = simpledialog.askstring(
            "Keepa API 설정",
            "Keepa API key를 입력하세요. 이 값은 현재 실행 메모리에만 보관되며 파일/DB에 저장되지 않습니다.",
            show="*",
            parent=self,
        )
        if key:
            self.keepa_session_key = key.strip()
            messagebox.showinfo("Keepa API 설정", "현재 세션용 API key가 설정되었습니다.")

    def _auto_target(self):
        try:
            target = int(self.auto_target_var.get())
        except ValueError:
            raise ValueError("Target candidates는 정수여야 합니다.")
        if target < 1:
            raise ValueError("Target candidates는 1 이상이어야 합니다.")
        return target

    def preview_sourcing(self):
        try:
            preview = SourcingEngine().preview(self.store_id(), self._auto_target())
        except Exception as exc:
            messagebox.showerror("자동 소싱 미리보기", str(exc))
            return
        messagebox.showinfo("자동 소싱 미리보기 (network 요청 없음)", json.dumps(preview, ensure_ascii=False, indent=2))

    def start_sourcing(self):
        try:
            target = self._auto_target()
            provider = KeepaProvider(api_key=self.keepa_session_key)
        except Exception as exc:
            messagebox.showerror("자동 소싱", str(exc))
            return
        self.current_sourcing_run = new_run_id(self.store_id())
        self.sourcing_info_var.set(f"Run {self.current_sourcing_run} | Status: RUNNING")
        self._run(
            lambda: SourcingEngine(provider, self._queue_sourcing_progress).run(
                self.store_id(), target, run_id=self.current_sourcing_run
            ),
            self._sourcing_done,
        )

    def _sourcing_done(self, result):
        self.progress.stop()
        self.sourcing_info_var.set(
            f"Run {result['run_id']} | Status: {result['status']} | Candidates {result['discovered_asins']} | "
            f"Hydrated {result['hydrated_products']} | MASTER +{result['inserted']} / updated {result['updated']} | "
            f"Tokens {result['tokens_consumed']} / left {result['tokens_left']}"
        )
        self.refresh()

    def _queue_sourcing_progress(self, result):
        self.after(0, lambda: self.sourcing_info_var.set(
            f"Run {result['run_id']} | Status: {result['status']} | Candidates {result['discovered_asins']} | "
            f"Hydrated {result['hydrated_products']} | MASTER +{result['inserted']} / updated {result['updated']} | "
            f"Tokens {result['tokens_consumed']} / left {result['tokens_left']}"
        ))

    def pause_sourcing(self):
        if self.current_sourcing_run:
            try:
                SourcingEngine.pause(self.current_sourcing_run)
                self.sourcing_info_var.set(f"Run {self.current_sourcing_run} | Status: PAUSED")
            except Exception as exc:
                messagebox.showerror("자동 소싱", str(exc))

    def resume_sourcing(self):
        if not self.current_sourcing_run:
            messagebox.showinfo("자동 소싱", "계속할 sourcing run이 없습니다.")
            return
        try:
            provider = KeepaProvider(api_key=self.keepa_session_key)
        except Exception as exc:
            messagebox.showerror("자동 소싱", str(exc))
            return
        self._run(
            lambda: SourcingEngine(provider, self._queue_sourcing_progress).resume(self.current_sourcing_run),
            self._sourcing_done,
        )

    def cancel_sourcing(self):
        if self.current_sourcing_run:
            try:
                result = SourcingEngine.cancel(self.current_sourcing_run)
                self.sourcing_info_var.set(f"Run {result['run_id']} | Status: CANCELLED")
            except Exception as exc:
                messagebox.showerror("자동 소싱", str(exc))

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
        if master_summary()["unique_products"] == 0:
            messagebox.showinfo(
                "소싱 상품이 필요합니다",
                "먼저 소싱 폴더에 상품 JSON을 넣고 소싱 상품 가져오기를 눌러주세요.",
            )
            return
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
            count = master["unique_products"]
            self.summary_text.set(f"MASTER products: {count:,}")
            if count == 0:
                self.empty_master_text.set(
                    "소싱 폴더에 상품 JSON을 넣고\n소싱 상품 가져오기를 눌러주세요."
                )
                self.create_package_button.configure(state="disabled")
            else:
                store = store_summary(self.store_id())
                counts = ", ".join(f"{key} {value:,}" for key, value in store["counts"].items())
                self.empty_master_text.set(
                    f"현재 Store 분류: {store['total']:,}" + (f" | {counts}" if counts else "")
                )
                self.create_package_button.configure(state="normal")
        except Exception as exc:
            self.summary_text.set(str(exc))
        self.refresh_package_info()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
