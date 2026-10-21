import subprocess
import zipfile

from mcpsmith.skills_path import skills_dir

SKILLS = ["build-mcp-server", "design-mcp-tools", "extract-api"]


def test_skills_dir_has_all_skills():
    for name in SKILLS:
        text = (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.split("---")[1].count(f"name: {name}\n") == 1


def test_wheel_contains_skills(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path)], check=True, capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    for name in SKILLS:
        assert f"mcpsmith/skills/{name}/SKILL.md" in names
