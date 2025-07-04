#!/bin/sh -f
# shellcheck disable=SC2034  # evals in this script fool shellcheck
#
# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

#
# Spack compiler wrapper script.
#
# Compiler commands go through this compiler wrapper in Spack builds.
# The compiler wrapper is a thin layer around the standard compilers.
# It enables several key pieces of functionality:
#
# 1. It allows Spack to swap compilers into and out of builds easily.
# 2. It adds several options to the compile line so that spack
#    packages can find their dependencies at build time and run time:
#      -I and/or -isystem arguments for dependency /include directories.
#      -L                 arguments for dependency /lib directories.
#      -Wl,-rpath         arguments for dependency /lib directories.
#

# Reset IFS to the default: whitespace-separated lists. When we use
# other separators, we set and reset it.
unset IFS

# Separator for lists whose names end with `_list`.
# We pick the alarm bell character, which is highly unlikely to
# conflict with anything. This is a literal bell character (which
# we have to use since POSIX sh does not convert escape sequences
# like '\a' outside of the format argument of `printf`).
# NOTE: Depending on your editor this may look empty, but it is not.
readonly lsep=''

# This is an array of environment variables that need to be set before
# the script runs. They are set by routines in spack.build_environment
# as part of the package installation process.
readonly params="\
SPACK_COMPILER_WRAPPER_PATH
SPACK_DEBUG_LOG_DIR
SPACK_DEBUG_LOG_ID
SPACK_SHORT_SPEC
SPACK_SYSTEM_DIRS
SPACK_MANAGED_DIRS"

# Optional parameters that aren't required to be set

# Boolean (true/false/custom) if we want to add debug flags
# SPACK_ADD_DEBUG_FLAGS

# If a custom flag is requested, it will be defined
# SPACK_DEBUG_FLAGS

# The compiler input variables are checked for sanity later:
#   SPACK_CC, SPACK_CXX, SPACK_F77, SPACK_FC
# The default compiler flags are passed from these variables:
#   SPACK_CFLAGS, SPACK_CXXFLAGS, SPACK_FFLAGS,
#   SPACK_LDFLAGS, SPACK_LDLIBS
# Debug env var is optional; set to "TRUE" for debug logging:
#   SPACK_DEBUG
# Test command is used to unit test the compiler script.
#   SPACK_TEST_COMMAND

# die MESSAGE
# Print a message and exit with error code 1.
die() {
    echo "[spack cc] ERROR: $*"
    exit 1
}

# empty VARNAME
# Return whether the variable VARNAME is unset or set to the empty string.
empty() {
    eval "test -z \"\${$1}\""
}

# setsep LISTNAME
# Set the global variable 'sep' to the separator for a list with name LISTNAME.
# There are three types of lists:
#   1. regular lists end with _list and are separated by $lsep
#   2. directory lists end with _dirs/_DIRS/PATH(S) and are separated by ':'
#   3. any other list is assumed to be separated by spaces: " "
setsep() {
    case "$1" in
        *_dirs|*_DIRS|*PATH|*PATHS)
            sep=':'
            ;;
        *_list)
            sep="$lsep"
            ;;
        *)
            sep=" "
            ;;
    esac
}

# prepend LISTNAME ELEMENT
#
# Prepend ELEMENT to the list stored in the variable LISTNAME.
# Handles empty lists and single-element lists.
prepend() {
    varname="$1"
    elt="$2"

    if empty "$varname"; then
        eval "$varname=\"\${elt}\""
    else
        # Get the appropriate separator for the list we're appending to.
        setsep "$varname"
        eval "$varname=\"\${elt}${sep}\${$varname}\""
    fi
}

# append LISTNAME ELEMENT [SEP]
#
# Append ELEMENT to the list stored in the variable LISTNAME,
# assuming the list is separated by SEP.
# Handles empty lists and single-element lists.
append() {
    varname="$1"
    elt="$2"

    if empty "$varname"; then
        eval "$varname=\"\${elt}\""
    else
        # Get the appropriate separator for the list we're appending to.
        setsep "$varname"
        eval "$varname=\"\${$varname}${sep}\${elt}\""
    fi
}

# extend LISTNAME1 LISTNAME2 [PREFIX]
#
# Append the elements stored in the variable LISTNAME2
# to the list stored in LISTNAME1.
# If PREFIX is provided, prepend it to each element.
extend() {
    # Figure out the appropriate IFS for the list we're reading.
    setsep "$2"
    if [ "$sep" != " " ]; then
        IFS="$sep"
    fi
    eval "for elt in \${$2}; do append $1 \"$3\${elt}\"; done"
    unset IFS
}

# preextend LISTNAME1 LISTNAME2 [PREFIX]
#
# Prepend the elements stored in the list at LISTNAME2
# to the list at LISTNAME1, preserving order.
# If PREFIX is provided, prepend it to each element.
preextend() {
    # Figure out the appropriate IFS for the list we're reading.
    setsep "$2"
    if [ "$sep" != " " ]; then
        IFS="$sep"
    fi

    # first, reverse the list to prepend
    _reversed_list=""
    eval "for elt in \${$2}; do prepend _reversed_list \"$3\${elt}\"; done"

    # prepend reversed list to preextend in order
    IFS="${lsep}"
    for elt in $_reversed_list; do prepend "$1" "$3${elt}"; done
    unset IFS
}

execute() {
    # dump the full command if the caller supplies SPACK_TEST_COMMAND=dump-args
    if [ -n "${SPACK_TEST_COMMAND=}" ]; then
        case "$SPACK_TEST_COMMAND" in
            dump-args)
                IFS="$lsep"
                for arg in $full_command_list; do
                    echo "$arg"
                done
                unset IFS
                exit
                ;;
            dump-env-*)
                var=${SPACK_TEST_COMMAND#dump-env-}
                eval "printf '%s\n' \"\$0: \$var: \$$var\""
                ;;
            *)
                die "Unknown test command: '$SPACK_TEST_COMMAND'"
                ;;
        esac
    fi

    #
    # Write the input and output commands to debug logs if it's asked for.
    #
    if [ "$SPACK_DEBUG" = TRUE ]; then
        input_log="$SPACK_DEBUG_LOG_DIR/spack-cc-$SPACK_DEBUG_LOG_ID.in.log"
        output_log="$SPACK_DEBUG_LOG_DIR/spack-cc-$SPACK_DEBUG_LOG_ID.out.log"
        echo "[$mode] $command $input_command" >> "$input_log"
        IFS="$lsep"
        echo "[$mode] "$full_command_list >> "$output_log"
        unset IFS
    fi

    # Execute the full command, preserving spaces with IFS set
    # to the alarm bell separator.
    IFS="$lsep"; exec $full_command_list
    exit
}

# Fail with a clear message if the input contains any bell characters.
if eval "[ \"\${*#*${lsep}}\" != \"\$*\" ]"; then
    die "Compiler command line contains our separator ('${lsep}'). Cannot parse."
fi

# ensure required variables are set
for param in $params; do
    if eval "test -z \"\${${param}:-}\""; then
        die "Spack compiler must be run from Spack! Input '$param' is missing."
    fi
done

# eval this because SPACK_MANAGED_DIRS and SPACK_SYSTEM_DIRS are inputs we don't wanna loop over.
# moving the eval inside the function would eval it every call.
eval "\
path_order() {
case \"\$1\" in
    $SPACK_MANAGED_DIRS) return 0 ;;
    $SPACK_SYSTEM_DIRS) return 2 ;;
    /*) return 1 ;;
esac
}
"

# path_list functions. Path_lists have 3 parts: spack_store_<list>, <list> and system_<list>,
# which are used to prioritize paths when assembling the final command line.

# init_path_lists LISTNAME
# Set <LISTNAME>, spack_store_<LISTNAME>, and system_<LISTNAME> to "".
init_path_lists() {
    eval "spack_store_$1=\"\""
    eval "$1=\"\""
    eval "system_$1=\"\""
}

# assign_path_lists LISTNAME1 LISTNAME2
# Copy contents of LISTNAME2 into LISTNAME1, for each path_list prefix.
assign_path_lists() {
    eval "spack_store_$1=\"\${spack_store_$2}\""
    eval "$1=\"\${$2}\""
    eval "system_$1=\"\${system_$2}\""
}

# append_path_lists LISTNAME ELT
# Append the provided ELT to the appropriate list, based on the result of path_order().
append_path_lists() {
    path_order "$2"
    case $? in
        0) eval "append spack_store_$1 \"\$2\"" ;;
        1) eval "append $1 \"\$2\"" ;;
        2) eval "append system_$1 \"\$2\"" ;;
    esac
}

# Check if optional parameters are defined
# If we aren't asking for debug flags, don't add them
if [ -z "${SPACK_ADD_DEBUG_FLAGS:-}" ]; then
    SPACK_ADD_DEBUG_FLAGS="false"
fi

# SPACK_ADD_DEBUG_FLAGS must be true/false/custom
is_valid="false"
for param in "true" "false" "custom"; do
  if [ "$param" = "$SPACK_ADD_DEBUG_FLAGS" ];  then
      is_valid="true"
  fi
done

# Exit with error if we are given an incorrect value
if [ "$is_valid" = "false" ]; then
    die "SPACK_ADD_DEBUG_FLAGS, if defined, must be one of 'true', 'false', or 'custom'."
fi

# Figure out the type of compiler, the language, and the mode so that
# the compiler script knows what to do.
#
# Possible languages are C, C++, Fortran 77, and Fortran 90.
# 'command' is set based on the input command to $SPACK_[CC|CXX|F77|F90]
#
# 'mode' is set to one of:
#    vcheck  version check
#    cpp     preprocess
#    cc      compile
#    as      assemble
#    ld      link
#    ccld    compile & link

# Note. SPACK_ALWAYS_XFLAGS are applied for all compiler invocations,
# including version checks (SPACK_XFLAGS variants are not applied
# for version checks).
command="${0##*/}"
comp="CC"
vcheck_flags=""
case "$command" in
    cpp)
        mode=cpp
        debug_flags="-g"
        vcheck_flags="${SPACK_ALWAYS_CPPFLAGS}"
        ;;
    cc|c89|c99|gcc|clang|armclang|icc|icx|pgcc|nvc|xlc|xlc_r|fcc|amdclang|cl.exe|craycc)
        command="$SPACK_CC"
        language="C"
        comp="CC"
        lang_flags=C
        debug_flags="-g"
        vcheck_flags="${SPACK_ALWAYS_CFLAGS}"
        ;;
    c++|CC|g++|clang++|armclang++|icpc|icpx|pgc++|nvc++|xlc++|xlc++_r|FCC|amdclang++|crayCC)
        command="$SPACK_CXX"
        language="C++"
        comp="CXX"
        lang_flags=CXX
        debug_flags="-g"
        vcheck_flags="${SPACK_ALWAYS_CXXFLAGS}"
        ;;
    ftn|f90|fc|f95|gfortran|flang|armflang|ifort|ifx|pgfortran|nvfortran|xlf90|xlf90_r|nagfor|frt|amdflang|crayftn)
        command="$SPACK_FC"
        language="Fortran 90"
        comp="FC"
        lang_flags=F
        debug_flags="-g"
        vcheck_flags="${SPACK_ALWAYS_FFLAGS}"
        ;;
    f77|xlf|xlf_r|pgf77)
        command="$SPACK_F77"
        language="Fortran 77"
        comp="F77"
        lang_flags=F
        debug_flags="-g"
        vcheck_flags="${SPACK_ALWAYS_FFLAGS}"
        ;;
    ld|ld.gold|ld.lld)
        mode=ld
        if [ -z "$SPACK_CC_RPATH_ARG" ]; then
            comp="CXX"
        fi
        ;;
    *)
        die "Unknown compiler: $command"
        ;;
esac

# If any of the arguments below are present, then the mode is vcheck.
# In vcheck mode, nothing is added in terms of extra search paths or
# libraries.
if [ -z "$mode" ] || [ "$mode" = ld ]; then
    for arg in "$@"; do
        case $arg in
            -v|-V|--version|-dumpversion)
                mode=vcheck
                break
                ;;
        esac
    done
fi

# Finish setting up the mode.
if [ -z "$mode" ]; then
    mode=ccld
    for arg in "$@"; do
        if [ "$arg" = "-E" ]; then
            mode=cpp
            break
        elif [ "$arg" = "-S" ]; then
            mode=as
            break
        elif [ "$arg" = "-c" ]; then
            mode=cc
            break
        fi
    done
fi

# This is needed to ensure we set RPATH instead of RUNPATH
# (or the opposite, depending on the configuration in config.yaml)
#
# Documentation on this mechanism is lacking at best. A few sources
# of information are (note that some of them take explicitly the
# opposite stance that Spack does):
#
# http://blog.qt.io/blog/2011/10/28/rpath-and-runpath/
# https://wiki.debian.org/RpathIssue
#
# The only discussion I could find on enabling new dynamic tags by
# default on ld is the following:
#
# https://sourceware.org/ml/binutils/2013-01/msg00307.html
#
dtags_to_add="${SPACK_DTAGS_TO_ADD}"
dtags_to_strip="${SPACK_DTAGS_TO_STRIP}"

linker_arg="ERROR: LINKER ARG WAS NOT SET, MAYBE THE PACKAGE DOES NOT DEPEND ON ${comp}?"
eval "linker_arg=\${SPACK_${comp}_LINKER_ARG:?${linker_arg}}"

# Set up rpath variable according to language.
rpath="ERROR: RPATH ARG WAS NOT SET, MAYBE THE PACKAGE DOES NOT DEPEND ON ${comp}?"
eval "rpath=\${SPACK_${comp}_RPATH_ARG:?${rpath}}"

# Dump the mode and exit if the command is dump-mode.
if [ "$SPACK_TEST_COMMAND" = "dump-mode" ]; then
    echo "$mode"
    exit
fi

#
# Filter '.' and Spack environment directories out of PATH so that
# this script doesn't just call itself
#
new_dirs=""
IFS=':'
for dir in $PATH; do
    addpath=true
    for spack_env_dir in $SPACK_COMPILER_WRAPPER_PATH; do
        case "${dir%%/}" in
            "$spack_env_dir"|'.'|'')
                addpath=false
                break
                ;;
        esac
    done
    if [ $addpath = true ]; then
        append new_dirs "$dir"
    fi
done
unset IFS
export PATH="$new_dirs"

if [ "$mode" = vcheck ]; then
    full_command_list="$command"
    args="$@"
    extend full_command_list vcheck_flags
    extend full_command_list args
    execute
fi

# Darwin's linker has a -r argument that merges object files together.
# It doesn't work with -rpath.
# This variable controls whether they are added.
add_rpaths=true
if [ "$mode" = ld ] || [ "$mode" = ccld ]; then
    if [ "${SPACK_SHORT_SPEC#*darwin}" != "${SPACK_SHORT_SPEC}" ]; then
        for arg in "$@"; do
            if [ "$arg" = "-r" ]; then
                if [ "$mode" = ld ] || [ "$mode" = ccld ]; then
                    add_rpaths=false
                    break
                fi
            elif [ "$arg" = "-Wl,-r" ] && [ "$mode" = ccld ]; then
                add_rpaths=false
                break
            fi
        done
    fi
fi

# Save original command for debug logging
input_command="$*"

#
# Parse the command line arguments.
#
# We extract -L, -I, -isystem and -Wl,-rpath arguments from the
# command line and recombine them with Spack arguments later.  We
# parse these out so that we can make sure that system paths come
# last, that package arguments come first, and that Spack arguments
# are injected properly.
#
# All other arguments, including -l arguments, are treated as
# 'other_args' and left in their original order.  This ensures that
# --start-group, --end-group, and other order-sensitive flags continue to
# work as the caller expects.
#
# The libs variable is initialized here for completeness, and it is also
# used later to inject flags supplied via `ldlibs` on the command
# line. These come into the wrappers via SPACK_LDLIBS.

# The loop below breaks up the command line into these lists of components.
# The lists are all bell-separated to be as flexible as possible, as their
# contents may come from the command line, from ' '-separated lists,
# ':'-separated lists, etc.

parse_Wl() {
    while [ $# -ne 0 ]; do
    if [ "$wl_expect_rpath" = yes ]; then
        append_path_lists return_rpath_dirs_list "$1"
        wl_expect_rpath=no
    else
        case "$1" in
            -rpath=*)
                arg="${1#-rpath=}"
                if [ -z "$arg" ]; then
                    shift; continue
                fi
                append_path_lists return_rpath_dirs_list "$arg"
                ;;
            --rpath=*)
                arg="${1#--rpath=}"
                if [ -z "$arg" ]; then
                    shift; continue
                fi
                append_path_lists return_rpath_dirs_list "$arg"
                ;;
            -rpath|--rpath)
                wl_expect_rpath=yes
                ;;
            "$dtags_to_strip")
                ;;
            -Wl)
                # Nested -Wl,-Wl means we're in NAG compiler territory. We don't support it.
                return 1
                ;;
            *)
                append return_other_args_list "-Wl,$1"
                ;;
        esac
    fi
    shift
    done
}

categorize_arguments() {

    unset IFS

    return_other_args_list=""
    return_isystem_was_used=""

    init_path_lists return_isystem_include_dirs_list
    init_path_lists return_include_dirs_list
    init_path_lists return_lib_dirs_list
    init_path_lists return_rpath_dirs_list

    # Global state for keeping track of -Wl,-rpath -Wl,/path
    wl_expect_rpath=no

    # Same, but for -Xlinker -rpath -Xlinker /path
    xlinker_expect_rpath=no

    while [ $# -ne 0 ]; do

        # an RPATH to be added after the case statement.
        rp=""

        # Multiple consecutive spaces in the command line can
        # result in blank arguments
        if [ -z "$1" ]; then
            shift
            continue
        fi

        if [ -n "${SPACK_COMPILER_FLAGS_KEEP}" ] ; then
            # NOTE: the eval is required to allow `|` alternatives inside the variable
            eval "\
            case \"\$1\" in
                $SPACK_COMPILER_FLAGS_KEEP)
                    append return_other_args_list \"\$1\"
                    shift
                    continue
                    ;;
            esac
            "
        fi
        # the replace list is a space-separated list of pipe-separated pairs,
        # the first in each pair is the original prefix to be matched, the
        # second is the replacement prefix
        if [ -n "${SPACK_COMPILER_FLAGS_REPLACE}" ] ; then
            for rep in ${SPACK_COMPILER_FLAGS_REPLACE} ; do
                before=${rep%|*}
                after=${rep#*|}
                eval "\
                stripped=\"\${1##$before}\"
                "
                if [ "$stripped" = "$1" ] ; then
                    continue
                fi

                replaced="$after$stripped"

                # it matched, remove it
                shift

                if [ -z "$replaced" ] ; then
                    # completely removed, continue OUTER loop
                    continue 2
                fi

                # re-build argument list with replacement
                set -- "$replaced" "$@"
            done
        fi

        case "$1" in
            -isystem*)
                arg="${1#-isystem}"
                return_isystem_was_used=true
                if [ -z "$arg" ]; then shift; arg="$1"; fi
                append_path_lists return_isystem_include_dirs_list "$arg"
                ;;
            -I*)
                arg="${1#-I}"
                if [ -z "$arg" ]; then shift; arg="$1"; fi
                append_path_lists return_include_dirs_list "$arg"
                ;;
            -L*)
                arg="${1#-L}"
                if [ -z "$arg" ]; then shift; arg="$1"; fi
                append_path_lists return_lib_dirs_list "$arg"
                ;;
            -l*)
                # -loopopt=0 is generated erroneously in autoconf <= 2.69,
                # and passed by ifx to the linker, which confuses it with a
                # library. Filter it out.
                # TODO: generalize filtering of args with an env var, so that
                # TODO: we do not have to special case this here.
                if { [ "$mode" = "ccld" ] || [ $mode = "ld" ]; } \
                    && [ "$1" != "${1#-loopopt}" ]; then
                    shift
                    continue
                fi
                arg="${1#-l}"
                if [ -z "$arg" ]; then shift; arg="$1"; fi
                append return_other_args_list "-l$arg"
                ;;
            -Wl,*)
                IFS=,
                if ! parse_Wl ${1#-Wl,}; then
                    append return_other_args_list "$1"
                fi
                unset IFS
                ;;
            -Xlinker)
                shift
                if [ $# -eq 0 ]; then
                    # -Xlinker without value: let the compiler error about it.
                    append return_other_args_list -Xlinker
                    xlinker_expect_rpath=no
                    break
                elif [ "$xlinker_expect_rpath" = yes ]; then
                    # Register the path of -Xlinker -rpath <other args> -Xlinker <path>
                    append_path_lists return_rpath_dirs_list "$1"
                    xlinker_expect_rpath=no
                else
                    case "$1" in
                        -rpath=*)
                            arg="${1#-rpath=}"
                            append_path_lists return_rpath_dirs_list "$arg"
                            ;;
                        --rpath=*)
                            arg="${1#--rpath=}"
                            append_path_lists return_rpath_dirs_list "$arg"
                            ;;
                        -rpath|--rpath)
                            xlinker_expect_rpath=yes
                            ;;
                        "$dtags_to_strip")
                            ;;
                        *)
                            append return_other_args_list -Xlinker
                            append return_other_args_list "$1"
                            ;;
                    esac
                fi
                ;;
            "$dtags_to_strip")
                ;;
            *)
                # if mode is not ld, we can just add to other args
                if [ "$mode" != "ld" ]; then
                    append return_other_args_list "$1"
                    shift
                    continue
                fi

                # if we're in linker mode, we need to parse raw RPATH args
                case "$1" in
                    -rpath=*)
                        arg="${1#-rpath=}"
                        append_path_lists return_rpath_dirs_list "$arg"
                        ;;
                    --rpath=*)
                        arg="${1#--rpath=}"
                        append_path_lists return_rpath_dirs_list "$arg"
                        ;;
                    -rpath|--rpath)
                        if [ $# -eq 1 ]; then
                            # -rpath without value: let the linker raise an error.
                            append return_other_args_list "$1"
                            break
                        fi
                        shift
                        append_path_lists return_rpath_dirs_list "$1"
                        ;;
                    *)
                        append return_other_args_list "$1"
                        ;;
                esac
                ;;
        esac
        shift
    done

    # We found `-Xlinker -rpath` but no matching value `-Xlinker /path`. Just append
    # `-Xlinker -rpath` again and let the compiler or linker handle the error during arg
    # parsing.
    if [ "$xlinker_expect_rpath" = yes ]; then
        append return_other_args_list -Xlinker
        append return_other_args_list -rpath
    fi

    # Same, but for -Wl flags.
    if [ "$wl_expect_rpath" = yes ]; then
        append return_other_args_list -Wl,-rpath
    fi
}

categorize_arguments "$@"

assign_path_lists isystem_include_dirs_list return_isystem_include_dirs_list
assign_path_lists include_dirs_list return_include_dirs_list
assign_path_lists lib_dirs_list return_lib_dirs_list
assign_path_lists rpath_dirs_list return_rpath_dirs_list

isystem_was_used="$return_isystem_was_used"
other_args_list="$return_other_args_list"

#
# Add flags from Spack's cppflags, cflags, cxxflags, fcflags, fflags, and
# ldflags. We stick to the order that gmake puts the flags in by default.
#
# See the gmake manual on implicit rules for details:
# https://www.gnu.org/software/make/manual/html_node/Implicit-Variables.html
#
flags_list=""

# Add debug flags
if [ "${SPACK_ADD_DEBUG_FLAGS}" = "true" ]; then
    extend flags_list debug_flags

# If a custom flag is requested, derive from environment
elif [ "$SPACK_ADD_DEBUG_FLAGS" = "custom" ]; then
    extend flags_list SPACK_DEBUG_FLAGS
fi

spack_flags_list=""

# Fortran flags come before CPPFLAGS
case "$mode" in
    cc|ccld)
        case $lang_flags in
            F)
                extend spack_flags_list SPACK_ALWAYS_FFLAGS
                extend spack_flags_list SPACK_FFLAGS
                ;;
        esac
        ;;
esac

# C preprocessor flags come before any C/CXX flags
case "$mode" in
    cpp|as|cc|ccld)
        extend spack_flags_list SPACK_ALWAYS_CPPFLAGS
        extend spack_flags_list SPACK_CPPFLAGS
        ;;
esac


# Add C and C++ flags
case "$mode" in
    cc|ccld)
        case $lang_flags in
            C)
                extend spack_flags_list SPACK_ALWAYS_CFLAGS
                extend spack_flags_list SPACK_CFLAGS
                preextend flags_list SPACK_TARGET_ARGS_CC
                ;;
            CXX)
                extend spack_flags_list SPACK_ALWAYS_CXXFLAGS
                extend spack_flags_list SPACK_CXXFLAGS
                preextend flags_list SPACK_TARGET_ARGS_CXX
                ;;
            F)
                preextend flags_list SPACK_TARGET_ARGS_FORTRAN
                ;;
        esac
        ;;
esac

# Linker flags
case "$mode" in
    ccld)
        extend spack_flags_list SPACK_LDFLAGS
        ;;
esac

IFS="$lsep"
    categorize_arguments $spack_flags_list
unset IFS

assign_path_lists spack_flags_isystem_include_dirs_list return_isystem_include_dirs_list
assign_path_lists spack_flags_include_dirs_list return_include_dirs_list
assign_path_lists spack_flags_lib_dirs_list return_lib_dirs_list
assign_path_lists spack_flags_rpath_dirs_list return_rpath_dirs_list

spack_flags_isystem_was_used="$return_isystem_was_used"
spack_flags_other_args_list="$return_other_args_list"


# On macOS insert headerpad_max_install_names linker flag
if [ "$mode" = ld ] || [ "$mode" = ccld ]; then
    if [ "${SPACK_SHORT_SPEC#*darwin}" != "${SPACK_SHORT_SPEC}" ]; then
        case "$mode" in
            ld)
                append flags_list "-headerpad_max_install_names" ;;
            ccld)
                append flags_list "-Wl,-headerpad_max_install_names" ;;
        esac
    fi
fi

if [ "$mode" = ccld ] || [ "$mode" = ld ]; then
    if [ "$add_rpaths" != "false" ]; then
        # Append RPATH directories. Note that in the case of the
        # top-level package these directories may not exist yet. For dependencies
        # it is assumed that paths have already been confirmed.
        extend spack_store_rpath_dirs_list SPACK_STORE_RPATH_DIRS
        extend rpath_dirs_list SPACK_RPATH_DIRS
    fi
fi

if [ "$mode" = ccld ] || [ "$mode" = ld ]; then
    extend spack_store_lib_dirs_list SPACK_STORE_LINK_DIRS
    extend lib_dirs_list SPACK_LINK_DIRS
fi

libs_list=""

# add RPATHs if we're in in any linking mode
case "$mode" in
    ld|ccld)
        # Set extra RPATHs
        extend lib_dirs_list SPACK_COMPILER_EXTRA_RPATHS
        if [ "$add_rpaths" != "false" ]; then
            extend rpath_dirs_list SPACK_COMPILER_EXTRA_RPATHS
        fi

        # Set implicit RPATHs
        if [ "$add_rpaths" != "false" ]; then
            extend rpath_dirs_list SPACK_COMPILER_IMPLICIT_RPATHS
        fi

        # Add SPACK_LDLIBS to args
        for lib in $SPACK_LDLIBS; do
            append libs_list "${lib#-l}"
        done
        ;;
esac

case "$mode" in
    cpp|cc|as|ccld)
        if [ "$spack_flags_isystem_was_used" = "true" ] || [ "$isystem_was_used" = "true" ]; then
            extend spack_store_isystem_include_dirs_list SPACK_STORE_INCLUDE_DIRS
            extend isystem_include_dirs_list SPACK_INCLUDE_DIRS
        else
            extend spack_store_include_dirs_list SPACK_STORE_INCLUDE_DIRS
            extend include_dirs_list SPACK_INCLUDE_DIRS
        fi
        ;;
esac

#
# Finally, reassemble the command line.
#
args_list="$flags_list"

# Include search paths partitioned by (in store, non-sytem, system)
# NOTE: adding ${lsep} to the prefix here turns every added element into two
extend args_list spack_store_spack_flags_include_dirs_list -I
extend args_list spack_store_include_dirs_list -I

extend args_list spack_flags_include_dirs_list -I
extend args_list include_dirs_list -I

extend args_list spack_store_spack_flags_isystem_include_dirs_list "-isystem${lsep}"
extend args_list spack_store_isystem_include_dirs_list "-isystem${lsep}"

extend args_list spack_flags_isystem_include_dirs_list "-isystem${lsep}"
extend args_list isystem_include_dirs_list "-isystem${lsep}"

extend args_list system_spack_flags_include_dirs_list -I
extend args_list system_include_dirs_list -I

extend args_list system_spack_flags_isystem_include_dirs_list "-isystem${lsep}"
extend args_list system_isystem_include_dirs_list "-isystem${lsep}"

# Library search paths partitioned by (in store, non-sytem, system)
extend args_list spack_store_spack_flags_lib_dirs_list "-L"
extend args_list spack_store_lib_dirs_list "-L"

extend args_list spack_flags_lib_dirs_list "-L"
extend args_list lib_dirs_list "-L"

extend args_list system_spack_flags_lib_dirs_list "-L"
extend args_list system_lib_dirs_list "-L"

# RPATHs arguments
rpath_prefix=""
case "$mode" in
    ccld)
        if [ -n "$dtags_to_add" ] ; then
            append args_list "$linker_arg$dtags_to_add"
        fi
        rpath_prefix="$rpath"
        ;;
    ld)
        if [ -n "$dtags_to_add" ] ; then
            append args_list "$dtags_to_add"
        fi
        rpath_prefix="-rpath${lsep}"
        ;;
esac

# if mode is ccld or ld, extend RPATH lists with the prefix determined above
if [ -n "$rpath_prefix" ]; then
    extend args_list spack_store_spack_flags_rpath_dirs_list "$rpath_prefix"
    extend args_list spack_store_rpath_dirs_list "$rpath_prefix"

    extend args_list spack_flags_rpath_dirs_list "$rpath_prefix"
    extend args_list rpath_dirs_list "$rpath_prefix"

    extend args_list system_spack_flags_rpath_dirs_list "$rpath_prefix"
    extend args_list system_rpath_dirs_list "$rpath_prefix"
fi

# Other arguments from the input command
extend args_list other_args_list
extend args_list spack_flags_other_args_list

# Inject SPACK_LDLIBS, if supplied
extend args_list libs_list "-l"

full_command_list="$command"
extend full_command_list args_list

# prepend the ccache binary if we're using ccache
if [ -n "$SPACK_CCACHE_BINARY" ]; then
    case "$lang_flags" in
        C|CXX)  # ccache only supports C languages
            prepend full_command_list "${SPACK_CCACHE_BINARY}"
            # workaround for stage being a temp folder
            # see #3761#issuecomment-294352232
            export CCACHE_NOHASHDIR=yes
            ;;
    esac
fi

execute
