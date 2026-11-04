"""Compare against the actual 0.35.0 HEAD, including unchanged old delivery."""
import json
import verify_parameter_workspaces as verification

verification.BASE = '3ca57141ed7b35f3c6e61708b01eaad54ec469f7'
original_freeze = verification.freeze
original_compare = verification.compare


def freeze(q):
    original_freeze(q)
    baseline = json.loads((q / 'baseline.json').read_text('utf-8'))
    for path in (verification.ROOT / '交付_0.35.0_rc1').rglob('*'):
        if path.is_file(): baseline['protected'][str(path)] = verification.sha(path)
    extra = verification.ROOT / 'docs/历史README_截至0.33.0.md'
    if extra.exists(): baseline['protected'][str(extra)] = verification.sha(extra)
    verification.save(q / 'baseline.json', baseline)


verification.freeze = freeze


def compare(q, after_name='after'):
    a = json.loads((q/'before/capture.json').read_text('utf-8'))
    b = json.loads((q/after_name/'capture.json').read_text('utf-8'))
    if a.get('PYTHONHASHSEED') != '0' or b.get('PYTHONHASHSEED') != '0':
        raise ValueError('Use the before/after commands so both processes run with PYTHONHASHSEED=0')
    return original_compare(q, after_name)


verification.compare = compare
if __name__ == '__main__': verification.main()
