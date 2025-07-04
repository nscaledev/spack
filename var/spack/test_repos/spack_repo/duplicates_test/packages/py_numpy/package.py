# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class PyNumpy(Package):
    """An extension that depends on pinned build dependencies"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/tdep-1.0.tar.gz"

    tags = ["build-tools"]

    version("1.25.0", md5="0123456789abcdef0123456789abcdef")

    extends("python")
    depends_on("py-setuptools@=59", type=("build", "run"))
    depends_on("gmake@4.1", type="build")
