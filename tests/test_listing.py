"""Finding sessions: search across partitions, and limits on the listing."""

from conftest import record


def test_search_finds_titles_in_every_partition_including_archived(two_orgs, capsys):
    w = two_orgs
    w.add_session(w.a, "3333aaaa-0000-4000-8000-000000000003", "cli-a-2",
                  title="Sports card prototypes", isArchived=True)
    w.add_session(w.b, "4444bbbb-0000-4000-8000-000000000004", "cli-b-2",
                  title="Livestream SPORTS widget designs")

    assert w.run("sessions", "-s", "sports") == 0
    out = capsys.readouterr().out
    assert "3333aaaa" in out and "4444bbbb" in out
    assert "work in A" not in out and "work in B" not in out
    assert "2 of 2 matching" in out


def test_search_terms_match_any_title_or_id_prefix(two_orgs, capsys):
    w = two_orgs
    w.run("sessions", "-s", "local_1111aaaa", "-s", "in B")
    out = capsys.readouterr().out
    assert "1111aaaa" in out and "2222bbbb" in out


def test_search_respects_partition_and_limit(two_orgs, capsys):
    w = two_orgs
    w.run("sessions", "-s", "work", "-p", w.ORG_A[:8])
    out = capsys.readouterr().out
    assert "1111aaaa" in out and "2222bbbb" not in out

    w.run("sessions", "-s", "work", "-n", "1")
    assert "1 of 2 matching" in capsys.readouterr().out


def test_search_with_no_match_says_so(two_orgs, capsys):
    assert two_orgs.run("sessions", "-s", "nothing like this") == 0
    assert "no session in 2 partition(s) matches" in capsys.readouterr().out


def test_limit_applies_to_partition_listing(two_orgs, capsys):
    w = two_orgs
    w.add_session(w.a, "3333aaaa-0000-4000-8000-000000000003", "cli-a-2", title="newer in A",
                  lastActivityAt=1_700_000_200_000)
    w.run("sessions", "-p", w.ORG_A[:8], "-n", "1")
    out = capsys.readouterr().out
    assert "newer in A" in out and "work in A" not in out


def test_search_filters_host_listing(two_orgs, capsys):
    w = two_orgs
    w.add_wsl_transcript("/home/me/a", "sports-1", records=[record("/home/me/a")], title="sports cards")
    w.add_wsl_transcript("/home/me/b", "other-1", records=[record("/home/me/b")], title="kafka audit")
    w.run("sessions", "-H", "wsl:Testbuntu", "-s", "sports")
    out = capsys.readouterr().out
    assert "sports-1" in out and "other-1" not in out
