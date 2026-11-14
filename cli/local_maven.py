# SPDX-License-Identifier: MIT
"""Run a pinned Maven view while supervising its process group under a read lock."""
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import prefetch_state


def group_running(group):
    if sys.platform.startswith("linux") and Path("/proc/self/stat").is_file():
        # Zombies have already terminated and cannot write through this view.
        # killpg(0) alone also reports zombies awaiting their reaper.
        unknown = False
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text(errors="replace").rpartition(")")[2].split()
                if int(fields[2]) != group:
                    continue
                if fields[0] not in ("Z", "X", "x"):
                    return True
                # A thread-group leader may exit before its other threads.
                for task in (path.parent / "task").glob("*/stat"):
                    try:
                        status = task.read_text(errors="replace").rpartition(")")[2].split()
                        if status[0] not in ("Z", "X", "x"):
                            return True
                    except FileNotFoundError:
                        continue
                    except (PermissionError, IndexError):
                        unknown = True
            except FileNotFoundError:
                continue
            except (PermissionError, ValueError, IndexError):
                unknown = True
        if not unknown:
            return False
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


@contextlib.contextmanager
def foreground(group):
    terminal = {"owned": os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()}
    if terminal["owned"]:
        set_foreground(group)
    try:
        yield terminal
    finally:
        if terminal["owned"]:
            set_foreground(os.getpgrp())


def set_foreground(group):
    handler = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(0, group)
    finally:
        signal.signal(signal.SIGTTOU, handler)


def run_maven(command):
    process = None
    pending = []
    canceled = []
    previous = {}

    def forward(number, frame):
        canceled.append(number)
        if process is None:
            pending.append(number)
            return
        try:
            os.killpg(process.pid, number)
            # A stopped Maven must resume to handle its termination signal.
            os.killpg(process.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass

    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, forward)
        # A dedicated group lets cancellation reach Invoker/JVM descendants,
        # without signaling the caller's shell or the lock-holding helper.
        process = subprocess.Popen(command, close_fds=True, process_group=0)
        for number in pending:
            forward(number, None)
        with foreground(process.pid) as terminal:
            while True:
                _, status = os.waitpid(process.pid, os.WUNTRACED)
                if os.WIFSTOPPED(status):
                    # Hand control back to the shell for ordinary job control.
                    # The lock remains held while this helper is stopped.
                    if terminal["owned"]:
                        set_foreground(os.getpgrp())
                    group = os.getpgrp()
                    if group != os.getpgid(os.getppid()):
                        os.killpg(group, os.WSTOPSIG(status))
                    else:
                        os.kill(os.getpid(), os.WSTOPSIG(status))
                    if os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp():
                        terminal["owned"] = True
                        set_foreground(process.pid)
                    else:
                        terminal["owned"] = False
                    os.killpg(process.pid, signal.SIGCONT)
                    continue
                if os.WIFEXITED(status):
                    process.returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    process.returncode = -os.WTERMSIG(status)
                else:
                    continue
                break
        # A successful direct Maven process can leave an Invoker descendant
        # finishing work. Keep the view locked until that group has terminated.
        while process.returncode == 0 and not canceled and group_running(process.pid):
            time.sleep(.01)
        if canceled and process.returncode == 0:
            return 128 + canceled[-1]
        return process.returncode
    finally:
        if process is not None and (canceled or process.returncode != 0):
            # Do not leave a late Invoker child alive when its direct Maven
            # parent exits during cancellation. This group belongs to us.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.returncode is None:
                process.wait()
            while group_running(process.pid):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                time.sleep(.01)
        for number, handler in previous.items():
            signal.signal(number, handler)


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    with prefetch_state.project_lock(root, exclusive=False) as descriptor:
        arguments = sys.argv[2:]
        suffix = None
        if len(arguments) >= 2 and arguments[0] == "--generation":
            generation = Path(arguments[1])
            arguments = arguments[2:]
        else:
            generation, suffix = prefetch_state.active_generation(root)
        manifest = prefetch_state.validate(generation, root, suffix)
        config = root / ".mvn/maven.config"
        prefix, _ = prefetch_state.split_config(config.read_bytes() if config.exists() else b"")
        if prefetch_state.digest(prefix) != manifest["publishedPrefixHash"]:
            raise prefetch_state.StateError("the authored Maven configuration differs from this view")
        prefetch_state.check_project_directory(root, descriptor)
        code = run_maven([manifest["paths"]["launcher"], *arguments])
        return code if code >= 0 else 128 - code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"maven-purifier: cannot run the activated Maven view: {error}", file=sys.stderr)
        raise SystemExit(1)
