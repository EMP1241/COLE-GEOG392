"""Loopback launcher with a retained socket and per-installation process lock."""

# ===========================================================================
# PLAIN-ENGLISH GUIDE TO THIS FILE
# ===========================================================================
#
# WHAT IT DOES
# This is the program that runs when someone double-clicks COLE. It is not the
# app itself - it is the doorman. Its job:
#
#   1. Verify the data files are intact
#   2. Make sure only ONE copy of COLE runs per installation
#   3. Claim a network port on this machine
#   4. Start the web server
#   5. Open the browser once the server actually answers
#   6. Shut everything down cleanly when the user quits
#
# WHY THIS IS HARDER THAN IT LOOKS
# Most of the complexity is about ONE problem: what happens when the user
# double-clicks the icon twice? Two copies would fight over the same port, and
# the second would either crash or, worse, quietly run alongside the first.
#
# The naive fix is to write the running process's ID to a file and have later
# copies kill it. That approach is genuinely dangerous: process IDs get reused
# by the operating system, so "kill whatever is in this file" can kill an
# unrelated program. This file explicitly refuses to do that - notice that
# several error messages end with "No unrelated service was stopped."
#
# Instead it uses an OS-HELD FILE LOCK. The operating system itself tracks who
# holds it, and releases it automatically when that process dies - even if it
# crashed. Nothing needs to be cleaned up, and nothing gets killed.
#
# KEY TERMS
#   LOOPBACK / 127.0.0.1 - the address meaning "this computer, and only this
#       computer." A server bound to it is unreachable from the network.
#   PORT - a numbered door on that address. COLE prefers 8080 and walks
#       upward if it is taken.
#   SOCKET - the actual claimed connection point.
#   WAITRESS - the production-quality web server that runs the Flask app.
#       Flask's own built-in server is for development only.
#   DESCRIPTOR - instance.json, a small file recording which port the running
#       copy is on, so a second launch can find and defer to it.
#   THREAD - a second line of execution running at the same time as the main
#       one. Used here so the browser can be opened while the server runs.
# ===========================================================================

from __future__ import annotations

import argparse  # command-line options (--port, --no-browser)
import hashlib
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import webbrowser

from . import __version__
from .app import create_app
from .repository import (
    DataError,
    REQUIRED_OUTPUTS,
    Repository,
    _validate_manifest,
    default_data_directory,
    manifest_contract_sha256,
)


class InstanceLock:
    """OS-held lock: process death releases it without unsafe stale-PID killing."""

    # THE SINGLE-INSTANCE GUARANTEE.
    #
    # Opens a file and asks the operating system to lock it. Only one process
    # can hold that lock at a time. If this process exits - cleanly, by crash,
    # or by force - the OS releases the lock automatically. That is the whole
    # point: there is no stale state to clean up and no process to kill.
    #
    # Windows and Unix have different locking APIs, so both are supported.

    def __init__(self, path):
        self.path = Path(path)
        self.stream = None  # None means "we do not hold the lock"

    def acquire(self):
        # Try to take the lock. True if we now own it, False if someone else does.
        if self.stream is not None:
            return True  # already ours
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+b" opens for append and reading, in binary, creating if absent -
        # and crucially never truncates, so an existing lock file is not
        # disturbed by merely opening it.
        stream = self.path.open("a+b", buffering=0)
        stream.seek(0)
        try:
            if os.name == "nt":  # "nt" means Windows
                import msvcrt

                # LK_NBLCK = non-blocking lock: fail immediately rather than
                # waiting, so a second launch can react instead of hanging.
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # macOS, Linux
                import fcntl

                # LOCK_EX exclusive, LOCK_NB non-blocking - same intent.
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Someone else holds it. Close our handle and report failure.
            stream.close()
            return False
        self.stream = stream
        try:
            self.stream.seek(0, 2)  # jump to the end
            if self.stream.tell() == 0:  # file is empty?
                # Original comment: Both locking APIs need at least one byte to lock, so write one.
                # Windows can lock past EOF; initialize only after ownership.
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
        except OSError:
            self.close()
            raise
        return True

    def close(self):
        # Release the lock, defensively.
        if self.stream is not None:
            # Clear the attribute BEFORE doing anything that might fail, so a
            # second call to close() cannot try to unlock twice.
            stream = self.stream
            self.stream = None
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream, fcntl.LOCK_UN)
            finally:
                # Original comment: Closing the OS handle still releases
                # ownership if explicit unlock fails. A later cleanup attempt
                # must remain safe.
                #
                # `finally` runs whether or not the unlock above succeeded, so
                # the file handle is always closed - and closing it releases
                # the lock regardless. Belt and braces.
                stream.close()


def reserve_socket(preferred=8080, attempts=20):
    # Claim a local port, trying `preferred` first, then the next 19.
    if not 0 <= preferred <= 65535 or not 1 <= attempts <= 100:
        raise ValueError("Invalid local port range.")
    # Port 0 is special: it means "operating system, pick any free port."
    # Used by tests so they never collide with a real running instance.
    ports = [0] if preferred == 0 else range(preferred, min(65536, preferred + attempts))
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            # Windows-only. By default Windows lets two programs bind the same
            # port in some circumstances, which would let a second COLE - or
            # another program entirely - silently hijack traffic. This option
            # demands exclusive ownership.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            # Note the address: 127.0.0.1, never 0.0.0.0. Binding to 0.0.0.0
            # would expose COLE to the whole local network.
            sock.bind(("127.0.0.1", port))
            # SUCCESS - and note the socket is RETURNED, still open. It is
            # handed to Waitress later. Closing and reopening would leave a gap
            # in which another program could steal the port.
            return sock
        except OSError:
            sock.close()  # taken; clean up and try the next one
    raise RuntimeError(
        "The COLE local port range is occupied. Close an earlier COLE instance or choose another --port."
    )


def fetch_health(port):
    # Ask a possibly-running COLE on `port` to identify itself.
    # A direct HTTP connection uses no proxy, redirects or TLS initialization.
    # urllib's default opener initializes HTTPS support even for this HTTP URL;
    # missing TLS resources in a frozen runtime must not break local startup.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        try:
            if response.status != 200:
                return None
            body = response.read(8193)
            if len(body) > 8192:
                raise ValueError("Local COLE health response exceeds 8192 bytes.")
            return json.loads(body)
        finally:
            response.close()
    finally:
        connection.close()


def compatible_health(health, descriptor, release_id, release_contract):
    # Is the thing answering on that port really OUR COLE, same version, same data?
    # This is the anti-hijack check, and every clause earns its place. Before
    # handing the user's browser to a server we did not start, we require:
    #
    #   - it calls itself "COLE" and says it is ready
    #   - its app version matches ours exactly
    #   - its data release and release fingerprint match ours exactly
    #   - the descriptor file agrees about the release fingerprint
    #   - the instance ID in the descriptor matches the one the server reports
    #     (this is the strongest link: it proves the descriptor file describes
    #     THAT server, not a previous one that happened to use the same port)
    #   - the IDs are the right length and the fingerprint is real hex
    #
    # If any clause fails we do not adopt that server. `all((...))` is only
    # true when every item is true.
    return (
        isinstance(descriptor, dict)
        and isinstance(health, dict)
        and health.get("app") == "COLE"
        and health.get("ready") is True
        and all(
            (
                health.get("app_version") == __version__,
                health.get("data_release_id") == release_id,
                health.get("release_contract_sha256") == release_contract,
                descriptor.get("release_contract_sha256") == release_contract,
                health.get("instance_id") == descriptor.get("instance_id"),
                isinstance(descriptor.get("instance_id"), str)
                and len(descriptor["instance_id"]) == 48,
                isinstance(release_contract, str)
                and len(release_contract) == 64
                and all(character in "0123456789abcdef" for character in release_contract),
            )
        )
    )


def installed_release_contract(data_directory):
    """Retain validated release metadata after checking every output fingerprint."""
    # A cheap pre-flight: verify the data is intact WITHOUT doing the full,
    # slower parse that Repository does. Run before the lock is taken, so a
    # second launch discovers broken data quickly.
    directory = Path(data_directory)
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_manifest(manifest)
    except DataError:
        raise  # already a clear message
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DataError(f"Cannot read the installed COLE release metadata: {exc}") from exc

    for filename in REQUIRED_OUTPUTS:
        digest = hashlib.sha256()
        size = 0
        try:
            with (directory / filename).open("rb") as stream:
                # Read in 1 MB chunks rather than loading whole files into
                # memory. The `:=` walrus operator reads a chunk, names it, and
                # tests it for emptiness in one line - the loop ends at EOF.
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
        except OSError as exc:
            raise DataError(f"Cannot verify the installed COLE data: {exc}") from exc
        expected = manifest["outputs"][filename]
        if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
            raise DataError(f"Prepared data integrity check failed: {filename}")
    # Return the manifest so main() can compare it against what Repository
    # loads a moment later - closing the window in which files could change.
    return manifest


def existing_url(path, release_id, release_contract):
    # If a compatible COLE is already running, return its address. Otherwise None.
    started = time.monotonic()
    try:
        descriptor = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(descriptor, dict):
            return None
        port = descriptor.get("port")
        # `type(port) is int` rejects True/False; the range check rejects
        # nonsense. Only then do we actually contact the port.
        if (
            type(port) is int
            and 0 < port <= 65535
            and compatible_health(fetch_health(port), descriptor, release_id, release_contract)
        ):
            return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError, TypeError, http.client.HTTPException) as exc:
        logging.debug("Local COLE health probe failed after %.3fs (%s): %s",
                      time.monotonic() - started, type(exc).__name__, exc, exc_info=True)
        # Missing file, unreadable file, garbled JSON, nothing listening - all
        # simply mean "no usable instance," which is not an error worth raising.
        pass
    return None


def wait_for_existing(path, release_id, release_contract, timeout=20, *, stopped=None):
    """Confirm an owner's service without attempting to change lock ownership."""
    # Used in two situations: a second launch waiting for the first to finish
    # starting, and the owner itself confirming its own server came up before
    # opening a browser at it.
    #
    # The docstring's promise matters: this function only WATCHES. It never
    # tries to seize the lock or stop anything.
    #
    # time.monotonic() is a clock that only moves forward and is immune to the
    # system clock being changed - the right clock for measuring elapsed time.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # `stopped` is a flag the user's Quit can set, so this loop abandons
        # the wait instead of holding shutdown up for 20 seconds.
        if stopped is not None and stopped.is_set():
            return None
        url = existing_url(path, release_id, release_contract)
        if url:
            return url
        time.sleep(0.15)  # poll gently rather than spinning the CPU
    raise RuntimeError(
        "Another COLE launch owns the application lock but did not become ready. Wait for it to finish or close that COLE process. No unrelated service was stopped."
    )


def acquire_or_find_existing(lock, path, release_id, release_contract, timeout=20):
    """Return an existing URL, or None with the OS lock owned by this process."""
    # THE CORE SINGLE-INSTANCE DANCE. Two possible outcomes:
    #   None -> we own the lock; we must start the server
    #   a URL -> someone else is already serving; just open a browser at it
    deadline = time.monotonic() + timeout
    while True:
        # Original comment: A failed contender must retry the OS lock: the
        # starting owner may have exited since the previous attempt. Never
        # infer ownership from an old descriptor or kill a process named by a
        # file.
        #
        # That last sentence is the safety rule this whole design exists to
        # honour. We always re-ask the OS rather than trusting a file.
        if lock.acquire():
            return None
        url = existing_url(path, release_id, release_contract)
        if url:
            return url
        # Neither able to take the lock nor able to find a healthy server: the
        # other copy is probably still starting. Loop and retry until timeout.
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Another COLE launch owns the application lock but did not become ready. Wait for it to finish or close that COLE process. No unrelated service was stopped."
            )
        time.sleep(0.15)


def runtime_directory(data_directory):
    # Where logs, the lock, and the descriptor live - unique per installation.
    # Prefers an explicit override, then Windows' LOCALAPPDATA, then a sensible
    # Unix location.
    base = Path(
        os.environ.get("COLE_RUNTIME_DIR")
        or (Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "state")) / "COLE")
    )
    # The clever part: the folder name is a short fingerprint of the DATA
    # DIRECTORY's path. So two COLE installations in different folders get
    # different runtime folders and different locks - meaning they can run
    # simultaneously. "One instance" means one per installation, not one per
    # machine. `.casefold()` handles Windows' case-insensitive paths.
    identity = hashlib.sha256(str(data_directory.resolve()).casefold().encode("utf-8")).hexdigest()[
        :16
    ]
    return base / identity


def show_message(message, *, error=False):
    # Tell the user something, whether or not a console window exists.
    stream = sys.stderr if error else sys.stdout
    if stream:
        print(message, file=stream, flush=True)
    elif os.name == "nt":
        # A packaged Windows app has NO console, so print() would vanish. Fall
        # back to a native pop-up box. 0x10 is the error icon, 0x40 is info.
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "COLE", 0x10 if error else 0x40)


def open_browser(url):
    # Open the user's default browser, with a fallback if that fails.
    try:
        opened = webbrowser.open(url)
    except Exception:
        logging.exception("The default browser could not open the healthy COLE service")
        opened = False
    if not opened:
        # The server is running fine - only the browser launch failed. So show
        # the address rather than treating this as a fatal error.
        show_message(
            f"COLE is running at {url}\n\nThe default browser did not open. Copy this address into a browser on this computer."
        )


def main(argv=None):
    # The startup sequence, and the shutdown sequence that mirrors it.
    parser = argparse.ArgumentParser(description="Run the offline COLE application locally.")
    parser.add_argument(
        "--no-browser", action="store_true", help="Run without opening a browser (for checks)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Preferred local port; zero selects a free test port.",
    )
    parser.add_argument(
        "--data-dir", type=Path, help="Prepared release directory for development checks."
    )
    args = parser.parse_args(argv)

    data_directory = args.data_dir or default_data_directory()
    runtime = runtime_directory(data_directory)

    # --- Logging, set up before anything that might fail ------------------
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        # "Rotating" means the log is capped at 2 MB and older content moves to
        # backup files, so a long-lived install cannot fill the disk.
        handler = RotatingFileHandler(
            runtime / "cole.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        )
    except OSError as exc:
        # Cannot even create a log. Report and give up - there is nowhere to
        # record anything that happens next.
        show_message(
            f"COLE cannot create its runtime directory or log: {exc}\n\nLocation: {runtime}",
            error=True,
        )
        return 1
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger()
    # Remember the previous level so it can be restored - important because
    # main() may be called by tests inside a larger process.
    original_log_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if os.environ.get("COLE_DIAGNOSTICS") == "1" else logging.INFO)

    # --- State that the cleanup block at the bottom needs to see ----------
    # All declared up front, so `finally` can safely check each one even if
    # startup failed before it was set.
    lock = InstanceLock(runtime / "instance.lock")
    descriptor_path = runtime / "instance.json"
    server = sock = ready_thread = None
    own_lock = False
    stopped = threading.Event()  # the "please shut down" flag
    startup_failures = []
    cleanup_failures = []
    status = 0

    try:
        # --- Step 1: verify data, before competing for the lock -----------
        preflight_manifest = installed_release_contract(data_directory)
        release_id = preflight_manifest["data_release_id"]
        release_contract = manifest_contract_sha256(preflight_manifest)

        # --- Step 2: are we the owner, or is one already running? ---------
        url = acquire_or_find_existing(lock, descriptor_path, release_id, release_contract)
        own_lock = url is None

        if not own_lock:
            # Someone else is serving. Point the browser at them and exit
            # quietly - this is what makes a second double-click just focus the
            # existing window instead of erroring.
            if not args.no_browser:
                open_browser(url)
            if sys.stdout:
                print(f"COLE already running: {url}", flush=True)
        else:
            # Original comment: Only the process that owns the installation
            # lock pays for the complete parsing and cross-file repository
            # validation. Exact manifest equality closes the preflight-to-load
            # replacement gap.
            #
            # "The gap" is the moment between the pre-flight check and this
            # full load, during which the files could in principle be swapped.
            # Comparing the two manifests for exact equality closes it.
            repository = Repository(data_directory)
            if repository.release_id != release_id or repository.manifest != preflight_manifest:
                raise DataError(
                    "The installed COLE release metadata changed during startup. Restart COLE."
                )

            # --- Step 3: claim the port, build the app --------------------
            sock = reserve_socket(args.port)
            port = sock.getsockname()[1]  # ask which port we actually got
            instance_id = secrets.token_hex(24)  # 24 bytes -> 48 hex characters
            # `on_shutdown=stopped.set` hands the app a way to raise the flag
            # this function's main loop is watching. That is how the Quit button
            # in the browser stops the program.
            app = create_app(repository, instance_id=instance_id, on_shutdown=stopped.set)

            # Original comment: Fail before opening a browser if the installed
            # UI bundle is incomplete.
            #
            # A missing app.js would otherwise produce a blank window with no
            # explanation. Better to fail with a message naming the file.
            for relative in (
                "templates/index.html",
                "static/app.js",
                "static/money.js",
                "static/styles.css",
                "static/theme.css",
                "static/vendor/leaflet.js",
                "static/vendor/leaflet.css",
            ):
                if not (Path(app.root_path) / relative).is_file():
                    raise RuntimeError(f"Missing bundled application asset: {relative}")

            # Imported here rather than at the top so a missing Waitress fails
            # at this specific, diagnosable point.
            from waitress import create_server

            server = create_server(
                app,
                sockets=[sock],
                threads=4,
                clear_untrusted_proxy_headers=True,
                max_request_body_size=1024 * 1024,
                channel_timeout=30,
            )

            # --- Step 4: publish the descriptor so later launches find us --
            descriptor = {
                "port": port,
                "instance_id": instance_id,
                "app_version": __version__,
                "data_release_id": repository.release_id,
                "release_contract_sha256": release_contract,
            }
            # ATOMIC WRITE: write to a temporary file, then rename it into
            # place. Renaming is atomic on every major OS, so another process
            # reading the descriptor sees either the old file or the complete
            # new one - never a half-written one.
            temporary = descriptor_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(descriptor), encoding="utf-8")
            temporary.replace(descriptor_path)

            def open_when_ready():
                # Wait for our own server to actually answer, then open a browser.
                # Runs on a separate thread. It has to: the main thread is about
                # to be fully occupied running the server, and the server cannot
                # answer the health check until it is running. So this thread
                # polls from the outside while the main thread serves.
                #
                # Note it verifies via the same public health endpoint any other
                # launch would use - we prove the server works rather than
                # assuming it does because we started it.
                try:
                    confirmed = wait_for_existing(
                        descriptor_path, repository.release_id, release_contract, stopped=stopped
                    )
                    if confirmed and not stopped.is_set():
                        if sys.stdout:
                            print(f"COLE ready: {confirmed}", flush=True)
                        logging.info(
                            "COLE ready at %s; release %s", confirmed, repository.release_id
                        )
                        if not args.no_browser:
                            open_browser(confirmed)
                except Exception as exc:
                    logging.exception("COLE readiness failed")
                    # Hand the failure to the main thread - exceptions cannot
                    # cross thread boundaries on their own - and trip the flag
                    # so the main loop stops.
                    startup_failures.append(exc)
                    stopped.set()

            # daemon=True means this thread will not keep the program alive.
            ready_thread = threading.Thread(
                target=open_when_ready, name="cole-readiness", daemon=True
            )
            ready_thread.start()

            # --- Step 5: THE MAIN SERVER LOOP -----------------------------
            # Original comment: Run one asyncore turn at a time so the owning
            # thread closes every socket after workers finish, without a
            # concurrent close/select race.
            #
            # Normally you would call server.run() and let it block forever.
            # Instead each iteration handles a slice of network activity and
            # returns, so this loop can check `stopped` between slices and shut
            # down in an orderly way from the same thread that owns the sockets.
            # Closing sockets from a different thread than the one watching them
            # is a classic source of intermittent crashes.
            while not stopped.is_set():
                server.asyncore.loop(timeout=0.2, count=1, map=server._map, use_poll=False)
                # Original comment: Waitress handles some listener exceptions
                # internally by removing/closing that dispatcher. Its trigger or
                # existing channels can keep the map nonempty after the listener
                # dies.
                #
                # In plain terms: the server can quietly stop listening while
                # still looking busy. Without this check COLE would spin
                # forever, appearing to run while accepting nothing. So we
                # verify three things each turn: the socket is still valid, the
                # server still accepts, and our socket is still the registered
                # listener.
                if not stopped.is_set() and (
                    sock.fileno() < 0
                    or not server.accepting
                    or server._map.get(sock.fileno()) is not server
                ):
                    raise RuntimeError(
                        "The COLE local listener stopped unexpectedly. Restart COLE."
                    )

            # Loop ended: either the user quit, or the readiness thread failed.
            if startup_failures:
                raise RuntimeError(f"COLE did not become ready: {startup_failures[0]}")

            # --- Step 6: ORDERLY SHUTDOWN, in a deliberate order ----------
            # First let in-flight requests finish. This is why clicking Quit
            # returns a proper "stopping" response instead of cutting the
            # connection mid-reply.
            server.task_dispatcher.shutdown(timeout=5)
            # Original comment: Complete any queued acknowledgment after the
            # last worker has returned. Stop accepting new connections, then
            # bound the drain of existing ones.
            server.close()  # no new connections
            for channel in list(server._map.values()):
                if hasattr(channel, "close_when_flushed"):
                    # Let each open connection finish sending, then close.
                    channel.close_when_flushed = True
            # Drain, but never forever: at most two seconds.
            drain_deadline = time.monotonic() + 2
            while server._map and time.monotonic() < drain_deadline:
                server.asyncore.loop(timeout=0.05, count=1, map=server._map, use_poll=False)
            logging.info("COLE shutdown complete")

    except KeyboardInterrupt:
        # Ctrl+C is a normal way to stop, not an error. `pass` falls through to
        # the cleanup block below with status still 0.
        pass
    except Exception as exc:
        logging.exception("COLE startup or runtime failure")
        # Always tell the user where the log is - this is a desktop app, and
        # they have no other way to find out what happened.
        message = f"COLE could not start or continue running: {exc}\n\nLog: {runtime / 'cole.log'}"
        show_message(message, error=True)
        status = 1

    finally:
        # `finally` ALWAYS runs - success, failure, or Ctrl+C. Everything that
        # must be released goes here.
        stopped.set()  # make sure the readiness thread stops waiting

        def cleanup(label, action):
            # Run one cleanup step; record failure instead of raising.
            # Critical detail: if one cleanup step raised, every later step
            # would be skipped and resources would leak. So each is isolated,
            # and failures are collected and reported at the end.
            try:
                action()
            except Exception as exc:
                cleanup_failures.append(f"{label}: {exc}")
                logging.exception("COLE cleanup failed: %s", label)

        if ready_thread is not None:
            # Original comment: Readiness polling observes stopped and its
            # health request is bounded. Do not let a late callback log or open
            # a closed service.
            #
            # i.e. wait up to a second for that thread so it cannot pop open a
            # browser pointing at a server that just shut down.
            cleanup("readiness thread", lambda: ready_thread.join(timeout=1))
        if server is not None:
            cleanup("workers", lambda: server.task_dispatcher.shutdown(timeout=5))
            cleanup(
                "server sockets",
                lambda: server.asyncore.close_all(map=server._map, ignore_all=True),
            )
        if sock is not None:
            cleanup("reserved socket", sock.close)
        if own_lock:
            # Only the owner removes the descriptor. A second launch that merely
            # found an existing server must not delete that server's file.
            cleanup("instance descriptor", lambda: descriptor_path.unlink(missing_ok=True))
        cleanup("instance lock", lock.close)

        # Original comment: Detach before closing: even a file-close error must
        # not leave a stale handler installed when main is called by tests or
        # another entry point.
        logger.removeHandler(handler)
        cleanup("log file", handler.close)
        logger.setLevel(original_log_level)

    if cleanup_failures:
        show_message(
            f"COLE encountered an error while closing: {'; '.join(cleanup_failures)}\n\nLog: {runtime / 'cole.log'}",
            error=True,
        )
        return 1
    # An "exit code": 0 means success, anything else means failure. The
    # operating system and any calling script read this.
    return status


if __name__ == "__main__":
    # Runs only when this file is executed directly, not when imported.
    # SystemExit carries main()'s return value out as the process exit code.
    raise SystemExit(main())
