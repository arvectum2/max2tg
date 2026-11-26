"""Exact observed nftables policy verifier, not authority to launch or apply.
Trusted caller obtains the entire ruleset in the intended dedicated namespace.
No host changes, no commands, no acceptance of partial table listings.
"""
from app.linux_egress_policy import BrokerPins,MAX_TTL
from app.egress_policy import EgressDenied


def _require(ok):
    if not ok:raise EgressDenied('Kernel egress policy mismatch')


def _match(protocol,field,right):return {'match':{'op':'==','left':{'payload':{'protocol':protocol,'field':field}},'right':right}}


def verify_ruleset(pins,observed,*,now=None):
    _require(type(pins) is BrokerPins);pins=pins.validated(now=now);remaining=pins.remaining(now=now)
    _require(type(observed) is dict and set(observed)=={'nftables'})
    items=observed['nftables'];_require(type(items) is list and len(items)==11)
    _require(type(items[0]) is dict and set(items[0])=={'metainfo'} and type(items[0]['metainfo']) is dict and items[0]['metainfo'].get('json_schema_version')==1)
    table={'family':'inet','name':'m2tg_egress'};base={'family':'inet','table':'m2tg_egress'}
    seen_table=False;seen_set=False;chains=set();rules={k:[] for k in ('input','output','forward')}
    for item in items[1:]:
        _require(type(item) is dict and len(item)==1)
        kind,v=next(iter(item.items()));_require(type(v) is dict and type(v.get('handle')) is int and v['handle']>0)
        v={k:x for k,x in v.items() if k!='handle'}
        if kind=='table':
            _require(not seen_table and v==table);seen_table=True
        elif kind=='chain':
            name=v.get('name');_require(type(name) is str and name in rules and name not in chains and v==dict(base,name=name,type='filter',hook=name,prio=0,policy='drop'));chains.add(name)
        elif kind=='set':
            _require(not seen_set);seen_set=True;elements=v.pop('elem',None)
            _require(v==dict(base,name='api_ipv4',type='ipv4_addr',flags=['timeout']) and type(elements) is list)
            addresses=set()
            for e in elements:
                _require(type(e) is dict and set(e)=={'elem'} and type(e['elem']) is dict);e=e['elem']
                _require(set(e)=={'val','timeout','expires'} and type(e['val']) is str and e['val'] not in addresses and type(e['timeout']) is int and type(e['expires']) is int and 0<e['expires']<=e['timeout']<=MAX_TTL and e['expires']<=remaining)
                addresses.add(e['val'])
            _require(addresses==set(pins.max_text+pins.telegram))
        elif kind=='rule':
            chain=v.get('chain');expr=v.pop('expr',None)
            _require(type(chain) is str and chain in rules and v==dict(base,chain=chain) and type(expr) is list)
            normalized=[]
            for e in expr:
                _require(type(e) is dict and len(e)==1)
                if 'counter' in e:
                    count=e['counter'];_require(type(count) is dict and set(count)=={'packets','bytes'} and all(type(n) is int and n>=0 for n in count.values()));normalized.append({'counter':None})
                else:normalized.append(e)
            rules[chain].append(normalized)
        else:_require(False)
    _require(seen_table and seen_set and chains==set(rules))
    drop=[{'counter':None},{'drop':None}];allow=[{'counter':None},{'accept':None}]
    out=[_match('ip','daddr','@api_ipv4'),_match('tcp','dport',443)]
    inp=[_match('ip','saddr','@api_ipv4'),_match('tcp','sport',443)]
    # nft JSON versions differ in state set order and single-state equality spelling.
    for states in (['established','new'],['new','established']):
        if rules['output']==[out+[{'match':{'op':'==','left':{'ct':{'key':'state'}},'right':{'set':states}}}]+allow,drop]:break
    else:_require(False)
    _require(any(rules['input']==[inp+[{'match':{'op':op,'left':{'ct':{'key':'state'}},'right':'established'}}]+allow,drop] for op in ('in','==')) and rules['forward']==[drop])
    return {'verified':True,'pins':len(pins.max_text)+len(pins.telegram),'live_launch_authorized':False}


def prepare_fresh_policy(pins,*,observe,install,clock=None):
    """Trusted orchestration callback in an already isolated namespace only.

    Observe the complete empty kernel ruleset, install once, re-read the full
    ruleset. Never flush/adopt a foreign table or retry an ambiguous install.
    This does not create or attest the network namespace itself.
    """
    import time
    clock=clock or time.time
    _require(type(pins) is BrokerPins);pins=pins.validated(now=clock())
    before=observe()
    _require(type(before) is dict and set(before)=={'nftables'} and type(before['nftables']) is list and len(before['nftables'])==1)
    item=before['nftables'][0];_require(type(item) is dict and set(item)=={'metainfo'} and type(item['metainfo']) is dict and item['metainfo'].get('json_schema_version')==1)
    install(pins.render(now=clock()))
    return verify_ruleset(pins,observe(),now=clock())
