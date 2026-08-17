"""Migration runner behaviour: applies in order, records version, is idempotent.

Also guards the numbering convention, which is a contract between contributors rather
than a style preference: the directory is applied in ascending order and is
forward-only, so numbers must be unique and contiguous. Two branches each adding an
`0005_` merge cleanly and then disagree about the schema depending on apply order. A
mistyped name is quieter still — `discover_migrations()` skips anything that doesn't
match, so the migration never runs and nobody is told.
"""

import re
from pathlib import Path

import aiosqlite
import pytest

from adjutant import db as database


async def test_fresh_database_gets_all_migrations_applied(tmp_path: Path):
    conn = await database.connect(tmp_path / "t.db")
    try:
        async with conn.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
            row = await cur.fetchone()
        expected = max(n for n, _ in database.discover_migrations())
        assert row["v"] == expected
    finally:
        await conn.close()


async def test_reconnecting_does_not_reapply_migrations(tmp_path: Path):
    p = tmp_path / "t.db"
    conn = await database.connect(p)
    await conn.close()
    conn = await database.connect(p)  # would raise "table already exists" if reapplied
    try:
        async with conn.execute("SELECT COUNT(*) AS c FROM schema_version") as cur:
            row = await cur.fetchone()
        assert row["c"] == len(database.discover_migrations())
    finally:
        await conn.close()


def test_non_conforming_filenames_are_ignored(tmp_path: Path):
    (tmp_path / "0001_real.sql").write_text("SELECT 1;")
    (tmp_path / "notes.sql").write_text("SELECT 1;")
    (tmp_path / "02_bad.sql").write_text("SELECT 1;")
    found = database.discover_migrations(tmp_path)
    assert [n for n, _ in found] == [1]


# --- numbering convention ------------------------------------------------------

_CONVENTION = re.compile(r"^(\d{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")


def migration_problems(directory: Path) -> list[str]:
    """Return one actionable message per numbering fault; empty means the dir is sound.

    Reads names only. Each message says what to rename the file to, because a diff of
    directory listings doesn't tell a first-time contributor what to do about it.
    """
    numbered: dict[int, list[str]] = {}
    unusable: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        match = _CONVENTION.match(path.name)
        if match:
            numbered.setdefault(int(match.group(1)), []).append(path.name)
        else:
            unusable.append(path.name)

    next_free = max(numbered, default=0) + 1
    problems: list[str] = []

    for name in unusable:
        problems.append(
            f"{name} is not NNNN_lower_snake_name.sql, so discover_migrations() may skip "
            f"it silently and the schema it creates will never exist. Rename it to "
            f"{next_free:04d}_<name>.sql."
        )

    for number in sorted(numbered):
        names = numbered[number]
        if len(names) > 1:
            problems.append(
                f"{' and '.join(names)} share migration number {number:04d}. Git merges "
                f"both without conflict and then apply order decides the schema. Rename "
                f"whichever landed second to {next_free:04d}_<name>.sql and claim that "
                f"number on your issue."
            )

    expected = 1
    for number in sorted(numbered):
        if number > expected:
            missing = ", ".join(f"{n:04d}" for n in range(expected, number))
            problems.append(
                f"{numbered[number][0]} sits above a gap — {missing} missing. Migrations "
                f"are applied in ascending order, so a gap means a file that other "
                f"machines have is absent here. Renumber contiguously from 0001."
            )
        expected = number + 1

    return problems


def _write(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_text("SELECT 1;")


def test_the_shipped_migrations_have_no_numbering_problems():
    assert migration_problems(database.MIGRATIONS_DIR) == []


def test_an_empty_directory_has_no_numbering_problems(tmp_path: Path):
    assert migration_problems(tmp_path) == []


def test_a_clean_sequence_has_no_numbering_problems(tmp_path: Path):
    _write(tmp_path, "0001_init.sql", "0002_infra.sql", "0003_ranks_bot_created.sql")
    assert migration_problems(tmp_path) == []


def test_files_that_are_not_migrations_are_left_alone(tmp_path: Path):
    _write(tmp_path, "0001_init.sql")
    (tmp_path / "README.md").write_text("how this directory works")
    assert migration_problems(tmp_path) == []


def test_two_files_sharing_a_number_are_reported(tmp_path: Path):
    _write(tmp_path, "0001_init.sql", "0002_ranks.sql", "0002_events.sql")

    problems = migration_problems(tmp_path)

    assert len(problems) == 1
    assert "0002_events.sql" in problems[0]
    assert "0002_ranks.sql" in problems[0]


def test_a_duplicate_says_which_number_to_use_instead(tmp_path: Path):
    _write(tmp_path, "0001_init.sql", "0002_infra.sql", "0003_ranks.sql", "0003_teams.sql")

    problems = migration_problems(tmp_path)

    assert "0004" in problems[0]
    assert "rename" in problems[0].lower()


def test_a_gap_in_the_sequence_is_reported(tmp_path: Path):
    _write(tmp_path, "0001_init.sql", "0004_events.sql")

    problems = migration_problems(tmp_path)

    assert len(problems) == 1
    assert "0002" in problems[0]
    assert "0003" in problems[0]


def test_a_sequence_that_does_not_start_at_one_is_reported(tmp_path: Path):
    _write(tmp_path, "0002_infra.sql", "0003_ranks.sql")

    problems = migration_problems(tmp_path)

    assert len(problems) == 1
    assert "0001" in problems[0]


def test_a_name_the_runner_would_silently_skip_is_reported(tmp_path: Path):
    _write(tmp_path, "0001_init.sql", "002_ranks.sql")

    problems = migration_problems(tmp_path)

    assert any("002_ranks.sql" in p for p in problems)


@pytest.mark.parametrize(
    "name",
    [
        "0002_Ranks.sql",
        "0002-ranks.sql",
        "0002_ranks with spaces.sql",
        "0002_.sql",
        "0002.sql",
        "ranks.sql",
        "00002_ranks.sql",
    ],
)
def test_names_that_break_the_convention_are_reported(tmp_path: Path, name: str):
    _write(tmp_path, "0001_init.sql", name)

    assert any(name in p for p in migration_problems(tmp_path))


def test_an_unusable_name_says_which_number_to_use_instead(tmp_path: Path):
    _write(tmp_path, "0001_init.sql", "0002_infra.sql", "ranks_again.sql")

    problems = migration_problems(tmp_path)

    assert "0003" in problems[0]
    assert "rename" in problems[0].lower()


def test_every_problem_names_the_file_it_is_about(tmp_path: Path):
    _write(tmp_path, "0002_ranks.sql", "0002_events.sql", "nope.sql")

    problems = migration_problems(tmp_path)

    assert problems
    assert all(".sql" in p for p in problems)
