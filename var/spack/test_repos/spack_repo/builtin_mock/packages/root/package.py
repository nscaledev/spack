# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Root(Package):
    homepage = "http://www.example.com"
    url = "http://www.example.com/root-1.0.tar.gz"

    version("1.0", md5="abcdef0123456789abcdef0123456789")

    depends_on("gmt")
