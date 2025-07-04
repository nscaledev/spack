# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)


from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class SinglevalueVariant(Package):
    homepage = "http://www.llnl.gov"
    url = "http://www.llnl.gov/mpileaks-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    variant(
        "fum",
        description="Single-valued variant with type in values",
        default="bar",
        values=str,
        multi=False,
    )
