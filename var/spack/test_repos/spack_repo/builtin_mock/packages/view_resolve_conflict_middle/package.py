# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ViewResolveConflictMiddle(Package):
    """See view-resolve-conflict-top"""

    has_code = False

    version("0.1.0")
    depends_on("view-file")

    def install(self, spec, prefix):
        bottom = spec["view-file"].prefix
        os.mkdir(os.path.join(prefix, "bin"))
        os.symlink(os.path.join(bottom, "bin", "x"), os.path.join(prefix, "bin", "x"))
        os.symlink(os.path.join(bottom, "bin", "x"), os.path.join(prefix, "bin", "y"))
