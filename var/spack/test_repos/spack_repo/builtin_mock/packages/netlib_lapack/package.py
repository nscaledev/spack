# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class NetlibLapack(Package):
    homepage = "http://www.netlib.org/lapack/"
    url = "http://www.netlib.org/lapack/lapack-3.5.0.tgz"

    version("3.5.0", md5="b1d3e3e425b2e44a06760ff173104bdf")

    provides("lapack")
    depends_on("blas")
