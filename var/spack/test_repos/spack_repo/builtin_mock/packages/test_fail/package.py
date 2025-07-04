# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class TestFail(Package):
    """This package has a test method that fails in a subprocess."""

    homepage = "http://www.example.com/test-failure"
    url = "http://www.test-failure.test/test-failure-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    def install(self, spec, prefix):
        mkdirp(prefix.bin)

    def test_fails(self):
        """trigger test failure"""
        unknown = which("unknown-program")
        unknown()
