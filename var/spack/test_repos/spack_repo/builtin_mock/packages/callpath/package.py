# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Callpath(Package):
    homepage = "https://github.com/tgamblin/callpath"
    url = "http://github.com/tgamblin/callpath-1.0.tar.gz"

    version("0.8", md5="0123456789abcdef0123456789abcdef")
    version("0.9", md5="0123456789abcdef0123456789abcdef")
    version("1.0", md5="0123456789abcdef0123456789abcdef")

    depends_on("c", type="build")

    depends_on("dyninst")
    depends_on("mpi")

    def install(self, spec, prefix):
        mkdirp(prefix)
        touch(join_path(prefix, "dummyfile"))

    def setup_run_environment(self, env: EnvironmentModifications) -> None:
        env.set("FOOBAR", self.name)
