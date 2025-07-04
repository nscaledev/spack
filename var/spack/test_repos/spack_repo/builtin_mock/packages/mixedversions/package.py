# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Mixedversions(Package):
    url = "http://www.fake-mixedversions.org/downloads/mixedversions-1.0.tar.gz"

    version("2.0.1", md5="0000000000000000000000000000000c")
    version("2.0", md5="0000000000000000000000000000000b")
    version("1.0.1", md5="0000000000000000000000000000000a")
