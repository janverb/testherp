#!/usr/bin/env python3
# TODO:
# - change language for all res.partner when database is created (or loaded?)
#   - where does default come from? (_initalize_database)
# - improve formatting to make singling out test cases easier
# - logging?
# - unittest2? nose?

import argparse
import collections
import contextlib
import os
import subprocess
import sys
import threading
import time
import unittest

odoo = None


def initialize_odoo(database):
    global odoo
    try:
        import openerp as odoo
    except ImportError:
        import odoo
    session.open(database)
    session.env["res.users"].browse(1).lang = "en_US"
    session.cr.commit()


def unwrap_testcases(suite):
    if isinstance(suite, unittest.suite.BaseTestSuite):
        for test in suite._tests:
            for case in unwrap_testcases(test):
                yield case
    else:
        yield suite


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
                cases[addon, mod_name, case._testMethodName].append(case)
                cases[addon, mod_name].append(case)
                cases[addon,].append(case)
    testcases = []
    for spec in specs:
        # Duplication is possible here but TestCase.__eq__ is broken
        to_add = cases[tuple(spec.split("."))]
        if not to_add:
            raise ValueError("Can't find test {}".format(spec))
        testcases.extend(to_add)
    suite = unittest.TestSuite(testcases)
    thread = threading.currentThread()
    odoo.tools.config["test_enable"] = True
    thread.testing = True
    try:
        unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        thread.testing = False


def run_process(argv, env=None):
    proc = subprocess.Popen(argv, env=env)
    proc.wait()
    if proc.returncode:
        raise RuntimeError(
            "Process {} failed with code {}".format(argv, proc.returncode)
        )


def run_odoo(executable, dbname, args, loglevel="warn"):
    run_process(
        [
            executable,
            "--stop-after-init",
            "-d",
            dbname,
            "--max-cron-threads=0",
            "--without-demo=",
            "--logfile=/dev/stdout",
            "--log-level={}".format(loglevel),
        ]
        + args
    )


def read_state(directory):
    fname = os.path.join(directory, ".testherp")
    if not os.path.isfile(fname):
        return {}
    databases = {}
    with open(fname) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            key, value = line.split("=")
            databases[frozenset(key.strip().split(","))] = value.strip()
    return databases


def write_state(directory, state):
    with open(os.path.join(directory, ".testherp"), "w") as f:
        for addons, dbname in sorted(state.items()):
            f.write("{} = {}".format(",".join(sorted(addons)), dbname))


def ensure_db_for(addons, state, executable):
    if addons in state:
        return state[addons]
    dbname = "testherp-seed-{}".format(int(time.time()))
    print("Creating database {} for {}".format(dbname, ", ".join(addons)))
    run_process(["createdb", dbname])
    print("Installing Odoo (this may take a while)")
    run_odoo(executable, dbname, ["-i", ",".join(addons)])
    print("Finished installing Odoo")
    state[addons] = dbname
    return dbname


@contextlib.contextmanager
def temp_db_for(addons, state):
    dbname = "testherp-temp-{}".format(int(time.time()))
    seed_db = state[addons]
    print("Creating temporary database {}".format(dbname))
    run_process(["createdb", dbname, "-T", seed_db])
    try:
        yield dbname
    finally:
        try:
            run_process(["dropdb", dbname])
        except Exception:
            print("Could not delete {}".format(dbname))
            pass


def run_test_process(executable, addons, tests, state):
    env = os.environ.copy()
    env["PYTHON_ODOO"] = "True"
    with temp_db_for(addons, state) as dbname:
        run_process([executable, __file__, dbname, tests], env=env)


def find_executable(directory="."):
    if not os.path.isdir(directory):
        raise ValueError("Directory {!r} does not exist".format(directory))
    executable = os.path.join(directory, "bin/python_odoo")
    if not os.path.isfile(executable):
        raise ValueError(
            "{!r} is not a buildout directory".format(os.path.abspath(directory))
        )
    return executable


def main(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser()
    parser.add_argument("tests", nargs="+", help="Tests to run")
    parser.add_argument("--directory", default=".", help="Buildout directory to use")
    args = parser.parse_args()
    python_odoo = find_executable(args.directory)
    start_odoo = python_odoo[:-len("python_odoo")] + "start_odoo"
    tests = ",".join(args.tests)
    addons = frozenset(test.split(".")[0] for test in tests.split(","))
    state = read_state(args.directory)
    ensure_db_for(addons, state=state, executable=start_odoo)
    write_state(args.directory, state)
    run_test_process(executable=python_odoo, addons=addons, tests=tests, state=state)


if __name__ == "__main__":
    if os.environ.get("PYTHON_ODOO"):
        assert len(sys.argv) == 3
        initialize_odoo(sys.argv[1])
        run_tests(sys.argv[2])
    else:
        sys.exit(main())
