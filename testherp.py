#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# This file really contains two scripts: one which handles the CLI and
# databases, and one which runs inside Odoo. They only share a little code.
# If the $PYTHON_ODOO environment variable is set the second script runs.
# They're rolled into one file so it can be a self-contained file that you
# can dump in your scripts folder and forget about. That could change in the
# future if necessary but it works for now.
# They're wrapped in Java-style classes TestManager and ProcessManager to make
# it easier to tell which code belongs to which and keep their states separate.

# TODO:
# - logging? (both installation and testing)
#   - unconditionally log to stdout
#   - set default log level to warn regardless of config, make this customizable
# - --seed to override seed db
# - don't print garbage on KeyboardInterrupt
# - adopt test_tags format?
# - use warnings or print to stderr?
# - support unittest flags (--catch, --locals)
# - write more tests for testherp itself
# - document .testherp file
# - make --quiet suppress most of testherp's own output
# - remember which tests failed last time (and the addon set/seed db they used?)
# - excluding tests, e.g. "account,-account.test_account_all_l10n"
# - --nuke to remove .testherp and drop the seed databases in it
#   - exclusive from other options?
# - search upward for buildout directory
# - detect when databases don't exist
# - handle data_dir?

from __future__ import print_function

import argparse
import collections
import contextlib
import csv
import functools
import importlib
import os
import random
import signal
import sys
import tempfile
import threading
import types
import unittest
import uuid

from subprocess import Popen

# We only need psycopg2 in the controller process, but it's also available in
# the Odoo process because Odoo uses it so no need to guard it
import psycopg2

from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2.sql import Identifier, SQL

PY3 = sys.version_info >= (3, 0)

if PY3:
    import configparser
else:
    import ConfigParser as configparser


MYPY = False
if MYPY:
    import typing as t
    from typing import cast

    _Err = t.Tuple[
        t.Union[t.Type[BaseException], None],
        t.Union[BaseException, None],
        t.Union[types.TracebackType, None],
    ]
    _SpecList = t.List[Spec]  # noqa: F821
    _SpecTuple = t.Union[t.Tuple[str], t.Tuple[str, str], t.Tuple[str, str, str]]
    if PY3:
        _Testable = t.Union[unittest.TestSuite, unittest.TestCase]
    else:
        _Testable = unittest.Testable
    from anybox.recipe.odoo.runtime.session import Session as _Session
else:

    def cast(typ, val):
        return val

    _SpecList = list

if MYPY and not PY3:
    # The Python 2 unittest typeshed is incomplete
    TextTestResult = t.Any
    TextTestRunner = t.Any
else:
    from unittest import TextTestResult
    from unittest import TextTestRunner


if MYPY:
    import odoo
elif os.environ.get("PYTHON_ODOO"):
    try:
        import openerp as odoo
    except ImportError:
        import odoo
else:
    odoo = None


class MarkedTestCase(unittest.TestCase):
    def __init__(self):
        # type: () -> None
        self._odooName = ("foo", "bar", "baz")
        raise TypeError("Not a real class, just for mypy")


# For tests that hang Odoo or otherwise make testing hard or impossible
# Tests that merely fail don't belong here
# Tuples of either (addon, module, method) or (addon, module)
blacklist = {
    # This one took so long that I ran out of patience before it ended
    ("account", "test_account_all_l10n"): "Would install all l10n modules"
}  # type: t.Dict[t.Union[t.Tuple[str, str], t.Tuple[str, str, str]], str]


def _quote(identifier):
    # type: (str) -> str
    return '"{}"'.format(identifier.replace('"', '""'))


class TestherpError(Exception):
    pass


class Spec(object):
    """A test specifier of the form addon[.module[.method]]."""

    def __init__(self, specstr):
        # type: (str) -> None
        parts = specstr.split(".")
        if not 1 <= len(parts) <= 3 or not all(parts):
            raise TestherpError("Malformed test spec {!r}".format(specstr))
        self.addon = parts[0]
        self.module = parts[1] if len(parts) >= 2 else None
        self.method = parts[2] if len(parts) >= 3 else None

    def as_tuple(self):
        # type: () -> _SpecTuple
        if self.method is not None:
            assert self.module is not None
            return (self.addon, self.module, self.method)
        if self.module is not None:
            return (self.addon, self.module)
        return (self.addon,)

    def __str__(self):
        # type: () -> str
        return ".".join(self.as_tuple())

    def __repr__(self):
        # type: () -> str
        return "{}({!r})".format(self.__class__.__name__, str(self))

    def __eq__(self, other):
        # type: (object) -> bool
        return (
            isinstance(other, Spec)
            and self.addon == other.addon
            and self.module == other.module
            and self.method == other.method
        )


class SpecList(_SpecList):
    """A list of test specifiers."""

    @classmethod
    def from_str(cls, specstr):
        # type: (str) -> SpecList
        return cls(Spec(piece) for piece in specstr.split(",") if piece)

    def __str__(self):
        # type: () -> str
        return ",".join(str(spec) for spec in self)

    def __repr__(self):
        # type: () -> str
        return "{}.from_str({!r})".format(self.__class__.__name__, str(self))

    def addons(self):
        # type: () -> t.FrozenSet[str]
        return frozenset(spec.addon for spec in self)

    def unwrap_testcases(self, suite):
        # type: (_Testable) -> t.Iterator[unittest.TestCase]
        """Recursively unwrap test suites into test cases."""
        if isinstance(suite, unittest.TestCase):
            yield suite
        else:
            assert isinstance(suite, unittest.TestSuite)
            for test in suite:
                for case in self.unwrap_testcases(test):
                    yield case

    def testcases_for_module(self, module):
        # type: (types.ModuleType) -> t.Iterator[unittest.TestCase]
        """Get all test cases for a python module."""
        return self.unwrap_testcases(unittest.TestLoader().loadTestsFromModule(module))

    def collect_testcases(self):
        # type: () -> t.Iterator[MarkedTestCase]
        """Get all test cases for an Odoo addon, with a _odooName attribute added."""
        for addon in sorted(self.addons()):
            for module in odoo.modules.module.get_test_modules(addon):
                mod_name = module.__name__.split(".")[-1]
                if (addon, mod_name) in blacklist:
                    # Merely marking the test cases as skip wouldn't stop
                    # setupClass from running
                    print(
                        "Skipping test module {}.{} because {!r}".format(
                            addon, mod_name, blacklist[addon, mod_name]
                        )
                    )
                    continue
                for case in self.testcases_for_module(module):
                    marked = cast(MarkedTestCase, case)
                    marked._odooName = (addon, mod_name, case._testMethodName)
                    yield marked


class TestManager(object):
    """The object used inside the python_odoo process to run the tests."""

    def __init__(self, database, specs, session):
        # type: (str, str, _Session) -> None
        session.open(database)
        # Some tests run queries that block if there are other cursors
        session.cr.close()

        # Odoo 10 gets confused if we don't do this
        odoo.tools.config["db_name"] = database
        threading.currentThread().dbname = database  # type: ignore

        # "-" is an excluding tag that matches all tests
        # This makes it less likely Odoo starts running tests itself
        odoo.tools.config["test_tags"] = "-"

        # Avoid conflict with other instances
        odoo.tools.config["http_port"] = random.randint(30000, 50000)

        # odoo.cli.server.start() does this, at least one test depends on it
        csv.field_size_limit(500 * 1024 * 1024)

        self.specs = SpecList.from_str(specs)

    def run_tests(self):
        # type: () -> int
        cases = collections.defaultdict(
            list
        )  # type: t.DefaultDict[_SpecTuple, t.List[MarkedTestCase]]
        for case in self.specs.collect_testcases():
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
        verbosity = int(os.environ.get("TESTHERP_VERBOSITY") or "1")
        failfast = bool(os.environ.get("TESTHERP_FAILFAST"))
        buffer = bool(os.environ.get("TESTHERP_BUFFER"))
        runner = OdooTextTestRunner(
            verbosity=verbosity, failfast=failfast, buffer=buffer
        )
        with self.run_server():
            result = runner.run(suite)
            return min(len(result.errors) + len(result.failures), 125)

    def mark_skip(self, case):
        # type: (MarkedTestCase) -> None
        """Skip tests that should never run."""
        cls = type(case)
        method = getattr(cls, case._testMethodName, None)
        method = getattr(method, "__func__", method)
        if case._odooName in blacklist:
            method.__unittest_skip__ = True
            method.__unittest_skip_why__ = blacklist[case._odooName]
            return
        if odoo.release.version_info >= (12, 0):
            return
        # In earlier Odoo versions tests are at_install or post_install and
        # should never run if they're neither
        at_install = getattr(cls, "at_install", True)
        at_install = getattr(method, "at_install", at_install)
        post_install = getattr(cls, "post_install", False)
        post_install = getattr(method, "post_install", post_install)
        if not (at_install or post_install):
            method.__unittest_skip__ = True
            method.__unittest_skip_why__ = "Test is not at_install or post_install"

    @contextlib.contextmanager
    def run_server(self):
        # type: () -> t.Iterator[None]
        """Run a server in the background if enabled."""
        if not os.environ.get("TESTHERP_SERVER"):
            orig_setup = odoo.tests.common.HttpCase.setUp

            def new_setup(self):
                # type: (t.Any) -> None
                orig_setup(self)

                def skipper(*a, **k):
                    # type: (t.Any, t.Any) -> None
                    self.skipTest(
                        "The web server is disabled, run with --server to enable"
                    )

                self.url_open = skipper
                self.phantom_run = skipper
                if hasattr(self, "opener"):
                    self.opener.open = skipper
                    self.opener.get = skipper
                    self.opener.post = skipper
                for attr in "xmlrpc_common", "xmlrpc_object", "xmlrpc_db":
                    if hasattr(self, attr):
                        getattr(self, attr)._ServerProxy__request = skipper

            odoo.tests.common.HttpCase.setUp = new_setup  # type: ignore

            yield None
        else:
            server = odoo.service.server.ThreadedServer(
                odoo.service.wsgi_server.application
            )
            server.start()
            try:
                yield
            finally:
                server.stop()


class OdooTextTestResult(TextTestResult):
    """Format tests as Specs and launch a debugger when appropriate."""

    def getDescription(self, test):
        # type: (unittest.TestCase) -> str
        if hasattr(test, "_odooName"):
            return ".".join(test._odooName)  # type: ignore
        return super(OdooTextTestResult, self).getDescription(test)

    def addError(self, test, err):
        # type: (unittest.TestCase, _Err) -> None
        super(OdooTextTestResult, self).addError(test, err)
        self.perhapsPdb(err)

    def addFailure(self, test, err):
        # type: (unittest.TestCase, _Err) -> None
        super(OdooTextTestResult, self).addFailure(test, err)
        self.perhapsPdb(err)

    def perhapsPdb(self, err):
        # type: (_Err) -> None
        debugger = os.environ.get("TESTHERP_DEBUGGER")
        if debugger:
            exc_type, exc_value, exc_tb = err
            if MYPY:
                import pdb as dbg
            else:
                dbg = importlib.import_module(debugger)
            print()
            print(repr(exc_value))
            dbg.post_mortem(exc_tb)

    def startTest(self, test):
        # type: (unittest.TestCase) -> None
        threading.currentThread().testing = True  # type: ignore
        odoo.tools.config["test_enable"] = True
        super(OdooTextTestResult, self).startTest(test)

    def stopTest(self, test):
        # type: (unittest.TestCase) -> None
        threading.currentThread().testing = False  # type: ignore
        odoo.tools.config["test_enable"] = False
        super(OdooTextTestResult, self).stopTest(test)


class OdooTextTestRunner(TextTestRunner):
    resultclass = OdooTextTestResult


class State(object):
    """The registry of seed databases."""

    def __init__(self, directory):
        # type: (str) -> None
        self.fname = os.path.join(directory, ".testherp")
        self.state = {}  # type: t.Dict[t.FrozenSet[str], str]
        if not os.path.isfile(self.fname):
            return
        with open(self.fname) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, value = line.split("=")
                addons = frozenset(
                    filter(None, (addon.strip() for addon in key.split(",")))
                )
                self.state[addons] = value.strip()

    def __getitem__(self, addons):
        # type: (t.FrozenSet[str]) -> str
        return self.state[addons]

    def __contains__(self, addons):
        # type: (t.FrozenSet[str]) -> bool
        return addons in self.state

    def __setitem__(self, addons, dbname):
        # type: (t.FrozenSet[str], str) -> None
        if addons in self.state:
            raise ValueError(
                "An entry for {} already exists".format(", ".join(sorted(addons)))
            )
        if any(char.isspace() for char in dbname) or len(dbname) > 64:
            raise ValueError("{!r} is not a safe database name".format(dbname))
        with open(self.fname, "a") as f:
            f.write("{} = {}\n".format(",".join(sorted(addons)), dbname))
        self.state[addons] = dbname

    def __len__(self):
        # type: () -> int
        return len(self.state)

    def __eq__(self, other):
        # type: (object) -> bool
        return (
            isinstance(other, State)
            and self.fname == other.fname
            and self.state == other.state
        )


class ProcessManager(object):
    """The object that manages databases and inferior processes."""

    def __init__(self, base_dir, specs):
        # type: (str, str) -> None
        self.base_dir = os.path.abspath(base_dir)
        if not os.path.isdir(self.base_dir):
            raise TestherpError("Directory {!r} does not exist".format(self.base_dir))
        self.python_odoo = os.path.join(self.base_dir, "bin/python_odoo")
        self.start_odoo = os.path.join(self.base_dir, "bin/start_odoo")
        self.odoo_cfg = os.path.join(self.base_dir, "etc/odoo.cfg")
        if not all(
            os.path.isfile(fname)
            for fname in [self.python_odoo, self.start_odoo, self.odoo_cfg]
        ):
            raise TestherpError(
                "{!r} is not a buildout directory".format(self.base_dir)
            )

        self.config = configparser.ConfigParser()
        self.config.read(self.odoo_cfg)

        self.database = self.connect_db()

        self.tests = SpecList.from_str(specs)
        if not self.tests:
            raise TestherpError("No tests given")

        self.state = State(self.base_dir)

    def connect_db(self):
        # type: () -> psycopg2.extensions.connection
        kwargs = {}
        for option in "host", "port", "user", "password":
            if self.config.has_option("options", "db_" + option):
                value = self.config.get(
                    "options", "db_" + option
                )  # type: t.Optional[str]
                if value in {"false", "False"}:
                    value = None
            else:
                value = None
            kwargs[option] = value
        connection = psycopg2.connect(**kwargs)
        connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        return connection

    def create_db(self, dbname, seed=None):
        # type: (str, t.Optional[str]) -> None
        if seed:
            with self.database.cursor() as cr:
                cr.execute(
                    SQL("CREATE DATABASE {} WITH TEMPLATE {}").format(
                        Identifier(dbname), Identifier(seed)
                    )
                )
        else:
            with self.database.cursor() as cr:
                cr.execute(SQL("CREATE DATABASE {}").format(Identifier(dbname)))

    def drop_db(self, dbname):
        # type: (str) -> None
        with self.database.cursor() as cr:
            cr.execute(SQL("DROP DATABASE {}").format(Identifier(dbname)))

    def run_tests(self, clean, update, verbosity, debugger, **flags):
        # type: (bool, bool, int, str, bool) -> int
        self.ensure_db(clean=clean, update=update)
        proc = self.run_test_process(verbosity=verbosity, debugger=debugger, **flags)
        return proc.returncode

    def ensure_db(self, clean, update):
        # type: (bool, bool) -> None
        """Ensure that a seed database exists for the current set of tests."""
        addons = self.tests.addons()
        if addons in self.state:
            dbname = self.state[addons]
            if clean:
                try:
                    self.drop_db(dbname)
                except psycopg2.Error:
                    print("Could not delete {}".format(dbname))
                else:
                    print("Deleted old database {}".format(dbname))
            elif update:
                print("Updating {} in {}".format(", ".join(sorted(addons)), dbname))
                self.run_odoo(dbname, ["-u", ",".join(addons)])
                return
            else:
                return
        else:
            dbname = "testherp-seed-{}".format(uuid.uuid4())
            self.state[addons] = dbname
        self.create_db(dbname)
        print(
            "Created seed database {} for {}".format(dbname, ", ".join(sorted(addons)))
        )
        print("Installing Odoo (this may take a while)")
        self.run_odoo(dbname, ["-i", ",".join(sorted(addons))])
        print("Finished installing Odoo")

    def run_test_process(self, keep, server, failfast, buffer, verbosity, debugger):
        # type: (bool, bool, bool, bool, int, t.Optional[str]) -> Popen[bytes]
        """Run an inferior process that executes the tests."""
        env = os.environ.copy()
        env["PYTHON_ODOO"] = "1"
        env["TESTHERP_VERBOSITY"] = str(verbosity)
        if debugger:
            env["TESTHERP_DEBUGGER"] = debugger
        if server:
            env["TESTHERP_SERVER"] = "1"
        if failfast:
            env["TESTHERP_FAILFAST"] = "1"
        if buffer:
            env["TESTHERP_BUFFER"] = "1"
        with self.temp_db(keep=keep) as dbname:
            return self.run_process(
                [self.python_odoo, __file__, dbname, str(self.tests)], env=env
            )

    @contextlib.contextmanager
    def temp_db(self, keep=False, no_clone=False):
        # type: (bool, bool) -> t.Iterator[str]
        """Create a temporary database from the correct seed."""
        seed_db = self.state[self.tests.addons()]
        if no_clone:
            # Saves about half a second, probably not worth exposing as a flag
            yield seed_db
            return
        dbname = "testherp-temp-{}".format(uuid.uuid4())
        try:
            self.create_db(dbname, seed=seed_db)
        except psycopg2.Error:
            raise TestherpError(
                "Can't create database from template, try running with --clean"
            )
        print("Created temporary database {}".format(dbname))
        try:
            yield dbname
        finally:
            if keep:
                print("Keeping database {}".format(dbname))
            else:
                try:
                    self.drop_db(dbname)
                except psycopg2.Error:
                    print("Could not delete {}".format(dbname))

    def run_odoo(self, dbname, args, loglevel="warn"):
        # type: (str, t.List[str], str) -> Popen[bytes]
        """Run Odoo without the baggage of the full etc/odoo.cfg."""
        with tempfile.NamedTemporaryFile(suffix=".cfg", mode="w") as config_file:
            # We use a temporary config file instead of passing these as flags
            # because addons_path may break otherwise
            # Relative paths are resolved relative to parts/odoo/odoo if
            # they're in the config file, but relative to the current directory
            # if they're passed through the flag
            # Invalid entries are ignored if they're in the config file but
            # cause an error if they're passed through the flag

            temp_config = configparser.ConfigParser()
            temp_config.add_section("options")
            temp_config.set(
                "options", "addons_path", self.config.get("options", "addons_path")
            )
            temp_config.set("options", "max_cron_threads", "0")
            temp_config.set("options", "log_level", loglevel)
            for option in "db_user", "db_password", "db_host", "db_port", "data_dir":
                if self.config.has_option("options", option):
                    temp_config.set(
                        "options", option, self.config.get("options", option)
                    )
            temp_config.write(config_file)
            config_file.flush()
            proc = self.run_process(
                [
                    self.start_odoo,
                    "--stop-after-init",
                    "--database",
                    dbname,
                    "--config",
                    config_file.name,
                ]
                + args
            )
            if proc.returncode:
                raise TestherpError("Odoo failed with code {}".format(proc.returncode))
            return proc

    def run_process(self, argv, env=None):
        # type: (t.List[str], t.Optional[t.Dict[str, str]]) -> Popen[bytes]
        """Run an inferior process to completion."""
        proc = Popen(argv, env=env)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGTERM)
                proc.wait()
        return proc


def testherp(argv=sys.argv[1:]):
    # type: (t.List[str]) -> int
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    toggle = functools.partial(arg, action="store_true")

    arg("tests", nargs="+", help="Tests to run")
    toggle("-c", "--clean", help="Replace the seed database")
    toggle("-u", "--update", help="Update the seed database")
    toggle("-k", "--keep", help="Don't delete the temporary database")
    toggle("-s", "--server", help="Run the web server while testing")
    toggle("-p", "--pdb", help="Launch pdb on failure or error")
    arg("-d", "--debugger", default=None, help="Launch a debugger on failure on error")
    toggle("-f", "--failfast", help="Stop on first fail or error")
    toggle("-b", "--buffer", help="Buffer stdout and stderr during tests")
    arg("-C", "--directory", default=".", help="Buildout directory to use")
    toggle("-v", "--verbose", help="Verbose test output")
    toggle("-q", "--quiet", help="Minimal test output")

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
        verbosity=1 + args.verbose - args.quiet,
        debugger=debugger,
        keep=args.keep,
        failfast=args.failfast,
        buffer=args.buffer,
    )


def main(argv=sys.argv[1:]):
    # type: (t.List[str]) -> int
    if os.environ.get("PYTHON_ODOO"):
        assert len(sys.argv) == 3
        session_obj = session  # type: ignore  # noqa: F821
        manager = TestManager(
            database=sys.argv[1], specs=sys.argv[2], session=session_obj
        )
        return manager.run_tests()
    else:
        try:
            return testherp(argv)
        except TestherpError as err:
            print("{}: error: {}".format(os.path.basename(sys.argv[0]), err))
            return 1


if __name__ == "__main__":
    sys.exit(main())
