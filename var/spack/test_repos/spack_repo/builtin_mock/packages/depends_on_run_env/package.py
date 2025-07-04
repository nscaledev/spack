# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DependsOnRunEnv(Package):
    """This package has a runtime dependency on another package which needs
    to perform shell modifications to run.
    """

    homepage = "http://www.example.com"
    url = "http://www.example.com/a-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    depends_on("modifies-run-env", type=("run",))

    def install(self, spec, prefix):
        mkdirp(prefix.bin)
