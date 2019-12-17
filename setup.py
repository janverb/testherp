import pkg_resources
import setuptools


def has_distribution(package):
    try:
        pkg_resources.get_distribution(package)
    except pkg_resources.DistributionNotFound:
        return False
    else:
        return True


# Let installers avoid building psycopg2, e.g. in CI
# Warning: this takes effect when building a wheel, not when installing it
if not has_distribution("psycopg2") and has_distribution("psycopg2-binary"):
    dependencies = ["psycopg2-binary"]
else:
    dependencies = ["psycopg2"]

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
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: GNU Affero General Public License v3 or "
        "later (AGPLv3+)",
        "Intended Audience :: Developers",
        "Environment :: Console",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development",
        "Topic :: Software Development :: Testing",
    ],
    install_requires=dependencies,
)
