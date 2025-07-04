# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class UnconstrainableConflict(Package):
    """Package with a conflict whose trigger cannot constrain its constraint."""

    homepage = "http://www.realurl.com"
    url = "http://www.realurl.com/unconstrainable-conflict-1.0.tar.gz"

    version("1.0", sha256="2e34cc4505556d1c1f085758e26f2f8eea0972db9382f051b2dcfb1d7d9e1825")

    # Two conflicts so there's always one that is not the current platform
    conflicts("target=x86_64", when="platform=darwin")
    conflicts("target=aarch64", when="platform=linux")
