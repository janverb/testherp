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
# - document .testherp.cfg file
# - make --quiet suppress most of testherp's own output
# - remember which tests failed last time (and the addon set/seed db they used?)
# - excluding tests, e.g. "account,-account.test_account_all_l10n"
# - --nuke to remove .testherp.cfg and drop the seed databases in it
#   - exclusive from other options?
# - search upward for buildout directory
# - handle data_dir?
# - tell the user if a test can't be imported? (Odoo 8)
# - unify all the monkeypatching into some snazzy decorator

from __future__ import print_function

import argparse
import collections
import contextlib
import csv
import functools
import importlib
import itertools
import os
import random
import re
import signal
import string
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
if MYPY:  # pragma: no cover
    import typing as t
    from typing import cast

    _Err = t.Union[
        t.Tuple[t.Type[BaseException], BaseException, types.TracebackType],
        t.Tuple[None, None, None],
    ]
    _SpecList = t.List[Spec]  # noqa: F821
    _SpecTuple = t.Union[t.Tuple[str], t.Tuple[str, str], t.Tuple[str, str, str]]
    if PY3:
        _Testable = t.Union[unittest.TestSuite, unittest.TestCase]
    else:
        _Testable = unittest.Testable
    from anybox.recipe.odoo.runtime.session import Session as _Session
else:

    def cast(_type, val):
        return val

    _SpecList = list


if MYPY:  # pragma: no cover
    import odoo
elif os.environ.get("PYTHON_ODOO"):
    try:
        import openerp as odoo
    except ImportError:
        import odoo
else:
    odoo = None


class MarkedTestCase(unittest.TestCase):
    def __init__(self, methodName="runTest"):  # pragma: no cover
        # type: (str) -> None
        self._odooName = ("foo", "bar", "baz")
        super(MarkedTestCase, self).__init__(methodName)
        raise TypeError("Not a real class, just for mypy")


# For tests that hang Odoo or otherwise make testing hard or impossible
# Tests that merely fail don't belong here
# Tuples of either (addon, module, method) or (addon, module)
blacklist = {
    # This one took so long that I ran out of patience before it ended
    ("account", "test_account_all_l10n"): "Would install all l10n modules"
}  # type: t.Dict[t.Union[t.Tuple[str, str], t.Tuple[str, str, str]], str]


class UserError(Exception):
    pass


class Spec(object):
    """A test specifier of the form addon[.module[.method]]."""

    def __init__(self, specstr):
        # type: (str) -> None
        parts = specstr.split(".")
        if not 1 <= len(parts) <= 3 or not all(parts):
            raise UserError("Malformed test spec {!r}".format(specstr))
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
        return isinstance(other, Spec) and self.as_tuple() == other.as_tuple()


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
        odoo.tools.config["dbfilter"] = "^" + re.escape(database) + "$"
        threading.currentThread().dbname = database  # type: ignore

        # "-" is an excluding tag that matches all tests
        # This makes it less likely Odoo starts running tests itself
        odoo.tools.config["test_tags"] = "-"

        # Avoid conflict with other instances
        # TODO: Be smarter about this, conflicts are still possible
        port = random.randint(30000, 50000)
        odoo.tools.config["http_port"] = port
        odoo.tools.config["xmlrpc_port"] = port

        try:
            # If there are no tests then odoo.tests may not be loaded
            # Of course in that case it's all pointless anyway but this
            # avoids a confusing crash
            importlib.import_module("{}.tests".format(odoo.__name__))
        except ImportError:
            pass

        # The port for testing is taken from the config at module load time
        odoo.tests.common.PORT = port
        try:
            odoo.addons.base_test_chrome.common.PORT = port  # type: ignore
        except AttributeError:
            pass

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
        if os.environ.get("TESTHERP_DEBUGGER"):
            self.patch_transactioncase_cleanup()
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

            def alter_setup_of(testclass):
                # type: (t.Any) -> None
                def new_setup(self):
                    # type: (t.Any) -> None
                    orig_setup(self)

                    def skipper(*_a, **_k):
                        # type: (object, object) -> None
                        self.skipTest(
                            "The web server is disabled, run with --server to enable"
                        )

                    self.url_open = skipper
                    self.phantom_run = skipper
                    self.phantom_js = skipper
                    if hasattr(self, "opener"):
                        self.opener.open = skipper
                        self.opener.get = skipper
                        self.opener.post = skipper
                    for attr in "xmlrpc_common", "xmlrpc_object", "xmlrpc_db":
                        if hasattr(self, attr):
                            getattr(self, attr)._ServerProxy__request = skipper

                orig_setup = testclass.setUp
                testclass.setUp = new_setup

            alter_setup_of(odoo.tests.common.HttpCase)

            try:
                alter_setup_of(
                    odoo.addons.base_test_chrome.common.HttpCase  # type: ignore
                )
            except AttributeError:
                pass

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

    def patch_transactioncase_cleanup(self):
        # type: () -> None
        """Patch TransactionCase to let us start a debugger before the cleanup.

        ``TransactionCase.setUp`` uses ``TestCase.addCleanup`` to close the
        cursor after a test run. Cleanup functions are run pretty much
        immediately after the test, or even after ``setUp`` fails, so there's
        no good way to interject before that.

        Instead we patch ``setUp`` and ``run`` to delay just that cleanup
        until after the test run, when we're finished messing around.
        """
        TransactionCase = odoo.tests.common.TransactionCase

        orig_setup = TransactionCase.setUp

        def setUp(self):
            # type: (t.Any) -> None
            orig_setup(self)

            if not getattr(self, "_cleanups", False):
                return

            self._deferred = []

            for ind, (function, args, kwargs) in enumerate(self._cleanups):
                if function.__name__ == "reset" and not args and not kwargs:

                    def deferred_reset(real_reset=function):
                        # type: (t.Callable[[], None]) -> None
                        self._deferred.append(real_reset)

                    self._cleanups[ind] = (deferred_reset, args, kwargs)

        TransactionCase.setUp = setUp  # type: ignore

        orig_run = TransactionCase.run

        def run(self, *args, **kwargs):
            # type: (t.Any, t.Any, t.Any) -> None
            try:
                orig_run(self, *args, **kwargs)
            finally:
                # if setUp throws then _deferred may not exist
                while getattr(self, "_deferred", False):
                    func = self._deferred.pop()
                    func()

        TransactionCase.run = run  # type: ignore


class OdooTextTestResult(unittest.TextTestResult):
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
            _exc_type, exc_value, exc_tb = err
            if MYPY:  # pragma: no cover
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


class OdooTextTestRunner(unittest.TextTestRunner):
    resultclass = OdooTextTestResult


class State(object):
    """The registry of seed databases."""

    def __init__(self, directory):
        # type: (str) -> None
        self.fname = os.path.join(directory, ".testherp.cfg")
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
        if any(char.isspace() for char in dbname) or len(dbname) > 63:
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
            raise UserError("Directory {!r} does not exist".format(self.base_dir))
        if not all(
            os.path.isfile(self.file_path(fname))
            for fname in ["bin/python_odoo", "bin/start_odoo", "etc/odoo.cfg"]
        ):
            raise UserError("{!r} is not a buildout directory".format(self.base_dir))

        self.config = configparser.ConfigParser()
        self.config.read(self.file_path("etc/odoo.cfg"))

        self.database = self.connect_db()

        self.tests = SpecList.from_str(specs)
        if not self.tests:
            raise UserError("No tests given")

        self.state = State(self.base_dir)

    def file_path(self, fname):
        # type: (str) -> str
        return os.path.join(self.base_dir, fname)

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

    def exists_db(self, dbname):
        # type: (str) -> bool
        with self.database.cursor() as cr:
            cr.execute(
                SQL("SELECT 1 FROM pg_database WHERE datname = %s LIMIT 1"), [dbname]
            )
            return bool(cr.fetchall())

    def run_tests(self, clean, update, env):
        # type: (bool, bool, t.Dict[str, str]) -> int
        self.ensure_db(clean=clean, update=update)
        proc = self.run_test_process(env=env)
        return proc.returncode

    def ensure_db(self, clean, update):
        # type: (bool, bool) -> None
        """Ensure that a seed database exists for the current set of tests."""
        addons = self.tests.addons()
        if addons in self.state:
            dbname = self.state[addons]
        else:
            dbname = self.generate_dbname(self.base_dir, addons)
            self.state[addons] = dbname

        if self.exists_db(dbname):
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

        self.create_db(dbname)
        print(
            "Created seed database {} for {}".format(dbname, ", ".join(sorted(addons)))
        )
        print("Installing Odoo (this may take a while)")
        try:
            self.run_odoo(dbname, ["-i", ",".join(sorted(addons))])
        except UserError:
            try:
                self.drop_db(dbname)
            except BaseException:
                print(
                    "Could not delete {}, you should probably "
                    "run with --clean next time".format(dbname)
                )
                raise
            else:
                print("Deleted failed database {}".format(dbname))
            raise
        print("Finished installing Odoo")

    def generate_dbname(self, base_dir, addons):
        # type: (str, t.FrozenSet[str]) -> str

        # It should be safe with LIKE and safe to paste unquoted into a shell
        # _ is not technically LIKE-safe but the worst possibility is that we
        # fetch too many results
        allowed_chars = set(string.ascii_letters + string.digits + "_-.,:/+=")
        dir_part = "".join(
            char for char in os.path.basename(base_dir) if char in allowed_chars
        )

        addons_part = (
            "-".join(sorted(addons))
            if len(addons) <= 3
            else "".join(addon[0] for addon in sorted(addons))
        )
        base_name = "testherp-{}-{}".format(dir_part, addons_part)

        # Database names are truncated at 63 characters
        # Let's leave room for -XXXX for the seed serial, that ought to be
        # enough for anyone
        base_name = base_name[:58]

        with self.database.cursor() as cr:
            cr.execute(
                SQL("SELECT datname FROM pg_database WHERE datname LIKE %s"),
                [base_name + "%"],
            )
            existing = {row[0] for row in cr.fetchall()}  # type: t.Set[str]
            if base_name not in existing:
                dbname = base_name
            else:
                for n in itertools.count():
                    dbname = "{}-{}".format(base_name, n)
                    if dbname not in existing:
                        break

        assert not self.exists_db(dbname)

        return dbname

    def run_test_process(self, env):
        # type: (t.Dict[str, str]) -> Popen[bytes]
        """Run an inferior process that executes the tests."""
        new_env = os.environ.copy()
        new_env["PYTHON_ODOO"] = "1"
        new_env.update(env)
        with self.temp_db() as dbname:
            return self.run_process(
                [self.file_path("bin/python_odoo"), __file__, dbname, str(self.tests)],
                env=new_env,
            )

    @contextlib.contextmanager
    def temp_db(self, no_clone=False):
        # type: (bool) -> t.Iterator[str]
        """Create a temporary database from the correct seed."""
        seed_db = self.state[self.tests.addons()]
        if no_clone:
            # Saves about half a second, probably not worth exposing as a flag
            yield seed_db
            return
        dbname_base = seed_db[:56]  # Truncated at 63
        dbname = "{}-{}".format(dbname_base, str(uuid.uuid4())[:6])
        try:
            self.create_db(dbname, seed=seed_db)
        except psycopg2.Error:
            raise UserError(
                "Can't create database from template, do you have another session open?"
            )
        print("Created temporary database {}".format(dbname))
        try:
            yield dbname
        finally:
            try:
                self.drop_db(dbname)
            except psycopg2.Error as err:
                print("Could not delete {}: {}".format(dbname, err))

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
                    self.file_path("bin/start_odoo"),
                    "--stop-after-init",
                    "--database",
                    dbname,
                    "--config",
                    config_file.name,
                ]
                + args
            )
            if proc.returncode:
                raise UserError("Odoo failed with code {}".format(proc.returncode))
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


def testherp():
    # type: () -> int
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    toggle = functools.partial(arg, action="store_true")

    arg("tests", nargs="+", help="Tests to run")
    toggle("-c", "--clean", help="Replace the seed database")
    toggle("-u", "--update", help="Update the seed database")
    toggle("-s", "--server", help="Run the web server while testing")
    toggle("-p", "--pdb", help="Launch pdb on failure or error")
    arg("-d", "--debugger", default=None, help="Launch a debugger on failure on error")
    toggle("-f", "--failfast", help="Stop on first fail or error")
    toggle("-b", "--buffer", help="Buffer stdout and stderr during tests")
    arg("-C", "--directory", default=".", help="Buildout directory to use")
    toggle("-v", "--verbose", help="Verbose test output")
    toggle("-q", "--quiet", help="Minimal test output")

    args = parser.parse_args()
    if args.clean and args.update:
        print(
            "Warning: --clean and --update are mutually exclusive. "
            "Interpreting as --clean."
        )

    debugger = "pdb" if args.pdb else args.debugger

    env = {
        "TESTHERP_VERBOSITY": str(1 + args.verbose - args.quiet),
    }
    if debugger:
        env["TESTHERP_DEBUGGER"] = debugger
    if args.server:
        env["TESTHERP_SERVER"] = "1"
    if args.failfast:
        env["TESTHERP_FAILFAST"] = "1"
    if args.buffer:
        env["TESTHERP_BUFFER"] = "1"

    manager = ProcessManager(args.directory, ",".join(args.tests))
    return manager.run_tests(clean=args.clean, update=args.update, env=env)


def main():
    # type: () -> int
    if os.environ.get("PYTHON_ODOO"):
        assert len(sys.argv) == 3
        session_obj = session  # type: ignore  # noqa: F821
        manager = TestManager(
            database=sys.argv[1], specs=sys.argv[2], session=session_obj
        )
        return manager.run_tests()
    else:
        try:
            return testherp()
        except UserError as err:
            print("{}: error: {}".format(os.path.basename(sys.argv[0]), err))
            return 1


if __name__ == "__main__":
    sys.exit(main())
