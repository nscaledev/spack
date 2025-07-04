# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Extension1(Package):
    """A package which extends another package"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/extension1-1.0.tar.gz"

    extends("extendee")

    version("1.0", md5="0123456789abcdef0123456789abcdef")
    version("2.0", md5="abcdef0123456789abcdef0123456789")

    def install(self, spec, prefix):
        mkdirp(prefix.bin)
        with open(os.path.join(prefix.bin, "extension1"), "w+", encoding="utf-8") as fout:
            fout.write(str(spec.version))
