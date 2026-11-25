"""Suppress empty CONTROL envelopes; preserve meaningful system notices."""

def system_notice(attach):
    if not isinstance(attach,dict) or attach.get('_type')!='CONTROL' or attach.get('event')!='system':
        return ''
    for key in ('message','shortMessage'):
        value=attach.get(key)
        if isinstance(value,str) and value.strip():return value.strip()
    return ''

def is_membership_event(attaches, text='', link=None):
    # Historical public name: filter all pure service envelopes, not just
    # membership, unless they contain actual system notice text.
    return (not text and not link and isinstance(attaches,list) and bool(attaches)
            and all(isinstance(a,dict) and a.get('_type')=='CONTROL'
                    and not system_notice(a) for a in attaches))

def is_membership_row(row):
    return is_membership_event(row.get('attaches'),row.get('text'),row.get('link'))
