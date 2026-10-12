"""Run the tests on Windows, in an Incus virtual machine reached over SSH.

Three parts, each starting from the same clean snapshot of the virtual
machine, which must already hold an ordinary, non-administrator user
reachable over SSH by key, with Python and Git on that user's PATH:

- unit: the unit tests of uc-portage, uc-automa and the repository
  under test, with their install tests, and uc-automa's check on itself;
- selftest: the network cut's self-test, uc_test_platform.incus_acl,
  with the probes run inside the virtual machine over SSH;
- scenario: the repository's tests/e2e_scenario.py, step by step, as
  that user, against real release pages, with the offline restore inside
  a cut;
- offline-install: uc-portage and uc-automa installed with pip
  --no-index, inside a cut, from wheels built on the host: theirs from
  the release tags their VERSION files name, and PyYAML's built from its
  source as pure Python, py3-none-any, so no compiler and no network
  are needed; then the installed uc-automa checks itself at its tag.

The virtual machine has no Incus agent, so everything inside it goes
over SSH, and the cut lets only the host's SSH through. uc-portage and
uc-automa are copied as git bundles of their checkouts beside the
repository under test, so they arrive as repositories; the repository
under test is copied as its working tree, the files git tracks or would
track. The virtual machine is restored to the snapshot and stopped
afterwards, and a record of the run is written into the repository under
test, under tests/e2e/windows/.

    uc-test-platform windows --repository ../osat-manager-restic \\
        --instance windows --snapshot osat-ready-tools --ssh osat-windows \\
        [--part unit]
"""

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import incus_acl as acl

# Set by main() from --repository: the repository under test, and the
# directory that holds it beside the uc-portage and uc-automa checkouts.
REPO = Path()
DEVELOPMENT = Path()

PARTS = ["unit", "selftest", "scenario", "offline-install"]
STEPS = ["snapshot", "install-manager", "install-two", "switch",
         "rename-alias", "protection", "remove", "OFFLINE",
         "restore-offline", "ONLINE", "uninstall", "compare"]
BUNDLED = ["uc-portage", "uc-automa"]
PYYAML = "6.0.3"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5"]
SRC = r"%USERPROFILE%\src"
VENV = SRC + r"\venv\Scripts"


class SshFailed(RuntimeError):
    pass


# What the record must not publish, learnt from the machine as the run
# goes, each with the placeholder that stands in for it.
PRIVATE = {}


def learn(host, instance):
    names = ssh(host, "echo %USERNAME%^|%COMPUTERNAME%", timeout=60).stdout.strip()
    if "|" in names:
        user, computer = names.split("|", 1)
        PRIVATE[user] = "<user>"
        PRIVATE[computer] = "<computer>"
    host4, host6 = acl.bridge_addresses(acl.nic_network(instance, "eth0"))
    PRIVATE.update({a: p for a, p in ((host4, "<host-ipv4>"), (host6, "<host-ipv6>")) if a})
    state = acl.query(f"/1.0/instances/{instance}/state")
    for a in state["network"]["eth0"]["addresses"]:
        PRIVATE[a["address"]] = "<vm-ipv4>" if a["family"] == "inet" else "<vm-ipv6>"


def placeholders(text):
    for value in sorted(PRIVATE, key=len, reverse=True):
        # Whole values only, so 10.0.0.1 is not replaced inside 10.0.0.12.
        text = re.sub(r"(?<![\w.:])" + re.escape(value) + r"(?![\w]|\.\d)",
                      PRIVATE[value], text, flags=re.IGNORECASE)
    return text


def ssh(host, command, input_text=None, timeout=1800):
    """Run a cmd.exe command line in the virtual machine."""
    return subprocess.run(["ssh", *SSH_OPTIONS, host, command], input=input_text,
                          capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace",
                          stdin=None if input_text is not None else subprocess.DEVNULL)


def remote_python(host, code, timeout=300):
    """Run Python code inside the virtual machine, isolated from the
    current directory and writing no bytecode cache into the user's own
    interpreter, and return its output; raise if SSH itself fails."""
    result = ssh(host, "python -I -B -", input_text=code, timeout=timeout)
    if result.returncode == 255:
        raise SshFailed(f"SSH to {host} failed: {result.stderr.strip()}")
    return result


# Runs each probe as its own Python process, with proxies and the rest of
# the environment cleared, all at once, and prints their outputs as JSON.
PROBE_RUNNER = """
import base64, json, os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
probes = json.loads(base64.b64decode({payload!r}))
env = {{"SYSTEMROOT": os.environ["SYSTEMROOT"],
        "PATH": os.path.join(os.environ["SYSTEMROOT"], "System32")}}
def one(probe):
    code, args = probe
    r = subprocess.run([sys.executable, "-I", "-B", "-c", code, *args], env=env,
                       capture_output=True, text=True, timeout=60)
    return (r.stdout or r.stderr).strip() or "FAILED (no output)"
with ThreadPoolExecutor(len(probes) or 1) as pool:
    print(json.dumps(list(pool.map(one, probes))))
"""

PROXY_REPORT = r"""
import os, subprocess, winreg
found = [f"{k}={v}" for k, v in os.environ.items() if k.lower().endswith("_proxy")]
key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                     r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL"):
    try:
        value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        continue
    if value not in (0, ""):
        found.append(f"Internet Settings {name}={value}")
winhttp = subprocess.run(["netsh", "winhttp", "show", "proxy"],
                         capture_output=True, text=True).stdout
if "Direct access" not in winhttp:
    found.append("WinHTTP: " + " ".join(winhttp.split()))
print("\n".join(found))
"""

ENVIRONMENT_REPORT = r"""
import ctypes, getpass, platform, subprocess, sys
git = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip()
print(f"{platform.platform()}, Python {platform.python_version()} at "
      f"{sys.executable}, {git}, user {getpass.getuser()}, "
      f"{'an ADMINISTRATOR' if ctypes.windll.shell32.IsUserAnAdmin() else 'not an administrator'}")
"""


class SshGuest:
    """The Windows virtual machine, for incus_acl's self-test."""

    def __init__(self, host, instance):
        self.host, self.instance = host, instance

    def prepare(self) -> str:
        wait_for_ssh(self.host)
        address = acl.wait_for_network(self.instance, self)
        learn(self.host, self.instance)
        return address

    def proxies(self) -> str:
        return remote_python(self.host, PROXY_REPORT).stdout.strip()

    def run(self, code, *args) -> str:
        return self.run_many([(code, args)])[0]

    def run_many(self, probes) -> list:
        payload = base64.b64encode(json.dumps(
            [[code, [str(a) for a in args]] for code, args in probes]).encode()).decode()
        result = remote_python(self.host, PROBE_RUNNER.format(payload=payload))
        try:
            outputs = json.loads(result.stdout)
        except ValueError:
            raise SshFailed(f"the probe runner failed: {result.stdout} {result.stderr}")
        if len(outputs) != len(probes):
            raise SshFailed("the probe runner returned the wrong number of results")
        return outputs


# ── The virtual machine ──────────────────────────────────────────────────────

def wait_for_ssh(host):
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            if ssh(host, "echo ready", timeout=30).stdout.strip() == "ready":
                break
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    else:
        raise RuntimeError(f"{host} did not answer over SSH after the restore")


def restore(instance, snapshot, host):
    acl.incus("stop", instance, "--force", check=False, timeout=300)
    acl.incus("snapshot", "restore", instance, snapshot, timeout=600)
    acl.incus("start", instance, timeout=300)
    wait_for_ssh(host)
    learn(host, instance)
    configured = subprocess.run(["ssh", "-G", host], capture_output=True, text=True,
                                check=True).stdout
    hostname = [line.split()[1] for line in configured.splitlines()
                if line.startswith("hostname ")][0]
    state = acl.query(f"/1.0/instances/{instance}/state")
    addresses = [a["address"] for a in state["network"]["eth0"]["addresses"]
                 if a["family"] == "inet"]
    if hostname not in addresses:
        raise RuntimeError(f"{host} resolves to {hostname}, but {instance} has "
                           f"{addresses}; the SSH monitor would watch the wrong "
                           f"address")


def stop_restored(instance, snapshot):
    acl.incus("stop", instance, "--force", check=False, timeout=300)
    acl.incus("snapshot", "restore", instance, snapshot, check=False, timeout=600)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          check=True).stdout


def sources_tar() -> bytes:
    """src/osat-manager-restic/ as its working tree, and src/<name>.bundle
    for each sibling repository."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        listed = git(REPO, "ls-files", "-z", "--cached", "--others",
                     "--exclude-standard").decode().split("\0")
        for rel in sorted(r for r in listed if r):
            path = REPO / rel
            if path.is_file():
                tar.add(str(path), arcname=f"src/{REPO.name}/{rel}")
        for name in BUNDLED:
            bundle = subprocess.run(["git", "-C", str(DEVELOPMENT / name), "bundle",
                                     "create", "-", "--all"], capture_output=True,
                                    check=True).stdout
            info = tarfile.TarInfo(f"src/{name}.bundle")
            info.size = len(bundle)
            tar.addfile(info, io.BytesIO(bundle))
    return buffer.getvalue()


def copy_sources(host, log):
    with tempfile.TemporaryDirectory() as scratch:
        local = Path(scratch) / "src.tar"
        local.write_bytes(sources_tar())
        subprocess.run(["scp", "-q", *SSH_OPTIONS, str(local), f"{host}:src.tar"],
                       check=True, timeout=600)
    clones = " && ".join(f"git clone -q -b main {name}.bundle {name} && del {name}.bundle"
                         for name in BUNDLED)
    result = ssh(host, f"tar -xf src.tar && del src.tar && cd /d {SRC} && {clones}")
    if result.returncode != 0:
        raise RuntimeError(f"copying the sources failed: {result.stdout}{result.stderr}")
    described = []
    for name in BUNDLED:
        described.append(f"{name} at {git(DEVELOPMENT / name, 'rev-parse', '--short', 'HEAD').decode().strip()}")
    head = git(REPO, "rev-parse", "--short", "HEAD").decode().strip()
    dirty = git(REPO, "status", "--porcelain").decode().strip()
    described.append(f"{REPO.name} at {head}" + (" with uncommitted changes" if dirty else ""))
    log.append("Sources: " + ", ".join(described))


def environment(host, log):
    log.append("Platform: " + remote_python(host, ENVIRONMENT_REPORT).stdout.strip())


def logged(log, title, result):
    output = (result.stdout + result.stderr).rstrip()
    log += [f"== {title} (exit {result.returncode})", output]
    print(f"== {title} (exit {result.returncode})\n{output}", flush=True)
    return result.returncode == 0


# ── The parts ───────────────────────────────────────────────────────────────

def part_unit(args, log):
    host = args.ssh
    copy_sources(host, log)
    environment(host, log)
    # PyYAML 6.0.3 publishes no Windows wheel for Python 3.15, and building
    # its C extension needs a compiler the machine does not have, so PyYAML
    # is built as pure Python, here and in the install tests.
    pure = "set PYYAML_FORCE_LIBYAML=0&& "
    log.append("PyYAML: built without its C extension (PYYAML_FORCE_LIBYAML=0), "
               "because PyYAML 6.0.3 has no Windows wheel for Python 3.15")
    steps = [
        ("create a virtual environment", f"python -m venv {SRC}\\venv"),
        ("install uc-portage and uc-automa, editable",
         f"{VENV}\\python -m pip install -q --disable-pip-version-check "
         f"-e {SRC}\\uc-portage -e {SRC}\\uc-automa"),
        ("uc-portage unit tests, with install tests",
         f"cd /d {SRC}\\uc-portage && set UC_PORTAGE_INSTALL_TESTS=1&& "
         f"{VENV}\\python -m unittest discover -s tests"),
        ("uc-automa unit tests, with install tests",
         f"cd /d {SRC}\\uc-automa && set UC_AUTOMA_INSTALL_TESTS=1&& "
         f"{VENV}\\python -m unittest discover -s tests"),
        ("uc-automa check on itself", f"cd /d {SRC}\\uc-automa && {VENV}\\uc-automa check"),
        (f"{REPO.name} unit tests, with install tests",
         f"cd /d {SRC}\\{REPO.name} && set OSAT_INSTALL_TESTS=1&& "
         f"{VENV}\\python -m unittest discover -s tests\\unit"),
    ]
    ok = True
    for title, command in steps:
        passed = logged(log, title, ssh(host, pure + command))
        ok = ok and passed
        if not passed and title.startswith(("create", "install")):
            return False
    return ok


def part_selftest(args, log):
    buffer = io.StringIO()

    class Tee(io.TextIOBase):
        def write(self, text):
            sys.__stdout__.write(text)
            sys.__stdout__.flush()
            return buffer.write(text)

    with contextlib.redirect_stdout(Tee()):
        code = acl.selftest(args.instance, args.snapshot,
                            guest=SshGuest(args.ssh, args.instance))
    log += ["== the network cut's self-test, probes over SSH", buffer.getvalue().rstrip()]
    return code == 0


def enter_cut(stack, guest, instance, log):
    """Cut the network, after finding which probes work, so they can be
    re-proven to fail at the end of the step."""
    host4, host6 = acl.bridge_addresses(acl.nic_network(instance, "eth0"))
    probes = acl.paired_probes(host4, host6)
    before = guest.run_many([(code, a) for _, code, a in probes])
    negatives = [(label, (lambda c=code, a=a: guest.run(c, *a)))
                 for (label, code, a), out in zip(probes, before)
                 if out.startswith("OK")]
    cut = stack.enter_context(acl.offline(instance))
    log.append(f"== network cut: {cut.name}, the host's SSH only; "
               f"{len(negatives)} probes worked before it and are "
               f"re-proven at the end of the step")
    return cut, negatives


def leave_cut(stack, cut, negatives, log):
    problems = acl.reprove(cut, negatives)
    if problems:
        log.append("== the cut was not re-proven: " + "; ".join(problems))
        return False
    stack.close()
    log.append("== at the end of the step the cut was intact and every "
               "probe still failed; the network is reconnected")
    return True


def part_scenario(args, log):
    host, instance = args.ssh, args.instance
    copy_sources(host, log)
    environment(host, log)
    guest = SshGuest(host, instance)
    script = f"{SRC}\\{REPO.name}\\tests\\e2e_scenario.py"
    # The comparison skips src, so the account snapshot is kept there.
    prefix = f"set E2E_SNAPSHOT={SRC}\\e2e-snapshot.json&& "
    with contextlib.ExitStack() as stack:
        cut, negatives = None, []
        for step in STEPS:
            if step == "OFFLINE":
                cut, negatives = enter_cut(stack, guest, instance, log)
                continue
            if step == "ONLINE":
                if not leave_cut(stack, cut, negatives, log):
                    return False
                continue
            # -B: the harness writes no bytecode cache; the manager's own
            # processes run as they would for anyone.
            if not logged(log, step, ssh(host, f"{prefix}python -B {script} {step}")):
                return False
    return True


def build_wheels(scratch: Path, log) -> Path:
    """Wheels for uc-portage and uc-automa from their release tags, and
    PyYAML's built from its source with its C extension off, in a fresh
    virtual environment on the host."""
    wheels = scratch / "wheels"
    venv = scratch / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    pip = [str(venv / "bin" / "python"), "-m", "pip", "-q",
           "--disable-pip-version-check"]
    for name in BUNDLED:
        repo = DEVELOPMENT / name
        tag = "v" + git(repo, "show", "HEAD:VERSION").decode().strip()
        tree = scratch / "src" / name
        tree.mkdir(parents=True)
        archive = subprocess.run(["git", "-C", str(repo), "archive", tag],
                                 capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(str(tree), filter="data")
        subprocess.run([*pip, "wheel", "--no-deps", "-w", str(wheels), str(tree)],
                       check=True)
        log.append(f"Built from {name}'s tag {tag}")
    subprocess.run([*pip, "wheel", "--no-deps", "--no-binary", "pyyaml", "-w",
                    str(wheels), f"pyyaml=={PYYAML}"], check=True,
                   env=dict(os.environ, PYYAML_FORCE_LIBYAML="0"))
    pure = wheels / f"pyyaml-{PYYAML}-py3-none-any.whl"
    if not pure.is_file():
        raise RuntimeError(f"PyYAML did not build as py3-none-any: "
                           f"{sorted(p.name for p in wheels.iterdir())}")
    with zipfile.ZipFile(pure) as z:
        compiled = [n for n in z.namelist() if n.endswith((".so", ".pyd", ".dll"))]
    if compiled:
        raise RuntimeError(f"the PyYAML wheel holds compiled code: {compiled}")
    for wheel in sorted(wheels.iterdir()):
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        log.append(f"Wheel {wheel.name} sha256 {digest}")
    return wheels


def part_offline_install(args, log):
    host, instance = args.ssh, args.instance
    copy_sources(host, log)
    environment(host, log)
    with tempfile.TemporaryDirectory() as scratch:
        wheels = build_wheels(Path(scratch), log)
        local = Path(scratch) / "wheels.tar"
        with tarfile.open(str(local), "w") as tar:
            tar.add(str(wheels), arcname="src/wheels")
        subprocess.run(["scp", "-q", *SSH_OPTIONS, str(local), f"{host}:wheels.tar"],
                       check=True, timeout=600)
    if not logged(log, "unpack the wheels", ssh(host, "tar -xf wheels.tar && del wheels.tar")):
        return False
    tag = "v" + git(DEVELOPMENT / "uc-automa", "show", "HEAD:VERSION").decode().strip()
    offline = SRC + r"\offline\Scripts"
    steps = [
        ("create a virtual environment", f"python -m venv {SRC}\\offline"),
        ("install uc-automa and its dependencies from the wheels, no index",
         f"{offline}\\python -m pip install --no-index --disable-pip-version-check "
         f"--find-links {SRC}\\wheels uc-automa"),
        ("PyYAML is the pure build, without libyaml",
         f"{offline}\\python -I -B -c \"import sys, yaml; "
         f"print('PyYAML', yaml.__version__, 'libyaml', yaml.__with_libyaml__); "
         f"sys.exit(1 if yaml.__with_libyaml__ or yaml.__version__ != '{PYYAML}' else 0)\""),
        ("the installed commands run", f"{offline}\\uc-portage --version && "
         f"{offline}\\uc-automa --version"),
        (f"the installed uc-automa checks uc-automa at {tag}",
         f"cd /d {SRC}\\uc-automa && git checkout -q {tag} && {offline}\\uc-automa check"),
    ]
    guest = SshGuest(host, instance)
    with contextlib.ExitStack() as stack:
        cut, negatives = enter_cut(stack, guest, instance, log)
        for title, command in steps:
            if not logged(log, title, ssh(host, command)):
                return False
        if not leave_cut(stack, cut, negatives, log):
            return False
    return True


def main(argv=None):
    global REPO, DEVELOPMENT
    parser = argparse.ArgumentParser(prog="uc-test-platform windows")
    parser.add_argument("--repository", required=True, type=Path,
                        help="The checkout under test; uc-portage and uc-automa "
                             "are taken from checkouts beside it.")
    parser.add_argument("--instance", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--ssh", required=True, help="The SSH host alias.")
    parser.add_argument("--part", choices=PARTS, action="append",
                        help="Run only this part; may be repeated.")
    args = parser.parse_args(argv)
    REPO = args.repository.resolve()
    DEVELOPMENT = REPO.parent
    parts = args.part or PARTS
    started = datetime.now(timezone.utc)
    results, logs = {}, {}
    for part in parts:
        log = logs[part] = []
        try:
            if part != "selftest":
                restore(args.instance, args.snapshot, args.ssh)
            results[part] = {"unit": part_unit, "selftest": part_selftest,
                             "scenario": part_scenario,
                             "offline-install": part_offline_install}[part](args, log)
        except Exception as exc:
            log.append(f"== stopped: {type(exc).__name__}: {exc}")
            print(log[-1], flush=True)
            results[part] = False
        finally:
            try:
                acl.recover(args.instance)
            except Exception as exc:
                log.append(f"== recovery: {exc}")
            stop_restored(args.instance, args.snapshot)
            log.append(f"== {args.instance} restored to snapshot "
                       f"{args.snapshot!r} and stopped")
    finished = datetime.now(timezone.utc)
    titles = {"unit": "Unit tests", "selftest": "Network cut self-test",
              "scenario": "End-to-end scenario",
              "offline-install": "Offline install from wheels"}
    lines = ["# Windows test run", ""]
    for part in parts:
        lines.append(f"- {titles[part]}: {'passed' if results[part] else 'FAILED'}")
    lines += ["", f"Started {started:%Y-%m-%dT%H:%M:%SZ}, finished "
              f"{finished:%Y-%m-%dT%H:%M:%SZ}, in the Incus virtual machine "
              f"{args.instance}, reached over SSH and restored to its clean "
              f"snapshot {args.snapshot!r} before each part and afterwards. "
              f"The account name, the computer name and the network addresses "
              f"are replaced by placeholders.", ""]
    for part in parts:
        lines += [f"## {titles[part]}", "", "```text", *logs[part], "```", ""]
    records = REPO / "tests" / "e2e" / "windows"
    records.mkdir(parents=True, exist_ok=True)
    record = records / f"{started.astimezone():%Y-%m-%d}--windows-test-run.md"
    record.write_text(placeholders("\n".join(lines)), encoding="utf-8")
    print(f"record written to {record.relative_to(REPO)}")
    return 0 if all(results.values()) else 1

