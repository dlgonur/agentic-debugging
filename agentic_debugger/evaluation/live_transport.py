"""Command/provider transport primitives for live evaluation.

This module owns the transport boundary: the ``ModelTransport``
protocol, the bounded capture/pipe helpers, the closed typed
command-error envelope, and ``JsonlCommandTransport`` (cancellable
JSONL command transport with idle watchdogs and output bounds).
Transport failures keep the accepted ``LiveTransportError`` taxonomy
with ``kind``/``timed_out``/``safe_message`` semantics."""

from __future__ import annotations

import json, os, subprocess, threading, time

from typing import Any, Callable, Mapping, Protocol

from agentic_debugger.evaluation.live_contracts import COMMAND_ERROR_SCHEMA_VERSION, LiveConfigurationError, LiveTransportError, MAX_MODEL_RESPONSE_BYTES, MAX_REJECTION_DETAIL_CHARS, _SECRET_VALUE, _TYPED_COMMAND_ERROR_KINDS


class ModelTransport(Protocol):
    def request(self,payload:Mapping[str,Any],timeout_seconds:float)->Mapping[str,Any]: ...

class _BoundedCapture:
    def __init__(self, maximum_bytes:int):
        self.maximum_bytes=maximum_bytes; self.data=bytearray(); self.truncated=False; self.lock=threading.Lock()
    def add(self, chunk:bytes):
        with self.lock:
            remaining=self.maximum_bytes-len(self.data)
            if remaining>0: self.data.extend(chunk[:remaining])
            if len(chunk)>remaining: self.truncated=True
    def text(self)->str:
        with self.lock: return bytes(self.data).decode("utf-8",errors="replace")

def _read_pipe(pipe:Any,capture:_BoundedCapture,activity:Callable[[],None]|None=None):
    try:
        read_chunk=getattr(pipe,"read1",pipe.read)
        while True:
            chunk=read_chunk(8192)
            if not chunk: return
            capture.add(chunk)
            if activity is not None: activity()
    except Exception:
        return

def _terminate_process(process:subprocess.Popen):
    try:
        process.terminate(); process.wait(timeout=1)
    except Exception:
        try: process.kill(); process.wait(timeout=2)
        except Exception: pass

def _typed_command_error_detail(stderr_text: str) -> tuple[str, str] | None:
    """Read only the closed, provider-safe command error envelope.

    Arbitrary configured-command stderr is intentionally ignored.  A command
    may contribute a typed failure only by emitting one strict JSON object
    with the accepted schema and closed vocabulary.  Its message is retained
    only when it is bounded, single-line, and not credential-shaped.
    """

    if type(stderr_text) is not str or not stderr_text:
        return None
    try:
        value = json.loads(stderr_text)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "kind",
        "message",
    }:
        return None
    kind = value.get("kind")
    message = value.get("message")
    if value.get("schema_version") != COMMAND_ERROR_SCHEMA_VERSION:
        return None
    if type(kind) is not str or kind not in _TYPED_COMMAND_ERROR_KINDS:
        return None
    if (
        type(message) is not str
        or not message
        or len(message.encode("utf-8", errors="replace")) > MAX_REJECTION_DETAIL_CHARS
    ):
        return None
    if any(ord(char) < 0x20 and char not in "\t\r\n" for char in message):
        return None
    if "\r" in message or "\n" in message or _SECRET_VALUE.search(message):
        return None
    return kind, message


def _typed_command_error_kind(stderr_text: str) -> str | None:
    """Compatibility projection of the accepted typed command error kind."""

    detail = _typed_command_error_detail(stderr_text)
    return detail[0] if detail is not None else None

class JsonlCommandTransport:
    def __init__(self,config,*,max_output_bytes=MAX_MODEL_RESPONSE_BYTES,activity_observer=None):
        if type(max_output_bytes) is not int or not 1024<=max_output_bytes<=4*1024*1024: raise LiveConfigurationError("max response bytes is invalid")
        if activity_observer is not None and not callable(activity_observer): raise LiveConfigurationError("activity_observer must be callable or None")
        self.config=config; self.max_output_bytes=max_output_bytes; self.activity_observer=activity_observer
    def _activity(self, kind):
        if self.activity_observer is not None:
            try: self.activity_observer(kind)
            except Exception: pass
    @staticmethod
    def subprocess_environment():
        environment={"PATH":os.environ.get("PATH",""),"PYTHONIOENCODING":"utf-8"}
        if os.name=="nt" and os.environ.get("SystemRoot"): environment["SystemRoot"]=os.environ["SystemRoot"]
        return environment
    def request(self,payload,timeout_seconds):
        try: request_bytes=(json.dumps(payload,ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8")
        except (TypeError,ValueError,UnicodeError): raise LiveTransportError("model request could not be serialized",kind="request_serialization") from None
        environment=self.subprocess_environment()
        try:
            process=subprocess.Popen(list(self.config.command),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,shell=False,env=environment,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0)
        except (OSError,ValueError): raise LiveTransportError("model command could not be launched",kind="launch_error") from None
        stdout=_BoundedCapture(self.max_output_bytes); stderr=_BoundedCapture(self.max_output_bytes)
        activity_lock=threading.Lock(); last_activity=[time.monotonic()]
        self._activity("request_started")
        def mark_activity():
            with activity_lock: last_activity[0]=time.monotonic()
            self._activity("stream_activity")
        def idle_expired():
            with activity_lock: return time.monotonic()-last_activity[0]>=timeout_seconds
        threads=[threading.Thread(target=_read_pipe,args=(process.stdout,stdout,mark_activity),daemon=True),threading.Thread(target=_read_pipe,args=(process.stderr,stderr,mark_activity),daemon=True)]
        for thread in threads: thread.start()
        write_error=[]
        def write_request():
            try:
                assert process.stdin is not None
                process.stdin.write(request_bytes)
                process.stdin.close()
            except (BrokenPipeError,OSError) as exc:
                write_error.append(exc)
        writer=threading.Thread(target=write_request,daemon=True)
        writer.start()
        while writer.is_alive():
            if idle_expired():
                _terminate_process(process)
                for thread in threads: thread.join(timeout=2)
                raise LiveTransportError("model request stdin was idle for too long",kind="request_timeout",timed_out=True) from None
            writer.join(timeout=0.05)
        mark_activity()
        while True:
            try:
                process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if idle_expired():
                    _terminate_process(process)
                    for thread in threads: thread.join(timeout=2)
                    raise LiveTransportError("model request was idle for too long",kind="request_timeout",timed_out=True) from None
        for thread in threads: thread.join(timeout=2)
        if stdout.truncated: raise LiveTransportError("model response exceeded the configured output bound",kind="response_too_large")
        if process.returncode!=0:
            typed_detail = _typed_command_error_detail(stderr.text())
            raise LiveTransportError(
                "model command failed",
                kind=typed_detail[0] if typed_detail is not None else "process_error",
                safe_message=typed_detail[1] if typed_detail is not None else None,
            )
        try: value=json.loads(stdout.text())
        except (UnicodeError,json.JSONDecodeError): raise LiveTransportError("model response was invalid JSON",kind="invalid_response") from None
        if not isinstance(value,Mapping): raise LiveTransportError("model response was not an object",kind="invalid_response")
        # A wrapper may emit its completed JSON response and close stdin before
        # the bounded request writer finishes.  A successful JSON response is
        # the provider completion contract; the harmless broken-pipe signal is
        # not a transport failure in that case.
        self._activity("request_completed")
        return value
