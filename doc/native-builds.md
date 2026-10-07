# Native Library Builds

Build native libraries from this checkout. Keep generated build directories
separate from source changes.

## Windows x86

The native CMake target builds a 32-bit DLL. It uses only this repository's
sources and native configuration. With Visual Studio
2019 Build Tools installed, run from the repository root:

```powershell
cmake -S . -B build/windows-x86 -G "Visual Studio 16 2019" -A Win32
cmake --build build/windows-x86 --config Release
```

Select the generator matching your installed Visual Studio version.

## Linux x86-64

Use the upstream Autotools build in a fresh, out-of-tree directory:

```sh
source_directory="$(pwd)"
mkdir -p ../libdsk-build/linux-x86_64
cd ../libdsk-build/linux-x86_64
"$source_directory/configure" --prefix="$PWD/install"
make -j4
python3 "$source_directory/tests/verify_delete_buffers.py" \
  --forkslave "$PWD/tools/forkslave" \
  --library "$PWD/lib/.libs/libdsk.so.5.4.2"
python3 "$source_directory/tests/rcpmfs_erase_probe.py" \
  --forkslave "$PWD/tools/forkslave" \
  --library "$PWD/lib/.libs/libdsk.so.5.4.2"
```

Do not run `make install` into system locations when validating a candidate.
The generated `tools/forkslave` libtool wrapper selects the build's library.
Running `tools/.libs/forkslave` directly can select a different installed
library with the same SONAME.

## Regression Checks

The tests use a local, bounded RPC helper and disposable directories. They
do not require a running TCP server or hardware. They cover deletion order,
pending partial-sector writes, sibling-file preservation, same-name
recreation and truncation of case-sensitive host filenames.

The deletion-buffer verifier checks 14 cases with 28 assertions, including both
cached-append orderings and uncommitted cached sectors. Add `--extent-only` to
check 13 host-imported file lengths at logical and physical extent boundaries,
including EX/S2 and RC for every intermediate extent. Exact full extents must
use RC=128 rather than wrapping to zero, and EX must identify the last occupied
logical extent. The erase
probe records three deletion orders; inspect its post-close snapshots to
confirm file absence, not merely successful RPC replies.

Offline regression success does not establish stability for all live
filesystem operations or concurrent writers. Promote a candidate only after
the required application-level tests also pass.

For Windows, these regression scripts accept any compatible native RPC
backend using `--forkslave`; the library CMake target does not build a
custom application backend. The RPC packet format is documented in
[protocol.txt](protocol.txt).