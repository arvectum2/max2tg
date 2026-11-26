import socket,time,json
from unittest.mock import AsyncMock
import pytest
from app.linux_egress_policy import BrokerPins,read_bundle
from app.egress_policy import EgressDenied


def bundle():return {'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],'expires_at':int(time.time())+60}


@pytest.mark.parametrize('patch',[{'max-text':['127.0.0.1']},{'telegram':['169.254.169.254']},{'max-text':['8.8.8.8','8.8.8.8']},{'telegram':['8.8.8.8']},{'telegram':['2001:4860:4860::8888']},{'max-text':['8.8.8.8; counter accept']},{'max-text':[]},{'max-text':['8.8.8.8']*17},{'expires_at':0},{'expires_at':True},{'expires_at':2**63},{'extra':[] }])
def test_invalid_bundle_denied(patch):
    with pytest.raises(EgressDenied):BrokerPins.parse(bundle()|patch)


def test_policy_default_drop_expiry_and_exact_pins():
    p=BrokerPins.parse(bundle());text=p.render()
    assert text.count('policy drop;')==3 and text.count('counter accept')==2
    assert '8.8.8.8 timeout ' in text and '9.9.9.9 timeout ' in text and 'flags timeout' in text
    assert 'ip daddr @api_ipv4 tcp dport 443 ct state { new, established }' in text
    assert 'ip saddr @api_ipv4 tcp sport 443 ct state established' in text
    assert 'udp' not in text and 'flush' not in text and 'ip6' not in text
    with pytest.raises(EgressDenied):p.render(now=p.expires_at)
    with pytest.raises(EgressDenied):read_bundle(json.dumps(bundle()).replace('"telegram":','"telegram": [], "telegram":'))


@pytest.mark.asyncio
async def test_connector_exact_roles_and_numeric_dial():
    p=BrokerPins.parse(bundle());dial=AsyncMock(return_value='socket')
    assert await p.connect('max-text','api.oneme.ru:443',dial=dial)=='socket'
    dial.assert_awaited_once_with('8.8.8.8',443,family=socket.AF_INET,flags=socket.AI_NUMERICHOST)
    dial.reset_mock()
    with pytest.raises(EgressDenied):await p.connect('max-text','api.telegram.org:443',dial=dial)
    with pytest.raises(EgressDenied):await BrokerPins(p.max_text,p.telegram,0).connect('telegram','api.telegram.org:443',dial=dial)
    dial.assert_not_awaited()
    assert await p.connect('telegram','api.telegram.org:443',dial=dial)=='socket'
    dial.assert_awaited_once_with('9.9.9.9',443,family=socket.AF_INET,flags=socket.AI_NUMERICHOST)
