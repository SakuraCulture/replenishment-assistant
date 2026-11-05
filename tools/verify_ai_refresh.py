"""0.38.0 comparison against the actual pre-change main, including old deliveries."""
import json
import verify_parameter_workspaces as verification

verification.BASE = 'ecdc9e197d1c748eae1c78fb434fda4a7a77a9e5'
original_freeze = verification.freeze


def freeze(q):
    original_freeze(q)
    baseline = json.loads((q/'baseline.json').read_text('utf-8'))
    for name in ('交付_0.37.0_rc1', '交付_0.37.0_rc2'):
        for path in (verification.ROOT/name).rglob('*'):
            if path.is_file(): baseline['protected'][str(path)] = verification.sha(path)
    verification.save(q/'baseline.json', baseline)


verification.freeze = freeze
if __name__ == '__main__': verification.main()
