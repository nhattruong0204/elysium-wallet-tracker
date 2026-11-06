"""Launchpad links use the existing verified card routes, never arbitrary metadata URLs."""
import pytest

from agora_tracker.cards import Links, launchpad_token_url
from agora_tracker.config import LinksConfig

TOKEN = '0x' + 'ab' * 20


@pytest.mark.parametrize(('pad', 'expected'), [
    ('kleos', f'https://www.thekleos.pro/token/{TOKEN}'),
    ('boxa', f'https://boxa.club/token?address={TOKEN}'),
    ('elyspump', f'https://elysp.com/#/token/{TOKEN}'),
    ('montra', None), ('unknown', None), (None, None),
])
def test_verified_launchpad_routes(pad, expected):
    assert launchpad_token_url(99901, pad, TOKEN) == expected


def test_launchpad_resolver_rejects_wrong_chain_address_and_host():
    assert launchpad_token_url(1, 'kleos', TOKEN) is None
    assert launchpad_token_url(99901, 'kleos', 'javascript:alert(1)') is None
    for host in ('javascript:bad', 'https://evil.test', 'https://www.thekleos.pro@evil.test'):
        assert launchpad_token_url(99901, 'kleos', TOKEN, Links(LinksConfig(kleos=host))) is None


@pytest.mark.parametrize(('pad', 'url'), [
    ('ascend', 'https://app.ascendlaunch.xyz/'),
    ('montra', 'https://montramarkets.xyz/'),
    ('obol', 'https://tryobol.world/'),
    ('volta', 'https://www.volta.family/'),
    ('eleos', 'https://eleos.fun/'),
    ('elyxa', 'https://elyxa.trade/'),
])
def test_registered_platform_shortcuts(pad, url):
    from agora_tracker.cards import activity_launchpad_link
    assert activity_launchpad_link(99901, pad, TOKEN) == (url, 'platform')
    assert launchpad_token_url(99901, pad, TOKEN) is None
    assert activity_launchpad_link(1, pad, TOKEN) == (None, None)
    assert activity_launchpad_link(99901, pad, 'invalid') == (None, None)


def test_unknown_venue_has_no_invented_platform():
    from agora_tracker.cards import activity_launchpad_link
    assert activity_launchpad_link(99901, 'other', TOKEN) == (None, None)
    assert activity_launchpad_link(99901, 'javascript:alert(1)', TOKEN) == (None, None)
    assert activity_launchpad_link(99901, 'boxa', TOKEN)[1] == 'token'
