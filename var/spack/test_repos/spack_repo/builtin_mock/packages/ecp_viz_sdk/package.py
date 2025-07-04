# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class EcpVizSdk(Package):
    """Package that has a dependency with a variant which
    adds a transitive dependency forced to use non default
    values.
    """

    homepage = "https://dev.null"

    version("1.0")

    depends_on("conditional-constrained-dependencies")
