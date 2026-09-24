import os
from pathlib import Path
import pytest
from agilecode.adapters.workspace import Workspace,digest,READ_LIMIT
from agilecode.adapters.search import SearchService
from agilecode.adapters.git import GitRepository
from agilecode.adapters.guard import CommandGuard
from agilecode.adapters.skills import SkillRegistry
from agilecode.core.types import AccessDenied,AgileCodeError,Conflict


@pytest.fixture
def scope(tmp_path):
    project=tmp_path/"project";work=project/".agilecode/worktrees/ticket";global_=tmp_path/"global"
    work.mkdir(parents=True);global_.mkdir();(work/"src").mkdir()
    return Workspace(work,project,global_)


@pytest.mark.parametrize("path,space,write",[
    ("../outside","workspace",True),("../../sibling/a.py","workspace",True),
    (".git/config","workspace",False),(".env","workspace",False),
    (".agilecode/agilecode.db","project",False),(".agilecode/worktrees/other/x","project",False),
    ("settings.json","global",True),("app.py","project",True),("/etc/passwd","project",False),
])
def test_scoped_paths_deny(scope,path,space,write):
    with pytest.raises(AccessDenied):scope.resolve(path,space,write=write)


def test_read_global_project_and_own_worktree(scope):
    (scope.global_root/"settings.json").write_text("{}")
    (scope.project/"README.md").write_text("project context")
    (scope.root/"app.py").write_text("own code")
    assert scope.read("settings.json","global")["content"]=="{}"
    assert scope.read("README.md","project")["content"]=="project context"
    assert scope.read("app.py")["content"]=="own code"


def test_parent_symlink_cannot_escape(scope,tmp_path):
    (scope.root/"escape").symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(AccessDenied):scope.write("escape/outside.txt","bad",None,True)
    assert not (tmp_path/"outside.txt").exists()


def test_exact_edits_hash_crlf_utf8_and_creation(scope):
    path=scope.root/"script.py";raw='text = "λ😀"\r\nnumber = 1\r\n'.encode();path.write_bytes(raw)
    read=scope.read("script.py")
    assert read["hash"]==digest(raw)
    result=scope.edit("script.py",read["hash"],[{"old":"number = 1","new":"number = 2","expected_count":1}])
    assert path.read_bytes()=='text = "λ😀"\r\nnumber = 2\r\n'.encode()
    with pytest.raises(Conflict):scope.write("script.py","lost",read["hash"],False)
    scope.write("new.txt","new\n",None,True)
    with pytest.raises(Conflict):scope.write("new.txt","bad\n",None,True)
    assert result["hash"]==digest(path.read_bytes())


def test_ambiguous_replacement_is_atomic(scope):
    path=scope.root/"a.txt";path.write_text("same same")
    with pytest.raises(Conflict):scope.edit("a.txt",digest(path.read_bytes()),[{"old":"same","new":"other","expected_count":1}])
    assert path.read_text()=="same same"


def test_binary_unchanged_and_long_line_labeled_partial(scope):
    (scope.root/"binary").write_bytes(b"x\x00y")
    with pytest.raises(AgileCodeError):scope.read("binary")
    (scope.root/"long.txt").write_text("a"*(READ_LIMIT+5))
    assert scope.read("long.txt")["partial"] is True


@pytest.mark.parametrize("command,expected",[
    ("cd src && pwd","allow"),("cd ..","deny"),("cd /tmp","deny"),
    ("git status","allow"),("git commit -am bad","deny"),("git -C .. status","deny"),
    ("git --git-dir=/tmp/repo status","deny"),("export GIT_WORK_TREE=/tmp","deny"),
    ("echo bad > ../outside","deny"),("echo yes > output.txt","allow"),
    ("cp app.py ../copy.py","deny"),("cp app.py src/copy.py","allow"),
    ("mv ../original app.py","deny"),("rm -rf ../../outside","deny"),
    ("echo ok | tee ../outside","deny"),("git diff --output=../outside","deny"),
    ("python -m unittest discover -s tests -q","allow"),("python -c 'print(1)'","ask"),
    ("for x in a b; do echo $x; done","ask"),("sleep 2 &","deny"),
    ("echo hello >&2","allow"),("export MY_VALUE=yes","allow"),("export HOME=/tmp","deny"),
    ("builtin git commit -m bad","deny"),("echo $(pwd)","ask"),
])
def test_practical_command_guard(scope,command,expected):
    assert CommandGuard(scope).validate(command,scope.root).decision==expected


@pytest.mark.asyncio
async def test_lexical_path_regex_ast_and_reference_search(repository,tmp_path):
    scope=Workspace(repository,repository,tmp_path/"global");search=SearchService(scope,GitRepository(repository))
    assert (await search.search("answer","symbol"))["matches"][0]["symbol"]=="answer"
    assert any(m["path"]=="app.py" for m in (await search.search("app.py","path"))["matches"])
    assert (await search.search(r"return\s+1","regex"))["matches"]
    assert (await search.search("answer","references"))["matches"][0]["kind"]=="reference_candidate"
    result=await search.search("a","text",limit=1)
    assert result["partial"]
    with pytest.raises(AgileCodeError):await search.search("[","regex")


def test_skills_provenance_overrides_and_errors(scope):
    global_=scope.global_root/"skills/example";project=scope.project/".agilecode/skills/example"
    for root,text in ((global_,"global instructions"),(project,"project instructions")):
        root.mkdir(parents=True);(root/"SKILL.md").write_text("---\nname: example\ndescription: Test skill\n---\n"+text)
    bad=scope.global_root/"skills/broken";bad.mkdir();(bad/"SKILL.md").write_text("No metadata")
    registry=SkillRegistry(scope.global_root,scope.project)
    assert registry.load("example",["example"])["instructions"]=="project instructions"
    assert any(r.get("error") for r in registry.catalog(["*"]))
    assert any(r.get("overridden") for r in registry.catalog(["*"]))
    with pytest.raises(AccessDenied):registry.load("example",[])
