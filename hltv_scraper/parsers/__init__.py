"""Pure HTML -> dataclass parsers.

Every function in this subpackage takes raw HTML (a ``str``) and returns model
objects. They never touch the network or the database, which makes them
straightforward to unit-test against saved fixtures and resilient to being
re-run. When HLTV changes its markup, this is the only layer that needs edits.
"""

from . import common, rankings, player_stats, matches, events  # noqa: F401
