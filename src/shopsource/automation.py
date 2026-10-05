"""Persistent beginner-mode automation queue.

Handlers are injected by the caller.  Creating or resuming a queue cannot by
itself perform provider or remote work; human-gate tasks wait for an explicit
confirmation before their handler can run.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from .db import connect, init_db
from .security import redact_text, redact_value

TASK_STATES = {"QUEUED", "RUNNING", "PAUSED", "WAITING_FOR_CONFIRMATION", "WAITING_FOR_INPUT",
               "SUCCEEDED", "SUCCEEDED_WITH_WARNINGS", "FAILED", "SKIPPED", "STOPPED"}
FINAL_TASK_STATES = {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS", "SKIPPED", "STOPPED"}
TRANSIENT_CODES = {"TIMEOUT", "NETWORK_INTERRUPTION", "HTTP_429", "THROTTLE", "TEMPORARY_PROVIDER", "TEMPORARY_SERVER"}
FRIENDLY_ERRORS = {
    "MISSING_SNAPSHOT": "최신 재고 확인이 필요합니다.",
    "BLOCKED_SOURCE_OUT_OF_STOCK": "원본 상품이 품절이라 판매 대상에서 제외했습니다.",
    "PREVIEW_STALE": "재고 또는 가격이 바뀌어 미리보기를 다시 만들었습니다.",
    "MISSING_SCOPE": "Shopify 권한이 부족합니다. 권한 확인이 필요합니다.",
    "TOKEN_BUDGET": "예상 API 사용량이 설정 한도를 넘어서 잠시 멈췄습니다.",
    "BAD_CREDENTIAL": "연결 정보가 올바르지 않습니다. 자격 증명을 확인하세요.",
    "CAPTCHA": "Amazon 확인 화면이 감지되었습니다. 사용자가 직접 확인해야 합니다.",
    "BUSINESS_INPUT": "사업자 또는 정책 정보를 입력해야 합니다.",
    "CONFLICT": "자동으로 결정할 수 없는 충돌이 있습니다. 내용을 확인하세요.",
}


def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def collection_prerequisite_workflow(store_id=None):
    """Persistent local-only collection prerequisite task for homepage flows."""
    return [{"task_key": "ENSURE_COLLECTION_PLAN", "title": "Ensure Collection Plan",
             "stage": "Local planning", "max_attempts": 1,
             "checkpoint": {"store_id": store_id} if store_id else {}}]


class AutomationTaskError(RuntimeError):
    def __init__(self, code, detail="", *, transient=False):
        super().__init__(detail or code); self.code=code; self.detail=detail or code
        self.transient=bool(transient or code in TRANSIENT_CODES)


def friendly_error(code, detail=""):
    return FRIENDLY_ERRORS.get(str(code or "").upper(), "작업을 완료하지 못했습니다. 자세한 내용을 확인하세요.")


SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_runs(
 run_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,workflow_key TEXT NOT NULL,mode TEXT NOT NULL,
 status TEXT NOT NULL,current_task_key TEXT,total_tasks INTEGER NOT NULL,completed_tasks INTEGER NOT NULL DEFAULT 0,
 failed_tasks INTEGER NOT NULL DEFAULT 0,warning_tasks INTEGER NOT NULL DEFAULT 0,pause_requested INTEGER NOT NULL DEFAULT 0,
 stop_requested INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,started_at TEXT,paused_at TEXT,finished_at TEXT,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_automation_runs_store_status ON automation_runs(store_id,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS automation_tasks(
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,task_key TEXT NOT NULL,title TEXT NOT NULL,stage TEXT NOT NULL,
 position INTEGER NOT NULL,status TEXT NOT NULL,attempt_count INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL DEFAULT 4,
 checkpoint_json TEXT NOT NULL DEFAULT '{}',result_summary_json TEXT NOT NULL DEFAULT '{}',error_code TEXT,error_message TEXT,
 technical_detail TEXT,requires_confirmation INTEGER NOT NULL DEFAULT 0,requires_user_input INTEGER NOT NULL DEFAULT 0,
 confirmation_prompt TEXT,handler_key TEXT,started_at TEXT,finished_at TEXT,updated_at TEXT NOT NULL,
 UNIQUE(run_id,task_key));
CREATE INDEX IF NOT EXISTS idx_automation_tasks_next ON automation_tasks(run_id,status,position);
CREATE TABLE IF NOT EXISTS automation_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,task_key TEXT,event_type TEXT NOT NULL,message TEXT NOT NULL,
 detail_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_automation_events_run ON automation_events(run_id,id DESC);
CREATE TABLE IF NOT EXISTS automation_settings(
 store_id TEXT PRIMARY KEY,auto_mode INTEGER NOT NULL DEFAULT 1,auto_retry INTEGER NOT NULL DEFAULT 1,
 max_retries INTEGER NOT NULL DEFAULT 4,auto_continue INTEGER NOT NULL DEFAULT 1,human_gates_only INTEGER NOT NULL DEFAULT 1,
 restart_notice INTEGER NOT NULL DEFAULT 1,provider_token_limit INTEGER,updated_at TEXT NOT NULL);
"""


class WorkflowAutomationService:
    def __init__(self, db=None, handlers=None):
        self.db=db; self.handlers=handlers or {}; init_db(db)
        with connect(db) as con: con.executescript(SCHEMA)

    def settings(self, store_id):
        with connect(self.db) as con: row=con.execute("SELECT * FROM automation_settings WHERE store_id=?",(store_id,)).fetchone()
        return dict(row) if row else {"store_id":store_id,"auto_mode":1,"auto_retry":1,"max_retries":4,
            "auto_continue":1,"human_gates_only":1,"restart_notice":1,"provider_token_limit":None}

    def save_settings(self, store_id, **values):
        current=self.settings(store_id); current.update({k:v for k,v in values.items() if k in current})
        with connect(self.db) as con:
            con.execute("""INSERT INTO automation_settings(store_id,auto_mode,auto_retry,max_retries,auto_continue,human_gates_only,restart_notice,provider_token_limit,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET auto_mode=excluded.auto_mode,auto_retry=excluded.auto_retry,
                max_retries=excluded.max_retries,auto_continue=excluded.auto_continue,human_gates_only=excluded.human_gates_only,
                restart_notice=excluded.restart_notice,provider_token_limit=excluded.provider_token_limit,updated_at=excluded.updated_at""",
                (store_id,int(bool(current["auto_mode"])),int(bool(current["auto_retry"])),max(1,min(10,int(current["max_retries"]))),
                 int(bool(current["auto_continue"])),int(bool(current["human_gates_only"])),int(bool(current["restart_notice"])),
                 current.get("provider_token_limit"),_now()))
        return self.settings(store_id)

    def create_run(self, store_id, workflow_key, tasks, *, mode="AUTO"):
        if mode not in {"AUTO","REVIEW","MANUAL"}: raise ValueError("unsupported automation mode")
        run_id="AUTO_"+secrets.token_hex(8); now=_now(); tasks=list(tasks)
        if len({str(x["task_key"]) for x in tasks}) != len(tasks): raise ValueError("duplicate task_key")
        default_attempts=self.settings(store_id)["max_retries"]
        with connect(self.db) as con:
            con.execute("""INSERT INTO automation_runs(run_id,store_id,workflow_key,mode,status,total_tasks,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",(run_id,store_id,workflow_key,mode,"QUEUED",len(tasks),now,now))
            con.executemany("""INSERT INTO automation_tasks(run_id,task_key,title,stage,position,status,max_attempts,checkpoint_json,
                requires_confirmation,requires_user_input,confirmation_prompt,handler_key,updated_at) VALUES(?,?,?,?,?,'QUEUED',?,?,?,?,?,?,?)""",
                [(run_id,str(t["task_key"]),str(t.get("title") or t["task_key"]),str(t.get("stage") or t["task_key"]),i,
                  int(t.get("max_attempts") or default_attempts),json.dumps(redact_value(t.get("checkpoint") or {}),ensure_ascii=False),
                  int(bool(t.get("requires_confirmation"))),int(bool(t.get("requires_user_input"))),t.get("confirmation_prompt"),
                  t.get("handler_key") or t["task_key"],now) for i,t in enumerate(tasks)])
        self._event(run_id,None,"RUN_CREATED",f"자동 작업 {len(tasks)}개를 준비했습니다.")
        return self.status(run_id)

    def _event(self,run_id,task_key,event_type,message,detail=None):
        with connect(self.db) as con: con.execute("INSERT INTO automation_events(run_id,task_key,event_type,message,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (run_id,task_key,event_type,redact_text(message,limit=500),json.dumps(redact_value(detail or {}),ensure_ascii=False),_now()))

    def _task(self, run_id, task_key):
        with connect(self.db) as con: row=con.execute("SELECT * FROM automation_tasks WHERE run_id=? AND task_key=?",(run_id,task_key)).fetchone()
        if not row: raise KeyError(task_key)
        value=dict(row); value["checkpoint"]=json.loads(value.pop("checkpoint_json") or "{}"); value["result_summary"]=json.loads(value.pop("result_summary_json") or "{}")
        return value

    def _refresh_run(self, run_id, *, status=None, current_task_key=None):
        with connect(self.db) as con:
            counts={r[0]:r[1] for r in con.execute("SELECT status,COUNT(*) FROM automation_tasks WHERE run_id=? GROUP BY status",(run_id,))}
            done=sum(counts.get(x,0) for x in ("SUCCEEDED","SUCCEEDED_WITH_WARNINGS","SKIPPED"))
            failed=counts.get("FAILED",0); warnings=counts.get("SUCCEEDED_WITH_WARNINGS",0)
            if status is None:
                if counts.get("WAITING_FOR_CONFIRMATION"): status="WAITING_FOR_CONFIRMATION"
                elif counts.get("WAITING_FOR_INPUT"): status="WAITING_FOR_INPUT"
                elif counts.get("PAUSED"): status="PAUSED"
                elif done==sum(counts.values()) and counts: status="SUCCEEDED_WITH_WARNINGS" if warnings else "SUCCEEDED"
                elif failed: status="FAILED"
                else: status="RUNNING"
            finished=_now() if status in {"SUCCEEDED","SUCCEEDED_WITH_WARNINGS","FAILED","STOPPED"} else None
            con.execute("UPDATE automation_runs SET status=?,current_task_key=?,completed_tasks=?,failed_tasks=?,warning_tasks=?,finished_at=COALESCE(?,finished_at),updated_at=? WHERE run_id=?",
                        (status,current_task_key,done,failed,warnings,finished,_now(),run_id))

    def run(self, run_id, *, max_tasks=None):
        processed=0
        while max_tasks is None or processed<int(max_tasks):
            with connect(self.db) as con:
                run=con.execute("SELECT * FROM automation_runs WHERE run_id=?",(run_id,)).fetchone()
                if not run: raise KeyError(run_id)
                if run["stop_requested"] or run["status"]=="STOPPED": break
                if run["pause_requested"]:
                    con.execute("UPDATE automation_runs SET status='PAUSED',paused_at=?,updated_at=? WHERE run_id=?",(_now(),_now(),run_id)); break
                task=con.execute("SELECT * FROM automation_tasks WHERE run_id=? AND status='QUEUED' ORDER BY position LIMIT 1",(run_id,)).fetchone()
            if not task: self._refresh_run(run_id); break
            checkpoint=json.loads(task["checkpoint_json"] or "{}")
            if task["requires_user_input"] and not checkpoint.get("user_input_confirmed"):
                self._set_waiting(task,"WAITING_FOR_INPUT","입력이 필요한 단계입니다."); break
            if task["requires_confirmation"] and not checkpoint.get("confirmed"):
                self._set_waiting(task,"WAITING_FOR_CONFIRMATION",task["confirmation_prompt"] or "사용자 확인이 필요합니다."); break
            if self.handlers.get(task["handler_key"]) is None:
                # Handler-free local work can use a bounded, connection-reusing
                # path. Each task is still committed atomically with its events.
                completed=self._execute_noop_chunk(run_id,dict(task),
                    limit=min(50,int(max_tasks)-processed) if max_tasks is not None else 50)
                processed+=completed
                if completed == 0: break
                continue
            task_state=dict(task)
            self._execute_task(task_state); processed+=1
            state=task_state.get("_final_status","FAILED")
            if state in {"FAILED","WAITING_FOR_INPUT","WAITING_FOR_CONFIRMATION"}: break
        return self.status(run_id)

    def _execute_noop_chunk(self, run_id, first_task, *, limit=50):
        """Run a bounded sequence of default local tasks with per-task durability."""
        limit=max(1,min(100,int(limit)))
        candidates=[first_task]
        with connect(self.db) as con:
            if limit>1:
                rows=con.execute(
                    "SELECT * FROM automation_tasks WHERE run_id=? AND status='QUEUED' AND position>? ORDER BY position LIMIT ?",
                    (run_id,int(first_task["position"]),limit-1),
                ).fetchall()
                for row in rows:
                    item=dict(row)
                    if (self.handlers.get(item["handler_key"]) is not None or
                            item["requires_confirmation"] or item["requires_user_input"]):
                        break
                    candidates.append(item)

            # Reconcile a potentially stale legacy summary at each safe chunk
            # boundary, then increment it atomically with every completed task.
            counts={r[0]:r[1] for r in con.execute(
                "SELECT status,COUNT(*) FROM automation_tasks WHERE run_id=? GROUP BY status",(run_id,))}
            done=sum(counts.get(s,0) for s in ("SUCCEEDED","SUCCEEDED_WITH_WARNINGS","SKIPPED"))
            con.execute("UPDATE automation_runs SET completed_tasks=?,failed_tasks=?,warning_tasks=? WHERE run_id=?",
                        (done,counts.get("FAILED",0),counts.get("SUCCEEDED_WITH_WARNINGS",0),run_id))
            completed_count=0
            for item in candidates:
                control=con.execute("SELECT pause_requested,stop_requested FROM automation_runs WHERE run_id=?",
                                    (run_id,)).fetchone()
                if not control or control["pause_requested"] or control["stop_requested"]:
                    break
                now=_now()
                result={"status":"SUCCEEDED","message":"안전한 로컬 단계를 완료했습니다."}
                updated=con.execute("""UPDATE automation_tasks SET status='SUCCEEDED',attempt_count=attempt_count+1,
                    result_summary_json=?,error_code=NULL,error_message=NULL,technical_detail=NULL,
                    started_at=COALESCE(started_at,?),finished_at=?,updated_at=? WHERE id=? AND status='QUEUED'""",
                    (json.dumps(redact_value(result),ensure_ascii=False),now,now,now,item["id"]))
                if updated.rowcount != 1:
                    con.rollback()
                    continue
                con.execute("""UPDATE automation_runs SET status='RUNNING',current_task_key=?,
                    completed_tasks=completed_tasks+1,started_at=COALESCE(started_at,?),updated_at=? WHERE run_id=?""",
                    (item["task_key"],now,now,run_id))
                con.execute("INSERT INTO automation_events(run_id,task_key,event_type,message,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                    (run_id,item["task_key"],"RUNNING",redact_text(f"{item['title']} 시작",limit=500),"{}",now))
                con.execute("INSERT INTO automation_events(run_id,task_key,event_type,message,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                    (run_id,item["task_key"],"SUCCEEDED",redact_text(result["message"],limit=500),
                     json.dumps(redact_value(result),ensure_ascii=False),now))
                con.commit()
                completed_count+=1
        return completed_count

    def _set_waiting(self, task, status, message):
        with connect(self.db) as con:
            con.execute("UPDATE automation_tasks SET status=?,error_message=?,updated_at=? WHERE id=?",(status,message,_now(),task["id"]))
        self._event(task["run_id"],task["task_key"],status,message); self._refresh_run(task["run_id"],status=status,current_task_key=task["task_key"])

    def _execute_task(self, task):
        run_id,key=task["run_id"],task["task_key"]
        with connect(self.db) as con:
            con.execute("UPDATE automation_tasks SET status='RUNNING',attempt_count=attempt_count+1,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?",(_now(),_now(),task["id"]))
            con.execute("UPDATE automation_runs SET status='RUNNING',current_task_key=?,started_at=COALESCE(started_at,?),updated_at=? WHERE run_id=?",(key,_now(),_now(),run_id))
        self._event(run_id,key,"RUNNING",f"{task['title']} 시작")
        handler=self.handlers.get(task["handler_key"])
        try:
            if handler is None: result={"status":"SUCCEEDED","message":"안전한 로컬 단계를 완료했습니다."}
            else: result=handler(self._task(run_id,key)) or {}
            status="SUCCEEDED_WITH_WARNINGS" if result.get("warnings") else "SUCCEEDED"
            task["_final_status"]=status
            with connect(self.db) as con: con.execute("UPDATE automation_tasks SET status=?,result_summary_json=?,error_code=NULL,error_message=NULL,technical_detail=NULL,finished_at=?,updated_at=? WHERE id=?",
                (status,json.dumps(redact_value(result),ensure_ascii=False),_now(),_now(),task["id"]))
            self._event(run_id,key,status,result.get("message") or f"{task['title']} 완료",result)
        except AutomationTaskError as exc:
            current=self._task(run_id,key); settings=self.settings(self.status(run_id)["store_id"])
            if exc.transient and settings["auto_retry"] and current["attempt_count"] < current["max_attempts"]:
                task["_final_status"]="QUEUED"
                message=f"일시적인 오류라 자동으로 다시 시도 중입니다. ({current['attempt_count'] + 1}/{current['max_attempts']})"
                with connect(self.db) as con: con.execute("UPDATE automation_tasks SET status='QUEUED',error_code=?,error_message=?,technical_detail=?,updated_at=? WHERE id=?",
                    (exc.code,message,redact_text(exc.detail,limit=500),_now(),task["id"]))
                self._event(run_id,key,"RETRYING",message,{"code":exc.code})
            else:
                wait=exc.code in {"BAD_CREDENTIAL","MISSING_SCOPE","CAPTCHA","BUSINESS_INPUT","CONFLICT","TOKEN_BUDGET"}
                status="WAITING_FOR_INPUT" if wait else "FAILED"
                task["_final_status"]=status
                message=friendly_error(exc.code,exc.detail)
                with connect(self.db) as con: con.execute("UPDATE automation_tasks SET status=?,error_code=?,error_message=?,technical_detail=?,updated_at=? WHERE id=?",
                    (status,exc.code,message,redact_text(exc.detail,limit=500),_now(),task["id"]))
                self._event(run_id,key,status,message,{"code":exc.code}); self._refresh_run(run_id,status=status,current_task_key=key)
        except Exception as exc:
            task["_final_status"]="FAILED"
            with connect(self.db) as con: con.execute("UPDATE automation_tasks SET status='FAILED',error_code='UNEXPECTED',error_message=?,technical_detail=?,updated_at=? WHERE id=?",
                ("예상하지 못한 오류가 발생했습니다. 자세한 내용을 확인하세요.",redact_text(exc,limit=500),_now(),task["id"]))
            self._refresh_run(run_id,status="FAILED",current_task_key=key)

    def pause(self, run_id):
        with connect(self.db) as con: con.execute("UPDATE automation_runs SET pause_requested=1,status='PAUSED',paused_at=?,updated_at=? WHERE run_id=?",(_now(),_now(),run_id)); con.execute("UPDATE automation_tasks SET status='PAUSED',updated_at=? WHERE run_id=? AND status='QUEUED'",(_now(),run_id))
        self._event(run_id,None,"PAUSED","자동 작업을 안전한 경계에서 일시정지했습니다."); return self.status(run_id)

    def resume(self, run_id):
        with connect(self.db) as con:
            con.execute("UPDATE automation_runs SET pause_requested=0,status='QUEUED',paused_at=NULL,updated_at=? WHERE run_id=?",(_now(),run_id))
            con.execute("UPDATE automation_tasks SET status='QUEUED',updated_at=? WHERE run_id=? AND status='PAUSED'",(_now(),run_id))
        self._event(run_id,None,"RESUMED","마지막 checkpoint에서 이어서 실행합니다."); return self.run(run_id)

    def stop(self, run_id):
        with connect(self.db) as con:
            con.execute("UPDATE automation_runs SET stop_requested=1,status='STOPPED',finished_at=?,updated_at=? WHERE run_id=?",(_now(),_now(),run_id))
            con.execute("UPDATE automation_tasks SET status='STOPPED',finished_at=?,updated_at=? WHERE run_id=? AND status IN ('QUEUED','PAUSED','WAITING_FOR_CONFIRMATION','WAITING_FOR_INPUT')",(_now(),_now(),run_id))
        self._event(run_id,None,"STOPPED","앞으로의 작업을 중단했습니다. 완료된 결과는 유지됩니다."); return self.status(run_id)

    def retry_failed(self, run_id):
        with connect(self.db) as con:
            con.execute("UPDATE automation_tasks SET status='QUEUED',error_code=NULL,error_message=NULL,updated_at=? WHERE run_id=? AND status='FAILED' AND attempt_count<max_attempts",(_now(),run_id))
            con.execute("UPDATE automation_runs SET status='QUEUED',stop_requested=0,pause_requested=0,finished_at=NULL,updated_at=? WHERE run_id=?",(_now(),run_id))
        self._event(run_id,None,"RETRY_FAILED","실패한 작업만 다시 시도합니다."); return self.run(run_id)

    def confirm(self, run_id, task_key, *, user_input=None):
        task=self._task(run_id,task_key)
        if task["status"] not in {"WAITING_FOR_CONFIRMATION","WAITING_FOR_INPUT"}: raise RuntimeError("확인 대기 중인 작업이 아닙니다.")
        checkpoint=task["checkpoint"]; checkpoint["confirmed"]=True
        if user_input is not None: checkpoint.update({"user_input_confirmed":True,"user_input":redact_value(user_input)})
        elif task["requires_user_input"]: raise RuntimeError("필요한 입력이 없습니다.")
        with connect(self.db) as con:
            con.execute("UPDATE automation_tasks SET status='QUEUED',checkpoint_json=?,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",(json.dumps(checkpoint,ensure_ascii=False),_now(),task["id"]))
            con.execute("UPDATE automation_runs SET status='QUEUED',updated_at=? WHERE run_id=?",(_now(),run_id))
        self._event(run_id,task_key,"CONFIRMED","사용자 확인 후 다음 안전 단계를 계속합니다."); return self.run(run_id)

    def status(self, run_id, *, event_limit=50):
        with connect(self.db) as con:
            run=con.execute("SELECT * FROM automation_runs WHERE run_id=?",(run_id,)).fetchone()
            if not run: raise KeyError(run_id)
            current=con.execute("SELECT title,stage,status,error_message FROM automation_tasks WHERE run_id=? AND task_key=?",(run_id,run["current_task_key"])).fetchone() if run["current_task_key"] else None
            events=[dict(x) for x in con.execute("SELECT event_type,message,created_at FROM automation_events WHERE run_id=? ORDER BY id DESC LIMIT ?",(run_id,min(200,max(1,event_limit))))]
        result=dict(run); total=max(1,result["total_tasks"]); result["progress_percent"]=round(result["completed_tasks"]/total*100); result["current_task"]=dict(current) if current else None; result["events"]=events
        return result

    def tasks(self, run_id, *, page=0, page_size=50, status=None):
        page_size=max(1,min(50,int(page_size))); params=[run_id]; where="run_id=?"
        if status: where+=" AND status=?"; params.append(status)
        with connect(self.db) as con:
            total=con.execute(f"SELECT COUNT(*) FROM automation_tasks WHERE {where}",params).fetchone()[0]
            rows=[dict(x) for x in con.execute(f"SELECT task_key,title,stage,position,status,attempt_count,max_attempts,error_code,error_message,updated_at FROM automation_tasks WHERE {where} ORDER BY position LIMIT ? OFFSET ?",[*params,page_size,page*page_size])]
        return {"rows":rows,"total":total,"page":page,"page_size":page_size}

    def task_result(self, run_id, task_key):
        """Return the bounded, redacted summary from one completed task."""
        task = self._task(run_id, task_key)
        return {"task_key": task_key, "status": task["status"], "result": redact_value(task.get("result_summary") or {})}

    def interrupted(self, store_id=None):
        params=[]; where="status IN ('QUEUED','RUNNING','PAUSED','WAITING_FOR_CONFIRMATION','WAITING_FOR_INPUT')"
        if store_id: where+=" AND store_id=?"; params.append(store_id)
        with connect(self.db) as con: return [dict(x) for x in con.execute(f"SELECT * FROM automation_runs WHERE {where} ORDER BY updated_at DESC",params)]


def source_safety_workflow(preview: dict):
    paid=bool(preview.get("estimated_tokens") or preview.get("provider_chargeable"))
    return [
        {"task_key":"LOCAL_PREVIEW","title":"검사 대상과 사용량 계산","stage":"사전점검"},
        {"task_key":"PROVIDER_CHECK","title":"최신 source 재고·가격 확인","stage":"재고 확인",
         "requires_confirmation":paid,"confirmation_prompt":f"Amazon/Keepa 최신 재고·가격을 확인합니다. 예상 {preview.get('estimated_batches',0)} batch / {preview.get('estimated_tokens',0)} token."},
        {"task_key":"FRESHNESS","title":"최신 확인 여부 평가","stage":"안전 평가"},
        {"task_key":"AVAILABILITY","title":"재고 상태 평가","stage":"안전 평가"},
        {"task_key":"PRICE_MARGIN","title":"가격 변동과 마진 평가","stage":"안전 평가"},
        {"task_key":"SOLD_OUT_DIAGNOSIS","title":"Sold Out 원인 분류","stage":"진단"},
        {"task_key":"ACTION_PLAN","title":"차단·재확인·재입고 후보 생성","stage":"조치 계획"},
        {"task_key":"REPORT","title":"요약 보고서 생성","stage":"완료"},
    ]


def store_build_workflow():
    names=("STORE_PROFILE","SOURCE_SAFETY","SOURCING_MASTER","PRODUCTS","COLLECTIONS","BRAND","NAVIGATION","HOMEPAGE","PAGES_SEO","COMPLETION","LAUNCH_READINESS")
    return [{"task_key":x,"title":x.replace("_"," ").title(),"stage":"자동 구축"} for x in names]
