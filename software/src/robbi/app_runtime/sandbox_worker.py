"""Disposable Linux app worker. Launched only through sandbox.py's namespaces."""

import ctypes
import errno
import importlib
import json
import os
import resource
import sys
import time
import types
from contextlib import contextmanager

from robbi.app_runtime.sandbox_wire import MAX_FRAME, encode, unpack


def restrict():
    """Additional irreversible restrictions before importing third-party code."""
    for kind, limit in (
        (resource.RLIMIT_AS, 128 * 1024 * 1024),
        (resource.RLIMIT_CPU, 30),
        (resource.RLIMIT_FSIZE, 8 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 48),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(kind, (limit, limit))
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(
        0x7FFF0000
    )  # allow, with explicit denied escape/resource paths
    if not ctx:
        raise RuntimeError("Cannot initialize app syscall filter")
    try:
        for name in (
            "clone",
            "clone3",
            "fork",
            "vfork",
            "execve",
            "execveat",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "mount",
            "umount2",
            "unshare",
            "setns",
            "bpf",
            "keyctl",
            "add_key",
            "request_key",
            "perf_event_open",
            "userfaultfd",
            "io_uring_setup",
            "memfd_create",
            "shmget",
            "msgget",
            "semget",
            "reboot",
            "socket",
            "socketpair",
        ):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if (
                number >= 0
                and lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, number, 0) != 0
            ):
                raise RuntimeError("Cannot install app syscall rule")
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError("Cannot activate app syscall filter")
    finally:
        lib.seccomp_release(ctx)


class Proxy:
    def __init__(self, call, prefix):
        self._call, self._prefix = call, prefix

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self._prefix == "services":
            return Proxy(self._call, "services." + name)

        def invoke(*args, **kwargs):
            kwargs.pop("cancellation", None)
            kwargs.pop("deadline", None)
            return self._call(self._prefix + "." + name, args, kwargs)

        return invoke

    @contextmanager
    def session(self, *resources, duration_s=15):
        self._call("session.begin", resources, {"duration_s": duration_s})
        try:
            yield self
        finally:
            self._call("session.end", [], {})


def main():
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    payload = json.loads(incoming.readline(MAX_FRAME + 1))
    # Ordinary app print output cannot corrupt the RPC channel.
    sys.stdout = open(os.devnull, "w")

    def call(method, args=(), kwargs=None):
        outgoing.write(encode({"method": method, "args": args, "kwargs": kwargs or {}}))
        outgoing.flush()
        reply = incoming.readline(MAX_FRAME + 1)
        if not reply or len(reply) > MAX_FRAME:
            raise RuntimeError("App broker disconnected")
        result = json.loads(reply)
        if "error" in result:
            raise RuntimeError(result["error"])
        return unpack(result["value"], records=True)

    context = Proxy(call, "context")
    context.app_id = payload["app_id"]
    context.config = payload["config"]
    context.permissions = frozenset(payload["permissions"])
    context.deadline = payload["deadline"]
    context.cancelled = lambda: time.monotonic() >= context.deadline
    context.robot, context.services = Proxy(call, "robot"), Proxy(call, "services")
    restrict()
    try:
        package = types.ModuleType("_app")
        package.__path__ = ["/app"]
        sys.modules["_app"] = package
        module, name = payload["entrypoint"].split(":")
        handler = getattr(importlib.import_module("_app." + module), name)
        result = handler(context, payload["tool"], payload["arguments"])
        outgoing.write(encode({"result": result}))
        outgoing.flush()
    except BaseException:
        # No traceback/host configuration is exposed across the boundary.
        outgoing.write(
            encode(
                {
                    "error": "App failed inside isolation; check its SDK compatibility and inputs"
                }
            )
        )
        outgoing.flush()


if __name__ == "__main__":
    main()
