Installation Guide
==================

Requirements
------------
We require Python 3.8+ and the packages `listed here <https://github.com/pyrddlgym-project/pyRDDLGym/blob/main/requirements.txt>`_.

Installing via pip
-----------------
.. code-block:: shell

    pip install pyRDDLGym

To run the basic examples, you will also need ``rddlrepository``

.. code-block:: shell

    pip install rddlrepository

We recommend installing everything together under a conda virtual environment:

.. code-block:: shell

    conda create -n rddl python=3.11
    conda activate rddl
    pip install pyrddlgym rddlrepository

Installing the Pre-Release Version via git
---------
.. code-block:: shell

    pip install git+https://github.com/pyrddlgym-project/pyRDDLGym.git
    pip install git+https://github.com/pyrddlgym-project/rddlrepository.git

