#!/usr/bin/env python3
"""Wireless WAP / WiFi dashboard — single-file CLI + optional web UI.

Sections:
  1. Constants / defaults
  2. Models (Profile, WiFiProfile, WAPProfile, WirelessInterface, META)
  3. Process manager (hostapd / wpa_supplicant / dnsmasq children)
  4. Apply (conf gen, uplink, NAT)
  5. Scan
  6. HTTP API
  7. Embedded UI (HTML/CSS/JS)
  8. CLI
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# =============================================================================
# 1. Constants / defaults
# =============================================================================

DEFAULT_HOST_ADDR = "192.168.12.1"
_host_addr: str = DEFAULT_HOST_ADDR
DEFAULT_COUNTRY = "IN"
DEFAULT_CHANNEL = 149
DEFAULT_WAP_SSID = "BloodPoint"
DEFAULT_WAP_PASSWORD = "12345678"
RUN_DIR = Path(os.environ.get("WIRELESS_DASHBOARD_RUN", "/run/wireless-dashboard"))
# URL prefix when reverse-proxied (e.g. Tailscale serve --set-path=/wireless)
BASE_PATH = os.environ.get("WIRELESS_DASHBOARD_BASE", "").rstrip("/")
PING_TARGET = "tailscale.com"

_children: dict[str, subprocess.Popen] = {}
_nat_pair: tuple[str, str] | None = None  # (ap_iface, uplink)
_config_path: Path = Path("./state.json")
_meta: "META | None" = None
_activity_log: list[dict[str, str]] = []
_LOG_MAX = 200
# iface -> {profile, mode, ssid, uplink, ok, detail}
_runtime: dict[str, dict[str, Any]] = {}


def log_event(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _activity_log.append({"time": ts, "message": msg})
    if len(_activity_log) > _LOG_MAX:
        del _activity_log[0 : len(_activity_log) - _LOG_MAX]
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def connection_summary() -> str:
    if not _runtime:
        return "No interfaces applied yet. Click for logs."
    parts: list[str] = []
    for iface, info in sorted(_runtime.items()):
        if info.get("ok"):
            mode = info.get("mode")
            if mode == "off":
                parts.append(f"{iface}: off")
            elif mode == "wifi":
                parts.append(f"{iface}: wifi → {info.get('ssid')}")
            else:
                up = info.get("uplink") or "?"
                parts.append(f"{iface}: wap “{info.get('ssid')}” via {up}")
        else:
            parts.append(f"{iface}: FAIL — {info.get('detail')}")
    return " · ".join(parts) + "  (click for logs)"


def status_payload() -> dict[str, Any]:
    return {
        "summary": connection_summary(),
        "runtime": _runtime,
        "active_profiles": {
            iface: info.get("profile")
            for iface, info in _runtime.items()
            if info.get("ok")
        },
        "logs": list(_activity_log),
    }


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def get_host_addr() -> str:
    return _host_addr


def default_off_dict() -> dict[str, Any]:
    return {"mode": "off"}


def default_wap_dict(ssid: str | None = None) -> dict[str, Any]:
    return {
        "mode": "wap",
        "ssid": ssid or DEFAULT_WAP_SSID,
        "password": DEFAULT_WAP_PASSWORD,
        "uplink": None,
        "channel": DEFAULT_CHANNEL,
        "country_code": DEFAULT_COUNTRY,
    }


def wpa_psk_hex(ssid: str, password: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), ssid.encode("utf-8"), 4096, 32
    ).hex()


# =============================================================================
# 2. Models
# =============================================================================


class Profile(ABC):
    mode: str
    ssid: str
    password: str

    def __init__(self, ssid: str, password: str) -> None:
        self.ssid = ssid
        self.password = password

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        ...

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Profile":
        mode = data.get("mode", "off")
        if mode == "off":
            return OffProfile.from_dict(data)
        if mode == "wifi":
            return WiFiProfile.from_dict(data)
        return WAPProfile.from_dict(data)


class OffProfile(Profile):
    """Bring the interface down; default profile at index 0."""

    mode = "off"

    def __init__(self) -> None:
        super().__init__(ssid="", password="")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": "off"}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None = None) -> "OffProfile":
        return cls()


class WiFiProfile(Profile):
    mode = "wifi"

    def __init__(
        self,
        ssid: str,
        password: str,
        scan_ssid: bool = True,
        key_mgmt: str = "WPA-PSK",
    ) -> None:
        super().__init__(ssid, password)
        self.scan_ssid = scan_ssid
        self.key_mgmt = key_mgmt

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "wifi",
            "ssid": self.ssid,
            "password": self.password,
            "scan_ssid": self.scan_ssid,
            "key_mgmt": self.key_mgmt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WiFiProfile":
        pw = data.get("password") or data.get("psk") or ""
        return cls(
            ssid=str(data.get("ssid", "")),
            password=str(pw),
            scan_ssid=bool(data.get("scan_ssid", True)),
            key_mgmt=str(data.get("key_mgmt", "WPA-PSK")),
        )


class WAPProfile(Profile):
    mode = "wap"

    def __init__(
        self,
        ssid: str,
        password: str,
        uplink: str | None = None,
        channel: int = DEFAULT_CHANNEL,
        country_code: str = DEFAULT_COUNTRY,
    ) -> None:
        super().__init__(ssid, password)
        self.uplink = uplink
        self.channel = int(channel)
        self.country_code = country_code or DEFAULT_COUNTRY

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "wap",
            "ssid": self.ssid,
            "password": self.password,
            "uplink": self.uplink,
            "channel": self.channel,
            "country_code": self.country_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WAPProfile":
        return cls(
            ssid=str(data.get("ssid", DEFAULT_WAP_SSID)),
            password=str(data.get("password", DEFAULT_WAP_PASSWORD)),
            uplink=data.get("uplink"),
            channel=int(data.get("channel", DEFAULT_CHANNEL)),
            country_code=str(data.get("country_code", DEFAULT_COUNTRY)),
        )


class WirelessInterface:
    def __init__(self, name: str, profile: int = 0) -> None:
        self.name = name
        self.profile = int(profile)

    def to_dict(self) -> dict[str, Any]:
        return {"profile": self.profile}

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "WirelessInterface":
        return cls(name=name, profile=int(data.get("profile", 0)))


class META:
    def __init__(
        self,
        interfaces: dict[str, WirelessInterface] | None = None,
        profiles: list[Profile] | None = None,
    ) -> None:
        self.interfaces = interfaces or {}
        self.profiles = profiles or []
        self.ensure_defaults()

    def ensure_defaults(self) -> None:
        if not self.profiles:
            self.profiles = [OffProfile.from_dict()]
        elif not isinstance(self.profiles[0], OffProfile):
            # Migrate: insert off at 0 and shift existing indices
            self.profiles.insert(0, OffProfile.from_dict())
            for iface in self.interfaces.values():
                iface.profile += 1

        seen: dict[int, str] = {}
        for name, iface in list(self.interfaces.items()):
            if iface.profile < 0 or iface.profile >= len(self.profiles):
                iface.profile = self._next_free_default(name, seen)
            if iface.profile in seen and seen[iface.profile] != name:
                iface.profile = self._next_free_default(name, seen)
            seen[iface.profile] = name

    def _alloc_default_off(self) -> int:
        self.profiles.append(OffProfile.from_dict())
        return len(self.profiles) - 1

    def _next_free_default(self, name: str, seen: dict[int, str]) -> int:
        if 0 not in seen:
            return 0
        return self._alloc_default_off()

    def to_dict(self) -> dict[str, Any]:
        return {
            "interfaces": {n: i.to_dict() for n, i in self.interfaces.items()},
            "profiles": [p.to_dict() for p in self.profiles],
        }

    def save(self, path: Path) -> None:
        self.ensure_defaults()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(self.to_dict(), f, indent=2)
            f.write("\n")

    @classmethod
    def load(cls, path: Path) -> "META":
        if not path.exists():
            meta = cls()
            meta.discover_interfaces()
            meta.save(path)
            return meta
        with path.open() as f:
            data = json.load(f)
        profiles = [Profile.from_dict(p) for p in data.get("profiles") or []]
        interfaces = {
            name: WirelessInterface.from_dict(name, raw)
            for name, raw in (data.get("interfaces") or {}).items()
        }
        meta = cls(interfaces=interfaces, profiles=profiles)
        meta.discover_interfaces()
        return meta

    def discover_interfaces(self) -> None:
        net = Path("/sys/class/net")
        if not net.is_dir():
            return
        wireless: list[str] = []
        for entry in sorted(net.iterdir()):
            name = entry.name
            if name == "lo":
                continue
            if (entry / "wireless").exists() or (entry / "phy80211").exists():
                wireless.append(name)
        used = {i.profile for i in self.interfaces.values()}
        for name in wireless:
            if name in self.interfaces:
                continue
            if 0 not in used:
                idx = 0
            else:
                idx = self._alloc_default_off()
            self.interfaces[name] = WirelessInterface(name, idx)
            used.add(idx)
        self.ensure_defaults()

    def profile_owner(self, index: int) -> str | None:
        for name, iface in self.interfaces.items():
            if iface.profile == index:
                return name
        return None

    def set_interface_profile(self, iface_name: str, index: int) -> None:
        if iface_name not in self.interfaces:
            raise KeyError(iface_name)
        if index < 0 or index >= len(self.profiles):
            raise ValueError("invalid profile index")
        owner = self.profile_owner(index)
        if owner is not None and owner != iface_name:
            raise ValueError(f"profile {index} already used by {owner}")
        self.interfaces[iface_name].profile = index

    def remove_profile(self, index: int) -> None:
        if index == 0:
            raise ValueError("cannot remove default profile 0")
        if index < 0 or index >= len(self.profiles):
            raise ValueError("invalid profile index")
        owner = self.profile_owner(index)
        del self.profiles[index]
        for name, iface in self.interfaces.items():
            if iface.profile == index:
                iface.profile = -1
            elif iface.profile > index:
                iface.profile -= 1
        if owner is not None:
            used = {
                i.profile
                for n, i in self.interfaces.items()
                if n != owner and i.profile >= 0
            }
            if 0 not in used:
                self.interfaces[owner].profile = 0
            else:
                self.interfaces[owner].profile = self._alloc_default_off()
        self.ensure_defaults()


# =============================================================================
# 3. Process manager
# =============================================================================


def _pidfile(key: str) -> Path:
    return RUN_DIR / f"{key}.pid"


def _write_pid(key: str, pid: int) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    _pidfile(key).write_text(str(pid))


def stop_child(key: str) -> None:
    proc = _children.pop(key, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    pf = _pidfile(key)
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.3)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        pf.unlink(missing_ok=True)


def start_child(key: str, cmd: list[str]) -> subprocess.Popen:
    stop_child(key)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = open(RUN_DIR / f"{key}.log", "ab", buffering=0)  # noqa: SIM115
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _children[key] = proc
    _write_pid(key, proc.pid)
    return proc


def stop_all_children() -> None:
    for key in list(_children.keys()):
        stop_child(key)
    if RUN_DIR.is_dir():
        for pf in RUN_DIR.glob("*.pid"):
            stop_child(pf.stem)


def cleanup_atexit() -> None:
    clear_nat()
    stop_all_children()


atexit.register(cleanup_atexit)


# =============================================================================
# 4. Apply
# =============================================================================


def list_net_ifaces() -> list[dict[str, Any]]:
    net = Path("/sys/class/net")
    out: list[dict[str, Any]] = []
    if not net.is_dir():
        return out
    for entry in sorted(net.iterdir()):
        name = entry.name
        if name == "lo":
            continue
        wireless = (entry / "wireless").exists() or (entry / "phy80211").exists()
        oper = "unknown"
        if (entry / "operstate").is_file():
            oper = (entry / "operstate").read_text().strip()
        out.append({"name": name, "kind": "wifi" if wireless else "ethernet", "state": oper})
    return out


def ping_ms(iface: str, target: str = PING_TARGET) -> float | None:
    ping = _which("ping")
    if not ping:
        return None
    r = _run([ping, "-c", "1", "-W", "1", "-I", iface, target])
    if r.returncode != 0:
        return None
    m = re.search(r"time=([\d.]+)", r.stdout)
    if not m:
        return None
    return float(m.group(1))


def pick_uplink(exclude: str, preferred: str | None = None) -> str | None:
    if preferred:
        return preferred
    candidates = [
        i for i in list_net_ifaces()
        if i["name"] != exclude and i["kind"] != "wifi"
    ]
    if not candidates:
        candidates = [i for i in list_net_ifaces() if i["name"] != exclude]
    if not candidates:
        return None

    scored: list[tuple[float, int, str]] = []
    for c in candidates:
        name = c["name"]
        state = (c["state"] or "").lower()
        state_rank = 0 if state == "up" else (1 if state == "unknown" else 2)
        ms = ping_ms(name)
        ping_rank = ms if ms is not None else 1e9
        scored.append((ping_rank, state_rank, name))
    scored.sort()
    return scored[0][2]


def clear_nat() -> None:
    global _nat_pair
    if not _nat_pair:
        return
    ap, uplink = _nat_pair
    ipt = _which("iptables")
    if ipt:
        for args in (
            ["-t", "nat", "-D", "POSTROUTING", "-o", uplink, "-j", "MASQUERADE"],
            ["-D", "FORWARD", "-i", ap, "-o", uplink, "-j", "ACCEPT"],
            [
                "-D", "FORWARD", "-i", uplink, "-o", ap,
                "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT",
            ],
        ):
            _run([ipt, *args])
    _nat_pair = None


def setup_nat(ap: str, uplink: str) -> None:
    global _nat_pair
    clear_nat()
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
    ipt = _which("iptables")
    if not ipt:
        return
    _run([ipt, "-t", "nat", "-A", "POSTROUTING", "-o", uplink, "-j", "MASQUERADE"], check=True)
    _run([ipt, "-A", "FORWARD", "-i", ap, "-o", uplink, "-j", "ACCEPT"], check=True)
    _run(
        [
            ipt, "-A", "FORWARD", "-i", uplink, "-o", ap,
            "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT",
        ],
        check=True,
    )
    _nat_pair = (ap, uplink)


def flush_iface(iface: str) -> None:
    ip = _which("ip")
    if not ip:
        return
    _run([ip, "addr", "flush", "dev", iface])
    _run([ip, "link", "set", iface, "up"])


def set_ap_address(iface: str, host_addr: str | None = None) -> None:
    host_addr = host_addr or get_host_addr()
    ip = _which("ip")
    if not ip:
        raise RuntimeError("ip command not found")
    flush_iface(iface)
    _run([ip, "addr", "add", f"{host_addr}/24", "dev", iface], check=True)


def write_hostapd_conf(iface: str, profile: WAPProfile) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / f"hostapd-{iface}.conf"
    psk = wpa_psk_hex(profile.ssid, profile.password)
    path.write_text(
        f"""\
ctrl_interface={RUN_DIR}/hostapd
ctrl_interface_group=0
interface={iface}
driver=nl80211
ssid={profile.ssid}
country_code={profile.country_code}
hw_mode=a
channel={profile.channel}
ieee80211d=1
ieee80211n=1
ieee80211ac=1
ieee80211ax=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_psk={psk}
"""
    )
    path.chmod(0o600)
    return path


def write_dnsmasq_conf(iface: str, host_addr: str | None = None) -> Path:
    host_addr = host_addr or get_host_addr()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / f"dnsmasq-{iface}.conf"
    prefix = ".".join(host_addr.split(".")[:3])
    # port=0: DHCP only — NixOS services.dnsmasq owns DNS on host_addr:53
    path.write_text(
        f"""\
interface={iface}
bind-interfaces
port=0
dhcp-range={prefix}.50,{prefix}.200,255.255.255.0,12h
dhcp-option=option:router,{host_addr}
dhcp-option=option:dns-server,{host_addr}
"""
    )
    return path


def write_wpa_conf(iface: str, profile: WiFiProfile) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / f"wpa-{iface}.conf"
    scan = 1 if profile.scan_ssid else 0
    km = (profile.key_mgmt or "WPA-PSK").upper()
    if km == "NONE":
        network_lines = [
            f'    ssid="{profile.ssid}"',
            "    key_mgmt=NONE",
            f"    scan_ssid={scan}",
            "    priority=10",
        ]
    else:
        network_lines = [
            f'    ssid="{profile.ssid}"',
            f'    psk="{profile.password}"',
            f"    key_mgmt={profile.key_mgmt or 'WPA-PSK'}",
            f"    scan_ssid={scan}",
            "    priority=10",
        ]
    body = "\n".join(
        [
            f"ctrl_interface={RUN_DIR}/wpa_supplicant",
            "ctrl_interface_group=wheel",
            "update_config=0",
            f"country={DEFAULT_COUNTRY}",
            "ap_scan=1",
            "",
            "network={",
            *network_lines,
            "}",
            "",
        ]
    )
    path.write_text(body)
    path.chmod(0o600)
    return path


def write_dhcpcd_conf(iface: str) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / f"dhcpcd-{iface}.conf"
    path.write_text(
        f"""\
hostname
clientid
persistent
option rapid_commit
option domain_name_servers, domain_name, domain_search, host_name
option classless_static_routes
option interface_mtu
require dhcp_server_identifier
slaac private
allowinterfaces {iface}
denyinterfaces !{iface}
"""
    )
    return path


def apply_wifi(iface: str, profile: WiFiProfile) -> None:
    stop_child(f"hostapd-{iface}")
    stop_child(f"dnsmasq-{iface}")
    if _nat_pair and _nat_pair[0] == iface:
        clear_nat()
    flush_iface(iface)
    conf = write_wpa_conf(iface, profile)
    wpa = _which("wpa_supplicant")
    if not wpa:
        raise RuntimeError("wpa_supplicant not found")
    start_child(f"wpa-{iface}", [wpa, "-i", iface, "-c", str(conf)])
    time.sleep(2)
    dhcpcd = _which("dhcpcd")
    if dhcpcd:
        dconf = write_dhcpcd_conf(iface)
        # -f: our conf; -B: stay foreground (child); -1: exit after getting a lease once is NOT used
        # so the child keeps renewing. --nohook avoids system hooks that need /etc.
        start_child(
            f"dhcpcd-{iface}",
            [dhcpcd, "-f", str(dconf), "-B", "-q", "--nohook", "lookup-hostname", iface],
        )
    else:
        dhclient = _which("dhclient")
        if dhclient:
            _run([dhclient, "-v", iface])


def apply_wap(iface: str, profile: WAPProfile) -> None:
    stop_child(f"wpa-{iface}")
    stop_child(f"dhcpcd-{iface}")
    hostapd = _which("hostapd")
    dnsmasq = _which("dnsmasq")
    if not hostapd:
        raise RuntimeError("hostapd not found")
    if not dnsmasq:
        raise RuntimeError("dnsmasq not found")

    uplink = pick_uplink(exclude=iface, preferred=profile.uplink)
    hap_conf = write_hostapd_conf(iface, profile)
    dns_conf = write_dnsmasq_conf(iface)
    set_ap_address(iface)

    start_child(f"hostapd-{iface}", [hostapd, str(hap_conf)])
    time.sleep(0.5)
    start_child(
        f"dnsmasq-{iface}",
        [dnsmasq, "--keep-in-foreground", f"--conf-file={dns_conf}"],
    )
    if uplink:
        setup_nat(iface, uplink)


def apply_off(iface: str) -> None:
    """Stop radio daemons and bring the interface down."""
    stop_child(f"hostapd-{iface}")
    stop_child(f"dnsmasq-{iface}")
    stop_child(f"wpa-{iface}")
    stop_child(f"dhcpcd-{iface}")
    if _nat_pair and _nat_pair[0] == iface:
        clear_nat()
    flush_iface(iface)
    ip = _which("ip")
    if ip:
        _run([ip, "link", "set", "dev", iface, "down"])



def apply_meta(meta: META) -> list[str]:
    global _runtime
    messages: list[str] = []
    meta.ensure_defaults()
    log_event("Apply started")
    for name, iface in meta.interfaces.items():
        prof = meta.profiles[iface.profile]
        try:
            if isinstance(prof, OffProfile):
                apply_off(name)
                msg = f"{name}: off"
                messages.append(msg)
                _runtime[name] = {
                    "profile": iface.profile,
                    "mode": "off",
                    "ssid": "",
                    "uplink": None,
                    "ok": True,
                    "detail": msg,
                }
                log_event(msg)
            elif isinstance(prof, WiFiProfile):
                apply_wifi(name, prof)
                msg = f"{name}: wifi → {prof.ssid}"
                messages.append(msg)
                _runtime[name] = {
                    "profile": iface.profile,
                    "mode": "wifi",
                    "ssid": prof.ssid,
                    "uplink": None,
                    "ok": True,
                    "detail": msg,
                }
                log_event(msg)
            elif isinstance(prof, WAPProfile):
                apply_wap(name, prof)
                uplink = pick_uplink(name, prof.uplink)
                msg = f"{name}: wap → {prof.ssid} (uplink={uplink})"
                messages.append(msg)
                _runtime[name] = {
                    "profile": iface.profile,
                    "mode": "wap",
                    "ssid": prof.ssid,
                    "uplink": uplink,
                    "ok": True,
                    "detail": msg,
                }
                log_event(msg)
            else:
                msg = f"{name}: unknown profile type"
                messages.append(msg)
                log_event(msg)
        except Exception as e:  # noqa: BLE001
            msg = f"{name}: FAIL {e}"
            messages.append(msg)
            _runtime[name] = {
                "profile": iface.profile,
                "mode": getattr(prof, "mode", "?"),
                "ssid": getattr(prof, "ssid", ""),
                "uplink": None,
                "ok": False,
                "detail": str(e),
            }
            log_event(msg)
    log_event("Apply finished")
    return messages


# =============================================================================
# 5. Scan
# =============================================================================


def _dedupe_networks(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for n in networks:
        ssid = (n.get("ssid") or "").strip()
        if not ssid:
            continue
        prev = best.get(ssid)
        if prev is None or (n.get("signal") or -999) > (prev.get("signal") or -999):
            best[ssid] = n
    return sorted(best.values(), key=lambda x: x.get("signal") or -999, reverse=True)


def _flags_indicate_open(flags: str) -> bool:
    """True if flags look like an open (no PSK/SAE/enterprise/WEP) network."""
    f = (flags or "").upper()
    secured = ("WPA", "RSN", "SAE", "WEP", "802.1X", "EAP", "OWE")
    return not any(tok in f for tok in secured)


def _parse_iw_scan(stdout: str) -> list[dict[str, Any]]:
    networks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.rstrip()
        if line.startswith("BSS "):
            if cur and cur.get("ssid"):
                networks.append(cur)
            cur = {
                "bssid": line.split()[1].split("(")[0],
                "ssid": "",
                "signal": None,
                "flags": "",
                "open": True,
            }
        elif cur is None:
            continue
        elif "signal:" in line:
            m = re.search(r"signal:\s*([-\d.]+)", line)
            if m:
                cur["signal"] = float(m.group(1))
        elif "capability:" in line:
            # Privacy bit ⇒ not open; also stash for flags display
            caps = line.split("capability:", 1)[1].strip()
            cur["flags"] = (cur.get("flags") or "") + f"[{caps}]"
            if re.search(r"\bPrivacy\b", caps, re.I):
                cur["open"] = False
        elif line.lstrip().startswith(("RSN:", "WPA:", "RSN:", "WPA2:")):
            cur["open"] = False
            tag = line.lstrip().split(":", 1)[0]
            cur["flags"] = (cur.get("flags") or "") + f"[{tag}]"
        elif "* Authentication suites:" in line or "* Pairwise ciphers:" in line:
            cur["open"] = False
        elif line.lstrip().startswith("SSID:"):
            ssid = line.split("SSID:", 1)[1].strip()
            cur["ssid"] = ssid
    if cur and cur.get("ssid"):
        networks.append(cur)
    return _dedupe_networks(networks)


def _parse_wpa_scan_results(stdout: str) -> list[dict[str, Any]]:
    # bssid / frequency / signal level / flags / ssid
    networks: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line or line.startswith("bssid"):
            continue
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        try:
            signal = float(parts[2])
        except ValueError:
            signal = None
        flags = parts[3]
        networks.append(
            {
                "bssid": parts[0],
                "signal": signal,
                "ssid": parts[4],
                "flags": flags,
                "open": _flags_indicate_open(flags),
            }
        )
    return _dedupe_networks(networks)


def _enrich_scan_networks(networks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach open/unsafe + saved profile matches from current META."""
    for n in networks:
        if "open" not in n:
            n["open"] = _flags_indicate_open(str(n.get("flags") or ""))
        n["unsafe"] = bool(n["open"])
        n["saved_profiles"] = []
        n["password_on_open"] = False
        ssid = n.get("ssid") or ""
        if _meta is None or not ssid:
            continue
        for i, p in enumerate(_meta.profiles):
            if not isinstance(p, WiFiProfile):
                continue
            if p.ssid != ssid:
                continue
            n["saved_profiles"].append(i)
            if n["open"] and (p.password or "").strip():
                n["password_on_open"] = True
                n["unsafe"] = True
    return networks


def _wpa_ctrl_dir() -> Path:
    return RUN_DIR / "wpa_supplicant"


def _scan_via_wpa_cli(iface: str) -> list[dict[str, Any]] | None:
    """Return networks via wpa_cli, or None if ctrl socket unavailable."""
    wpa_cli = _which("wpa_cli")
    ctrl = _wpa_ctrl_dir()
    if not wpa_cli or not ctrl.is_dir():
        return None
    # socket appears as ctrl/iface
    if not (ctrl / iface).exists() and not any(ctrl.iterdir()):
        return None
    r = _run([wpa_cli, "-p", str(ctrl), "-i", iface, "scan"])
    if r.returncode != 0 and "FAIL" in (r.stdout + r.stderr):
        return None
    time.sleep(3)
    r = _run([wpa_cli, "-p", str(ctrl), "-i", iface, "scan_results"])
    if r.returncode != 0:
        return None
    return _parse_wpa_scan_results(r.stdout)


def _scan_via_iw(iface: str) -> tuple[list[dict[str, Any]], str | None]:
    iw = _which("iw")
    if not iw:
        return [], "iw not found"
    ip = _which("ip")
    if ip:
        _run([ip, "link", "set", "dev", iface, "up"])
    # Blocking scan (needs exclusive radio — not while hostapd holds it)
    r = _run([iw, "dev", iface, "scan"])
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "iw scan failed").strip()
        # fallback to dump of last scan
        dump = _run([iw, "dev", iface, "scan", "dump"])
        nets = _parse_iw_scan(dump.stdout) if dump.returncode == 0 else []
        return nets, err if not nets else None
    return _parse_iw_scan(r.stdout), None


def scan_wifi(iface: str) -> dict[str, Any]:
    """Scan for APs. Uses wpa_cli when wpa_supplicant owns the iface;
    otherwise briefly stops hostapd/dnsmasq so iw can scan."""
    # Prefer wpa_cli — works while associated / scanning as client
    via_wpa = _scan_via_wpa_cli(iface)
    if via_wpa is not None:
        return {
            "networks": _enrich_scan_networks(via_wpa),
            "method": "wpa_cli",
            "error": None,
        }

    # Free radio from AP mode if needed
    had_hostapd = (RUN_DIR / f"hostapd-{iface}.pid").exists() or f"hostapd-{iface}" in _children
    had_dnsmasq = (RUN_DIR / f"dnsmasq-{iface}.pid").exists() or f"dnsmasq-{iface}" in _children
    if had_hostapd:
        stop_child(f"hostapd-{iface}")
    if had_dnsmasq:
        stop_child(f"dnsmasq-{iface}")
    if had_hostapd or had_dnsmasq:
        time.sleep(0.5)

    nets, err = _scan_via_iw(iface)

    # Restore AP if we tore it down
    if (had_hostapd or had_dnsmasq) and _meta is not None:
        iface_state = _meta.interfaces.get(iface)
        if iface_state is not None:
            prof = _meta.profiles[iface_state.profile]
            if isinstance(prof, WAPProfile):
                try:
                    apply_wap(iface, prof)
                except Exception as e:  # noqa: BLE001
                    err = (err + "; " if err else "") + f"restore AP failed: {e}"

    return {
        "networks": _enrich_scan_networks(nets),
        "method": "iw",
        "error": err,
    }


# =============================================================================
# 6. HTTP API
# =============================================================================


def render_index() -> str:
    """Single-page HTML with inlined CSS/JS and BASE_PATH for reverse proxies."""
    js = PAGE_JS.replace("__BASE_PATH__", BASE_PATH)
    # Avoid </script> in JS strings breaking out of the inline script tag
    js = js.replace("</script>", "<\\/script>")
    html = PAGE_HTML.replace("__PAGE_CSS__", PAGE_CSS)
    html = html.replace("__PAGE_JS__", js)
    return html




class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str) -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, html: str) -> None:
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw)

    def do_GET(self) -> None:
        global _meta
        assert _meta is not None
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)

        if path == "/" or path == "":
            self._html(200, render_index())
            return
        if path == "/app.js":
            body = PAGE_JS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/style.css":
            body = PAGE_CSS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/css")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._json(200, _meta.to_dict())
            return
        if path == "/api/interfaces":
            self._json(200, {"interfaces": list_net_ifaces()})
            return
        if path == "/api/scan":
            iface = (qs.get("iface") or [None])[0]
            if not iface:
                self._text(400, "iface required")
                return
            log_event(f"Scan requested on {iface}")
            self._json(200, scan_wifi(iface))
            return
        if path == "/api/status":
            self._json(200, status_payload())
            return
        self._text(404, "not found")

    def do_PUT(self) -> None:
        global _meta
        assert _meta is not None
        path = urlparse(self.path).path

        if path == "/api/state":
            try:
                data = self._read_json()
            except json.JSONDecodeError:
                self._text(400, "invalid json")
                return
            profiles = [Profile.from_dict(p) for p in data.get("profiles") or []]
            interfaces = {
                name: WirelessInterface.from_dict(name, raw)
                for name, raw in (data.get("interfaces") or {}).items()
            }
            owners: dict[int, str] = {}
            for name, iface in interfaces.items():
                if iface.profile in owners:
                    self._json(
                        409,
                        {
                            "error": (
                                f"profile {iface.profile} used by "
                                f"{owners[iface.profile]} and {name}"
                            )
                        },
                    )
                    return
                owners[iface.profile] = name
            _meta = META(interfaces=interfaces, profiles=profiles)
            _meta.save(_config_path)
            self._json(200, _meta.to_dict())
            return

        m = re.fullmatch(r"/api/interfaces/([^/]+)/profile", path)
        if m:
            name = m.group(1)
            try:
                data = self._read_json()
                idx = int(data["profile"])
                _meta.set_interface_profile(name, idx)
                _meta.save(_config_path)
                self._json(200, _meta.to_dict())
            except KeyError:
                self._text(404, "unknown interface")
            except ValueError as e:
                self._json(409, {"error": str(e)})
            except (TypeError, json.JSONDecodeError) as e:
                self._text(400, str(e))
            return

        self._text(404, "not found")

    def do_POST(self) -> None:
        global _meta
        assert _meta is not None
        path = urlparse(self.path).path

        if path == "/api/apply":
            msgs = apply_meta(_meta)
            self._json(200, {"ok": True, "messages": msgs})
            return

        if path == "/api/profiles":
            try:
                data = self._read_json()
            except json.JSONDecodeError:
                self._text(400, "invalid json")
                return
            idx = data.get("index")
            prof = Profile.from_dict(data.get("profile") or data)
            if idx is None:
                _meta.profiles.append(prof)
                idx = len(_meta.profiles) - 1
            else:
                idx = int(idx)
                if idx == 0 and not isinstance(prof, OffProfile):
                    self._text(400, "profiles[0] must remain off")
                    return
                if idx < 0 or idx >= len(_meta.profiles):
                    self._text(400, "bad index")
                    return
                _meta.profiles[idx] = prof
            _meta.ensure_defaults()
            _meta.save(_config_path)
            self._json(200, {"index": idx, "state": _meta.to_dict()})
            return

        self._text(404, "not found")

    def do_DELETE(self) -> None:
        global _meta
        assert _meta is not None
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/profiles/(\d+)", path)
        if not m:
            self._text(404, "not found")
            return
        idx = int(m.group(1))
        try:
            _meta.remove_profile(idx)
            _meta.save(_config_path)
            self._json(200, _meta.to_dict())
        except ValueError as e:
            self._json(400, {"error": str(e)})


# =============================================================================
# 7. Embedded UI (HTML / CSS / JS) — presentation only; config via API
# =============================================================================

PAGE_CSS = """
/* Mirror Glance theme tokens so we can sync from the parent page */
:root {
  --scheme: ;
  --bgh: 240;
  --bgs: 8%;
  --bgl: 9%;
  --bghs: var(--bgh), var(--bgs);
  --cm: 1;
  --tsm: 1;

  --color-primary: hsl(43, 50%, 70%);
  --color-positive: var(--color-primary);
  --color-negative: hsl(0, 70%, 70%);
  --color-background: hsl(var(--bghs), var(--bgl));
  --color-widget-background: hsl(var(--bghs), calc(var(--bgl) + 1%));
  --color-widget-background-highlight: hsl(var(--bghs), calc(var(--scheme) (var(--scheme) var(--bgl) + 4%)));
  --color-separator: hsl(var(--bghs), calc(var(--scheme) ((var(--scheme) var(--bgl)) + 4% * var(--cm))));
  --color-widget-content-border: hsl(var(--bghs), calc(var(--scheme) (var(--scheme) var(--bgl) + 4%)));

  --ths: var(--bgh), calc(var(--bgs) * var(--tsm));
  --color-text-highlight: hsl(var(--ths), calc(var(--scheme) var(--cm) * 85%));
  --color-text-paragraph: hsl(var(--ths), calc(var(--scheme) var(--cm) * 73%));
  --color-text-base: hsl(var(--ths), calc(var(--scheme) var(--cm) * 58%));
  --color-text-base-muted: hsl(var(--ths), calc(var(--scheme) var(--cm) * 52%));
  --color-text-subdue: hsl(var(--ths), calc(var(--scheme) var(--cm) * 35%));

  --border-radius: 5px;
  --font: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
}
:root[data-scheme=light] {
  --scheme: 100% -;
}

* { box-sizing: border-box; }
html, body {
  margin: 0; min-height: 100%;
  background: var(--color-background);
  color: var(--color-text-paragraph);
  font: 14px/1.45 var(--font);
  -webkit-font-smoothing: antialiased;
}
body { padding: 14px; }
.page { max-width: 780px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }

.widget {
  background: var(--color-widget-background);
  border: 1px solid var(--color-widget-content-border);
  border-radius: var(--border-radius);
  overflow: hidden;
}
.widget-header {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-separator);
}
.widget-header h1, .widget-header h2 {
  margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.02em;
  color: var(--color-text-subdue); text-transform: uppercase;
}
.widget-header .title-accent { color: var(--color-primary); }
.widget-body { padding: 12px 14px; }
.muted { color: var(--color-text-subdue); font-size: 12px; margin: 0; }

.grid {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 10px 14px;
  align-items: center;
}
.grid .label { color: var(--color-text-base-muted); font-size: 12px; font-weight: 500; }
.grid .span { grid-column: 1 / -1; }
.grid .actions { display: flex; flex-wrap: wrap; gap: 8px; }

select, input[type=text], input[type=password], input[type=number] {
  width: 100%; padding: 8px 10px;
  border: 1px solid var(--color-widget-content-border); border-radius: var(--border-radius);
  background: var(--color-background); color: var(--color-text-highlight);
  font: inherit; outline: none;
}
select:focus, input:focus {
  outline: 2px solid var(--color-primary);
  outline-offset: 0.1rem;
}
input[readonly] { opacity: 0.75; color: var(--color-text-base); }
label.inline {
  display: inline-flex; align-items: center; gap: 8px;
  color: var(--color-text-subdue); font-size: 12px; cursor: pointer; user-select: none;
}
input[type=checkbox] { accent-color: var(--color-primary); width: auto; }

button {
  padding: 7px 12px; border-radius: var(--border-radius); cursor: pointer;
  border: 1px solid var(--color-widget-content-border);
  background: var(--color-widget-background-highlight); color: var(--color-text-paragraph);
  font: 12px/1.2 var(--font); font-weight: 500;
}
button:hover { border-color: var(--color-text-subdue); }
button.primary {
  border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-widget-content-border));
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 14%, var(--color-widget-background));
}
button.primary:hover {
  background: color-mix(in srgb, var(--color-primary) 22%, var(--color-widget-background));
}
button.danger {
  border-color: color-mix(in srgb, var(--color-negative) 50%, var(--color-widget-content-border));
  color: var(--color-negative);
}
button:disabled { opacity: 0.45; cursor: default; }

#status {
  margin: 0; cursor: pointer; font-size: 13px;
  padding: 10px 14px; border-radius: var(--border-radius);
  border: 1px solid var(--color-widget-content-border);
  background: var(--color-widget-background);
  color: var(--color-text-paragraph);
}
#status:hover { border-color: var(--color-text-subdue); }
#status::after {
  content: " · logs";
  color: var(--color-text-subdue); font-size: 11px;
}
#flash {
  margin: 0; min-height: 1.1em; font-size: 12px; color: var(--color-text-subdue);
  padding: 0 2px;
}

.modal-backdrop {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: color-mix(in srgb, var(--color-background) 55%, transparent);
  backdrop-filter: blur(4px);
  align-items: center; justify-content: center; padding: 16px;
}
.modal-backdrop.open { display: flex; }
.modal {
  background: var(--color-widget-background); color: var(--color-text-paragraph);
  border: 1px solid var(--color-widget-content-border); border-radius: var(--border-radius);
  max-width: 640px; width: 100%; max-height: 80vh; overflow: auto;
  box-shadow: 0 16px 48px rgba(0,0,0,0.45);
}
.modal header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 14px; border-bottom: 1px solid var(--color-separator);
}
.modal header h2 {
  margin: 0; font-size: 13px; font-weight: 600; letter-spacing: 0.02em;
  text-transform: uppercase; color: var(--color-primary);
}
.modal .body { padding: 4px 0; }
.modal footer {
  padding: 10px 14px; border-top: 1px solid var(--color-separator);
  color: var(--color-text-subdue); font-size: 12px;
}
#scan-hint { padding: 8px 14px 4px; font-size: 12px; color: var(--color-text-subdue); margin: 0; }

table.nets { width: 100%; border-collapse: collapse; font-size: 13px; }
table.nets th, table.nets td {
  padding: 9px 14px; text-align: left; vertical-align: top;
  border-bottom: 1px solid var(--color-separator);
}
table.nets th {
  color: var(--color-text-subdue); font-size: 11px; text-transform: uppercase; font-weight: 600;
}
table.nets tr.pick { cursor: pointer; }
table.nets tr.pick:hover { background: var(--color-widget-background-highlight); }
table.nets tr.saved {
  background: color-mix(in srgb, var(--color-positive) 6%, transparent);
}
.tag {
  display: inline-block; font-size: 11px; padding: 2px 7px; margin: 0 4px 2px 0;
  border-radius: 999px; border: 1px solid var(--color-separator); color: var(--color-text-subdue);
}
.tag.unsafe {
  border-color: color-mix(in srgb, var(--color-negative) 50%, var(--color-separator));
  color: var(--color-negative);
  background: color-mix(in srgb, var(--color-negative) 10%, transparent);
}
.tag.saved {
  border-color: color-mix(in srgb, var(--color-positive) 45%, var(--color-separator));
  color: var(--color-positive);
  background: color-mix(in srgb, var(--color-positive) 10%, transparent);
}
.tag.warn {
  border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-separator));
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
}

#log-body {
  font: 12px/1.45 var(--mono);
  white-space: pre-wrap; padding: 12px 14px; max-height: 50vh; overflow: auto;
  background: var(--color-background); color: var(--color-text-paragraph); margin: 0;
}
#log-body .t { color: var(--color-primary); margin-right: 8px; }

@media (max-width: 560px) {
  .grid { grid-template-columns: 1fr; gap: 4px 0; }
  .grid .label { margin-top: 6px; }
}
"""

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Wireless</title>
  <style>__PAGE_CSS__</style>
</head>
<body>
  <div class="page">
    <section class="widget">
      <div class="widget-header">
        <h1><span class="title-accent">Wireless</span> · interface &amp; profile</h1>
      </div>
      <div class="widget-body">
        <div class="grid">
          <div class="label">Interface</div>
          <div><select id="iface"></select></div>

          <div class="label">Profile</div>
          <div><select id="profile"></select></div>

          <div class="label">Actions</div>
          <div class="actions">
            <button type="button" id="btn-scan">Scan for WiFi</button>
            <button type="button" id="btn-wap">New WAP profile</button>
            <button type="button" class="primary" id="btn-apply">Apply</button>
          </div>
        </div>
      </div>
    </section>

    <section class="widget" id="edit-panel">
      <div class="widget-header">
        <h2 id="edit-title">Edit profile</h2>
      </div>
      <div class="widget-body">
        <div class="grid">
          <div class="label">Mode</div>
          <div><input id="edit-mode" readonly></div>

          <div class="label cred-row">SSID</div>
          <div class="cred-row"><input id="edit-ssid" type="text"></div>

          <div class="label cred-row">Password</div>
          <div class="cred-row">
            <input id="edit-password" type="password">
            <label class="inline" style="margin-top:8px;">
              <input type="checkbox" id="edit-password-show"> Show
            </label>
          </div>

          <div class="span muted" id="off-note" style="display:none;">
            Off profile: interface is powered down (no WiFi / no AP).
          </div>

          <div class="label wap-row">Uplink</div>
          <div class="wap-row"><select id="edit-uplink"></select></div>

          <div class="label wap-row">Channel</div>
          <div class="wap-row"><input id="edit-channel" type="number"></div>

          <div class="label wap-row">Country</div>
          <div class="wap-row"><input id="edit-country" type="text"></div>

          <div class="label wifi-row">Hidden SSID</div>
          <div class="wifi-row">
            <label class="inline"><input type="checkbox" id="edit-scan-ssid"> scan_ssid</label>
          </div>

          <div class="label wifi-row">key_mgmt</div>
          <div class="wifi-row"><input id="edit-key-mgmt" type="text" value="WPA-PSK"></div>

          <div class="span actions">
            <button type="button" class="primary" id="btn-save">Save profile</button>
            <button type="button" class="danger" id="btn-remove" hidden>Remove profile</button>
          </div>
        </div>
      </div>
    </section>

    <p id="status" title="Click to open logs">No interfaces applied yet.</p>
    <p id="flash"></p>
  </div>

  <div id="log-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="log-modal-title">
    <div class="modal">
      <header>
        <h2 id="log-modal-title">Console</h2>
        <button type="button" id="btn-log-close">Close</button>
      </header>
      <pre id="log-body"></pre>
      <footer>Newest at bottom. Click status anytime to refresh.</footer>
    </div>
  </div>

  <div id="scan-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="scan-modal-title">
    <div class="modal">
      <header>
        <h2 id="scan-modal-title">Available networks</h2>
        <button type="button" id="btn-scan-close">Close</button>
      </header>
      <p id="scan-hint"></p>
      <div class="body">
        <table class="nets">
          <thead><tr><th>SSID</th><th>Signal</th><th>Status</th></tr></thead>
          <tbody id="scan-body"></tbody>
        </table>
      </div>
      <footer>Saved → switch profile. New → create. Open networks marked unsafe.</footer>
    </div>
  </div>

  <script>__PAGE_JS__</script>
</body>
</html>
"""

PAGE_JS = r"""
let state = { interfaces: {}, profiles: [] };
let netIfaces = [];
let selectedIface = "";
let editingIndex = null;
let activeProfiles = {}; // iface -> profile index currently applied
const BASE = "__BASE_PATH__"; // filled by server (e.g. /wireless)

const $ = (id) => document.getElementById(id);

/** Copy Glance theme CSS variables from parent (same-origin iframe). */
function syncGlanceTheme() {
  try {
    if (!window.parent || window.parent === window) return false;
    const parentRoot = window.parent.document.documentElement;
    const src = getComputedStyle(parentRoot);
    const dst = document.documentElement;
    const keys = [
      "--bgh", "--bgs", "--bgl", "--cm", "--tsm",
      "--color-primary", "--color-positive", "--color-negative",
    ];
    for (const k of keys) {
      const v = src.getPropertyValue(k).trim();
      if (v) dst.style.setProperty(k, v);
    }
    const parentScheme = parentRoot.getAttribute("data-scheme");
    const isLight =
      parentScheme === "light" ||
      parentRoot.classList.contains("light-scheme") ||
      src.getPropertyValue("--scheme").includes("100%");
    if (isLight) {
      dst.setAttribute("data-scheme", "light");
    } else {
      dst.removeAttribute("data-scheme");
    }
    return true;
  } catch (e) {
    // cross-origin — keep defaults
    return false;
  }
}

function watchGlanceTheme() {
  syncGlanceTheme();
  try {
    if (!window.parent || window.parent === window) return;
    const parentRoot = window.parent.document.documentElement;
    const obs = new MutationObserver(() => syncGlanceTheme());
    obs.observe(parentRoot, { attributes: true, attributeFilter: ["style", "class", "data-scheme"] });
    // theme picker also mutates :root style frequently
    setInterval(syncGlanceTheme, 1000);
  } catch (_) { /* ignore */ }
}
function setFlash(msg) { $("flash").textContent = msg || ""; }
function setStatus(msg) {
  // keep connection summary on #status; route transient messages to flash
  setFlash(msg);
}

async function refreshConnectionStatus() {
  try {
    const data = await api("/api/status");
    activeProfiles = data.active_profiles || {};
    $("status").textContent = data.summary || "No status.";
    fillProfileSelect();
    return data;
  } catch (e) {
    $("status").textContent = "Status unavailable: " + e.message;
    return null;
  }
}

function openLogModal(logs) {
  const body = $("log-body");
  const lines = (logs || []).map(e =>
    `<span class="t">${e.time || ""}</span>${(e.message || "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}`
  );
  body.innerHTML = lines.length ? lines.join("\n") : "<span class=\"t\">—</span> no logs yet";
  $("log-modal").classList.add("open");
  body.scrollTop = body.scrollHeight;
}
function closeLogModal() { $("log-modal").classList.remove("open"); }

async function api(path, opts) {
  const res = await fetch(BASE + path, opts);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!res.ok) {
    const err = (data && data.error) || text || res.statusText;
    throw new Error(err);
  }
  return data;
}

function signalLabel(dbm) {
  if (dbm == null || dbm === "") return "—";
  return `${dbm} dBm`;
}

function fillIfaceSelect() {
  const sel = $("iface");
  const names = Object.keys(state.interfaces || {});
  sel.innerHTML = names.map(n => `<option value="${n}">${n}</option>`).join("");
  if (selectedIface && names.includes(selectedIface)) sel.value = selectedIface;
  else selectedIface = sel.value || "";
}

function fillProfileSelect() {
  const sel = $("profile");
  const bound = state.interfaces[selectedIface]?.profile ?? 0;
  const activeIdx = activeProfiles[selectedIface];
  sel.innerHTML = (state.profiles || []).map((p, i) => {
    const owner = Object.entries(state.interfaces).find(([, v]) => v.profile === i)?.[0];
    let tag = "";
    if (owner && owner !== selectedIface) tag += ` [in use: ${owner}]`;
    if (activeIdx != null && i === activeIdx) tag += " [active]";
    const label = p.mode === "off" ? `${i}: off` : `${i}: ${p.mode} — ${p.ssid}`;
    return `<option value="${i}" ${i === bound ? "selected" : ""}>${label}${tag}</option>`;
  }).join("");
}

function fillUplinkSelect(current) {
  const sel = $("edit-uplink");
  const eth = netIfaces.filter(i => i.kind !== "wifi" && i.name !== selectedIface);
  sel.innerHTML = `<option value="">Auto</option>` + eth.map(i =>
    `<option value="${i.name}">${i.name} (${i.state})</option>`
  ).join("");
  sel.value = current || "";
}

function showEditor(index, draft) {
  editingIndex = index;
  const p = draft || state.profiles[index];
  $("edit-panel").style.display = "";
  $("edit-title").textContent = index == null ? "New profile" : `Edit profile ${index}`;
  $("edit-mode").value = p.mode;
  $("edit-ssid").value = p.ssid || "";
  $("edit-password").value = p.password || "";
  if ($("edit-password-show")) {
    $("edit-password-show").checked = false;
    $("edit-password").type = "password";
  }
  const isOff = p.mode === "off";
  const isWap = p.mode === "wap";
  const isWifi = p.mode === "wifi";
  document.querySelectorAll(".cred-row").forEach(r => { r.style.display = isOff ? "none" : ""; });
  document.querySelectorAll(".wap-row").forEach(r => { r.style.display = isWap ? "" : "none"; });
  document.querySelectorAll(".wifi-row").forEach(r => { r.style.display = isWifi ? "" : "none"; });
  if (isWap) {
    fillUplinkSelect(p.uplink);
    $("edit-channel").value = p.channel ?? 149;
    $("edit-country").value = p.country_code || "IN";
  } else if (isWifi) {
    $("edit-scan-ssid").checked = !!p.scan_ssid;
    $("edit-key-mgmt").value = p.key_mgmt || "WPA-PSK";
  }
  const rem = $("btn-remove");
  rem.hidden = !(index != null && index !== 0);
  $("btn-save").hidden = isOff && index === 0; // profile 0 off is fixed
  const note = $("off-note");
  if (note) note.style.display = isOff ? "" : "none";
}

function openScanModal() { $("scan-modal").classList.add("open"); }
function closeScanModal() { $("scan-modal").classList.remove("open"); }

function pickNetwork(draft) {
  closeScanModal();
  showEditor(null, draft);
}

async function switchToSavedProfile(idx) {
  closeScanModal();
  try {
    state = await api(`/api/interfaces/${encodeURIComponent(selectedIface)}/profile`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: idx }),
    });
    fillProfileSelect();
    showEditor(idx);
    setStatus(`Switched ${selectedIface} → saved profile ${idx}`);
  } catch (e) {
    // profile in use by another iface — still open editor on that profile
    fillProfileSelect();
    showEditor(idx);
    setStatus("Saved profile " + idx + " (bind failed: " + e.message + ")");
  }
}

function statusTags(n) {
  const parts = [];
  if (n.open || n.unsafe) parts.push('<span class="tag unsafe">Unsafe · open</span>');
  if (n.password_on_open) {
    parts.push('<span class="tag warn">Saved with password on open network</span>');
  }
  const saved = n.saved_profiles || [];
  if (saved.length) {
    parts.push(`<span class="tag saved">Saved #${saved.join(", #")}</span>`);
  }
  return parts.join(" ") || '<span class="muted">—</span>';
}

async function reload() {
  setFlash("Loading…");
  const [st, ifs] = await Promise.all([
    api("/api/state"),
    api("/api/interfaces"),
  ]);
  state = st;
  netIfaces = ifs.interfaces || [];
  fillIfaceSelect();
  await refreshConnectionStatus();
  setFlash("Ready.");
}

$("iface").addEventListener("change", () => {
  selectedIface = $("iface").value;
  fillProfileSelect();
  const idx = state.interfaces[selectedIface]?.profile;
  if (idx != null) showEditor(idx);
});

$("profile").addEventListener("change", async () => {
  const idx = parseInt($("profile").value, 10);
  try {
    state = await api(`/api/interfaces/${encodeURIComponent(selectedIface)}/profile`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: idx }),
    });
    fillProfileSelect();
    showEditor(idx);
    setStatus(`Bound ${selectedIface} → profile ${idx}`);
  } catch (e) {
    setStatus("Error: " + e.message);
    fillProfileSelect();
  }
});

$("btn-scan").addEventListener("click", async () => {
  if (!selectedIface) return;
  setStatus("Scanning…");
  openScanModal();
  $("scan-hint").textContent = "Scanning " + selectedIface + "…";
  $("scan-body").innerHTML = "";
  try {
    const data = await api(`/api/scan?iface=${encodeURIComponent(selectedIface)}`);
    const body = $("scan-body");
    const nets = data.networks || [];
    let hint = `Found ${nets.length} network(s)`;
    if (data.method) hint += ` via ${data.method}`;
    if (data.error) hint += ` — ${data.error}`;
    $("scan-hint").textContent = hint;

    for (const n of nets) {
      const tr = document.createElement("tr");
      const saved = n.saved_profiles || [];
      tr.className = "pick" + (saved.length ? " saved" : "");
      tr.innerHTML = `<td>${n.ssid || "(hidden)"}</td><td>${signalLabel(n.signal)}</td><td>${statusTags(n)}</td>`;
      tr.addEventListener("click", () => {
        if (saved.length) {
          switchToSavedProfile(saved[0]);
          if (n.password_on_open) {
            setStatus(`Profile #${saved[0]}: network is OPEN but profile has a password — unsafe`);
          }
          return;
        }
        if (n.open) {
          pickNetwork({
            mode: "wifi",
            ssid: n.ssid,
            password: "",
            scan_ssid: false,
            key_mgmt: "NONE",
          });
          setStatus(`Open network ${n.ssid} — marked unsafe; Save to create profile`);
          return;
        }
        pickNetwork({
          mode: "wifi",
          ssid: n.ssid,
          password: "",
          scan_ssid: false,
          key_mgmt: "WPA-PSK",
        });
        setStatus(`Selected ${n.ssid} (${signalLabel(n.signal)}) — enter password and Save`);
      });
      body.appendChild(tr);
    }
    const hidden = document.createElement("tr");
    hidden.className = "pick";
    hidden.innerHTML = `<td><em>Connect to Hidden WiFi</em></td><td></td><td></td>`;
    hidden.addEventListener("click", () => {
      pickNetwork({
        mode: "wifi",
        ssid: "",
        password: "",
        scan_ssid: true,
        key_mgmt: "WPA-PSK",
      });
      setStatus("Hidden WiFi — enter SSID/password and Save");
    });
    body.appendChild(hidden);
    setStatus(hint);
  } catch (e) {
    $("scan-hint").textContent = "Scan failed: " + e.message;
    setStatus("Scan failed: " + e.message);
  }
});

$("status").addEventListener("click", async () => {
  setFlash("Loading logs…");
  const data = await refreshConnectionStatus();
  openLogModal(data && data.logs);
  setFlash("");
});
$("btn-log-close").addEventListener("click", closeLogModal);
$("log-modal").addEventListener("click", (e) => {
  if (e.target === $("log-modal")) closeLogModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeLogModal(); closeScanModal(); }
});

$("btn-scan-close").addEventListener("click", closeScanModal);
$("scan-modal").addEventListener("click", (e) => {
  if (e.target === $("scan-modal")) closeScanModal();
});

$("btn-wap").addEventListener("click", () => {
  showEditor(null, {
    mode: "wap",
    ssid: "BloodPoint",
    password: "12345678",
    uplink: null,
    channel: 149,
    country_code: "IN",
  });
  setStatus("New WAP profile — edit and Save");
});

$("edit-password-show").addEventListener("change", () => {
  $("edit-password").type = $("edit-password-show").checked ? "text" : "password";
});

$("btn-remove").addEventListener("click", async () => {
  if (editingIndex == null || editingIndex === 0) return;
  if (!confirm("Remove profile " + editingIndex + "?")) return;
  try {
    state = await api("/api/profiles/" + editingIndex, { method: "DELETE" });
    fillProfileSelect();
    const bound = state.interfaces[selectedIface]?.profile ?? 0;
    showEditor(bound);
    setStatus("Removed profile; rebound editor to #" + bound);
  } catch (e) {
    setStatus("Remove failed: " + e.message);
  }
});

$("btn-save").addEventListener("click", async () => {
  const mode = $("edit-mode").value;
  let profile;
  if (mode === "wap") {
    const up = $("edit-uplink").value;
    profile = {
      mode: "wap",
      ssid: $("edit-ssid").value,
      password: $("edit-password").value,
      uplink: up || null,
      channel: parseInt($("edit-channel").value, 10) || 149,
      country_code: $("edit-country").value || "IN",
    };
  } else {
    profile = {
      mode: "wifi",
      ssid: $("edit-ssid").value,
      password: $("edit-password").value,
      scan_ssid: $("edit-scan-ssid").checked,
      key_mgmt: $("edit-key-mgmt").value || "WPA-PSK",
    };
  }
  try {
    const body = { profile };
    if (editingIndex != null) body.index = editingIndex;
    const res = await api("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state = res.state;
    const idx = res.index;
    if (editingIndex == null && selectedIface) {
      try {
        state = await api(`/api/interfaces/${encodeURIComponent(selectedIface)}/profile`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile: idx }),
        });
      } catch (e) {
        setStatus("Saved profile " + idx + " but bind failed: " + e.message);
        fillProfileSelect();
        return;
      }
    }
    fillProfileSelect();
    showEditor(idx);
    setStatus("Saved profile " + idx);
  } catch (e) {
    setStatus("Save failed: " + e.message);
  }
});

$("btn-apply").addEventListener("click", async () => {
  setFlash("Applying…");
  try {
    const res = await api("/api/apply", { method: "POST" });
    setFlash((res.messages || []).join(" · ") || "Applied.");
    await refreshConnectionStatus();
  } catch (e) {
    setFlash("Apply failed: " + e.message);
    await refreshConnectionStatus();
  }
});

watchGlanceTheme();
reload().then(() => {
  const idx = state.interfaces[selectedIface]?.profile;
  if (idx != null) showEditor(idx);
}).catch(e => setStatus("Error: " + e.message));
"""


# =============================================================================
# 8. CLI
# =============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Wireless WAP/WiFi dashboard",
        add_help=False,
    )
    p.add_argument(
        "--help",
        action="help",
        help="show this help message and exit",
    )
    p.add_argument(
        "-c",
        "--config",
        default="./state.json",
        help="path to state.json (default: ./state.json); created if missing",
    )
    p.add_argument(
        "-h",
        "--host_addr",
        default=DEFAULT_HOST_ADDR,
        metavar="ADDR",
        help=f"AP/gateway IPv4 for WAP mode (default: {DEFAULT_HOST_ADDR})",
    )
    p.add_argument(
        "--serve",
        type=int,
        default=None,
        metavar="PORT",
        help="serve dashboard on PORT (foreground); omit to apply once and exit",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _config_path, _meta, _host_addr
    args = parse_args(argv)
    _host_addr = args.host_addr
    _config_path = Path(args.config)
    _meta = META.load(_config_path)
    _meta.save(_config_path)

    if args.serve is None:
        msgs = apply_meta(_meta)
        for m in msgs:
            print(m)
        return 0

    try:
        msgs = apply_meta(_meta)
        for m in msgs:
            print(m, file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"initial apply warning: {e}", file=sys.stderr)

    host, port = "0.0.0.0", int(args.serve)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Listening on http://{host}:{port}  (config: {_config_path}, host_addr: {_host_addr})", flush=True)

    def _stop(signum: int, frame: Any) -> None:
        print("\nShutting down…", flush=True)
        server.shutdown()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        cleanup_atexit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
