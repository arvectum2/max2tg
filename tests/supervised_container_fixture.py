"""Synthetic process fixture, no credentials or messenger network calls."""
import os,sys,time,signal
from pathlib import Path

def main():
    assert os.getuid()==65532 and len(sys.argv[1])==24
    assert not Path('/registry').exists() and not Path('/var/run/docker.sock').exists()
    assert not any(k in os.environ for k in ('TG_BOT_TOKEN','MAX_TOKEN'))
    state=Path('/state');heartbeat=state/'heartbeat';heartbeat.unlink(missing_ok=True)
    # Persistent counter records actual invocations, not Docker start requests.
    count=state/'starts';n=int(count.read_text())+1 if count.exists() else 1;count.write_text(str(n))
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    while True:
        if (state/'crash').exists():
            (state/'crash').unlink();raise SystemExit(17)
        heartbeat.touch();time.sleep(.15)

if __name__=='__main__':main()
