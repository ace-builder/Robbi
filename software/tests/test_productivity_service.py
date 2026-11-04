"""Service contracts with in-memory ports: no RobotController/config/hardware."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from robbi.errors import BusyError
from robbi.services.productivity import ProductivityService, ResearchOptions


class Repository:
    def __init__(self):
        self.notes = {}
        self.research = {}
        self.jobs = {}

    def create_note(self, **fields):
        note = {"id": len(self.notes) + 1, **fields}
        self.notes[note["id"]] = note
        return note

    def create_research_note(self, **fields):
        self.research[1] = {"id": 1, "status": "pending", **fields}
        self.jobs[2] = {"id": 2, "status": "pending", "input": {"research_note_id": 1, "query": fields["query"]}}
        return {**self.research[1], "job_id": 2}

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def update_job(self, job_id, **fields):
        self.jobs[job_id].update(fields)
        return self.jobs[job_id]

    def update_research_note(self, note_id, fields):
        self.research[note_id].update(fields)
        return self.research[note_id]


class Scheduler:
    def __init__(self, rejection=None):
        self.work = []
        self.rejection = rejection

    def submit(self, callback, *args):
        if self.rejection:
            raise self.rejection
        self.work.append((callback, args))

    def finish(self):
        callback, args = self.work.pop(0)
        callback(*args)


def make_service(*, rejection=None, result=None, publish=None, options=None):
    repository, scheduler, events, searches = Repository(), Scheduler(rejection), [], []

    def search(query, **arguments):
        searches.append((query, arguments))
        return result or {"ok": True, "results": [{"title": "Guide", "content": "Useful facts", "url": "https://example.test/guide"}]}

    service = ProductivityService(repository, search=SimpleNamespace(search=search), scheduler=scheduler,
        publish_event=publish or (lambda kind, payload: events.append((kind, payload))),
        research_options=options or ResearchOptions, clock=lambda: 1234)
    return service, repository, scheduler, events, searches


def test_service_import_does_not_load_robot_or_global_configuration():
    code = """
import sys
class NoRobotImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'robot' or fullname.startswith('robot.'):
            raise AssertionError('The service must not depend on robot configuration or controller')
sys.meta_path.insert(0, NoRobotImports())
from robbi.services.productivity import ProductivityService
assert 'robot.config' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10)


def test_note_write_survives_broken_event_sink():
    def unavailable(*_):
        raise OSError("event storage unavailable")
    service, repository, _, _, _ = make_service(publish=unavailable)
    note = service.create_note({"text": "Remember the cable", "structured": {"tag": "parts"}})
    assert repository.notes[note["id"]]["raw_text"] == "Remember the cable"
    assert note["structured_json"] == {"tag": "parts"}


def test_research_runs_on_supplied_scheduler_and_reads_current_options():
    current = [ResearchOptions(1, "basic")]
    service, repository, scheduler, events, searches = make_service(options=lambda: current[0])
    service.create_research_note({"text": "motor encoders"})
    assert repository.jobs[2]["status"] == "pending"
    assert searches == []
    current[0] = ResearchOptions(3, "advanced")
    scheduler.finish()
    assert searches == [("motor encoders", {"max_results": 3, "search_depth": "advanced"})]
    assert repository.jobs[2]["status"] == "done"
    assert repository.research[1]["status"] == "ready"
    assert repository.research[1]["completed_at"] == 1234
    assert "Guide: Useful facts" in repository.research[1]["summary"]
    assert events[-1][1]["tool"] == "research_note_ready"


@pytest.mark.parametrize("rejection", [BusyError("Research queue full"), RuntimeError("Executor stopped")])
def test_rejected_work_never_leaves_durable_job_pending(rejection):
    service, repository, scheduler, events, searches = make_service(rejection=rejection)
    with pytest.raises(type(rejection), match=str(rejection)):
        service.create_research_note({"query": "encoder guide"})
    assert repository.jobs[2]["status"] == "failed"
    assert repository.jobs[2]["error"] == str(rejection)
    assert repository.research[1]["status"] == "failed"
    assert repository.jobs[2]["completed_at"] == 1234
    assert scheduler.work == [] and searches == []
    assert events[-1][0] == "tool.failed"


def test_network_failure_records_failed_note_and_job():
    service, repository, scheduler, events, _ = make_service(result={"ok": False, "error": "Wi-Fi disconnected"})
    service.create_research_note({"query": "encoder guide"})
    scheduler.finish()
    assert repository.jobs[2]["error"] == "Wi-Fi disconnected"
    assert repository.jobs[2]["status"] == repository.research[1]["status"] == "failed"
    assert events[-1][0] == "tool.failed"


def test_missing_research_job_performs_no_external_work():
    service, _, _, events, searches = make_service()
    service.run_research_job(404)
    assert searches == [] and events == []


def test_existing_http_routes_use_service_and_report_queue_backpressure():
    from robot.domains.productivity import ProductivityDomain
    from robot.webapp import create_app
    from robbi.web import install_error_handlers

    service, repository, _, _, _ = make_service(rejection=BusyError("Research queue full"))
    # Compatibility adapter has no hardware, executors or shared controller
    # state; these routes need only the independently composed service.
    host = ProductivityDomain()
    host.productivity_service = service
    app = create_app(host)
    install_error_handlers(app)
    client = app.test_client()
    response = client.post("/notes", json={"text": "Check encoder cable"})
    assert response.status_code == 200
    assert response.get_json()["note"]["raw_text"] == "Check encoder cable"
    response = client.post("/research_notes", json={"query": "encoder guide"})
    assert response.status_code == 429
    assert repository.jobs[2]["status"] == repository.research[1]["status"] == "failed"
