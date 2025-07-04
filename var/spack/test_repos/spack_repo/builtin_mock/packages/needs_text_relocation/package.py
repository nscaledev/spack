# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class NeedsTextRelocation(Package):
    """A dumy package that encodes its prefix."""

    homepage = "https://www.cmake.org"
    url = "https://cmake.org/files/v3.4/cmake-3.4.3.tar.gz"

    version("0.0.0", md5="12345678qwertyuiasdfghjkzxcvbnm0")

    def install(self, spec, prefix):
        mkdirp(prefix.bin)

        exe = join_path(prefix.bin, "exe")
        with open(exe, "w", encoding="utf-8") as f:
            f.write(prefix)
        set_executable(exe)

        otherexe = join_path(prefix.bin, "otherexe")
        with open(otherexe, "w", encoding="utf-8") as f:
            f.write("Lorem Ipsum")
        set_executable(otherexe)
