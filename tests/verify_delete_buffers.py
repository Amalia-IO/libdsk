#!/usr/bin/env python3
"""Assert native rcpmfs deletion, buffer ownership, recreation and truncate behavior."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any


BLOCK_SIZE = 4096
EXTENT_SIZE = BLOCK_SIZE * 8
SECTOR_SIZE = 1024
TAIL_SIZE = 128


class ProbeError(RuntimeError):
    pass


def load_probe_module():
    probe_path = Path(__file__).resolve().with_name("rcpmfs_erase_probe.py")
    spec = importlib.util.spec_from_file_location("rcpmfs_erase_probe", probe_path)
    if spec is None or spec.loader is None:
        raise ProbeError(f"Cannot load RPC helpers from {probe_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def payload_for(size: int, salt: int) -> bytes:
    return bytes((index * 73 + (index >> 5) * 19 + salt) & 0xFF
                 for index in range(size))


def changed_tail(payload: bytes) -> bytes:
    count = min(TAIL_SIZE, len(payload))
    start = len(payload) - count
    return payload[:start] + bytes(
        value ^ ((index * 29 + 0xA7) & 0xFF)
        for index, value in enumerate(payload[start:])
    )


def find_extents(rpc: Any, filename: str, evidence_label: str) -> list[dict[str, Any]]:
    directory = rpc.dump_directory(evidence_label)
    stem, extension = filename.upper().split(".", 1)
    target = stem.ljust(8).encode("ascii") + extension.ljust(3).encode("ascii")
    extents = []
    for entry_index in range(len(directory) // 32):
        raw = directory[entry_index * 32:(entry_index + 1) * 32]
        name = bytes(value & 0x7F for value in raw[1:12])
        if raw[0] != 0 or name != target:
            continue
        allocations = [int.from_bytes(raw[offset:offset + 2], "little")
                       for offset in range(16, 32, 2)]
        extents.append({
            "entry_index": entry_index,
            "directory_sector": (entry_index * 32) // SECTOR_SIZE,
            "offset_in_sector": (entry_index * 32) % SECTOR_SIZE,
            "EX": raw[12],
            "S2": raw[14],
            "RC": raw[15],
            "extent_number": (raw[14] << 5) | (raw[12] & 0x1F),
            "allocation_blocks": [block for block in allocations if block],
        })
    extents.sort(key=lambda item: (item["extent_number"], item["entry_index"]))
    if not extents:
        raise ProbeError(f"No directory extents found for {filename}")
    return extents


def write_pending_tail(rpc: Any, payload: bytes, extents: list[dict[str, Any]]) -> bytes:
    if rpc.geometry is None or rpc.geometry["secsize"] != SECTOR_SIZE:
        raise ProbeError("Native geometry is unavailable or not 1024-byte sectors")
    updated = changed_tail(payload)
    file_offset = len(payload) - min(TAIL_SIZE, len(payload))
    extent_index, within_extent = divmod(file_offset, EXTENT_SIZE)
    if extent_index >= len(extents):
        raise ProbeError(f"File offset {file_offset} has no matching directory extent")
    block_index, within_block = divmod(within_extent, BLOCK_SIZE)
    extent_blocks = extents[extent_index]["allocation_blocks"]
    if block_index >= len(extent_blocks):
        raise ProbeError(f"File offset {file_offset} has no allocation block")

    block_size_in_sectors = BLOCK_SIZE // SECTOR_SIZE
    sector_in_block, byte_in_sector = divmod(within_block, SECTOR_SIZE)
    linear_sector = extent_blocks[block_index] * block_size_in_sectors + sector_in_block
    sector_data = bytearray(rpc.pread(linear_sector))
    tail = updated[file_offset:]
    if byte_in_sector + len(tail) > len(sector_data):
        raise ProbeError("Pending tail crosses a native sector boundary")
    sector_data[byte_in_sector:byte_in_sector + len(tail)] = tail
    rpc.pwrite(linear_sector, bytes(sector_data))
    return updated


def erase_entry(rpc: Any, extent: dict[str, Any]) -> None:
    sector = bytearray(rpc.pread(extent["directory_sector"]))
    offset = extent["offset_in_sector"]
    if sector[offset] != 0:
        raise ProbeError(
            f"Refusing to erase unexpected user byte {sector[offset]} "
            f"at directory entry {extent['entry_index']}"
        )
    sector[offset] = 0xE5
    rpc.pwrite(extent["directory_sector"], bytes(sector))


def erase_all(rpc: Any, extents: list[dict[str, Any]], order: tuple[int, ...] | None) -> None:
    if order is None:
        sectors = {extent["directory_sector"] for extent in extents}
        if len(sectors) != 1:
            raise ProbeError("Atomic erase case requires all extents in one directory sector")
        sector_index = next(iter(sectors))
        sector = bytearray(rpc.pread(sector_index))
        for extent in extents:
            offset = extent["offset_in_sector"]
            if sector[offset] != 0:
                raise ProbeError(f"Unexpected user byte at directory entry {extent['entry_index']}")
            sector[offset] = 0xE5
        rpc.pwrite(sector_index, bytes(sector))
        return
    for extent_index in order:
        erase_entry(rpc, extents[extent_index])


def assert_file(path: Path, expected: bytes | None, label: str) -> None:
    if expected is None:
        if path.exists():
            raise ProbeError(f"{label}: deleted file unexpectedly exists ({path.stat().st_size} bytes)")
        actual_hash = None
        actual_size = None
    else:
        if not path.is_file():
            raise ProbeError(f"{label}: expected file is absent: {path}")
        actual = path.read_bytes()
        if actual != expected:
            mismatch = next((index for index, pair in enumerate(zip(actual, expected))
                             if pair[0] != pair[1]), min(len(actual), len(expected)))
            raise ProbeError(
                f"{label}: content mismatch at {mismatch}, actual length {len(actual)}, "
                f"expected length {len(expected)}"
            )
        actual_hash = hashlib.sha256(actual).hexdigest()
        actual_size = len(actual)
    print(json.dumps({"event": "assertion_pass", "label": label,
                      "exists": expected is not None, "length": actual_size,
                      "sha256": actual_hash}, sort_keys=True), flush=True)


def restart_bridge(bridge: Any) -> None:
    bridge.stop()
    bridge.start()


def run_delete_case(probe: Any, bridge: Any, export: Path, evidence: Path,
                    filename: str, size: int, case: str,
                    order: tuple[int, ...] | None) -> None:
    target = export / filename
    original = payload_for(size, len(case))
    target.write_bytes(original)
    rpc = probe.NativeRPC(bridge, evidence / "events.jsonl", evidence, case)
    try:
        rpc.open(export)
        extents = find_extents(rpc, filename, f"{case}_before")
        expected_count = (size + EXTENT_SIZE - 1) // EXTENT_SIZE
        if len(extents) != expected_count:
            raise ProbeError(f"{case}: expected {expected_count} extents, found {len(extents)}")
        updated = write_pending_tail(rpc, original, extents)
        assert_file(target, updated, f"{case}: pending partial write")
        erase_all(rpc, extents, order)
        rpc.close_handle()
        assert_file(target, None, f"{case}: absent after close")
        restart_bridge(bridge)
    finally:
        rpc.close_handle()


def run_sibling_case(probe: Any, bridge: Any, export: Path, evidence: Path) -> None:
    case = "sibling_pending_write"
    target_name, sibling_name = "ERASE01.BIN", "SIBLING.BIN"
    target, sibling = export / target_name, export / sibling_name
    target_payload = payload_for(258 * 128, 0x31)
    sibling_payload = payload_for(17 * 128, 0x72)
    target.write_bytes(target_payload)
    sibling.write_bytes(sibling_payload)
    rpc = probe.NativeRPC(bridge, evidence / "events.jsonl", evidence, case)
    try:
        rpc.open(export)
        target_extents = find_extents(rpc, target_name, f"{case}_target_before")
        sibling_extents = find_extents(rpc, sibling_name, f"{case}_sibling_before")
        target_updated = write_pending_tail(rpc, target_payload, target_extents)
        sibling_updated = write_pending_tail(rpc, sibling_payload, sibling_extents)
        erase_all(rpc, target_extents, tuple(range(len(target_extents))))
        rpc.close_handle()
        assert_file(target, None, f"{case}: deleted target absent after close")
        assert_file(sibling, sibling_updated, f"{case}: unrelated pending write preserved")
        restart_bridge(bridge)
    finally:
        rpc.close_handle()


def run_recreate_case(probe: Any, bridge: Any, export: Path, evidence: Path) -> None:
    case = "same_name_recreate"
    target = export / "ERASE01.BIN"
    original = payload_for(258 * 128, 0x24)
    recreated = payload_for(258 * 128, 0xD3)
    target.write_bytes(original)
    rpc = probe.NativeRPC(bridge, evidence / "events.jsonl", evidence, case)
    try:
        rpc.open(export)
        extents = find_extents(rpc, target.name, f"{case}_before")
        write_pending_tail(rpc, original, extents)
        erase_all(rpc, extents, tuple(range(len(extents))))
        target.write_bytes(recreated)
        rpc.close_handle()
        assert_file(target, recreated, f"{case}: recreated contents survive close")
        restart_bridge(bridge)
    finally:
        rpc.close_handle()


def run_truncate_case(probe: Any, bridge: Any, export: Path, evidence: Path) -> None:
    case = "truncate_preserves_prefix"
    target = export / "SMALL.BIN"
    original = payload_for(258, 0x56)
    target.write_bytes(original)
    rpc = probe.NativeRPC(bridge, evidence / "events.jsonl", evidence, case)
    try:
        rpc.open(export)
        extents = find_extents(rpc, target.name, f"{case}_before")
        updated = write_pending_tail(rpc, original, extents)
        extent = extents[0]
        sector = bytearray(rpc.pread(extent["directory_sector"]))
        entry_offset = extent["offset_in_sector"]
        sector[entry_offset + 13] = 0
        sector[entry_offset + 15] = 1
        rpc.pwrite(extent["directory_sector"], bytes(sector))
        rpc.close_handle()
        assert_file(target, updated[:128], f"{case}: surviving prefix and length")
    finally:
        rpc.close_handle()


def run_cached_append_case(probe: Any, bridge: Any, export: Path,
                           evidence: Path, directory_first: bool) -> None:
    case = "cached_append_directory_first" if directory_first else "cached_append_data_first"
    target = export / ("APPEND1.BIN" if directory_first else "APPEND2.BIN")
    original = payload_for(41 * 128, 0x35)
    updated = payload_for(42 * 128, 0x35)
    target.write_bytes(original)
    rpc = probe.NativeRPC(bridge, evidence / "events.jsonl", evidence, case)
    try:
        rpc.open(export)
        extents = find_extents(rpc, target.name, f"{case}_before")
        file_offset = len(original) - (len(original) % SECTOR_SIZE)
        extent_index, within_extent = divmod(file_offset, EXTENT_SIZE)
        block_index, within_block = divmod(within_extent, BLOCK_SIZE)
        extent_blocks = extents[extent_index]["allocation_blocks"]
        if block_index >= len(extent_blocks):
            raise ProbeError(f"{case}: append offset has no allocation block")
        sectors_per_block = BLOCK_SIZE // SECTOR_SIZE
        sector_in_block = within_block // SECTOR_SIZE
        data_sector = extent_blocks[block_index] * sectors_per_block + sector_in_block
        sector_data = bytearray(rpc.pread(data_sector))
        rpc.pwrite(data_sector, bytes(sector_data))

        extent = extents[extent_index]
        directory_sector = bytearray(rpc.pread(extent["directory_sector"]))
        entry_offset = extent["offset_in_sector"]
        if directory_sector[entry_offset + 15] != 41:
            raise ProbeError(f"{case}: expected RC=41 before append")
        directory_sector[entry_offset + 15] = 42
        sector_data[:len(updated) - file_offset] = updated[file_offset:]
        if directory_first:
            rpc.pwrite(extent["directory_sector"], bytes(directory_sector))
            rpc.pwrite(data_sector, bytes(sector_data))
        else:
            rpc.pwrite(data_sector, bytes(sector_data))
            assert_file(target, original, f"{case}: uncommitted append remains buffered")
            rpc.pwrite(extent["directory_sector"], bytes(directory_sector))
        assert_file(target, updated, f"{case}: appended bytes immediately visible")

        cached = rpc.pread(data_sector)
        if cached != bytes(sector_data):
            raise ProbeError(f"{case}: cached native sector does not contain appended bytes")
        print(json.dumps({"event": "assertion_pass",
                          "label": f"{case}: native cached readback",
                          "sha256": hashlib.sha256(cached).hexdigest()}, sort_keys=True),
              flush=True)
        rpc.close_handle()
    finally:
        rpc.close_handle()


def run_pending_cached_case(probe: Any, bridge: Any, export: Path,
                            evidence: Path, zero_length_owner: bool) -> None:
    case = "cached_zero_length_owner" if zero_length_owner else "cached_unallocated_sector"
    rpc = probe.NativeRPC(bridge, evidence / "events.jsonl", evidence, case)
    target = export / "PENDING.BIN"
    if zero_length_owner:
        target.write_bytes(payload_for(128, 0x63))
    try:
        rpc.open(export)
        if zero_length_owner:
            extent = find_extents(rpc, target.name, f"{case}_before")[0]
            data_sector = extent["allocation_blocks"][0] * (BLOCK_SIZE // SECTOR_SIZE)
            rpc.pwrite(data_sector, rpc.pread(data_sector))
            directory_sector = bytearray(rpc.pread(extent["directory_sector"]))
            entry_offset = extent["offset_in_sector"]
            directory_sector[entry_offset + 13] = 0
            directory_sector[entry_offset + 15] = 0
            rpc.pwrite(extent["directory_sector"], bytes(directory_sector))
        else:
            data_sector = probe.TOTAL_BLOCKS * (BLOCK_SIZE // SECTOR_SIZE) - 1
            rpc.pwrite(data_sector, payload_for(SECTOR_SIZE, 0x45))
        before = {path.name: path.read_bytes() for path in export.iterdir() if path.is_file()}
        updated = payload_for(SECTOR_SIZE, 0xA8)
        rpc.pwrite(data_sector, updated)
        after = {path.name: path.read_bytes() for path in export.iterdir() if path.is_file()}
        if before != after:
            raise ProbeError(f"{case}: buffered write changed the host export")
        if zero_length_owner:
            assert_file(target, b"", f"{case}: zero valid bytes stay buffered")
        else:
            print(json.dumps({"event": "assertion_pass",
                              "label": f"{case}: host export unchanged"}, sort_keys=True),
                  flush=True)
        if rpc.pread(data_sector) != updated:
            raise ProbeError(f"{case}: pending sector contents were not retained")
        print(json.dumps({"event": "assertion_pass",
                          "label": f"{case}: pending native readback"}, sort_keys=True),
              flush=True)
        if zero_length_owner:
            directory_sector[entry_offset + 15] = 1
            rpc.pwrite(extent["directory_sector"], bytes(directory_sector))
            assert_file(target, updated[:128], f"{case}: committed prefix later flushed")
        rpc.close_handle()
    finally:
        rpc.close_handle()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forkslave", type=Path, required=True)
    parser.add_argument("--library", type=Path, help="native library path recorded in evidence")
    args = parser.parse_args()

    probe = load_probe_module()
    forkslave = args.forkslave.resolve()
    library = probe.library_path_for(forkslave, args.library)
    if not forkslave.is_file() or not library.is_file():
        raise ProbeError(f"Expected native backend and library: {forkslave}, {library}")
    run_id = uuid.uuid4().hex[:10]
    export = forkslave.parent / f"delete-buffer-{run_id}"
    evidence = forkslave.parent / f"delete-buffer-{run_id}-evidence"
    export.mkdir()
    evidence.mkdir()
    (export / ".libdsk.ini").write_text(probe.CONFIG_TEXT, encoding="ascii", newline="\n")

    bridge = None
    try:
        server_module = probe.load_native_backend()
        bridge = server_module.ForkslaveBridge(str(forkslave), str(export))
        bridge.start()

        for size, case, order in (
            (17 * 128, "records17_primary", (0,)),
            (258 * 128, "records258_primary_secondary", (0, 1)),
            (258 * 128, "records258_secondary_primary", (1, 0)),
            (258 * 128, "records258_same_sector", None),
            (256, "bytes256_primary", (0,)),
            (257, "bytes257_primary", (0,)),
            (258, "bytes258_primary", (0,)),
        ):
            run_delete_case(probe, bridge, export, evidence, "ERASE01.BIN", size,
                            case, order)
        run_sibling_case(probe, bridge, export, evidence)
        run_recreate_case(probe, bridge, export, evidence)
        run_truncate_case(probe, bridge, export, evidence)
        run_cached_append_case(probe, bridge, export, evidence, directory_first=True)
        if bridge.process.wait(timeout=2) != 0:
            raise ProbeError("Native backend failed after directory-first append")
        run_cached_append_case(probe, bridge, export, evidence, directory_first=False)
        if bridge.process.wait(timeout=2) != 0:
            raise ProbeError("Native backend failed after data-first append")
        run_pending_cached_case(probe, bridge, export, evidence, zero_length_owner=True)
        if bridge.process.wait(timeout=2) != 0:
            raise ProbeError("Native backend failed after zero-length owner case")
        run_pending_cached_case(probe, bridge, export, evidence, zero_length_owner=False)
        if bridge.process.wait(timeout=2) != 0:
            raise ProbeError("Native backend failed after unallocated sector case")
        print(json.dumps({"event": "probe_complete", "cases": 14, "assertions": 28,
                          "export": str(export), "evidence": str(evidence),
                          "forkslave": str(forkslave),
                          "dll_sha256": hashlib.sha256(
                              library.read_bytes()).hexdigest()},
                         sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"event": "probe_failure", "error_type": type(exc).__name__,
                          "error": str(exc), "export": str(export),
                          "evidence": str(evidence)}, sort_keys=True), flush=True)
        traceback.print_exc()
        return 1
    finally:
        if bridge is not None:
            bridge.stop()


if __name__ == "__main__":
    raise SystemExit(main())