# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ViewNotIgnored(Package):
    """Install files that should not be ignored by spack."""

    homepage = "http://www.spack.org"
    url = "http://www.spack.org/downloads/aml-1.0.tar.gz"
    has_code = False

    version("0.1.0")

    install_test_files = [
        "foo.spack",
        ".spack.bar",
        "aspack",
        "bin/foo.spack",
        "bin/.spack.bar",
        "bin/aspack",
    ]

    def install(self, spec, prefix):
        for test_file in self.install_test_files:
            path = os.path.join(prefix, test_file)
            mkdirp(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as f:
                f.write(test_file)

    @classmethod
    def assert_installed(cls, prefix):
        for test_file in cls.install_test_files:
            path = os.path.join(prefix, test_file)
            assert os.path.exists(path), "Missing installed file: {}".format(path)

    @classmethod
    def assert_not_installed(cls, prefix):
        for test_file in cls.install_test_files:
            path = os.path.join(prefix, test_file)
            assert not os.path.exists(path), "File was not uninstalled: {}".format(path)
