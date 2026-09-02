# Recruiting QR code

Points at **https://portal.solprovision.com/join** — the one page on the solprovision domain that
isn't Discord-gated, which is what makes it the right target for a code a stranger scans.

| File | Use it for |
|---|---|
| `join-qr.svg` | **Print.** Vector — scales from a business card to a banner with no loss. Give this to anyone doing print work. |
| `join-qr.png` | Screens: Discord, slides, streams. 900×900. |
| `join-qr-brand.svg` / `.png` | Same code in Sol Provision ink on bone, for pieces that sit on our own layouts. |

Both variants encode the identical URL. The branded one is only tinted, never inverted.

## Regenerating

Only if the URL changes. Don't edit the images by hand.

```bash
pip install segno
python tools/make_join_qr.py
```

The URL lives at the top of `tools/make_join_qr.py`.

## Three things that break QR codes

**Don't crop the white border.** The 4-module quiet zone is part of the spec, not padding. This is
the most common way a working code stops working — verified: cropping the border makes these files
undecodable.

**Don't invert it.** Scanners expect dark modules on a light background. Light-on-dark works on many
modern phones and fails on others, and a failed scan on a recruiting poster is a lost recruit. If a
piece needs to sit on a dark background, put the code in a light panel rather than inverting it.

**Be careful dropping a logo in the middle.** These are error-correction level H, the highest, which
is what makes a centre logo survivable at all — but "30% recoverable" refers to scattered codewords,
not to any 30% of the picture. A blob over the centre also covers the alignment pattern, which no
error correction restores. If someone adds a logo, **scan the finished artwork with two different
phones before it goes to print.**

## Verified

Both PNGs decode to exactly the join URL, and still decode when scaled down to 100×100px. The
branded variant measures 16.9:1 contrast, far above the ~3:1 a scanner needs. Both SVGs parse.

Error-correction headroom is *not* something we measured — that figure is the spec's claim about
level H, not a local test result. Hence the "scan it before printing" advice above.
