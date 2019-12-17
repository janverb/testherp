import os
import shutil
import sys
import tempfile

from contextlib import contextmanager
from unittest import TestCase

from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

import testherp

from testherp import ProcessManager, Spec, SpecList, State, TestherpError, _quote

PY3 = sys.version_info >= (3, 0)

if PY3:
    from unittest import mock
else:
    import mock


class TestTestherp(TestCase):
    def test_specs(self):
        a = Spec("foo.bar.baz")
        b = Spec("foo.bar")
        c = Spec("foo")

        self.assertEqual(a.as_tuple(), ("foo", "bar", "baz"))
        self.assertEqual(b.as_tuple(), ("foo", "bar"))
        self.assertEqual(c.as_tuple(), ("foo",))

        self.assertEqual(a.method, "baz")
        self.assertEqual(b.method, None)
        self.assertEqual(b.module, "bar")
        self.assertEqual(c.module, None)
        self.assertEqual(c.addon, "foo")

        self.assertNotEqual(a, b)
        self.assertNotEqual(b, c)
        self.assertNotEqual(a, c)

        for spec in a, b, c:
            self.assertEqual(spec, Spec(str(spec)))

        with self.assertRaises(TestherpError):
            Spec("")
        with self.assertRaises(TestherpError):
            Spec(".")
        with self.assertRaises(TestherpError):
            Spec("foo..bar")
        with self.assertRaises(TestherpError):
            Spec("foo.bar.baz.qux")

    def test_speclist(self):
        a = SpecList.from_str("foo.bar,baz.bar.qux")
        b = SpecList.from_str("foobar,,")
        c = SpecList.from_str("foo.bar,foo")

        self.assertEqual(a, [Spec("foo.bar"), Spec("baz.bar.qux")])
        self.assertEqual(b, [Spec("foobar")])
        self.assertEqual(c, [Spec("foo.bar"), Spec("foo")])

        self.assertSetEqual(a.addons(), {"foo", "baz"})
        self.assertSetEqual(b.addons(), {"foobar"})
        self.assertSetEqual(c.addons(), {"foo"})

        self.assertEqual(str(a), "foo.bar,baz.bar.qux")
        self.assertEqual(str(b), "foobar")
        self.assertEqual(str(c), "foo.bar,foo")

        for speclist in a, b, c:
            self.assertEqual(speclist, SpecList.from_str(str(speclist)))

        with self.assertRaises(TestherpError):
            SpecList.from_str("foo,bar,foo.bar.baz.qux")

    def test_quoting(self):
        self.assertEqual(_quote("foo"), '"foo"')
        self.assertEqual(_quote('foo"bar"'), '"foo""bar"""')

    def test_state(self):
        with self.tempdir() as directory:
            fname = os.path.join(directory, ".testherp")

            state = State.read(directory)
            self.assertEqual(state, {})
            state[frozenset({"baz", "bar"})] = "a"
            state[frozenset({"foo"})] = "b"

            state.write(directory)
            self.assertTrue(os.path.isfile(fname))
            with open(fname) as f:
                contents = f.read()
            self.assertEqual(contents, "foo = b\nbar,baz = a\n")

            state2 = State.read(directory)
            self.assertEqual(state, state2)

    def test_process_manager(self):
        with self.buildoutdir() as (base_dir, connect):
            addons = frozenset({"bar", "foo"})
            python_odoo_out = os.path.join(base_dir, "bin/python_odoo.called")
            start_odoo_out = os.path.join(base_dir, "bin/start_odoo.called")

            manager = ProcessManager(base_dir, "foo,bar.baz")
            self.assertEqual(manager.config.get("options", "db_user"), "test_user")
            self.assertEqual(manager.tests, [Spec("foo"), Spec("bar.baz")])
            self.assertEqual(manager.state, {})
            connect.assert_called_once_with(
                host=None, port=None, user="test_user", password=None
            )
            connect.return_value.set_isolation_level.assert_called_once_with(
                ISOLATION_LEVEL_AUTOCOMMIT
            )
            manager.run_tests(
                clean=False,
                update=False,
                verbosity=2,
                debugger="pdb",
                keep=False,
                server=False,
                failfast=True,
                buffer=False,
            )
            connect.return_value.cursor.assert_called_with()
            cursor = connect.return_value.cursor.return_value.__enter__.return_value
            self.assertIn(addons, manager.state)
            self.assertEqual(len(manager.state), 1)
            self.assertEqual(State.read(base_dir), manager.state)
            seed_db = manager.state[addons]
            cursor.execute.assert_any_call('CREATE DATABASE "{}"'.format(seed_db))

            self.assertTrue(os.path.isfile(start_odoo_out))
            self.assertTrue(os.path.isfile(python_odoo_out))

            with open(start_odoo_out) as f:
                start_odoo_args = f.readline().split()
            with open(python_odoo_out) as f:
                py_odoo_args = f.readline().split()
                py_odoo_env = set(f.readlines())

            self.assertEqual(len(py_odoo_args), 3)
            self.assertEqual(py_odoo_args[0], testherp.__file__)
            temp_db = py_odoo_args[1]
            self.assertEqual(py_odoo_args[2], "foo,bar.baz")

            self.assertIn("PYTHON_ODOO=1\n", py_odoo_env)
            self.assertIn("TESTHERP_VERBOSITY=2\n", py_odoo_env)
            self.assertIn("TESTHERP_DEBUGGER=pdb\n", py_odoo_env)
            self.assertIn("TESTHERP_FAILFAST=1\n", py_odoo_env)
            self.assertNotIn("TESTHERP_SERVER=1\n", py_odoo_env)
            self.assertNotIn("TESTHERP_BUFFER=1\n", py_odoo_env)

            cursor.execute.assert_any_call(
                'CREATE DATABASE "{}" WITH TEMPLATE "{}"'.format(temp_db, seed_db)
            )
            cursor.execute.assert_any_call('DROP DATABASE "{}"'.format(temp_db))

            self.assertEqual(len(start_odoo_args), 7)
            self.assertIn("bar,foo", start_odoo_args)
            self.assertIn(seed_db, start_odoo_args)

    def test_buildout_directory_validation(self):
        with self.tempdir() as directory:
            with self.assertRaises(TestherpError):
                ProcessManager(os.path.join(directory, "fake"), "foo,bar.baz")

            with self.assertRaises(TestherpError):
                ProcessManager(directory, "foo,bar.baz")

    def test_empty_tests_error(self):
        with self.buildoutdir() as (directory, connect):
            with self.assertRaises(TestherpError):
                ProcessManager(directory, "")

            with self.assertRaises(TestherpError):
                ProcessManager(directory, ",")

    @contextmanager
    def tempdir(self):
        directory = tempfile.mkdtemp()
        try:
            yield directory
        finally:
            shutil.rmtree(directory)

    @contextmanager
    def buildoutdir(self):
        src = os.path.join(os.path.dirname(__file__), "buildoutdir")
        with mock.patch("psycopg2.connect") as connect, self.tempdir() as parent_dir:
            dst = os.path.join(parent_dir, "buildout")
            shutil.copytree(src, dst)
            yield dst, connect
