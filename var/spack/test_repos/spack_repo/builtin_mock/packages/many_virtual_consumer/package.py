# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ManyVirtualConsumer(Package):
    """PAckage that depends on many virtual packages"""

    url = "http://www.example.com/"
    url = "http://www.example.com/2.0.tar.gz"

    version("1.0", md5="abcdef1234567890abcdef1234567890")

    depends_on("mpi")
    depends_on("lapack")

    # This directive is an example of imposing a constraint on a
    # dependency is that dependency is in the DAG. This pattern
    # is mainly used with virtual providers.
    depends_on("low-priority-provider@1.0", when="^[virtuals=mpi,lapack] low-priority-provider")
