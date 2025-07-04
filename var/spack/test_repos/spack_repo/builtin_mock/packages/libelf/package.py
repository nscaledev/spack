# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Libelf(Package):
    homepage = "http://www.mr511.de/software/english.html"
    url = "http://www.mr511.de/software/libelf-0.8.13.tar.gz"

    version("0.8.13", md5="4136d7b4c04df68b686570afa26988ac")
    version("0.8.12", md5="e21f8273d9f5f6d43a59878dc274fec7")
    version("0.8.10", md5="9db4d36c283d9790d8fa7df1f4d7b4d9")

    patch("local.patch", when="@0.8.10")

    depends_on("c", type="build")

    def install(self, spec, prefix):
        touch(prefix.libelf)
