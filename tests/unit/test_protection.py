"""Bot-protection page detection.

A challenge page that is mistaken for source HTML would either invent an empty
catalogue or fail with a misleading "format changed" error. These markers must
stay conservative: a real listing that happens to mention Cloudflare is rare;
a missed challenge page is recovered by the collectors' empty-parse failures.
"""

from acm_hub.collectors.protection import looks_like_bot_protection


def test_hard_cloudflare_block_is_detected() -> None:
    html = "<html><h1>Sorry, you have been blocked</h1><p>Cloudflare Ray ID</p></html>"
    assert looks_like_bot_protection(html) is True


def test_js_challenge_is_detected_without_a_content_type() -> None:
    html = "<html><title>Just a moment...</title><p>Performance & security by Cloudflare</p></html>"
    assert looks_like_bot_protection(html) is True


def test_a_normal_source_page_is_not_a_challenge() -> None:
    html = "<html><h2>Ready to mentor</h2><div class='mentor-ready-block'></div></html>"
    assert looks_like_bot_protection(html) is False
