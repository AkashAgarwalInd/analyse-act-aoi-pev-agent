"""Bundle ../skills/analyse-act into the package so the runner's stage prompts come from the skill itself.

From the repo, the skill sits next to this directory. In an unpacked sdist it was already copied to
src/aa_agent/skill, where the wheel's package glob picks it up.
"""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        skill = Path(self.root).parent / "skills" / "analyse-act"
        if not skill.is_dir():
            return
        dest = "src/aa_agent/skill" if self.target_name == "sdist" else "aa_agent/skill"
        build_data["force_include"][str(skill)] = dest
