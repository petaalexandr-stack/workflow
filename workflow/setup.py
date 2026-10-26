"""Build-only asset mirroring; agents/*.md is the single source of role prompts."""
from pathlib import Path
from shutil import copy2, copytree
from setuptools import setup
from setuptools.command.build_py import build_py


class BundleAgentFiles(build_py):
    def run(self):
        super().run()
        project = Path(__file__).parent.resolve()
        destination = Path(self.build_lib) / "workflow_engine" / "config"
        destination.mkdir(parents=True, exist_ok=True)
        copy2(project / "AGENTS.md", destination / "AGENTS.md")
        copytree(project / "agents", destination / "agents", dirs_exist_ok=True, ignore=None)
        skill_dir = destination / "skills" / "workflow-ideas"
        skill_dir.mkdir(parents=True, exist_ok=True)
        copy2(project / ".agents" / "skills" / "workflow-ideas" / "SKILL.md", skill_dir / "SKILL.md")


setup(cmdclass={"build_py": BundleAgentFiles})
