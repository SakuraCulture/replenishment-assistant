"""0.38.1 strict comparison against 0.38.0, protecting both old candidates."""
import json
import verify_parameter_workspaces as verification

verification.BASE = 'fdd3111fcbc7f91e556d7cae0afb867f0c8c4213'
original_freeze=verification.freeze


def freeze(q):
    original_freeze(q)
    baseline=json.loads((q/'baseline.json').read_text('utf-8'))
    for name in ('交付_0.37.0_rc2','交付_0.38.0_rc1'):
        for path in (verification.ROOT/name).rglob('*'):
            if path.is_file():baseline['protected'][str(path)]=verification.sha(path)
    verification.save(q/'baseline.json',baseline)


verification.freeze=freeze
if __name__=='__main__':verification.main()
