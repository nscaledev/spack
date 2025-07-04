# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)


from spack_repo.builtin_mock.build_systems.makefile import MakefilePackage

from spack.package import *


class DevBuildTestDependent(MakefilePackage):
    homepage = "example.com"
    url = "fake.com"

    version("0.0.0", sha256="0123456789abcdef0123456789abcdef")

    filename = "dev-build-test-file.txt"
    original_string = "This file should be edited"
    replacement_string = "This file has been edited"

    depends_on("dev-build-test-install")

    def edit(self, spec, prefix):
        with open(self.filename, "r+", encoding="utf-8") as f:
            assert f.read() == self.original_string
            f.seek(0)
            f.truncate()
            f.write(self.replacement_string)

    def build(self, spec, prefix):
        pass

    def install(self, spec, prefix):
        install(self.filename, prefix)
