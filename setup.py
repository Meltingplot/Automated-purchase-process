from setuptools import setup, find_packages

setup(
    name="procurement_ai",
    version="0.1.0",
    description="AI-powered procurement automation for ERPNext",
    author="Meltingplot GmbH",
    author_email="info@meltingplot.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=[],  # dependencies declared in pyproject.toml
)
