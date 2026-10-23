"""Read-only acceptance of exact Trace lookup via the configured Kubernetes shell."""
import argparse
import json
from agents.alert_context import namespace_trace_query
from web_app.app import _shell_sync


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--trace-id', required=True)
    parser.add_argument('--expected-spans', type=int, default=5)
    args=parser.parse_args()
    commands=[]
    def shell(command, timeout):
        commands.append(command)
        return _shell_sync(command, timeout, 'default')
    prefixes={}
    services=namespace_trace_query(args.namespace,'/api/services',None,shell,prefixes)
    assert isinstance(services['data'],list)
    trace=namespace_trace_query(args.namespace,'/api/traces/'+args.trace_id,None,shell,prefixes)
    match=[t for t in trace['data'] if t['traceID'].lower()==args.trace_id.lower()]
    assert len(match)==1 and len(match[0]['spans'])==args.expected_spans
    initial=len(commands)
    for path in ('/api/traces/../../config','/api/traces/'+args.trace_id+'?query=bad'):
        try: namespace_trace_query(args.namespace,path,None,shell,prefixes)
        except ValueError: pass
        else: raise AssertionError('invalid path accepted')
    assert len(commands)==initial
    print(json.dumps(dict(passed=True,namespace=args.namespace,trace_id=args.trace_id,
                          spans=len(match[0]['spans']),prefix=prefixes[args.namespace],
                          rejected_paths_before_execution=2,read_only_commands=len(commands))))


if __name__=='__main__':main()
