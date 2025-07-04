# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class HashTest2(Package):
    """Used to test package hashing"""

    homepage = "http://www.hashtest2.org"
    url = "http://www.hashtest1.org/downloads/hashtest2-1.1.tar.bz2"

    version("1.1", md5="a" * 32)
    version("1.2", md5="b" * 32)
    version("1.3", md5="c" * 31 + "x")  # Source hash differs from hash-test1@1.3
    version("1.4", md5="d" * 32)

    patch("patch1.patch", when="@1.1")

    variant("variantx", default=False, description="Test variant X")
    variant("varianty", default=False, description="Test variant Y")

    def setup_dependent_build_environment(
        self, env: EnvironmentModifications, dependent_spec: Spec
    ) -> None:
        pass

    def install(self, spec, prefix):
        print("install 1")
        os.listdir(os.getcwd())

        # sanity_check_prefix requires something in the install directory
        mkdirp(prefix.bin)
