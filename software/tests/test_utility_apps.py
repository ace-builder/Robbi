"""Offline acceptance tests through actual bundled manifests, loader and SDK."""
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import threading

import pytest

from robbi.app_runtime.host import AppHost
from app_project_testing import patch_on_load, source_module
from robbi.app_services.http import HttpClient
from robbi.errors import ValidationError


ROOT = Path(__file__).resolve().parents[1]
APP_IDS = {"random-number", "world-clock", "date-calculator", "percentage-helper", "tip-splitter",
           "recipe-scaler", "unit-converter", "text-toolbox", "password-generator", "habit-tracker",
           "expense-tracker", "packing-list", "bookmarks"}


class Robot:
    def capture_token(self):
        return 0, threading.Event()


@pytest.fixture
def make_host(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Offline utilities must not make HTTP requests")
    monkeypatch.setattr(HttpClient, "request", no_network)
    hosts = []

    def make(*, data=None, permissions=False):
        host = AppHost(ROOT, data or tmp_path / str(len(hosts)), Robot(), permissions_enabled=permissions)
        hosts.append(host)
        patch_on_load(monkeypatch, host, APP_IDS, today=lambda context: date(2026, 10, 23))
        snapshot = host.scan()
        assert not snapshot["errors"]
        return host

    yield make
    for host in hosts:
        host.stop()
        host._executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def host(make_host):
    return make_host()


def call(host, app, tool, **arguments):
    value = host.call(app, tool, arguments)
    assert value["ok"], value
    return value["data"]


def test_all_thirteen_manifests_are_discoverable_without_import(host):
    entries = {app["id"]: app for app in host.catalog()}
    assert APP_IDS <= entries.keys()
    assert not host._modules
    for app_id in APP_IDS:
        assert entries[app_id]["enabled"]
        assert 1 <= len(entries[app_id]["tools"]) <= 3
        assert "network" not in entries[app_id]["permissions"]


def test_random_numbers_include_endpoints_and_preserve_requested_uniqueness(host):
    assert call(host, "random-number", "generate", minimum=-9, maximum=-9, count=3)["numbers"] == [-9] * 3
    values = call(host, "random-number", "generate", minimum=-50, maximum=49, count=100, unique=True)["numbers"]
    assert set(values) == set(range(-50, 50))
    assert len(call(host, "random-number", "generate", minimum=-10**12, maximum=10**12, count=100, unique=True)["numbers"]) == 100
    for arguments in ({"minimum": 3, "maximum": 2}, {"minimum": 1, "maximum": 2, "count": 3, "unique": True}):
        assert not host.call("random-number", "generate", arguments)["ok"]
    with pytest.raises(ValidationError):
        host.call("random-number", "generate", {"count": 101})


def test_clock_converts_date_boundary_and_rejects_nonexistent_and_ambiguous_time(host):
    converted = call(host, "world-clock", "convert", local_time="2026-10-23T00:30", source_timezone="Europe/Berlin",
                     timezones=["America/New_York", "Asia/Tokyo"])
    assert converted["times"][0]["datetime"] == "2026-10-22T18:30:00-04:00"
    assert converted["times"][1]["datetime"] == "2026-10-23T07:30:00+09:00"
    for text in ("2026-03-29T02:30", "2026-10-25T02:30", "bad date"):
        assert not host.call("world-clock", "convert", {"local_time": text})["ok"]
    early = call(host, "world-clock", "convert", local_time="2026-10-25T02:30", occurrence="earlier", timezones=["UTC"])
    late = call(host, "world-clock", "convert", local_time="2026-10-25T02:30", occurrence="later", timezones=["UTC"])
    assert early["utc"] == "2026-10-25T00:30:00+00:00"
    assert late["utc"] == "2026-10-25T01:30:00+00:00"
    assert call(host, "world-clock", "now", timezones=["UTC"])["times"][0]["timezone"] == "UTC"
    assert not host.call("world-clock", "now", {"timezones": ["Not/A_Zone"]})["ok"]


def test_calendar_handles_leap_years_countdown_signed_weekdays_and_holidays(host):
    assert call(host, "date-calculator", "shift", date="2024-02-28", days=1)["date"] == "2024-02-29"
    assert call(host, "date-calculator", "shift", days=1)["date"] == "2026-10-24"
    forward = call(host, "date-calculator", "difference", start="2026-10-23", end="2026-10-27",
                   holidays=["2026-10-26", "2026-10-26", "2026-10-24"])
    assert (forward["calendar_days"], forward["business_days"]) == (4, 1)
    backward = call(host, "date-calculator", "difference", start="2026-10-27", end="2026-10-23")
    assert (backward["calendar_days"], backward["business_days"]) == (-4, -2)
    assert call(host, "date-calculator", "difference", end="2026-10-24")["calendar_days"] == 1
    assert not host.call("date-calculator", "shift", {"date": "9999-12-31", "days": 1})["ok"]
    assert not host.call("date-calculator", "difference", {"end": "2026-02-30"})["ok"]


def test_percentages_and_discount_boundaries(host):
    assert call(host, "percentage-helper", "percent_of", value=240, percent=18)["value"] == "43.2"
    assert call(host, "percentage-helper", "change", value=-20, new_value=-10)["percent_change"] == "50"
    assert call(host, "percentage-helper", "adjust", value=79.99, percent=25)["value"] == "59.9925"
    assert call(host, "percentage-helper", "adjust", value=10, percent=10, operation="increase")["value"] == "11"
    assert not host.call("percentage-helper", "change", {"value": 0, "new_value": 5})["ok"]
    assert not host.call("percentage-helper", "adjust", {"value": 1, "percent": 101})["ok"]


def test_tip_shares_sum_exactly_and_respect_currency_precision(host):
    split = call(host, "tip-splitter", "split", bill=10.05, people=4, tip_percent=10)
    assert split["tip"] == "1.01"
    assert split["shares"] == ["2.77", "2.77", "2.76", "2.76"]
    assert sum(map(Decimal, split["shares"])) == Decimal(split["total"])
    yen = call(host, "tip-splitter", "split", bill=100, people=3, tip_percent=0, currency="JPY", decimal_places=0)
    assert yen["shares"] == ["34", "33", "33"]
    assert not host.call("tip-splitter", "split", {"bill": 5, "currency": "123"})["ok"]


def test_recipe_scaling_preserves_units_and_rejects_empty_ingredients(host):
    scaled = call(host, "recipe-scaler", "scale", original_portions=4, wanted_portions=6,
                  ingredients=[{"name": "Flour", "amount": 250, "unit": "g"}, {"name": "Eggs", "amount": 2}])
    assert scaled["factor"] == "1.5"
    assert scaled["ingredients"] == [{"name": "Flour", "amount": "375", "unit": "g"},
                                     {"name": "Eggs", "amount": "3", "unit": ""}]
    with pytest.raises(ValidationError):
        host.call("recipe-scaler", "scale", {"original_portions": 0, "wanted_portions": 1, "ingredients": []})


def test_converter_distinguishes_binary_data_and_categories(host):
    assert call(host, "unit-converter", "convert", value=1, from_unit="MiB", to_unit="B")["value"] == "1048576"
    assert call(host, "unit-converter", "convert", value=1, from_unit="MB", to_unit="MiB")["value"] == "0.95367432"
    assert call(host, "unit-converter", "convert", value=32, from_unit="F", to_unit="C")["value"] == "0"
    assert call(host, "unit-converter", "convert", value=1, from_unit="kWh", to_unit="J")["value"] == "3600000"
    huge = call(host, "unit-converter", "convert", value=10**12, from_unit="GiB", to_unit="bit")
    assert Decimal(huge["value"]) == Decimal(10**12) * 1073741824 * 8
    for args in ({"value": 1, "from_unit": "kg", "to_unit": "l"}, {"value": -1, "from_unit": "K", "to_unit": "C"}):
        assert not host.call("unit-converter", "convert", args)["ok"]


def test_text_unicode_counting_and_output_bounds(host):
    counted = call(host, "text-toolbox", "analyze", text="Café 🚀\nTwo words")
    assert counted["words"] == 4 and counted["lines"] == 2
    assert counted["utf8_bytes"] > counted["characters"]
    assert call(host, "text-toolbox", "transform", text="straße", operation="upper")["text"] == "STRASSE"
    assert call(host, "text-toolbox", "transform", text=" a\n  b\t", operation="normalize_whitespace")["text"] == "a b"
    assert call(host, "text-toolbox", "analyze", text="")["lines"] == 0
    assert not host.call("text-toolbox", "transform", {"text": "ß" * 4000, "operation": "upper"})["ok"]
    longest = host.call("text-toolbox", "transform", {"text": "🚀" * 4000, "operation": "upper"})
    assert longest["ok"]
    assert longest["data"]["text"] == "🚀" * 4000
    assert len(json.dumps(longest, ensure_ascii=False).encode("utf-8")) < 65536


def test_passwords_use_requested_groups_and_do_not_write_app_state(host):
    result = call(host, "password-generator", "generate", count=5, length=32)
    for password in result["passwords"]:
        assert len(password) == 32
        assert any(char.islower() for char in password)
        assert any(char.isupper() for char in password)
        assert any(char.isdigit() for char in password)
        assert any(not char.isalnum() for char in password)
        assert not set(password) & set("Il1O0o")
    digits = call(host, "password-generator", "generate", lowercase=False, uppercase=False, symbols=False)["passwords"][0]
    assert digits.isdigit()
    assert not (host.directory / "data" / "password-generator" / "state.json").exists()
    assert not host.call("password-generator", "generate", {"lowercase": False, "uppercase": False, "digits": False, "symbols": False})["ok"]


def test_habits_are_idempotent_and_report_retained_streaks(host):
    call(host, "habit-tracker", "manage", action="create", name="Read")
    for day in ("2026-10-20", "2026-10-21", "2026-10-22", "2026-10-22"):
        call(host, "habit-tracker", "manage", action="mark", name="Read", date=day)
    habit = call(host, "habit-tracker", "manage")["habits"][0]
    assert habit["completed_days"] == habit["current_streak"] == habit["best_streak"] == 3
    assert not habit["completed_today"]
    assert not host.call("habit-tracker", "manage", {"action": "mark", "name": "Read", "date": "2026-10-24"})["ok"]
    call(host, "habit-tracker", "manage", action="unmark", name="Read", date="2026-10-21")
    assert call(host, "habit-tracker", "manage")["habits"][0]["current_streak"] == 1


def test_expenses_round_decimal_and_never_mix_currencies(host):
    one = call(host, "expense-tracker", "add", amount=1.005, category="Food")["expense"]
    call(host, "expense-tracker", "add", amount=2, currency="USD", date="2026-10-01")
    call(host, "expense-tracker", "add", amount=.1, currency="eur")
    listed = call(host, "expense-tracker", "list")
    assert listed["totals"] == {"EUR": "1.11", "USD": "2.00"}
    assert call(host, "expense-tracker", "list", start="2026-10-23", end="2026-10-23")["totals"] == {"EUR": "1.11"}
    call(host, "expense-tracker", "remove", id=one["id"])
    assert call(host, "expense-tracker", "list", currency="eur")["totals"] == {"EUR": "0.10"}
    assert not host.call("expense-tracker", "list", {"start": "2026-10-24", "end": "2026-10-23"})["ok"]


def test_packing_quantities_are_absolute_and_trip_specific(host):
    call(host, "packing-list", "manage", action="create", trip="Weekend", departure="2026-10-24")
    call(host, "packing-list", "manage", action="add", trip="Weekend", item_name="Shirts", quantity=5, category="Clothes")
    for _ in range(2):
        packed = call(host, "packing-list", "manage", action="pack", trip="Weekend", item_number=1, packed=3)
        assert packed["remaining"] == 2
    assert not host.call("packing-list", "manage", {"action": "pack", "trip": "Weekend", "packed": 6})["ok"]
    assert call(host, "packing-list", "manage", action="show", trip="Weekend")["items"][0]["packed"] == 3
    call(host, "packing-list", "manage", action="delete", trip="Weekend")
    assert call(host, "packing-list", "manage")["trips"] == []


def test_bookmarks_update_existing_url_search_tags_and_reject_unsafe_urls(host):
    first = call(host, "bookmarks", "save", title="Hiking", url="https://example.org/trails", tags=["Outside", "outside"])["bookmark"]
    updated = call(host, "bookmarks", "save", title="Trails", url=first["url"], tags=["walking"])["bookmark"]
    assert first["id"] == updated["id"] and first["tags"] == ["outside"]
    assert len(call(host, "bookmarks", "search", query="WALK")["bookmarks"]) == 1
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:secret@example.org", "https://example.org:bad", "https://exa mple.org", "https://example.org/\nheader"):
        assert not host.call("bookmarks", "save", {"title": "bad", "url": url})["ok"]
    call(host, "bookmarks", "remove", id=first["id"])
    assert call(host, "bookmarks", "search")["bookmarks"] == []


def test_all_stateful_apps_survive_new_host_instances(make_host, tmp_path):
    data = tmp_path / "persistent"
    first = make_host(data=data)
    call(first, "habit-tracker", "manage", action="create", name="Read")
    call(first, "expense-tracker", "add", amount=12)
    call(first, "packing-list", "manage", action="create", trip="Trip")
    call(first, "bookmarks", "save", title="Example", url="https://example.org")
    first.stop()
    first._executor.shutdown(wait=True, cancel_futures=True)
    second = make_host(data=data)
    assert call(second, "habit-tracker", "manage")["habits"][0]["name"] == "Read"
    assert call(second, "expense-tracker", "list")["totals"] == {"EUR": "12.00"}
    assert call(second, "packing-list", "manage")["trips"][0]["name"] == "Trip"
    assert call(second, "bookmarks", "search")["bookmarks"][0]["title"] == "Example"


def test_habit_capacity_and_corrupt_state_never_overwrite_existing_records(host):
    for number in range(12):
        call(host, "habit-tracker", "manage", action="create", name=f"Habit {number}")
    state_path = host.directory / "data" / "habit-tracker" / "state.json"
    before = state_path.read_bytes()
    assert not host.call("habit-tracker", "manage", {"action": "create", "name": "Thirteenth"})["ok"]
    assert state_path.read_bytes() == before
    state_path.write_text(json.dumps({"habits": {"Broken": [["not a date"]]}}), encoding="utf-8")
    before = state_path.read_bytes()
    assert not host.call("habit-tracker", "manage", {})["ok"]
    assert state_path.read_bytes() == before


def test_state_permission_required_and_call_not_replayed(make_host):
    host = make_host(permissions=True)
    result = host.call("habit-tracker", "manage", {"action": "create", "name": "Read"})
    assert not result["ok"]
    assert not (host.directory / "data" / "habit-tracker" / "state.json").exists()


def test_unicode_bookmarks_hit_byte_budget_without_losing_saved_links(host):
    state_path = host.directory / "data" / "bookmarks" / "state.json"
    successful = 0
    for number in range(40):
        before = state_path.read_bytes() if state_path.exists() else None
        response = host.call("bookmarks", "save", {"title": f"Bookmark {number}",
            "url": f"https://example.org/{number}/" + "🚀" * 900})
        if not response["ok"]:
            assert "storage is full" in response["text"]
            assert state_path.read_bytes() == before
            break
        successful += 1
    else:
        pytest.fail("Unicode bookmarks should reach the explicit byte budget before 40 entries")
    listed = host.call("bookmarks", "search", {})
    assert listed["ok"] and len(listed["data"]["bookmarks"]) == successful
    assert len(json.dumps(listed, ensure_ascii=False).encode("utf-8")) < 65536


@pytest.mark.parametrize("app,tool,arguments", [
    ("random-number", "generate", {"minimum": True}),
    ("password-generator", "generate", {"length": 11}),
    ("password-generator", "generate", {"count": 6}),
    ("tip-splitter", "split", {"bill": 1, "people": 0}),
    ("text-toolbox", "analyze", {"text": "a" * 4001}),
    ("unit-converter", "convert", {"value": float("inf"), "from_unit": "m", "to_unit": "ft"}),
])
def test_manifest_rejects_invalid_inputs_before_loading(host, app, tool, arguments):
    with pytest.raises(ValidationError):
        host.call(app, tool, arguments)
    assert app not in host._modules


def _saved_state(host, app, value):
    path = host.directory / "data" / app / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_percentage_change_accepts_smallest_valid_nonzero_input(host):
    response = host.call("percentage-helper", "change", {"value": 5e-324, "new_value": 1})
    assert response["ok"], response
    assert Decimal(response["data"]["percent_change"]) == Decimal("2e325")
    assert len(json.dumps(response)) < 65536


def test_case_expanding_bookmark_tags_cannot_poison_saved_state(host):
    call(host, "bookmarks", "save", title="Existing", url="https://example.org")
    path = host.directory / "data" / "bookmarks" / "state.json"
    before = path.read_bytes()
    response = host.call("bookmarks", "save", {"title": "New", "url": "https://example.org/new", "tags": ["ß" * 30]})
    assert not response["ok"] and "tag" in response["text"]
    assert path.read_bytes() == before
    assert len(call(host, "bookmarks", "search")["bookmarks"]) == 1


@pytest.mark.parametrize("app,key,tool,entry", [
    ("expense-tracker", "entries", "remove", {"id": "abcdef123456", "amount": "1.00", "currency": "EUR", "date": "2026-10-23", "category": "Food", "description": ""}),
    ("bookmarks", "bookmarks", "remove", {"id": "abcdef123456", "title": "Link", "url": "https://example.org", "tags": []}),
])
def test_duplicate_record_ids_are_rejected_before_any_removal(host, app, key, tool, entry):
    path = _saved_state(host, app, {key: [entry, entry]})
    before = path.read_bytes()
    response = host.call(app, tool, {"id": entry["id"]})
    assert not response["ok"] and "Saved" in response["text"]
    assert path.read_bytes() == before


def test_maximum_bookmark_list_fits_result_budget(host):
    entries = [{"id": f"{number:012x}", "title": "🚀" * 100,
                "url": f"https://example.org/{number}/" + "x" * 900, "tags": []} for number in range(39)]
    value = {"bookmarks": entries}
    assert len(json.dumps(value, ensure_ascii=False).encode("utf-8")) < 55000
    _saved_state(host, "bookmarks", value)
    response = host.call("bookmarks", "search", {})
    assert response["ok"], response
    assert len(response["data"]["bookmarks"]) == 39
    assert len(json.dumps(response, ensure_ascii=False).encode("utf-8")) < 65536


def test_habit_summary_handles_earliest_calendar_date():
    assert source_module("habit-tracker").habit_summary("Read", [], date.min)["current_streak"] == 0


def test_display_keeps_whole_number_zeroes_when_zero_places_requested():
    assert source_module("unit-converter").display(100, places=0) == "100"


@pytest.mark.parametrize("app,key,tool,args,value", [
    ("expense-tracker", "entries", "remove", {"id": "abcdef123456"}, [{"id": "abcdef123456", "amount": "0.00", "currency": "EUR", "date": "2026-10-23", "category": "Food", "description": ""}]),
    ("expense-tracker", "entries", "add", {"amount": 1}, [{"id": "abcdef123456", "amount": "9999999999.99", "currency": "EUR", "date": "2026-10-23", "category": "Food", "description": ""}]),
    ("packing-list", "trips", "manage", {"action": "create", "trip": "New"}, {"": {"departure": "", "items": []}}),
    ("packing-list", "trips", "manage", {"action": "delete", "trip": "Trip"}, {"Trip": {"departure": "", "items": [{"name": " ", "category": "Other", "quantity": 1, "packed": 0}]}}),
    ("bookmarks", "bookmarks", "remove", {"id": "abcdef123456"}, [{"id": "abcdef123456", "title": " ", "url": "https://example.org", "tags": []}]),
])
def test_invalid_saved_values_never_mutate_records(host, app, key, tool, args, value):
    path = _saved_state(host, app, {key: value})
    before = path.read_bytes()
    response = host.call(app, tool, args)
    assert not response["ok"] and response.get("text")
    assert path.read_bytes() == before


def test_duplicate_saved_urls_are_rejected_before_bookmark_update(host):
    entries = [{"id": f"{number:012x}", "title": "Link", "url": "https://example.org", "tags": []} for number in range(2)]
    path = _saved_state(host, "bookmarks", {"bookmarks": entries})
    before = path.read_bytes()
    response = host.call("bookmarks", "save", {"title": "Changed", "url": "https://example.org"})
    assert not response["ok"] and "Saved bookmarks" in response["text"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("local_time,source,target", [
    ("0001-01-01T00:00", "Asia/Tokyo", "UTC"),
    ("9999-12-31T23:59", "UTC", "Asia/Tokyo"),
])
def test_clock_extreme_dates_fail_with_actionable_completed_results(host, local_time, source, target):
    response = host.call("world-clock", "convert", {"local_time": local_time, "source_timezone": source, "timezones": [target]})
    assert not response["ok"] and "years 1 and 9999" in response["text"]
    assert next(reversed(host._jobs.values()))["state"] == "done"
