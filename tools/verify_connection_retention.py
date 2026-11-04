"""Fixed 0.36.0 comparison for connection lifetime changes; isolated QA only."""
import json
import verify_parameter_workspaces as verification

verification.BASE = '2487601d9a3d5830704f4dbab8e6dc56961a1fd4'
_freeze = verification.freeze


def freeze(q):
    _freeze(q)
    baseline = json.loads((q / 'baseline.json').read_text('utf-8'))
    for name in ('交付_0.35.1_rc1', '交付_0.36.0_rc1'):
        for path in (verification.ROOT / name).rglob('*'):
            if path.is_file():
                baseline['protected'][str(path)] = verification.sha(path)
    verification.save(q / 'baseline.json', baseline)


verification.freeze = freeze
if __name__ == '__main__':
    verification.main()
