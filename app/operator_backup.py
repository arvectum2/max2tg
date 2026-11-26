"""Trusted first-checkpoint offline backup and encrypted restore rehearsal.
No launch, secret export or overwrite. Operator must stop all writers.
Plaintext capture and restore require protected private storage; this is not
an independent custody or production host attestation.
"""
from app.coordinated_backup import capture_delivery_state,_disjoint
from app.recovery_manifest import RecoveryAnchor,_private_dir
from app.encrypted_storage import create_key
from app.encrypted_backup import encrypt_snapshot,materialize_encrypted


def first_checkpoint(registry,workers,*,snapshots,archive,keys,anchor_dir,restores,provisioning_stopped=False):
    if provisioning_stopped is not True:raise PermissionError('Provisioning must be stopped')
    roots=[_private_dir(p) for p in (snapshots,archive,keys,anchor_dir,restores)]
    states=[_private_dir(registry),*(_private_dir(p) for p in workers.values())]
    _disjoint(roots+states)
    if any(any(p.iterdir()) for p in roots):raise PermissionError('Fresh separate backup roots required')
    snapshots,archive,keys,anchor_dir,restores=roots
    anchor=RecoveryAnchor(anchor_dir)
    snap,record=capture_delivery_state(registry,workers,snapshots,anchor,name='snapshot-001',provisioning_stopped=True)
    key=keys/'backup.key';create_key(key,'backup:'+record['epoch'])
    sealed=archive/'snapshot.enc';e=encrypt_snapshot(snap,sealed,key,anchor)
    restored,verified=materialize_encrypted(sealed,key,restores,anchor,name='snapshot-restore')
    if e['members']!=verified['members'] or e['generation']!=verified['generation']:raise RuntimeError('Backup generation mismatch')
    return {'verified':True,'members':e['members'],'bytes':e['bytes'],'generation':e['generation'],'structural_only':True,'live_launch_authorized':False},restored
