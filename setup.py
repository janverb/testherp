import setuptools

setuptools.setup(
    name="testherp",
    py_modules=["testherp"],
    entry_points={"console_scripts": ["testherp = testherp:main"]},
    version="0.0.1",
    author="Jan Verbeek",
    author_email="jverbeek@therp.nl",
    description="Granular test runner for Odoo buildouts",
    license="AGPLv3+",
    classifiers=[
        "Framework :: Odoo",
        "Framework :: Buildout",
        "Development Status :: 2 - Pre-Alpha",
        "License :: OSI Approved :: GNU Affero General Public License v3 or "
        "later (AGPLv3+)",
        "Intended Audience :: Developers",
        "Environment :: Console",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development",
        "Topic :: Software Development :: Testing",
    ],
)
