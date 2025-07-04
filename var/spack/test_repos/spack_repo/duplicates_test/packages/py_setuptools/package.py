# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class PySetuptools(Package):
    """Build tool for an extendable package"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/tdep-1.0.tar.gz"

    tags = ["build-tools"]

    extends("python")

    version("60", md5="0123456789abcdef0123456789abcdef")
    version("59", md5="0123456789abcdef0123456789abcdef")
