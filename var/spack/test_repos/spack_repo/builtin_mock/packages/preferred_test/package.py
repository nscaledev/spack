# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class PreferredTest(Package):
    """Dummy package with develop version and preferred version"""

    homepage = "https://github.com/LLNL/mpileaks"
    url = "https://github.com/LLNL/mpileaks/releases/download/v1.0/mpileaks-1.0.tar.gz"

    version("develop", git="https://github.com/LLNL/mpileaks.git")
    version(
        "1.0",
        sha256="abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        preferred=True,
    )
