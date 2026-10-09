import unittest

from terminal_qr import BLACK, QUIET_ZONE, RESET, WHITE, qr_modules, render_terminal_qr


class TerminalQrTest(unittest.TestCase):
    def test_qr_has_quiet_zone_and_terminal_colors(self):
        url = "http://192.168.31.76:8080/"
        modules = qr_modules(url)
        width = len(modules)
        self.assertTrue(all(len(row) == width for row in modules))
        self.assertTrue(all(not value for row in modules[:QUIET_ZONE] for value in row))
        self.assertTrue(all(not value for row in modules[-QUIET_ZONE:] for value in row))
        self.assertTrue(all(not row[x] for row in modules for x in range(QUIET_ZONE)))
        self.assertTrue(all(not row[x] for row in modules for x in range(width - QUIET_ZONE, width)))
        self.assertTrue(any(value for row in modules for value in row))
        rendered = render_terminal_qr(url)
        self.assertEqual(width, len(rendered.splitlines()))
        self.assertIn(BLACK, rendered)
        self.assertIn(WHITE, rendered)
        self.assertTrue(all(line.endswith(RESET) for line in rendered.splitlines()))


if __name__ == "__main__":
    unittest.main()
