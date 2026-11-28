from copy import deepcopy
import pytest
from app.linux_egress_policy import BrokerPins
from app.linux_namespace_gate import verify_ruleset
from app.egress_policy import EgressDenied

NOW=1000
PINS=BrokerPins.parse({'max-text':['8.8.8.8'],'telegram':['9.9.9.9'],'expires_at':1060},now=NOW)


def sample():
    base={'family':'inet','table':'m2tg_egress'};counter={'counter':{'packets':2,'bytes':100}};drop=[counter,{'drop':None}]
    def match(proto,field,right):return {'match':{'op':'==','left':{'payload':{'protocol':proto,'field':field}},'right':right}}
    def rule(chain,expr,handle):return {'rule':dict(base,chain=chain,expr=expr,handle=handle)}
    return {'nftables':[{'metainfo':{'json_schema_version':1}}, {'table':{'family':'inet','name':'m2tg_egress','handle':1}},*[{'chain':dict(base,name=n,type='filter',hook=n,prio=0,policy='drop',handle=i+1)} for i,n in enumerate(('output','input','forward'))],{'set':dict(base,name='api_ipv4',type='ipv4_addr',flags=['timeout'],handle=4,elem=[{'elem':{'val':ip,'timeout':60,'expires':55}} for ip in ('8.8.8.8','9.9.9.9')])},rule('output',[match('ip','daddr','@api_ipv4'),match('tcp','dport',443),{'match':{'op':'==','left':{'ct':{'key':'state'}},'right':{'set':['established','new']}}},counter,{'accept':None}],6),rule('output',drop,7),rule('input',[match('ip','saddr','@api_ipv4'),match('tcp','sport',443),{'match':{'op':'in','left':{'ct':{'key':'state'}},'right':'established'}},counter,{'accept':None}],8),rule('input',drop,9),rule('forward',drop,10)]}


def test_exact_ruleset():
    v=sample();old=deepcopy(v);assert verify_ruleset(PINS,v,now=NOW)=={'verified':True,'pins':2,'live_launch_authorized':False};assert v==old
    v['nftables'][8]['rule']['expr'][2]['match']['op']='==';assert verify_ruleset(PINS,v,now=NOW)['verified']


@pytest.mark.parametrize('drift',['extra','missing','chain','port','state','pin','expiry','duplicate','order','counter','metadata','foreign','expired'])
def test_ruleset_drift_denied(drift):
    v=sample();i=v['nftables'];now=NOW
    if drift=='extra':i.append(deepcopy(i[-1]))
    elif drift=='missing':i.pop()
    elif drift=='chain':i[2]['chain']['policy']='accept'
    elif drift=='port':i[6]['rule']['expr'][1]['match']['right']=80
    elif drift=='state':i[6]['rule']['expr'][2]['match']['right']['set'].append('related')
    elif drift=='pin':i[5]['set']['elem'][0]['elem']['val']='1.1.1.1'
    elif drift=='expiry':i[5]['set']['elem'][0]['elem']['expires']=61
    elif drift=='duplicate':i[5]['set']['elem'].append(deepcopy(i[5]['set']['elem'][0]))
    elif drift=='order':i[6],i[7]=i[7],i[6]
    elif drift=='counter':i[6]['rule']['expr'][3]['counter']['bytes']=-1
    elif drift=='metadata':i[0]['metainfo']['json_schema_version']=2
    elif drift=='foreign':i[1]['table']['name']='foreign'
    else:now=1060
    with pytest.raises(EgressDenied):verify_ruleset(PINS,v,now=now)


def test_fresh_install_only_after_empty_observation():
    from app.linux_namespace_gate import prepare_fresh_policy
    empty={'nftables':[{'metainfo':{'json_schema_version':1}}]};calls=[];observations=iter([empty,sample()])
    assert prepare_fresh_policy(PINS,observe=lambda:next(observations),install=calls.append,clock=lambda:NOW)['verified']
    assert calls==[PINS.render(now=NOW)]
    with pytest.raises(EgressDenied):prepare_fresh_policy(PINS,observe=sample,install=calls.append,clock=lambda:NOW)
    assert len(calls)==1


def test_failed_install_never_retried_and_readback_required():
    from app.linux_namespace_gate import prepare_fresh_policy
    empty={'nftables':[{'metainfo':{'json_schema_version':1}}]};calls=[]
    def fail(rules):calls.append(rules);raise TimeoutError('ambiguous install')
    with pytest.raises(TimeoutError):prepare_fresh_policy(PINS,observe=lambda:empty,install=fail,clock=lambda:NOW)
    assert len(calls)==1
    with pytest.raises(EgressDenied):prepare_fresh_policy(PINS,observe=lambda:empty,install=calls.append,clock=lambda:NOW)
    assert len(calls)==2
