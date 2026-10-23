"""Cooperative HTTP I/O with an owned process, not a filesystem sandbox."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .processes import ProcessScope, worker_python
from .tool_protocol import json_utf8, strict_json


class NativeResponse(dict):
    __slots__ = ('cleanup_failed',)

    def __init__(self, value):
        super().__init__(value)
        self.cleanup_failed = False


def cleanup_failed(value):
    return type(value) is NativeResponse and value.cleanup_failed is True


def _environment():
    # Authentication travels only in the private pipe, never argv or workspace env.
    allowed = {'systemroot', 'windir', 'temp', 'tmp', 'home', 'userprofile', 'path',
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'ssl_cert_file', 'ssl_cert_dir'}
    return {key: value for key, value in os.environ.items() if key.lower() in allowed}


def iter_request(*, endpoint, body, headers, name, timeout, byte_limit, check_current=None):
    from .drivers import (DriverProtocolError, DriverUnavailable, MAX_API_RESPONSE_BYTES,
                          _encode_tool_request, _tool_failure)

    _encode_tool_request(endpoint=endpoint, body=body, headers=headers,
                         timeout=timeout, byte_limit=byte_limit)
    request = None
    try:
        request = json_utf8({'endpoint': endpoint, 'body': body, 'headers': headers, 'name': name,
                            'timeout': timeout, 'byte_limit': byte_limit}, allow_nan=False)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        pass
    if request is None:
        raise _tool_failure(DriverProtocolError('Tool request is not valid JSON'), 'invalid_request') from None
    deadline = time.monotonic() + timeout
    process, scope = None, None
    reader, writer = None, None
    output = bytearray()
    read_done = threading.Event()
    failures = set()
    frame_limit = 4 * MAX_API_RESPONSE_BYTES + 8192
    callback_error = None
    response = None

    def current():
        nonlocal callback_error
        if check_current is not None:
            try:
                check_current()
            except BaseException as error:
                callback_error = error
                raise

    def read_output():
        try:
            with process.stdout as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    if len(output) + len(chunk) > frame_limit:
                        failures.add('output_limit')
                        break
                    output.extend(chunk)
        except OSError:
            failures.add('read')
        finally:
            read_done.set()

    def write_input():
        try:
            with process.stdin as stream:
                remaining = memoryview(request)
                while remaining:
                    size = stream.write(remaining)
                    if not size:
                        raise OSError('Input pipe closed')
                    remaining = remaining[size:]
        except OSError:
            failures.add('write')

    try:
        current()
        process = subprocess.Popen(
            [worker_python(), '-I', '-B', str(Path(__file__).with_name('_native_api_worker.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_environment(), shell=False, start_new_session=os.name != 'nt',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
        # The worker reads stdin only after it belongs to this invocation's scope.
        scope = ProcessScope(process)
        current()
        if time.monotonic() >= deadline:
            raise _tool_failure(DriverUnavailable('Tool API request timed out'), 'timeout')
        reader = threading.Thread(target=read_output, name='cater-api-output', daemon=True)
        writer = threading.Thread(target=write_input, name='cater-api-input', daemon=True)
        reader.start()
        writer.start()
        while True:
            if read_done.is_set() and response is None:
                if failures & {'read', 'output_limit'}:
                    raise DriverUnavailable('Native API transport did not return a complete result')
                try:
                    envelope = strict_json(bytes(output))
                except (ValueError, UnicodeError, RecursionError, OverflowError):
                    raise DriverProtocolError('Invalid native API transport result') from None
                if not isinstance(envelope, dict):
                    raise DriverProtocolError('Invalid native API transport result')
                if set(envelope) == {'data'} and isinstance(envelope['data'], dict):
                    response = NativeResponse(envelope['data'])
                elif set(envelope) == {'error'}:
                    raise _error(envelope['error'])
                else:
                    raise DriverProtocolError('Invalid native API transport result')
            if response is not None and process.poll() is not None:
                response.cleanup_failed |= process.returncode != 0 or 'write' in failures
                return response
            if failures & {'read', 'output_limit'}:
                raise DriverUnavailable('Native API transport pipe did not complete')
            if time.monotonic() >= deadline:
                if response is not None:
                    response.cleanup_failed = True
                    return response
                raise _tool_failure(DriverUnavailable('Tool API request timed out'), 'timeout')
            try:
                current()
            except Exception:
                if response is not None:
                    response.cleanup_failed = True
                    return response
                raise
            yield
            delay = min(0.01, max(0, deadline - time.monotonic()))
            if read_done.is_set():
                time.sleep(delay)
            else:
                read_done.wait(delay)
    except (OSError, ValueError) as error:
        if error is callback_error:
            raise
        raise DriverUnavailable('Native API transport could not start or complete safely') from None
    finally:
        if process is not None:
            primary = sys.exception()
            cleanup_problem = False
            try:
                if scope is not None:
                    scope.close()
                elif process.poll() is None:
                    process.kill()
            except OSError:
                cleanup_problem = True
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_problem = True
            finally:
                for thread in (writer, reader):
                    if thread is not None:
                        thread.join(timeout=2)
                for stream, thread in ((process.stdin, writer), (process.stdout, reader)):
                    if stream is not None and not stream.closed and (thread is None or not thread.is_alive()):
                        stream.close()
            cleanup_problem |= any(thread is not None and thread.is_alive() for thread in (writer, reader))
            cleanup_problem |= bool(failures & {'read', 'write', 'output_limit'})
            if cleanup_problem:
                if response is not None:
                    response.cleanup_failed = True
                elif primary is not None:
                    primary.add_note('Native API transport cleanup was not confirmed')
                    primary.native_cleanup_failed = True
                else:
                    raise DriverUnavailable('Native API transport cleanup was not confirmed')


def _error(error):
    from .drivers import (DriverProtocolError, DriverUnavailable, ToolRequestRejected,
                          TOOL_FAILURE_KINDS, TOOL_TRANSPORT_PHASES, TOOL_TRANSPORT_ISSUES)

    if (not isinstance(error, dict) or set(error) != {'type', 'message', 'diagnostics'}
            or type(error['type']) is not str
            or error['type'] not in {'DriverProtocolError', 'DriverUnavailable', 'ToolRequestRejected'}
            or not isinstance(error['message'], str) or len(error['message']) > 1000
            or not isinstance(error['diagnostics'], dict)):
        return DriverProtocolError('Invalid native API error result')
    kind = error['type']
    status = error['diagnostics'].get('status_code')
    if kind == 'ToolRequestRejected' and (type(status) is not int or status != 429):
        return DriverProtocolError('Invalid native API error result')
    result = (ToolRequestRejected(error['message'], status_code=status) if kind == 'ToolRequestRejected' else
              DriverProtocolError(error['message']) if kind == 'DriverProtocolError' else
              DriverUnavailable(error['message']))
    for key, value in error['diagnostics'].items():
        valid = ((key == 'failure_kind' and type(value) is str and value in TOOL_FAILURE_KINDS)
                 or (key == 'transport_phase' and type(value) is str and value in TOOL_TRANSPORT_PHASES)
                 or (key == 'transport_issue' and type(value) is str and value in TOOL_TRANSPORT_ISSUES)
                 or (key == 'status_code' and type(value) is int and 100 <= value <= 599))
        if not valid:
            return DriverProtocolError('Invalid native API error result')
        setattr(result, key, value)
    return result


def worker_main():
    import sys
    from .drivers import (DriverError, DriverProtocolError, DriverUnavailable, ToolRequestRejected,
                          _open_api_request, _tool_api_request, tool_failure_diagnostics)

    try:
        request = strict_json(sys.stdin.buffer.read())
        if not isinstance(request, dict) or set(request) != {'endpoint', 'body', 'headers', 'name', 'timeout', 'byte_limit'}:
            raise DriverProtocolError('Invalid native API worker input')
        result = {'data': _tool_api_request(**request, opener=_open_api_request)}
    except DriverError as error:
        kind = ('ToolRequestRejected' if isinstance(error, ToolRequestRejected) else
                'DriverProtocolError' if isinstance(error, DriverProtocolError) else 'DriverUnavailable')
        result = {'error': {'type': kind, 'message': str(error), 'diagnostics': tool_failure_diagnostics(error)}}
    except Exception:
        result = {'error': {'type': 'DriverUnavailable', 'message': 'Native API worker failed', 'diagnostics': {}}}
    sys.stdout.buffer.write(json_utf8(result))
    sys.stdout.buffer.flush()

