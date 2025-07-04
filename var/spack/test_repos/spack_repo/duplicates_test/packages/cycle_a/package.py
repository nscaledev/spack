# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class CycleA(Package):
    """Package that would lead to cycles if default variant values are used"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/tdep-1.0.tar.gz"

    version("2.0", md5="0123456789abcdef0123456789abcdef")

    variant("cycle", default=True, description="activate cycles")
    depends_on("cycle-b", when="+cycle")
