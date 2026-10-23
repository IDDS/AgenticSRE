"""Remote stable-window negative, missing-Trace and burst-load checks."""
import os
from functools import partial

from sustained_alert_acceptance import sustained
from trace_alert_acceptance import main


if __name__ == '__main__':
    if os.environ.get('AGENTICSRE_RUN_EXTENDED_LOAD_EXPERIMENT') != '1':
        raise SystemExit('explicit extended load experiment opt-in required')
    main(scenario=partial(sustained, stable_window=True, extended=True), metric_mode='stable_window')
