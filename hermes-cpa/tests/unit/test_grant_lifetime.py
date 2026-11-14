"""key_cmd grants report their real lifetime so Hermes' token cache never outlives them."""
import json
import time

from hermes_cpa.subscription_client import grant_lifetime


def _token(expiry):
    record = json.dumps(['consumer', ['identity'], 0, expiry, 'nonce'], separators=(',', ':')).encode().hex()
    return record + '.signature'


def test_lifetime_is_the_remaining_validity():
    lifetime = grant_lifetime(_token(time.time() + 30))
    assert 28 <= lifetime <= 30


def test_expired_or_malformed_grant_reports_minimum():
    assert grant_lifetime(_token(time.time() - 5)) == 1
    assert grant_lifetime('not-a-grant') == 1
