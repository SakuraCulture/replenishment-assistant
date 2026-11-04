"""0.36 strict comparison with the actual pre-change main commit."""
import json
import verify_parameter_workspaces as verification

verification.BASE = '35b33de022be33958ec57851f722130feb328d7a'
_freeze = verification.freeze
_compare = verification.compare


def freeze(q):
    _freeze(q)
    baseline = json.loads((q / 'baseline.json').read_text('utf-8'))
    for name in ('交付_0.35.0_rc1', '交付_0.35.1_rc1'):
        for path in (verification.ROOT / name).rglob('*'):
            if path.is_file():
                baseline['protected'][str(path)] = verification.sha(path)
    verification.save(q / 'baseline.json', baseline)


def compare(q, after_name='after'):
    for name in ('before', after_name):
        value = json.loads((q / name / 'capture.json').read_text('utf-8'))
        if value.get('PYTHONHASHSEED') != '0':
            raise ValueError('Use matching PYTHONHASHSEED=0 before/after processes')
    return _compare(q, after_name)


verification.freeze = freeze
verification.compare = compare
if __name__ == '__main__':
    verification.main()
