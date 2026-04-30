"""Setup file for code_execution package."""

from setuptools import find_packages, setup

setup(
    name="code_execution",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[],  # Dependencies are in each server's requirements.txt
)
