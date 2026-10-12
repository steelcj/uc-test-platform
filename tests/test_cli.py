import contextlib
import io
import unittest

from support import REPO

from uc_test_platform import __version__
from uc_test_platform.cli import main


class Cli(unittest.TestCase):

    def test_version_is_the_version_file(self):
        self.assertEqual(__version__,
                         (REPO / "VERSION").read_text(encoding="utf-8").strip())

    def test_version_option(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ended:
            main(["--version"])
        self.assertEqual(ended.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"uc-test-platform {__version__}")

    def test_no_arguments(self):
        self.assertEqual(main([]), 0)

    def test_each_command_has_help(self):
        for command in ("windows", "linux", "cut"):
            with self.subTest(command=command):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        self.assertRaises(SystemExit) as ended:
                    main([command, "--help"])
                self.assertEqual(ended.exception.code, 0)
                self.assertIn(f"uc-test-platform {command}", out.getvalue())

    def test_drivers_need_the_repository(self):
        for command in ("windows", "linux"):
            with self.subTest(command=command):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), \
                        self.assertRaises(SystemExit) as ended:
                    main([command, "--instance", "x", "--snapshot", "y",
                          "--ssh", "z"])
                self.assertEqual(ended.exception.code, 2)
                self.assertIn("--repository", err.getvalue())


if __name__ == "__main__":
    unittest.main()
