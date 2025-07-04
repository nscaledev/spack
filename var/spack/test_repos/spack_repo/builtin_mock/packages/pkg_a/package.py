# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems import autotools

from spack.package import *


class PkgA(autotools.AutotoolsPackage):
    """Simple package with one optional dependency"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/a-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")
    version("2.0", md5="abcdef0123456789abcdef0123456789")

    variant(
        "foo", description="", values=any_combination_of("bar", "baz", "fee").with_default("bar")
    )

    variant("foobar", values=("bar", "baz", "fee"), default="bar", description="", multi=False)

    variant("lorem_ipsum", description="", default=False)

    variant("bvv", default=True, description="The good old BV variant")

    variant(
        "libs",
        default="shared",
        values=("shared", "static"),
        multi=True,
        description="Type of libraries to install",
    )

    depends_on("pkg-b", when="foobar=bar")
    depends_on("test-dependency", type="test")

    depends_on("c", type="build")

    parallel = False


class AutotoolsBuilder(autotools.AutotoolsBuilder):
    def with_or_without_fee(self, activated):
        if not activated:
            return "--no-fee"
        return "--fee-all-the-time"

    def autoreconf(self, pkg, spec, prefix):
        pass

    def configure(self, pkg, spec, prefix):
        pass

    def build(self, pkg, spec, prefix):
        pass

    def install(self, pkg, spec, prefix):
        # sanity_check_prefix requires something in the install directory
        # Test requires overriding the one provided by `AutotoolsPackage`
        mkdirp(prefix.bin)
