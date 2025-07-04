# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class Extension2(Package):
    """A package which extends another package. It also depends on another
    package which extends the same package."""

    homepage = "http://www.example.com"
    url = "http://www.example.com/extension2-1.0.tar.gz"

    extends("extendee")
    depends_on("extension1", type=("build", "run"))

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    def install(self, spec, prefix):
        mkdirp(prefix.bin)
        with open(os.path.join(prefix.bin, "extension2"), "w+", encoding="utf-8") as fout:
            fout.write(str(spec.version))
