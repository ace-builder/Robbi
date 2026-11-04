from robot.kids import KidsEngine
from robot.onboarding import OnboardingStore


class FakeMemory:
    def __init__(self):
        self.calls = []

    def remember(self, payload, source=""):
        self.calls.append((payload, source))
        return {"added": [1], "updated": [], "deleted": [], "search_results": []}


class FakeProductivity:
    def __init__(self):
        self.items = []

    def add_list_item(self, list_name, title, source=""):
        self.items.append({"list_name": list_name, "title": title, "source": source})
        return self.items[-1]


def test_onboarding_preview_apply_is_safe(tmp_path):
    store = OnboardingStore(tmp_path / "draft.json", tmp_path / "profile.json")
    memory = FakeMemory()
    productivity = FakeProductivity()

    draft = store.save_draft({"user_name": "maker", "robot_name": "Tiny Bot", "open_tasks": ["test onboarding"]})
    assert draft["user_name"] == "maker"
    assert store.profile() == {}

    result = store.apply(draft, memory, productivity)
    assert result["profile"]["wake_phrase"] == "Hey Robbi"
    assert result["profile"]["robot_name"] == "Tiny Bot"
    assert productivity.items[0]["title"] == "test onboarding"
    assert any(source == "onboarding:approved" for _, source in memory.calls)


def test_kids_rps_and_stop(tmp_path):
    kids = KidsEngine(tmp_path / "kids.json")
    start = kids.start("rps")
    assert start["ok"] is True
    assert kids.status()["active"] is True

    result = kids.handle_input("rock")
    assert result["ok"] is True
    assert "chose" in result["speak"]

    stopped = kids.stop()
    assert stopped["ok"] is True
    assert kids.status()["active"] is False
