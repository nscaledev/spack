# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class TestDepWithImposedConditions(Package):
    """Simple package with no dependencies"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/e-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    depends_on("pkg-c@1.0", type="test")
