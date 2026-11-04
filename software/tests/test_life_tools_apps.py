"""Household apps exercised through real manifests, loader, SDK and undo."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import threading

from PIL import Image
import pytest

from robbi.app_runtime.host import AppHost
from app_project_testing import patch_on_load, project_module
from robbi.app_services.http import HttpClient
from robbi.errors import ValidationError


ROOT = Path(__file__).resolve().parents[1]
APPS = {"where-did-i-put-it", "fridge-rescue", "home-care", "decision-notebook"}


class Clock:
    @staticmethod
    def now(zone):
        return datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc).astimezone(zone)


class Robot:
    def __init__(self):
        self.cancelled = threading.Event()
        self.captures = 0
        self.timers = {}
        self.next_timer = 0

    def capture_token(self):
        return 0, self.cancelled

    def capture_image(self):
        self.captures += 1
        image = io.BytesIO()
        Image.new("RGB", (40, 30), "blue").save(image, format="PNG")
        return image.getvalue()

    def timer(self, operation, arguments, token, cancelled):
        assert not cancelled()
        if operation == "start":
            self.next_timer += 1
            identifier = f"timer-{self.next_timer}"
            self.timers[identifier] = arguments
            return {"ok": True, "data": {"id": identifier, **arguments}}
        if operation == "cancel":
            existed = self.timers.pop(arguments["id"], None) is not None
            return {"ok": existed, "text": "Timer cancelled." if existed else "Timer is no longer active."}
        return {"ok": True, "data": {"timers": self.timers}}


@pytest.fixture
def make_host(tmp_path, monkeypatch):
    def forbidden_network(*args, **kwargs):
        pytest.fail("These apps must work without network access")
    monkeypatch.setattr(HttpClient, "request", forbidden_network)
    source = tmp_path / "projects"
    for app in APPS:
        shutil.copytree(ROOT / "apps" / app, source / "apps" / app,
                        ignore=shutil.ignore_patterns("__pycache__"))
    hosts = []
    def make(*, data=None, permissions=False):
        host = AppHost(source, data or tmp_path / str(len(hosts)), Robot(), permissions_enabled=permissions)
        hosts.append(host)
        patch_on_load(monkeypatch, host, APPS, datetime=Clock)
        assert not host.scan()["errors"]
        return host
    yield make
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def host(make_host):
    return make_host()


def call(host, app, tool, **arguments):
    response = host.call(app, tool, arguments)
    assert response["ok"], response
    return response["data"]


def path(host, app):
    return host.directory / "data" / app / "state.json"


def create(host, app):
    tool, arguments = {
        "where-did-i-put-it": ("remember", {"name": "Keys", "location": "Blue drawer", "notes": "Spare set"}),
        "fridge-rescue": ("stock", {"action": "save", "name": "Tomatoes", "quantity": "2 cans", "expires": "2026-10-28", "storage": "pantry"}),
        "home-care": ("manage", {"name": "Water filter", "every_days": 30, "first_due": "2026-10-20", "notes": "Kitchen"}),
        "decision-notebook": ("save", {"title": "Commute", "options": ["Bike", "Bus"], "criteria": ["Comfort", "Cost"], "weights": [1, 3], "notes": "Winter"}),
    }[app]
    response = host.call(app, tool, arguments, include_invocation=True)
    assert response["ok"], response
    return response


def test_apps_have_compact_discoverable_manifests_and_no_network_permissions(host):
    catalog = {app["id"]: app for app in host.catalog()}
    assert APPS <= catalog.keys()
    assert not host._modules
    for app in APPS:
        assert catalog[app]["enabled"]
        assert len(catalog[app]["tools"]) == 3
        assert "network" not in catalog[app]["permissions"]
        assert catalog[app]["config_schema"]["properties"]["timezone"]["default"] == "Europe/Berlin"
    for app, name in (("where-did-i-put-it", "find"), ("fridge-rescue", "suggest"), ("home-care", "agenda")):
        assert next(tool for tool in catalog[app]["tools"] if tool["name"] == name)["read_only"]


def test_location_search_update_found_and_remove_have_stable_ids(host):
    item = create(host, "where-did-i-put-it")["data"]["item"]
    found = call(host, "where-did-i-put-it", "find", query="BLUE")
    assert found["items"][0]["id"] == item["id"]
    updated = call(host, "where-did-i-put-it", "remember", id=item["id"], name="Spare keys", location="Coat pocket")["item"]
    assert updated["id"] == item["id"] and updated["notes"] == "Spare set"
    call(host, "where-did-i-put-it", "update", id=item["id"], action="found", location="Desk")
    assert call(host, "where-did-i-put-it", "find", status="stored")["items"] == []
    assert call(host, "where-did-i-put-it", "find", status="found")["items"][0]["location"] == "Desk"
    call(host, "where-did-i-put-it", "update", id=item["id"], action="remove")
    assert call(host, "where-did-i-put-it", "find")["items"] == []


def test_location_photos_are_explicit_bounded_references_and_undo_keeps_artifacts(host):
    response = host.call("where-did-i-put-it", "remember", {"name": "Passport", "location": "Green folder", "photo": True}, include_invocation=True)
    assert response["ok"]
    assert "without interpreting" in response["text"]
    assert host.robot.captures == 1
    artifact = response["artifacts"][0]
    assert artifact["width"] == 280 and artifact["height"] == 240
    item = response["data"]["item"]
    assert item["photo_id"] == artifact["id"] and item["photo_url"] == artifact["url"]
    updated = call(host, "where-did-i-put-it", "remember", id=item["id"], name="Passport", location="Red folder")
    assert updated["item"]["photo_id"] == artifact["id"]
    assert host.robot.captures == 1
    # Undo the later edit first, then the original location/photo association.
    assert host.undo(host.history("where-did-i-put-it")[0]["id"])["ok"]
    assert host.undo(response["invocation"]["id"])["ok"]
    assert not path(host, "where-did-i-put-it").exists()
    assert host.artifacts("where-did-i-put-it").path(artifact["id"]).exists()


def test_photo_permissions_are_preflighted_before_camera_or_state_write(make_host):
    host = make_host(permissions=True)
    host.set_permissions("where-did-i-put-it", {"state": "granted"})
    response = host.call("where-did-i-put-it", "remember", {"name": "Keys", "location": "Drawer", "photo": True})
    assert response["permission_required"]
    assert set(response["permissions"]) == {"robot.camera", "robot.display"}
    assert host.robot.captures == 0 and not path(host, "where-did-i-put-it").exists()
    call(host, "where-did-i-put-it", "remember", name="Keys", location="Drawer")
    assert host.robot.captures == 0


def test_unavailable_camera_does_not_save_a_location(host, monkeypatch):
    monkeypatch.setattr(host.robot, "capture_image", lambda: None)
    response = host.call("where-did-i-put-it", "remember", {"name": "Keys", "location": "Drawer", "photo": True})
    assert not response["ok"]
    assert not path(host, "where-did-i-put-it").exists()


def test_fridge_batch_updates_preserve_omitted_fields_and_sort_dates(host):
    item = create(host, "fridge-rescue")["data"]["item"]
    updated = call(host, "fridge-rescue", "stock", action="save", id=item["id"], expires="2026-10-27")["item"]
    assert updated == {**item, "expires": "2026-10-27"}
    late = call(host, "fridge-rescue", "stock", action="save", name="Milk", expires="2026-10-25")["item"]
    call(host, "fridge-rescue", "stock", action="save", name="Rice")
    assert [row["id"] for row in call(host, "fridge-rescue", "stock", view="past_date")["items"]] == [late["id"]]
    assert call(host, "fridge-rescue", "stock", view="soon", days=1)["items"][0]["days_until_date"] == 1
    assert [row["name"] for row in call(host, "fridge-rescue", "stock")["items"]] == ["Milk", "Tomatoes", "Rice"]
    call(host, "fridge-rescue", "stock", action="save", id=item["id"], expires="")
    assert not host.call("fridge-rescue", "stock", {"action": "save", "name": "Bread", "expires": "2026-02-30"})["ok"]


def test_recipe_matching_is_offline_alias_aware_and_does_not_change_shopping(host):
    for name, expires in [("Nudeln", ""), ("Tomaten", "2026-10-27"), ("ZwIeBeLn", "2026-10-25")]:
        call(host, "fridge-rescue", "stock", action="save", name=name, expires=expires)
    before = path(host, "fridge-rescue").read_bytes()
    suggested = call(host, "fridge-rescue", "suggest")
    pasta = next(recipe for recipe in suggested["recipes"] if recipe["id"] == "tomato-pasta")
    assert pasta["matched"] == ["pasta", "tomato"]
    assert pasta["missing"] == ["onion"] and pasta["use_soon"] == ["tomato"]
    assert len(pasta["steps"]) == 2 and suggested["past_date_excluded"] == 1
    assert path(host, "fridge-rescue").read_bytes() == before
    assert call(host, "fridge-rescue", "shopping")["items"] == []
    excluded = call(host, "fridge-rescue", "suggest", exclude=["tomatoes"])
    assert all("tomato" not in row["matched"] + row["missing"] for row in excluded["recipes"])


def test_fridge_shopping_explicit_add_missing_deduplicates_and_reopens_bought(host):
    call(host, "fridge-rescue", "stock", action="save", name="Potatoes")
    added = call(host, "fridge-rescue", "shopping", action="add_missing", recipe_id="potato-hash")
    assert added["list"] == "fridge-rescue" and added["items"][0]["name"] == "onion"
    identifier = added["items"][0]["id"]
    assert call(host, "fridge-rescue", "shopping", action="add", items=["Onions", "ZwIeBeLn"])["items"] == []
    call(host, "fridge-rescue", "shopping", action="bought", id=identifier)
    assert call(host, "fridge-rescue", "shopping", action="add_missing", recipe_id="potato-hash")["items"][0]["id"] == identifier
    call(host, "fridge-rescue", "shopping", action="remove", id=identifier)
    assert call(host, "fridge-rescue", "shopping")["items"] == []
    assert not host.call("fridge-rescue", "shopping", {"action": "add_missing", "recipe_id": "invented"})["ok"]


def test_home_due_dates_move_from_actual_completion_and_duplicate_is_idempotent(host):
    chore = create(host, "home-care")["data"]["chore"]
    assert call(host, "home-care", "agenda", days_ahead=0)["chores"][0]["days_until_due"] == -6
    done = call(host, "home-care", "act", id=chore["id"], completed_on="2026-10-25")["chore"]
    assert done["next_due"] == "2026-11-24" and done["completions"] == 1
    before = path(host, "home-care").read_bytes()
    assert call(host, "home-care", "act", id=chore["id"], completed_on="2026-10-25")["chore"] == done
    assert path(host, "home-care").read_bytes() == before
    for invalid in ("2026-10-24", "2026-10-27", "2026-02-30"):
        assert not host.call("home-care", "act", {"id": chore["id"], "completed_on": invalid})["ok"]
    assert path(host, "home-care").read_bytes() == before
    changed = call(host, "home-care", "manage", id=chore["id"], every_days=7)["chore"]
    assert changed["next_due"] == "2026-11-01" and changed["notes"] == "Kitchen"
    manual = call(host, "home-care", "manage", id=chore["id"], first_due="2026-11-10")["chore"]
    edited = call(host, "home-care", "manage", id=chore["id"], name="Replace filter")["chore"]
    assert edited["next_due"] == manual["next_due"] and edited["every_days"] == 7


def test_home_reminders_are_opt_in_and_undo_cancels_only_the_exact_timer(host):
    chore = create(host, "home-care")["data"]["chore"]
    assert host.robot.timers == {}
    response = host.call("home-care", "act", {"action": "remind", "id": chore["id"], "minutes": 1440}, include_invocation=True)
    assert response["ok"]
    timer_id = response["data"]["timer"]["id"]
    assert host.robot.timers[timer_id]["seconds"] == 86400
    host.robot.timers["unrelated"] = {"seconds": 60}
    assert host.undo(response["invocation"]["id"])["ok"]
    assert set(host.robot.timers) == {"unrelated"}
    assert call(host, "home-care", "agenda")["chores"][0]["id"] == chore["id"]
    assert not host.call("home-care", "act", {"action": "remind", "id": chore["id"]})["ok"]
    with pytest.raises(ValidationError):
        host.call("home-care", "act", {"action": "remind", "id": chore["id"], "minutes": 1441})


def test_home_reminder_permission_does_not_block_ordinary_chore_state(make_host):
    host = make_host(permissions=True)
    host.set_permissions("home-care", {"state": "granted", "robot.reminders": "denied"})
    chore = create(host, "home-care")["data"]["chore"]
    before = path(host, "home-care").read_bytes()
    response = host.call("home-care", "act", {"action": "remind", "id": chore["id"], "minutes": 20})
    assert response["permission_required"] and response["permissions"] == ["robot.reminders"]
    assert host.robot.timers == {} and path(host, "home-care").read_bytes() == before


def test_decision_scores_require_complete_matrix_and_report_weighted_reasons(host):
    decision = create(host, "decision-notebook")["data"]["decision"]
    for option, criterion, score in [("Bike", "Comfort", 4), ("Bike", "Cost", 10), ("Bus", "Comfort", 8)]:
        compared = call(host, "decision-notebook", "score", id=decision["id"], option=option, criterion=criterion, score=score, reason="My preference")["comparison"]
        assert compared["ranking"] == []
    compared = call(host, "decision-notebook", "score", id=decision["id"], option="bus", criterion="cost", score=5, reason="Ticket cost")["comparison"]
    assert compared["ranking"] == ["Bike", "Bus"]
    assert [option["weighted_score"] for option in compared["options"]] == [8.5, 5.75]
    assert compared["options"][1]["reasons"][1] == {"criterion": "Cost", "weight": 3, "score": 5, "reason": "Ticket cost"}
    assert compared["best_scored"] == ["Bike"]
    reviewed = call(host, "decision-notebook", "review", action="outcome", id=decision["id"], choice="Bus", outcome="Rain made the bus more comfortable.")["comparison"]
    assert reviewed["outcome"]["choice"] == "Bus"
    assert reviewed["outcome"]["date"] == "2026-10-26"
    assert call(host, "decision-notebook", "review", query="commute")["decisions"][0]["has_outcome"]


def test_decision_edits_preserve_weights_notes_and_require_explicit_score_reset(host):
    decision = create(host, "decision-notebook")["data"]["decision"]
    call(host, "decision-notebook", "score", id=decision["id"], option="Bike", criterion="Cost", score=8)
    edited = call(host, "decision-notebook", "save", id=decision["id"], title="Winter commute", options=["Bike", "Bus"], criteria=["Comfort", "Cost"])["decision"]
    assert edited["weights"] == [1, 3] and edited["notes"] == "Winter" and len(edited["ratings"]) == 1
    before = path(host, "decision-notebook").read_bytes()
    arguments = {"id": decision["id"], "title": "New options", "options": ["Walk", "Bus"], "criteria": ["Comfort", "Cost"]}
    assert not host.call("decision-notebook", "save", arguments)["ok"]
    assert path(host, "decision-notebook").read_bytes() == before
    edited = call(host, "decision-notebook", "save", **arguments, reset_scores=True)["decision"]
    assert edited["ratings"] == [] and edited["outcome"] is None


def test_decision_ties_and_invalid_weights_names_and_outcome_dates(host):
    decision = call(host, "decision-notebook", "save", title="Trip", options=["A", "B"], criteria=["Ease"])["decision"]
    for option in ("A", "B"):
        compared = call(host, "decision-notebook", "score", id=decision["id"], option=option, criterion="Ease", score=5)["comparison"]
    assert compared["best_scored"] == ["A", "B"]
    for options, criteria, weights in [(["A", "a"], ["X"], [1]), (["A", "B"], ["X", "x"], [1, 1]), (["A", "B"], ["X", "Y"], [1])]:
        assert not host.call("decision-notebook", "save", {"title": "Invalid", "options": options, "criteria": criteria, "weights": weights})["ok"]
    assert not host.call("decision-notebook", "review", {"action": "outcome", "id": decision["id"], "choice": "A", "outcome": "Good", "reviewed_on": "2026-10-27"})["ok"]


@pytest.mark.parametrize("app", sorted(APPS))
def test_every_app_persists_across_hosts_and_its_creation_is_undoable(make_host, tmp_path, app):
    host = make_host(data=tmp_path / "shared")
    response = create(host, app)
    saved = path(host, app).read_bytes()
    restarted = make_host(data=tmp_path / "shared")
    tool = {"where-did-i-put-it": "find", "fridge-rescue": "stock", "home-care": "agenda", "decision-notebook": "review"}[app]
    assert restarted.call(app, tool, {})["ok"]
    assert path(restarted, app).read_bytes() == saved
    assert host.undo(response["invocation"]["id"])["ok"]
    assert not path(host, app).exists()
    assert not host.undo(response["invocation"]["id"])["ok"]


@pytest.mark.parametrize("app", sorted(APPS))
def test_invalid_saved_record_blocks_reads_and_writes_without_data_loss(host, app):
    create(host, app)
    saved = json.loads(path(host, app).read_text(encoding="utf-8"))
    collection = next(key for key in saved if key != "version")
    saved[collection][0]["unknown_field"] = "Must never be silently discarded"
    path(host, app).write_text(json.dumps(saved), encoding="utf-8")
    before = path(host, app).read_bytes()
    tool = {"where-did-i-put-it": "find", "fridge-rescue": "stock", "home-care": "agenda", "decision-notebook": "review"}[app]
    response = host.call(app, tool, {})
    assert not response["ok"] and "backup" in response["text"]
    assert path(host, app).read_bytes() == before


@pytest.mark.parametrize("mutation", ["version", "duplicate_id", "invalid_date", "bool_interval", "broken_json"])
def test_corruption_variants_are_not_reinitialized(host, mutation):
    create(host, "home-care")
    file = path(host, "home-care")
    state = json.loads(file.read_text(encoding="utf-8"))
    if mutation == "version":
        state["version"] = 2
    elif mutation == "duplicate_id":
        state["chores"].append(dict(state["chores"][0]))
    elif mutation == "invalid_date":
        state["chores"][0]["next_due"] = "2026-02-30"
    elif mutation == "bool_interval":
        state["chores"][0]["every_days"] = True
    file.write_text("{broken" if mutation == "broken_json" else json.dumps(state), encoding="utf-8")
    before = file.read_bytes()
    assert not host.call("home-care", "manage", {"name": "New chore"})["ok"]
    assert file.read_bytes() == before


def test_local_today_obeys_timezone_and_invalid_timezone_never_writes(host):
    host.configure("where-did-i-put-it", {"timezone": "America/Los_Angeles"})
    item = call(host, "where-did-i-put-it", "remember", name="Wallet", location="Shelf")["item"]
    assert item["updated"] == "2026-10-25"
    host.configure("home-care", {"timezone": "Not/A_Timezone"})
    assert not host.call("home-care", "manage", {"name": "Filter"})["ok"]
    assert not path(host, "home-care").exists()


def test_overflow_and_invalid_completion_leave_state_unchanged(host, monkeypatch):
    chore = create(host, "home-care")["data"]["chore"]
    before = path(host, "home-care").read_bytes()
    monkeypatch.setattr(project_module(host, "home-care"), "today", lambda context: datetime(9999, 12, 31).date())
    response = host.call("home-care", "act", {"id": chore["id"], "completed_on": "9999-12-31"})
    assert not response["ok"] and "outside" in response["text"]
    assert path(host, "home-care").read_bytes() == before


def test_unicode_state_budget_refuses_atomically_and_search_result_stays_bounded(host):
    successes = 0
    for index in range(40):
        file = path(host, "where-did-i-put-it")
        before = file.read_bytes() if file.exists() else None
        response = host.call("where-did-i-put-it", "remember", {"name": "😀" * 79 + str(index % 10), "location": "😀" * 180, "notes": "😀" * 160})
        if not response["ok"]:
            assert "limit" in response["text"]
            assert file.read_bytes() == before
            break
        successes += 1
    assert 1 < successes < 40
    result = host.call("where-did-i-put-it", "find", {})
    assert result["ok"] and len(result["data"]["items"]) == successes
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 65536


def test_bounded_collection_and_invalid_record_id_do_not_replace_saved_data(host):
    created = create(host, "home-care")["data"]["chore"]
    state = {"version": 1, "chores": [{**created, "id": f"{index:012x}"} for index in range(40)]}
    file = path(host, "home-care")
    file.write_text(json.dumps(state), encoding="utf-8")
    before = file.read_bytes()
    assert not host.call("home-care", "manage", {"name": "One too many"})["ok"]
    assert not host.call("home-care", "manage", {"action": "remove", "id": "ffffffffffff"})["ok"]
    assert file.read_bytes() == before


@pytest.mark.parametrize("app", sorted(APPS))
def test_existing_record_edits_are_undoable_without_losing_prior_data(host, app):
    created = create(host, app)
    row = next(iter(created["data"].values()))
    identifier = row["id"]
    before = path(host, app).read_bytes()
    tool, arguments = {
        "where-did-i-put-it": ("update", {"id": identifier, "action": "found", "location": "Coat pocket"}),
        "fridge-rescue": ("stock", {"id": identifier, "action": "remove"}),
        "home-care": ("act", {"id": identifier, "action": "complete"}),
        "decision-notebook": ("score", {"id": identifier, "option": "Bike", "criterion": "Cost", "score": 9}),
    }[app]
    response = host.call(app, tool, arguments, include_invocation=True)
    assert response["ok"] and path(host, app).read_bytes() != before
    assert host.undo(response["invocation"]["id"])["ok"]
    assert path(host, app).read_bytes() == before


def test_all_chores_view_retrieves_distant_future_records(host):
    chore = call(host, "home-care", "manage", name="Long term check", first_due="2099-01-01")["chore"]
    assert call(host, "home-care", "agenda", days_ahead=3650)["chores"] == []
    assert call(host, "home-care", "agenda", include_all=True)["chores"][0]["id"] == chore["id"]


@pytest.mark.parametrize("corruption", ["bad_weight", "duplicate_rating", "bad_option_index"])
def test_corrupt_decision_matrix_is_preserved_for_repair(host, corruption):
    row = create(host, "decision-notebook")["data"]["decision"]
    call(host, "decision-notebook", "score", id=row["id"], option="Bike", criterion="Cost", score=8)
    file = path(host, "decision-notebook")
    state = json.loads(file.read_text(encoding="utf-8"))
    row = state["decisions"][0]
    if corruption == "bad_weight":
        row["weights"][0] = True
    elif corruption == "duplicate_rating":
        row["ratings"].append(dict(row["ratings"][0]))
    else:
        row["ratings"][0]["option"] = 9
    file.write_text(json.dumps(state), encoding="utf-8")
    before = file.read_bytes()
    response = host.call("decision-notebook", "review", {"id": row["id"], "action": "compare"})
    assert not response["ok"] and "backup" in response["text"]
    assert file.read_bytes() == before


def test_shopping_batch_that_exceeds_capacity_does_not_partially_add_items(host):
    create(host, "fridge-rescue")
    file = path(host, "fridge-rescue")
    state = json.loads(file.read_text(encoding="utf-8"))
    state["shopping"] = [{"id": f"{index:012x}", "name": f"Item {index}", "bought": False} for index in range(59)]
    file.write_text(json.dumps(state), encoding="utf-8")
    before = file.read_bytes()
    response = host.call("fridge-rescue", "shopping", {"action": "add", "items": ["First new item", "Second new item"]})
    assert not response["ok"] and "60" in response["text"]
    assert file.read_bytes() == before
