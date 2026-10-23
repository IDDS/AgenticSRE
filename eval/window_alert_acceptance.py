"""Opt-in actual request-window positive and boundary regression runs."""
import os
from functools import partial
from sustained_alert_acceptance import sustained
from trace_alert_acceptance import main

if __name__ == '__main__':
    if os.environ.get('AGENTICSRE_RUN_WINDOW_EXPERIMENT') != '1':
        raise SystemExit('explicit request-window experiment opt-in required')
    main(scenario=partial(sustained,stable_window=True,window_merge=True,
                         extended=bool(os.environ.get('AGENTICSRE_WINDOW_BOUNDARIES')),
                         all_scenarios=os.environ.get('AGENTICSRE_WINDOW_BOUNDARIES')=='all',
                         sample_time_negative=bool(os.environ.get('AGENTICSRE_PROBE_SAMPLE_LAG_MS'))),
         metric_mode='stable_window',window_proofs=True)
