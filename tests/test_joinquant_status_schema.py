"""Fixtures derived from the saved official frontend contract, never live bodies."""
import json

import pytest

from keepflame.core.models import KeepStatus
from tests.helpers import FakeHttpClient, response
from tests.test_joinquant import run_keeper
from tests.test_joinquant_auto import FakePage, execute


def interpret(data, *, http_status=200):
    return run_keeper(FakeHttpClient(response(http_status, json.dumps({'code': '00000', 'data': data}))))


@pytest.mark.parametrize('tasks,basis,login_type', [
    ({}, 'login_record_absent', 'missing'),
    ({'login': {}}, 'login_status_absent', 'object'),
])
def test_official_missing_record_patterns_are_only_eligible(tasks, basis, login_type):
    result = interpret({'taskList': tasks})
    assert result.status is KeepStatus.WAITING_HUMAN
    assert result.human_action.kind == 'COMPLETE_OFFICIAL_SIGNIN'
    assert result.metadata['signin_state'] == 'eligible'
    assert result.metadata['eligibility_basis'] == basis
    assert 'task_status' not in result.metadata
    assert result.metadata['status_shape'] == {'data': 'object', 'taskList': 'object', 'login': login_type,
                                              'status': 'unavailable' if login_type == 'missing' else 'missing'}


@pytest.mark.parametrize('data', [
    None, [], 'synthetic-sensitive', {}, {'taskList': None}, {'taskList': []},
    {'taskList': [{'ruleKey': 'login', 'status': 1}]},
    {'taskList': {'login': None}}, {'taskList': {'login': []}}, {'taskList': {'login': True}},
])
def test_unknown_containers_do_not_become_eligible_or_complete(data):
    result = interpret(data)
    assert result.status is KeepStatus.FAILED
    assert result.human_action is None
    assert 'status_shape' in result.metadata
    assert 'synthetic-sensitive' not in json.dumps(result.metadata)


@pytest.mark.parametrize('value', [None, True, False, 2, -1, 0.0, 1.0, '2', 'done', '01', ' 1', [], {}])
def test_unknown_present_status_never_defaults_to_eligible(value):
    result = interpret({'taskList': {'login': {'status': value}}})
    assert result.status is KeepStatus.FAILED
    assert 'task_status' not in result.metadata


@pytest.mark.parametrize('value,status', [(0, KeepStatus.WAITING_HUMAN), ('0', KeepStatus.WAITING_HUMAN), (1, KeepStatus.SUCCESS), ('1', KeepStatus.SUCCESS)])
def test_existing_explicit_zero_one_contract_is_unchanged(value, status):
    result = interpret({'taskList': {'login': {'status': value}}})
    assert result.status is status
    assert result.metadata['task_status'] == int(value)


def test_shape_diagnostic_does_not_copy_values_or_dynamic_keys():
    sentinel = 'synthetic-sensitive-value'
    data = {'currTime': sentinel, 'account_alias': sentinel, 'taskList': {'login': {'status': sentinel, sentinel: sentinel}}}
    result = interpret(data)
    assert result.metadata['status_shape'] == {'data': 'object', 'taskList': 'object', 'login': 'object', 'status': 'string'}
    assert sentinel not in json.dumps(result.metadata)
    assert 'account_alias' not in json.dumps(result.metadata)


def test_missing_after_submission_is_not_confirmation_and_does_not_resubmit():
    eligible = interpret({'taskList': {'login': {}}})
    page = FakePage([eligible, eligible, eligible, eligible])
    result = execute(page)
    assert result.status is KeepStatus.FAILED
    assert result.metadata['reason'] == 'completion_unconfirmed'
    assert page.challenges == 1 and len(page.answers) == 1 and page.checks == 4


def test_eligible_requires_later_explicit_one_for_success():
    page = FakePage([interpret({'taskList': {}}), interpret({'taskList': {'login': {'status': 1}}})])
    result = execute(page)
    assert result.status is KeepStatus.SUCCESS and result.metadata['automatic_action'] is True
    assert page.challenges == 1 and len(page.answers) == 1 and page.checks == 2


@pytest.mark.parametrize('status', [403, 429])
def test_rejection_precedes_eligible_envelope_and_no_action_occurs(status):
    page = FakePage([interpret({'taskList': {}}, http_status=status)])
    result = execute(page)
    assert result.status is not KeepStatus.SUCCESS
    assert page.challenges == 0 and page.answers == [] and page.checks == 1
    assert result.metadata['remote_status'] == status


def test_unknown_shape_stops_before_challenge():
    page = FakePage([interpret({'taskList': [{'ruleKey': 'login', 'status': 1}]})])
    result = execute(page)
    assert result.status is KeepStatus.FAILED and result.metadata['reason'] == 'status_unavailable'
    assert page.challenges == 0 and page.answers == []


def test_absent_data_is_distinguished_from_null_without_inventing_descendant_fields():
    result = run_keeper(FakeHttpClient(response(200, '{"code":"00000"}')))
    assert result.status is KeepStatus.FAILED
    assert result.metadata['status_shape'] == {'data': 'missing', 'taskList': 'unavailable', 'login': 'unavailable', 'status': 'unavailable'}
    assert interpret(None).metadata['status_shape']['data'] == 'null'


# Keep these as raw JSON: building a Python dict would erase the ambiguity.
DUPLICATE_PAYLOADS = [
    pytest.param('{"code":"00000","data":{"taskList":{"login":{"status":null},"login":{}}}}', id='duplicate_login'),
    pytest.param('{"code":"10001","code":"00000","data":{"taskList":{}}}', id='duplicate_code'),
    pytest.param('{"code":"00000","data":null,"data":{"taskList":{}}}', id='duplicate_data'),
    pytest.param('{"code":"00000","data":{"taskList":null,"taskList":{}}}', id='duplicate_task_list'),
    pytest.param('{"code":"00000","data":{"taskList":{"login":{"status":0,"status":1}}}}', id='duplicate_status'),
    pytest.param(r'{"code":"00000","data":{"taskList":{"login":{"status":0,"st\u0061tus":1}}}}', id='escaped_duplicate_status'),
    pytest.param('{"code":"00000","code":"10001","data":{"taskList":{}}}', id='reversed_code'),
    pytest.param('{"code":"00000","data":{"taskList":{"login":{"status":1,"status":0}}}}', id='reversed_status'),
    pytest.param('{"code":"00000","data":{"taskList":{"login":{"status":1,"status":1}}}}', id='identical_values'),
    pytest.param(r'{"code":"00000","data":{"taskList":{"login":null,"lo\u0067in":{}}}}', id='escaped_duplicate_parent'),
    pytest.param('{"code":"00000","data":{"taskList":{"login":{"status":1}},"extra":[{"synthetic-private-key":"synthetic-private-first","synthetic-private-key":"synthetic-private-last"}]}}', id='nested_array_object'),
]


@pytest.mark.parametrize('raw', DUPLICATE_PAYLOADS)
def test_duplicate_members_reject_entire_response_before_actions(raw):
    result = run_keeper(FakeHttpClient(response(200, raw)))
    assert result.status is KeepStatus.FAILED
    assert result.human_action is None
    assert result.metadata == {'error_type': 'protocol', 'protocol_reason': 'duplicate_json_member', 'signin_state': 'unknown'}
    assert 'synthetic-private' not in result.message + json.dumps(result.metadata)
    page = FakePage([result])
    flow = execute(page)
    assert flow.status is KeepStatus.FAILED
    assert flow.metadata['protocol_reason'] == 'duplicate_json_member'
    assert flow.metadata['signin_state'] == 'unknown'
    assert page.checks == 1 and page.challenges == 0 and page.answers == []


def test_same_key_in_distinct_objects_and_unique_escaped_key_remain_valid():
    raw = r'{"code":"00000","data":{"taskList":{"login":{"st\u0061tus":1},"post_view":{"status":0}}}}'
    result = run_keeper(FakeHttpClient(response(200, raw)))
    assert result.status is KeepStatus.SUCCESS and result.metadata['task_status'] == 1


@pytest.mark.parametrize('status', [403, 429])
def test_http_rejection_still_precedes_duplicate_json_parsing(status):
    raw = '{"code":"10001","code":"00000","data":{"taskList":{"login":{}}}}'
    result = run_keeper(FakeHttpClient(response(status, raw)))
    page = FakePage([result])
    flow = execute(page)
    assert flow.status is not KeepStatus.SUCCESS
    assert flow.metadata['remote_status'] == status
    assert page.checks == 1 and page.challenges == 0 and page.answers == []
