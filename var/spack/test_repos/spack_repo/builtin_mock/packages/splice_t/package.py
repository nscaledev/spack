# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class SpliceT(Package):
    """Simple package with one optional dependency"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/splice-t-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    depends_on("splice-h")
    depends_on("splice-z")

    def install(self, spec, prefix):
        with open(prefix.join("splice-t"), "w", encoding="utf-8") as f:
            f.write("splice-t: {0}".format(prefix))
            f.write("splice-h: {0}".format(spec["splice-h"].prefix))
            f.write("splice-z: {0}".format(spec["splice-z"].prefix))
