# LibDsk 1.5.22

LibDsk is a library for accessing floppy disks and disk image files. It is
intended for emulator tools, filesystem utilities and emulators that need
access to real disks or their images.

## This is an unofficial fork!

Originally written by John Elliott
[seasip.webmaster@gmail.com](mailto:seasip.webmaster@gmail.com) and distributed
under the GNU Library General Public License version 2, or, at your option,
any later version.

*Primary site*: [https://www.seasip.info/Unix/LibDsk/](https://www.seasip.info/Unix/LibDsk/)

This repository is based on the original LibDsk 1.5.22 source release. It is
not John Elliott's official upstream repository. The primary site lists
1.5.22 as a development version and provides the original
[source archive](https://www.seasip.info/Unix/LibDsk/libdsk-1.5.22.tar.gz) and
[Windows executables](https://www.seasip.info/Unix/LibDsk/libdsk1522.zip).

## Features

LibDsk provides a common interface to multiple disk image formats and
platform-dependent floppy drivers. Supported formats include:

- Raw and logical-order raw images.
- CPCEMU DSK and extended DSK images.
- MYZ80, NanoWasp and CFI images.
- ImageDisk, CopyQM and TeleDisk images.
- ApriDisk, QRST, JV3, D88, D64 and SAP images.
- LDBS and its text representation, LDBST.

The `rcpmfs` backend presents a host directory as a CP/M disk. LibDsk also
supports remote access to another LibDsk instance, including serial-line
transport. See the manual for backend-specific capabilities and limitations.

Utilities include `dskid`, `dskdump`, `dskform`, `dsktrans`, `dskconv`,
`dskscan`, `dskutil`, `dsklabel` and `dskparse`. Their documentation is in
the [manual pages](man/).

## Building

### Linux and Unix

The bundled [installation instructions](doc/INSTALL) describe the standard
build sequence:

```sh
./configure
make
make install
```

Install manual pages separately with `make install-man`.

Documented configuration options include:

- `--disable-floppy`: omit native floppy-driver support.
- `--without-zlib`: omit on-the-fly gzip decompression.
- `--without-bzlib`: omit on-the-fly bzip2 decompression.
- `--with-jni`: include Java Native Interface bindings.

Upstream notes that physical floppy access on newer Linux kernels depends on
the availability of `FDRAWCMD`. This restriction concerns the native floppy
driver, not ordinary disk image files.

### Windows

The installation instructions describe builds with Borland C++ and Visual
C++, using the supplied workspaces and platform-specific `config.h` files.
The [win32vc6](win32vc6/) directory contains the Visual C++ 6 project and
workspace files. Consult the installation instructions for the other supplied
platform builds.

## Native Library Builds

See the [native build guide](doc/native-builds.md) for the Windows x86 CMake
library target, out-of-tree Linux builds and isolated library regression checks.

## Documentation

- [Library manual](doc/libdsk.txt): API, formats, filesystem backends and limitations.
- [Installation instructions](doc/INSTALL): build and configuration options.
- [Remote protocol](doc/protocol.txt): LibDsk RPC packet formats.
- [ChangeLog](ChangeLog): release history.
- [Upstream website](https://www.seasip.info/Unix/LibDsk/): downloads, supported disk types and notices.

## License

See [doc/COPYING](doc/COPYING) for the GNU Library General Public License
version 2. The source license notices permit version 2 or, at your option,
any later version. The library is distributed without warranty.