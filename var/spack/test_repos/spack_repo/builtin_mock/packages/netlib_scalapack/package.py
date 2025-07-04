# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)


from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class NetlibScalapack(Package):
    homepage = "http://www.netlib.org/scalapack/"
    url = "http://www.netlib.org/scalapack/scalapack-2.1.0.tgz"

    version("2.1.0", "b1d3e3e425b2e44a06760ff173104bdf")

    provides("scalapack")

    depends_on("mpi")
    depends_on("lapack")
    depends_on("blas")
