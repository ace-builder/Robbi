"""Integration checks for the expanded bundled app library and calculator."""
import json
from pathlib import Path
import threading

import pytest

from robbi.app_runtime.host import AppHost


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path):
    class Robot:
        def capture_token(self):
            return 0, threading.Event()
    host = AppHost(ROOT, tmp_path, Robot())
    assert host.scan()["errors"] == []
    yield host
    host.stop()
    host._executor.shutdown(wait=True, cancel_futures=True)


def test_every_app_discoverable_with_constraints_loaded_on_demand(host):
    prompt = host.prompt_catalog()
    assert len(prompt) <= 7000
    lines = [json.loads(line) for line in prompt.splitlines() if line.startswith('{')]
    assert {row['app'] for row in lines} == {app['id'] for app in host.catalog()}
    assert all('input_schema' not in row for row in lines)
    assert host._modules == {}
    detailed=host.discover_capabilities('calculator calculate expression')
    calc=next(row for row in detailed['tools'] if row['app']=='calculator' and row['tool']=='calculate')
    assert calc['input_schema']['required'] == ['expression']
    assert calc['input_schema']['properties']['expression']['maxLength'] == 200
    assert 'x-order' not in json.dumps(detailed)
    host.set_enabled('weather', False)
    assert all(json.loads(line)['app'] != 'weather' for line in host.prompt_catalog().splitlines() if line.startswith('{'))


@pytest.mark.parametrize('expression,expected', [
    ('(18 * 24) / 3', 144), ('sqrt(81) + abs(-4)', 13), ('sin(pi / 2)', 1),
    ('log(e) + log10(100)', 3), ('floor(2.9) + ceil(2.1)', 5), ('2 ** -3', .125),
])
def test_scientific_calculator_returns_computed_values(host, expression, expected):
    result = host.call('calculator', 'calculate', {'expression': expression})
    assert result['ok']
    assert result['data']['value'] == pytest.approx(expected)


@pytest.mark.parametrize('expression', [
    '__import__("os")', '(1).__class__', 'sum([1,2])', 'sqrt(-1)', 'log(0)',
    'sqrt(1,2)', 'sqrt(x=2)', 'True + 1', '10 ** 100000', '1/0', '1e999', '(-1)**0.5',
])
def test_calculator_rejects_arbitrary_code_and_unbounded_or_invalid_math(host, expression):
    assert host.call('calculator', 'calculate', {'expression': expression})['ok'] is False


def test_free_maps_link_needs_no_owner_network_grant(host):
    result = host.call('google-maps', 'directions', {'destination': 'Berlin Hauptbahnhof'})
    assert result['ok']
    assert result['data']['link_only']
    assert result['data']['url'].startswith('https://www.google.com/maps/dir/')
    assert not host._services  # Link creation never initializes a provider.
