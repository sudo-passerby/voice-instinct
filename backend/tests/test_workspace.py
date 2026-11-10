from workspace import private_workdir


def test_each_errand_gets_a_private_folder_under_the_workspace_root(tmp_path):
    d = private_workdir("Write a snake game please now", "abcdef123456", tmp_path / "ws")
    assert d.is_dir() and d.parent == (tmp_path / "ws").resolve()
    assert d.name == "write-a-snake-game-abcdef"


def test_empty_or_symbol_only_task_still_gets_a_valid_name(tmp_path):
    assert private_workdir("???", "abcdef123456", tmp_path).name == "task-abcdef"
