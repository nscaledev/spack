# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Zmpi(Package):
    """This is a fake MPI package used to demonstrate virtual package providers
    with dependencies."""

    homepage = "http://www.spack-fake-zmpi.org"
    url = "http://www.spack-fake-zmpi.org/downloads/zmpi-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    provides("mpi@:10.0")

    depends_on("fake")
    depends_on("c", type="build")
