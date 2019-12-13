#!/usr/bin/env python3

# This file really contains two scripts: one which handles the CLI and
# databases, and one which runs inside Odoo. They only share a little code.
# If the $PYTHON_ODOO environment variable is set the second script runs.
# They're rolled into one file so it can be a self-contained script that you
# can dump in your scripts folder and forget about. That could change in the
# future if necessary but it works for now.
# They're rolled into Java-style classes TestManager and ProcessManager to make
# it easier to tell which code belongs to which and keep their states separate.

# TODO:
# - logging? (both installation and testing)
# - --seed to override seed db
# - don't print garbage on KeyboardInterrupt
# - adopt test_tags format?
# - patch to make phantomjs raise skip if server is not running?
# - patch common.HttpCase.xmlrpc_common to raise skip?
# - use warnings or print to stderr?
# - support unittest flags (--quiet, --failfast, --catch, --buffer, --locals)
# - write tests for testherp itself
# - add some type hints for mypy

from __future__ import print_function

import argparse
import collections
import contextlib
import csv
import importlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

# For tests that hang Odoo or otherwise make testing hard or impossible
# Tests that merely fail don't belong here
# Tuples of either (addon, module, method) or (addon, module)
blacklist = {("account", "test_account_all_l10n"): "Would install all l10n modules"}


class Spec(object):
    """A test specifier of the form addon[.module[.method]]."""

    def __init__(self, specstr):
        parts = specstr.split(".")
        if not 1 <= len(parts) <= 3:
            raise ValueError("Malformed test spec {!r}".format(specstr))
        self.addon = parts[0]
        self.module = parts[1] if len(parts) >= 2 else None
        self.method = parts[2] if len(parts) >= 3 else None

    def as_tuple(self):
        if self.method is not None:
            return (self.addon, self.module, self.method)
        if self.module is not None:
            return (self.addon, self.module)
        return (self.addon,)

    def __str__(self):
        return ".".join(self.as_tuple())

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, str(self))


class SpecList(list):
    """A list of test specifiers."""

    @classmethod
    def from_str(cls, specstr):
        return cls(Spec(piece) for piece in specstr.split(",") if piece)

    def __str__(self):
        return ",".join(sorted(str(spec) for spec in self))

    def __repr__(self):
        return "{}.from_str({!r})".format(self.__class__.__name__, str(self))

    def addons(self):
        return frozenset(spec.addon for spec in self)

    def unwrap_testcases(self, suite):
        """Recursively unwrap test suites into test cases."""
        if isinstance(suite, unittest.suite.BaseTestSuite):
            for test in suite._tests:
                for case in self.unwrap_testcases(test):
                    yield case
        else:
            yield suite

    def testcases_for_module(self, module):
        """Get all test cases for a python module."""
        return self.unwrap_testcases(unittest.TestLoader().loadTestsFromModule(module))

    def collect_testcases(self, odoo):
        """Get all test cases for an Odoo addon, with a _odooName attribute."""
        for addon in sorted(self.addons()):
            for module in odoo.modules.module.get_test_modules(addon):
                mod_name = module.__name__.split(".")[-1]
                if (addon, mod_name) in blacklist:
                    print(
                        "Skipping test module {}.{} because {!r}".format(
                            addon, mod_name, blacklist[addon, mod_name]
                        )
                    )
                    continue
                for case in self.testcases_for_module(module):
                    case._odooName = (addon, mod_name, case._testMethodName)  # dirty
                    yield case


class TestManager(object):
    """The object used inside the python_odoo process to run the tests."""

    def __init__(self, database, specs, session):
        try:
            import openerp as odoo
        except ImportError:
            import odoo
        self.odoo = odoo

        session.open(database)
        # Some tests run queries that block if there are other cursors
        session.cr.close()

        # Odoo 10 gets confused if we don't do this
        odoo.tools.config["db_name"] = database
        threading.currentThread().dbname = database

        # odoo.cli.server.start() does this, at least one test depends on it
        csv.field_size_limit(500 * 1024 * 1024)

        self.specs = SpecList.from_str(specs)

    def run_tests(self):
        cases = collections.defaultdict(list)
        for case in self.specs.collect_testcases(self.odoo):
            addon, module, method = case._odooName
            cases[addon, module, method].append(case)
            cases[addon, module].append(case)
            cases[(addon,)].append(case)
        testcases = []
        for spec in self.specs:
            to_add = cases[spec.as_tuple()]
            for case in to_add:
                self.mark_skip(case)
            if not to_add:
                print("Warning: can't find test {}".format(spec))
            testcases.extend(to_add)
        suite = unittest.TestSuite(testcases)
        verbosity = 2 if os.environ.get("TESTHERP_VERBOSE") else 1
        with self.run_server():
            result = OdooTextTestRunner(verbosity=verbosity).run(suite)
            return min(len(result.errors) + len(result.failures), 255)

    def mark_skip(self, case):
        """Skip tests that should never run."""
        # TODO: make compatible with later Odoo versions
        cls = type(case)
        method = getattr(cls, case._testMethodName, None)
        if case._odooName in blacklist:
            method.__unittest_skip__ = True
            method.__unittest_skip_why__ = blacklist[case._odooName]
            return
        at_install = getattr(cls, "at_install", True)
        at_install = getattr(method, "at_install", at_install)
        post_install = getattr(cls, "post_install", False)
        post_install = getattr(method, "post_install", post_install)
        if not (at_install or post_install):
            # Mostly tests for the at_install/post_install mechanism itself
            method.__unittest_skip__ = True
            method.__unittest_skip_why__ = "Test is not at_install or post_install"

    @contextlib.contextmanager
    def run_server(self):
        """Run a server in the background if enabled."""
        if not os.environ.get("TESTHERP_SERVER"):
            yield None
            return
        server = self.odoo.service.server.ThreadedServer(
            self.odoo.service.wsgi_server.application
        )
        server.start()
        try:
            yield server
        finally:
            server.stop()


class OdooTextTestResult(unittest.TextTestResult):
    """Format tests as Specs and launch a debugger when appropriate."""

    def getDescription(self, test):
        if hasattr(test, "_odooName"):
            return ".".join(test._odooName)
        return super(OdooTextTestResult, self).getDescription(test)

    def addError(self, test, err):
        super(OdooTextTestResult, self).addError(test, err)
        self.perhapsPdb(err)

    def addFailure(self, test, err):
        super(OdooTextTestResult, self).addFailure(test, err)
        self.perhapsPdb(err)

    def perhapsPdb(self, err):
        debugger = os.environ.get("TESTHERP_DEBUGGER")
        if debugger:
            dbg = importlib.import_module(debugger)
            print()
            print(repr(err[1]))
            dbg.post_mortem(err[2])

    def startTest(self, test):
        try:
            import openerp as odoo
        except ImportError:
            import odoo
        threading.currentThread().testing = True
        odoo.tools.config["test_enable"] = True
        return super(OdooTextTestResult, self).startTest(test)

    def stopTest(self, test):
        try:
            import openerp as odoo
        except ImportError:
            import odoo
        threading.currentThread().testing = False
        odoo.tools.config["test_enable"] = False
        return super(OdooTextTestResult, self).stopTest(test)


class OdooTextTestRunner(unittest.TextTestRunner):
    resultclass = OdooTextTestResult


class State(dict):
    """The registry of seed databases."""

    @classmethod
    def read(cls, directory):
        fname = os.path.join(directory, ".testherp")
        state = cls()
        if not os.path.isfile(fname):
            return state
        with open(fname) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                key, value = line.split("=")
                state[frozenset(key.strip().split(","))] = value.strip()
        return state

    def write(self, directory):
        entries = [
            (",".join(sorted(addons)), dbname) for addons, dbname in self.items()
        ]
        # Fewest addons first, then alphabetical
        entries.sort(key=lambda entry: (entry[0].count(","), entry[0]))
        with open(os.path.join(directory, ".testherp"), "w") as f:
            for entry in entries:
                print("{} = {}".format(*entry), file=f)


class ProcessManager(object):
    """The object that manages databases and inferior processes."""

    def __init__(self, base_dir, specs):
        self.base_dir = os.path.abspath(base_dir)
        if not os.path.isdir(self.base_dir):
            raise ValueError("Directory {!r} does not exist".format(self.base_dir))
        self.python_odoo = os.path.join(self.base_dir, "bin/python_odoo")
        self.start_odoo = os.path.join(self.base_dir, "bin/start_odoo")
        if not os.path.isfile(self.python_odoo):
            raise ValueError("{!r} is not a buildout directory".format(self.base_dir))

        self.config = configparser.ConfigParser()
        with open(os.path.join(self.base_dir, "etc/odoo.cfg")) as f:
            self.config.read_file(f)

        self.tests = SpecList.from_str(specs)

        self.state = State.read(self.base_dir)

    def run_tests(
        self,
        clean=False,
        update=False,
        server=False,
        verbose=False,
        keep=False,
        debugger=None,
    ):
        self.ensure_db(clean=clean, update=update)
        proc = self.run_test_process(
            keep=keep, server=server, verbose=verbose, debugger=debugger
        )
        return proc.returncode

    def ensure_db(self, clean=False, update=False):
        """Ensure that a seed database exists for the current set of tests."""
        addons = self.tests.addons()
        if addons in self.state:
            old_dbname = self.state[addons]
            if clean:
                print("Deleting old database {}".format(old_dbname))
                proc = self.run_process(["dropdb", old_dbname], check=False)
                if proc.returncode:
                    print("Could not delete {}".format(old_dbname))
            elif update:
                print("Updating {}".format(", ".join(sorted(addons))))
                self.run_odoo(old_dbname, ["-u", ",".join(addons)])
                return
            else:
                return
        dbname = "testherp-seed-{}".format(uuid.uuid4())
        print("Creating database {} for {}".format(dbname, ", ".join(sorted(addons))))
        self.run_process(["createdb", dbname])
        print("Installing Odoo (this may take a while)")
        self.run_odoo(dbname, ["-i", ",".join(addons)])
        print("Finished installing Odoo")
        self.state[addons] = dbname
        self.state.write(self.base_dir)

    def run_test_process(self, keep=False, server=False, verbose=False, debugger=None):
        """Run an inferior process that executes the tests."""
        env = os.environ.copy()
        env["PYTHON_ODOO"] = "1"
        if server:
            env["TESTHERP_SERVER"] = "1"
        if verbose:
            env["TESTHERP_VERBOSE"] = "1"
        if debugger:
            env["TESTHERP_DEBUGGER"] = debugger
        with self.temp_db(keep=keep) as dbname:
            return self.run_process(
                [self.python_odoo, __file__, dbname, str(self.tests)],
                env=env,
                check=False,
            )

    @contextlib.contextmanager
    def temp_db(self, keep=False, no_clone=False):
        """Create a temporary database from the correct seed."""
        seed_db = self.state[self.tests.addons()]
        if no_clone:
            # Saves about half a second, probably not worth exposing as a flag
            yield seed_db
            return
        dbname = "testherp-temp-{}".format(uuid.uuid4())
        proc = self.run_process(["createdb", dbname, "-T", seed_db], check=False)
        if proc.returncode:
            raise RuntimeError(
                "Can't create database from template, try running with --clean"
            )
        print("Created temporary database {}".format(dbname))
        try:
            yield dbname
        finally:
            if keep:
                print("Keeping database {}".format(dbname))
            else:
                proc = self.run_process(["dropdb", dbname], check=False)
                if proc.returncode:
                    print("Could not delete {}".format(dbname))

    def run_odoo(self, dbname, args, loglevel="warn"):
        """Run Odoo without the baggage of the full etc/odoo.cfg."""
        with tempfile.NamedTemporaryFile(mode="w") as config_file:
            # We use a temporary config file instead of passing these as flags
            # because addons_path may break otherwise
            # Relative paths are resolved relative to parts/odoo/odoo if
            # they're in the config file, but relative to the current directory
            # if they're passed as flags
            # Invalid entries are ignored if they're in the config file but
            # cause an error if they're passed as flags

            # TODO: perhaps db_user, etc. should be copied from the original
            temp_config = configparser.ConfigParser()
            temp_config.add_section("options")
            temp_config.set(
                "options", "addons_path", self.config.get("options", "addons_path")
            )
            temp_config.set("options", "max_cron_threads", "0")
            temp_config.set("options", "log_level", loglevel)
            temp_config.write(config_file)
            config_file.flush()
            return self.run_process(
                [
                    self.start_odoo,
                    "--stop-after-init",
                    "--database=" + dbname,
                    "--config=" + config_file.name,
                ]
                + args
            )

    def run_process(self, argv, env=None, check=True):
        """Run an inferior process to completion."""
        proc = subprocess.Popen(argv, env=env)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGTERM)
                proc.wait()
        if check and proc.returncode:
            raise RuntimeError(
                "Process {} failed with code {}".format(argv, proc.returncode)
            )
        return proc


def testherp(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser()
    arg = parser.add_argument

    arg("tests", nargs="+", help="Tests to run")
    arg("-C", "--directory", default=".", help="Buildout directory to use")
    arg("-p", "--pdb", action="store_true", help="Launch pdb on failure or error")
    arg("-d", "--debugger", default=None, help="Launch a debugger on failure on error")
    arg("-c", "--clean", action="store_true", help="Replace the seed database")
    arg("-u", "--update", action="store_true", help="Update the seed database")
    arg("-k", "--keep", action="store_true", help="Don't delete the testing database")
    arg("-v", "--verbose", action="store_true", help="Verbose test output")
    # The server is hard to kill and isn't necessary for most tests
    # It is necessary for PhantomJS tests but those are slow and annoying
    # So we don't enable it by default
    arg("-s", "--server", action="store_true", help="Run the XML RPC server")

    args = parser.parse_args(argv)
    if args.clean and args.update:
        print(
            "Warning: --clean and --update are mutually exclusive. "
            "Interpreting as --clean."
        )

    debugger = "pdb" if args.pdb else args.debugger

    manager = ProcessManager(args.directory, ",".join(args.tests))
    return manager.run_tests(
        clean=args.clean,
        update=args.update,
        server=args.server,
        verbose=args.verbose,
        debugger=debugger,
        keep=args.keep,
    )


def main(argv=sys.argv[1:]):
    # TODO: handle common exceptions in a user-friendly way
    if os.environ.get("PYTHON_ODOO"):
        assert len(sys.argv) == 3
        manager = TestManager(
            database=sys.argv[1], specs=sys.argv[2], session=session  # noqa: F821
        )
        return manager.run_tests()
    else:
        return testherp(argv)


if __name__ == "__main__":
    sys.exit(main())
