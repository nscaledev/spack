# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse

from spack.cmd.common import arguments
from spack.cmd.compiler import compiler_list

description = "list available compilers"
section = "system"
level = "short"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--scope", action=arguments.ConfigScope, help="configuration scope to read/modify"
    )
    subparser.add_argument(
        "--remote", action="store_true", help="list also compilers from registered buildcaches"
    )


def compilers(parser, args):
    compiler_list(args)
