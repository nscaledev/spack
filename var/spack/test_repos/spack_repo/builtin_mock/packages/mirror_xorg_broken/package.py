# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.autotools import AutotoolsPackage
from spack_repo.builtin_mock.build_systems.xorg import XorgPackage

from spack.package import *


class MirrorXorgBroken(AutotoolsPackage, XorgPackage):
    """Simple x.org package"""

    homepage = "http://cgit.freedesktop.org/xorg/util/macros/"
    url = "https://www.x.org/archive/individual/util/util-macros-1.19.1.tar.bz2"

    version("1.19.1", sha256="18d459400558f4ea99527bc9786c033965a3db45bf4c6a32eefdc07aa9e306a6")
