# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Extendee(Package):
    """A package with extensions"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/extendee-1.0.tar.gz"

    extendable = True

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    def install(self, spec, prefix):
        mkdirp(prefix.bin)
