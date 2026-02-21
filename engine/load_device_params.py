#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _coerce_scalar(val: str) -> Any:
    v = val.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "~", ""):
        return None
    try:
        if any(ch in v for ch in (".", "e", "E")):
            return float(v)
        return int(v)
    except Exception:
        return v


def cheap_yaml_load(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    stack: List[Tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        m = re.match(r"^\s*([A-Za-z0-9_.-]+):\s*(.*)$", raw)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)

        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root

        if rest == "" or rest in ("|", ">"):
            newd: Dict[str, Any] = {}
            parent[key] = newd
            stack.append((indent, newd))
        else:
            parent[key] = _coerce_scalar(rest)
    return root


def load_yaml(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except Exception:
        return cheap_yaml_load(text)


def get_entry(data: Dict[str, Any], l3_mb: int) -> Dict[str, Any]:
    if str(l3_mb) in data and isinstance(data[str(l3_mb)], dict):
        return data[str(l3_mb)]
    if l3_mb in data and isinstance(data[l3_mb], dict):
        return data[l3_mb]
    raise KeyError(f"missing L3 entry for {l3_mb}")


def must(d: Dict[str, Any], k: str) -> Any:
    if k not in d:
        raise KeyError(f"missing key: {k}")
    return d[k]


def device_file(devices_dir: Path, tech: str) -> Path:
    # tech is a file stem like "sram14" -> sram14.yaml
    return devices_dir / f"{tech}.yaml"


def pick_blob(entry: Dict[str, Any], want: str) -> Dict[str, Any]:
    """
    Supports both schemas:
      (A) single-device schema:
          32: { rd_cyc: ..., wr_cyc: ..., r_pj: ..., w_pj: ..., leak_mw: ... }
      (B) old hybrid schema:
          32: { sram: {...}, mram: {...} }

    want is "sram" or "mram" and is only used if schema (B) is present.
    """
    if want in entry and isinstance(entry[want], dict):
        return entry[want]
    # If only one of the nested keys exists, allow it (helpful for transitional files).
    if want == "sram" and "sram" in entry and isinstance(entry["sram"], dict):
        return entry["sram"]
    if want == "mram" and "mram" in entry and isinstance(entry["mram"], dict):
        return entry["mram"]
    # Otherwise assume single-device schema
    return entry


def emit(prefix: str, blob: Dict[str, Any]) -> None:
    # Emits shell assignments (not export) so callers can: source <(python ...)
    print(f"{prefix}_RD_CYC={must(blob,'rd_cyc')}")
    print(f"{prefix}_WR_CYC={must(blob,'wr_cyc')}")
    print(f"{prefix}_R_PJ={must(blob,'r_pj')}")
    print(f"{prefix}_W_PJ={must(blob,'w_pj')}")
    print(f"{prefix}_LEAK_MW={must(blob,'leak_mw')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Emit SRAM_* and MRAM_* device params as shell assignments.")
    ap.add_argument("--l3", required=True, type=int, choices=[2, 32, 128])
    ap.add_argument("--devices-dir", default="", help="Override devices dir (default: mx2/config/devices)")

    # Non-HCA (single TECH)
    ap.add_argument("--tech", default="", help="Single tech name (e.g., sram14, mram14, sram7, mram32, sram32)")

    # HCA (explicit SRAM/MRAM techs)
    ap.add_argument("--sram-tech", default="", help="SRAM tech name for HCA (e.g., sram14)")
    ap.add_argument("--mram-tech", default="", help="MRAM tech name for HCA (e.g., mram14)")

    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent.parent  # mx2/engine -> mx2 -> repo
    devices_dir = Path(args.devices_dir).resolve() if args.devices_dir else (repo_root / "mx2" / "config" / "devices")

    try:
        if args.sram_tech or args.mram_tech:
            if not args.sram_tech or not args.mram_tech:
                raise SystemExit("[ERR] must set BOTH --sram-tech and --mram-tech (or neither).")

            s_path = device_file(devices_dir, args.sram_tech)
            m_path = device_file(devices_dir, args.mram_tech)
            if not s_path.exists():
                raise SystemExit(f"[ERR] SRAM tech file not found: {s_path}")
            if not m_path.exists():
                raise SystemExit(f"[ERR] MRAM tech file not found: {m_path}")

            s_data = load_yaml(s_path)
            m_data = load_yaml(m_path)

            s_entry = get_entry(s_data, args.l3)
            m_entry = get_entry(m_data, args.l3)

            s_blob = pick_blob(s_entry, "sram")
            m_blob = pick_blob(m_entry, "mram")

            emit("SRAM", s_blob)
            emit("MRAM", m_blob)
            return

        if not args.tech:
            raise SystemExit("[ERR] must set --tech (or set both --sram-tech and --mram-tech).")

        path = device_file(devices_dir, args.tech)
        if not path.exists():
            raise SystemExit(f"[ERR] tech file not found: {path}")

        data = load_yaml(path)
        entry = get_entry(data, args.l3)

        # If old hybrid schema exists in this file, use it. Otherwise single-device => mirror.
        if ("sram" in entry and isinstance(entry.get("sram"), dict)) or ("mram" in entry and isinstance(entry.get("mram"), dict)):
            if "sram" not in entry or "mram" not in entry:
                raise SystemExit(f"[ERR] hybrid schema requires both sram and mram blocks: {path} (l3={args.l3})")
            emit("SRAM", pick_blob(entry, "sram"))
            emit("MRAM", pick_blob(entry, "mram"))
        else:
            # single-device schema: mirror into both to satisfy existing bash consumers
            emit("SRAM", entry)
            emit("MRAM", entry)

    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERR] {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()