"""parse_players: pure parsing of a raw RCON `#players` response.

No berconpy required — this exercises only the top-level parsing function
against plausible response shapes, since Reforger's RCON has no published
schema doc to test against directly.
"""

from adjutant.serverlink.rcon_link import parse_players


def test_parse_players_returns_empty_list_for_empty_string():
    assert parse_players("") == []


def test_parse_players_returns_empty_list_for_zero_player_response():
    text = "Players:\n----------------------------\n(0 players in total)"
    assert parse_players(text) == []


def test_parse_players_parses_semicolon_columns():
    text = "0;abc123;Alice\n1;def456;Bob"
    players = parse_players(text)
    assert [(p.player_id, p.uuid, p.name) for p in players] == [
        ("0", "abc123", "Alice"),
        ("1", "def456", "Bob"),
    ]


def test_parse_players_skips_header_and_separator_lines():
    text = "Players:\n----------------------------\n0;abc123;Alice\n----------------------------\n(1 players in total)"
    players = parse_players(text)
    assert [p.name for p in players] == ["Alice"]


def test_parse_players_handles_names_with_spaces():
    text = "0;abc123;Alice The Great"
    players = parse_players(text)
    assert players[0].name == "Alice The Great"


def test_parse_players_handles_names_with_semicolons():
    text = "0;abc123;Alice;the;Great"
    players = parse_players(text)
    assert players[0].name == "Alice;the;Great"


def test_parse_players_handles_unicode_names():
    text = "0;abc123;Jörg 中文名 🎮"
    players = parse_players(text)
    assert players[0].name == "Jörg 中文名 🎮"


def test_parse_players_parses_whitespace_delimited_columns():
    text = "0   abc123def456   Alice Wonder"
    players = parse_players(text)
    assert players[0].player_id == "0"
    assert players[0].uuid == "abc123def456"
    assert players[0].name == "Alice Wonder"


def test_parse_players_skips_unparseable_lines_without_raising():
    text = "Players:\n0;abc123;Alice\nsome garbled nonsense with no columns\n1;def456;Bob"
    players = parse_players(text)
    assert [p.name for p in players] == ["Alice", "Bob"]


def test_parse_players_skips_blank_lines():
    text = "0;abc123;Alice\n\n\n1;def456;Bob"
    players = parse_players(text)
    assert len(players) == 2


def test_parse_players_handles_crlf_line_endings():
    text = "0;abc123;Alice\r\n1;def456;Bob\r\n"
    players = parse_players(text)
    assert len(players) == 2


def test_parse_players_strips_surrounding_whitespace_from_columns():
    text = "0 ; abc123 ; Alice "
    players = parse_players(text)
    assert (players[0].player_id, players[0].uuid, players[0].name) == ("0", "abc123", "Alice")


def test_parse_players_ignores_rows_with_missing_name():
    text = "0;abc123;"
    assert parse_players(text) == []


def test_parse_players_ignores_rows_with_missing_id():
    text = ";abc123;Alice"
    assert parse_players(text) == []


def test_parse_players_never_raises_on_garbage_input():
    # Whole point of the tolerant design: garbage in, empty/partial list out.
    text = "\x00\x01 not player data at all \n\n???;???\n"
    result = parse_players(text)
    assert isinstance(result, list)
