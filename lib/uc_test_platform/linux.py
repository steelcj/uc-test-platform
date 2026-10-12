"""Run the end-to-end scenario on Linux, in a fresh Incus container.

Launches a container from a clean image, creates an ordinary user,
copies the repository under test in, and runs each step of its
tests/e2e_scenario.py as that user against real release pages.
Disconnecting the network for the offline restore is done here, as root,
between steps. The container is deleted afterwards. A record of the run
is written into the repository under test, under tests/e2e/linux/.

    uc-test-platform linux --repository <checkout> [--image debian/12]
    uc-test-platform linux --repository <checkout> --instance <container> --snapshot <clean-snapshot>

With --instance, an existing container is restored to a clean snapshot
before the run and restored to it again afterwards, instead of launching
and deleting a new one.
"""

import argparse
import io
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Set by main() from --repository: the repository under test.
REPO = Path()
USER, UID = "tester", 1500
STEPS = ["snapshot", "install-manager", "install-two", "switch",
         "rename-alias", "protection", "remove", "OFFLINE",
         "restore-offline", "ONLINE", "uninstall", "compare"]


def incus(*args, input_bytes=None, check=True):
    result = subprocess.run(["incus", *args], input=input_bytes,
                            capture_output=True)
    if check and result.returncode != 0:
        raise SystemExit(f"incus {' '.join(args)} failed:\n"
                         f"{result.stderr.decode(errors='replace')}")
    return result


def as_root(name, *argv, check=True):
    return incus("exec", name, "--", *argv, check=check)


def as_user(name, *argv):
    return incus("exec", name, "--user", str(UID), "--group", str(UID),
                 "--cwd", f"/home/{USER}",
                 "--env", f"HOME=/home/{USER}",
                 "--env", f"PATH=/home/{USER}/.local/bin:/usr/local/bin:/usr/bin:/bin",
                 "--", *argv, check=False)


def checkout_tar() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(REPO.rglob("*")):
            rel = path.relative_to(REPO)
            if rel.parts[0] == ".git" or "__pycache__" in rel.parts:
                continue
            info = tar.gettarinfo(str(path), arcname=f"src/{rel.as_posix()}")
            info.uid = info.gid = UID
            info.uname = info.gname = USER
            if path.is_file():
                with path.open("rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)
    return buffer.getvalue()


def main(argv=None):
    global REPO
    parser = argparse.ArgumentParser(prog="uc-test-platform linux")
    parser.add_argument("--repository", required=True, type=Path,
                        help="The checkout under test.")
    parser.add_argument("--image", default="debian/12")
    parser.add_argument("--instance", help="An existing container to use.")
    parser.add_argument("--snapshot", help="With --instance: the clean "
                        "snapshot to restore before and after the run.")
    parser.add_argument("--keep", action="store_true",
                        help="Keep the container for inspection.")
    args = parser.parse_args(argv)
    REPO = args.repository.resolve()
    log = []
    started = datetime.now(timezone.utc)
    if args.instance:
        if not args.snapshot:
            raise SystemExit("--instance needs --snapshot, to start clean")
        name = args.instance
        source = f"{name}, restored to snapshot {args.snapshot!r}"
        incus("stop", name, "--force", check=False)
        incus("snapshot", "restore", name, args.snapshot)
        incus("start", name)
    else:
        name = f"{REPO.name}-e2e-{int(time.time())}"
        # A name with a slash is a remote image; anything else is a local
        # image fingerprint or alias, which avoids the remote server.
        source = f"images:{args.image}" if "/" in args.image else args.image
        incus("launch", source, name, "-q")
    ok = False
    try:
        for _ in range(60):
            if as_root(name, "true", check=False).returncode == 0 and \
                    as_root(name, "getent", "hosts", "github.com",
                            check=False).returncode == 0:
                break
            time.sleep(2)
        if as_root(name, "which", "python3", check=False).returncode != 0:
            as_root(name, "sh", "-c", "apt-get update -qq && apt-get install "
                    "-y -qq python3 >/dev/null 2>&1 || dnf -y -q install "
                    "python3 >/dev/null 2>&1")
        if as_root(name, "id", "-u", str(UID), check=False).returncode == 0:
            raise SystemExit(f"uid {UID} already exists in {name}; the "
                             f"scenario needs a fresh account")
        as_root(name, "useradd", "-m", "-u", str(UID), "-s", "/bin/sh", USER)
        incus("exec", name, "--", "tar", "-x", "-C", f"/home/{USER}",
              input_bytes=checkout_tar())
        python = as_root(name, "python3", "--version").stdout.decode().strip()
        os_name = as_root(name, "sh", "-c", ". /etc/os-release; echo $PRETTY_NAME"
                          ).stdout.decode().strip()
        log += [f"Image: {source} ({os_name}), {python}, "
                f"user {USER} (uid {UID}), not root", ""]
        for step in STEPS:
            if step == "OFFLINE":
                as_root(name, "ip", "link", "set", "eth0", "down")
                log.append("== network disconnected: ip link set eth0 down")
                continue
            if step == "ONLINE":
                as_root(name, "ip", "link", "set", "eth0", "up")
                log.append("== network reconnected")
                continue
            result = as_user(name, "python3", f"/home/{USER}/src/tests/e2e_scenario.py", step)
            output = (result.stdout + result.stderr).decode(errors="replace")
            log.append(output.rstrip())
            print(output.rstrip())
            if result.returncode != 0:
                raise SystemExit(f"step {step} failed")
        ok = True
    finally:
        finished = datetime.now(timezone.utc)
        if args.instance:
            incus("stop", name, "--force", check=False)
            incus("snapshot", "restore", name, args.snapshot, check=False)
            log.append(f"== {name} restored to snapshot {args.snapshot!r} "
                       f"and stopped")
        elif not args.keep:
            incus("delete", "-f", name, check=False)
        records = REPO / "tests" / "e2e" / "linux"
        records.mkdir(parents=True, exist_ok=True)
        record = records / f"{started.astimezone():%Y-%m-%d}--linux-scenario-run.md"
        record.write_text("\n".join([
            "# Linux end-to-end scenario run", "",
            f"Result: {'passed' if ok else 'FAILED'}", "",
            f"Started {started:%Y-%m-%dT%H:%M:%SZ}, finished "
            f"{finished:%Y-%m-%dT%H:%M:%SZ}, in "
            + (f"the Incus container {name}, restored to its clean snapshot "
               f"{args.snapshot!r} before and after the run."
               if args.instance else
               "a fresh Incus container that was deleted afterwards."), "",
            "```text", *log, "```", ""]), encoding="utf-8")
        print(f"record written to {record.relative_to(REPO)}")
    return 0 if ok else 1

