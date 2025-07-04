# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class T(Package):
    version("2.1")
    version("2.0")

    depends_on("u", when="@2.1:")

    depends_on("c", type="build")
