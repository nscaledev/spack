# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class SpliceVh(Package):
    """Simple package with one optional dependency"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/splice-vh-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    variant("foo", default=False, description="nope")
    variant("bar", default=False, description="nope")
    variant("baz", default=False, description="nope")

    depends_on("splice-z")
    depends_on("splice-z+foo", when="+foo")

    provides("something")

    def install(self, spec, prefix):
        with open(prefix.join("splice-vh"), "w", encoding="utf-8") as f:
            f.write("splice-vh: {0}".format(prefix))
            f.write("splice-z: {0}".format(spec["splice-z"].prefix))
