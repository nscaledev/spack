.. Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. Spack documentation master file, created by
   sphinx-quickstart on Mon Dec  9 15:32:41 2013.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

===================
Spack
===================

Spack is a package management tool designed to support multiple
versions and configurations of software on a wide variety of platforms
and environments.  It was designed for large supercomputing centers,
where many users and application teams share common installations of
software on clusters with exotic architectures, using libraries that
do not have a standard ABI.  Spack is non-destructive: installing a
new version does not break existing installations, so many
configurations can coexist on the same system.

Most importantly, Spack is *simple*.  It offers a simple *spec* syntax
so that users can specify versions and configuration options
concisely.  Spack is also simple for package authors: package files
are written in pure Python, and specs allow package authors to
maintain a single file for many different builds of the same package.

See the :doc:`features` for examples and highlights.

Get Spack from the `GitHub repository
<https://github.com/spack/spack>`_ and install your first
package:

.. code-block:: console

   $ git clone -c feature.manyFiles=true --depth=2 https://github.com/spack/spack.git
   $ cd spack/bin
   $ ./spack install libelf

.. note::
   ``-c feature.manyFiles=true`` improves Git's performance on repositories with 1,000+ files.

   ``--depth=2`` prunes the git history to reduce the size of the Spack installation.

If you're new to Spack and want to start using it, see :doc:`getting_started`,
or refer to the full manual below.


.. toctree::
   :maxdepth: 2
   :caption: Basics

   features
   getting_started
   basic_usage
   replace_conda_homebrew
   frequently_asked_questions

.. toctree::
   :maxdepth: 2
   :caption: Links

   Tutorial (spack-tutorial.rtfd.io) <https://spack-tutorial.readthedocs.io>
   Packages (packages.spack.io) <https://packages.spack.io>
   Binaries (binaries.spack.io) <https://cache.spack.io>

.. toctree::
   :maxdepth: 2
   :caption: Reference

   configuration
   config_yaml
   include_yaml
   packages_yaml
   build_settings
   environments
   env_vars_yaml
   containers
   mirrors
   module_file_support
   repositories
   binary_caches
   bootstrapping
   command_index
   chain
   extensions
   pipelines
   signing
   gpu_configuration

.. toctree::
   :maxdepth: 2
   :caption: Contributing

   contribution_guide
   packaging_guide
   build_systems
   developer_guide

.. toctree::
   :maxdepth: 2
   :caption: API Docs

   Spack API Docs <spack>
   Spack Builtin Repo <spack_repo>
   LLNL API Docs <llnl>

==================
Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
