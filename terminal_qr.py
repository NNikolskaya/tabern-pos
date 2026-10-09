"""Print a scannable LAN URL QR code in an ANSI-capable terminal."""

from vendor.qrcodegen import QrCode


BLACK = "\x1b[40m  "
WHITE = "\x1b[47m  "
RESET = "\x1b[0m"
QUIET_ZONE = 4


def qr_modules(url):
    qr = QrCode.encode_text(url, QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    width = size + QUIET_ZONE * 2
    return [
        [qr.get_module(x - QUIET_ZONE, y - QUIET_ZONE)
         if QUIET_ZONE <= x < size + QUIET_ZONE and QUIET_ZONE <= y < size + QUIET_ZONE
         else False
         for x in range(width)]
        for y in range(width)
    ]


def render_terminal_qr(url):
    return "\n".join(
        "".join(BLACK if dark else WHITE for dark in row) + RESET
        for row in qr_modules(url)
    )
