# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from spack.package import *

from ..multimethod.package import Multimethod


class MultimethodInheritor(Multimethod):
    """This package is designed for use with Spack's multimethod test.
    It has a bunch of test cases for the @when decorator that the
    test uses.
    """

    @when("@1.0")
    def inherited_and_overridden(self):
        return "inheritor@1.0"

    #
    # Test multi-level inheritance
    #
    @when("@2:")
    def base_method(self):
        return "multimethod-inheritor"
