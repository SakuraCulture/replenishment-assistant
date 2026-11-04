"""Frozen 0.36.2 business comparison for scheduled replenishment."""
import json
import verify_parameter_workspaces as verification
verification.BASE='6e24b697f0686bab73b2565f45170895c92c2a76'
_freeze=verification.freeze
def freeze(q):
    _freeze(q)
    value=json.loads((q/'baseline.json').read_text('utf-8'))
    for name in ['交付_0.36.1_rc2','交付_0.36.2_rc1']:
        for p in (verification.ROOT/name).rglob('*'):
            if p.is_file():value['protected'][str(p)]=verification.sha(p)
    verification.save(q/'baseline.json',value)
verification.freeze=freeze
if __name__=='__main__':verification.main()
