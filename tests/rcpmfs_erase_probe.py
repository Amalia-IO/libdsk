#!/usr/bin/env python3
"""Disposable host-only reproducer for rcpmfs extent deletion after a partial write."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import traceback
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from native_backend import NativeBackend


FILE_SIZE = 33024
BLOCK_SIZE = 4096
DIR_BLOCKS = 8
TOTAL_BLOCKS = 2048
SECTOR_SIZE = 1024
DIR_SECTORS = 32
FILENAME = "ERASE01.BIN"
GEOM_FIELDS = (
    "sidedness",
    "cylinders",
    "heads",
    "sectors",
    "secbase",
    "secsize",
    "datarate",
    "rwgap",
    "fmtgap",
    "fm",
    "nomulti",
    "noskip",
)
CONFIG_TEXT = """[RCPMFS]
BlockSize=4096
DirBlocks=8
TotalBlocks=2048
SysTracks=0
Version=3
Format=myz80
"""
LIBDSK_SOURCE_VERSION = "1.5.22"


class ProbeError(RuntimeError):
    pass


def emit(log_path: Path, event: str, **values: Any) -> None:
    record = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **values,
    }
    line = json.dumps(record, sort_keys=True, default=str)
    print(line, flush=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as log:
        log.write(line + "\n")


def load_native_backend():
    return SimpleNamespace(ForkslaveBridge=NativeBackend)


def library_path_for(executable: Path, requested: Path | None) -> Path:
    if requested is not None:
        return requested.resolve(strict=True)
    if sys.platform == "win32":
        return executable.parent / "libdsk.dll"
    return executable.parent.parent / "lib" / ".libs" / "libdsk.so"


def pack_string(value: str | None) -> bytes:
    if value is None:
        return struct.pack(">H", 0)
    encoded = value.encode("ascii") + b"\x00"
    return struct.pack(">H", len(encoded)) + encoded


def pack_geometry(geometry: dict[str, int]) -> bytes:
    return b"".join(struct.pack(">h", geometry[name]) for name in GEOM_FIELDS)


def unpack_geometry(data: bytes) -> dict[str, int]:
    if len(data) != 24:
        raise ProbeError(f"Malformed geometry: expected 24 bytes, received {len(data)}")
    return {
        name: struct.unpack(">h", data[index * 2:index * 2 + 2])[0]
        for index, name in enumerate(GEOM_FIELDS)
    }


def deterministic_payload() -> bytes:
    return bytes((index * 73 + (index >> 5) * 19 + 0x5B) & 0xFF for index in range(FILE_SIZE))


def expected_after_partial_write(payload: bytes) -> bytes:
    changed_tail = bytes(value ^ ((index * 29 + 0xA7) & 0xFF) for index, value in enumerate(payload[-256:]))
    return payload[:-256] + changed_tail


def sector_chs(geometry: dict[str, int], linear_sector: int) -> tuple[int, int, int]:
    heads = geometry["heads"]
    sectors = geometry["sectors"]
    if heads <= 0 or sectors <= 0 or geometry["cylinders"] <= 0:
        raise ProbeError(f"Invalid native geometry: {geometry}")
    cylinder, track_sector = divmod(linear_sector, heads * sectors)
    head, sector_index = divmod(track_sector, sectors)
    if cylinder >= geometry["cylinders"]:
        raise ProbeError(f"Linear sector {linear_sector} exceeds native geometry {geometry}")
    return cylinder, head, geometry["secbase"] + sector_index


class NativeRPC:
    def __init__(self, bridge: Any, log_path: Path, evidence: Path, case_name: str):
        self.bridge = bridge
        self.log_path = log_path
        self.evidence = evidence
        self.case_name = case_name
        self.handle: int | None = None
        self.geometry: dict[str, int] | None = None

    def call(self, code: int, body: bytes, expected_lengths: set[int], label: str) -> bytes:
        request = struct.pack(">H", code) + body
        with redirect_stdout(sys.stderr):
            response = self.bridge.call(request)
        if len(response) not in expected_lengths:
            raise ProbeError(
                f"Malformed {label} response: expected lengths {sorted(expected_lengths)}, "
                f"received {len(response)} bytes ({response.hex()})"
            )
        emit(self.log_path, "rpc", case=self.case_name, operation=label, code=code,
             request_bytes=len(request), response_bytes=len(response), response_hex=response[:32].hex())
        return response

    def open(self, export_path: Path) -> None:
        body = pack_string(str(export_path.resolve())) + pack_string("rcpmfs") + pack_string(None)
        response = self.call(101, body, {6}, "OPEN")
        error, handle = struct.unpack(">hI", response)
        self.check_error("OPEN", error)
        self.handle = handle
        emit(self.log_path, "opened", case=self.case_name, handle=handle,
             filename=str(export_path.resolve()), driver="rcpmfs", compression=None)

        response = self.call(121, struct.pack(">I", handle), {26}, "GETGEOM")
        error = struct.unpack(">h", response[:2])[0]
        geometry = unpack_geometry(response[2:])
        self.check_error("GETGEOM", error)
        if geometry["secsize"] != SECTOR_SIZE:
            raise ProbeError(f"Expected {SECTOR_SIZE}-byte sectors, got {geometry}")
        self.geometry = geometry
        emit(self.log_path, "geometry", case=self.case_name, native_error=error,
               geometry=geometry)

    @staticmethod
    def check_error(operation: str, error: int) -> None:
        if error != 0:
            raise ProbeError(f"{operation} returned native libdsk error {error}")

    def pread(self, linear_sector: int) -> bytes:
        if self.handle is None or self.geometry is None:
            raise ProbeError("PREAD requested without an open native handle")
        cylinder, head, sector = sector_chs(self.geometry, linear_sector)
        body = (struct.pack(">I", self.handle) + pack_geometry(self.geometry)
                + struct.pack(">iii", cylinder, head, sector))
        response = self.call(105, body, {4 + SECTOR_SIZE}, "PREAD")
        error, length = struct.unpack(">hH", response[:4])
        self.check_error("PREAD", error)
        data = response[4:]
        if length != SECTOR_SIZE or len(data) != length:
            raise ProbeError(
                f"Malformed PREAD buffer: announced {length}, received {len(data)}, "
                f"expected {SECTOR_SIZE}"
            )
        return data

    def pwrite(self, linear_sector: int, data: bytes) -> None:
        if self.handle is None or self.geometry is None:
            raise ProbeError("PWRITE requested without an open native handle")
        if len(data) != SECTOR_SIZE:
            raise ProbeError(f"PWRITE must contain one full {SECTOR_SIZE}-byte sector")
        cylinder, head, sector = sector_chs(self.geometry, linear_sector)
        body = (struct.pack(">I", self.handle) + pack_geometry(self.geometry)
                + struct.pack(">H", len(data)) + data
                + struct.pack(">iii", cylinder, head, sector))
        response = self.call(108, body, {2}, "PWRITE")
        self.check_error("PWRITE", struct.unpack(">h", response)[0])

    def close_handle(self) -> None:
        if self.handle is None:
            return
        handle = self.handle
        self.handle = None
        try:
            response = self.call(103, struct.pack(">I", handle), {2}, "CLOSE")
            error = struct.unpack(">h", response)[0]
            self.check_error("CLOSE", error)
            emit(self.log_path, "closed", case=self.case_name, handle=handle, native_error=error)
        except Exception as exc:
            emit(self.log_path, "cleanup_failure", case=self.case_name,
                 operation="CLOSE", handle=handle, error=repr(exc))
            raise

    def dump_directory(self, label: str) -> bytes:
        data = b"".join(self.pread(sector) for sector in range(DIR_SECTORS))
        path = self.evidence / f"{self.case_name}_{label}_dir.bin"
        path.write_bytes(data)
        emit(self.log_path, "directory_dump", case=self.case_name, label=label,
             sectors=DIR_SECTORS, bytes=len(data), path=str(path))
        return data


def find_extents(directory: bytes) -> list[dict[str, Any]]:
    stem, extension = FILENAME.upper().split(".", 1)
    target = stem.ljust(8).encode("ascii") + extension.ljust(3).encode("ascii")
    extents = []
    for entry_index in range(len(directory) // 32):
        raw = directory[entry_index * 32:(entry_index + 1) * 32]
        user = raw[0]
        name = bytes(value & 0x7F for value in raw[1:12])
        if user != 0 or name != target:
            continue
        allocations = [int.from_bytes(raw[offset:offset + 2], "little")
                       for offset in range(16, 32, 2)]
        extent = {
            "entry_index": entry_index,
            "directory_sector": (entry_index * 32) // SECTOR_SIZE,
            "offset_in_sector": (entry_index * 32) % SECTOR_SIZE,
            "user": user,
            "name": name.decode("ascii", errors="replace"),
            "EX": raw[12],
            "S2": raw[14],
            "RC": raw[15],
            "extent_number": (raw[14] << 5) | (raw[12] & 0x1F),
            "allocation_blocks": [block for block in allocations if block],
        }
        extents.append(extent)
    extents.sort(key=lambda item: (item["extent_number"], item["entry_index"]))
    return extents


def host_snapshot(log_path: Path, case_name: str, target_path: Path,
                  expected: bytes, stage: str) -> dict[str, Any]:
    if target_path.is_file():
        actual = target_path.read_bytes()
        actual_hash = hashlib.sha256(actual).hexdigest()
        expected_hash = hashlib.sha256(expected).hexdigest()
        result = {
            "exists": True,
            "length": len(actual),
            "sha256": actual_hash,
            "expected_length": len(expected),
            "expected_sha256": expected_hash,
            "matched_hash": len(actual) == len(expected) and actual_hash == expected_hash,
        }
    else:
        result = {
            "exists": False,
            "length": None,
            "sha256": None,
            "expected_length": len(expected),
            "expected_sha256": hashlib.sha256(expected).hexdigest(),
            "matched_hash": False,
        }
    emit(log_path, "host_snapshot", case=case_name, stage=stage,
         path=str(target_path), **result)
    return result


def prepare_partial_write(rpc: NativeRPC, payload: bytes, extents: list[dict[str, Any]],
                          log_path: Path) -> bytes:
    if len(extents) != 2:
        raise ProbeError(f"Expected exactly two file extents, found {len(extents)}: {extents}")
    if len(extents[0]["allocation_blocks"]) != 8 or not extents[1]["allocation_blocks"]:
        raise ProbeError(f"Unexpected allocation map for 33024-byte file: {extents}")
    if rpc.geometry is None or rpc.geometry["secsize"] != SECTOR_SIZE:
        raise ProbeError("Native geometry is unavailable or has the wrong sector size")

    extent_capacity = BLOCK_SIZE * 8
    byte_offset = FILE_SIZE - 256
    extent_index, within_extent = divmod(byte_offset, extent_capacity)
    block_index, within_block = divmod(within_extent, BLOCK_SIZE)
    extent_blocks = extents[extent_index]["allocation_blocks"]
    if block_index >= len(extent_blocks):
        raise ProbeError(f"File offset {byte_offset} is not represented by its allocation map")
    sectors_per_block = BLOCK_SIZE // SECTOR_SIZE
    sector_in_block = within_block // SECTOR_SIZE
    linear_sector = extent_blocks[block_index] * sectors_per_block + sector_in_block
    sector_data = bytearray(rpc.pread(linear_sector))
    tail_start = within_block % SECTOR_SIZE
    if tail_start + 256 > len(sector_data):
        raise ProbeError("Final 256-byte file tail crosses the selected native sector")
    changed_tail = expected_after_partial_write(payload)[-256:]
    sector_data[tail_start:tail_start + 256] = changed_tail
    rpc.pwrite(linear_sector, bytes(sector_data))
    updated = expected_after_partial_write(payload)
    emit(log_path, "partial_data_write", case=rpc.case_name, file_offset=byte_offset,
         linear_sector=linear_sector, sector_chs=sector_chs(rpc.geometry, linear_sector),
         sector_size=SECTOR_SIZE, changed_file_bytes=256, allocation_block=extent_blocks[block_index],
         expected_sha256=hashlib.sha256(updated).hexdigest())
    return updated


def erase_entry(rpc: NativeRPC, extent: dict[str, Any], label: str,
                evidence: Path, log_path: Path) -> None:
    directory_sector = extent["directory_sector"]
    before = rpc.pread(directory_sector)
    before_path = evidence / f"{rpc.case_name}_{label}_before_sector.bin"
    before_path.write_bytes(before)
    entry_offset = extent["offset_in_sector"]
    if before[entry_offset] != 0:
        raise ProbeError(
            f"Refusing to erase unexpected directory user byte 0x{before[entry_offset]:02x} "
            f"at entry {extent['entry_index']}"
        )
    after = bytearray(before)
    after[entry_offset] = 0xE5
    rpc.pwrite(directory_sector, bytes(after))
    after_path = evidence / f"{rpc.case_name}_{label}_after_sector.bin"
    after_path.write_bytes(after)
    emit(log_path, "directory_entry_erased", case=rpc.case_name, extent_label=label,
         entry_index=extent["entry_index"], directory_sector=directory_sector,
         offset_in_sector=entry_offset, EX=extent["EX"], S2=extent["S2"], RC=extent["RC"],
         allocation_blocks=extent["allocation_blocks"], before_path=str(before_path),
         after_path=str(after_path))


def run_case(bridge: Any, export_path: Path, evidence: Path, log_path: Path,
             payload: bytes, case_name: str, erase_order: tuple[int, ...] | None) -> None:
    target_path = export_path / FILENAME
    target_path.write_bytes(payload)
    emit(log_path, "case_start", case=case_name, target=str(target_path), size=len(payload),
         baseline_sha256=hashlib.sha256(payload).hexdigest())
    rpc = NativeRPC(bridge, log_path, evidence, case_name)
    try:
        rpc.open(export_path)
        directory_before = rpc.dump_directory("before")
        extents = find_extents(directory_before)
        if len(extents) != 2:
            raise ProbeError(f"Expected two native directory extents, found {len(extents)}: {extents}")
        emit(log_path, "extent_map", case=case_name, extents=extents,
             same_native_directory_sector=(extents[0]["directory_sector"] == extents[1]["directory_sector"]))

        updated_payload = prepare_partial_write(rpc, payload, extents, log_path)
        host_snapshot(log_path, case_name, target_path, updated_payload, "after_partial_data_write")

        if erase_order is None:
            if extents[0]["directory_sector"] != extents[1]["directory_sector"]:
                emit(log_path, "case_skipped", case=case_name,
                     reason="primary and secondary extents do not share one native directory sector")
                return
            sector_index = extents[0]["directory_sector"]
            before = rpc.pread(sector_index)
            before_path = evidence / f"{case_name}_both_before_sector.bin"
            before_path.write_bytes(before)
            after = bytearray(before)
            for extent in extents:
                offset = extent["offset_in_sector"]
                if after[offset] != 0:
                    raise ProbeError(f"Unexpected directory user byte at entry {extent['entry_index']}")
                after[offset] = 0xE5
            rpc.pwrite(sector_index, bytes(after))
            (evidence / f"{case_name}_both_after_sector.bin").write_bytes(after)
            emit(log_path, "both_extents_erased_in_one_write", case=case_name,
                 directory_sector=sector_index, entry_indices=[item["entry_index"] for item in extents],
                 before_path=str(before_path), after_path=str(evidence / f"{case_name}_both_after_sector.bin"))
            rpc.dump_directory("after_both_write")
            host_snapshot(log_path, case_name, target_path, updated_payload, "after_both_directory_write")
        else:
            for extent_index in erase_order:
                label = "primary" if extent_index == 0 else "secondary"
                erase_entry(rpc, extents[extent_index], label, evidence, log_path)
                rpc.dump_directory(f"after_{label}_write")
                host_snapshot(log_path, case_name, target_path, updated_payload,
                              f"after_{label}_directory_write")

        rpc.close_handle()
        host_snapshot(log_path, case_name, target_path, updated_payload, "after_close")
        emit(log_path, "case_complete", case=case_name)
    finally:
        rpc.close_handle()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--forkslave",
        type=Path,
        required=True,
        help="native backend executable; use the libtool wrapper on Linux",
    )
    parser.add_argument("--library", type=Path, help="native library path recorded in evidence")
    args = parser.parse_args()

    script_path = Path(__file__).resolve()
    repository_root = script_path.parents[1]
    forkslave_path = args.forkslave.resolve(strict=True)
    dll_path = library_path_for(forkslave_path, args.library)
    build_info_path = forkslave_path.parent / "libdsk-build-info.txt"
    if not build_info_path.is_file() and args.forkslave:
        build_info_path = forkslave_path.parent.parent / "libdsk-build-info.txt"
    parent = forkslave_path.parent
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    export_path = parent / f"native-erase-{run_id}"
    evidence = parent / f"native-erase-{run_id}-evidence"
    log_path = evidence / "events.jsonl"
    bridge = None
    result = 0

    try:
        if not forkslave_path.is_file() or not dll_path.is_file():
            raise ProbeError(f"Native tools missing: forkslave={forkslave_path}, libdsk={dll_path}")
        parent.mkdir(parents=True, exist_ok=True)
        export_path.mkdir(parents=False, exist_ok=False)
        evidence.mkdir(parents=False, exist_ok=False)
        (export_path / ".libdsk.ini").write_text(CONFIG_TEXT, encoding="ascii", newline="\n")
        payload = deterministic_payload()
        payload_path = evidence / "payload_33024.bin"
        payload_path.write_bytes(payload)
        (evidence / "profile_libdsk.ini").write_text(CONFIG_TEXT, encoding="ascii", newline="\n")

        emit(log_path, "probe_start", script=str(script_path), source_root=str(repository_root),
             forkslave=str(forkslave_path), forkslave_sha256=hashlib.sha256(forkslave_path.read_bytes()).hexdigest(),
             libdsk=str(dll_path), libdsk_sha256=hashlib.sha256(dll_path.read_bytes()).hexdigest(),
               libdsk_source_version=LIBDSK_SOURCE_VERSION,
               libdsk_build_info=(build_info_path.read_text(encoding="ascii").splitlines()
                              if build_info_path.is_file() else None),
             export=str(export_path), evidence=str(evidence), payload=str(payload_path),
             payload_length=len(payload), payload_sha256=hashlib.sha256(payload).hexdigest(),
             profile_source="generated from verified rcpmfs geometry; protected live export not accessed",
             filler_files_created=0, hardware_or_server_touched=False)

        module = load_native_backend()
        bridge = module.ForkslaveBridge(str(forkslave_path), str(export_path))
        with redirect_stdout(sys.stderr):
            bridge.start()
        emit(log_path, "native_process_started", pid=bridge.process.pid,
               working_directory=str(export_path), library_directory=str(dll_path.parent))

        run_case(bridge, export_path, evidence, log_path, payload,
                 "primary_then_secondary", (0, 1))
        run_case(bridge, export_path, evidence, log_path, payload,
                 "secondary_then_primary", (1, 0))
        run_case(bridge, export_path, evidence, log_path, payload,
                 "both_same_sector", None)
        emit(log_path, "probe_complete", export=str(export_path), evidence=str(evidence))
    except Exception as exc:
        result = 1
        try:
            emit(log_path, "probe_failure", error_type=type(exc).__name__, error=str(exc),
                 traceback=traceback.format_exc(), export=str(export_path), evidence=str(evidence))
        except Exception:
            print(json.dumps({"event": "probe_failure", "error": repr(exc)}), flush=True)
        print(traceback.format_exc(), file=sys.stderr, flush=True)
    finally:
        if bridge is not None:
            try:
                with redirect_stdout(sys.stderr):
                    bridge.stop()
                emit(log_path, "native_process_stopped")
            except Exception as exc:
                result = 1
                try:
                    emit(log_path, "cleanup_failure", operation="stop_bridge", error=repr(exc))
                except Exception:
                    print(f"Bridge cleanup failure: {exc!r}", file=sys.stderr, flush=True)
        if export_path.exists():
            print(json.dumps({"event": "paths", "export": str(export_path),
                              "evidence": str(evidence)}, sort_keys=True), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())