from setuptools import setup, find_packages

setup(
    name="purchase_automation",
    version="0.1.0",
    description="Automated purchase process for ERPNext with dual-model LLM document extraction",
    author="Meltingplot",
    author_email="info@meltingplot.net",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=[
        "pydantic>=2.0",
        "anthropic>=0.40.0",
        "openai>=1.50.0",
        "httpx>=0.27.0",
        "PyMuPDF>=1.24.0",
        "Pillow>=10.0.0",
    ],
)
