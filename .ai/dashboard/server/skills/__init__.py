"""Skill + agent definition trees: filesystem scanners and dual-tree mirroring.

``config`` (was ``skills_config``) scans the .claude/skills and .agents/skills
trees; ``tree`` (was ``skill_tree``) keeps the two trees in sync — mirror a
.claude skill into .agents and vice versa. No primary module: the package just
groups the two skill-domain engines that used to be flat ``server.*`` modules.
"""
