"""Fail-closed network guard used by the deterministic local eval harness."""

from __future__ import annotations

import socket
import os
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


_MESSAGE = "WEEX_EVAL_OFFLINE blocked network access"


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError(_MESSAGE)


def install_network_block() -> dict[str, Any]:
    originals: dict[str, Any] = {
        "socket_class": socket.socket,
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
        "urlopen": urllib.request.urlopen,
        "subprocess_popen": subprocess.Popen,
        "offline_env": os.environ.get("WEEX_EVAL_OFFLINE"),
    }

    class OfflineSocket(originals["socket_class"]):
        def connect(self, _address: Any) -> None:
            raise RuntimeError(_MESSAGE)

        def connect_ex(self, _address: Any) -> int:
            raise RuntimeError(_MESSAGE)

        def sendto(self, *_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError(_MESSAGE)

        def sendmsg(self, *_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError(_MESSAGE)

    python_runner = Path(__file__).resolve().parents[1] / "evals" / "offline_guard" / "python_runner.py"
    original_popen = originals["subprocess_popen"]

    def guarded_popen(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args")
        if kwargs.get("shell"):
            raise RuntimeError(_MESSAGE)
        if not isinstance(command, (list, tuple)) or not command:
            raise RuntimeError(_MESSAGE)
        command = list(command)
        executable = Path(str(command[0])).name.lower()
        if not executable.startswith("python"):
            raise RuntimeError(_MESSAGE)
        if len(command) < 2 or str(command[1]) in {"-c", "-m"}:
            raise RuntimeError(_MESSAGE)
        script = Path(str(command[1])).resolve()
        if script != python_runner and script.suffix == ".py":
            command = [command[0], str(python_runner), str(script), *command[2:]]
        return original_popen(command, *args[1:], **kwargs)

    socket.socket = OfflineSocket
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked
    urllib.request.urlopen = _blocked
    subprocess.Popen = guarded_popen
    os.environ["WEEX_EVAL_OFFLINE"] = "1"
    return originals


def restore_network(originals: dict[str, Any]) -> None:
    socket.socket = originals["socket_class"]
    socket.create_connection = originals["create_connection"]
    socket.getaddrinfo = originals["getaddrinfo"]
    urllib.request.urlopen = originals["urlopen"]
    subprocess.Popen = originals["subprocess_popen"]
    if originals["offline_env"] is None:
        os.environ.pop("WEEX_EVAL_OFFLINE", None)
    else:
        os.environ["WEEX_EVAL_OFFLINE"] = originals["offline_env"]


@contextmanager
def network_blocked() -> Iterator[None]:
    originals = install_network_block()
    try:
        yield
    finally:
        restore_network(originals)
