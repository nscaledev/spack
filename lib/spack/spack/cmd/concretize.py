# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse

import llnl.util.tty as tty
from llnl.string import plural

import spack.cmd
import spack.cmd.common.arguments
import spack.environment as ev

description = "concretize an environment and write a lockfile"
section = "environments"
level = "long"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--test",
        default=None,
        choices=["root", "all"],
        help="concretize with test dependencies of only root packages or all packages",
    )
    subparser.add_argument(
        "-q", "--quiet", action="store_true", help="don't print concretized specs"
    )

    spack.cmd.common.arguments.add_concretizer_args(subparser)
    spack.cmd.common.arguments.add_common_arguments(subparser, ["jobs"])


def concretize(parser, args):
    env = spack.cmd.require_active_env(cmd_name="concretize")

    if args.test == "all":
        tests = True
    elif args.test == "root":
        tests = [spec.name for spec in env.user_specs]
    else:
        tests = False

    with env.write_transaction():
        concretized_specs = env.concretize(tests=tests)
        if not args.quiet:
            if concretized_specs:
                tty.msg(f"Concretized {plural(len(concretized_specs), 'spec')}:")
                ev.display_specs([concrete for _, concrete in concretized_specs])
            else:
                tty.msg("No new specs to concretize.")
        env.write()
