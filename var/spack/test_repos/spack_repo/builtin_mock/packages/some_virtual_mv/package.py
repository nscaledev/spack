# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class SomeVirtualMv(Package):
    """Package providing a virtual dependency and with a multivalued variant."""

    homepage = "http://www.example.com"
    url = "http://www.example.com/foo-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    provides("somevirtual")

    # This multi valued variant is needed to trigger an optimization
    # criteria for clingo
    variant(
        "libs",
        default="shared,static",
        values=("shared", "static"),
        multi=True,
        description="Build shared libs, static libs or both",
    )
