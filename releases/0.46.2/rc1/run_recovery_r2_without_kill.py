"""Use the existing recovery UI scenario with no forced timeout termination."""
import runpy,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
run=subprocess.run
def supervised(*args,**kwargs):
    command=args[0] if args else kwargs.get('args',[])
    if '--collection-maintenance-smoke' in command:kwargs.pop('timeout',None)
    return run(*args,**kwargs)
subprocess.run=supervised
sys.path.insert(0,str(ROOT/'tools'))
script=ROOT/'tools/verify_recovery_exe.py'
sys.argv=[str(script),'--exe',str(ROOT/'交付_0.46.2_rc1/补货助手/补货助手.exe'),
          '--out',str(ROOT/'qa/deletion_0462_final/exe-recovery-r2')]
print('Supervision: existing recovery actions; wait for normal exit; no forced timeout kill',flush=True)
runpy.run_path(str(script),run_name='__main__')

