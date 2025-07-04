# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DevelopBranchVersion(Package):
    """Dummy package with develop version"""

    homepage = "http://www.openblas.net"
    url = "http://github.com/xianyi/OpenBLAS/archive/v0.2.15.tar.gz"
    git = "https://github.com/dummy/repo.git"

    version("develop", branch="develop")
    version("0.2.15", md5="b1190f3d3471685f17cfd1ec1d252ac9")
