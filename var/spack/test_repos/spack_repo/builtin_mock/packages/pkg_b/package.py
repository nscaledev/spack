# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class PkgB(Package):
    """Simple package with no dependencies"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/b-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")
    version("0.9", md5="abcd456789abcdef0123456789abcdef")

    variant(
        "foo", description="", values=any_combination_of("bar", "baz", "fee").with_default("bar")
    )

    depends_on("c", type="build")
    depends_on("test-dependency", type="test")
