# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class InstalledDepsA(Package):
    """Used by test_installed_deps test case."""

    #     a
    #    / \
    #   b   c   b --> d build/link
    #   |\ /|   b --> e build/link
    #   |/ \|   c --> d build
    #   d   e   c --> e build/link

    homepage = "http://www.example.com"
    url = "http://www.example.com/a-1.0.tar.gz"

    version("1", md5="0123456789abcdef0123456789abcdef")
    version("2", md5="abcdef0123456789abcdef0123456789")
    version("3", md5="def0123456789abcdef0123456789abc")

    depends_on("installed-deps-b", type=("build", "link"))
    depends_on("installed-deps-c", type=("build", "link"))
