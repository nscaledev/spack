# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.autotools import AutotoolsPackage

from spack.package import *


class VersionTestRoot(AutotoolsPackage):
    """Uses version-test-pkg, as a build dependency"""

    homepage = "http://www.spack.org"
    url = "http://www.spack.org/downloads/aml-1.0.tar.gz"

    version("0.1.0", sha256="cc89a8768693f1f11539378b21cdca9f0ce3fc5cb564f9b3e4154a051dcea69b")
    depends_on("version-test-pkg", type="build")
