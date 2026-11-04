"""Calculator regressions found during independent implementation review."""
from pathlib import Path
import threading

import pytest

from robbi.app_runtime.host import AppHost


@pytest.fixture
def host(tmp_path):
    class Robot:
        def capture_token(self):
            return 0, threading.Event()
    host = AppHost(Path(__file__).resolve().parents[1], tmp_path, Robot())
    host.scan()
    yield host
    host.stop()
    host._executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("expression,expected", [("123456789 + 1", "123456790"), ("1 / 3", "0.3333333333333333"), (" 2 + 3 ", "5")])
def test_calculator_text_preserves_returned_precision_and_accepts_outer_spaces(host, expression, expected):
    response = host.call("calculator", "calculate", {"expression": expression})
    assert response["ok"], response
    assert response["text"].split(" = ")[1] == expected


@pytest.mark.parametrize("expression", ["1/0", "sqrt(-1)", "log(0)", "1+", "2**1000", "__import__('os')"])
def test_expected_calculator_errors_are_completed_actionable_results(host, expression):
    response = host.call("calculator", "calculate", {"expression": expression})
    assert not response["ok"] and response.get("text")
    job = next(reversed(host._jobs.values()))
    assert job["state"] == "done" and job["result"] == response
    assert "error" not in response


def test_fahrenheit_absolute_zero_is_valid_and_conversion_text_is_precise(host):
    response = host.call("calculator", "convert", {"value": -459.67, "from_unit": "F", "to_unit": "K"})
    assert response["ok"] and response["data"]["value"] == 0
    response = host.call("calculator", "convert", {"value": 123456789, "from_unit": "m", "to_unit": "m"})
    assert response["ok"] and response["text"] == "123456789 m = 123456789 m"
