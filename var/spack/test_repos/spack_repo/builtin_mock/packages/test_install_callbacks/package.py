# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems import _checks as checks
from spack_repo.builtin_mock.build_systems import generic

from spack.package import *


class TestInstallCallbacks(generic.Package):
    """This package illustrates install callback test failure."""

    homepage = "http://www.example.com/test-install-callbacks"
    url = "http://www.test-failure.test/test-install-callbacks-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")


class GenericBuilder(generic.GenericBuilder):
    # Include an undefined callback method
    install_time_test_callbacks = ["undefined-install-test"]
    run_after("install")(checks.execute_install_time_tests)

    def install(self, pkg, spec, prefix):
        mkdirp(prefix.bin)
