#!/usr/bin/env python3
# TODO:
# - improve formatting to make singling out test cases easier
# - logging?
# - --seed to override seed db
# - xmlrpc tests
# - don't print garbage on KeyboardInterrupt
# - fix for Odoo 12
# - make test_tags not fail
# - make test_tags useful
# - adopt test_tags format
# - patch to make phantomjs raise skip if server is not running?

from __future__ import print_function

import argparse
import atexit
import collections
import contextlib
import csv
import os
import pdb
import random
import signal
import subprocess
import sys
import threading
import unittest
import uuid

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

odoo = None

base_dir = None
python_odoo = None
start_odoo = None
config_file = None
state = None


def initialize_odoo(database):
    global odoo
    try:
        import openerp as odoo
    except ImportError:
        import odoo
    session.open(database)
    atexit.register(session.cr.close)
    # odoo.cli.server.start() does this, at least one test depends on it
    csv.field_size_limit(500 * 1024 * 1024)


def unwrap_testcases(suite):
    if isinstance(suite, unittest.suite.BaseTestSuite):
        for test in suite._tests:
            for case in unwrap_testcases(test):
                yield case
    else:
        yield suite


class OdooTextTestResult(unittest.TextTestResult):
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
        if os.environ.get("TESTHERP_PDB"):
            print(repr(err[1]))
            pdb.post_mortem(err[2])


class OdooTextTestRunner(unittest.TextTestRunner):
    resultclass = OdooTextTestResult


# These tests hang Odoo for some reason - these should be considered bugs
# Tests that merely fail don't belong here
blacklist = {("base", "test_ir_actions", "test_create_unique")}


def should_run_test(case):
    """Check whether a test should be run at all."""
    # TODO: make compatible with later Odoo versions
    cls = type(case)
    method = getattr(cls, case._testMethodName, None)
    at_install = getattr(cls, "at_install", True)
    post_install = getattr(cls, "post_install", False)
    at_install = getattr(method, "at_install", at_install)
    post_install = getattr(method, "post_install", post_install)
    return at_install or post_install


def run_tests(tests):
    specs = tests.split(",")
    addons = {spec.split(".")[0] for spec in specs}
    cases = collections.defaultdict(list)
    for addon in addons:
        for module in odoo.modules.module.get_test_modules(addon):
            for case in unwrap_testcases(
                unittest.TestLoader().loadTestsFromModule(module)
            ):
                mod_name = module.__name__.split(".")[-1]
                case._odooName = (addon, mod_name, case._testMethodName)  # dirty
                if not should_run_test(case) or case._odooName in blacklist:
                    print("Skipping {}".format(".".join(case._odooName)))
                    continue
                cases[addon, mod_name, case._testMethodName].append(case)
                cases[addon, mod_name].append(case)
                cases[addon,].append(case)
    testcases = []
    for spec in specs:
        # Duplication is possible here but TestCase.__eq__ is broken
        to_add = cases[tuple(spec.split("."))]
        if not to_add:
            print("Warning: can't find test {}".format(spec))
        testcases.extend(to_add)
    suite = unittest.TestSuite(testcases)
    verbosity = 2 if os.environ.get("TESTHERP_VERBOSE") else 1
    with testing_mode():
        with run_server():
            OdooTextTestRunner(verbosity=verbosity).run(suite)


@contextlib.contextmanager
def run_server():
    if os.environ.get("TESTHERP_NO_SERVER"):
        yield None
        return
    server = odoo.service.server.ThreadedServer(odoo.service.wsgi_server.application)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@contextlib.contextmanager
def testing_mode():
    thread = threading.currentThread()
    odoo.tools.config["test_enable"] = True
    thread.testing = True
    try:
        yield
    finally:
        thread.testing = False
        odoo.tools.config["test_enable"] = False


def run_process(argv, env=None):
    try:
        proc = subprocess.Popen(argv, env=env)
        proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGTERM)
            proc.wait()
    if proc.returncode:
        raise RuntimeError(
            "Process {} failed with code {}".format(argv, proc.returncode)
        )


def get_addons_path():
    parser = configparser.ConfigParser()
    with open(config_file) as f:
        parser.read_file(f)
    odoo_dir = os.path.join(base_dir, "parts/odoo/openerp")
    if not os.path.isdir(odoo_dir):
        odoo_dir = os.path.join(base_dir, "parts/odoo/odoo")
    if not os.path.isdir(odoo_dir):
        raise RuntimeError("Can't find Odoo directory")
    olddir = os.getcwd()
    try:
        os.chdir(odoo_dir)
        return ",".join(
            os.path.abspath(path)
            for path in parser["options"]["addons_path"].split(",")
        )
    finally:
        os.chdir(olddir)


def run_odoo(dbname, args, loglevel="warn"):
    run_process(
        [
            start_odoo,
            "--stop-after-init",
            "-d",
            dbname,
            "--max-cron-threads=0",
            "--without-demo=",
            "--log-level={}".format(loglevel),
            "--config=/dev/null",
            "--addons-path={}".format(get_addons_path()),
        ]
        + args
    )


def read_state():
    global state
    fname = os.path.join(base_dir, ".testherp")
    state = {}
    if not os.path.isfile(fname):
        return
    with open(fname) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            key, value = line.split("=")
            state[frozenset(key.strip().split(","))] = value.strip()


def write_state():
    with open(os.path.join(base_dir, ".testherp"), "w") as f:
        for addons, dbname in sorted(state.items()):
            print("{} = {}".format(",".join(sorted(addons)), dbname), file=f)


def ensure_db_for(addons, clean, update):
    if addons in state:
        old_dbname = state[addons]
        if clean:
            print("Deleting old database {}".format(old_dbname))
            try:
                run_process(["dropdb", old_dbname])
            except RuntimeError:
                print("Could not delete {}".format(old_dbname))
        elif update:
            print("Updating {}".format(", ".join(sorted(addons))))
            run_odoo(old_dbname, ["-u", ",".join(addons)])
            return old_dbname
        else:
            return old_dbname
    dbname = "testherp-seed-{}".format(uuid.uuid4())
    print("Creating database {} for {}".format(dbname, ", ".join(sorted(addons))))
    run_process(["createdb", dbname])
    print("Installing Odoo (this may take a while)")
    run_odoo(dbname, ["-i", ",".join(addons)])
    print("Finished installing Odoo")
    state[addons] = dbname
    return dbname


@contextlib.contextmanager
def temp_db_for(addons, keep=False):
    dbname = "testherp-temp-{}".format(uuid.uuid4())
    seed_db = state[addons]
    try:
        run_process(["createdb", dbname, "-T", seed_db])
    except RuntimeError:
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
            try:
                run_process(["dropdb", dbname])
            except RuntimeError:
                print("Could not delete {}".format(dbname))


def run_test_process(addons, tests, keep):
    with temp_db_for(addons, keep=keep) as dbname:
        run_process([python_odoo, __file__, dbname, tests])


def find_executable(directory):
    if not os.path.isdir(directory):
        raise ValueError("Directory {!r} does not exist".format(directory))
    executable = os.path.join(directory, "bin/python_odoo")
    if not os.path.isfile(executable):
        raise ValueError("{!r} is not a buildout directory".format(directory))
    return executable


def testherp(argv=sys.argv[1:]):
    global base_dir, python_odoo, start_odoo, config_file

    parser = argparse.ArgumentParser()
    parser.add_argument("tests", nargs="+", help="Tests to run")
    parser.add_argument(
        "-C", "--directory", default=".", help="Buildout directory to use"
    )
    parser.add_argument(
        "--pdb", action="store_true", help="Launch pdb on failure or error"
    )
    parser.add_argument(
        "-c", "--clean", action="store_true", help="Replace the seed database"
    )
    parser.add_argument(
        "-u", "--update", action="store_true", help="Update the seed database"
    )
    parser.add_argument(
        "-k", "--keep", action="store_true", help="Don't delete the testing database"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose test output"
    )
    parser.add_argument(
        "-s", "--no-server", action="store_true", help="Don't run the XML RPC server"
    )
    args = parser.parse_args()
    base_dir = os.path.abspath(args.directory)
    python_odoo = find_executable(base_dir)
    start_odoo = os.path.join(base_dir, "bin/start_odoo")
    config_file = os.path.join(base_dir, "etc/odoo.cfg")
    tests = ",".join(args.tests)
    addons = frozenset(test.split(".")[0] for test in tests.split(","))
    read_state()
    ensure_db_for(addons, clean=args.clean, update=args.update)
    write_state()
    os.environ["PYTHON_ODOO"] = "1"
    if args.pdb:
        os.environ["TESTHERP_PDB"] = "1"
    if args.verbose:
        os.environ["TESTHERP_VERBOSE"] = "1"
    if args.no_server:
        os.environ["TESTHERP_NO_SERVER"] = "1"
    run_test_process(addons=addons, tests=tests, keep=args.keep)
    # TODO: return 1 if tests failed
    return 0


def main(argv=sys.argv[1:]):
    if os.environ.get("PYTHON_ODOO"):
        assert len(sys.argv) == 3
        initialize_odoo(sys.argv[1])
        run_tests(sys.argv[2])
        return 0
    else:
        return testherp()


if __name__ == "__main__":
    sys.exit(main())
