"""Downstream modelling built on the scraped datastore.

These modules read the SQLite database and turn raw HLTV statistics into the
higher-level signals the project is ultimately about:

    contribution  a transparent, role-aware player-contribution score
    meta          weapon/economy/role meta trends across time periods

They depend only on the DB, so they work the same whether the data came from a
live scrape or a fixture-loaded test database.
"""
