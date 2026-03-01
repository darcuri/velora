import unittest

from velora.cli import main


class TestCliSmoke(unittest.TestCase):
    def test_status(self):
        self.assertEqual(main(["status"]), 0)


if __name__ == "__main__":
    unittest.main()
