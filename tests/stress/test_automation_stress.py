from shopsource.automation import AutomationTaskError, WorkflowAutomationService
from shopsource.db import connect


def test_5000_task_queue_indexed_and_paged(tmp_path):
    svc=WorkflowAutomationService(tmp_path/"queue.db"); tasks=[{"task_key":f"T{i}"} for i in range(5000)]
    run=svc.create_run("001","MASSIVE",tasks)["run_id"]
    assert svc.status(run)["total_tasks"]==5000 and len(svc.tasks(run,page_size=50)["rows"])==50


def test_restart_mid_run_2000_items(tmp_path):
    db=tmp_path/"restart.db"; svc=WorkflowAutomationService(db); run=svc.create_run("001","AUDIT",[{"task_key":f"A{i}"} for i in range(2000)])["run_id"]
    first=svc.run(run,max_tasks=777)
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM automation_tasks WHERE run_id=? AND status='SUCCEEDED'",(run,)).fetchone()[0]==777
        assert con.execute("SELECT COUNT(*) FROM automation_tasks WHERE run_id=? AND status='QUEUED'",(run,)).fetchone()[0]==1223
    assert first["completed_tasks"]==777

    resumed=WorkflowAutomationService(db).run(run)
    reopened=WorkflowAutomationService(db)
    assert resumed["completed_tasks"]==2000 and resumed["status"]=="SUCCEEDED"
    assert reopened.status(run)["completed_tasks"]==2000 and reopened.status(run)["status"]=="SUCCEEDED"
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM automation_tasks WHERE run_id=? AND attempt_count!=1",(run,)).fetchone()[0]==0
        assert con.execute("SELECT COUNT(*) FROM automation_events WHERE run_id=?",(run,)).fetchone()[0]==4001
        assert con.execute("SELECT COUNT(*) FROM automation_events WHERE run_id=? AND task_key IS NOT NULL",(run,)).fetchone()[0]==4000


def test_noop_chunk_honors_pause_and_stop_at_safe_boundary(tmp_path,monkeypatch):
    db=tmp_path/"controls.db"; svc=WorkflowAutomationService(db)
    original=svc._execute_noop_chunk
    run=svc.create_run("001","PAUSE",[{"task_key":f"P{i}"} for i in range(60)])["run_id"]
    triggered={"value":False}
    def pause_before_chunk(run_id,first_task,*,limit=50):
        if not triggered["value"]:
            triggered["value"]=True; svc.pause(run_id)
        return original(run_id,first_task,limit=limit)
    monkeypatch.setattr(svc,"_execute_noop_chunk",pause_before_chunk)
    paused=svc.run(run)
    assert paused["status"]=="PAUSED" and paused["completed_tasks"]==0
    monkeypatch.setattr(svc,"_execute_noop_chunk",original)
    assert svc.resume(run)["status"]=="SUCCEEDED"

    stopped=svc.create_run("001","STOP",[{"task_key":f"S{i}"} for i in range(60)])["run_id"]
    def stop_before_chunk(run_id,first_task,*,limit=50):
        svc.stop(run_id)
        return original(run_id,first_task,limit=limit)
    monkeypatch.setattr(svc,"_execute_noop_chunk",stop_before_chunk)
    result=svc.run(stopped)
    assert result["status"]=="STOPPED" and result["completed_tasks"]==0


def test_100_transient_failures_bounded(tmp_path):
    calls={}
    def flaky(task):
        key=task["task_key"]; calls[key]=calls.get(key,0)+1
        if calls[key]==1: raise AutomationTaskError("TIMEOUT",transient=True)
    handlers={f"T{i}":flaky for i in range(100)}; svc=WorkflowAutomationService(tmp_path/"retry.db",handlers=handlers)
    run=svc.create_run("001","RETRY",[{"task_key":f"T{i}","max_attempts":4} for i in range(100)])["run_id"]
    assert svc.run(run)["status"]=="SUCCEEDED" and sum(calls.values())==200


def test_mixed_human_gates_do_not_scan_or_run_ahead(tmp_path):
    svc=WorkflowAutomationService(tmp_path/"gate.db"); spec=[]
    for i in range(500): spec.append({"task_key":f"T{i}","requires_confirmation":i in {100,300}})
    run=svc.create_run("001","MIXED",spec)["run_id"]; first=svc.run(run)
    assert first["completed_tasks"]==100 and first["status"]=="WAITING_FOR_CONFIRMATION"
    second=svc.confirm(run,"T100"); assert second["completed_tasks"]==300 and second["status"]=="WAITING_FOR_CONFIRMATION"
