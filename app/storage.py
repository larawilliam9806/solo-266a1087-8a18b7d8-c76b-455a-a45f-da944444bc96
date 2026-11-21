"""SQLite persistence for survey batches, households, and path answers."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS households (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    household_no TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    UNIQUE (batch_id, household_no)
);

CREATE TABLE IF NOT EXISTS answers (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL,
    qid TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (household_id, prefix, qid)
);

-- Rows that shared a household number on import.  They are held outside
-- ``households`` until a reviewer settles every row of the group, so they can
-- never leak into the editor or the analysis export.
CREATE TABLE IF NOT EXISTS pending_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    original_no TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    decided_at TEXT,
    UNIQUE (batch_id, original_no)
);

CREATE TABLE IF NOT EXISTS pending_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES pending_groups(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    answers TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'undecided',
    verified_no TEXT,
    household_id INTEGER REFERENCES households(id) ON DELETE SET NULL
);
"""

# The reserved number of a group that still waits for a decision.
GROUPS_PENDING = "pending"
GROUPS_ACCEPTED = "accepted"


class GroupError(Exception):
    """A 重号 group cannot be accepted (yet).  Nothing is written."""


def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


@contextmanager
def transaction(connection):
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _prefix_key(prefix):
    return json.dumps(list(prefix), ensure_ascii=False, separators=(",", ":"))


def _prefix_tuple(key):
    try:
        value = json.loads(key)
    except (TypeError, ValueError):
        return ()
    return tuple(str(item) for item in value)


def create_pending_group(connection, batch_id, original_no, rows):
    """Hold duplicate-numbered CSV rows until a reviewer settles the group.

    ``rows`` is a list of ``{"row_number": ..., "answers": {(prefix, qid): value}}``.
    Runs inside the caller's transaction so the batch, its editable rows and
    its pending groups all appear together (or not at all).
    """
    cursor = connection.execute(
        "INSERT INTO pending_groups (batch_id, original_no) VALUES (?, ?)",
        (batch_id, original_no))
    group_id = cursor.lastrowid
    for entry in rows:
        connection.execute(
            "INSERT INTO pending_rows (group_id, row_number, answers) "
            "VALUES (?, ?, ?)",
            (group_id, entry["row_number"],
             json.dumps([[list(prefix), qid, value]
                         for (prefix, qid), value in
                         (entry["answers"] or {}).items()],
                        ensure_ascii=False)))
    return group_id


def create_batch_with_pending(connection, name, definition, households, groups):
    """Insert an imported batch, its editable rows and its pending groups.

    One transaction: either the whole import lands or nothing does.
    """
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO batches (name, definition) VALUES (?, ?)",
            (name, json.dumps(definition, ensure_ascii=False)))
        batch_id = cursor.lastrowid
        for entry in households:
            cursor = connection.execute(
                "INSERT INTO households (batch_id, household_no, row_number) "
                "VALUES (?, ?, ?)",
                (batch_id, entry["household_no"], entry["row_number"]))
            household_id = cursor.lastrowid
            rows = [
                (household_id, _prefix_key(prefix), qid, value)
                for (prefix, qid), value in (entry["answers"] or {}).items()
            ]
            connection.executemany(
                "INSERT OR REPLACE INTO answers "
                "(household_id, prefix, qid, value) VALUES (?, ?, ?, ?)", rows)
        for group in groups:
            create_pending_group(connection, batch_id,
                                 group["original_no"], group["rows"])
    return batch_id


def get_batch(connection, batch_id):
    row = connection.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "definition": json.loads(row["definition"]),
        "created_at": row["created_at"],
    }


def list_batches(connection):
    rows = connection.execute(
        "SELECT b.id, b.name, b.created_at, COUNT(h.id) AS households "
        "FROM batches b LEFT JOIN households h ON h.batch_id = b.id "
        "GROUP BY b.id ORDER BY b.id DESC").fetchall()
    return [dict(row) for row in rows]


def list_households(connection, batch_id):
    rows = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? ORDER BY row_number, id", (batch_id,)).fetchall()
    return [dict(row) for row in rows]


def get_household(connection, batch_id, household_id):
    row = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? AND id = ?",
        (batch_id, household_id)).fetchone()
    return dict(row) if row else None


def load_answers(connection, household_id):
    rows = connection.execute(
        "SELECT prefix, qid, value FROM answers WHERE household_id = ?",
        (household_id,)).fetchall()
    return {(_prefix_tuple(row["prefix"]), row["qid"]): row["value"]
            for row in rows}


def save_answers(connection, household_id, changes):
    """Persist ``{key: value_or_None}`` for one household.

    ``key`` is ``(prefix_tuple, qid)``; ``None`` deletes.  All changes are
    written in a single transaction, so a failure leaves the previous answers
    fully intact.
    """
    with transaction(connection):
        for (prefix, qid), value in changes.items():
            key = _prefix_key(prefix)
            if value is None:
                connection.execute(
                    "DELETE FROM answers WHERE household_id = ? "
                    "AND prefix = ? AND qid = ?",
                    (household_id, key, qid))
            else:
                connection.execute(
                    "INSERT INTO answers (household_id, prefix, qid, value) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (household_id, prefix, qid) "
                    "DO UPDATE SET value = excluded.value",
                    (household_id, key, qid, value))


def _answers_from_json(payload):
    try:
        items = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    answers = {}
    for prefix, qid, value in items:
        answers[(tuple(str(item) for item in prefix), str(qid))] = value
    return answers


def list_pending_groups(connection, batch_id):
    """Groups as ``{id, original_no, status, rows}`` in CSV row order."""
    groups = connection.execute(
        "SELECT id, original_no, status, created_at, decided_at "
        "FROM pending_groups WHERE batch_id = ? ORDER BY id",
        (batch_id,)).fetchall()
    rows = connection.execute(
        "SELECT p.id, p.group_id, p.row_number, p.answers, p.decision, "
        "p.verified_no, p.household_id FROM pending_rows p "
        "JOIN pending_groups g ON g.id = p.group_id "
        "WHERE g.batch_id = ? ORDER BY p.group_id, p.row_number, p.id",
        (batch_id,)).fetchall()
    by_group = {}
    for row in rows:
        by_group.setdefault(row["group_id"], []).append({
            "id": row["id"],
            "row_number": row["row_number"],
            "answers": _answers_from_json(row["answers"]),
            "decision": row["decision"],
            "verified_no": row["verified_no"],
            "household_id": row["household_id"],
        })
    return [{
        "id": group["id"],
        "original_no": group["original_no"],
        "status": group["status"],
        "created_at": group["created_at"],
        "decided_at": group["decided_at"],
        "rows": by_group.get(group["id"], []),
    } for group in groups]


def get_pending_group(connection, batch_id, group_id):
    for group in list_pending_groups(connection, batch_id):
        if group["id"] == group_id:
            return group
    return None


def save_pending_decisions(connection, group_id, decisions):
    """Record one group's row decisions.

    ``decisions`` maps ``pending_row_id`` to ``("discard", None)`` or
    ``("keep", verified_no)``.  This is staging only: nothing joins the batch
    until :func:`accept_pending_group` runs.
    """
    with transaction(connection):
        row = connection.execute(
            "SELECT status FROM pending_groups WHERE id = ?",
            (group_id,)).fetchone()
        if row is None:
            raise GroupError("待核组不存在")
        if row["status"] != GROUPS_PENDING:
            raise GroupError("该组已核定，不能再修改")
        for row_id, (decision, verified_no) in decisions.items():
            connection.execute(
                "UPDATE pending_rows SET decision = ?, verified_no = ? "
                "WHERE id = ? AND group_id = ?",
                (decision, verified_no, row_id, group_id))


def accepted_numbers(connection, batch_id):
    """Household numbers already used by the batch's settled rows."""
    return {row["household_no"] for row in connection.execute(
        "SELECT household_no FROM households WHERE batch_id = ?",
        (batch_id,))}


def pending_reserved_numbers(connection, batch_id, exclude_group_id):
    """Original numbers still reserved by other undecided groups."""
    rows = connection.execute(
        "SELECT original_no FROM pending_groups "
        "WHERE batch_id = ? AND status = ? AND id != ?",
        (batch_id, GROUPS_PENDING, exclude_group_id)).fetchall()
    return {row["original_no"] for row in rows}


def accept_pending_group(connection, batch_id, group_id, decisions):
    """Settle a whole group in one transaction.

    Every row must have a decision; the kept numbers must differ from each
    other, from the batch's settled households and from numbers other waiting
    groups still reserve.  Kept rows then join ``households`` with their
    cleaned answers and the group is marked accepted.

    ``decisions`` is ``{pending_row_id: (decision, verified_no)}`` exactly as
    submitted; it is validated and written inside the same transaction, so a
    rejected group leaves both the batch and the staging untouched.

    Returns the list of new household ids.  :class:`GroupError` is raised with
    nothing written when the group cannot be accepted.
    """
    with transaction(connection):
        group = connection.execute(
            "SELECT id, original_no, status FROM pending_groups "
            "WHERE id = ? AND batch_id = ?", (group_id, batch_id)).fetchone()
        if group is None:
            raise GroupError("待核组不存在")
        if group["status"] != GROUPS_PENDING:
            raise GroupError("该组已核定，不能重复生效")

        rows = connection.execute(
            "SELECT id, row_number FROM pending_rows WHERE group_id = ? "
            "ORDER BY row_number, id", (group_id,)).fetchall()
        if not rows:
            raise GroupError("待核组没有数据行")

        resolved = {}
        for row in rows:
            decision, verified_no = decisions.get(
                row["id"], ("undecided", None))
            if decision not in ("keep", "discard"):
                raise GroupError(
                    "CSV 第 %d 行还没有决定保留或弃用" % row["row_number"])
            if decision == "keep":
                number = (verified_no or "").strip()
                if not number:
                    raise GroupError(
                        "CSV 第 %d 行保留但未填写核实户号" % row["row_number"])
                resolved[row["id"]] = number
            else:
                resolved[row["id"]] = None

        numbers = [number for number in resolved.values() if number]
        duplicates = sorted({number for number in numbers
                             if numbers.count(number) > 1})
        if duplicates:
            raise GroupError("保留行的核实户号重复：%s" % "、".join(duplicates))

        taken = accepted_numbers(connection, batch_id)
        clash = sorted(set(numbers) & taken)
        if clash:
            raise GroupError("核实户号已被本批正式答卷占用：%s"
                             % "、".join(clash))

        reserved = pending_reserved_numbers(connection, batch_id, group_id)
        clash = sorted(set(numbers) & reserved)
        if clash:
            raise GroupError("核实户号仍被其他待核组占用：%s"
                             % "、".join(clash))

        for row in rows:
            verified_no = resolved[row["id"]]
            if verified_no is None:
                connection.execute(
                    "UPDATE pending_rows SET decision = 'discard', "
                    "verified_no = NULL, household_id = NULL WHERE id = ?",
                    (row["id"],))
                continue
            cursor = connection.execute(
                "INSERT INTO households (batch_id, household_no, row_number) "
                "VALUES (?, ?, ?)",
                (batch_id, verified_no, row["row_number"]))
            household_id = cursor.lastrowid
            answers = _answers_from_json(connection.execute(
                "SELECT answers FROM pending_rows WHERE id = ?",
                (row["id"],)).fetchone()["answers"])
            connection.executemany(
                "INSERT OR REPLACE INTO answers "
                "(household_id, prefix, qid, value) VALUES (?, ?, ?, ?)",
                [(household_id, _prefix_key(prefix), qid, value)
                 for (prefix, qid), value in answers.items()])
            connection.execute(
                "UPDATE pending_rows SET decision = 'keep', verified_no = ?, "
                "household_id = ? WHERE id = ?",
                (verified_no, household_id, row["id"]))

        connection.execute(
            "UPDATE pending_groups SET status = ?, "
            "decided_at = datetime('now', 'localtime') WHERE id = ?",
            (GROUPS_ACCEPTED, group_id))
    return [row["id"] for row in rows]


def db_path_from_env():
    return os.environ.get("SURVEY_DB", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "survey.sqlite3"))
