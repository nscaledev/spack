# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Bowtie(Package):
    """Mock package to test conflicts on compiler ranges"""

    homepage = "http://www.example.org"
    url = "http://bowtie-1.2.2.tar.bz2"

    version("1.4.0", md5="1c837ecd990bb022d07e7aab32b09847")
    version("1.3.0", md5="1c837ecd990bb022d07e7aab32b09847")
    version("1.2.2", md5="1c837ecd990bb022d07e7aab32b09847")
    version("1.2.0", md5="1c837ecd990bb022d07e7aab32b09847")

    depends_on("c", type="build")

    conflicts("%gcc@:4.5.0", when="@1.2.2")
    conflicts("%gcc@:10.2.1", when="@:1.2.9")
    conflicts("%gcc", when="@1.3")
