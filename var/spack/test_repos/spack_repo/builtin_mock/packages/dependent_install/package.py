# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DependentInstall(Package):
    """Dependent which has a working install method"""

    homepage = "http://www.example.com"
    url = "http://www.example.com/a-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")
    version("2.0", md5="0123456789abcdef0123456789abcdef")

    depends_on("dependency-install@2.0", when="@2.0")
    depends_on("dependency-install@1.0", when="@1.0")

    def install(self, spec, prefix):
        touch(join_path(prefix, "an_installation_file"))
