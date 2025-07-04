.. Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. _getting_started:

===============
Getting Started
===============

Getting Spack is easy.  You can clone it from the `GitHub repository
<https://github.com/spack/spack>`_ using this command:

.. code-block:: console

   $ git clone -c feature.manyFiles=true --depth=2 https://github.com/spack/spack.git

This will create a directory called ``spack``. Once you have cloned Spack, we recommend sourcing the appropriate script for your shell:

.. tab-set::

   .. tab-item:: bash/zsh/sh

      .. code-block:: console

         $ . spack/share/spack/setup-env.sh

   .. tab-item:: tcsh/csh

      .. code-block:: console

         $ source spack/share/spack/setup-env.csh

   .. tab-item:: fish

      .. code-block:: console

         $ . spack/share/spack/setup-env.fish

That's it! You're ready to use Spack.

.. note::
   ``-c feature.manyFiles=true`` improves Git's performance on repositories with 1,000+ files.

   ``--depth=2`` prunes the git history to reduce the size of the Spack installation.

-------------
Prerequisites
-------------

To check that the prerequisites for running Spack are met on your system you can use:

.. code-block:: console

   $ spack bootstrap status --optional
   Spack v1.0.0 - python@3.13

   [PASS] Core Functionalities

   [PASS] Binary packages

   [PASS] Optional Features

If all pre-requisites are met, the output should look similar to the one shown above. When a prerequisite is missing,
Spack will point it out, and show whether it can be bootstrapped, or it is user's responsibility to make it available:

.. code-block:: console

   $ spack bootstrap status --optional
   Spack v1.0.0 - python@3.13

   [PASS] Core Functionalities

   [FAIL] Binary packages
     [B] MISSING "gpg2": required to sign/verify buildcaches

   [PASS] Optional Features


   Spack will take care of bootstrapping any missing dependency marked as [B]. Dependencies marked as [-] are instead required to be found on the system.

In the case above, the system is missing ``gpg2``, and thus Spack can't verify the signature on binary packages.

In general, Spack's requirements can be easily installed on most modern Linux systems;
on macOS, the Command Line Tools package is required, and a full Xcode suite
may be necessary for some packages such as Qt and apple-gl. Spack is designed
to run on HPC platforms like Cray.

A build matrix showing which packages are working on which systems is shown below.

.. tab-set::

   .. tab-item:: Debian/Ubuntu

      .. code-block:: console

         apt update
         apt install bzip2 ca-certificates g++ gcc gfortran git gzip lsb-release patch python3 tar unzip xz-utils zstd

   .. tab-item:: RHEL

      .. code-block:: console

         dnf install epel-release
         dnf group install "Development Tools"
         dnf install gcc-gfortran redhat-lsb-core python3 unzip

   .. tab-item:: macOS Brew

      .. code-block:: console

         brew update
         brew install gcc git zip

.. _shell-support:

^^^^^^^^^^^^^
Shell support
^^^^^^^^^^^^^

Sourcing the shell scripts will put the ``spack`` command in your ``PATH``, set
up your ``MODULEPATH`` to use Spack's packages, and add other useful
shell integration for :ref:`certain commands <packaging-shell-support>`,
:ref:`environments <environments>`, and :ref:`modules <modules>`. For
``bash`` and ``zsh``, it also sets up tab completion.

In order to know which directory to add to your ``MODULEPATH``, these scripts
query the ``spack`` command. On shared filesystems, this can be a bit slow,
especially if you log in frequently. If you don't use modules, or want to set
``MODULEPATH`` manually instead, you can set the ``SPACK_SKIP_MODULES``
environment variable to skip this step and speed up sourcing the file.

When the ``spack`` command is executed, it searches for an appropriate
Python interpreter to use, which can be explicitly overridden by setting
the ``SPACK_PYTHON`` environment variable.  When sourcing the appropriate shell
setup script, ``SPACK_PYTHON`` will be set to the interpreter found at
sourcing time, ensuring future invocations of the ``spack`` command will
continue to use the same consistent Python version regardless of changes in
the environment.

^^^^^^^^^^^^^^^^^^^^
Bootstrapping clingo
^^^^^^^^^^^^^^^^^^^^

Spack uses ``clingo`` under the hood to resolve optimal versions and variants of
dependencies when installing a package. Since ``clingo`` itself is a binary,
Spack has to install it on initial use, which is called bootstrapping.

Spack provides two ways of bootstrapping ``clingo``: from pre-built binaries
(default), or from sources. The fastest way to get started is to bootstrap from
pre-built binaries.

The first time you concretize a spec, Spack will bootstrap automatically:

.. code-block:: console

   $ spack spec zlib
   ==> Bootstrapping clingo from pre-built binaries
   ==> Fetching https://mirror.spack.io/bootstrap/github-actions/v0.4/build_cache/linux-centos7-x86_64-gcc-10.2.1-clingo-bootstrap-spack-ba5ijauisd3uuixtmactc36vps7yfsrl.spec.json
   ==> Fetching https://mirror.spack.io/bootstrap/github-actions/v0.4/build_cache/linux-centos7-x86_64/gcc-10.2.1/clingo-bootstrap-spack/linux-centos7-x86_64-gcc-10.2.1-clingo-bootstrap-spack-ba5ijauisd3uuixtmactc36vps7yfsrl.spack
   ==> Installing "clingo-bootstrap@spack%gcc@10.2.1~docs~ipo+python+static_libstdcpp build_type=Release arch=linux-centos7-x86_64" from a buildcache
   ==> Bootstrapping patchelf from pre-built binaries
   ==> Fetching https://mirror.spack.io/bootstrap/github-actions/v0.4/build_cache/linux-centos7-x86_64-gcc-10.2.1-patchelf-0.16.1-p72zyan5wrzuabtmzq7isa5mzyh6ahdp.spec.json
   ==> Fetching https://mirror.spack.io/bootstrap/github-actions/v0.4/build_cache/linux-centos7-x86_64/gcc-10.2.1/patchelf-0.16.1/linux-centos7-x86_64-gcc-10.2.1-patchelf-0.16.1-p72zyan5wrzuabtmzq7isa5mzyh6ahdp.spack
   ==> Installing "patchelf@0.16.1%gcc@10.2.1 ldflags="-static-libstdc++ -static-libgcc"  build_system=autotools arch=linux-centos7-x86_64" from a buildcache
   Input spec
   --------------------------------
   zlib

   Concretized
   --------------------------------
   zlib@1.2.13%gcc@9.4.0+optimize+pic+shared build_system=makefile arch=linux-ubuntu20.04-icelake

The default bootstrap behavior is to use pre-built binaries. You can verify the
active bootstrap repositories with:

.. command-output:: spack bootstrap list

If for security concerns you cannot bootstrap ``clingo`` from pre-built
binaries, you have to disable fetching the binaries we generated with GitHub Actions.

.. code-block:: console

   $ spack bootstrap disable github-actions-v0.6
   ==> "github-actions-v0.6" is now disabled and will not be used for bootstrapping
   $ spack bootstrap disable github-actions-v0.5
   ==> "github-actions-v0.5" is now disabled and will not be used for bootstrapping

You can verify that the new settings are effective with ``spack bootstrap list``.

.. note::

   When bootstrapping from sources, Spack requires a full install of Python
   including header files (e.g. ``python3-dev`` on Debian), and a compiler
   with support for C++14 (GCC on Linux, Apple Clang on macOS) and static C++
   standard libraries on Linux.

Spack will build the required software on the first request to concretize a spec:

.. code-block:: console

   $ spack spec zlib
   [+] /usr (external bison-3.0.4-wu5pgjchxzemk5ya2l3ddqug2d7jv6eb)
   [+] /usr (external cmake-3.19.4-a4kmcfzxxy45mzku4ipmj5kdiiz5a57b)
   [+] /usr (external python-3.6.9-x4fou4iqqlh5ydwddx3pvfcwznfrqztv)
   ==> Installing re2c-1.2.1-e3x6nxtk3ahgd63ykgy44mpuva6jhtdt
   [ ... ]
   zlib@1.2.11%gcc@10.1.0+optimize+pic+shared arch=linux-ubuntu18.04-broadwell

"""""""""""""""""""
The Bootstrap Store
"""""""""""""""""""

All the tools Spack needs for its own functioning are installed in a separate store, which lives
under the ``${HOME}/.spack`` directory. The software installed there can be queried with:

.. code-block:: console

   $ spack -b find
   -- linux-ubuntu18.04-x86_64 / gcc@10.1.0 ------------------------
   clingo-bootstrap@spack  python@3.6.9  re2c@1.2.1

In case it's needed, the bootstrap store can also be cleaned with:

.. code-block:: console

   $ spack clean -b
   ==> Removing bootstrapped software and configuration in "/home/spack/.spack/bootstrap"

^^^^^^^^^^^^^^^^^^
Check Installation
^^^^^^^^^^^^^^^^^^

With Spack installed, you should be able to run some basic Spack
commands.  For example:

.. command-output:: spack spec netcdf-c

In theory, Spack doesn't need any additional installation; just
download and run!  But in real life, additional steps are usually
required before Spack can work in a practical sense.  Read on...

^^^^^^^^^^^^^^^^^
Clean Environment
^^^^^^^^^^^^^^^^^

Many package installs can be broken by changing environment
variables.  For example, a package might pick up the wrong build-time
dependencies (most of them not specified) depending on the setting of
``PATH``.  ``GCC`` seems to be particularly vulnerable to these issues.

Therefore, it is recommended that Spack users run with a *clean
environment*, especially for ``PATH``.  Only software that comes with
the system, or that you know you wish to use with Spack, should be
included.  This procedure will avoid many strange build errors.


.. _compiler-config:

----------------------
Compiler configuration
----------------------

Spack has the ability to build packages with multiple compilers and compiler versions.
Compilers can be made available to Spack by specifying them manually in ``packages.yaml``,
or automatically by running ``spack compiler find``.
For convenience, Spack will automatically detect compilers the first time it needs them,
if no compiler is available.

.. _cmd-spack-compilers:

^^^^^^^^^^^^^^^^^^^
``spack compilers``
^^^^^^^^^^^^^^^^^^^

You can see which compilers are available to Spack by running ``spack
compilers`` or ``spack compiler list``:

.. code-block:: console

   $ spack compilers
   ==> Available compilers
   -- gcc ubuntu20.04-x86_64 ---------------------------------------
   [e]  gcc@10.5.0  [+]  gcc@15.1.0  [+]  gcc@14.3.0

   -- intel-oneapi-compilers ubuntu20.04-x86_64 --------------------
   [+]  intel-oneapi-compilers@2025.1.1

Compilers marked with an ``[e]`` are available as externals, while those marked with a ``[+]``
are installed in the local Spack's store.

Any of these compilers can be used to build Spack packages.  More on how this is done is in :ref:`sec-specs`.

.. _cmd-spack-compiler-add:

^^^^^^^^^^^^^^^^^^^^^^
``spack compiler add``
^^^^^^^^^^^^^^^^^^^^^^

An alias for ``spack compiler find``.

.. _cmd-spack-compiler-find:

^^^^^^^^^^^^^^^^^^^^^^^
``spack compiler find``
^^^^^^^^^^^^^^^^^^^^^^^

If you do not see a compiler in the list shown by:

.. code-block:: console

   $ spack compiler list

but you want to use it with Spack, you can simply run ``spack compiler find`` with the
path to where the compiler is installed.  For example:

.. code-block:: console

   $ spack compiler find /opt/intel/oneapi/compiler/2025.1/bin/
   ==> Added 1 new compiler to /home/user/.spack/packages.yaml
       intel-oneapi-compilers@2025.1.0
   ==> Compilers are defined in the following files:
       /home/user/.spack/packages.yaml

Or you can run ``spack compiler find`` with no arguments to force
auto-detection.  This is useful if you do not know where compilers are
installed, but you know that new compilers have been added to your
``PATH``.  For example, you might load a module, like this:

.. code-block:: console

   $ module load gcc/4.9.0
   $ spack compiler find
   ==> Added 1 new compiler to /home/user/.spack/packages.yaml
       gcc@4.9.0

This loads the environment module for gcc-4.9.0 to add it to
``PATH``, and then it adds the compiler to Spack.

.. note::

   By default, Spack does not fill in the ``modules:`` field in the
   ``packages.yaml`` file.  If you are using a compiler from a
   module, then you should add this field manually.
   See the section on :ref:`compilers-requiring-modules`.

.. _cmd-spack-compiler-info:

^^^^^^^^^^^^^^^^^^^^^^^
``spack compiler info``
^^^^^^^^^^^^^^^^^^^^^^^

If you want to see additional information of specific compilers, you can run
``spack compiler info``:

.. code-block:: console

   $ spack compiler info gcc
   gcc@=8.4.0 languages='c,c++,fortran' arch=linux-ubuntu20.04-x86_64:
     prefix: /usr
     compilers:
       c: /usr/bin/gcc-8
       cxx: /usr/bin/g++-8
       fortran: /usr/bin/gfortran-8

   gcc@=9.4.0 languages='c,c++,fortran' arch=linux-ubuntu20.04-x86_64:
     prefix: /usr
     compilers:
       c: /usr/bin/gcc
       cxx: /usr/bin/g++
       fortran: /usr/bin/gfortran

   gcc@=10.5.0 languages='c,c++,fortran' arch=linux-ubuntu20.04-x86_64:
     prefix: /usr
     compilers:
       c: /usr/bin/gcc-10
       cxx: /usr/bin/g++-10
       fortran: /usr/bin/gfortran-10

This shows the details of the compilers that were detected by Spack.
Notice also that we didn't have to be too specific about the version. We just said ``gcc``, and we got information
about all the matching compilers.

^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Manual compiler configuration
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

If auto-detection fails, you can manually configure a compiler by editing your ``~/.spack/packages.yaml`` file.
You can do this by running ``spack config edit packages``, which will open the file in
:ref:`your favorite editor <controlling-the-editor>`.

Each compiler has an "external" entry in the file with some ``extra_attributes``:

.. code-block:: yaml

   packages:
     gcc:
       externals:
       - spec: gcc@10.5.0 languages='c,c++,fortran'
         prefix: /usr
         extra_attributes:
           compilers:
             c: /usr/bin/gcc-10
             cxx: /usr/bin/g++-10
             fortran: /usr/bin/gfortran-10

The compiler executables are listed under ``extra_attributes:compilers``, and are keyed by language.
Once you save the file, the configured compilers will show up in the list displayed by ``spack compilers``.

You can also add compiler flags to manually configured compilers. These flags should be specified in the
``flags`` section of the compiler specification. The valid flags are ``cflags``, ``cxxflags``, ``fflags``,
``cppflags``, ``ldflags``, and ``ldlibs``. For example:

.. code-block:: yaml

   packages:
     gcc:
       externals:
       - spec: gcc@10.5.0 languages='c,c++,fortran'
         prefix: /usr
         extra_attributes:
           compilers:
             c: /usr/bin/gcc-10
             cxx: /usr/bin/g++-10
             fortran: /usr/bin/gfortran-10
           flags:
             cflags: -O3 -fPIC
             cxxflags: -O3 -fPIC
             cppflags: -O3 -fPIC

These flags will be treated by Spack as if they were entered from
the command line each time this compiler is used. The compiler wrappers
then inject those flags into the compiler command. Compiler flags
entered from the command line will be discussed in more detail in the
following section.

Some compilers also require additional environment configuration.
Examples include Intel's oneAPI and AMD's AOCC compiler suites,
which have custom scripts for loading environment variables and setting paths.
These variables should be specified in the ``environment`` section of the compiler
specification. The operations available to modify the environment are ``set``, ``unset``,
``prepend_path``, ``append_path``, and ``remove_path``. For example:

.. code-block:: yaml

   packages:
     intel-oneapi-compilers:
       externals:
       - spec: intel-oneapi-compilers@2025.1.0
         prefix: /opt/intel/oneapi
         extra_attributes:
           compilers:
             c: /opt/intel/oneapi/compiler/2025.1/bin/icx
             cxx: /opt/intel/oneapi/compiler/2025.1/bin/icpx
             fortran: /opt/intel/oneapi/compiler/2025.1/bin/ifx
           environment:
             set:
               MKL_ROOT: "/path/to/mkl/root"
             unset: # A list of environment variables to unset
               - CC
             prepend_path: # Similar for append|remove_path
               LD_LIBRARY_PATH: /ld/paths/added/by/setvars/sh

^^^^^^^^^^^^^^^^^^^^^^^
Build Your Own Compiler
^^^^^^^^^^^^^^^^^^^^^^^

If you are particular about which compiler/version you use, you might wish to have Spack build it for you.
For example:

.. code-block:: console

   $ spack install gcc@14+binutils

Once the compiler is installed, you can start using it without additional configuration:

.. code-block:: console

   $ spack install hdf5~mpi %gcc@14

The same holds true for compilers that are made available from build caches, when reusing them is allowed.

.. _compilers-requiring-modules:

^^^^^^^^^^^^^^^^^^^^^^^^^^^
Compilers Requiring Modules
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Many installed compilers will work regardless of the environment they are called with.
However, some installed compilers require environment variables to be set in order to run;
this is typical for Intel and other proprietary compilers.

On typical HPC clusters, these environment modifications are usually delegated to some "module" system.
In such a case, you should tell Spack which module(s) to load in order to run the chosen compiler:

.. code-block:: yaml

   packages:
     gcc:
       externals:
       - spec: gcc@10.5.0 languages='c,c++,fortran'
         prefix: /opt/compilers
         extra_attributes:
           compilers:
             c: /opt/compilers/bin/gcc-10
             cxx: /opt/compilers/bin/g++-10
             fortran: /opt/compilers/bin/gfortran-10
         modules: [gcc/10.5.0]

Some compilers require special environment settings to be loaded not just
to run, but also to execute the code they build, breaking packages that
need to execute code they just compiled.  If it's not possible or
practical to use a better compiler, you'll need to ensure that
environment settings are preserved for compilers like this (i.e., you'll
need to load the module or source the compiler's shell script).

By default, Spack tries to ensure that builds are reproducible by
cleaning the environment before building.  If this interferes with your
compiler settings, you CAN use ``spack install --dirty`` as a workaround.
Note that this MAY interfere with package builds.

.. _licensed-compilers:

^^^^^^^^^^^^^^^^^^
Licensed Compilers
^^^^^^^^^^^^^^^^^^

Some proprietary compilers require licensing to use.  If you need to
use a licensed compiler, the process is similar to a mix of
build your own, plus modules:

#. Create a Spack package (if it doesn't exist already) to install
   your compiler.  Follow instructions on installing :ref:`license`.

#. Once the compiler is installed, you should be able to test it by
   using Spack to load the module it just created, and running simple
   builds (e.g., ``cc helloWorld.c && ./a.out``)

#. Add the newly-installed compiler to ``packages.yaml`` as shown above.

.. _mixed-toolchains:

^^^^^^^^^^^^^^^^^^^^^^^^^^
Fortran compilers on macOS
^^^^^^^^^^^^^^^^^^^^^^^^^^

Modern compilers typically come with related compilers for C, C++ and
Fortran bundled together.  When possible, results are best if the same
compiler is used for all languages.

In some cases, this is not possible.  For example, Xcode on macOS provides no Fortran compilers.
The user is therefore forced to use a mixed toolchain: Xcode-provided Clang for C/C++ and e.g.
GNU ``gfortran`` for Fortran.

#. You need to make sure that Xcode is installed. Run the following command:

   .. code-block:: console

      $ xcode-select --install


   If the Xcode command-line tools are already installed, you will see an
   error message:

   .. code-block:: none

      xcode-select: error: command line tools are already installed, use "Software Update" to install updates


#. For most packages, the Xcode command-line tools are sufficient. However,
   some packages like ``qt`` require the full Xcode suite. You can check
   to see which you have installed by running:

   .. code-block:: console

      $ xcode-select -p


   If the output is:

   .. code-block:: none

      /Applications/Xcode.app/Contents/Developer


   you already have the full Xcode suite installed. If the output is:

   .. code-block:: none

      /Library/Developer/CommandLineTools


   you only have the command-line tools installed. The full Xcode suite can
   be installed through the App Store. Make sure you launch the Xcode
   application and accept the license agreement before using Spack.
   It may ask you to install additional components. Alternatively, the license
   can be accepted through the command line:

   .. code-block:: console

      $ sudo xcodebuild -license accept


   Note: the flag is ``-license``, not ``--license``.

#. There are different ways to get ``gfortran`` on macOS. For example, you can
   install GCC with Spack (``spack install gcc``), with Homebrew (``brew install
   gcc``), or from a `DMG installer
   <https://github.com/fxcoudert/gfortran-for-macOS/releases>`_.

#. Run ``spack compiler find`` to locate both Apple-Clang and GCC.

Since languages in Spack are modeled as virtual packages, ``apple-clang`` will be used to provide
C and C++, while GCC will be used for Fortran.

^^^^^^^^^^^^^^^^^^^^^
Compiler Verification
^^^^^^^^^^^^^^^^^^^^^

You can verify that your compilers are configured properly by installing a simple package. For example:

.. code-block:: console

   $ spack install zlib-ng%gcc@5.3.0


.. _vendor-specific-compiler-configuration:

--------------------------------------
Vendor-Specific Compiler Configuration
--------------------------------------

This section provides details on how to get vendor-specific compilers working.

^^^^^^^^^^^^^^^
Intel Compilers
^^^^^^^^^^^^^^^

Intel compilers are unusual because a single Intel compiler version
can emulate multiple GCC versions.  In order to provide this
functionality, the Intel compiler needs GCC to be installed.
Therefore, the following steps are necessary to successfully use Intel
compilers:

#. Install a version of GCC that implements the desired language
   features (``spack install gcc``).

#. Tell the Intel compiler how to find that desired GCC.  This may be
   done in one of two ways:

      "By default, the compiler determines which version of ``gcc`` or ``g++``
      you have installed from the ``PATH`` environment variable.

      If you want to use a version of ``gcc`` or ``g++`` other than the default
      version on your system, you need to use either the ``--gcc-install-dir``
      or ``--gcc-toolchain`` compiler option to specify the path to the version of
      ``gcc`` or ``g++`` that you want to use."

      -- `Intel Reference Guide <https://software.intel.com/en-us/node/522750>`_

Intel compilers may therefore be configured in one of two ways with
Spack: using modules, or using compiler flags.

^^^
NAG
^^^

The Numerical Algorithms Group provides a licensed Fortran compiler.
It is recommended to use GCC for your C/C++ compilers.

The NAG Fortran compilers are a bit more strict than other compilers, and many
packages will fail to install with error messages like:

.. code-block:: none

   Error: mpi_comm_spawn_multiple_f90.f90: Argument 3 to MPI_COMM_SPAWN_MULTIPLE has data type DOUBLE PRECISION in reference from MPI_COMM_SPAWN_MULTIPLEN and CHARACTER in reference from MPI_COMM_SPAWN_MULTIPLEA

In order to convince the NAG compiler not to be too picky about calling conventions,
you can use ``FFLAGS=-mismatch`` and ``FCFLAGS=-mismatch``. This can be done through
the command line:

.. code-block:: console

   $ spack install openmpi fflags="-mismatch"

Or it can be set permanently in your ``packages.yaml``:

.. code-block:: yaml

   packages:
     nag:
       externals:
       - spec: nag@6.1
         prefix: /opt/nag/bin
         extra_attributes:
           compilers:
             fortran: /opt/nag/bin/nagfor
         flags:
           fflags: -mismatch

.. _toolchains:

----------
Toolchains
----------

Spack can be configured to associate certain combinations of specs for
easy reference on the command line and in config and environment
files. These combinations are called ``toolchains``, because their
primary intended use is for associating compiler combinations to
apply. Toolchains are referenced by name like a direct dependency,
using the ``%`` sigil. There are two styles of toolchain config, one
using conditional dependencies through the spec syntax and one with
conditionals explicitly in the yaml:

.. code-block:: yaml

   toolchains:
     gcc_all: cflags=-O3 '%[when=%c virtuals=c]gcc %[when=%cxx virtuals=cxx]gcc %[when=%fortran virtuals=fortran]gcc'
     llvm_gfortran:
     - spec: cflags=-O3
     - spec: '%[virtuals=c]llvm'
       when: '%c'
     - spec: '%[virtuals=cxx]llvm'
       when: '%cxx'
     - spec: '%[virtuals=fortran]gcc'
       when: '%fortran'

The two syntaxes are equivalent. It is not necessary to use
conditional dependencies with toolchains, but in most cases it his
highly recommended. Similarly, while any spec constraint can be
included, it is most useful to use compiler flags, architectures, and
conditional dependencies. With the above config, the ``gcc_all``
toolchain imposes conditional dependencies such that gcc is used as
the provider for ``c``, ``cxx``, and ``fortran`` for any package using
that toolchain that depends on each language. The conditional
dependencies allow the toolchain to be applied to any package
regardless of which languages it depends on. The ``llvm_gfortran``
toolchain is the same, except it uses ``llvm`` for ``c`` and ``cxx``
and ``gcc`` for ``fortran``.

These two toolchains could be used independently or even in the same
spec, e.g. ``spack install hdf5+fortran%llvm_gfortran ^mpich
%gcc_all``. This will install an hdf5 compiled with ``llvm`` for the
C/C++ components, but with the fortran components compiled with
``gfortran``, but will build it against an MPICH installation compiled
entirely with ``gcc`` for C, C++, and Fortran.

.. note::

   Toolchains are currently limited to exclude non-direct dependencies
   (using the ``^`` syntax).

---------------
System Packages
---------------

Once compilers are configured, you need to determine which pre-installed system packages,
if any, to use in builds.  These are also configured in the ``~/.spack/packages.yaml`` file.
For example, to use an OpenMPI installed in /opt/local, you would use:

.. code-block:: yaml

   packages:
     openmpi:
       buildable: False
       externals:
       - spec: openmpi@1.10.1
         prefix: /opt/local

In general, *Spack is easier to use and more reliable if it builds all of its own dependencies*.
However, there are several packages for which one commonly needs to use system versions:

^^^
MPI
^^^

On supercomputers, sysadmins have already built MPI versions that take
into account the specifics of that computer's hardware.  Unless you
know how they were built and can choose the correct Spack variants,
you are unlikely to get a working MPI from Spack.  Instead, use an
appropriate pre-installed MPI.

If you choose a pre-installed MPI, you should consider using the
pre-installed compiler used to build that MPI.

^^^^^^^
OpenSSL
^^^^^^^

The ``openssl`` package underlies much of modern security in a modern
OS; an attacker can easily "pwn" any computer on which they can modify SSL.
Therefore, any ``openssl`` used on a system should be created in a
"trusted environment" --- for example, that of the OS vendor.

OpenSSL is also updated by the OS vendor from time to time, in
response to security problems discovered in the wider community.  It
is in everyone's best interest to use any newly updated versions as
soon as they come out.  Modern Linux installations have standard
procedures for security updates without user involvement.

Spack running at user-level is not a trusted environment, nor do Spack
users generally keep up-to-date on the latest security holes in SSL.  For
these reasons, a Spack-installed OpenSSL should likely not be trusted.

As long as the system-provided SSL works, you can use it instead.  You
can check if it works by trying to download from an ``https://`` URL.  For
example:

.. code-block:: console

    $ curl -O https://github.com/ImageMagick/ImageMagick/archive/7.0.2-7.tar.gz

To tell Spack to use the system-supplied OpenSSL, first determine what
version you have:

.. code-block:: console

   $ openssl version
   OpenSSL 1.0.2g  1 Mar 2016

Then add the following to ``~/.spack/packages.yaml``:

.. code-block:: yaml

    packages:
        openssl:
            externals:
            - spec: openssl@1.0.2g
              prefix: /usr
            buildable: False


^^^^^^^^^^^^^
BLAS / LAPACK
^^^^^^^^^^^^^

The recommended way to use system-supplied BLAS / LAPACK packages is
to add the following to ``packages.yaml``:

.. code-block:: yaml

    packages:
        netlib-lapack:
            externals:
            - spec: netlib-lapack@3.6.1
              prefix: /usr
            buildable: False
        all:
            providers:
                blas: [netlib-lapack]
                lapack: [netlib-lapack]

.. note::

   Above we pretend that the system-provided BLAS / LAPACK is ``netlib-lapack``
   only because it is the only BLAS / LAPACK provider that uses standard names
   for libraries (as opposed to, for example, ``libopenblas.so``).

   Although we specify an external package in ``/usr``, Spack is smart enough not
   to add ``/usr/lib`` to RPATHs, where it could cause unrelated system
   libraries to be used instead of their Spack equivalents. ``usr/bin`` will be
   present in PATH. However, it will have lower precedence compared to paths
   from other dependencies. This ensures that binaries in Spack dependencies
   are preferred over system binaries.

^^^
Git
^^^

Some Spack packages use ``git`` to download, which might not work on
some computers.  For example, the following error was
encountered on a Macintosh during ``spack install julia@master``:

.. code-block:: console

   ==> Cloning git repository:
     https://github.com/JuliaLang/julia.git
     on branch master
   Cloning into 'julia'...
   fatal: unable to access 'https://github.com/JuliaLang/julia.git/':
       SSL certificate problem: unable to get local issuer certificate

This problem is related to OpenSSL, and in some cases might be solved
by installing a new version of ``git`` and ``openssl``:

#. Run ``spack install git``
#. Add the output of ``spack module tcl loads git`` to your ``.bashrc``.

If this doesn't work, it is also possible to disable checking of SSL
certificates by using:

.. code-block:: console

   $ spack --insecure install

Using ``--insecure`` makes Spack disable SSL checking when fetching
   from websites and from Git.

.. warning::

   This workaround should be used ONLY as a last resort!  Without SSL
   certificate verification, Spack and Git will download from sites you
   wouldn't normally trust.  The code you download and run may then be
   compromised!  While this is not a major issue for archives that will
   be checksummed, it is especially problematic when downloading from
   named Git branches or tags, which relies entirely on trusting a
   certificate for security (no verification).

-----------------------
Utilities Configuration
-----------------------

Although Spack does not need installation *per se*, it does rely on
other packages to be available on its host system.  If those packages
are out of date or missing, then Spack will not work.  Sometimes, an
appeal to the system's package manager can fix such problems.  If not,
the solution is to have Spack install the required packages, and then
have Spack use them.

For example, if ``curl`` doesn't work, one could use the following steps
to provide Spack a working ``curl``:

.. code-block:: console

    $ spack install curl
    $ spack load curl

or alternately:

.. code-block:: console

    $ spack module tcl loads curl >>~/.bashrc

or if environment modules don't work:

.. code-block:: console

    $ export PATH=`spack location --install-dir curl`/bin:$PATH


External commands are used by Spack in two places: within core Spack,
and in the package recipes. The bootstrapping procedure for these two
cases is somewhat different, and is treated separately below.

^^^^^^^^^^^^^^^^^^^^
Core Spack Utilities
^^^^^^^^^^^^^^^^^^^^

Core Spack uses the following packages, mainly to download and unpack
source code: ``curl``, ``env``, ``git``, ``go``, ``hg``, ``svn``,
``tar``, ``unzip``, ``patch``

As long as the user's environment is set up to successfully run these
programs from outside of Spack, they should work inside of Spack as
well.  They can generally be activated as in the ``curl`` example above;
or some systems might already have an appropriate hand-built
environment module that may be loaded.  Either way works.

A few notes on specific programs in this list:

""""""""""""""""""""""""""
curl, git, Mercurial, etc.
""""""""""""""""""""""""""

Spack depends on curl to download tarballs, the format that most
Spack-installed packages come in.  Your system's curl should always be
able to download unencrypted ``http://``.  However, the curl on some
systems has problems with SSL-enabled ``https://`` URLs, due to
outdated / insecure versions of OpenSSL on those systems.  This will
prevent Spack from installing any software requiring ``https://``
until a new curl has been installed, using the technique above.

.. warning::

   remember that if you install ``curl`` via Spack that it may rely on a
   user-space OpenSSL that is not upgraded regularly.  It may fall out of
   date faster than your system OpenSSL.

Some packages use source code control systems as their download method:
``git``, ``hg``, ``svn`` and occasionally ``go``.  If you had to install
a new ``curl``, then chances are the system-supplied version of these
other programs will also not work, because they also rely on OpenSSL.
Once ``curl`` has been installed, you can similarly install the others.


^^^^^^^^^^^^^^^^^
Package Utilities
^^^^^^^^^^^^^^^^^

Spack may also encounter bootstrapping problems inside a package's
``install()`` method.  In this case, Spack will normally be running
inside a *sanitized build environment*.  This includes all of the
package's dependencies, but none of the environment Spack inherited
from the user: if you load a module or modify ``$PATH`` before
launching Spack, it will have no effect.

In this case, you will likely need to use the ``--dirty`` flag when
running ``spack install``, causing Spack to **not** sanitize the build
environment.  You are now responsible for making sure that environment
does not do strange things to Spack or its installs.

Another way to get Spack to use its own version of something is to add
that something to a package that needs it.  For example:

.. code-block:: python

   depends_on('binutils', type='build')

This is considered best practice for some common build dependencies,
such as ``autotools`` (if the ``autoreconf`` command is needed) and
``cmake`` --- ``cmake`` especially, because different packages require
a different version of CMake.

""""""""
binutils
""""""""

.. https://groups.google.com/forum/#!topic/spack/i_7l_kEEveI

Sometimes, strange error messages can happen while building a package.
For example, ``ld`` might crash.  Or one receives a message like:

.. code-block:: console

   ld: final link failed: Nonrepresentable section on output


or:

.. code-block:: console

   ld: .../_fftpackmodule.o: unrecognized relocation (0x2a) in section `.text'

These problems are often caused by an outdated ``binutils`` on your
system.  Unlike CMake or Autotools, adding ``depends_on('binutils')`` to
every package is not considered a best practice because every package
written in C/C++/Fortran would need it.  A potential workaround is to
load a recent ``binutils`` into your environment and use the ``--dirty``
flag.

-----------
GPG Signing
-----------

.. _cmd-spack-gpg:

^^^^^^^^^^^^^
``spack gpg``
^^^^^^^^^^^^^

Spack has support for signing and verifying packages using GPG keys. A
separate keyring is used for Spack, so any keys available in the user's home
directory are not used.

^^^^^^^^^^^^^^^^^^
``spack gpg init``
^^^^^^^^^^^^^^^^^^

When Spack is first installed, its keyring is empty. Keys stored in
:file:`var/spack/gpg` are the default keys for a Spack installation. These
keys may be imported by running ``spack gpg init``. This will import the
default keys into the keyring as trusted keys.

^^^^^^^^^^^^^
Trusting keys
^^^^^^^^^^^^^

Additional keys may be added to the keyring using
``spack gpg trust <keyfile>``. Once a key is trusted, packages signed by the
owner of the key may be installed.

^^^^^^^^^^^^^
Creating keys
^^^^^^^^^^^^^

You may also create your own key so that you may sign your own packages using
``spack gpg create <name> <email>``. By default, the key has no expiration,
but it may be set with the ``--expires <date>`` flag (see the ``gnupg2``
documentation for accepted date formats). It is also recommended to add a
comment as to the use of the key using the ``--comment <comment>`` flag. The
public half of the key can also be exported for sharing with others so that
they may use packages you have signed using the ``--export <keyfile>`` flag.
Secret keys may also be later exported using the
``spack gpg export <location> [<key>...]`` command.

.. note::

   Key creation speed
      The creation of a new GPG key requires generating a lot of random numbers.
      Depending on the entropy produced on your system, the entire process may
      take a long time (*even appearing to hang*). Virtual machines and cloud
      instances are particularly likely to display this behavior.

      To speed it up, you may install tools like ``rngd``, which is
      usually available as a package in the host OS.  For example, on an
      Ubuntu machine you need to give the following commands:

      .. code-block:: console

         $ sudo apt-get install rng-tools
         $ sudo rngd -r /dev/urandom

      before generating the keys.

      Another alternative is ``haveged``, which can be installed on
      RHEL/CentOS machines as follows:

      .. code-block:: console

         $ sudo yum install haveged
         $ sudo chkconfig haveged on

      `This Digital Ocean tutorial
      <https://www.digitalocean.com/community/tutorials/how-to-setup-additional-entropy-for-cloud-servers-using-haveged>`_
      provides a good overview of sources of randomness.

Here is an example of creating a key. Note that we provide a name for the key first
(which we can use to reference the key later) and an email address:

.. code-block:: console

    $ spack gpg create dinosaur dinosaur@thedinosaurthings.com


If you want to export the key as you create it:


.. code-block:: console

    $ spack gpg create --export key.pub dinosaur dinosaur@thedinosaurthings.com

Or the private key:


.. code-block:: console

    $ spack gpg create --export-secret key.priv dinosaur dinosaur@thedinosaurthings.com


You can include both ``--export`` and ``--export-secret``, each with
an output file of choice, to export both.


^^^^^^^^^^^^
Listing keys
^^^^^^^^^^^^

In order to list the keys available in the keyring, the
``spack gpg list`` command will list trusted keys with the ``--trusted`` flag
and keys available for signing using ``--signing``. If you would like to
remove keys from your keyring, use ``spack gpg untrust <keyid>``. Key IDs can be
email addresses, names, or (best) fingerprints. Here is an example of listing
the key that we just created:

.. code-block:: console

    gpgconf: socketdir is '/run/user/1000/gnupg'
    /home/spackuser/spack/opt/spack/gpg/pubring.kbx
    ----------------------------------------------------------
    pub   rsa4096 2021-03-25 [SC]
          60D2685DAB647AD4DB54125961E09BB6F2A0ADCB
    uid           [ultimate] dinosaur (GPG created for Spack) <dinosaur@thedinosaurthings.com>


Note that the name "dinosaur" can be seen under the uid, which is the unique
id. We might need this reference if we want to export or otherwise reference the key.


^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Signing and Verifying Packages
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

In order to sign a package, ``spack gpg sign <file>`` should be used. By
default, the signature will be written to ``<file>.asc``, but that may be
changed by using the ``--output <file>`` flag. If there is only one signing
key available, it will be used, but if there is more than one, the key to use
must be specified using the ``--key <keyid>`` flag. The ``--clearsign`` flag
may also be used to create a signed file which contains the contents, but it
is not recommended. Signed packages may be verified by using
``spack gpg verify <file>``.


^^^^^^^^^^^^^^
Exporting Keys
^^^^^^^^^^^^^^

You might want to export a public key, and that looks like this. Let's
use the previous example and ask Spack to export the key with uid "dinosaur."
We will provide an output location (typically a `*.pub` file) and the name of
the key.

.. code-block:: console

    $ spack gpg export dinosaur.pub dinosaur

You can then look at the created file, `dinosaur.pub`, to see the exported key.
If you want to include the private key, then just add `--secret`:

.. code-block:: console

    $ spack gpg export --secret dinosaur.priv dinosaur

This will write the private key to the file `dinosaur.priv`.

.. warning::

    You should be very careful about exporting private keys. You likely would
    only want to do this in the context of moving your Spack installation to
    a different server, and wanting to preserve keys for a build cache. If you
    are unsure about exporting, you can ask your local system administrator
    or for help on an issue or the Spack Slack.


.. _windows_support:

----------------
Spack On Windows
----------------

Windows support for Spack is currently under development. While this work is still in an early stage,
it is currently possible to set up Spack and perform a few operations on Windows.  This section will guide
you through the steps needed to install Spack and start running it on a fresh Windows machine.

^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Step 1: Install prerequisites
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To use Spack on Windows, you will need the following packages:

Required:
* Microsoft Visual Studio
* Python
* Git
* 7z

Optional:
* Intel Fortran (needed for some packages)

.. note::

  Currently MSVC is the only compiler tested for C/C++ projects. Intel OneAPI provides Fortran support.

"""""""""""""""""""""""
Microsoft Visual Studio
"""""""""""""""""""""""

Microsoft Visual Studio provides the only Windows C/C++ compiler that is currently supported by Spack.
Spack additionally requires that the Windows SDK (including WGL) to be installed as part of your
Visual Studio installation as it is required to build many packages from source.

We require several specific components to be included in the Visual Studio installation.
One is the C/C++ toolset, which can be selected as "Desktop development with C++" or "C++ build tools,"
depending on installation type (Professional, Build Tools, etc.)  The other required component is
"C++ CMake tools for Windows," which can be selected from among the optional packages.
This provides CMake and Ninja for use during Spack configuration.


If you already have Visual Studio installed, you can make sure these components are installed by
rerunning the installer.  Next to your installation, select "Modify" and look at the
"Installation details" pane on the right.

"""""""""""""
Intel Fortran
"""""""""""""

For Fortran-based packages on Windows, we strongly recommend Intel's oneAPI Fortran compilers.
The suite is free to download from Intel's website, located at
https://software.intel.com/content/www/us/en/develop/tools/oneapi/components/fortran-compiler.html.
The executable of choice for Spack will be Intel's Beta Compiler, ifx, which supports the classic
compiler's (ifort's) frontend and runtime libraries by using LLVM.

""""""
Python
""""""

As Spack is a Python-based package, an installation of Python will be needed to run it.
Python 3 can be downloaded and installed from the Windows Store, and will be automatically added
to your ``PATH`` in this case.

.. note::
   Spack currently supports Python versions later than 3.2 inclusive.

"""
Git
"""

A bash console and GUI can be downloaded from https://git-scm.com/downloads.
If you are unfamiliar with Git, there are a myriad of resources online to help
guide you through checking out repositories and switching development branches.

When given the option of adjusting your ``PATH``, choose the ``Git from the
command line and also from 3rd-party software`` option. This will automatically
update your ``PATH`` variable to include the ``git`` command.

Spack support on Windows is currently dependent on installing the Git for Windows project
as the project providing Git support on Windows. This is additionally the recommended method
for installing Git on Windows, a link to which can be found above. Spack requires the
utilities vendored by this project.

"""
7zip
"""

A tool for extracting ``.xz`` files is required for extracting source tarballs. The latest 7-Zip
can be located at https://sourceforge.net/projects/sevenzip/.

^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Step 2: Install and setup Spack
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

We are now ready to get the Spack environment set up on our machine. We
begin by using Git to clone the Spack repo, hosted at https://github.com/spack/spack.git
into a desired directory, for our purposes today, called ``spack_install``.

In order to install Spack with Windows support, run the following one-liner
in a Windows CMD prompt.

.. code-block:: console

   git clone https://github.com/spack/spack.git

.. note::
   If you chose to install Spack into a directory on Windows that is set up to require Administrative
   Privileges, Spack will require elevated privileges to run.
   Administrative Privileges can be denoted either by default, such as
   ``C:\Program Files``, or administrator-applied administrative restrictions
   on a directory that Spack installs files to such as ``C:\Users``

^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Step 3: Run and configure Spack
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

On Windows, Spack supports both primary native shells, Powershell and the traditional command prompt.
To use Spack, pick your favorite shell, and run ``bin\spack_cmd.bat`` or ``share/spack/setup-env.ps1``
(you may need to Run as Administrator) from the top-level Spack
directory. This will provide a Spack-enabled shell. If you receive a warning message that Python is not in your ``PATH``
(which may happen if you installed Python from the website and not the Windows Store), add the location
of the Python executable to your ``PATH`` now. You can permanently add Python to your ``PATH`` variable
by using the ``Edit the system environment variables`` utility in Windows Control Panel.

To configure Spack, first run the following command inside the Spack console:

.. code-block:: console

   spack compiler find

This creates a ``.staging`` directory in our Spack prefix, along with a ``windows`` subdirectory
containing a ``packages.yaml`` file. On a fresh Windows installation with the above packages
installed, this command should only detect Microsoft Visual Studio and the Intel Fortran
compiler will be integrated within the first version of MSVC present in the ``packages.yaml``
output.

Spack provides a default ``config.yaml`` file for Windows that it will use unless overridden.
This file is located at ``etc\spack\defaults\windows\config.yaml``. You can read more on how to
do this and write your own configuration files in the :ref:`Configuration Files<configuration>` section of our
documentation. If you do this, pay particular attention to the ``build_stage`` block of the file
as this specifies the directory that will temporarily hold the source code for the packages to
be installed. This path name must be sufficiently short for compliance with CMD, otherwise you
will see build errors during installation (particularly with CMake) tied to long path names.

To allow Spack's use of external tools and dependencies already on your system, the
external pieces of software must be described in the ``packages.yaml`` file.
There are two methods to populate this file:

The first and easiest choice is to use Spack to find installations on your system. In
the Spack terminal, run the following commands:

.. code-block:: console

   spack external find cmake
   spack external find ninja

The ``spack external find <name>`` will find executables on your system
with the same name given. The command will store the items found in
``packages.yaml`` in the ``.staging\`` directory.

Assuming that the command found CMake and Ninja executables in the previous
step, continue to Step 4. If no executables were found, we may need to manually direct Spack towards the CMake
and Ninja installations we set up with Visual Studio. Therefore, your ``packages.yaml`` file will look something
like this, possibly with slight variations in the paths to CMake and Ninja:

.. code-block:: yaml

   packages:
     cmake:
       externals:
       - spec: cmake@3.19
         prefix: 'c:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake'
       buildable: False
     ninja:
       externals:
       - spec: ninja@1.8.2
         prefix: 'c:\Program Files (x86)\Microsoft Visual Studio\2019\Professional\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja'
       buildable: False

You can also use a separate installation of CMake if you have one and prefer
to use it. If you don't have a path to Ninja analogous to the above, then you can
obtain it by running the Visual Studio Installer and following the instructions
at the start of this section. Also note that YAML files use spaces for indentation
and not tabs, so ensure that this is the case when editing one directly.


.. note:: Cygwin
   The use of Cygwin is not officially supported by Spack and is not tested.
   However, Spack will not prevent this, so if choosing to use Spack
   with Cygwin, know that no functionality is guaranteed.

^^^^^^^^^^^^^^^^^
Step 4: Use Spack
^^^^^^^^^^^^^^^^^

Once the configuration is complete, it is time to give the installation a test.  Install a basic package through the
Spack console via:

.. code-block:: console

   spack install cpuinfo

If in the previous step, you did not have CMake or Ninja installed, running the command above should install both packages.

.. note:: Spec Syntax Caveats
   Windows has a few idiosyncrasies when it comes to the Spack spec syntax and the use of certain shells
   See the Spack spec syntax doc for more information


^^^^^^^^^^^^^^
For developers
^^^^^^^^^^^^^^

The intent is to provide a Windows installer that will automatically set up
Python, Git, and Spack, instead of requiring the user to do so manually.
Instructions for creating the installer are at
https://github.com/spack/spack/blob/develop/lib/spack/spack/cmd/installer/README.md
