# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack_repo.builtin_mock.build_systems.generic import Package

from spack.package import *


class OverrideContextTemplates(Package):
    """This package updates the context for Tcl modulefiles.

    And additional lines that shouldn't be in the short description.
    """

    homepage = "http://www.fake-spack-example.org"
    url = "http://www.fake-spack-example.org/downloads/fake-1.0.tar.gz"

    version("1.0", md5="0123456789abcdef0123456789abcdef")

    tcl_template = "extension.tcl"
    tcl_context = {"sentence": "sentence from package"}
