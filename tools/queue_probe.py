"""QA-only spawn target: deterministic uneven store timings, then real calculation."""
import time


def calculate(loaded,store,settings,profile):
    import batch_service
    time.sleep(.1 if store in {'QA_0','QA_4'} else 2)
    return batch_service.calculate_store(loaded,store,settings,profile)
