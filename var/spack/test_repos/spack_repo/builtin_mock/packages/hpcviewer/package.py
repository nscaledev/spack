# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.autotools import AutotoolsPackage

from spack.package import *


class Hpcviewer(AutotoolsPackage):
    """Uses version-test-pkg, as a build dependency"""

    homepage = "http://www.spack.org"
    url = "http://www.spack.org/downloads/aml-1.0.tar.gz"

    version("2019.02", md5="0123456789abcdef0123456789abcdef")

    depends_on("java@11:", type=("build", "run"), when="@2021.0:")
    depends_on("java@8", type=("build", "run"), when="@:2020")
