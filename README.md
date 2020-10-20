# Conda Charmer's Corner

A project to create the conda build environment used to create the CCDC API conda packages

Please follow (this workflow)[https://confluence.ccdc.cam.ac.uk/x/HBV_/] when making changes to the repository.

## Getting Started

- Clone this repository locally
- Create a python3 virtualenv
- pip install -r requirements.txt
- run with python create_build_environment.py

## Changing the list of packages

- change the required_conda_packages field in MinicondaBuildEnvironment
- run create_offline_installer.py
- push
- joy

## Changing the miniconda version
- change the default version in the miniconda_installer_version method
- test locally
- push
- edit the conda-build-environment-creation pipeline, select variables and update the miniconda_installer_version variable to match the new version
- wait for the build
- joy
