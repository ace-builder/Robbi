from robot.controller import RobotController


def test_focus_timer_seconds_are_not_minutes():
    controller = RobotController.__new__(RobotController)

    assert controller._parse_focus_timer_duration_s_v2("start a focus timer for 30 seconds") == 30
    assert controller._parse_focus_timer_duration_s_v2("start a focus timer for 2 minutes") == 120


def test_focus_timer_stop_intent_is_detected():
    controller = RobotController.__new__(RobotController)

    assert controller._looks_like_focus_timer_stop_request_v2("stop the focus timer please")
    assert not controller._looks_like_focus_timer_stop_request_v2("start a focus timer for 5 minutes")


def test_reminder_request_parses_delay_and_text():
    controller = RobotController.__new__(RobotController)

    parsed = controller._parse_reminder_request("remind me in 15 seconds to stand up")

    assert parsed is not None
    assert parsed["delay_seconds"] == 15
    assert "stand up" in parsed["message"]
