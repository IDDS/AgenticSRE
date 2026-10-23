"""Sustained real requests with stable per-Pod rolling-window measurements."""
import os
from functools import partial

from sustained_alert_acceptance import sustained
from trace_alert_acceptance import main


if __name__ == '__main__':
    if os.environ.get('AGENTICSRE_RUN_STABLE_WINDOW_EXPERIMENT') != '1':
        raise SystemExit('explicit stable window experiment opt-in required')
    main(scenario=partial(sustained, stable_window=True), metric_mode='stable_window')
