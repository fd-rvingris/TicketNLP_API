"""Email boilerplate cleaning tests. All names and text are made up."""

from text_clean import PATTERNS, clean_text, protected, removed_chars

BANNER = ("CAUTION: This email originated from outside of the organization. Do not click links or open "
          "attachments unless you recognize the sender and know the content is safe.")
FOOTER = ("This message is intended only for the use of the addressee and may contain information that is "
          "privileged and confidential.\r\nAny disclosure, copying, distribution or use of this message is prohibited.")


def clean(desc, subject=""):
    return clean_text(subject, desc)[1]


def test_banner_paragraph_and_subject_tag_are_removed():
    s, d = clean_text("[EXTERNAL] Disk space on FAKESQL01", f"{BANNER}\r\n\r\nThe log drive is full.")
    assert s == "Disk space on FAKESQL01"
    assert d == "The log drive is full."
    assert clean("This message came from outside your organization.\r\n\r\nThe job failed.") == "The job failed."


def test_footer_runs_to_the_end_of_its_paragraph_and_following_disclaimer_paragraphs():
    text = ("Please check the backup.\r\n\r\n" + FOOTER + "\r\n\r\nIf you received this message in error, notify "
            "the sender; unauthorized disclosure is prohibited.")
    assert clean(text) == "Please check the backup."
    # an ordinary paragraph after the footer stays
    assert clean("CONFIDENTIALITY NOTICE: privileged.\r\n\r\nAlso the restore is slow.") == "Also the restore is slow."
    assert clean("This e-mail and any attachments are confidential.\r\n\r\nNext step: call us.") == "Next step: call us."


def test_image_links_and_urls_are_removed_but_link_text_stays():
    text = ("See the screenshot ![](https://fake.example.com/a.png) [cid:image001.png@01DA0000.00000000] "
            "image002.jpg and the [runbook](https://fake.example.com/runbook) at www.fake.example.com/x")
    assert clean(text) == "See the screenshot    and the runbook at"


def test_quoted_history_is_cut_unless_the_reply_is_almost_empty():
    reply = "Please restore the Fake Co database to yesterday.\r\n\r\n"
    history = "From: Jane Doe\r\nSent: Monday\r\nTo: Support\r\nSubject: old thread\r\n\r\nOlder words here."
    assert clean(reply + history) == "Please restore the Fake Co database to yesterday."
    assert clean(reply + "On Mon, Jan 5, 2026 at 9:00 AM Jane Doe wrote:\r\n> older text") \
        == "Please restore the Fake Co database to yesterday."
    assert "Older words here." in clean("See below.\r\n\r\n" + history)


def test_standalone_greeting_and_sign_off_with_signature_are_removed():
    text = "Hi team,\r\n\r\nThe nightly job did not run.\r\n\r\nThanks,\r\nJane Doe\r\nData Engineer | Fake Co"
    assert clean(text) == "The nightly job did not run."
    # a greeting that carries the request is kept
    line = "Hi team, can you give the reporting service account read access?"
    assert clean(line) == line


def test_mailbox_name_and_mentions_are_removed():
    assert clean("Hello Fortified Data Support team, @Jane Doe please look at the slow query.") \
        == "Hello  team,  please look at the slow query."


def test_lines_with_servers_sql_or_error_numbers_are_never_removed():
    assert protected("Thanks, FAKESQL01 is back") and protected("GRANT SELECT ON dbo.Fake")
    assert protected("Login failed, Error 18456") and protected("CORP\\svc-fake")
    assert not protected("Thanks,")
    text = "The login fails.\r\n\r\nCONFIDENTIALITY NOTICE: login failed with Error: 18456 on this host"
    assert "Error: 18456" in clean(text)
    # a T-SQL variable is not a mention
    assert "@Today" in clean("DECLARE @Today date = GETDATE();")
    history = "Reply with enough words to keep cutting.\r\n\r\nFrom: Jane\r\nSent: Monday\r\nSELECT * FROM dbo.Fake"
    assert "SELECT * FROM dbo.Fake" in clean(history)


def test_never_returns_empty_text_when_the_input_had_words():
    assert clean(BANNER) == BANNER
    s, d = clean_text("[EXTERNAL]", "Thanks,")
    assert (s, d) == ("[EXTERNAL]", "Thanks,")
    assert removed_chars("", BANNER) == {"fallback": 1}
    assert clean_text("", "") == ("", "")


def test_crlf_and_non_breaking_spaces():
    text = "Hi team,\r\n\r\nThe disk is full.\r\n\r\nRegards,\r\n"
    assert clean(text) == "The disk is full."
    assert "\r" not in clean("one\r\ntwo")


def test_patterns_can_be_chosen_and_counted():
    text = f"{BANNER}\r\n\r\nThe disk is full.\r\n\r\nThanks,"
    assert clean_text("", text, patterns=("signoff",))[1].startswith("CAUTION")
    r = removed_chars("", text)
    assert set(r) == {"banner", "signoff"} and set(r) <= set(PATTERNS)
