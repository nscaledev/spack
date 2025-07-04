# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DlaFuture(Package):
    """A package that depends on 3 different virtuals, that might or might not be provided
    by the same node.
    """

    homepage = "http://www.example.com"
    url = "http://www.example.com/dla-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    depends_on("blas")
    depends_on("lapack")
    depends_on("scalapack")
