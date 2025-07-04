# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class OptionalDepTest2(Package):
    """Depends on the optional-dep-test package"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/optional-dep-test-2-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    variant("odt", default=False)
    variant("mpi", default=False)

    depends_on("optional-dep-test", when="+odt")
    depends_on("optional-dep-test+mpi", when="+mpi")
