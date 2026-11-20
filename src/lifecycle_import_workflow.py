"""Preview a selected import range through either document preparation path."""
import lifecycle_batch
import payment_matching
from lifecycle_stages import TITLES


def prepare(path, documents, scope, payment_index, source_company, remember,
            progress=lambda _: None, cancel=None):
    """Build the existing plan. Confirmation and commit remain separate."""
    if payment_index:
        return payment_matching.prepare(
            path, documents, scope, payment_index, source_company, remember, progress, cancel,
        )
    return lifecycle_batch.prepare(path, documents, scope, progress, cancel)


def review_scope(plan, documents, excluded):
    """Count the full saved batch and report opted-out sources in input order."""
    omitted = [str(item.get('path') or item['info']['name']) + '：' + item['message']
               for item in excluded]
    omitted.extend(
        doc['scan']['files'][0]['name'] + ' / ' + TITLES[doc['scan']['stage']]
        for doc in documents if not doc['selected']
    )
    return len(plan['plans']), sum(p['payload']['pending'] for p in plan['plans']), omitted
