"""Gracefully stop a running Firefly AI Pet via its control socket.

Usage:
    python tools/stop_pet.py

Connects to the pet's QLocalServer and sends "quit". If the pet is not
running, removes any stale pid file. Exits 0 in all normal cases.
"""

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

PROJECT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = PROJECT_DIR / "runtime"
PID_FILE = RUNTIME_DIR / "pet.pid"
SERVER_NAME = "FireflyAIPet-SingleInstance"


def main():
    app = QCoreApplication(sys.argv)

    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)

    if socket.waitForConnected(1500):
        socket.write(b"quit")
        socket.flush()
        socket.waitForBytesWritten(1000)
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.UnconnectedState:
            socket.waitForDisconnected(1500)
        print("Stopped Firefly AI Pet.")
        return 0

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
            print("Firefly AI Pet is not running (removed stale pid file).")
        except OSError:
            print("Firefly AI Pet is not running.")
    else:
        print("Firefly AI Pet is not running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
