# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DevelopTest(Package):
    """Dummy package with develop version"""

    homepage = "http://www.openblas.net"
    url = "http://github.com/xianyi/OpenBLAS/archive/v0.2.15.tar.gz"

    version("develop", git="https://github.com/dummy/repo.git")
    version("0.2.15", md5="b1190f3d3471685f17cfd1ec1d252ac9")
