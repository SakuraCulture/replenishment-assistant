"""Fixed 0.36.1 baseline, isolated comparison for global connection indicators."""
import json
import verify_parameter_workspaces as verification
verification.BASE='b6a64a97bb96a29ec9248c3292ba2837c4c1f5d2'
_freeze=verification.freeze
def freeze(q):
    _freeze(q)
    baseline=json.loads((q/'baseline.json').read_text('utf-8'))
    for name in ['交付_0.36.0_rc1','交付_0.36.1_rc2']:
        for path in (verification.ROOT/name).rglob('*'):
            if path.is_file():baseline['protected'][str(path)]=verification.sha(path)
    verification.save(q/'baseline.json',baseline)
verification.freeze=freeze
if __name__=='__main__':verification.main()
