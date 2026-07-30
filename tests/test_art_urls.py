"""Thumbnail URL upscaling — the 144p album-art fix.

YT Music's payloads cap thumbnails at ~120px; the CDN serves any size the
URL asks for. _thumb must rewrite the size segment so big art surfaces
(now-playing strip, mini player) never upscale a postage stamp.
"""
import unittest

from tide.sources.ytmusic import _thumb, _upscale_art_url


class UpscaleArtUrlTest(unittest.TestCase):
    def test_googleusercontent_wh_is_upscaled_flags_preserved(self) -> None:
        url = "https://lh3.googleusercontent.com/abc123=w120-h120-l90-rj"
        self.assertEqual(
            _upscale_art_url(url),
            "https://lh3.googleusercontent.com/abc123=w544-h544-l90-rj",
        )

    def test_googleusercontent_s_form_is_upscaled(self) -> None:
        url = "https://yt3.ggpht.com/abc=s88-c-k-c0x00ffffff-no-rj"
        self.assertEqual(
            _upscale_art_url(url),
            "https://yt3.ggpht.com/abc=s544-c-k-c0x00ffffff-no-rj",
        )

    def test_ytimg_small_variants_become_hqdefault(self) -> None:
        for small in ("default", "mqdefault", "sddefault"):
            url = f"https://i.ytimg.com/vi/dQw4w9WgXcQ/{small}.jpg"
            self.assertEqual(
                _upscale_art_url(url),
                "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
            )

    def test_ytimg_hqdefault_untouched(self) -> None:
        url = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        self.assertEqual(_upscale_art_url(url), url)

    def test_foreign_urls_pass_through(self) -> None:
        for url in (
            "https://f4.bcbits.com/img/a1234_16.jpg",
            "https://i1.sndcdn.com/artworks-abc-t500x500.jpg",
            "",
        ):
            self.assertEqual(_upscale_art_url(url), url)

    def test_thumb_picks_last_and_upscales(self) -> None:
        items = [
            {"url": "https://lh3.googleusercontent.com/a=w60-h60-l90-rj"},
            {"url": "https://lh3.googleusercontent.com/a=w120-h120-l90-rj"},
        ]
        self.assertEqual(
            _thumb(items),
            "https://lh3.googleusercontent.com/a=w544-h544-l90-rj",
        )


if __name__ == "__main__":
    unittest.main()
