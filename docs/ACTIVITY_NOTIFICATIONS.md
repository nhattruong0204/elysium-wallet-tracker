# Activity links and notifications

The Activity launchpad column uses the existing card token routes for chain 99901:
Kleos `https://www.thekleos.pro/token/<address>`, BOXA
`https://boxa.club/token?address=<address>`, and ElysPump
`https://elysp.com/#/token/<address>`. These routes are defined by the existing Links
helpers and centrally allowlisted by `launchpad_token_url` in cards.py. Attribution
comes from the stored token launchpad, not a transaction router. Montra `/token/<address>` and Obol `/coin/<address>` use owner-verified token routes.
For Ascend, Volta, Eleos and Elyxa, the owner-supplied platform
homepages are available as shortcuts. Their tooltip explicitly says platform (homepage),
and the API marks them with `launchpad_url_kind: platform`; verified token routes use
`token`. No token path is guessed. Other unregistered venues remain plain labels. No third-party validation requests are made.

The existing incremental Activity poller counts distinct new matching event legs.
Background, unfocused, or scrolled-away arrivals remain pending. The tab title and
`N new` button show unread activity; Show newest or viewing the pending rows clears it.
Returning to the tab alone does not clear pending activity. Filters/navigation establish
a fresh baseline; loading older rows does not notify. Overflow retains the existing
reload-to-see-newest behavior; the unread count covers returned distinct rows only.

Sound defaults OFF. The enabled preference is saved, but a new page only offers Enable
sound until clicked again to satisfy autoplay rules. Click Sound to unlock Web Audio; ON appears
only once the context is running. A short generated tone is coalesced to one per batch
and a five-second cooldown. A localStorage timestamp suppresses simultaneous tones
across tabs on a best-effort basis; unavailable storage may permit cross-tab duplicates.
There is no desktop/push permission request. Background detection follows the existing
visibility-aware polling cadence and browser throttling; it is not instantaneous.

Tests can use the installed Chromium with `TEST_CHROMIUM_EXECUTABLE=/usr/bin/chromium`.
CI otherwise uses Playwright's downloaded browser. No database migration is needed.


Platform shortcut registry (`LAUNCHPAD_PLATFORMS` in cards.py):

| Venue | Platform homepage |
| --- | --- |
| Ascend | https://app.ascendlaunch.xyz/ |
| Volta | https://www.volta.family/ |
| Eleos | https://eleos.fun/ |
| Elyxa | https://elyxa.trade/ |

The stored launchpad attribution selects the shortcut; "other venue" is not inferred
from a token name or execution router. A platform shortcut opens the homepage, not a
verified token detail page. Third-party availability is outside the dashboard's control.
