"""Project engine ownership; does not lock independent tickets against each other."""
import fcntl
import os
from pathlib import Path
from agilecode.core.types import AgileCodeError


class ProjectLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise AgileCodeError("This project already has a running AgileCode engine. Close that instance before opening another.") from None
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
    def close(self):
        if not self.handle.closed:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
