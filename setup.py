import os
from pathlib import Path
from setuptools import setup, find_packages


setup(
    name='querymodelling',
    version=os.getenv("github.ref"),
    author='George Haddad',
    description='build consistent api query models for fastapi',
    long_description_content_type='text/markdown',
    long_description=Path('README.md', encoding='utf-8').read_text(),
    packages=find_packages(),
    include_package_data=True,
    license="Apache Software License (Apache 2.0)",
    keywords=[
        'python', 'fastapi', 'sqlmodel', 'query', 'model', 'api', 'querymodel'
    ],
    classifiers=[
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        'Development Status :: 4 - Beta',
    ]
)