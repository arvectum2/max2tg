"""Read-only aggregate Linux process/storage observations in disposable containers."""
import os,stat,json
from pathlib import Path


def observe(roots):
    # PID 1 is the actual fixture runtime, not this docker exec helper.
    fds=0;sockets=0
    for p in Path('/proc/1/fd').iterdir():
        try:target=os.readlink(p)
        except FileNotFoundError:continue
        fds+=1;sockets+=target.startswith('socket:[')
    db_bytes=0;db_files=0;db_main_bytes=0;wal_bytes=0;shm_bytes=0
    for root in roots:
        for p in Path(root).glob('**/*'):
            if not (p.name.endswith('.sqlite3') or p.name.endswith('.sqlite3-wal') or p.name.endswith('.sqlite3-shm')):continue
            try:s=p.stat(follow_symlinks=False)
            except FileNotFoundError:continue
            if stat.S_ISREG(s.st_mode):
                db_bytes+=s.st_size;db_files+=1
                if p.name.endswith('-wal'):wal_bytes+=s.st_size
                elif p.name.endswith('-shm'):shm_bytes+=s.st_size
                else:db_main_bytes+=s.st_size
    status=dict(line.split(':',1) for line in Path('/proc/1/status').read_text().splitlines() if ':' in line)
    return {'fds':fds,'sockets':sockets,'threads':int(status['Threads']),'rss_kib':int(status['VmRSS'].split()[0]),'db_bytes':db_bytes,'db_files':db_files,'db_main_bytes':db_main_bytes,'wal_bytes':wal_bytes,'shm_bytes':shm_bytes}

if __name__=='__main__':
    import sys
    print(json.dumps(observe(sys.argv[1:]),sort_keys=True))


def summarize_resources(checkpoints,*,cycles):
    if len(checkpoints)!=cycles+2:raise ValueError('Missing resource checkpoints')
    roles=set(checkpoints[0]['roles']);fields={'fds','sockets','threads','rss_kib','db_bytes','db_files','db_main_bytes','wal_bytes','shm_bytes'}
    for c in checkpoints:
        if set(c['roles'])!=roles:raise ValueError('Changed runtime cohort')
        for row in c['roles'].values():
            if set(row)!=fields or any(type(v) is not int or v<0 for v in row.values()) or row['sockets']>row['fds'] or row['db_bytes']!=row['db_main_bytes']+row['wal_bytes']+row['shm_bytes']:raise ValueError('Invalid resource observation')
    for i,c in enumerate(checkpoints[1:-1]):
        if not 32+90*i<=c['at']<=50+90*i:raise ValueError('Missing idle checkpoint')
    # Compare like-for-like idle points. Baseline/final can have live IPCs.
    first=checkpoints[1]['roles'];last=checkpoints[-2]['roles']
    for role in roles:
        if last[role]['fds']-first[role]['fds']>8 or last[role]['sockets']-first[role]['sockets']>8 or last[role]['threads']-first[role]['threads']>2:raise ValueError('Idle descriptor/thread growth bound exceeded')
    totals=[{'at':c['at'],**{k:sum(r[k] for r in c['roles'].values()) for k in sorted(fields)}} for c in checkpoints]
    return {'checkpoints':totals,'max_idle_fd_growth_per_role':max(last[r]['fds']-first[r]['fds'] for r in roles),'max_idle_socket_growth_per_role':max(last[r]['sockets']-first[r]['sockets'] for r in roles),'max_idle_thread_growth_per_role':max(last[r]['threads']-first[r]['threads'] for r in roles)}
