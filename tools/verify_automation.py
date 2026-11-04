"""0.35 fixed baseline comparison; writes only to the explicit isolated QA root."""
import sys
import json
import verify_parameter_workspaces as verification

verification.BASE = 'f0e3765d5f317cf45d708f3774bee90652bc5dc2'
original_freeze = verification.freeze
def protect_current_delivery(q):
    baseline = json.loads((q / 'baseline.json').read_text('utf-8'))
    for folder in [verification.ROOT / '交付_0.34.0_rc2']:
        for path in folder.rglob('*'):
            if path.is_file(): baseline['protected'].setdefault(str(path), verification.sha(path))
    for path in verification.ROOT.glob('*0.34.0*zip'):
        baseline['protected'].setdefault(str(path), verification.sha(path))
    verification.save(q / 'baseline.json', baseline)
def freeze(q):
    original_freeze(q)
    protect_current_delivery(q)
verification.freeze = freeze
if __name__ == '__main__':
    if '--qa' not in sys.argv:
        sys.argv.extend(['--qa', str(verification.ROOT / 'qa/v0350')])
    verification.main()
