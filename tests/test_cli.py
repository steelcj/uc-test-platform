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


if __name__ == "__main__":
    unittest.main()
