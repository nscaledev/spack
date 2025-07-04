# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class NoRedistribute(Package):
    """Package which has source code that should not be added to a public
    mirror"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/no-redistribute-1.0.tar.gz"

    redistribute(source=False, binary=False)

    version("1.0", "0123456789abcdef0123456789abcdef")

    def install(self, spec, prefix):
        # sanity_check_prefix requires something in the install directory
        # Test requires overriding the one provided by `AutotoolsPackage`
        mkdirp(prefix.bin)
