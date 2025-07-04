# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class DevBuildTestInstallPhases(Package):
    homepage = "example.com"
    url = "fake.com"

    version("0.0.0", sha256="0123456789abcdef0123456789abcdef")

    phases = ["one", "two", "three", "install"]

    def one(self, spec, prefix):
        print("One locomoco")

    def two(self, spec, prefix):
        print("Two locomoco")

    def three(self, spec, prefix):
        print("Three locomoco")

    def install(self, spec, prefix):
        mkdirp(prefix.bin)
        print("install")
