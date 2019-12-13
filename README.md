`testherp` is a script that runs Odoo tests in a user-friendly manner. It manages databases for you and runs only the tests you tell it to, even on older Odoo versions. The output is easier to follow than the usual logs.

`testherp.py` is a stand-alone script. You can copy it wherever you want or you can install it to `~/.local/bin` or similar with `pip3 install --user -e .`.

Run it in a buildout directory with the tests you want to run as arguments.

Here's an example run:

```
$ testherp letsencrypt
Creating database testherp-seed-0cb44986-df17-4bcd-88c5-e4580ce30403 for letsencrypt
Installing Odoo (this may take a while)
Finished installing Odoo
Created temporary database testherp-temp-9453cac2-e036-4554-955d-5d0da0b77a23
F..............
======================================================================
FAIL: letsencrypt.test_letsencrypt.test_altnames_parsing
----------------------------------------------------------------------
Traceback (most recent call last):
  File "[...]/parts/server-tools/letsencrypt/tests/test_letsencrypt.py", line 153, in test_altnames_parsing
    ['example.com', 'example.net', 'example.org'],
AssertionError: Lists differ: [u'example.com', u'example.org... != [u'example.com', u'example.net...

First differing element 1:
u'example.org'
u'example.net'

- [u'example.com', u'example.org', u'example.net']
+ [u'example.com', u'example.net', u'example.org']

----------------------------------------------------------------------
Ran 15 tests in 17.077s

FAILED (failures=1)
```

It installs the `letsencrypt` addon and its dependencies into a clean database. Then it runs the tests for `letsencrypt` in a temporary database cloned from the seed.

Let's say you fixed the test and want to run it again. You can do:

```
$ testherp letsencrypt.test_letsencrypt.test_altnames_parsing
Created temporary database testherp-temp-4af9debd-3301-48dd-80f9-56b033ac7988
.
----------------------------------------------------------------------
Ran 1 test in 0.031s

OK
```

It remembered that it still had a seed database for `letsencrypt`, and only ran the requested test. The whole command took less than two seconds to run.

A test takes the form of `addon[.module[.method]]`. Examples of tests that `testherp` understands are `hr`, `base.test_ir_actions`, and `calendar.test_calendar.test_validation_error`.

If you specify tests from multiple addons then a seed database will be created for that combination of addons. Tests that are incompatible with other addons may fail.

If you want to start over with a fresh seed database, run it with `--clean`. To update the tested addons in the seed database, use `--update`. With `--keep` the temporary database isn't dropped after the tests are done.

Running with `--pdb` immediately starts a `pdb` post-mortem when a test fails.

`--verbose` shows detailed output with each test as its own row instead of a tiny dot.

`--server` runs the XML RPC server during testing. Some tests need it, but it makes the testing harder to interrupt, so it's disabled by default.
