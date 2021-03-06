`testherp` is a script that runs Odoo tests in a user-friendly manner. It manages databases for you and runs only the tests you tell it to, even on older Odoo versions. The output is easier to follow than the usual logs.

`testherp.py` is a stand-alone script. You can copy it wherever you want or you can install it to `~/.local/bin` or similar with `pip install --user -e .`.

Run it in a buildout directory with the tests you want to run as arguments.

# Example

```
$ testherp letsencrypt
Created seed database testherp-odoo10-letsencrypt for letsencrypt
Installing Odoo (this may take a while)
Finished installing Odoo
Created temporary database testherp-odoo10-letsencrypt-4df6bb
F..............
======================================================================
FAIL: letsencrypt.test_letsencrypt.test_altnames_parsing
----------------------------------------------------------------------
Traceback (most recent call last):
  File "[...]/odoo10/parts/server-tools/letsencrypt/tests/test_letsencrypt.py", line 145, in test_altnames_parsing
    ['example.com', 'example.net', 'example.org'],
AssertionError: Lists differ: [u'example.com', u'example.org... != [u'example.com', u'example.net...

First differing element 1:
u'example.org'
u'example.net'

- [u'example.com', u'example.org', u'example.net']
+ [u'example.com', u'example.net', u'example.org']

----------------------------------------------------------------------
Ran 15 tests in 12.993s

FAILED (failures=1)
```

It installs the `letsencrypt` addon and its dependencies into a clean database. Then it runs the tests for `letsencrypt` in a temporary database cloned from the seed.

Let's say you fixed the test and want to run it again. You can do:

```
$ testherp letsencrypt.test_letsencrypt.test_altnames_parsing
Created temporary database testherp-odoo10-letsencrypt-58ac19
.
----------------------------------------------------------------------
Ran 1 test in 0.029s

OK
```

It remembered that it still had a seed database for `letsencrypt`, and only ran the requested test.

# Usage

A test takes the form of `addon[.module[.method]]`. Examples of tests that `testherp` understands are `hr`, `base.test_ir_actions`, and `calendar.test_calendar.test_validation_error`.

If you specify tests from multiple addons then a seed database will be created for that combination of addons. Tests that are incompatible with other addons may fail, so you get the best results running tests from one addon at a time.

If you want to start over with a fresh seed database, run it with `-c`/`--clean`. To update the tested addons in the seed database, use `-u`/`--update`.

Running with `-p`/`--pdb` immediately starts a `pdb` post-mortem when a test fails. The cursor is kept alive, so you can still inspect the state and run experiments. You can use `-d`/`--debugger` to specify an alternative debugger, e.g. `-d ipdb`.

`-v`/`--verbose` shows detailed output with each test as its own row instead of a tiny dot. `-q`/`--quiet` hides even the dots.

`-s`/`--server` runs the web server during testing. Some tests need it, but it makes the testing harder to interrupt and web tests take much longer, so it's disabled by default.

# Why to use it

- No need to manage databases and write out long commands manually
- Test output is easier to read
- Run only the tests you want to
- No need to wait for modules to install

# Why not to use it

- Rarely, tests that pass in runbot fail in here
  - The opposite is theoretically possible too
- You can't test multiple modules with incompatible tests in a single run
- `--test-tags` in Odoo 12+ gives the same granularity
