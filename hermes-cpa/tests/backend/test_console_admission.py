"""Owner admission and terminal receipts without starting a native process."""
from types import SimpleNamespace

import pytest

from hermes_cpa.settings import CpaSettings
from hermes_cpa.supervisor import CONSOLE_JOBS, CpaSupervisor


@pytest.fixture
def owner(tmp_path):
    supervisor = CpaSupervisor(CpaSettings(tmp_path), mode='managed', policy=lambda: True,
                               console=lambda *_: SimpleNamespace())
    # Leave accepted jobs queued so admission, not scheduling, is under test.
    supervisor._worker = SimpleNamespace(is_alive=lambda: True)
    return supervisor


def intent(owner, request_id='generated-job'):
    return {'action': 'console.execute', 'request_id': request_id,
            'expected_generation': owner.generation, 'lease_id': 'generated-lease',
            'review_id': 'generated-review', '_scope': {'principal': 'generated'}}


@pytest.mark.parametrize('grant', [None, {}, {'token': 'generated-revoked'}])
def test_supervisor_rejects_host_grant_before_ledger_or_queue(owner, monkeypatch, grant):
    import hermes_cpa.operation_guard as guard
    checked = []
    args = intent(owner)

    def denied(value, accepted_intent, *, timeout):
        checked.append((value, accepted_intent, timeout))
        return False

    monkeypatch.setattr(guard, 'revalidate_grant', denied)
    for n in range(CONSOLE_JOBS + 1):
        rejected = owner.submit(dict(args, request_id=f'revoked-{n}', _host_grant=grant))
        assert rejected == {'ok': False, 'code': 'POLICY_LOCKED'}
    assert not owner._operations and owner._queue.empty()
    assert checked == [(grant, dict(args, request_id=f'revoked-{n}'), 1.)
                       for n in range(CONSOLE_JOBS + 1)]
    # Denied hosted requests did not consume any of the accepted-job slots.
    for n in range(CONSOLE_JOBS):
        assert owner.submit(intent(owner, f'native-{n}'))['ok']
    assert owner.submit(intent(owner, 'overflow'))['code'] == 'BUSY'


def test_supervisor_validates_exact_hosted_intent_and_preserves_native_cli(owner, monkeypatch):
    import hermes_cpa.operation_guard as guard
    checked = []

    def allowed(grant, accepted_intent, *, timeout):
        checked.append((grant, accepted_intent, timeout))
        return True

    monkeypatch.setattr(guard, 'revalidate_grant', allowed)
    args = intent(owner)
    grant = {'token': 'generated-current'}
    hosted = owner.submit(dict(args, _host_grant=grant))
    assert hosted['ok'] and hosted['state'] == 'pending'
    assert checked == [(grant, args, 1.)]
    native = owner.submit({'action': 'start', 'request_id': 'native-cli',
                           'expected_generation': owner.generation})
    assert native['ok'] and len(checked) == 1


@pytest.mark.parametrize('code', ['OUTCOME_UNKNOWN', 'RECONCILE_REQUIRED'])
def test_supervisor_never_labels_uncertain_console_outcomes_failed(owner, code):
    args = intent(owner)
    assert owner.submit(args)['ok']
    owner._finish(args['request_id'], {'ok': False, 'code': code, 'result_ready': True})
    receipt = owner.submit(args)
    assert receipt['state'] == 'unknown' and receipt['code'] == code
    assert owner._queue.qsize() == 1


def test_supervisor_keeps_observed_withheld_response_succeeded(owner):
    args = intent(owner)
    assert owner.submit(args)['ok']
    owner._finish(args['request_id'], {'ok': True, 'code': 'RESPONSE_WITHHELD', 'result_ready': True})
    receipt = owner.submit(args)
    assert receipt['state'] == 'succeeded' and receipt['code'] == 'RESPONSE_WITHHELD'
    assert owner._queue.qsize() == 1


def test_supervisor_unexpected_job_exception_remains_unknown(owner):
    args = intent(owner)
    assert owner.submit(args)['ok']
    rejected = []

    def dispatch(_):
        raise RuntimeError('GENERATED_PRIVATE_AFTER_DISPATCH')

    owner.console = SimpleNamespace(execute_job=dispatch,
                                    job_rejected=lambda args, code: rejected.append(code))
    owner._console_job(args, True)
    receipt = owner.submit(args)
    assert receipt['state'] == 'unknown' and receipt['code'] == 'OUTCOME_UNKNOWN'
    assert rejected == ['OUTCOME_UNKNOWN']
    assert 'GENERATED_PRIVATE' not in str(receipt)


@pytest.mark.parametrize('raw', [None, {}, {'ok': False, 'code': 'private detail'}])
def test_supervisor_invalid_result_projection_cannot_claim_failed_native_operation(owner, raw):
    args = intent(owner)
    assert owner.submit(args)['ok']
    owner.console = SimpleNamespace(execute_job=lambda _: raw)
    owner._console_job(args, True)
    receipt = owner.submit(args)
    assert receipt['state'] == 'unknown' and receipt['code'] == 'OUTCOME_UNKNOWN'
