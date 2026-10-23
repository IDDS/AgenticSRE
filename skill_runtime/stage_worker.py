"""Bounded Skill processes with host-side tool/adaptor RPC.

Process termination is not a remote transaction cancellation or a security
sandbox. Host calls already dispatched may finish; their results are discarded
after timeout and must not cause automatic retries.
"""
from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path

MAX_MESSAGE = 8 * 1024 * 1024
HOST_SLOTS = threading.BoundedSemaphore(8)


class StageFailure(Exception):
    def __init__(self, code, detail='', outcome_unknown=False):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.outcome_unknown = outcome_unknown


class Channel:
    def __init__(self, sock):
        self.sock = sock

    def send(self, value):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(data) > MAX_MESSAGE:
            raise ValueError('skill IPC message too large')
        self.sock.sendall(struct.pack('!I', len(data)) + data)

    def receive(self):
        def read(size):
            pieces = []
            while size:
                chunk = self.sock.recv(size)
                if not chunk:
                    raise EOFError('skill worker disconnected')
                pieces.append(chunk)
                size -= len(chunk)
            return b''.join(pieces)
        size = struct.unpack('!I', read(4))[0]
        if size > MAX_MESSAGE:
            raise ValueError('skill IPC message too large')
        return json.loads(read(size))


class StageWorker:
    def __init__(self, module_path, tools, adapters):
        self.module_path = str(Path(module_path).resolve())
        self.tools = tools
        self.adapters = adapters
        self.process = None
        self.channel = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.process is not None:
            # Include subprocesses started by the Skill in its process group.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=2)
            self.process = None
        if self.channel is not None:
            self.channel.sock.close()
            self.channel = None

    def _start(self):
        parent, child = socket.socketpair()
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--worker', str(child.fileno()), self.module_path],
                pass_fds=(child.fileno(),), start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.channel = Channel(parent)

    def _host_call(self, message, deadline):
        remaining = max(0, deadline-time.monotonic())
        if not HOST_SLOTS.acquire(timeout=remaining):
            raise StageFailure('skill_host_busy', 'host call capacity exhausted')
        future = Future()
        try:
            request = copy.deepcopy(message)
            if request['kind'] == 'adapter':
                adapter = self.adapters[request['adapter']]
                method = request['method']
                if method.startswith('_') or not callable(getattr(adapter, method, None)):
                    raise ValueError('adapter method is not public/callable')
        except BaseException:
            HOST_SLOTS.release()
            raise
        # A late host response must not mutate this run or its returned trace.
        if request['kind'] == 'tool':
            from skill_runtime.tool_context import SkillToolContext
            detached = SkillToolContext(runner=self.tools.runner, dry_run=self.tools.dry_run,
                                        payload=copy.deepcopy(self.tools.payload))
            audit = {'tool': request['tool'], 'args': request['kwargs'],
                     'dry_run': self.tools.dry_run, 'success': False, 'executed': None,
                     'outcome_unknown': True, 'error': 'host_call_in_flight'}
            self.tools.calls.append(audit)
            operation = lambda: detached.execute(request['tool'], **request['kwargs'])
        else:
            audit = None
            operation = lambda: getattr(adapter, method)(*request['args'], **request['kwargs'])

        def invoke():
            try:
                future.set_result(operation())
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                HOST_SLOTS.release()
        threading.Thread(target=invoke, daemon=True, name='skill-host-call').start()
        try:
            value = future.result(timeout=max(0, deadline-time.monotonic()))
        except FutureTimeout:
            if audit is not None:
                audit['error'] = 'skill_stage_timeout'
            raise
        if audit is not None:
            audit.clear()
            audit.update(copy.deepcopy(value))
        return value

    def run(self, stage, context, timeout_s):
        deadline = time.monotonic() + timeout_s
        external_started = False
        try:
            if self.process is None:
                self._start()
            self.channel.sock.settimeout(max(.001, deadline-time.monotonic()))
            self.channel.send({'stage': stage, 'module_path': self.module_path,
                               'context': {k:v for k,v in context.items() if k not in ('tools','adapters')},
                               'adapters': list(self.adapters)})
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('stage deadline exceeded')
                self.channel.sock.settimeout(remaining)
                message = self.channel.receive()
                if message['kind'] == 'result':
                    context.update(message['context'])
                    return message['value']
                if message['kind'] == 'error':
                    raise StageFailure('skill_stage_exception', message['detail'], external_started or not context['dry_run'])
                if message['kind'] not in ('tool','adapter'):
                    raise ValueError('invalid Skill worker message')
                external_started = True
                try:
                    result = self._host_call(message, deadline)
                    response = {'ok': True, 'value': result}
                except (FutureTimeout, StageFailure):
                    raise
                except Exception as exc:
                    response = {'ok': False, 'error': str(exc)}
                self.channel.sock.settimeout(max(.001, deadline-time.monotonic()))
                self.channel.send(response)
        except (TimeoutError, FutureTimeout) as exc:
            raise StageFailure('skill_stage_timeout', str(exc), external_started or not context['dry_run']) from None
        except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
            raise StageFailure('skill_worker_crashed', str(exc), external_started or not context['dry_run']) from None


def worker_main(fd, module_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    channel = Channel(socket.socket(fileno=fd))
    modules = {}

    def rpc(message):
        channel.send(message)
        response = channel.receive()
        if not response['ok']:
            raise RuntimeError(response['error'])
        return response['value']

    class Tools:
        def execute(self, tool, **kwargs):
            return rpc({'kind':'tool', 'tool':tool, 'kwargs':kwargs})

    class Adapter:
        def __init__(self, name):
            self.name = name
        def __getattr__(self, method):
            return lambda *args, **kwargs: rpc({'kind':'adapter', 'adapter':self.name,
                                               'method':method, 'args':args, 'kwargs':kwargs})

    while True:
        request = channel.receive()
        try:
            path = request.get('module_path', module_path)
            if path not in modules:
                spec = importlib.util.spec_from_file_location('skill_runtime_isolated_'+str(abs(hash(path))), path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                modules[path] = module
            module = modules[path]
            context = request['context']
            context['tools'] = Tools()
            context['adapters'] = {name:Adapter(name) for name in request['adapters']}
            value = getattr(module, request['stage'])(context)
            if dataclasses.is_dataclass(value) and type(value).__name__ == 'StageResult':
                value = {'__stage_result__': dataclasses.asdict(value)}
            channel.send({'kind':'result', 'value':value,
                          'context': {k:v for k,v in context.items() if k not in ('tools','adapters')}})
        except Exception as exc:
            channel.send({'kind':'error', 'detail':f'{type(exc).__name__}: {exc}'})


if __name__ == '__main__':
    worker_main(int(sys.argv[2]), sys.argv[3])
