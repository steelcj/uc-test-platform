"""Cutting an Incus instance off the network, except the host's SSH.

For the end-to-end scenario's offline step on a machine reached over
SSH, such as a Windows virtual machine. A network ACL, named uniquely
for each run, allows TCP from the host to the instance's port 22 and its
replies, and nothing else. The instance's NIC rejects all other traffic
in both directions. The ACL is applied as an instance-level override of
the NIC.

The cut is undone by four independent layers, so the instance comes back
online even if the script dies:

- the script itself, in a finally block;
- a dead-man's switch on the host: a user systemd timer, armed before
  the cut takes effect, that undoes this cut after a fixed time whether
  or not the script is still running;
- start-up recovery: every run first removes leftover cuts, by the ACL
  name prefix, and their NIC overrides on the target instance;
- the snapshot restore, by the driver, as the final backstop.

Only cuts this module made are touched: an override is removed only when
its ACL carries the prefix, and nothing else on the host is changed.

An offline step has a time limit, and the cut guards it twice: the
dead-man's switch must be set to fire at least DEADMAN_MARGIN seconds
after the limit, and systemd's own count of the time left must exceed
the limit before the step starts. At the end of the step, before the
cut is removed, reprove() requires the cut to be in place and every
negative probe to still fail, so a switch that fired early fails the
step instead of letting it pass with network.

Every self-test run reports the IPv6 internet path as "verified",
"blocked", or "not testable on this network: no IPv6 route on the
host"; it never passes silently. Repeat the self-test on a network with
IPv6 before marking Windows verified.

What the self-test established on its first host, 2026-10-09:

- The cut drops rather than rejects: Incus accepts reject, but denied
  traffic times out, so every probe has a short explicit timeout.
- Incus exempts the bridge's DNS from ACLs. During the cut the instance
  can still query the host's resolver, which forwards DNS queries to the
  internet; nothing else on the host is reachable, and no other host
  service relays to the internet.
- The host has no IPv6 route to the internet, so direct HTTPS over IPv6
  cannot be shown to work before the cut, and is reported as not
  provable; the IPv6 cut is proven by probes to the host over IPv6.

The self-test reaches the instance through a guest: a Linux container
with incus exec by default, or a Windows virtual machine over SSH, as
the Windows driver, uc_test_platform.windows, supplies. The kill test
exercises only the host's side, and runs in a Linux container.

    uc-test-platform cut selftest <container> --snapshot <clean-snapshot>
    uc-test-platform cut killtest <container> --snapshot <clean-snapshot>
    uc-test-platform cut recover <container>
"""

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

PREFIX = "osat-offline-"
RELEASE_URL = "https://github.com/restic/restic/releases/download/v0.19.1/SHA256SUMS"
# Public addresses with HTTPS, one per family; GitHub publishes no IPv6.
HOST_TCP_PORTS = (22, 53, 80, 443, 631, 3128, 3142, 8000, 8080)
# Incus lets an instance reach its bridge's DNS whatever the ACL says.
# The resolver answers names, so DNS queries still reach the internet
# during the cut; it carries no HTTP, which the relay probes check.
EXEMPT_HOST_PORTS = {53}
CONTROL_PORT = 443        # open on the host before the cut; cut during it
STEP_TIMEOUT = 120
STEP_SECONDS = 300        # how long an offline step may take
DEADMAN_MARGIN = 120      # the dead-man's switch fires at least this much later
DEADMAN_SECONDS = 600
SCRIPT = str(Path(__file__).resolve())


def incus(*args, check=True, timeout=STEP_TIMEOUT):
    # stdin is empty: some incus commands read YAML from a non-terminal stdin.
    result = subprocess.run(["incus", *args], capture_output=True, text=True,
                            timeout=timeout, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise RuntimeError(f"incus {' '.join(args)}: {result.stderr.strip()}")
    return result


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)


def query(path: str) -> dict:
    return json.loads(incus("query", path).stdout)


def nic_network(instance: str, nic: str) -> str:
    return query(f"/1.0/instances/{instance}")["expanded_devices"][nic]["network"]


def bridge_addresses(network: str):
    config = query(f"/1.0/networks/{network}")["config"]
    v4 = config.get("ipv4.address", "").split("/")[0] or None
    v6 = config.get("ipv6.address", "").split("/")[0] or None
    return v4, v6


def instance_config(instance: str) -> dict:
    """The instance's own configuration and devices, without the
    volatile keys Incus changes at every start."""
    data = query(f"/1.0/instances/{instance}")
    return {"config": {k: v for k, v in data["config"].items()
                       if not k.startswith("volatile.")},
            "devices": data["devices"]}


def acl_names():
    return [a.rsplit("/", 1)[-1] for a in query("/1.0/network-acls")]


def deadman_units():
    out = systemctl("list-units", "--all", "--plain", "--no-legend",
                    f"{PREFIX}*").stdout
    return sorted({line.split()[0] for line in out.splitlines() if line.strip()})


# ── Undoing a cut ────────────────────────────────────────────────────────────

def undo_cut(instance: str, nic: str, name: str) -> None:
    """Remove the override only if it carries this cut's ACL, then the
    ACL. Idempotent, so a late dead-man's switch is harmless."""
    device = instance_config(instance)["devices"].get(nic)
    if device and device.get("security.acls") == name:
        incus("config", "device", "remove", instance, nic, check=False)
    if name in acl_names():
        incus("network", "acl", "delete", name, check=False)


def disarm(unit: str) -> None:
    systemctl("stop", f"{unit}.timer", f"{unit}.service")
    systemctl("reset-failed", f"{unit}.timer", f"{unit}.service")


def recover(instance: str, nic: str = "eth0") -> list:
    """Start-up recovery: undo every leftover cut on the instance, delete
    every leftover ACL with the prefix, and stop leftover dead-man's
    switches. Returns what it did."""
    done = []
    device = instance_config(instance)["devices"].get(nic)
    if device:
        if device.get("security.acls", "").startswith(PREFIX):
            incus("config", "device", "remove", instance, nic, check=False)
            done.append(f"removed the {nic} override carrying {device['security.acls']}")
        else:
            raise RuntimeError(f"{instance} overrides {nic} with something this "
                               f"module did not make; refusing to touch it")
    for name in acl_names():
        if name.startswith(PREFIX):
            incus("network", "acl", "delete", name, check=False)
            done.append(f"deleted the ACL {name}")
    for unit in deadman_units():
        base = unit.rsplit(".", 1)[0]
        disarm(base)
        done.append(f"stopped {unit}")
    leftovers = [n for n in acl_names() if n.startswith(PREFIX)]
    if leftovers:
        raise RuntimeError(f"could not delete {leftovers}")
    return done


def deadman_remaining(unit: str) -> float:
    """Seconds until systemd fires the dead-man's switch, read from
    systemd itself; 0 when the timer is not pending."""
    out = systemctl("list-timers", "--all", "--output=json").stdout
    for timer in json.loads(out or "[]"):
        if timer.get("unit") == f"{unit}.timer" and timer.get("next"):
            return timer["next"] / 1e6 - time.time()
    return 0.0


def arm_deadman(instance: str, nic: str, name: str, seconds: int) -> str:
    """A user systemd timer that undoes this cut after `seconds`,
    independent of this script."""
    unit = f"{name}-deadman"
    result = subprocess.run(
        # systemd's default timer accuracy is a minute; fire when scheduled.
        ["systemd-run", "--user", f"--unit={unit}", f"--on-active={seconds}s",
         "--timer-property=AccuracySec=1s",
         f"--description=OSAT offline step: undo {name}",
         sys.executable, SCRIPT, "undo", instance, "--nic", nic, "--acl", name],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"could not arm the dead-man's switch: "
                           f"{result.stderr.strip()}")
    return unit


class Cut:
    """An applied cut: its ACL, its dead-man's switch and the step's
    time limit."""

    def __init__(self, instance, nic, name, unit, host4, host6, step_seconds):
        self.instance, self.nic, self.name, self.unit = instance, nic, name, unit
        self.host4, self.host6 = host4, host6
        self.step_seconds = step_seconds
        self.started = time.monotonic()

    def intact(self) -> list:
        """What is wrong with the cut now, or an empty list."""
        problems = []
        device = instance_config(self.instance)["devices"].get(self.nic)
        if not device or device.get("security.acls") != self.name:
            problems.append("the NIC override is gone")
        if self.name not in acl_names():
            problems.append(f"the ACL {self.name} is gone")
        if deadman_remaining(self.unit) <= 0:
            problems.append("the dead-man's switch has fired or is not pending")
        if time.monotonic() - self.started > self.step_seconds:
            problems.append(f"the step took longer than its "
                            f"{self.step_seconds} s limit")
        return problems


def reprove(cut: Cut, negative_probes) -> list:
    """At the end of an offline step, before the cut is removed: the cut
    must still be in place, and every negative probe must still fail.
    negative_probes is a list of (label, callable returning the probe's
    output). Returns the problems found; an empty list means re-proven."""
    problems = cut.intact()
    for label, probe in negative_probes:
        outcome = probe()
        if not outcome.startswith("FAILED"):
            problems.append(f"{label} worked at the end of the step: {outcome}")
    return problems


@contextlib.contextmanager
def offline(instance: str, nic: str = "eth0", deadman_seconds: int = DEADMAN_SECONDS,
            step_seconds: int = STEP_SECONDS, deadman_margin: int = DEADMAN_MARGIN):
    """While inside, the instance exchanges only the host's SSH connection;
    everything else is refused in both directions. Yields a Cut.

    Refuses to start unless the dead-man's switch fires after the step's
    time limit plus a margin, and checks systemd's own count of the time
    left before the step begins."""
    if deadman_seconds < step_seconds + deadman_margin:
        raise RuntimeError(f"the dead-man's switch ({deadman_seconds} s) must "
                           f"fire at least {deadman_margin} s after the "
                           f"step's limit ({step_seconds} s)")
    recover(instance, nic)
    host4, host6 = bridge_addresses(nic_network(instance, nic))
    name = f"{PREFIX}{uuid.uuid4().hex[:12]}"
    unit = None
    try:
        incus("network", "acl", "create", name,
              "--description", "OSAT offline step: the host's SSH only")
        for host in (h for h in (host4, host6) if h):
            incus("network", "acl", "rule", "add", name, "ingress",
                  "action=allow", "protocol=tcp", f"source={host}",
                  "destination_port=22")
            incus("network", "acl", "rule", "add", name, "egress",
                  "action=allow", "protocol=tcp", f"destination={host}",
                  "source_port=22")
        # Armed before the cut takes effect, so it covers every moment of it.
        unit = arm_deadman(instance, nic, name, deadman_seconds)
        incus("config", "device", "override", instance, nic,
              f"security.acls={name}",
              "security.acls.default.ingress.action=reject",
              "security.acls.default.egress.action=reject")
        left = deadman_remaining(unit)
        if left <= step_seconds:
            raise RuntimeError(f"only {left:.0f} s left on the dead-man's "
                               f"switch, not more than the step's "
                               f"{step_seconds} s")
        yield Cut(instance, nic, name, unit, host4, host6, step_seconds)
    finally:
        undo_cut(instance, nic, name)
        if unit:
            disarm(unit)


# ── Probes run inside the instance, with the environment cleared ─────────────
#
# Incus accepts reject as the default action, but on this host denied
# traffic is silently dropped: refusals arrive as timeouts, not resets.
# The cut is correct either way; each probe therefore has a short,
# explicit timeout instead of waiting on the system's.

PROBE_TIMEOUT = 4

HTTPS_PROBE = f"""
import sys, time, urllib.request
start = time.time()
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
    opener.open(sys.argv[1], timeout={PROBE_TIMEOUT}).read(64)
    print("OK", round(time.time() - start, 1))
except Exception as exc:
    print("FAILED", round(time.time() - start, 1), type(exc).__name__, exc)
"""

TCP_PROBE = f"""
import socket, sys, time
family = socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET
start = time.time()
try:
    s = socket.socket(family, socket.SOCK_STREAM); s.settimeout({PROBE_TIMEOUT})
    s.connect((sys.argv[1], int(sys.argv[2]))); s.close()
    print("OK", round(time.time() - start, 1))
except Exception as exc:
    print("FAILED", round(time.time() - start, 1), type(exc).__name__)
"""

RELAY_PROBE = f"""
import socket, sys
family = socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET
answers = []
for request in (b"CONNECT github.com:443 HTTP/1.1\\r\\nHost: github.com:443\\r\\n\\r\\n",
                b"GET http://github.com/ HTTP/1.1\\r\\nHost: github.com\\r\\nConnection: close\\r\\n\\r\\n"):
    try:
        s = socket.socket(family, socket.SOCK_STREAM); s.settimeout({PROBE_TIMEOUT})
        s.connect((sys.argv[1], int(sys.argv[2]))); s.sendall(request)
        answers.append(s.recv(200).split(b"\\r\\n")[0].decode("latin-1") or "(closed)")
        s.close()
    except Exception as exc:
        answers.append("no answer: " + type(exc).__name__)
answered = [a for a in answers if not a.startswith("no answer")]
print(("OK " if answered else "FAILED ") + " | ".join(answers))
"""

DNS_PROBE = f"""
import socket, sys
family = socket.AF_INET6 if ":" in sys.argv[1] else socket.AF_INET
query = (b"\\x12\\x34\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00"
         b"\\x06github\\x03com\\x00\\x00\\x01\\x00\\x01")
s = socket.socket(family, socket.SOCK_DGRAM); s.settimeout({PROBE_TIMEOUT})
try:
    s.sendto(query, (sys.argv[1], 53)); data = s.recv(512)
    print("OK", data[7], "answer(s)")
except Exception as exc:
    print("FAILED", type(exc).__name__)
"""

CLEAN_ENV = ["env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin"]
RELEASE_URL_V6 = "https://[2606:4700:4700::1111]/"


def run_probe(instance, code, *args):
    result = incus("exec", instance, "--", *CLEAN_ENV, "python3", "-c", code,
                   *[str(a) for a in args], check=False, timeout=60)
    return (result.stdout or result.stderr).strip() or "FAILED (no output)"


class ExecGuest:
    """A Linux container reached with incus exec. The self-test reaches
    its instance only through a guest, so another kind, such as a Windows
    virtual machine reached over SSH, can supply its own: prepare() brings
    the instance online with an SSH server and returns the address the
    host's SSH monitor watches, proxies() reports any proxy configured
    inside it, and run() and run_many() run probes inside it. A guest
    must raise, never return a FAILED probe, when it cannot reach the
    instance at all, so a broken channel is not mistaken for a cut."""

    def __init__(self, instance):
        self.instance = instance

    def prepare(self) -> str:
        address = wait_for_network(self.instance, self)
        ensure_sshd(self.instance)
        return address

    def proxies(self) -> str:
        return incus("exec", self.instance, "--", "sh", "-c",
                     "env | grep -i _proxy; grep -i proxy /etc/environment "
                     "/etc/apt/apt.conf /etc/apt/apt.conf.d/* 2>/dev/null",
                     check=False).stdout.strip()

    def run(self, code, *args) -> str:
        return run_probe(self.instance, code, *args)

    def run_many(self, probes) -> list:
        """probes is a list of (code, args); returns their outputs in order."""
        return [self.run(code, *args) for code, args in probes]


def ssh_banner(address: str) -> bool:
    try:
        with socket.create_connection((address, 22), timeout=PROBE_TIMEOUT) as s:
            return s.recv(64).startswith(b"SSH-")
    except OSError:
        return False


class SshMonitor(threading.Thread):
    """Checks the host's SSH connection to the instance every second."""

    def __init__(self, address):
        super().__init__(daemon=True)
        self.address, self.checks, self.failures = address, 0, 0
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            self.checks += 1
            if not ssh_banner(self.address):
                self.failures += 1
            self.stopping.wait(1)


def wait_for_network(instance: str, guest=None) -> str:
    """Wait until the instance has both its IPv4 and a global IPv6
    address and reaches restic's release URL; return its IPv4 address."""
    guest = guest or ExecGuest(instance)
    for _ in range(90):
        state = query(f"/1.0/instances/{instance}/state")
        addresses = (state.get("network") or {}).get("eth0", {}).get("addresses", [])
        v4 = [a["address"] for a in addresses if a["family"] == "inet"]
        v6 = [a for a in addresses if a["family"] == "inet6" and a["scope"] == "global"]
        if v4 and v6 and guest.run(HTTPS_PROBE, RELEASE_URL).startswith("OK"):
            return v4[0]
        time.sleep(2)
    raise RuntimeError(f"{instance} did not come up with IPv4, IPv6 and HTTPS")


def ensure_sshd(instance: str) -> None:
    incus("exec", instance, "--", "sh", "-c",
          "command -v sshd >/dev/null || (apt-get update -qq && "
          "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
          "openssh-server >/dev/null 2>&1); mkdir -p /run/sshd; "
          "(command -v systemctl >/dev/null && systemctl start ssh) || "
          "/usr/sbin/sshd", timeout=600)


# ── Self-test ────────────────────────────────────────────────────────────────

def host_has_ipv6_route() -> bool:
    result = subprocess.run(["ip", "-6", "route", "show", "default"],
                            capture_output=True, text=True, timeout=10)
    return bool(result.stdout.strip())


def ipv6_status(before: str, during: str, after: str) -> str:
    """One of: verified; blocked; not testable on this network; or not
    verified, when the cut failed to stop it. Reported on every run."""
    if before.startswith("OK"):
        if during.startswith("FAILED") and after.startswith("OK"):
            return "verified"
        return f"not verified: before OK, during {during}, after {after}"
    if not host_has_ipv6_route():
        return "not testable on this network: no IPv6 route on the host"
    return f"blocked: the host has an IPv6 route, but the path fails before the cut ({before})"


def paired_probes(host4, host6):
    """(label, code, args): each must succeed before the cut, fail during
    it, and succeed again after it."""
    probes = [("direct HTTPS over IPv4 to restic's release URL, no proxy",
               HTTPS_PROBE, (RELEASE_URL,)),
              ("direct HTTPS over IPv6 to a public address, no proxy",
               HTTPS_PROBE, (RELEASE_URL_V6,))]
    for host in (h for h in (host4, host6) if h):
        family = "IPv6" if ":" in host else "IPv4"
        probes.append((f"{family}: TCP to the host {host}:{CONTROL_PORT}",
                       TCP_PROBE, (host, CONTROL_PORT)))
        for port in (80, 443):
            probes.append((f"{family}: relay attempts through the host {host}:{port}",
                           RELAY_PROBE, (host, port)))
    return probes


def selftest(instance: str, snapshot: str, guest=None) -> int:
    results, notes = [], []
    guest = guest or ExecGuest(instance)

    def record(label, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}",
              flush=True)

    incus("stop", instance, "--force", check=False)
    incus("snapshot", "restore", instance, snapshot)
    name = None
    try:
        incus("start", instance)
        address = guest.prepare()
        proxies = guest.proxies()
        record("no proxy is configured in the instance", not proxies, proxies)
        before_config = instance_config(instance)
        host4, host6 = bridge_addresses(nic_network(instance, "eth0"))
        probes = paired_probes(host4, host6)

        labels = [label for label, _, _ in probes]
        baseline = dict(zip(labels, guest.run_many(
            [(code, args) for _, code, args in probes])))
        record("before: the host's SSH answers", ssh_banner(address))

        monitor = SshMonitor(address)
        with offline(instance) as cut:
            monitor.start()
            record("before the step: the dead-man's switch fires after the "
                   "step's limit", deadman_remaining(cut.unit) > cut.step_seconds,
                   f"{deadman_remaining(cut.unit):.0f} s left, limit "
                   f"{cut.step_seconds} s")
            time.sleep(2)
            during = dict(zip(labels, guest.run_many(
                [(code, args) for _, code, args in probes])))
            open_ports = {}
            for host in (h for h in (host4, host6) if h):
                outputs = guest.run_many([(TCP_PROBE, (host, p)) for p in
                                          HOST_TCP_PORTS] + [(DNS_PROBE, (host,))])
                open_ports[host] = [p for p, out in zip(HOST_TCP_PORTS, outputs)
                                    if out.startswith("OK")]
                notes.append(f"during the cut, DNS on the host {host}: {outputs[-1]}")
            negatives = [(label, (lambda c=code, a=args: guest.run(c, *a)))
                         for label, code, args in probes
                         if baseline[label].startswith("OK")]
            problems = reprove(cut, negatives)
            record("at the end of the step: the cut is still in place and "
                   "every negative probe still fails", not problems,
                   "; ".join(problems))
            monitor.stopping.set()
            monitor.join()
        after = dict(zip(labels, guest.run_many(
            [(code, args) for _, code, args in probes])))
        v6_label = "direct HTTPS over IPv6 to a public address, no proxy"
        status = ipv6_status(baseline[v6_label], during[v6_label], after[v6_label])
        print(f"IPV6  the IPv6 internet path: {status}", flush=True)
        notes.append(f"the IPv6 internet path: {status}")
        if status.startswith("not verified"):
            record("the IPv6 internet path is cut", False, status)

        for label, _, _ in probes:
            before_ok = baseline[label].startswith("OK")
            if not before_ok:
                print(f"N/A   {label}: fails before the cut too, so it proves "
                      f"nothing here: {baseline[label]}", flush=True)
                notes.append(f"not provable on this host: {label}")
                continue
            record(f"{label}: works before the cut", True, baseline[label])
            record(f"{label}: fails during the cut",
                   during[label].startswith("FAILED"), during[label])
            record(f"{label}: works again after the cut",
                   after[label].startswith("OK"), after[label])
            if "relay" in label:
                record(f"{label}: never relays to GitHub",
                       " 200" not in baseline[label] and "github" not in
                       baseline[label].lower(), baseline[label])
        for host, ports in open_ports.items():
            unexpected = [p for p in ports if p not in EXEMPT_HOST_PORTS]
            record(f"during the cut: nothing on the host {host} is reachable "
                   f"but its DNS", not unexpected, f"open: {ports}")
        record("throughout the cut: the host's SSH kept answering",
               monitor.failures == 0 and monitor.checks > 0,
               f"{monitor.checks} checks, {monitor.failures} failed")
        after_config = instance_config(instance)
        record("afterwards: the instance's configuration is as before",
               after_config == before_config)
        record("afterwards: no NIC override", "eth0" not in after_config["devices"])
        record("afterwards: no ACL with the prefix is left",
               not [n for n in acl_names() if n.startswith(PREFIX)])
        record("afterwards: no dead-man's switch is left", not deadman_units(),
               str(deadman_units()))
    except Exception as exc:
        record("the self-test ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            recover(instance)
        except Exception as exc:
            print(f"recovery: {exc}")
        incus("stop", instance, "--force", check=False)
        incus("snapshot", "restore", instance, snapshot, check=False)
    for note in notes:
        print(f"NOTE  {note}")
    print(f"{instance} restored to snapshot {snapshot!r} and stopped")
    return 0 if results and all(results) else 1


# ── Kill test: the clean-up layers that do not need the script ──────────────

def hold(instance: str, deadman_seconds: int) -> None:
    """Apply a cut and wait to be killed. Used by the kill test."""
    recover(instance)
    host4, host6 = bridge_addresses(nic_network(instance, "eth0"))
    name = f"{PREFIX}{uuid.uuid4().hex[:12]}"
    incus("network", "acl", "create", name,
          "--description", "OSAT offline step: kill test")
    for host in (h for h in (host4, host6) if h):
        incus("network", "acl", "rule", "add", name, "ingress", "action=allow",
              "protocol=tcp", f"source={host}", "destination_port=22")
    arm_deadman(instance, "eth0", name, deadman_seconds)
    incus("config", "device", "override", instance, "eth0",
          f"security.acls={name}",
          "security.acls.default.ingress.action=reject",
          "security.acls.default.egress.action=reject")
    print(f"CUT {name}", flush=True)
    while True:
        time.sleep(60)


def cut_in_place(instance):
    device = instance_config(instance)["devices"].get("eth0")
    acls = [n for n in acl_names() if n.startswith(PREFIX)]
    return bool(device) or bool(acls)


def killtest(instance: str, snapshot: str) -> int:
    results = []

    def record(label, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}",
              flush=True)

    def killed_run(deadman_seconds):
        child = subprocess.Popen([sys.executable, SCRIPT, "hold", instance,
                                  "--deadman-seconds", str(deadman_seconds)],
                                 stdout=subprocess.PIPE, text=True,
                                 start_new_session=True)
        line = child.stdout.readline().strip()
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return line

    incus("stop", instance, "--force", check=False)
    incus("snapshot", "restore", instance, snapshot)
    try:
        incus("start", instance)
        wait_for_network(instance)
        before = instance_config(instance)

        # The dead-man's switch alone.
        line = killed_run(deadman_seconds=45)
        record("a run was killed in the middle of the cut", line.startswith("CUT"), line)
        offline_now = run_probe(instance, HTTPS_PROBE, RELEASE_URL)
        record("after the kill, the cut is still in place", cut_in_place(instance)
               and offline_now.startswith("FAILED"), offline_now)
        deadline = time.time() + 120
        while time.time() < deadline and cut_in_place(instance):
            time.sleep(5)
        record("the dead-man's switch alone removed the cut",
               not cut_in_place(instance))
        record("and left the configuration as before",
               instance_config(instance) == before)
        again = run_probe(instance, HTTPS_PROBE, RELEASE_URL)
        record("the instance is online again", again.startswith("OK"), again)
        record("the dead-man's switch left no unit behind", not deadman_units(),
               str(deadman_units()))

        # Start-up recovery alone, with a dead-man's switch too far off to help.
        line = killed_run(deadman_seconds=3600)
        record("a second run was killed in the middle of the cut", line.startswith("CUT"), line)
        record("its cut and its dead-man's switch are in place",
               cut_in_place(instance) and bool(deadman_units()), str(deadman_units()))
        actions = recover(instance)
        record("the next run's start-up recovery alone removed the cut",
               not cut_in_place(instance), "; ".join(actions))
        record("and left the configuration as before",
               instance_config(instance) == before)
        record("and stopped the stale dead-man's switch", not deadman_units(),
               str(deadman_units()))
        again = run_probe(instance, HTTPS_PROBE, RELEASE_URL)
        record("the instance is online again", again.startswith("OK"), again)

        # A dead-man's switch that fires before the step ends is caught.
        negative = [("direct HTTPS to restic's release URL",
                     lambda: run_probe(instance, HTTPS_PROBE, RELEASE_URL))]
        with offline(instance, deadman_seconds=30, step_seconds=10,
                     deadman_margin=5) as cut:
            record("an early-firing switch: the cut holds at first",
                   not reprove(cut, negative))
            time.sleep(50)                  # the step overruns; the switch fires
            problems = reprove(cut, negative)
            device = instance_config(instance)["devices"].get("eth0")
            state = (f"override {'present' if device else 'gone'}, ACL "
                     f"{'present' if cut.name in acl_names() else 'gone'}")
            record("an early-firing switch: the end-of-step re-proof fails, "
                   "with network back", bool(problems) and any(
                       "worked at the end" in p for p in problems),
                   f"{state}; " + "; ".join(problems))
            record("an early-firing switch: the re-proof names the missing "
                   "override and ACL", "the NIC override is gone" in problems
                   and f"the ACL {cut.name} is gone" in problems, state)
        record("and the cut is gone afterwards", not cut_in_place(instance))

        # A switch too short for the step is refused before anything is cut.
        try:
            with offline(instance, deadman_seconds=100, step_seconds=300):
                refused = False
        except RuntimeError as exc:
            refused = "must fire at least" in str(exc)
        record("a dead-man's switch shorter than the step is refused before "
               "any cut", refused and not cut_in_place(instance)
               and not deadman_units())
    except Exception as exc:
        record("the kill test ran to the end", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            recover(instance)
        except Exception as exc:
            print(f"recovery: {exc}")
        incus("stop", instance, "--force", check=False)
        incus("snapshot", "restore", instance, snapshot, check=False)
    print(f"{instance} restored to snapshot {snapshot!r} and stopped")
    return 0 if results and all(results) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="uc-test-platform cut")
    parser.add_argument("command", choices=["selftest", "killtest", "recover",
                                            "undo", "hold"])
    parser.add_argument("instance")
    parser.add_argument("--snapshot")
    parser.add_argument("--nic", default="eth0")
    parser.add_argument("--acl")
    parser.add_argument("--deadman-seconds", type=int, default=DEADMAN_SECONDS)
    args = parser.parse_args(argv)
    if args.command in ("selftest", "killtest") and not args.snapshot:
        parser.error("selftest and killtest need --snapshot")
    if args.command == "selftest":
        return selftest(args.instance, args.snapshot)
    if args.command == "killtest":
        return killtest(args.instance, args.snapshot)
    if args.command == "recover":
        for action in recover(args.instance, args.nic):
            print(action)
        return 0
    if args.command == "undo":
        undo_cut(args.instance, args.nic, args.acl)
        return 0
    hold(args.instance, args.deadman_seconds)
    return 0


# The dead-man's switch and the kill test run this file by its path, as
# SCRIPT, so it stays runnable as a script, from a checkout or an install.
if __name__ == "__main__":
    sys.exit(main())
