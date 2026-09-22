

def test_a_failure_says_why_where_it_will_be_read():
    """"task execution raised" is true of every failure and useful for none.
    A run that stopped because its branch does not exist said exactly that to
    a log file on the host, and nothing to the page someone was watching."""
    from agent_core.runtime.daemon import _why

    clone = RuntimeError(
        "git clone failed (128): Cloning into 'sample-service'...\n"
        "warning: Could not find remote branch ticket-100-bugfix to clone.\n"
        "fatal: Remote branch ticket-100-bugfix not found in upstream origin"
    )
    said = _why(clone)
    assert "git clone failed (128)" in said
    assert "Remote branch ticket-100-bugfix not found" in said
    assert len(said) <= 400

    # A one-line failure is not repeated back to itself.
    assert _why(ValueError("no repo_url in the state")).count("no repo_url") == 1
    # ...and one with nothing to say still names its type.
    assert "KeyError" in _why(KeyError())
