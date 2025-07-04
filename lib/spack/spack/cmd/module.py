# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
from typing import Callable, Dict

import spack.cmd.modules.lmod
import spack.cmd.modules.tcl

description = "generate/manage module files"
section = "user environment"
level = "short"


_subcommands: Dict[str, Callable] = {}


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    sp = subparser.add_subparsers(metavar="SUBCOMMAND", dest="module_command")
    spack.cmd.modules.lmod.add_command(sp, _subcommands)
    spack.cmd.modules.tcl.add_command(sp, _subcommands)


def module(parser, args):
    _subcommands[args.module_command](parser, args)
