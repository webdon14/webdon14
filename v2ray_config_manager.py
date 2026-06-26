#!/usr/bin/env python3
"""
V2Ray Config Manager

Features:
- Parse VLESS/VMess links
- Test latency (TCP and/or ICMP ping)
- Sort by fastest first
- Export sorted links to file (links/json)
- Generate QR codes (PNG)

Examples:
  python v2ray_config_manager.py --input-file links.txt --output-file sorted.txt --method auto --qr
  python v2ray_config_manager.py "vless://..." "vmess://..." --format json --output-file sorted.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse


@dataclass
class ConfigNode:
    raw_link: str
    protocol: str
    name: str
    host: str
    port: int
    params: Dict[str, str] = field(default_factory=dict)
    latency_ms: Optional[float] = None
    status: str = "untested"
    error: Optional[str] = None


class ParseError(Exception):
    pass


def _b64_decode_with_padding(value: str) -> bytes:
    value = value.strip()
    padding = len(value) % 4
    if padding:
        value += "=" * (4 - padding)
    return base64.urlsafe_b64decode(value.encode("utf-8"))


def parse_vless(link: str) -> ConfigNode:
    parsed = urlparse(link)
    if parsed.scheme.lower() != "vless":
        raise ParseError("Not a VLESS link")

    host = parsed.hostname
    if not host:
        raise ParseError("Missing host in VLESS link")

    port = parsed.port or 443
    params_raw = parse_qs(parsed.query)
    params = {k: v[0] if v else "" for k, v in params_raw.items()}
    name = unquote(parsed.fragment) if parsed.fragment else host

    return ConfigNode(
        raw_link=link,
        protocol="vless",
        name=name,
        host=host,
        port=port,
        params=params,
    )


def parse_vmess(link: str) -> ConfigNode:
    if not link.lower().startswith("vmess://"):
        raise ParseError("Not a VMess link")

    payload = link[len("vmess://") :].strip()
    if not payload:
        raise ParseError("Empty VMess payload")

    data: Dict[str, str]

    # Try base64-encoded JSON first (the most common format).
    try:
        decoded = _b64_decode_with_padding(payload).decode("utf-8", errors="replace")
        data = json.loads(decoded)
    except Exception:
        # Some tools may store plain JSON after vmess://
        try:
            data = json.loads(payload)
        except Exception as exc:
            raise ParseError(f"Invalid VMess payload: {exc}") from exc

    host = data.get("add") or data.get("host")
    if not host:
        raise ParseError("Missing host in VMess config")

    port_raw = data.get("port", "443")
    try:
        port = int(str(port_raw).strip())
    except ValueError as exc:
        raise ParseError("Invalid VMess port") from exc

    name = str(data.get("ps") or data.get("remark") or host)

    return ConfigNode(
        raw_link=link,
        protocol="vmess",
        name=name,
        host=str(host),
        port=port,
        params={k: str(v) for k, v in data.items()},
    )


def parse_link(link: str) -> ConfigNode:
    link = link.strip()
    if not link:
        raise ParseError("Empty link")

    if link.lower().startswith("vless://"):
        return parse_vless(link)
    if link.lower().startswith("vmess://"):
        return parse_vmess(link)

    raise ParseError("Unsupported link protocol (only vless:// and vmess://)")


def tcp_latency(host: str, port: int, timeout: float) -> float:
    start = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        pass
    end = time.perf_counter()
    return (end - start) * 1000.0


def ping_latency(host: str, timeout: float) -> float:
    is_windows = platform.system().lower().startswith("win")

    if is_windows:
        timeout_ms = str(max(1, int(timeout * 1000)))
        cmd = ["ping", "-n", "1", "-w", timeout_ms, host]
    else:
        timeout_s = str(max(1, int(round(timeout))))
        cmd = ["ping", "-c", "1", "-W", timeout_s, host]

    completed = subprocess.run(cmd, capture_output=True, text=True)
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")

    # Try multiple known ping formats.
    patterns = [
        r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms",
        r"Average\s*=\s*(\d+)ms",
        r"avg\s*=\s*\d+\.\d+/(\d+\.\d+)/\d+\.\d+/\d+\.\d+",
    ]

    for pattern in patterns:
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))

    raise RuntimeError("Could not parse ping latency")


def measure_node_latency(node: ConfigNode, method: str, timeout: float) -> ConfigNode:
    try:
        measured: Optional[float] = None

        if method in {"auto", "tcp"}:
            measured = tcp_latency(node.host, node.port, timeout)

        if measured is None and method in {"auto", "ping"}:
            measured = ping_latency(node.host, timeout)

        if measured is None:
            raise RuntimeError("No latency method executed")

        node.latency_ms = round(measured, 2)
        node.status = "ok"
        node.error = None
    except Exception as exc:
        node.latency_ms = None
        node.status = "failed"
        node.error = str(exc)

    return node


def read_links(input_file: Optional[str], cli_links: List[str]) -> List[str]:
    links: List[str] = []

    if input_file:
        path = Path(input_file)
        if not path.exists():
            raise FileNotFoundError(f"Input file not found: {input_file}")

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                links.append(line)

    for link in cli_links:
        if link and link.strip():
            links.append(link.strip())

    # Keep order, remove duplicates.
    seen = set()
    unique_links = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)

    return unique_links


def sort_nodes(nodes: List[ConfigNode]) -> List[ConfigNode]:
    return sorted(
        nodes,
        key=lambda n: (n.latency_ms is None, n.latency_ms if n.latency_ms is not None else float("inf")),
    )


def save_output(nodes: List[ConfigNode], output_file: str, output_format: str) -> None:
    path = Path(output_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "json":
        payload = [asdict(node) for node in nodes]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        lines = [node.raw_link for node in nodes]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safe_filename(name: str, index: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")
    if not cleaned:
        cleaned = f"node_{index}"
    return cleaned[:80]


def generate_qr_codes(nodes: List[ConfigNode], qr_dir: str) -> None:
    try:
        import qrcode  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "QR generation requires the 'qrcode' package. Install with: pip install qrcode[pil]"
        ) from exc

    out_dir = Path(qr_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, node in enumerate(nodes, start=1):
        if node.status != "ok":
            continue
        filename = _safe_filename(node.name, idx) + ".png"
        file_path = out_dir / filename
        img = qrcode.make(node.raw_link)
        img.save(file_path)


def print_summary(nodes: List[ConfigNode]) -> None:
    print("\nResults:")
    print("=" * 72)
    print(f"{'#':<4} {'Proto':<7} {'Name':<26} {'Host:Port':<24} {'Latency(ms)':>11}")
    print("-" * 72)

    for idx, node in enumerate(nodes, start=1):
        host_port = f"{node.host}:{node.port}"
        latency = f"{node.latency_ms:.2f}" if node.latency_ms is not None else "-"
        name = (node.name[:23] + "...") if len(node.name) > 26 else node.name
        print(f"{idx:<4} {node.protocol:<7} {name:<26} {host_port:<24} {latency:>11}")

    failed = [n for n in nodes if n.status != "ok"]
    if failed:
        print("\nFailed nodes:")
        for node in failed:
            print(f"- {node.name} ({node.host}:{node.port}) -> {node.error}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="V2Ray Config Manager (VLESS/VMess)")
    parser.add_argument("links", nargs="*", help="VLESS/VMess links")
    parser.add_argument("--input-file", help="Text file containing one link per line")
    parser.add_argument("--output-file", help="Output file path")
    parser.add_argument("--format", choices=["links", "json"], default="links", help="Output file format")
    parser.add_argument(
        "--method",
        choices=["auto", "tcp", "ping"],
        default="auto",
        help="Latency test method (auto tries tcp first, then ping)",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="Timeout per node in seconds")
    parser.add_argument("--workers", type=int, default=20, help="Concurrent workers for latency tests")
    parser.add_argument("--qr", action="store_true", help="Generate QR PNG files for successful nodes")
    parser.add_argument("--qr-dir", default="qrs", help="Directory for generated QR files")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        links = read_links(args.input_file, args.links)
    except Exception as exc:
        print(f"Error reading links: {exc}", file=sys.stderr)
        return 1

    if not links:
        print("No links provided. Use --input-file and/or pass links as arguments.", file=sys.stderr)
        return 1

    nodes: List[ConfigNode] = []
    parse_errors: List[str] = []

    for link in links:
        try:
            nodes.append(parse_link(link))
        except Exception as exc:
            parse_errors.append(f"{link[:80]} -> {exc}")

    if parse_errors:
        print("Parse warnings:")
        for msg in parse_errors:
            print(f"- {msg}")
        print()

    if not nodes:
        print("No valid VLESS/VMess links found.", file=sys.stderr)
        return 1

    max_workers = max(1, args.workers)
    tested_nodes: List[ConfigNode] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(measure_node_latency, node, args.method, args.timeout): node
            for node in nodes
        }
        for future in as_completed(future_map):
            tested_nodes.append(future.result())

    sorted_nodes = sort_nodes(tested_nodes)
    print_summary(sorted_nodes)

    if args.output_file:
        try:
            save_output(sorted_nodes, args.output_file, args.format)
            print(f"\nSaved output: {args.output_file}")
        except Exception as exc:
            print(f"Failed to save output: {exc}", file=sys.stderr)

    if args.qr:
        try:
            generate_qr_codes(sorted_nodes, args.qr_dir)
            print(f"Generated QR codes in: {args.qr_dir}")
        except Exception as exc:
            print(f"QR generation failed: {exc}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
