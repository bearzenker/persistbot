import pytest

from persistbot import allowlist
from persistbot.allowlist import Rejected, check


@pytest.mark.parametrize("name", allowlist.COMMANDS)
def test_every_listed_command_is_allowed(name):
    assert check(f"{name} arg") == [name, "arg"]
    assert check(name) == [name]


def test_the_list_is_the_design_docs():
    assert len(allowlist.COMMANDS) == 33
    assert allowlist.WILDCARDS == ("ls*", "?zip", "?unzip")


@pytest.mark.parametrize(
    "command, argv0",
    [
        ("lsblk -f", "lsblk"),
        ("lscpu", "lscpu"),
        ("ls_x-1", "ls_x-1"),
        ("gzip -k a.log", "gzip"),
        ("bzip a", "bzip"),
        ("gunzip a.gz", "gunzip"),
    ],
)
def test_wildcard_names_match(command, argv0):
    assert check(command)[0] == argv0


@pytest.mark.parametrize(
    "command",
    [
        "ls.evil",        # the wildcard part is name characters only
        "ls/../rm x",
        "zip2 a",         # anchored on the right
        "ggzip a",        # ? is exactly one character
        "unzipx a",
        "gzipped",
        "lsblk;id",
    ],
)
def test_wildcard_names_do_not_overmatch(command):
    with pytest.raises(Rejected):
        check(command)


def test_design_doc_counterexample_newline_then_rm():
    with pytest.raises(Rejected, match="line breaks"):
        check("ls\nrm\x20/tmp/important")


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd | grep root",
        "ls; rm -rf x",
        "ls && rm x",
        "ls & ",
        "cat $(which ls)",
        "cat `which ls`",
        "cat $HOME/x",
        "cat < /etc/passwd",
        "ls > out.txt",
        "ls\trm",
        "ls\r\nrm x",
        "ls \x00",
        "ls \x7f",
        "ls \x1b[0m",
    ],
)
def test_shell_syntax_and_control_characters_are_rejected(command):
    with pytest.raises(Rejected):
        check(command)


@pytest.mark.parametrize(
    "command",
    ["/bin/ls", "/usr/bin/rm -rf x", "./ls", "../ls", "~/ls", "bin/ls"],
)
def test_commands_given_as_paths_are_rejected(command):
    with pytest.raises(Rejected, match="not an allowed command"):
        check(command)


@pytest.mark.parametrize(
    "command",
    ["python3 x.py", "bash -c ls", "sh", "curl http://x", "env ls", "lsx.sh", "RM x", "", "   ",
     "'ls' -la", '"rm" x', "l\\s", "rmx", "catalog"],
)
def test_commands_not_on_the_list_are_rejected(command):
    with pytest.raises(Rejected):
        check(command)


@pytest.mark.parametrize("command", ["sudo cat /var/log/messages", "sudo", "sudo -u root ls"])
def test_sudo_is_rejected_by_name(command):
    with pytest.raises(Rejected, match="sudo"):
        check(command)


def test_quoted_arguments_are_split_like_a_shell_would():
    assert check("grep -r 'two words' /var/log") == ["grep", "-r", "two words", "/var/log"]
    assert check('find . -name "*.log"') == ["find", ".", "-name", "*.log"]
    assert check("  ls   -la  ") == ["ls", "-la"]


def test_unbalanced_quote_is_rejected():
    with pytest.raises(Rejected, match="parse"):
        check("grep 'oops /var/log")


def test_rejection_message_lists_what_is_allowed():
    with pytest.raises(Rejected) as info:
        check("python3 x.py")
    assert "'python3' is not an allowed command" in str(info.value)
    assert "grep" in str(info.value) and "ls*" in str(info.value)
