# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.autotools import AutotoolsPackage
from spack_repo.builtin_mock.build_systems.sourceware import SourcewarePackage

from spack.package import *


class MirrorSourceware(AutotoolsPackage, SourcewarePackage):
    """Simple sourceware.org package"""

    homepage = "https://sourceware.org/bzip2/"
    sourceware_mirror_path = "bzip2/bzip2-1.0.8.tar.gz"

    version("1.0.8", sha256="ab5a03176ee106d3f0fa90e381da478ddae405918153cca248e682cd0c4a2269")
