import pytest

from persistbot import allowlist
from persistbot.allowlist import Rejected, parse


def argv(command):
    """The words of a single command."""
    pipeline = parse(command)
    assert len(pipeline.commands) == 1 and pipeline.stdout is None
    return pipeline.commands[0].argv


def stages(command):
    return [item.argv for item in parse(command).commands]


# the list ---------------------------------------------------------------


@pytest.mark.parametrize("name", allowlist.COMMANDS)
def test_every_listed_command_is_allowed(name):
    assert argv(f"{name} arg") == [name, "arg"]
    assert argv(name) == [name]


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
    assert argv(command)[0] == argv0


@pytest.mark.parametrize(
    "command",
    ["ls.evil", "ls/../rm x", "zip2 a", "ggzip a", "unzipx a", "gzipped"],
)
def test_wildcard_names_do_not_overmatch(command):
    with pytest.raises(Rejected, match="not an allowed command"):
        parse(command)


@pytest.mark.parametrize(
    "command",
    ["/bin/ls", "/usr/bin/rm -rf x", "./ls", "../ls", "~/ls", "bin/ls"],
)
def test_commands_given_as_paths_are_rejected(command):
    with pytest.raises(Rejected, match="bare command name"):
        parse(command)


@pytest.mark.parametrize(
    "command",
    ["python3 x.py", "bash -c ls", "sh", "curl http://x", "env ls", "lsx.sh", "RM x",
     "rmx", "catalog", "'python3' x", '"bash" -c ls', "pyth\\on3"],
)
def test_commands_not_on_the_list_are_rejected(command):
    with pytest.raises(Rejected, match="not an allowed command"):
        parse(command)


@pytest.mark.parametrize("command", ["", "   ", "''", '""'])
def test_empty_commands_are_rejected(command):
    with pytest.raises(Rejected):
        parse(command)


@pytest.mark.parametrize(
    "command", ["sudo cat /var/log/messages", "sudo", "sudo -u root ls", "ls | sudo tail"]
)
def test_sudo_is_rejected_by_name(command):
    with pytest.raises(Rejected, match="sudo is not available"):
        parse(command)


def test_a_quoted_command_name_is_judged_by_what_it_says():
    assert argv("'ls' -la") == ["ls", "-la"]
    assert argv('"rm" x') == ["rm", "x"]
    assert argv("l\\s") == ["ls"]


def test_rejection_message_lists_what_is_allowed():
    with pytest.raises(Rejected) as info:
        parse("python3 x.py")
    assert "'python3' is not an allowed command" in str(info.value)
    assert "grep" in str(info.value) and "ls*" in str(info.value)


# control characters -----------------------------------------------------


def test_design_doc_counterexample_newline_then_rm():
    with pytest.raises(Rejected, match="line breaks"):
        parse("ls\nrm\x20/tmp/important")


@pytest.mark.parametrize(
    "command",
    ["ls\trm", "ls\r\nrm x", "ls \x00", "ls \x7f", "ls \x1b[0m", "grep 'a\nb' f", 'grep "a\tb" f'],
)
def test_control_characters_are_rejected_even_inside_quotes(command):
    with pytest.raises(Rejected, match="control characters"):
        parse(command)


# pipelines --------------------------------------------------------------


def test_a_pipeline_is_split_into_its_commands():
    assert stages("ps aux | grep nginx") == [["ps", "aux"], ["grep", "nginx"]]
    assert stages("ps aux --sort=-%mem|head -10") == [["ps", "aux", "--sort=-%mem"], ["head", "-10"]]
    assert stages("cat a | sort | uniq -c | sort -rn | head -5") == [
        ["cat", "a"], ["sort"], ["uniq", "-c"], ["sort", "-rn"], ["head", "-5"],
    ]


@pytest.mark.parametrize(
    "command",
    [
        "ps aux | python3 -c 1",
        "cat /etc/passwd | sh",
        "ls | /bin/rm x",
        "python3 x | grep a",
        "ls | grep a | bash",
        "ls | xargs rm",
    ],
)
def test_every_command_in_a_pipeline_must_be_allowed(command):
    with pytest.raises(Rejected, match="not an allowed command"):
        parse(command)


@pytest.mark.parametrize("command", ["ls |", "| ls", "ls | | grep a", "ls || grep a", "|"])
def test_malformed_pipelines_are_rejected(command):
    with pytest.raises(Rejected):
        parse(command)


def test_pipeline_length_is_limited():
    assert len(stages(" | ".join(["cat"] * allowlist.MAX_COMMANDS))) == allowlist.MAX_COMMANDS
    with pytest.raises(Rejected, match="at most"):
        parse(" | ".join(["cat"] * (allowlist.MAX_COMMANDS + 1)))


def test_a_pipe_inside_quotes_is_text():
    assert argv("grep 'a|b' f") == ["grep", "a|b", "f"]
    assert argv('grep -E "x|y" f') == ["grep", "-E", "x|y", "f"]
    assert argv("grep a\\|b f") == ["grep", "a|b", "f"]


# redirection ------------------------------------------------------------


def test_stderr_redirects_belong_to_their_command():
    pipeline = parse("find / -name app.conf 2>/dev/null | grep etc 2>&1")
    assert [(c.argv[0], c.stderr) for c in pipeline.commands] == [("find", "null"), ("grep", "stdout")]
    assert parse("ls 2> /dev/null").commands[0].stderr == "null"
    assert parse("ls").commands[0].stderr == "show"
    assert stages("ls 2>&1|grep x") == [["ls"], ["grep", "x"]]


def test_stdout_redirect_on_the_last_command():
    pipeline = parse("ps aux | grep nginx > found.txt")
    assert (pipeline.stdout, pipeline.append) == ("found.txt", False)
    pipeline = parse("ls >>'my log.txt' 2>&1")
    assert (pipeline.stdout, pipeline.append) == ("my log.txt", True)
    assert pipeline.commands[0].argv == ["ls"] and pipeline.commands[0].stderr == "stdout"
    assert parse("ls 1>/dev/null").stdout == "/dev/null"
    assert parse("ls a2>out").commands[0].argv == ["ls", "a2"]  # only a bare 2 is a stream
    assert parse("ls '3'>out").commands[0].argv == ["ls", "3"]   # a quoted number is a word
    assert parse("ls 3 >out").commands[0].argv == ["ls", "3"]


def test_redirections_apply_left_to_right():
    after = parse("cat missing > out 2>&1")
    assert after.stdout == "out" and after.commands[0].stderr == "stdout"
    before = parse("cat missing 2>&1 > out")
    assert before.stdout == "out" and before.commands[0].stderr == "show"


@pytest.mark.parametrize(
    "command",
    [
        "ls > a | grep x",       # > before the end of a pipeline
        "ls > a > b",
        "ls >",
        "ls > ''",
        "ls 2> errors.txt",      # stderr only to /dev/null
        "ls 2>> /dev/null",
        "ls 2>&2",
        "ls >&2",
        "ls 2>&1x",
        "ls &> out",
        "cat 3>report.txt",      # a stream other than 1 or 2
        "cat 0> x",
        "cat 10>x",
        "cat < /etc/passwd",
        "cat << EOF",
    ],
)
def test_other_redirections_are_rejected(command):
    with pytest.raises(Rejected):
        parse(command)


def test_a_redirect_inside_quotes_is_text():
    assert argv("grep '>' f") == ["grep", ">", "f"]
    assert argv('awk "NR>2" f') == ["awk", "NR>2", "f"]


# what only a shell could do ---------------------------------------------


@pytest.mark.parametrize(
    "command, fragment",
    [
        ("ls; rm -rf x", "; is not available"),
        ("ls ;rm x", "; is not available"),
        ("ls && rm x", "&& is not available"),
        ("ls & ", "& is not available"),
        ("ls &", "& is not available"),
        ("cat $(which ls)", r"\$\(\.\.\.\) is not available"),
        ('cat "$(which ls)"', r"\$\(\.\.\.\) is not available"),
        ("cat `which ls`", "backticks"),
        ('cat "`which ls`"', "backticks"),
        ("cat $HOME/x", "variables"),
        ('cat "$HOME/x"', "variables"),
        ("cat ${HOME}/x", "variables"),
        ("ls $1", "variables"),
        ("ls $?", "variables"),
        ("ls | grep $USER", "variables"),
    ],
)
def test_shell_only_syntax_is_rejected_with_a_reason(command, fragment):
    with pytest.raises(Rejected, match=fragment):
        parse(command)


def test_single_quotes_pass_everything_through_as_text():
    assert argv("awk '{print $1}' f") == ["awk", "{print $1}", "f"]
    assert argv("grep 'a;b&c`d$(e)<f' x") == ["grep", "a;b&c`d$(e)<f", "x"]
    assert argv("awk -F: '$3 >= 1000 && $1 != \"x\" {print $1; n++}' /etc/passwd")[2] == (
        '$3 >= 1000 && $1 != "x" {print $1; n++}'
    )


def test_a_dollar_that_is_not_a_variable_is_text():
    assert argv('grep "error$" f') == ["grep", "error$", "f"]
    assert argv("grep ^a$ f") == ["grep", "^a$", "f"]
    assert argv('grep "cost \\$5" f') == ["grep", "cost $5", "f"]
    assert argv("grep \\$HOME f") == ["grep", "$HOME", "f"]


# quoting ----------------------------------------------------------------


def test_quoting_is_read_as_a_shell_would():
    assert argv("grep -r 'two words' /var/log") == ["grep", "-r", "two words", "/var/log"]
    assert argv('find . -name "*.log"') == ["find", ".", "-name", "*.log"]
    assert argv("  ls   -la  ") == ["ls", "-la"]
    assert argv("ls a\\ b") == ["ls", "a b"]
    assert argv("grep 'it'\\''s' f") == ["grep", "it's", "f"]
    assert argv('grep "say \\"hi\\"" f') == ["grep", 'say "hi"', "f"]
    assert argv('grep "a\\nb" f') == ["grep", "a\\nb", "f"]
    assert argv("ls '' x") == ["ls", "", "x"]
    assert argv("ls pre'mid'\"post\"") == ["ls", "premidpost"]


@pytest.mark.parametrize("command", ["grep 'oops /var/log", 'grep "oops /var/log', "ls \\"])
def test_unfinished_quoting_is_rejected(command):
    with pytest.raises(Rejected, match="parse"):
        parse(command)
