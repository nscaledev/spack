# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import glob
import os
import re

from _vendoring.macholib import MachO, mach_o
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *
from spack.util.elf import delete_needed_from_elf, parse_elf


class GccRuntime(Package):
    """Package for GCC compiler runtime libraries"""

    homepage = "https://gcc.gnu.org"
    has_code = False

    tags = ["runtime"]

    # gcc-runtime versions are declared dynamically
    skip_version_audit = ["platform=linux", "platform=darwin", "platform=windows"]

    maintainers("haampie")

    license("GPL-3.0-or-later WITH GCC-exception-3.1")

    LIBRARIES = [
        "asan",
        "atomic",
        "gcc_s",
        "gfortran",
        "gomp",
        "hwasan",
        "itm",
        "lsan",
        "quadmath",
        "ssp",
        "stdc++",
        "tsan",
        "ubsan",
    ]

    # libgfortran ABI
    provides("fortran-rt", "libgfortran")
    provides("libgfortran@3", when="@:6")
    provides("libgfortran@4", when="@7")
    provides("libgfortran@5", when="@8:")

    depends_on("libc", type="link", when="platform=linux")

    depends_on("gcc", type="build")

    def install(self, spec, prefix):
        gcc_pkg = self["gcc"]
        if spec.platform in ["linux", "freebsd"]:
            libraries = get_elf_libraries(compiler=gcc_pkg, libraries=self.LIBRARIES)
        elif spec.platform == "darwin":
            libraries = self._get_libraries_macho()
        else:
            raise RuntimeError("Unsupported platform")

        mkdir(prefix.lib)

        if not libraries:
            tty.warn("Could not detect any shared GCC runtime libraries")
            return

        for path, name in libraries:
            install(path, os.path.join(prefix.lib, name))

        if spec.platform in ("linux", "freebsd"):
            _drop_libgfortran_zlib(prefix.lib)

    def _get_libraries_macho(self):
        """Same as _get_libraries_elf but for Mach-O binaries"""
        cc = self._get_compiler()
        path_and_install_name = []
        for name in self.LIBRARIES:
            if name == "gcc_s":
                # On darwin, libgcc_s is versioned and can't be linked as -lgcc_s,
                # but needs a suffix we don't know, so we parse it from the link line.
                match = re.search(
                    r"\s-l(gcc_s\.[0-9.]+)\s", cc("-xc", "-", "-shared-libgcc", "-###", error=str)
                )
                if match is None:
                    continue
                name = match.group(1)

            path = cc(f"-print-file-name=lib{name}.dylib", output=str).strip()

            if not os.path.isabs(path):
                continue

            macho = MachO.MachO(path)

            # Get the LC_ID_DYLIB load command
            for load_command, _, data in macho.headers[-1].commands:
                if load_command.cmd == mach_o.LC_ID_DYLIB:
                    # Strip off @rpath/ prefix, or even an absolute path.
                    dylib_name = os.path.basename(data.rstrip(b"\x00").decode())
                    break
            else:
                continue

            # Locate by dylib name
            runtime_path = cc(f"-print-file-name={dylib_name}", output=str).strip()

            if not os.path.isabs(runtime_path):
                continue

            path_and_install_name.append((runtime_path, dylib_name))

        return path_and_install_name

    def _get_compiler(self):
        gcc_pkg = self["gcc"]
        exe_path = None
        for attr_name in ("cc", "cxx", "fortran"):
            try:
                exe_path = getattr(gcc_pkg, attr_name)
            except AttributeError:
                pass

            if not exe_path:
                continue
            cc = Executable(exe_path)
            break
        else:
            raise InstallError(f"cannot find any compiler for {gcc_pkg.spec}")
        return cc

    @property
    def libs(self):
        # Currently these libs are not linkable with -l, they all have a suffix.
        return LibraryList([])

    @property
    def headers(self):
        return HeaderList([])


def _drop_libgfortran_zlib(lib_dir: str) -> None:
    """Due to a bug in GCC's autotools setup (https://gcc.gnu.org/bugzilla/show_bug.cgi?id=87182),
    libz sometimes appears as a redundant system dependency of libgfortran. Delete it."""
    libraries = glob.glob(os.path.join(lib_dir, "libgfortran*.so*"))
    if len(libraries) == 0:
        return
    with open(libraries[0], "rb+") as f:
        elf = parse_elf(f, dynamic_section=True)
        if not elf.has_needed:
            return
        libz = next((x for x in elf.dt_needed_strs if x.startswith(b"libz.so")), None)
        if libz is None:
            return
        delete_needed_from_elf(f, elf, libz)


def get_elf_libraries(compiler, libraries):
    """Get the GCC runtime libraries for ELF binaries"""
    cc = Executable(compiler.cc)
    lib_regex = re.compile(rb"\blib[a-z-_]+\.so\.\d+\b")
    path_and_install_name = []

    for name in libraries:
        # Look for the dynamic library that gcc would use to link,
        # that is with .so extension and without abi suffix.
        path = cc(f"-print-file-name=lib{name}.so", output=str).strip()

        # gcc reports an absolute path on success
        if not os.path.isabs(path):
            continue

        # Now there are two options:
        # 1. the file is an ELF file
        # 2. the file is a linker script referencing the actual library
        with open(path, "rb") as f:
            try:
                # Try to parse as an ELF file
                soname = parse_elf(f, dynamic_section=True).dt_soname_str.decode("utf-8")
            except Exception:
                # On failure try to "parse" as ld script; the actual
                # library needs to be mentioned by filename.
                f.seek(0)
                script_matches = lib_regex.findall(f.read())
                if len(script_matches) != 1:
                    continue
                soname = script_matches[0].decode("utf-8")

        # Now locate and install the runtime library
        runtime_path = cc(f"-print-file-name={soname}", output=str).strip()

        if not os.path.isabs(runtime_path):
            continue

        path_and_install_name.append((runtime_path, soname))

    return path_and_install_name
