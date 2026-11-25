"""Explicit encrypted credentials; no automatic legacy plaintext fallback."""
import json,re
from pathlib import Path
from app.worker_secrets import _validate
from app.encrypted_storage import install_envelope,open_envelope,rotate_envelope


def _worker_path(folder):
    p=Path(folder)
    if (p/'max.json').exists() or (p/'max.json').is_symlink():raise PermissionError('Plaintext worker secret prohibited in encrypted mode')
    return p/'max.enc'


def install_encrypted_worker(folder,sid,values,key_path):
    data=json.dumps(_validate(sid,values),separators=(',',':')).encode()
    install_envelope(_worker_path(folder),data,key_path,'max:'+sid,'credentials')


def load_encrypted_worker(folder,sid,key_path):
    data=open_envelope(_worker_path(folder),key_path,'max:'+sid,'credentials')
    if len(data)>32768:raise ValueError('Worker secret limit')
    try:return _validate(sid,json.loads(data))
    except (TypeError,ValueError,UnicodeError):raise ValueError('Invalid encrypted worker secret') from None


def rotate_encrypted_worker(folder,sid,old,new):
    load_encrypted_worker(folder,sid,old)
    return rotate_envelope(_worker_path(folder),old,new,'max:'+sid,'credentials')


def _token(bot_id,token):
    if type(bot_id) is not int or not 0<bot_id<2**63 or type(token) is not str or len(token)>4096 or not re.fullmatch(str(bot_id)+r':[A-Za-z0-9_-]+',token):raise ValueError('Invalid bot credential identity')
    return token


def install_encrypted_bot(folder,bot_id,token,key_path):
    install_envelope(Path(folder)/'telegram.enc',_token(bot_id,token).encode(),key_path,'telegram:'+str(bot_id),'bot-token')


def load_encrypted_bot(folder,bot_id,key_path):
    data=open_envelope(Path(folder)/'telegram.enc',key_path,'telegram:'+str(bot_id),'bot-token')
    try:return _token(bot_id,data.decode('ascii'))
    except (ValueError,UnicodeError):raise ValueError('Invalid encrypted bot secret') from None


def rotate_encrypted_bot(folder,bot_id,old,new):
    load_encrypted_bot(folder,bot_id,old)
    return rotate_envelope(Path(folder)/'telegram.enc',old,new,'telegram:'+str(bot_id),'bot-token')
