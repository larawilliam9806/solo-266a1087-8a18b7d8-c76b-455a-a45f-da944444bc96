import html
import io
import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DB", str(tmp_path / "survey.sqlite3"))
    monkeypatch.setenv("SURVEY_SECRET", "test")
    for module in ("storage", "engine", "importer", "main"):
        sys.modules.pop(module, None)
    import main
    main.app.config.update(TESTING=True)
    with main.app.test_client() as test_client:
        yield test_client


CSV_TEXT = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）
H1,自购,喷药机,2,无,,,12000
H2,租赁,喷药机,2,有,按年整租,3,8000
H3,自购,,,,,,9000
H4,租赁,,,有,按季短租,,,
"""

JUMPS = {
    "order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
    "titles": {
        "Q1": "设备来源", "Q2": "自购设备类型", "Q3": "自购台数",
        "Q4": "是否享受购置补贴", "Q5": "租赁方式", "Q6": "租赁台数",
        "Q7": "年费用合计（元）",
    },
    "types": {"Q1": "single", "Q2": "single", "Q4": "single",
              "Q5": "single", "Q3": "number", "Q6": "number",
              "Q7": "number"},
    "options": {"Q1": ["自购", "租赁"], "Q2": ["喷药机", "采摘平台"],
                "Q4": ["有", "无"], "Q5": ["按年整租", "按季短租"]},
    "jumps": {"Q1": {"自购": "Q2", "租赁": "Q5"},
              "Q2": {"采摘平台": "Q7"},
              "Q4": {"有": "Q7", "无": "Q7"}},
}


def upload(client, csv_text=CSV_TEXT, jumps=JUMPS):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(jumps).encode("utf-8")),
                      "jumps.json"),
        "batch_name": "测试批次",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    return response


def form_fields(body):
    """(qid, prefix, value) for every question rendered on the edit page."""
    entries = []
    pattern = re.compile(
        r'name="k_qid_(\d+)"\s+value="([^"]*)">\s*'
        r'<input\s+type="hidden"\s+name="k_prefix_\1"\s+value="([^"]*)"',
        re.S)
    for index, qid, prefix in pattern.findall(body):
        tags = re.findall(r'<input[^>]*name="v_%s"[^>]*>' % index, body, re.S)
        chosen = next((tag for tag in tags if "checked" in tag),
                      tags[0] if tags else "")
        value_match = re.search(r'data-value="([^"]*)"', chosen)
        entries.append((html.unescape(qid), json.loads(html.unescape(prefix)),
                        html.unescape(value_match.group(1)) if value_match
                        else ""))
    return entries


def values(body):
    return {(qid, tuple(prefix)): value
            for qid, prefix, value in form_fields(body)}


def submit(client, entries, household=2):
    data = {}
    for position, (qid, prefix, value) in enumerate(entries, start=1):
        data["k_qid_%d" % position] = qid
        data["k_prefix_%d" % position] = json.dumps(list(prefix))
        data["v_%d" % position] = value
    return client.post("/batch/1/household/%d" % household, data=data,
                       follow_redirects=True).get_data(as_text=True)


def replace(entries, qid, prefix, value):
    return [(q, tuple(p), value if (q == qid and tuple(p) == tuple(prefix))
             else v) for q, p, v in entries]


SELF = ("Q1", "Q2", "Q3", "Q4")
RENT = ("Q1", "Q5", "Q6")


def test_import_and_list(client):
    body = upload(client).get_data(as_text=True)
    assert "测试批次" in body
    for household in ("H1", "H2", "H3", "H4"):
        assert household in body


def test_self_buy_path_and_shared_cost(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/1").get_data(as_text=True))
    assert [qid for qid, _, _ in fields] == ["Q1", "Q2", "Q3", "Q4", "Q7"]
    assert dict((q, v) for q, _, v in fields)["Q7"] == "12000"


def test_rental_path_skips_self_buy_questions(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    assert [qid for qid, _, _ in fields] == ["Q1", "Q5", "Q6", "Q7"]
    assert dict((q, v) for q, _, v in fields)["Q7"] == "8000"


def test_import_ignores_unreached_answers(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    h2 = next(line for line in text.splitlines() if line.startswith("H2"))
    cells = h2.split(",")
    # H2 is a rental household; its self-buy columns must not be imported.
    assert cells[header.index("Q2 自购设备类型")] == ""
    assert cells[header.index("Q3 自购台数")] == ""
    assert cells[header.index("Q4 是否享受购置补贴")] == ""
    assert cells[header.index("当前路径")] == "Q1 → Q5 → Q6 → Q7"
    assert cells[header.index("Q5 租赁方式")] == "按年整租"


def test_switch_branch_saves_separate_answers(client):
    upload(client)
    # H2 starts on the rental route.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "喷药机",
        "k_qid_3": "Q3", "k_prefix_3": '["Q1", "Q2"]', "v_3": "4",
        "k_qid_4": "Q4", "k_prefix_4": '["Q1", "Q2", "Q3"]', "v_4": "无",
        "k_qid_5": "Q7", "k_prefix_5": '["Q1", "Q2", "Q3", "Q4"]',
        "v_5": "15000",
    }, follow_redirects=True).get_data(as_text=True)

    assert "其他路径上保存的答案" in body
    current = values(body)
    assert current[("Q1", ())] == "自购"
    assert current[("Q7", SELF)] == "15000"
    # The rental path's answers are still stored.
    assert current[("Q5", ("Q1",))] if ("Q5", ("Q1",)) in current else True
    assert "按年整租" in body and "8000" in body

    # Switch back to rental: its own answers return, including its cost.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert current[("Q5", ("Q1",))] == "按年整租"
    assert current[("Q6", ("Q1", "Q5"))] == "3"
    assert current[("Q7", RENT)] == "8000"


def test_shared_cost_question_is_per_path(client):
    upload(client)
    # Change the rental household's cost.
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q7", RENT, "9000"))
    assert values(body)[("Q7", RENT)] == "9000"

    # Move to self-buy: the cost shows the self-buy value, not 9000.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "喷药机",
        "k_qid_3": "Q3", "k_prefix_3": '["Q1", "Q2"]', "v_3": "4",
        "k_qid_4": "Q4", "k_prefix_4": '["Q1", "Q2", "Q3"]', "v_4": "无",
        "k_qid_5": "Q7", "k_prefix_5": '["Q1", "Q2", "Q3", "Q4"]',
        "v_5": "",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert current[("Q7", SELF)] == ""
    assert "9000" in body  # still visible as another path's answer

    # Return to rental: 9000 is intact.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True).get_data(as_text=True)
    assert values(body)[("Q7", RENT)] == "9000"


def test_stale_fields_of_old_route_are_not_saved(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    # Keep every rental field but change the route: they are now stale.
    body = submit(client, [("Q1", (), "自购")] + fields[1:])
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2"]
    assert current[("Q2", ("Q1",))] == ""


def test_invalid_single_option_rejected_and_previous_answers_kept(client):
    upload(client)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "丢弃",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "按年整租" in export and "8000" in export


def test_export_lists_effective_answers_and_first_missing(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    lines = text.strip().splitlines()
    header = lines[0].split(",")
    assert header[0] == "户号"
    assert "当前路径" in header
    assert "Q7 年费用合计（元）" in header
    assert header[-2] == "首个漏答题"

    h2 = next(line for line in lines if line.startswith("H2"))
    cells = h2.split(",")
    assert cells[header.index("首个漏答题")] == ""
    assert cells[header.index("Q1 设备来源")] == "租赁"
    assert cells[header.index("Q5 租赁方式")] == "按年整租"
    assert cells[header.index("Q7 年费用合计（元）")] == "8000"


def test_missing_answer_is_reported(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    h3 = next(line for line in text.splitlines() if line.startswith("H3"))
    cells = h3.split(",")
    assert cells[header.index("Q1 设备来源")] == "自购"
    assert cells[header.index("首个漏答题")] == "Q2"
    assert cells[header.index("漏答题干")] == "自购设备类型"
    # Nothing after the first blank is imported, even though Q7 was filled.
    assert cells[header.index("Q7 年费用合计（元）")] == ""

    h4 = next(line for line in text.splitlines() if line.startswith("H4"))
    cells = h4.split(",")
    assert cells[header.index("Q5 租赁方式")] == "按季短租"
    assert cells[header.index("首个漏答题")] == "Q6"


def test_clearing_answer_stops_path(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q5", ("Q1",), ""))
    assert [qid for qid, _, _ in form_fields(body)] == ["Q1", "Q5"]


def test_jump_directly_to_shared_cost_question(client):
    upload(client)
    # 自购 + 采摘平台 jumps straight to Q7, skipping Q3/Q4.
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "采摘平台",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2", "Q7"]
    assert current[("Q7", ("Q1", "Q2"))] == ""


def test_save_failure_keeps_last_saved_answers(client, monkeypatch):
    upload(client)
    import main
    original = main.storage.save_answers
    monkeypatch.setattr(main.storage, "save_answers",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("disk full")))
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
    }, follow_redirects=True)
    assert "disk full" in response.get_data(as_text=True)

    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h2 = next(line for line in export.splitlines() if line.startswith("H2"))
    assert "租赁" in h2 and "8000" in h2
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q1", ())] == "租赁"
    assert values(page)[("Q7", RENT)] == "8000"
    monkeypatch.setattr(main.storage, "save_answers", original)


def test_database_error_keeps_download_on_last_saved(client, monkeypatch):
    upload(client)
    import main

    def explode(connection, household_id, changes):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main.storage, "save_answers", explode)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h2 = next(line for line in export.splitlines() if line.startswith("H2"))
    assert "租赁" in h2 and "8000" in h2


def test_batch_route_returns_404_for_missing_batch(client):
    assert client.get("/batch/99").status_code == 404
    assert client.get("/batch/1/household/99").status_code == 404


def test_import_rejects_bad_json(client):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(CSV_TEXT.encode()), "answers.csv"),
        "json_file": (io.BytesIO(b"{not json"), "jumps.json"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "导入失败" in response.get_data(as_text=True)


def test_import_rejects_bad_jump_target(client):
    jumps = dict(JUMPS, jumps={"Q1": {"租赁": "Q99"}})
    response = upload(client, jumps=jumps)
    assert "导入失败" in response.get_data(as_text=True)


# --- 重号核定 -------------------------------------------------------------

FULL_JUMPS = [
    {"id": "Q1", "title": "设备来源", "type": "single",
     "options": ["自购", "租赁"], "jumps": {"租赁": "Q5"}},
    "Q2", "Q3", "Q4", "Q5", "Q6", "Q7",
]
DUP_CSV = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）
H1,自购,喷药机,2,无,,,12000
H2,租赁,喷药机,2,有,按年整租,3,8000
H1,租赁,,,有,按季短租,2,6000
"""


def full_jumps():
    """The same questionnaire as JUMPS, but in complete-list form."""
    return [
        {"id": "Q1", "title": "设备来源", "type": "single",
         "options": ["自购", "租赁"], "jumps": {"自购": "Q2", "租赁": "Q5"}},
        {"id": "Q2", "title": "自购设备类型", "type": "single",
         "options": ["喷药机", "采摘平台"], "jumps": {"采摘平台": "Q7"}},
        {"id": "Q3", "title": "自购台数", "type": "number"},
        {"id": "Q4", "title": "是否享受购置补贴", "type": "single",
         "options": ["有", "无"], "jumps": {"有": "Q7", "无": "Q7"}},
        {"id": "Q5", "title": "租赁方式", "type": "single",
         "options": ["按年整租", "按季短租"]},
        {"id": "Q6", "title": "租赁台数", "type": "number"},
        {"id": "Q7", "title": "年费用合计（元）", "type": "number"},
    ]


def upload_dup(client, csv_text=DUP_CSV):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(full_jumps()).encode("utf-8")),
                      "jumps.json"),
        "batch_name": "重号批次",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def pending_ids(client):
    """``[(group_id, [row_id, ...])]`` straight from the review page form."""
    body = client.get("/batch/1").get_data(as_text=True)
    groups = re.findall(
        r'href="/batch/1/pending/(\d+)">', body)
    out = []
    for group_id in groups:
        page = client.get("/batch/1/pending/%s" % group_id).get_data(
            as_text=True)
        row_ids = sorted(set(re.findall(r'name="row_(\d+)_decision"', page)),
                         key=int)
        out.append((int(group_id), [int(value) for value in row_ids]))
    return out


def decide(client, group_id, entries, action="accept"):
    data = {}
    for row_id, decision, number in entries:
        data["row_%d_decision" % row_id] = decision
        data["row_%d_no" % row_id] = number
    data["action"] = action
    return client.post("/batch/1/pending/%d" % group_id, data=data,
                       follow_redirects=True).get_data(as_text=True)


def test_duplicates_are_held_out_of_editor_and_export(client):
    body = upload_dup(client)
    assert "重号待核" in body and "H1" in body
    # Only H2 is an editable household.
    page = client.get("/batch/1").get_data(as_text=True)
    assert "H2" in page
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "H2" in export
    assert not any(line.startswith("H1") for line in export.splitlines())
    assert "12000" not in export and "6000" not in export


def test_review_page_lists_row_numbers_and_clean_answers(client):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    assert len(rows) == 2
    page = client.get("/batch/1/pending/%d" % group_id).get_data(as_text=True)
    assert "第 1 行" in page and "第 3 行" in page
    assert "Q1 → Q2 → Q3 → Q4 → Q7" in page   # self-buy row
    assert "Q1 → Q5 → Q6 → Q7" in page        # rental row
    # The rental row's stray self-buy columns were cleaned away.
    detail = client.get(
        "/batch/1/pending/%d/row/%d" % (group_id, rows[1])).get_data(
        as_text=True)
    assert "按季短租" in detail and "6000" in detail


def test_group_needs_every_row_decided(client):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    body = decide(client, group_id, [(rows[0], "keep", "H1")])
    assert "还没有决定" in body
    # Still pending: nothing has joined the batch.
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert not any(line.startswith("H1") for line in export.splitlines())


def test_accepting_keeps_and_discards_together(client):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    body = decide(client, group_id, [
        (rows[0], "keep", "H1"),
        (rows[1], "keep", "H9"),
    ])
    assert "已核定" in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert any(line.startswith("H1") for line in export.splitlines())
    assert any(line.startswith("H9") for line in export.splitlines())
    # Both rows are now editable with their own cleaned answers.
    page = client.get("/batch/1").get_data(as_text=True)
    assert "H1" in page and "H9" in page
    household_ids = re.findall(r'/batch/1/household/(\d+)', page)
    answers = {}
    for household_id in household_ids:
        detail = client.get(
            "/batch/1/household/%s" % household_id).get_data(as_text=True)
        answers[re.search(r"<h1>户号 (\S+)</h1>", detail).group(1)] = detail
    assert "12000" in answers["H1"]
    assert "6000" in answers["H9"]


def test_discarded_row_never_appears(client):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    decide(client, group_id, [
        (rows[0], "keep", "H1"),
        (rows[1], "discard", ""),
    ])
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert any(line.startswith("H1") for line in export.splitlines())
    assert "6000" not in export
    page = client.get("/batch/1/pending/%d" % group_id).get_data(as_text=True)
    assert "已弃用" in page


def test_kept_numbers_must_be_distinct(client):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    body = decide(client, group_id, [
        (rows[0], "keep", "H7"),
        (rows[1], "keep", "H7"),
    ])
    assert "核实户号重复" in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "H7" not in export


def test_kept_number_cannot_take_a_settled_household(client):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    body = decide(client, group_id, [
        (rows[0], "keep", "H2"),   # H2 is a normal row of this batch
        (rows[1], "discard", ""),
    ])
    assert "已被本批正式答卷占用" in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert export.count("H2") == 1


def test_two_groups_contending_for_same_number(client):
    csv_text = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）
H1,自购,喷药机,2,无,,,12000
H1,租赁,,,有,按季短租,2,6000
H5,自购,喷药机,1,有,,,9000
H5,租赁,,,有,按年整租,3,7000
"""
    upload_dup(client, csv_text)
    groups = pending_ids(client)
    assert len(groups) == 2
    first_id, first_rows = groups[0]
    second_id, second_rows = groups[1]

    body = decide(client, first_id, [
        (first_rows[0], "keep", "H5"),   # H5 is the other group's original no
        (first_rows[1], "discard", ""),
    ])
    assert "其他待核组占用" in body

    body = decide(client, first_id, [
        (first_rows[0], "keep", "H8"),
        (first_rows[1], "discard", ""),
    ])
    assert "已核定" in body

    # The second group also wants H8: it loses, and stays pending.
    body = decide(client, second_id, [
        (second_rows[0], "keep", "H8"),
        (second_rows[1], "discard", ""),
    ])
    assert "已被本批正式答卷占用" in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert export.count("H8") == 1

    body = decide(client, second_id, [
        (second_rows[0], "keep", "H5"),
        (second_rows[1], "discard", ""),
    ])
    assert "已核定" in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert export.count("H5") == 1


def test_failed_write_leaves_group_pending(client, monkeypatch):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    import main
    monkeypatch.setattr(
        main.storage, "accept_pending_group",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk full")))
    body = decide(client, group_id, [
        (rows[0], "keep", "H1"), (rows[1], "keep", "H9")])
    assert "核定未生效" in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert not any(line.startswith("H9") for line in export.splitlines())
    page = client.get("/batch/1/pending/%d" % group_id).get_data(as_text=True)
    assert "待核" in page


def test_saved_decisions_survive_but_do_not_apply(client):
    upload_dup(client)
    group_id, rows = pending_ids(client)[0]
    body = decide(client, group_id, [
        (rows[0], "keep", "H1"), (rows[1], "keep", "H9")], action="save")
    assert "尚未生效" in body
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "H9" not in export
    page = client.get("/batch/1/pending/%d" % group_id).get_data(as_text=True)
    assert 'value="H9"' in page
