# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class PyFloating(Package):
    """An extension that depends on:
    - py-setuptools without further constraints
    - py-shapely, which depends on py-setuptools@=60
    - py-numpy, which depends on py-setuptools@=59

    We need to ensure that by default the root node gets the best version
    of setuptools it could.
    """

    homepage = "http://www.example.com"
    url = "http://www.example.com/tdep-1.0.tar.gz"

    version("1.25.0", md5="0123456789abcdef0123456789abcdef")

    extends("python")
    depends_on("py-numpy", type=("build", "run"))
    depends_on("py-shapely", type=("build", "run"))
    depends_on("py-setuptools", type="build")
