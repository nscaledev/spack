# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse

import llnl.util.tty as tty

import spack.cmd
from spack.cmd.common import arguments

description = "remove specs from an environment"
section = "environments"
level = "long"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "-a", "--all", action="store_true", help="remove all specs from (clear) the environment"
    )
    subparser.add_argument(
        "-l",
        "--list-name",
        dest="list_name",
        default="specs",
        help="name of the list to remove specs from",
    )
    subparser.add_argument(
        "-f", "--force", action="store_true", help="remove concretized spec (if any) immediately"
    )
    arguments.add_common_arguments(subparser, ["specs"])


def remove(parser, args):
    env = spack.cmd.require_active_env(cmd_name="remove")

    with env.write_transaction():
        if args.all:
            env.clear()
        else:
            for spec in spack.cmd.parse_specs(args.specs):
                env.remove(spec, args.list_name, force=args.force)
                tty.msg(f"{spec} has been removed from {env.manifest}")
        env.write()
