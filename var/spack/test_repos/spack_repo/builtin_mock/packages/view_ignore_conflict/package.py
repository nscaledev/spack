# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import os

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class ViewIgnoreConflict(Package):
    """Installs a file in <prefix>/bin/x, conflicting with the file <dep>/bin/x in a view. In
    a view, we should find this package's file, not the dependency's file."""

    has_code = False

    version("0.1.0")
    depends_on("view-file")

    def install(self, spec, prefix):
        os.mkdir(os.path.join(prefix, "bin"))
        with open(os.path.join(prefix, "bin", "x"), "wb") as f:
            f.write(b"file")
